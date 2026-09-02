"""A socket for cheap evaluators that screen configurations before ORFS runs.

This module is the reusable contribution: not any particular model, but the
contract a model implements so a loop can use it without knowing what it is.

**The author's side is deliberately small.** One method is required:

    class MySkewModel(SurrogateEvaluator):
        predicts = {"clock_skew_setup": "orfs_metric"}

        def _predict(self, state, candidates):
            return [{"clock_skew_setup": my_model(c["CTS_CLUSTER_SIZE"])}
                    for c in candidates]

That is a complete surrogate. ``calibrate`` defaults to a no-op,
``observes_stage`` to None, ``requires`` to (). You declare only what you use.
A model needing more says so:

    class SwiftCTSEvaluator(SurrogateEvaluator):
        observes_stage = "place"
        requires = ("def", "timing_rpt")
        predicts = {"clock_skew_setup": "orfs_metric", "power_total": "orfs_metric"}

**Why inputs are not standardised.** Different surrogates need different things:
a clock-tree model wants placement geometry and a timing report; a feasibility
screen wants only the knobs; a congestion model wants the routed database. No
common feature vector exists, and inventing one would either cripple the
detailed models or bloat the interface. So :class:`DesignState` *points at*
artifacts and parses nothing — each surrogate takes what it needs and declares
that in ``requires``, which is how the loop knows what to produce.

**What IS standardised is the output.** A prediction of something ORFS measures
must use ORFS's own metric name. That single rule buys three things for free:
comparison against ground truth needs no adapter, selection can rank
generically because direction is known per metric, and the loop never has to
know which model produced a number.

Predictions of things ORFS does *not* measure (a probability that a
configuration will build, say) are allowed, declared with a different kind, and
scored differently — see :class:`Scorecard`.
"""

from __future__ import annotations

import logging
import math
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from chia_openroad.openroad import SUMMARY_KEYS

logger = logging.getLogger(__name__)

#: Every metric ORFS reports that a surrogate may predict, and which direction
#: is better. Exported so a plugin author never has to grep our source to learn
#: that clock skew is spelled ``clock_skew_setup``.
ORFS_METRICS: dict[str, str] = {
    "worst_slack": "higher",          # ns; negative means missing timing
    "tns": "higher",                  # ns, total negative slack
    "hold_worst_slack": "higher",
    "power_total": "lower",           # W
    "instance_area": "lower",         # um^2
    "die_area": "lower",
    "utilization": "lower",
    "clock_skew_setup": "lower",      # ns
    "clock_buffer_area": "lower",     # um^2
    "clock_buffer_count": "lower",
}
assert set(ORFS_METRICS) <= set(SUMMARY_KEYS), "ORFS_METRICS drifted from SUMMARY_KEYS"

#: Kinds a prediction may carry.
#:   orfs_metric -- something ORFS measures; validated against ground truth
#:   probability -- in [0, 1]; scored by calibration, not by residual
#:   score       -- an arbitrary ranking quantity; not validated
KINDS = ("orfs_metric", "probability", "score")


@dataclass
class DesignState:
    """What a surrogate may observe. Points at artifacts; parses nothing."""
    design: str
    platform: str
    work_home: str = ""
    stage: str | None = None                 # checkpoint this state came from
    artifacts: dict[str, str] = field(default_factory=dict)   # name -> path
    metrics: dict = field(default_factory=dict)               # parsed ORFS JSON
    knobs: dict = field(default_factory=dict)                 # what produced it

    def describe(self) -> str:
        """What is actually available here — for an author writing against a
        real state rather than a docstring."""
        import os
        lines = [f"design={self.design} platform={self.platform} stage={self.stage}"]
        lines.append("artifacts:")
        for name, path in sorted(self.artifacts.items()):
            mark = "ok " if os.path.exists(path) else "MISSING"
            lines.append(f"  {mark} {name:14s} {path}")
        populated = [k for k in ORFS_METRICS if k in self.metrics]
        lines.append(f"metrics populated: {', '.join(populated) or '(none)'}")
        lines.append(f"knobs: {self.knobs or '(defaults)'}")
        return "\n".join(lines)


@dataclass
class Prediction:
    knobs: dict
    values: dict[str, float]
    kind: dict[str, str] = field(default_factory=dict)
    cost_s: float = 0.0


