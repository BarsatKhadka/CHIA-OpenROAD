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
from chia_openroad.knob_specs import KNOB_STAGE, STAGE_ORDER
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

#: Wall-clock cap on a single ORFS stage, passed down to run_stage.
#:
#: run_stage's own default is 86400 — a backstop against a wedged process, not
#: a scheduling policy. One candidate showed why that is too loose:
#: CORE_ASPECT_RATIO=0.7 with CORE_UTILIZATION=45 on aes gives a narrow, densely
#: packed die that detailed routing cannot close. It sat at "90% with 243
#: violations" for 3h26m while the other three candidates finished in 28-53
#: min, and an iteration cannot advance until every candidate returns, so one
#: such proposal stalls the whole loop. At the 24h default, for a day.
#:
#: 5400s is ~3x the observed median aes build (~1840s). A stage past that is
#: not close to converging, and "did not route in 90 minutes" is a true and
#: useful thing for the agent to learn about a floorplan.
STAGE_TIMEOUT_SECONDS = 5400



def _breaks_anchor(cfg: dict, observes_stage: str | None) -> list[str]:
    """Knobs in *cfg* that change the very state a surrogate reads.

    A surrogate with ``observes_stage="place"`` predicts *from a finished
    placement* — SwiftCTS reads that placement's DEF and timing report. The
    placement it actually read is the shared one every candidate branches from,
    built at the design's default knobs. So a configuration that changes
    anything at or before ``place`` would produce a *different* placement, and
    the prediction describes a design that will not exist.

    This is not hypothetical: every candidate in a 12-candidate aes run set
    CORE_UTILIZATION, so every prediction was anchored to a placement none of
    them would build, and nothing said so. The number is still indicative —
    the model is fitted on real designs — but the agent has to know it is
    reasoning about a stand-in.
    """
    if not observes_stage or observes_stage not in STAGE_ORDER:
        return []
    limit = STAGE_ORDER.index(observes_stage)
    return sorted(k for k in cfg
                  if k in KNOB_STAGE and STAGE_ORDER.index(KNOB_STAGE[k]) <= limit)


