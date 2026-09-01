"""Measure each knob's buildable range on a specific design.

ORFS publishes no ranges, and the nominal ones in
:mod:`chia_openroad.knob_policy` come from a sweep of a *different* design on a
*different* PDK. On gcd/sky130hd the truth is narrow: the design's own config
sets CORE_UTILIZATION=38, and 40 builds while 42 fails at global route, 50 at
CTS, and 65 at global placement. A nominal range of [20, 60] is therefore
mostly a lie for this design.

That matters beyond wasted compute. One of the four evaluation arms measures
*tool invocations*; if an agent spends half of them on configurations that
cannot build, the measurement reflects the model's ignorance of this design
rather than anything about surrogates.

So: probe once per design, before the agent runs, and hand it measured bounds.

**What this does not capture.** Each knob is calibrated in isolation, with the
others at their defaults. Interactions are real — PLACE_DENSITY must exceed
CORE_UTILIZATION or placement cannot legalize — so a combination drawn from
per-knob bands can still fail. The band is a necessary condition, not a
sufficient one. Failures that survive it are what
:class:`chia_openroad.failure_log.FailureLog` is for.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from chia_openroad.knob_policy import KnobPolicy, KnobSpec
from chia_openroad.openroad import DEFAULT_GATE_STAGE, run_flow

logger = logging.getLogger(__name__)


@dataclass
class KnobCalibration:
    """The measured outcome for one knob."""
    name: str
    low: float | None = None          # lowest value that built
    high: float | None = None         # highest value that built
    nominal: tuple = ()               # what we started from
    probes: int = 0                   # runs spent
    failures: list = field(default_factory=list)   # (value, reason)

    @property
    def measured(self) -> bool:
        return self.low is not None and self.high is not None

    def summary(self) -> str:
        if not self.measured:
            return f"{self.name}: NOTHING BUILT across {self.nominal} ({self.probes} probes)"
        span = f"[{self.low}, {self.high}]"
        return (f"{self.name}: {span} of nominal [{self.nominal[0]}, {self.nominal[1]}] "
                f"({self.probes} probes, {len(self.failures)} failed)")


def _try(run, work_root: str, design_config: str, base: dict, name: str, value,
         gate: str, **kw) -> tuple[bool, str]:
    """One gated probe. Returns (built, reason).

    Gated at cts by default: the whole point is to learn feasibility cheaply,
    and routing is ~75% of the flow. A configuration that survives to cts is
    accepted as buildable for calibration purposes -- routing can still fail,
    and the failure log picks that up during the real run.
    """
    knobs = dict(base)
    knobs[name] = value
    # Each probe needs its own tree: knob changes invalidate stages, and
    # sharing one WORK_HOME across probes would serialise them.
    work = f"{work_root}/{name.lower()}_{str(value).replace('.', 'p')}"
    results = run_flow(run, work, design_config, knobs, gate=None, target=gate, **kw)
    last = results[-1]
    if last.success:
        return True, "built"
    reason = last.failure.as_hint() if last.failure else f"rc={last.returncode}"
    return False, reason


def calibrate_knob(run, spec: KnobSpec, design_config: str, work_root: str,
                   base_knobs: dict | None = None, *, budget: int = 12,
                   gate: str = DEFAULT_GATE_STAGE, tolerance: float = 0.05,
                   **kw) -> KnobCalibration:
    """Find the buildable range of one knob by bisecting each end.

    Bisection rather than a linear sweep because the interesting quantity is the
    *edge*, and a linear sweep at useful resolution costs far more runs. Roughly
    ``budget`` probes total, split between the two ends.

    ``tolerance`` is the relative width at which bisection stops: 0.05 means the
    edge is located to within 5% of the nominal span.
    """
    cal = KnobCalibration(spec.name, nominal=(spec.low, spec.high))
    base = dict(base_knobs or {})

    if spec.kind == "choice":
        # Small discrete set: just try them all, no bisection to do.
        built = []
        for value in spec.values:
            ok, reason = _try(run, work_root, design_config, base, spec.name, value, gate, **kw)
            cal.probes += 1
            built.append(value) if ok else cal.failures.append((value, reason))
        if built:
            cal.low, cal.high = min(built), max(built)
        return cal

    # Start from a value we believe builds: the midpoint of nominal.
    span = spec.high - spec.low
    seed = round((spec.low + spec.high) / 2, 4)
    ok, reason = _try(run, work_root, design_config, base, spec.name, seed, gate, **kw)
    cal.probes += 1
    if not ok:
        cal.failures.append((seed, reason))
        # Midpoint failed; walk inward from both ends looking for anything that
        # builds, rather than declaring the knob unusable on one data point.
        for value in (spec.low, spec.high, round(spec.low + span / 4, 4),
                      round(spec.high - span / 4, 4)):
            ok, reason = _try(run, work_root, design_config, base, spec.name, value, gate, **kw)
            cal.probes += 1
            if ok:
                seed = value
                break
            cal.failures.append((value, reason))
        if not ok:
            return cal          # nothing built; leave low/high None

    def bisect(good, bad):
        """Push `good` toward `bad` while it keeps building."""
        while cal.probes < budget and abs(bad - good) > tolerance * span:
            mid = round((good + bad) / 2, 4)
            if spec.kind == "int":
                mid = int(round(mid))
                if mid in (good, bad):
                    break
            built, reason = _try(run, work_root, design_config, base, spec.name, mid, gate, **kw)
            cal.probes += 1
            if built:
                good = mid
            else:
                cal.failures.append((mid, reason))
                bad = mid
        return good

    cal.high = bisect(seed, spec.high)
    cal.low = bisect(seed, spec.low)
    if spec.kind == "int":
        cal.low, cal.high = int(cal.low), int(cal.high)
    logger.info("calibrated %s", cal.summary())
    return cal


def calibrate(run, policy: KnobPolicy, design_config: str, work_root: str,
              names: list[str] | None = None, **kw) -> dict[str, KnobCalibration]:
    """Calibrate every knob in *policy* (or just *names*) on this design."""
    targets = [policy.knobs[n] for n in (names or sorted(policy.knobs))]
    out: dict[str, KnobCalibration] = {}
    for spec in targets:
        out[spec.name] = calibrate_knob(run, spec, design_config, work_root, **kw)
        logger.info("%s", out[spec.name].summary())
    return out


def apply(policy: KnobPolicy, calibrations: dict[str, KnobCalibration]) -> KnobPolicy:
    """Fold measured bounds back into a policy, dropping knobs nothing built for."""
    measured = {n: (c.low, c.high) for n, c in calibrations.items() if c.measured}
    dropped = [n for n, c in calibrations.items() if not c.measured]
    if dropped:
        logger.warning("dropping knobs with no buildable value: %s", ", ".join(dropped))
    kept = tuple(k for k in policy.knobs.values() if k.name not in dropped)
    return KnobPolicy(kept).with_calibration(measured)