class SurrogateEvaluator(ABC):
    """Base class for a cheap evaluator. Implement ``_predict``; the rest is optional."""

    #: Display name. Defaults to the class name.
    name: str = ""
    #: Which ORFS stage this needs to observe from, or None if it needs no
    #: design state at all. The loop branches the flow here.
    observes_stage: str | None = None
    #: Artifact names required in ``DesignState.artifacts``. The loop reads this
    #: to know what to produce — e.g. ("def",) makes it dump DEF after placement.
    requires: tuple[str, ...] = ()
    #: What this predicts: name -> kind. Names of kind "orfs_metric" MUST be
    #: keys of ORFS_METRICS, or they can never be checked against reality.
    predicts: dict[str, str] = {}

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if not cls.name:
            cls.name = cls.__name__

    # -- what an author implements ----------------------------------------
    @abstractmethod
    def _predict(self, state: DesignState, candidates: list[dict]) -> list[dict]:
        """Return one ``{metric: value}`` dict per candidate, in order.

        Plain dicts — the framework wraps, times and validates them.
        """

    def calibrate(self, state: DesignState, run=None, budget_runs: int = 0) -> None:
        """Adapt to this design. Default: nothing to do.

        ``run`` is the loop's own ORFS callable, so a model that needs a couple
        of real runs to fit borrows the loop's execution path rather than
        opening its own — those runs then land in the same ledger and count
        against the same budget.
        """
        return None

    # -- what the framework guarantees -------------------------------------
    def predict(self, state: DesignState, candidates: list[dict]) -> list[Prediction]:
        """Wrap ``_predict`` with timing and validation."""
        started = time.monotonic()
        raw = self._predict(state, list(candidates))
        elapsed = time.monotonic() - started
        if len(raw) != len(candidates):
            raise ValueError(
                f"{self.name}._predict returned {len(raw)} predictions for "
                f"{len(candidates)} candidates; they must correspond in order")
        per = elapsed / max(len(candidates), 1)
        out = []
        for knobs, values in zip(candidates, raw):
            undeclared = set(values) - set(self.predicts)
            if undeclared:
                raise ValueError(
                    f"{self.name} returned undeclared key(s) {sorted(undeclared)}; "
                    f"add them to `predicts` so they can be scored")
            out.append(Prediction(knobs=dict(knobs), values=dict(values),
                                  kind={k: self.predicts[k] for k in values},
                                  cost_s=per))
        return out


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, type] = {}


def register(name: str):
    """Register a surrogate so a loop can select it by config string."""
    def wrap(cls):
        _REGISTRY[name] = cls
        cls.name = name
        return cls
    return wrap


def get(name: str) -> type:
    if name not in _REGISTRY:
        raise KeyError(f"no surrogate {name!r}; registered: {sorted(_REGISTRY)}")
    return _REGISTRY[name]


def registered() -> list[str]:
    return sorted(_REGISTRY)


# ---------------------------------------------------------------------------
# Conformance
# ---------------------------------------------------------------------------

#: A real sky130hd gcd flow is ~200 s. A screen that costs an appreciable
#: fraction of that is not saving anything.
CHEAP_ENOUGH_S = 1.0