@ray.remote(num_cpus=0)
def _run_candidate(work_home: str, design_config: str, knobs: dict,
                   gate: str | None, target: str, orfs_home: str | None,
                   branch_from: str | None = None,
                   branch_through: str = "place",
                   measure_clock: bool = False,
                   num_cores: int | None = None,
                   stage_timeout: int = STAGE_TIMEOUT_SECONDS):
    """Driver-side orchestrator for one candidate.

    ``num_cpus=0`` because this holds no resources itself — it reserves an
    :class:`OpenROADNode` bundle, and *that* is what consumes an ``orfs`` slot.
    Without this the placement group would wait behind its own orchestrator.
    """
    from chia.base.ChiaFunction import get
    # Two argument sets on purpose: `branch` and `measure_clock` take neither
    # knobs nor a thread count, and passing one is a TypeError that surfaces
    # only at run time inside a Ray task.
    kw = {"orfs_home": orfs_home} if orfs_home else {}
    run_kw = dict(kw)
    if num_cores:
        run_kw["num_cores"] = num_cores
    if stage_timeout:
        run_kw["timeout_seconds"] = stage_timeout
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
            r = get(node.run_stage.chia_remote(stage, **inner, **run_kw))
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
              local_calls: bool = False, surrogate=None, surrogates=None,
              state=None):
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
        #: The LIVE fitted model, when one is reachable — which is only when a
        #: driver calls these methods in-process. Over MCP the tool is pickled
        #: into a Ray actor with no import path for SwiftCTS, so it stays None
        #: and `predict_knobs` is simply not registered.
        #:
        #: The precomputed `screen` above answers "what looks good?" over a
        #: fixed grid. It cannot answer "what about THIS one?" for a config the
        #: agent invented, which is the question worth asking before spending
        #: ~30 min on a build.
        #: MANY, not one. A design has stages, and a user plugging models in
        #: will have different ones for different stages — a floorplan
        #: feasibility model, a CTS wirelength model, a routing congestion
        #: model. Each declares `observes_stage` and `domain`, so each can be
        #: asked about the knobs it actually speaks to and stays silent on the
        #: rest. `surrogate=` (singular) is accepted as a convenience.
        given = list(surrogates or ([surrogate] if surrogate is not None else []))
        self.surrogates = [s for s in given if s is not None]
        #: Kept for callers that only ever had one.
        self.surrogate = self.surrogates[0] if self.surrogates else None
        self.state = state
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
        if self.surrogates and self.state is not None:
            tools += [self.describe_surrogates, self.predict_knobs]
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
                With parent_id set these are a DELTA on that candidate: its
                knobs are inherited and yours override them, so listing one
                knob means "that configuration, with this one changed". With
                no parent they are the full configuration and anything omitted
                takes the design's default.
            parent_id: derive from that candidate. 0 means start fresh from
                the shared placement. It records the lineage so the search
                reads as a tree, and it is what makes refinement possible.
        """
        parent = int(parent_id) or None
        # Inherit the parent's knobs under the delta. Without this a
        # refinement collapses to a near-default configuration: the agent
        # writes {"from": 6, "CTS_BUF_DISTANCE": 70} meaning "#6 with this one
        # knob changed", and every other knob #6 set silently reverts. Measured
        # on cb_picorv32, where #6 itself carried one knob instead of its
        # parent's four, and #9 and #10 each carried exactly one. Every
        # "refinement" in the v2 and v3 runs was in fact a fresh
        # near-default build, which is why refining the leader did not behave
        # like refining anything.
        if parent:
            prow = self.store.get(parent)
            inherited = dict(getattr(prow, "knobs", None) or {}) if prow else {}
            if inherited:
                knobs = {**inherited, **dict(knobs)}
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

    # A fitted model must never enter the pickle. ChiaTool is serialised to
    # reach its MCP actor at construction time -- before any in-process call --
    # and that actor has no import path for a plugin's dependencies:
    #   ModuleNotFoundError: No module named 'swiftcts'
    # raised inside _ToolServerActor's deserialize, killing the run at startup.
    # Being called in-process later does not help: the pickle already happened.
    #
    # So the driver's copy holds the models and answers predict_knobs; the
    # actor's copy has none and simply does not offer that tool. Same object,
    # different reach, which is the honest shape -- a model that cannot be
    # shipped should not pretend to be available where it cannot run.
    def __getstate__(self):
        state = super().__getstate__()
        state["surrogates"] = []
        state["surrogate"] = None
        state["state"] = None
        return state

    def describe_surrogates(self) -> str:
        """What fast models are available, what each reads, and what it predicts.

        Read this before trusting predict_knobs. A model that observes a stage
        predicts *from a finished run of that stage* — so it is only fully
        valid for configurations that do not change that stage or anything
        before it.
        """
        if not self.surrogates:
            return "no fast models are available; every judgement needs a build."
        lines = []
        for sur in self.surrogates:
            stage = sur.observes_stage
            preds = ", ".join(f"{k} ({v})" for k, v in
                              sorted((getattr(sur, "predicts", {}) or {}).items()))
            lines.append(f"{sur.name}:")
            lines.append(f"  predicts: {preds or '(nothing declared)'}")
            if stage:
                lines.append(f"  reads a finished {stage} stage"
                             + (f", specifically: {', '.join(sur.requires)}"
                                if getattr(sur, "requires", ()) else ""))
                after = STAGE_ORDER[STAGE_ORDER.index(stage) + 1:] \
                    if stage in STAGE_ORDER else []
                lines.append(f"  so it is fully valid only for knobs that act "
                             f"after {stage}"
                             + (f" ({', '.join(after)})" if after else "")
                             + f". Change {stage} or earlier and it predicts "
                               f"from a stand-in.")
            else:
                lines.append("  needs no design state; valid for any configuration")
            dom = getattr(sur, "domain", {}) or {}
            if dom:
                lines.append("  fitted ranges: " + ", ".join(
                    f"{k} {v[0]:g}..{v[1]:g}" for k, v in sorted(dom.items())))
                lines.append("  it is blind to every other knob, and will "
                             "predict identically as they vary")
        return "\n".join(lines)

    def predict_knobs(self, knobs) -> str:
        """Ask the fast models what they expect for configurations YOU choose.

        Costs milliseconds instead of ~30 minutes, so use it to narrow a set of
        ideas before spending a build on one. Unlike screen_candidates, which
        ranks a fixed grid decided in advance, this answers for the exact
        configurations you pass.

        Several models may answer, each speaking only for the stage it observes
        — one may predict clock wirelength, another whether the design builds
        at all. Each reports the knobs it ignores; two configurations differing
        only in an ignored knob will predict identically, and a tie is not a
        decision.

        Args:
            knobs: one knob dict, or a list of them (max 20 per call).
        """
        if not self.surrogates:
            # The actor's copy, or a loop wired without models.
            return ("no fast model is reachable here; every judgement needs a "
                    "real build via propose_candidate.")
        if isinstance(knobs, dict):
            batch = [knobs]
        elif isinstance(knobs, (list, tuple)):
            batch = list(knobs)
        else:
            return "knobs must be a dict or a list of dicts."
        if not batch:
            return "no configurations given."
        if len(batch) > 20:
            return f"too many at once ({len(batch)}); pass at most 20."

        clean, problems = [], []
        for i, cfg in enumerate(batch):
            if not isinstance(cfg, dict):
                problems.append(f"[{i}] not a knob dict")
                continue
            ok, reason = self.policy.check(cfg)
            if not ok:
                problems.append(f"[{i}] {reason}")
                continue
            clean.append(cfg)
        if not clean:
            return "nothing predictable:\n" + "\n".join(f"  {p}" for p in problems)

        # Ask each model once for the whole batch, then report per configuration
        # so the agent compares like with like.
        answers, declined = {}, []
        for sur in self.surrogates:
            try:
                answers[sur.name] = (sur, sur.predict(self.state, clean))
            except Exception as exc:
                declined.append(f"{sur.name} could not answer: {exc}")

        if not answers:
            return "no model could answer:\n" + "\n".join(f"  {d}" for d in declined)

        lines = [f"{len(answers)} model(s) consulted "
                 f"(estimates, not measurements):"]
        for idx, cfg in enumerate(clean):
            lines.append("  " + ", ".join(f"{k}={v}" for k, v in sorted(cfg.items())))
            for name, (sur, preds) in answers.items():
                p = preds[idx]
                stage = sur.observes_stage or "any stage"
                vals = ", ".join(f"{k}={v:.6g}" for k, v in sorted(p.values.items()))
                lines.append(f"      {name} [{stage}] -> "
                             f"{vals or '(nothing it predicts)'}")
                dom = getattr(sur, "domain", {}) or {}
                ignored = sorted(set(cfg) - set(dom))
                if ignored and dom:
                    lines.append(f"          ignores: {', '.join(ignored)}")
                outside = sorted(k for k in set(cfg) & set(dom)
                                 if not (dom[k][0] <= float(cfg[k]) <= dom[k][1]))
                if outside:
                    lines.append(f"          OUTSIDE its fitted range, "
                                 f"extrapolated: {', '.join(outside)}")
                stale = _breaks_anchor(cfg, sur.observes_stage)
                if stale:
                    lines.append(
                        f"          NOTE: it reads a finished "
                        f"{sur.observes_stage}, but {', '.join(stale)} would "
                        f"change that {sur.observes_stage}. Predicted from the "
                        f"current one — indicative, not specific to this config.")
        if declined:
            lines += [f"  {d}" for d in declined]
        if problems:
            lines.append("could not predict:")
            lines += [f"  {p}" for p in problems]
        lines.append("Only a real build settles anything — propose_candidate to confirm.")
        return "\n".join(lines)

    def past_failures(self, limit: int = 10) -> str:
        """Configurations already known not to build, and why.

        Read this before proposing: values near a known failure are likely to
        fail too, and a repeat costs a full run for no information.
        """
        hints = self.failures.hints(limit=limit)
        return "\n".join(hints) if hints else "nothing has failed yet"
