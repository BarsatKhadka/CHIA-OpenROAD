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

import ray
from chia.base.tools.ChiaTool import ChiaTool

from chia_openroad.candidate_store import CandidateStore
from chia_openroad.failure_log import FailureLog
from chia_openroad.knob_policy import KnobPolicy
from chia_openroad.openroad import DEFAULT_GATE_STAGE, OpenROADNode, run_flow

logger = logging.getLogger(__name__)

#: Keep a poll well inside the MCP HTTP timeout (timing_opt suggests <~200 s).
MAX_POLL_SECONDS = 180


@ray.remote(num_cpus=0)
def _run_candidate(work_home: str, design_config: str, knobs: dict,
                   gate: str | None, target: str, orfs_home: str | None,
                   branch_from: str | None = None,
                   branch_through: str = "place",
                   measure_clock: bool = False):
    """Driver-side orchestrator for one candidate.

    ``num_cpus=0`` because this holds no resources itself — it reserves an
    :class:`OpenROADNode` bundle, and *that* is what consumes an ``orfs`` slot.
    Without this the placement group would wait behind its own orchestrator.
    """
    from chia.base.ChiaFunction import get
    kw = {"orfs_home": orfs_home} if orfs_home else {}
    with OpenROADNode() as node:
        if branch_from:
            # Seed from the shared prefix so make resumes at the first stage
            # after the branch point. Without this every candidate rebuilds an
            # identical placement, and a screen that picks better candidates
            # saves nothing at all.
            get(node.branch.chia_remote(branch_from, work_home, design_config,
                                        through_stage=branch_through, **kw))

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
              branch_through: str = "place", surrogate=None, state=None,
              measure_clock: bool = False):
        #: Shared prefix every candidate seeds from, if the loop built one.
        self.branch_from = branch_from
        self.branch_through = branch_through
        #: Optional screen. The agent sees its ranking; ORFS still decides.
        self.surrogate = surrogate
        self.state = state
        self.measure_clock = measure_clock
        self.scorecard = None
        self.policy = policy
        self.store = store
        self.failures = failures
        self.design_config = design_config
        self.work_root = work_root
        self.arm = arm
        self.gate = gate
        self.orfs_home = orfs_home
        self._pending: dict[int, object] = {}     # candidate id -> ObjectRef

        tools = [self.list_legal_knobs, self.propose_candidate,
                 self.candidate_status, self.list_candidates,
                 self.compare_candidates, self.past_failures,
                 self.best_candidate]
        if self.surrogate is not None:
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

    def propose_candidate(self, knobs: dict) -> str:
        """Propose one configuration and start building it.

        Returns immediately with a candidate id; the flow takes minutes. Poll
        with candidate_status(id).

        Args:
            knobs: ORFS knob names to values, e.g. {"CTS_CLUSTER_SIZE": 20}.
                Anything you omit keeps this design's default. Only knobs from
                list_legal_knobs() are accepted.
        """
        cid = self.store.propose(knobs, arm=self.arm)

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

        ref = _run_candidate.remote(f"{self.work_root}/cand-{cid:05d}",
                                    self.design_config, knobs, self.gate,
                                    "finish", self.orfs_home,
                                    self.branch_from, self.branch_through,
                                    self.measure_clock)
        self._pending[cid] = ref
        return (f"candidate {cid} started with {knobs}. "
                f"Poll candidate_status({cid}); expect a few minutes.")

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

        wait = max(0, min(int(max_wait_seconds), MAX_POLL_SECONDS))
        ready, _ = ray.wait([ref], timeout=wait)
        if not ready:
            return f"candidate {candidate_id} still running; poll again"

        del self._pending[candidate_id]
        try:
            results = ray.get(ref)
        except Exception as exc:                       # worker died, preempted, etc
            self.store.reject(candidate_id, f"run error: {exc}")
            return f"candidate {candidate_id} errored: {exc}"

        self.store.record(candidate_id, results)
        for r in results:
            self.failures.record(r)
        return self.store.get(candidate_id).one_line()

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
        """Ask a fast model which configurations look most promising.

        These are ESTIMATES from a surrogate, not measurements. They cost
        milliseconds instead of a build, so they are worth using to choose what
        to build — but only a real run decides anything. Propose the ones that
        look good and check them with propose_candidate.

        Args:
            count: how many suggestions to return, best first.
        """
        from chia_openroad.surrogate import ORFS_METRICS
        grid = self._screen_grid()
        preds = self.surrogate.predict(self.state, grid)
        objective = next((k for k, v in self.surrogate.predicts.items()
                          if v == "orfs_metric" and k in ORFS_METRICS), None)
        if objective is None:
            return "the screen predicts nothing comparable to an ORFS metric"
        better_is_higher = ORFS_METRICS[objective] == "higher"
        ranked = sorted((p for p in preds if objective in p.values),
                        key=lambda p: p.values[objective], reverse=better_is_higher)
        head = (f"{self.surrogate.name} ranked {len(ranked)} configurations by "
                f"predicted {objective} ({'higher' if better_is_higher else 'lower'} "
                f"is better) in {sum(p.cost_s for p in preds):.2f}s. "
                f"These are predictions, not results:")
        lines = [f"  {p.values[objective]:>12.6g}  "
                 f"{', '.join(f'{k}={v}' for k, v in sorted(p.knobs.items()))}"
                 for p in ranked[:count]]
        return head + "\n" + "\n".join(lines)

    def _screen_grid(self, per_axis: int = 6) -> list[dict]:
        """Configurations to screen: the legal ranges, intersected with whatever
        range the surrogate was actually fitted over."""
        import itertools
        axes = []
        for name, spec in sorted(self.policy.knobs.items()):
            if spec.stage != "cts" or spec.low is None:
                continue
            low, high = spec.low, spec.high
            fitted = getattr(self.surrogate, "domain", {}).get(name)
            if fitted:                       # never ask outside the training domain
                low, high = max(low, fitted[0]), min(high, fitted[1])
            if high <= low:
                continue
            step = (high - low) / (per_axis - 1)
            vals = [round(low + i * step, 3) for i in range(per_axis)]
            if spec.kind == "int":
                vals = sorted({int(round(v)) for v in vals})
            axes.append([(name, v) for v in vals])
        return [dict(c) for c in itertools.product(*axes)] if axes else []

    def past_failures(self, limit: int = 10) -> str:
        """Configurations already known not to build, and why.

        Read this before proposing: values near a known failure are likely to
        fail too, and a repeat costs a full run for no information.
        """
        hints = self.failures.hints(limit=limit)
        return "\n".join(hints) if hints else "nothing has failed yet"
