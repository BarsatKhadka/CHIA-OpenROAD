"""A feasibility screen: will this configuration build at all?

The first plugin, and chosen to be as unlike a clock-tree model as possible —
if the interface fits both, it is probably general. Where SwiftCTS observes a
placement, needs DEF and a timing report, predicts three ORFS metrics and fits
on real ORFS runs, this observes nothing, needs no artifacts, predicts a
probability, and fits on the failure log we already keep.

It is also the more valuable screen at the moment. Calibrating gcd/sky130hd
showed the waste is not in slightly-suboptimal configurations but in ones that
cannot build: CORE_UTILIZATION=45 and 50 both died at CTS. A quality screen
shaves a flow; this one skips it.

The model is deliberately simple — a distance-weighted vote over known
outcomes, no training, no dependencies. A screen that needs a training pipeline
before it can say "that will not build" is not saving anyone anything.
"""

from __future__ import annotations

from chia_openroad.knob_policy import KnobPolicy
from chia_openroad.surrogate import DesignState, SurrogateEvaluator, register


@register("feasibility")
class FeasibilityScreen(SurrogateEvaluator):
    """Predicts ``p_builds`` from configurations already known to build or fail."""

    observes_stage = None          # needs no design state at all
    requires = ()                  # and no artifacts
    predicts = {"p_builds": "probability"}

    def __init__(self, policy: KnobPolicy | None = None, sharpness: float = 4.0):
        #: Known outcomes: (knobs, built). Seeded by calibrate(), grown by observe().
        self.history: list[tuple[dict, bool]] = []
        self.policy = policy or KnobPolicy()
        #: How sharply distance discounts a neighbour's vote. Higher = more local.
        self.sharpness = sharpness

    # -- fitting ----------------------------------------------------------
    def calibrate(self, state: DesignState, run=None, budget_runs: int = 0) -> None:
        """Seed from the calibrated policy. Spends no ORFS runs.

        Calibration already measured each knob's buildable band by bisection,
        and every probe it made is evidence. Rather than repeat that work, take
        the band itself as the prior: inside is likely to build, outside is not.
        """
        for spec in self.policy.knobs.values():
            if spec.kind == "choice" or spec.low is None:
                continue
            span = spec.high - spec.low
            mid = (spec.low + spec.high) / 2
            self.history.append(({spec.name: mid}, True))
            # Just outside a *calibrated* band is real evidence of failure.
            # Just outside a nominal one is not — nothing was measured there.
            if spec.calibrated:
                self.history.append(({spec.name: spec.high + 0.15 * span}, False))
                self.history.append(({spec.name: spec.low - 0.15 * span}, False))

    def observe(self, knobs: dict, built: bool) -> None:
        """Record a real outcome. The loop calls this after every candidate."""
        self.history.append((dict(knobs), bool(built)))

    def observe_failure_log(self, failures) -> None:
        """Absorb everything a :class:`~chia_openroad.failure_log.FailureLog`
        already knows, so a fresh screen starts with the whole run's history."""
        for record in failures._by_knobs.values():
            self.observe(record.get("knobs", {}), built=False)

    # -- predicting -------------------------------------------------------
    def _distance(self, a: dict, b: dict) -> float:
        """Normalised distance between two configurations.

        Each knob is scaled by its own legal span so that a 10-unit move in
        CORE_UTILIZATION and a 0.1 move in PLACE_DENSITY count comparably.
        Knobs present in one configuration and not the other are ignored: an
        omitted knob means "the design's default", not "zero".
        """
        shared = set(a) & set(b)
        if not shared:
            return float("inf")
        total = 0.0
        for name in shared:
            spec = self.policy.knobs.get(name)
            try:
                x, y = float(a[name]), float(b[name])
            except (TypeError, ValueError):
                total += 0.0 if str(a[name]) == str(b[name]) else 1.0
                continue
            span = (spec.high - spec.low) if (spec and spec.low is not None
                                             and spec.high != spec.low) else 1.0
            total += ((x - y) / span) ** 2
        return (total / len(shared)) ** 0.5

    def _predict(self, state, candidates):
        out = []
        for knobs in candidates:
            # Anything the policy rejects cannot build by definition, and costs
            # nothing to rule out.
            ok, _ = self.policy.check(knobs)
            if not ok:
                out.append({"p_builds": 0.0})
                continue
            if not self.history:
                out.append({"p_builds": 0.5})       # no evidence either way
                continue
            weight_yes = weight_no = 0.0
            for known, built in self.history:
                d = self._distance(knobs, known)
                if d == float("inf"):
                    continue
                w = 2.718281828 ** (-self.sharpness * d)
                if built:
                    weight_yes += w
                else:
                    weight_no += w
            total = weight_yes + weight_no
            # Pull toward 0.5 when evidence is thin, so a single distant
            # neighbour cannot produce a confident answer.
            p = 0.5 if total < 1e-9 else (weight_yes + 0.5) / (total + 1.0)
            out.append({"p_builds": round(p, 4)})
        return out
