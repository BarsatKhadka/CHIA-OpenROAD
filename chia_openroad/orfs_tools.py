"""The agent's entire surface. Everything it can do is in this file.

The architecture the proposal argues for is: *the agent proposes, programmatic
evaluation decides.* That only means anything if the split is enforced in code,
so this module is deliberately narrow. It exposes seven tools, and none of them
can:

* run ``drc`` or ``lvs``, or influence their verdict
* parse a report, or supply a metric the agent computed itself
* set a knob outside the calibrated legal set
* pass ``force_rebuild``, ``allow_unknown_knobs``, or ``extra_make_args``
* decide whether a configuration is feasible — only ORFS decides that

``run_stage`` is never reachable. The agent proposes a knob dict and reads
summaries; execution, invalidation, parsing, signoff and the feasibility
verdict all stay on this side of the line.

This mirrors what CHIA's own case studies do — the ISA study walls the agent
off from Spike co-simulation, the critical-path study re-synthesizes with no AI
involved before accepting anything.

**Why start/poll rather than a blocking call.** ``timing_opt`` documents the
reason: a long tool call exceeds the MCP HTTP timeout, so the tool returns a
handle in sub-seconds and the agent polls. Our flows take 20-200 s, well past
it. ``propose_candidate`` therefore dispatches and returns an id;
``candidate_status`` waits, bounded.
"""

from __future__ import annotations

import logging
import os
import uuid

import ray
from chia.base.tools.ChiaTool import ChiaTool

from chia_openroad.candidate_store import CandidateStore
from chia_openroad.failure_log import FailureLog
from chia_openroad.knob_policy import KnobPolicy
from chia_openroad.openroad import DEFAULT_GATE_STAGE, OpenROADNode, run_flow

logger = logging.getLogger(__name__)

#: Cap on a single poll when the tool is reached over MCP, where the HTTP
#: round trip must not stall (timing_opt suggests <~200 s).
MAX_POLL_SECONDS = 180

#: Cap when the tool is called in-process by a loop we drive ourselves. There
#: is no HTTP round trip to hold open, so a poll can simply wait for the build
#: instead of returning "still running" twenty times — which is what led one
#: agent to conclude the build system was broken and stop.
MAX_POLL_SECONDS_LOCAL = 5400


#: Cap on a single poll when the tool is reached over MCP, where the HTTP
#: round trip must not stall (timing_opt suggests <~200 s).
MAX_POLL_SECONDS = 180

#: Cap when the tool is called in-process by a loop we drive ourselves. There
#: is no HTTP round trip to hold open, so a poll can simply wait for the build
#: instead of returning "still running" twenty times — which is what led one
#: agent to conclude the build system was broken and stop.
MAX_POLL_SECONDS_LOCAL = 5400


@ray.remote(num_cpus=0)
def _run_candidate(work_home: str, design_config: str, knobs: dict,
                   gate: str | None, target: str, orfs_home: str | None,
                   branch_from: str | None = None,
                   branch_through: str = "place",
                   measure_clock: bool = False,
                   num_cores: int | None = None):
    """Driver-side orchestrator for one candidate.

    ``num_cpus=0`` because this holds no resources itself — it reserves an
    :class:`OpenROADNode` bundle, and *that* is what consumes an ``orfs`` slot.
    Without this the placement group would wait behind its own orchestrator.
    """
    from chia.base.ChiaFunction import get
    kw = {"orfs_home": orfs_home} if orfs_home else {}
    if num_cores:
        kw["num_cores"] = num_cores
    with OpenROADNode() as node:
        if branch_from:
            # Seed from the shared prefix so make resumes at the first stage
            # after the branch point. Without this every candidate rebuilds an
            # identical placement, and a screen that picks better candidates
            # saves nothing at all.
            get(node.branch.chia_remote(branch_from, work_home, design_config,
                                        through_stage=branch_through, **kw))
            # Note: branch_through bounds what is copied. Copying a finished
            # parent wholesale would be wrong — the later stages belong to the
            # parent's knobs, and invalidation would have to delete them again.

        def run(stage, **inner):
            r = get(node.run_stage.chia_remote(stage, **inner, **kw))
            if measure_clock and r.success and stage in ("route", "finish"):
                clock = get(node.measure_clock.chia_remote(
                    inner["work_home"], inner["design_config"], stage="route", **kw))
                r.clock_metrics = clock
                r.summary.update(clock)
            return r

        return run_flow(run, work_home, design_config, knobs, gate=gate, target=target)


