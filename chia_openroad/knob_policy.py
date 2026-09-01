"""The bounded action space: which knobs an agent may set, and over what range.

The proposal's phrasing is "a bounded set of legal configuration changes". This
module is that bound. It exists separately from :mod:`chia_openroad.openroad`
because the node's job is to run whatever it is given correctly, while deciding
*what may be asked for* is policy — and policy is what the trust boundary is
made of.

Three sources, in decreasing authority:

1. :mod:`chia_openroad.knob_specs`, generated from ORFS's own
   ``variables.json`` — what exists, which stage it hits, its type. Anything
   not in there is rejected outright.
2. ORFS's ``tunable`` flag: 18 variables ORFS itself considers sweepable. A
   strong prior, but conservative — it omits PLACE_DENSITY, GPL_TIMING_DRIVEN
   and ROUTING_LAYER_ADJUSTMENT, all of which ORFS AutoTuner sweeps.
3. ``~/ChipDreamer/datagen/orfs_knob_map.md`` — a knob set already validated
   against real sky130/asap7 sweeps, with the grids those sweeps used. That is
   what the DEFAULT_KNOBS below encode.

**Ranges here are nominal, not feasible.** ORFS publishes no bounds, and the
buildable window is design- and PDK-specific: gcd/sky130hd defaults to
CORE_UTILIZATION=38 and only builds within roughly +-2 of it — 42 fails at
global route, 50 at CTS, 65 at global placement. An agent has no way to know
that. So a nominal range is a starting point for calibration
(:mod:`chia_openroad.calibrate`), never something to hand an agent unmeasured.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from chia_openroad.knob_specs import KNOB_STAGE, KNOB_TYPE, KNOWN, TUNABLE


@dataclass(frozen=True)
class KnobSpec:
    """One legal knob: what it does, and what values may be proposed for it."""
    name: str
    stage: str                     # earliest ORFS stage it affects
    kind: str                      # "float" | "int" | "choice"
    values: tuple = ()             # for "choice": the allowed values
    low: float | None = None       # for numeric: nominal bounds
    high: float | None = None
    description: str = ""
    #: True once a per-design calibration has replaced the nominal bounds with
    #: measured ones. Nothing should be handed to an agent while this is False.
    calibrated: bool = False

    def contains(self, value) -> bool:
        if self.kind == "choice":
            return str(value) in {str(v) for v in self.values}
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False
        if self.kind == "int" and number != int(number):
            return False
        return (self.low is None or number >= self.low) and \
               (self.high is None or number <= self.high)

    def describe(self) -> str:
        """One line for an agent-facing tool listing."""
        if self.kind == "choice":
            span = "{" + ", ".join(str(v) for v in self.values) + "}"
        else:
            span = f"[{self.low}, {self.high}]"
        mark = "" if self.calibrated else "  (NOMINAL — not calibrated for this design)"
        return f"{self.name} ({self.stage}, {self.kind}) in {span} — {self.description}{mark}"


#: The default legal set. Grids come from orfs_knob_map.md, which took them
#: from a real sky130 sweep (sweep_branch.py:GRID) and validated the ORFS
#: translation. Bounds are the span of those grids.
DEFAULT_KNOBS: tuple[KnobSpec, ...] = (
    # --- floorplan ---
    KnobSpec("CORE_UTILIZATION", "floorplan", "float", low=20, high=60,
             description="core utilization %; the single strongest area/congestion lever"),
    KnobSpec("CORE_ASPECT_RATIO", "floorplan", "choice", values=(0.7, 1.0, 1.4, 2.0),
             description="die height/width ratio"),
    KnobSpec("PLACE_DENSITY", "floorplan", "float", low=0.25, high=0.90,
             description="target placement density; must exceed utilization or placement cannot legalize"),
    # --- placement ---
    KnobSpec("GPL_ROUTABILITY_DRIVEN", "place", "choice", values=(0, 1),
             description="let global placement spread cells to relieve congestion"),
    KnobSpec("GPL_TIMING_DRIVEN", "place", "choice", values=(0, 1),
             description="let global placement weight timing-critical nets"),
    # --- clock tree (the stage a CTS surrogate screens) ---
    KnobSpec("CTS_CLUSTER_SIZE", "cts", "int", low=10, high=40,
             description="max sinks per clock cluster; drives buffer count and clock power"),
    KnobSpec("CTS_CLUSTER_DIAMETER", "cts", "float", low=10, high=100,
             description="max cluster diameter in um; trades skew against wirelength"),
    KnobSpec("CTS_BUF_DISTANCE", "cts", "float", low=30, high=200,
             description="distance between clock buffers in um"),
    # --- routing ---
    KnobSpec("ROUTING_LAYER_ADJUSTMENT", "floorplan", "float", low=0.1, high=0.7,
             description="routing layer capacity derate; low eases detailed routing, high risks detours"),
    KnobSpec("SETUP_SLACK_MARGIN", "floorplan", "float", low=0.0, high=0.2,
             description="extra setup margin (ns) the resizer targets"),
    KnobSpec("HOLD_SLACK_MARGIN", "floorplan", "float", low=0.0, high=0.1,
             description="extra hold margin (ns) the resizer targets"),
)

#: Documented exclusions. Recording *why* a knob is absent stops it being
#: quietly re-added later.
EXCLUDED: dict[str, str] = {
    "CTS_CLK_MAX_WIRE_LENGTH":
        "no OpenROAD equivalent — clock_tree_synthesis -max_wire_length was "
        "removed; the lever is CTS_BUF_DISTANCE (orfs_knob_map.md)",
    "GLOBAL_PLACEMENT_ARGS":
        "free-form argument string, not a scalar — cannot be range-checked, so "
        "it is not safe to expose to an agent as-is",
    "DIE_AREA":
        "absolute geometry; conflicts with CORE_UTILIZATION and is design-specific",
    "CORE_AREA":
        "same as DIE_AREA",
}


class KnobPolicy:
    """The legal knob set for one design, and the gate every proposal passes.

    ::

        policy = KnobPolicy()
        ok, reason = policy.check({"CORE_UTILIZATION": 45})
        policy.describe_for_agent()      # what list_legal_knobs() returns
    """

    def __init__(self, knobs: tuple[KnobSpec, ...] = DEFAULT_KNOBS):
        self.knobs = {k.name: k for k in knobs}
        self._audit()

    def _audit(self) -> None:
        """Fail loudly if the policy references a knob ORFS does not have.

        A legal set that names a variable make would silently ignore is worse
        than no legal set at all: every proposal touching it looks like a swept
        parameter and is in fact a no-op.
        """
        for name, spec in self.knobs.items():
            if name not in KNOWN:
                raise ValueError(
                    f"policy lists {name!r}, which is not an ORFS variable — "
                    f"make would accept and ignore it")
            if KNOB_STAGE.get(name) and KNOB_STAGE[name] != spec.stage:
                raise ValueError(
                    f"policy says {name} affects {spec.stage}, ORFS says "
                    f"{KNOB_STAGE[name]}")

    def check(self, knobs: dict) -> tuple[bool, str]:
        """Is this proposal inside the legal set? Returns (ok, reason)."""
        for name, value in (knobs or {}).items():
            spec = self.knobs.get(str(name))
            if spec is None:
                if str(name) in EXCLUDED:
                    return False, f"{name} is excluded: {EXCLUDED[str(name)]}"
                return False, (f"{name} is not in the legal knob set; "
                               f"legal: {', '.join(sorted(self.knobs))}")
            if not spec.contains(value):
                return False, f"{name}={value} is outside {spec.describe()}"
        return True, "ok"

    def describe_for_agent(self) -> str:
        """The legal set as text, grouped by stage — what an agent is shown."""
        lines = []
        for stage in ("floorplan", "place", "cts", "route", "finish"):
            here = [k for k in self.knobs.values() if k.stage == stage]
            if not here:
                continue
            lines.append(f"[{stage}]")
            lines += [f"  {k.describe()}" for k in sorted(here, key=lambda x: x.name)]
        uncalibrated = [k.name for k in self.knobs.values() if not k.calibrated]
        if uncalibrated:
            lines.append("")
            lines.append(f"WARNING: {len(uncalibrated)} knob(s) carry nominal ranges that "
                         f"have not been measured on this design. Values inside a nominal "
                         f"range may still fail to build.")
        return "\n".join(lines)

    def with_calibration(self, measured: dict[str, tuple[float, float]]) -> "KnobPolicy":
        """A copy whose numeric bounds come from measurement, not from the grid."""
        updated = []
        for spec in self.knobs.values():
            if spec.name in measured and spec.kind in ("float", "int"):
                low, high = measured[spec.name]
                updated.append(KnobSpec(spec.name, spec.stage, spec.kind, spec.values,
                                        low, high, spec.description, calibrated=True))
            else:
                updated.append(spec)
        return KnobPolicy(tuple(updated))
