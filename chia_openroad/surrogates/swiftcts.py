"""SwiftCTS behind the surrogate interface.

Deliberately NOT part of the upstream CHIA contribution. SwiftCTS is separate
prior work, and keeping it out of the PR is what demonstrates that
:mod:`chia_openroad.surrogate` is a general socket rather than a shape built
around one model.

SwiftCTS predicts clock power, clock wirelength and clock skew for a
(placement, CTS-knob) pair, using physics-informed models over features parsed
from a DEF, a timing report and (optionally) a SAIF.

    model.add_design(pid, def_path, saif_path, timing_path, t_clk)
    pred = model.predict(pid, cd=..., cs=..., mw=..., bd=...)   -> CTSPrediction

Three things had to be resolved to put it behind the interface, and two of them
are honest limitations rather than solved problems.

**1. All three outputs are checkable, but two needed extracting.**
ORFS's ``6_report.json`` carries ``finish__clock__skew__setup`` but only
*total* power and no wirelength breakdown.
:meth:`OpenROADNode.measure_clock` supplies the rest: ``report_power`` groups
power and one group is Clock, and ``report_wire_length -net <clock nets>``
gives routed clock length. So all three predictions are scored against reality,
in ORFS-side units.

**2. SwiftCTS's ``mw`` knob has no ORFS equivalent.**
``CTS_CLK_MAX_WIRE_LENGTH`` was removed from OpenROAD — the lever is
``-distance_between_buffers``, i.e. ``CTS_BUF_DISTANCE`` — which
``orfs_knob_map.md`` already documents. ``mw`` is held at a constant so the
model sees a consistent value rather than a varying one it cannot control.

**3. ORFS produces no SAIF.**
Switching activity comes from gate-level simulation, which the flow does not
run. Every SAIF-derived feature is read with ``.get(name, default)``, so an
absent SAIF degrades power accuracy instead of failing. Power is already
unvalidated (see 1), so this costs ranking quality, not correctness.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile

from chia_openroad.surrogate import DesignState, SurrogateEvaluator, register

logger = logging.getLogger(__name__)

#: Held constant: no ORFS variable controls it (orfs_knob_map.md).
FIXED_MAX_WIRE = 180.0

#: SwiftCTS knob name -> ORFS make variable.
KNOB_MAP = {
    "cd": "CTS_CLUSTER_DIAMETER",
    "cs": "CTS_CLUSTER_SIZE",
    "bd": "CTS_BUF_DISTANCE",
}


@register("swiftcts")
class SwiftCTSEvaluator(SurrogateEvaluator):
    """Screens CTS configurations against a fixed placement."""

    observes_stage = "place"
    requires = ("def", "timing_rpt")
    # All three are checkable. Skew comes from ORFS's own metric JSON; clock
    # power and wirelength come from OpenROADNode.measure_clock, which reads
    # report_power's Clock group and report_wire_length over the clock nets.
    # Names and units are ORFS-side, so the scorecard needs no adapter.
    predicts = {
        "clock_skew_setup": "orfs_metric",      # ns
        "clock_power_w": "orfs_metric",         # W
        "clock_wirelength_um": "orfs_metric",   # um
    }

    def __init__(self, model_path: str | None = None, swiftcts_dir: str | None = None,
                 defaults: dict | None = None):
        self.model_path = model_path or os.path.expanduser(
            "~/SwiftCTS/SwiftCTS/saved_models/model.pkl")
        self.swiftcts_dir = swiftcts_dir or os.path.dirname(os.path.dirname(self.model_path))
        #: Knob values used when a candidate leaves one unset — the model needs
        #: all four, ORFS falls back to the design's defaults.
        self.defaults = defaults or {"CTS_CLUSTER_DIAMETER": 50.0,
                                     "CTS_CLUSTER_SIZE": 20.0,
                                     "CTS_BUF_DISTANCE": 100.0}
        self._model = None
        self._pid: str | None = None

    # -- loading ----------------------------------------------------------
    def _load(self):
        if self._model is not None:
            return self._model
        if self.swiftcts_dir not in sys.path:
            sys.path.insert(0, self.swiftcts_dir)
        from swiftcts import SwiftCTS            # noqa: E402  (path set above)
        self._model = SwiftCTS.load(self.model_path)
        logger.info("loaded SwiftCTS from %s", self.model_path)
        return self._model

    # -- fitting ----------------------------------------------------------
    def calibrate(self, state: DesignState, run=None, budget_runs: int = 0) -> None:
        """Register this placement with the model.

        Not optional: SwiftCTS parses the DEF, timing report and clock period
        once per placement, and ``predict`` cannot run before it has. This is
        the "adapt to a new placement" step — it spends no ORFS runs of its own,
        because the loop has already built the placement it is adapting to.
        """
        model = self._load()
        missing = [a for a in self.requires if not state.artifacts.get(a)]
        if missing:
            raise ValueError(
                f"SwiftCTS needs {missing} in DesignState.artifacts. The loop "
                f"produces these because `requires` declares them; if they are "
                f"absent the placement stage did not emit them. Have: "
                f"{sorted(state.artifacts)}")

        saif = state.artifacts.get("saif")
        if not saif:
            # No SAIF from ORFS. An empty file parses to {}, and every
            # SAIF-derived feature is read with a default, so power prediction
            # degrades rather than the model failing.
            tmp = tempfile.NamedTemporaryFile("w", suffix=".saif", delete=False)
            tmp.close()
            saif = tmp.name
            logger.warning("no SAIF available; power features fall back to "
                           "defaults (clock power is unvalidated anyway)")

        t_clk = self._clock_period(state)
        self._pid = f"{state.design}_{state.platform}"
        model.add_design(self._pid, state.artifacts["def"], saif,
                         state.artifacts["timing_rpt"], t_clk)
        logger.info("registered placement %s (t_clk=%.3f ns)", self._pid, t_clk)

    @staticmethod
    def _clock_period(state: DesignState) -> float:
        """Clock period in ns, from ORFS's own artifact or metrics."""
        path = state.artifacts.get("clock_period")
        if path and os.path.exists(path):
            with open(path) as f:
                return float(f.read().strip())
        fmax = state.metrics.get("finish__timing__fmax__clock:core_clock")
        if isinstance(fmax, (int, float)) and fmax > 0:
            return 1e9 / fmax
        raise ValueError(
            "cannot determine the clock period: no clock_period artifact and no "
            "fmax in metrics. SwiftCTS needs it to build features.")

    # -- predicting -------------------------------------------------------
    def _predict(self, state, candidates):
        if self._pid is None:
            raise RuntimeError(
                "call calibrate(state) before predict() — SwiftCTS parses the "
                "placement once and predicts against it many times")
        model = self._load()
        out = []
        for knobs in candidates:
            args = {}
            for short, orfs_name in KNOB_MAP.items():
                args[short] = float(knobs.get(orfs_name, self.defaults[orfs_name]))
            pred = model.predict(self._pid, mw=FIXED_MAX_WIRE, **args)
            # SwiftCTS works in mW and mm; ground truth is in W and um. Convert
            # here rather than in the scorecard, so the comparison is like for
            # like and the interface never has to know about a model's units.
            values = {"clock_power_w": float(pred.power_mW) / 1000.0,
                      "clock_wirelength_um": float(pred.wl_mm) * 1000.0}
            # skew_ns is None when the model can only give a z-score; omitting
            # the key beats reporting a number in the wrong units, and the
            # conformance rule about stable keys will catch it if it varies.
            if pred.skew_ns is not None:
                values["clock_skew_setup"] = float(pred.skew_ns)
            out.append(values)
        return out