class ORFSAgentTool(ChiaTool):
    """MCP tools for an agent driving ORFS. See the module docstring for what
    is deliberately absent."""

    # Defined as setup() with NO __init__ on purpose. ChiaTool.__init_subclass__
    # only auto-brackets setup() with ChiaTool.__init__ (before) and
    # __post_init__ (after) for a subclass that defines setup() and no
    # __init__. Writing both means neither setup() nor __post_init__ runs: the
    # MCP server never starts, `hostname` stays None, no tools are registered,
    # and the LLM fails with an opaque "unhandled errors in a TaskGroup"
    # because there is nothing to connect to.
    def setup(self, *, policy: KnobPolicy, store: CandidateStore,
              failures: FailureLog, design_config: str, work_root: str,
              arm: str = "agent", gate: str | None = DEFAULT_GATE_STAGE,
              orfs_home: str | None = None, branch_from: str | None = None,
              branch_through: str = "place", screen: dict | None = None,
              measure_clock: bool = False, parallel_slots: int = 1,
              local_calls: bool = False):
        #: In-flight candidates: id -> ObjectRef.
        #:
        #: A plain dict, deliberately. An earlier version kept these in a Ray
        #: actor so the driver and the tool's pickled MCP copy could share
        #: them — but ray.get resolves nested ObjectRefs, so the actor handed
        #: back a finished result list where a ref was expected
        #:   TypeError: wait() expected a list of ray.ObjectRef ... got list
        #: The sharing is no longer needed: the loop drives the tool in-process
        #: (see chia_openroad/iterate.py), so one copy owns the refs it made.
        self._pending: dict[int, object] = {}
        #: Distinguishes this run's work directories from a previous run's.
        #: Without it, a fresh ledger restarts candidate ids at 1 while
        #: /tmp/<root>/cand-00001 still holds a completed tree from last time —
        #: make finds it up to date and "builds" it in a second, silently
        #: recycling an old result as a new one. Observed exactly that.
        self.run_token = uuid.uuid4().hex[:8]
        #: Shared prefix every candidate seeds from, if the loop built one.
        self.branch_from = branch_from
        self.branch_through = branch_through
        #: A PRECOMPUTED ranking, not a live model:
        #:   {"name":..., "metric":..., "better":"lower"|"higher",
        #:    "cost_s":..., "ranked":[(knobs, value), ...]}
        #:
        #: The surrogate screens against a *fixed* placement, so its ordering
        #: cannot change during the loop — computing it once on the driver is
        #: equivalent and avoids shipping a fitted model into the tool's Ray
        #: actor. It could not go there anyway: a ChiaTool is pickled to reach
        #: its actor, and the actor has no import path for SwiftCTS.
        self.screen = screen
        self.measure_clock = measure_clock
        #: Threads each candidate may use. One `orfs` slot is about one core,
        #: so a candidate must not claim the whole machine: three candidates
        #: each taking NUM_CORES=4 on a 4-core box drove load to 12.9 and made
        #: every one of them roughly three times slower for no gain.
        import os as _os
        self.num_cores = max(1, (_os.cpu_count() or 1) // max(parallel_slots, 1))
        #: True when a driver calls these methods directly rather than over
        #: MCP, which removes the HTTP timeout constraint on polling.
        self.local_calls = local_calls
        #: How many candidates the cluster can build at once. The agent is told,
        #: so it overlaps proposals instead of serialising them.
        self.parallel_slots = parallel_slots
        self.policy = policy
        self.store = store
        self.failures = failures
        self.design_config = design_config
        self.work_root = work_root
        self.arm = arm
        self.gate = gate
        self.orfs_home = orfs_home

        tools = [self.list_legal_knobs, self.propose_candidate,
                 self.candidate_status, self.list_candidates,
                 self.compare_candidates, self.past_failures,
                 self.best_candidate]
        if self.screen:
            tools.append(self.screen_candidates)
        for fn in tools:
            self.mcp.add_tool(fn, name=f"{self.name}_{fn.__name__}")

    # ------------------------------------------------------------------ #
    def list_legal_knobs(self) -> str:
        """List every knob you may set, its stage, and its allowed range.

        Proposing anything outside this list, or outside a listed range, will be
        rejected without running. Ranges marked NOMINAL have not been measured
        on this design, so a value inside one may still fail to build.
        """
        return self.policy.describe_for_agent()

    def propose_candidate(self, knobs: dict, parent_id: int = 0) -> str:
        """Propose one configuration and start building it.

        Returns immediately with a candidate id; the flow takes about an hour.

        Args:
            knobs: ORFS knob names to values, e.g. {"CTS_CLUSTER_SIZE": 20}.
                These are the FULL configuration, not a delta — anything you
                omit takes this design's default, whatever the parent used.
            parent_id: build starting from that candidate's design state
                instead of from the shared placement. 0 means start from the
                shared placement. Use it to say "this is a variation on #14";
                it records the lineage so the exploration is a visible tree.
                Note it rarely saves time on this design — routing dominates,
                and almost any knob change forces a full re-route.
        """
        parent = int(parent_id) or None
        cid = self.store.propose(knobs, arm=self.arm, parent_id=parent)

        ok, reason = self.policy.check(knobs)
        if not ok:
            self.store.reject(cid, reason)
            return f"candidate {cid} REJECTED: {reason}"

        prior = self.failures.known_bad(knobs)
        if prior:
            hint = prior["failure"].get("message", "previously failed")
            self.store.reject(cid, f"already known to fail: {hint}")
            return (f"candidate {cid} NOT RUN: this exact configuration failed "
                    f"before — {hint}. Try a different one.")

        seen = self.store.seen(knobs)
        if seen and seen.status == "built":
            self.store.reject(cid, f"duplicate of #{seen.id}")
            return f"candidate {cid} NOT RUN: identical to #{seen.id}, which built. {seen.one_line()}"

        # Branch from the named parent's tree when given one, else the shared
        # placement. Stage invalidation then discards whatever the new knobs
        # made stale, so an "early" knob change simply rebuilds more.
        seed = self.branch_from
        if parent:
            prior = self.store.get(parent)
            if prior and prior.status == "built":
                seed = f"{self.work_root}/{self.run_token}-cand-{parent:05d}"
        ref = _run_candidate.remote(f"{self.work_root}/{self.run_token}-cand-{cid:05d}",
                                    self.design_config, knobs, self.gate,
                                    "finish", self.orfs_home,
                                    seed, self.branch_through,
                                    self.measure_clock, self.num_cores)
        self._pending[cid] = ref
        return (f"candidate {cid} started with {knobs}. A full build takes "
                f"around an hour. Up to {self.parallel_slots} candidates run at "
                f"once, so propose the others you want now and poll them all "
                f"afterwards rather than waiting on this one.")

    def candidate_status(self, candidate_id: int, max_wait_seconds: int = 60) -> str:
        """Check a candidate, optionally waiting for it.

        Args:
            candidate_id: id from propose_candidate.
            max_wait_seconds: block up to this long (capped at 180) before
                reporting back. Use a short wait to interleave other work.
        """
        ref = self._pending.get(candidate_id)
        if ref is None:
            existing = self.store.get(candidate_id)
            return existing.one_line() if existing else f"no candidate {candidate_id}"

        ceiling = MAX_POLL_SECONDS_LOCAL if self.local_calls else MAX_POLL_SECONDS
        wait = max(0, min(int(max_wait_seconds), ceiling))
        ready, _ = ray.wait([ref], timeout=wait)
        if not ready:
            # Report what stage it has reached, not just that it is alive.
            # "still running" repeated twenty times reads as a hung system: an
            # agent given only that concluded the build system was broken and
            # stopped. Progress reads as progress.
            return (f"candidate {candidate_id} {self._progress(candidate_id)}. "
                    f"A full build takes around an hour — propose other "
                    f"candidates while this one runs rather than waiting on it.")

        self._pending.pop(candidate_id, None)
        try:
            results = ray.get(ref)
        except Exception as exc:                       # worker died, preempted, etc
            self.store.reject(candidate_id, f"run error: {exc}")
            return f"candidate {candidate_id} errored: {exc}"

        self.store.record(candidate_id, results)
        for r in results:
            self.failures.record(r)
        return self.store.get(candidate_id).one_line()

    def pending_ids(self) -> list:
        """Candidates still in flight — for a caller draining after the agent
        stops. Not exposed as an MCP tool; this is the loop's business."""
        return sorted(self._pending)

    def _progress(self, cid: int) -> str:
        """Which stage a running candidate has reached, from its checkpoints."""
        import glob
        work = f"{self.work_root}/{self.run_token}-cand-{cid:05d}"
        found = glob.glob(os.path.join(work, "results", "*", "*", "*", "*.odb"))
        if not found:
            return "is starting up"
        reached = max(os.path.basename(f) for f in found)
        stage = {"1": "synthesis", "2": "floorplan", "3": "placement",
                 "4": "clock tree synthesis", "5": "routing",
                 "6": "finishing"}.get(reached[0], reached)
        return f"is running: reached {stage} ({reached})"

    def list_candidates(self, limit: int = 20) -> str:
        """Everything tried so far, newest first, with outcomes."""
        rows = self.store.list(limit=limit)
        if not rows:
            return "nothing tried yet"
        return "\n".join(c.one_line() for c in rows)

    def compare_candidates(self, metric: str = "worst_slack") -> str:
        """Rank the candidates that built, by one metric.

        Args:
            metric: worst_slack, tns, power_total, instance_area, die_area,
                clock_skew_setup, clock_buffer_count, utilization.
        """
        built = [c for c in self.store.list(status="built", limit=1000)
                 if isinstance((c.metrics or {}).get(metric), (int, float))]
        if not built:
            return f"no built candidate has {metric}"
        # Slack: larger is better. Power/area/skew: smaller is better.
        maximize = metric in ("worst_slack", "tns", "hold_worst_slack")
        built.sort(key=lambda c: c.metrics[metric], reverse=maximize)
        head = f"ranked by {metric} ({'higher' if maximize else 'lower'} is better):"
        return head + "\n" + "\n".join(
            f"  {c.metrics[metric]:>12.6g}  #{c.id} "
            f"{', '.join(f'{k}={v}' for k, v in sorted(c.knobs.items())) or '(defaults)'}"
            for c in built)

    def best_candidate(self, metric: str = "worst_slack") -> str:
        """The best candidate so far by one metric, with its full summary."""
        maximize = metric in ("worst_slack", "tns", "hold_worst_slack")
        c = self.store.best(metric, maximize=maximize)
        if not c:
            return f"no built candidate has {metric}"
        metrics = ", ".join(f"{k}={v:.6g}" for k, v in sorted((c.metrics or {}).items())
                            if isinstance(v, (int, float)))
        return f"{c.one_line()}\n  all metrics: {metrics}"

    def screen_candidates(self, count: int = 5) -> str:
        """Which configurations a fast model predicts will do well.

        These are ESTIMATES, not measurements. They cost milliseconds rather
        than a build, so they are worth using to decide what to build — but only
        a real run settles anything. Propose the promising ones with
        propose_candidate and check them.

        Args:
            count: how many to return, best first.
        """
        sc = self.screen
        ranked = sc["ranked"][:max(1, int(count))]
        head = (f"{sc['name']} ranked {len(sc['ranked'])} configurations by "
                f"predicted {sc['metric']} ({sc['better']} is better) in "
                f"{sc['cost_s']:.2f}s. These are predictions, not results:")
        lines = [f"  {value:>12.6g}  "
                 f"{', '.join(f'{k}={v}' for k, v in sorted(knobs.items()))}"
                 for knobs, value in ranked]
        return head + "\n" + "\n".join(lines)

    def past_failures(self, limit: int = 10) -> str:
        """Configurations already known not to build, and why.

        Read this before proposing: values near a known failure are likely to
        fail too, and a repeat costs a full run for no information.
        """
        hints = self.failures.hints(limit=limit)
        return "\n".join(hints) if hints else "nothing has failed yet"