def conformance_check(surrogate: SurrogateEvaluator, state: DesignState | None = None,
                      candidates: list[dict] | None = None, verbose: bool = True) -> bool:
    """Check a surrogate before wiring it into a loop that takes an hour to fail.

    Catches the mistakes that are otherwise invisible: a mis-spelled metric name
    is never compared against ground truth, so the model appears perfect because
    nothing ever checked it.
    """
    problems: list[str] = []

    def report(ok, label, detail="", warn=False):
        if verbose:
            tag = "warn" if (warn and not ok) else ("ok  " if ok else "FAIL")
            print(f"  [{tag}] {label}" + (f" — {detail}" if detail else ""))
        if not ok and not warn:
            problems.append(label)

    state = state or DesignState(design="probe", platform="probe")
    candidates = candidates or [{"CTS_CLUSTER_SIZE": v} for v in (12, 18, 24, 30, 36)]

    report(bool(surrogate.predicts), "declares at least one prediction",
           ", ".join(surrogate.predicts) or "none")

    bad_kind = {k: v for k, v in surrogate.predicts.items() if v not in KINDS}
    report(not bad_kind, "every declared kind is valid", str(bad_kind) or f"kinds: {KINDS}")

    not_real = [k for k, v in surrogate.predicts.items()
                if v == "orfs_metric" and k not in ORFS_METRICS]
    report(not not_real, "every orfs_metric name is a real ORFS metric",
           f"unknown: {not_real}" if not_real else f"{len(ORFS_METRICS)} known")

    started = time.monotonic()
    try:
        preds = surrogate.predict(state, candidates)
        elapsed = time.monotonic() - started
    except Exception as exc:
        report(False, "predict() runs", f"{type(exc).__name__}: {exc}")
        if verbose:
            print(f"\n  {len(problems)} problem(s): {', '.join(problems)}")
        return False

    report(len(preds) == len(candidates), "one prediction per candidate",
           f"{len(preds)} for {len(candidates)}")

    numeric = all(isinstance(v, (int, float)) and math.isfinite(v)
                  for p in preds for v in p.values.values())
    report(numeric, "all values numeric and finite")

    probs = [(k, v) for p in preds for k, v in p.values.items()
             if p.kind.get(k) == "probability" and not 0.0 <= v <= 1.0]
    report(not probs, "probabilities lie in [0, 1]", str(probs[:3]) if probs else "")

    keys_stable = len({tuple(sorted(p.values)) for p in preds}) == 1
    report(keys_stable, "same keys for every candidate",
           "a varying key set cannot be ranked or scored")

    cheap = elapsed < CHEAP_ENOUGH_S
    report(cheap, "cheap enough to be worth screening with",
           f"{elapsed:.2f}s for {len(candidates)} candidates "
           f"(a real ORFS flow is ~200s)", warn=True)

    if verbose:
        print(f"\n  {'CONFORMANT' if not problems else 'NOT CONFORMANT: ' + ', '.join(problems)}")
    return not problems


# ---------------------------------------------------------------------------
# Scorecard
# ---------------------------------------------------------------------------

@dataclass
class Scorecard:
    """Predicted vs actual, accumulated as the loop runs.

    This is deliverable #4 -- "a quantitative evaluation of the value of
    surrogate models" -- so it belongs in the framework rather than being
    reconstructed by hand afterwards. A plugin author gets accuracy feedback
    without writing any evaluation code.
    """
    surrogate: str
    pairs: dict[str, list[tuple[float, float]]] = field(default_factory=dict)
    #: (predicted probability, actually built) for "probability" predictions
    calibration: list[tuple[float, bool]] = field(default_factory=list)

    def observe(self, prediction: Prediction, actual: dict, built: bool = True) -> None:
        for key, value in prediction.values.items():
            kind = prediction.kind.get(key)
            if kind == "orfs_metric" and isinstance(actual.get(key), (int, float)):
                self.pairs.setdefault(key, []).append((value, actual[key]))
            elif kind == "probability":
                self.calibration.append((value, built))

    def summary(self) -> str:
        lines = [f"surrogate: {self.surrogate}"]
        for key, pairs in sorted(self.pairs.items()):
            if not pairs:
                continue
            errs = [abs(p - a) for p, a in pairs]
            mean_abs = sum(errs) / len(errs)
            scale = sum(abs(a) for _, a in pairs) / len(pairs) or 1.0
            # Rank correlation matters more than absolute error: a screen only
            # has to order candidates correctly, not predict their values.
            order = _spearman([p for p, _ in pairs], [a for _, a in pairs])
            lines.append(f"  {key:22s} n={len(pairs):3d}  MAE={mean_abs:.4g} "
                         f"({100*mean_abs/scale:.1f}% of mean)  rank_corr={order:+.2f}")
        if self.calibration:
            hi = [b for p, b in self.calibration if p >= 0.5]
            lo = [b for p, b in self.calibration if p < 0.5]
            lines.append(f"  {'p(builds)':22s} n={len(self.calibration):3d}  "
                         f"predicted-yes actually built {sum(hi)}/{len(hi) or 0}, "
                         f"predicted-no actually built {sum(lo)}/{len(lo) or 0}")
        if len(lines) == 1:
            lines.append("  (nothing scored yet)")
        return "\n".join(lines)


def _spearman(xs: list[float], ys: list[float]) -> float:
    """Rank correlation, no scipy. Returns 0.0 when undefined."""
    n = len(xs)
    if n < 2:
        return 0.0

    def rank(vals):
        order = sorted(range(n), key=lambda i: vals[i])
        r = [0.0] * n
        i = 0
        while i < n:                      # average ties
            j = i
            while j + 1 < n and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2.0
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx, ry = rank(xs), rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else 0.0
