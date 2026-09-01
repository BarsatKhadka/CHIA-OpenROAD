"""chia_openroad.openroad — OpenROAD-flow-scripts (ORFS) nodes.

:meth:`OpenROADNode.run_stage` wraps one ORFS ``make`` invocation. ORFS's
interface is uniform across stages (synth, floorplan, place, cts, route,
finish, drc, lvs): the same make variables in, a stage target, per-stage ODB
checkpoints and metric JSONs out. One node therefore covers the whole flow
with the stage as a parameter — the same reasoning ``chia.vlsi.hammer`` gives
for covering every hammer action with a single ``run``.

WORK_HOME is PATH-BASED: every checkpoint (``1_synth.odb`` … ``6_final.gds``)
and every metric JSON lives on the worker that ran the stage, so chained
stages and report fetches must land on the SAME worker. :class:`OpenROADNode`
enforces that with a placement group (see
:class:`chia.base.colocated.ColocatedNode`).

``make`` also gives us resume for free: each stage target depends on the
previous stage's ODB, so ``run_stage("route")`` on a WORK_HOME that already
holds ``3_place.odb`` restarts at CTS rather than at synthesis. That is the
behaviour ``chia.base.cache`` bypass is layered on top of.

This module knows nothing about designs, platforms, or which knobs are legal —
that arrives via ``design_config`` and the ``knobs`` mapping. Deciding *which*
knobs an agent may set is a separate concern and deliberately not here.

Destined for ``chia/vlsi/openroad.py`` upstream; developed out-of-tree until
the rest of the flow is proven.
"""

from __future__ import annotations

import functools
import glob as _glob
import json
import logging
import os
import re
import signal
import shutil
import subprocess
import time
from dataclasses import dataclass, field

from chia.base.ChiaFunction import ChiaFunction
from chia.base.colocated import ColocatedNode

from chia_openroad.knob_specs import (
    DEPRECATED, KNOB_DEFAULT, KNOB_STAGE, KNOB_TYPE, KNOWN, STAGE_ORDER, TUNABLE,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ORFS facts
# ---------------------------------------------------------------------------

#: Default ORFS checkout inside the chia-orfs worker image.
DEFAULT_ORFS_HOME = "/OpenROAD-flow-scripts"

#: Stage make targets, in flow order. ``drc``/``lvs`` are signoff targets that
#: depend on ``finish``; only some platforms ship the KLayout decks they need
#: (sky130hd and ihp-sg13g2 have both, asap7/nangate45 have DRC only).
STAGES = ("synth", "floorplan", "place", "cts", "route", "finish", "drc", "lvs")

#: What each stage leaves behind, relative to RESULTS_DIR (or REPORTS_DIR for
#: ``drc``). Presence of this file is what makes the stage resumable, and what
#: ``run_stage`` reports back as ``checkpoint``.
STAGE_CHECKPOINT = {
    "synth": ("results", "1_synth.odb"),
    "floorplan": ("results", "2_floorplan.odb"),
    "place": ("results", "3_place.odb"),
    "cts": ("results", "4_cts.odb"),
    "route": ("results", "5_route.odb"),
    "finish": ("results", "6_final.gds"),
    "drc": ("reports", "6_drc.lyrdb"),
    "lvs": ("results", "6_lvs.lvsdb"),
}

#: Headline metrics, pulled out of ORFS's own JSON so callers (and later, a
#: surrogate comparing predictions to ground truth) don't each re-derive them.
#: Key names verified against a real sky130hd/gcd ``6_report.json``.
SUMMARY_KEYS = {
    "worst_slack": "finish__timing__setup__ws",
    "tns": "finish__timing__setup__tns",
    "hold_worst_slack": "finish__timing__hold__ws",
    "power_total": "finish__power__total",
    "instance_area": "finish__design__instance__area",
    "die_area": "finish__design__die__area",
    "utilization": "finish__design__instance__utilization",
    # The CTS quantities a clock-tree surrogate predicts.
    "clock_skew_setup": "finish__clock__skew__setup",
    "clock_buffer_area": "finish__design__instance__area__class:clock_buffer",
    "clock_buffer_count": "finish__design__instance__count__class:clock_buffer",
}

#: ORFS numbers every artifact by stage (``1_synth.odb``, ``4_1_cts.odb``,
#: ``6_final.gds``), which is what lets us invalidate a suffix of the flow by
#: filename alone. drc/lvs are signoff products of stage 6.
STAGE_PREFIX = {"synth": 1, "floorplan": 2, "place": 3,
                "cts": 4, "route": 5, "finish": 6, "drc": 6, "lvs": 6}

#: Written into WORK_HOME so a later call can tell which knobs produced the
#: checkpoints already sitting there. Without it there is no way to know.
KNOB_MANIFEST = ".chia_orfs_knobs.json"

#: Make variables are UPPER_SNAKE. Anything else in a knob mapping is rejected
#: rather than passed through — a knob key is appended to a `make` argv, so an
#: unconstrained key could smuggle in a flag or another target.
_KNOB_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class OrfsStageResult:
    """One ``make <stage>`` run. Returned by value; artifacts stay on the worker."""
    success: bool
    returncode: int
    stage: str
    work_home: str            # on the worker that ran the stage
    design_config: str
    platform: str
    design: str
    variant: str
    knobs: dict[str, str]
    elapsed_s: float
    #: Absolute path to the stage's checkpoint on the worker, or None if the
    #: stage did not get far enough to write it.
    checkpoint: str | None = None
    #: ORFS's final report (``6_report.json``), empty until ``finish`` has run.
    metrics: dict = field(default_factory=dict)
    #: Every per-stage JSON found: basename -> parsed contents.
    stage_metrics: dict[str, dict] = field(default_factory=dict)
    #: SUMMARY_KEYS resolved against whatever metrics are available.
    summary: dict[str, float] = field(default_factory=dict)
    #: DRC/LVS verdict, read from the report. Only set for those stages.
    signoff: OrfsSignoff | None = None
    #: Why it failed, when it failed. None on success.
    failure: OrfsFailure | None = None
    #: Stage from which stale artifacts were deleted before this run, if the
    #: knobs differed from the ones that produced the existing WORK_HOME.
    invalidated_from: str | None = None
    #: What that invalidation removed (relative paths), for the record.
    invalidated: list[str] = field(default_factory=list)
    stdout_tail: str = ""
    stderr_tail: str = ""


@dataclass
class OrfsSignoff:
    """The verdict of a DRC or LVS run, read from the report rather than the
    exit code.

    ORFS's KLayout decks do not fail the process on a bad result. The sky130hd
    LVS deck has its ``raise`` commented out::

        if ! compare
          #raise "ERROR : Netlists don't match"
          puts "ERROR : Netlists don't match"

    so ``make lvs`` exits 0 whether the netlists match or not. Trusting the
    return code would certify a layout as LVS-clean when it is not — a false
    pass inside the layer the whole agent/verification split depends on. DRC is
    the same shape: the report is written either way, and the violation count is
    the only thing that means anything.
    """
    stage: str                    # "drc" or "lvs"
    clean: bool | None            # None when no verdict could be found
    detail: str = ""
    violations: int | None = None   # DRC only


@dataclass
class OrfsFailure:
    """Why a stage failed, in a form worth handing back to an agent.

    A raw ORFS log is megabytes of tool chatter; what an agent needs is the
    tool's own error code and one line of explanation. ``DPL-0038`` /
    "Utilization greater than 100%" tells it the density is impossible for this
    design far more usefully than a stack of placement statistics.
    """
    stage: str                 # the make target that failed
    step: str | None = None    # ORFS sub-step, e.g. "4_1_cts"
    code: str | None = None    # tool error code, e.g. "DPL-0038"
    message: str = ""          # the one-line reason
    knobs: dict[str, str] = field(default_factory=dict)

    def as_hint(self) -> str:
        """One line, suitable for feeding straight back into a prompt."""
        knobs = ", ".join(f"{k}={v}" for k, v in sorted(self.knobs.items()))
        code = f" [{self.code}]" if self.code else ""
        return f"{knobs or '(defaults)'} -> failed at {self.step or self.stage}{code}: {self.message}"


@dataclass
class OrfsCollectResult:
    work_home: str
    files: dict[str, str]       # relpath -> text contents (errors="replace")
    skipped: dict[str, int]     # matched but over max_bytes_per_file; size shown


@dataclass
class OrfsMatchResult:
    work_home: str
    matches: list[tuple[str, int]]   # (relpath, size), first-seen order
    skipped: dict[str, int]


# ---------------------------------------------------------------------------
# Worker-side helpers (module level so they resolve by import on the worker)
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=8)
def _orfs_env(orfs_home: str) -> dict[str, str]:
    """The environment ``env.sh`` produces, captured rather than reimplemented.

    ``env.sh`` prepends the OpenROAD / yosys / kepler-formal bin directories to
    PATH and exports OPENROAD and FLOW_HOME. Sourcing it and reading back the
    result keeps us correct if upstream adds to it, and lets every later
    subprocess call run without a shell (so knob values are never re-parsed by
    one). Cached because it costs a subprocess and never changes for a given
    checkout.
    """
    env_sh = os.path.join(orfs_home, "env.sh")
    if not os.path.isfile(env_sh):
        raise FileNotFoundError(
            f"no env.sh at {env_sh} — is ORFS_HOME right? (expected the ORFS "
            f"checkout root, which is {DEFAULT_ORFS_HOME} in the chia-orfs image)"
        )
    out = subprocess.run(
        ["bash", "-lc", f'source "{env_sh}" >/dev/null 2>&1 && env -0'],
        capture_output=True, check=True,
    ).stdout
    env: dict[str, str] = {}
    for entry in out.split(b"\0"):
        if not entry:
            continue
        key, sep, value = entry.decode("utf-8", "replace").partition("=")
        if sep:
            env[key] = value
    return env


def _parse_design_config(path: str) -> tuple[str, str]:
    """(platform, design_nickname) from an ORFS ``config.mk``.

    ORFS derives its output directories from these two, and we need them to
    find the results the flow just wrote. ``DESIGN_NICKNAME ?= DESIGN_NAME``
    (variables.mk:6), so the nickname falls back to the name.
    """
    platform = name = nickname = None
    with open(path) as f:
        for line in f:
            m = re.match(r"\s*export\s+(\w+)\s*[:?]?=\s*(.*?)\s*$", line)
            if not m:
                continue
            key, value = m.group(1), m.group(2)
            if key == "PLATFORM":
                platform = value
            elif key == "DESIGN_NAME":
                name = value
            elif key == "DESIGN_NICKNAME":
                nickname = value
    if not platform or not (nickname or name):
        raise ValueError(
            f"{path}: could not find PLATFORM and DESIGN_NAME/DESIGN_NICKNAME"
        )
    return platform, (nickname or name)


def _dirs(work_home: str, platform: str, design: str, variant: str) -> dict[str, str]:
    """ORFS's four output directories (variables.mk:46-49)."""
    return {
        kind: os.path.join(work_home, kind, platform, design, variant)
        for kind in ("logs", "objects", "reports", "results")
    }


def _read_metrics(log_dir: str) -> tuple[dict, dict[str, dict]]:
    """(final report, {basename: per-stage JSON}) from a run's log directory."""
    stage_metrics: dict[str, dict] = {}
    for path in sorted(_glob.glob(os.path.join(log_dir, "*.json"))):
        try:
            with open(path) as f:
                stage_metrics[os.path.basename(path)] = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue  # a stage that died mid-write
    return stage_metrics.get("6_report.json", {}), stage_metrics


#: OpenROAD/yosys/KLayout errors look like "[ERROR DPL-0038] message"; ORFS's
#: own Tcl wrapper reports "Error: cts.tcl, 83 message".
_TOOL_ERROR_RE = re.compile(r"\[ERROR\s+([A-Z]{2,4}-\d{3,4})\]\s*(.+)")
_TCL_ERROR_RE = re.compile(r"^Error:\s*(\S+?),\s*\d+\s*(.+)", re.M)


def _extract_failure(stage: str, dirs: dict[str, str], knobs: dict[str, str],
                     stdout: str, stderr: str) -> OrfsFailure | None:
    """Find the actual reason a stage failed.

    Looks in the newest stage log first (where the tool writes its own error),
    then falls back to the make output. Returns None if nothing recognisable is
    found — better to say nothing than to invent a cause.
    """
    log_dir = dirs.get("logs", "")
    newest_step = None
    candidates: list[tuple[str, str]] = []
    if os.path.isdir(log_dir):
        logs = sorted(_glob.glob(os.path.join(log_dir, "*.log")),
                      key=os.path.getmtime, reverse=True)
        for path in logs[:3]:
            try:
                with open(path, errors="replace") as f:
                    text = f.read()[-200_000:]
            except OSError:
                continue
            step = os.path.basename(path)[:-4]
            candidates.append((step, text))
            if newest_step is None:
                newest_step = step
    candidates.append((newest_step, (stdout or "") + "\n" + (stderr or "")))

    for step, text in candidates:
        m = _TOOL_ERROR_RE.search(text)
        if m:
            return OrfsFailure(stage=stage, step=step, code=m.group(1),
                               message=m.group(2).strip()[:300], knobs=dict(knobs))
    for step, text in candidates:
        m = _TCL_ERROR_RE.search(text)
        if m:
            return OrfsFailure(stage=stage, step=step, code=None,
                               message=f"{m.group(1)}: {m.group(2).strip()}"[:300],
                               knobs=dict(knobs))
    return None


#: KLayout writes DRC violations as <item> elements in the report database.
_DRC_ITEM_RE = re.compile(r"<item>")


def _read_signoff(stage: str, dirs: dict[str, str]) -> OrfsSignoff | None:
    """Read the real DRC/LVS verdict out of the report. See :class:`OrfsSignoff`."""
    if stage == "drc":
        path = os.path.join(dirs["reports"], "6_drc.lyrdb")
        if not os.path.isfile(path):
            return OrfsSignoff(stage, None, "no DRC report was written")
        try:
            with open(path, errors="replace") as f:
                count = len(_DRC_ITEM_RE.findall(f.read()))
        except OSError as exc:
            return OrfsSignoff(stage, None, f"could not read DRC report: {exc}")
        return OrfsSignoff(stage, count == 0, f"{count} violation(s)", violations=count)

    if stage == "lvs":
        path = os.path.join(dirs["logs"], "6_lvs.log")
        if not os.path.isfile(path):
            return OrfsSignoff(stage, None, "no LVS log was written")
        try:
            with open(path, errors="replace") as f:
                text = f.read()
        except OSError as exc:
            return OrfsSignoff(stage, None, f"could not read LVS log: {exc}")
        if "Congratulations" in text and "Netlists match" in text:
            return OrfsSignoff(stage, True, "netlists match")
        if "Netlists don't match" in text:
            return OrfsSignoff(stage, False, "netlists do not match")
        # No verdict at all usually means the deck died before comparing —
        # a parse error on the CDL, say. Not clean, but not a mismatch either.
        return OrfsSignoff(stage, None, "LVS produced no verdict (deck failed before compare?)")
    return None


def _summarize(metrics: dict, stage_metrics: dict[str, dict]) -> dict[str, float]:
    """SUMMARY_KEYS resolved against the final report, then any stage JSON.

    Before ``finish`` runs there is no ``6_report.json``, but the ``finish__*``
    keys already appear in late-stage JSONs — so fall back to scanning them
    newest-first rather than returning nothing.
    """
    out: dict[str, float] = {}
    sources = [metrics] + [stage_metrics[k] for k in sorted(stage_metrics, reverse=True)]
    for label, key in SUMMARY_KEYS.items():
        for source in sources:
            if key in source:
                out[label] = source[key]
                break
    return out


def _coerce(name: str, value) -> str:
    """Stringify a knob value, checking it against ORFS's declared type.

    Only 15 of ORFS's 249 variables declare a type, so this catches what it
    can and lets the rest through. Booleans become 1/0 because that is what
    ORFS's Tcl expects — Python's "True" would reach make as a bare word.
    """
    if isinstance(value, bool):
        return "1" if value else "0"
    declared = KNOB_TYPE.get(name)
    if declared in ("int", "float"):
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(
                f"knob {name} is declared {declared} by ORFS, got {value!r}")
        if declared == "int" and number != int(number):
            raise ValueError(f"knob {name} is declared int by ORFS, got {value!r}")
    return str(value)


def _validate_knobs(knobs: dict | None, *, allow_unknown: bool = False) -> dict[str, str]:
    """Check each knob is a real ORFS variable with a plausible value.

    Four checks, in increasing order of what ORFS can tell us:

    1. **Shape.** Each knob becomes one ``KEY=VALUE`` argv entry. No shell is
       involved, so a *value* cannot inject a command — but an unconstrained
       *key* could add a make flag (``-B``) or a second target (``clean``), so
       keys must look like make variables.
    2. **Existence.** An unknown name is almost always a typo, and make accepts
       it in silence: ``make finish TOTALLY_MADE_UP_KNOB=7`` runs happily and
       changes nothing. The caller then believes it swept a parameter it never
       touched — which would quietly corrupt an experiment. Rejecting beats
       that. ``allow_unknown`` exists for a newer ORFS than our generated table.
    3. **Deprecation.** ORFS flags these; accepting one silently is a trap.
    4. **Type**, where ORFS declares one (15 of 249 variables).

    Ranges are deliberately *not* checked here. ORFS publishes none, and sane
    bounds are design-specific — CORE_UTILIZATION has roughly a +-2 window
    around 38 on gcd/sky130hd. That belongs in per-design calibration.
    """
    clean: dict[str, str] = {}
    for key, value in (knobs or {}).items():
        name = str(key)
        if not _KNOB_KEY_RE.match(name):
            raise ValueError(
                f"illegal knob name {name!r}: ORFS make variables are UPPER_SNAKE")
        if name in DEPRECATED:
            raise ValueError(f"knob {name} is deprecated in ORFS; it has no effect")
        if name not in KNOWN and not allow_unknown:
            close = [k for k in KNOWN if k.startswith(name[:6])][:3]
            raise ValueError(
                f"unknown ORFS variable {name!r} — make would accept it and "
                f"silently change nothing"
                + (f". Did you mean {', '.join(close)}?" if close else "")
                + ". Pass allow_unknown=True to override.")
        text = _coerce(name, value)
        if "\n" in text:
            raise ValueError(f"knob {name!r} value contains a newline")
        clean[name] = text
    return clean


def earliest_affected_stage(knob_names) -> str | None:
    """The earliest stage any of *knob_names* touches, or None if none are known.

    ORFS's own docs list a variable under every stage that reads it, so a knob
    may appear under several. The earliest one is what matters: change the knob
    and every stage from there on is stale.

    The mapping is therefore *conservative*. ``ROUTING_LAYER_ADJUSTMENT`` is
    documented under floorplan as well as grt/route, so we invalidate from
    floorplan even though intuition says routing. Safe, sometimes wasteful.
    Unknown names (a typo, or a variable ORFS does not document) return the
    earliest stage of the rest — an unknown knob is never a reason to skip
    invalidation.
    """
    stages = [KNOB_STAGE[k] for k in knob_names if k in KNOB_STAGE]
    if len(stages) != len(list(knob_names)):
        # At least one unknown knob: we cannot reason about it, so assume the
        # worst and rebuild everything.
        return STAGE_ORDER[0]
    if not stages:
        return None
    return min(stages, key=STAGE_ORDER.index)


def _read_knob_manifest(work_home: str) -> dict[str, str]:
    try:
        with open(os.path.join(work_home, KNOB_MANIFEST)) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _write_knob_manifest(work_home: str, knobs: dict[str, str]) -> None:
    with open(os.path.join(work_home, KNOB_MANIFEST), "w") as f:
        json.dump(knobs, f, indent=1, sort_keys=True)


def _invalidate_from(dirs: dict[str, str], stage: str) -> list[str]:
    """Delete every artifact from *stage* onward. Returns what was removed.

    This exists because **make does not invalidate on variable change** —
    verified with `make -n`: changing CORE_UTILIZATION, CTS_CLUSTER_SIZE, or
    even a knob that does not exist all report "all up to date". Make compares
    file timestamps, and a knob is not a file. Without this, changing a knob in
    a populated WORK_HOME silently returns the previous run's results under the
    new knob's label.
    """
    first = STAGE_PREFIX[stage]
    removed: list[str] = []
    for kind in ("results", "logs", "reports", "objects"):
        base = dirs.get(kind)
        if not base or not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            m = re.match(r"(\d)_", name)
            if not m or int(m.group(1)) < first:
                continue
            path = os.path.join(base, name)
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path)
                else:
                    os.unlink(path)
                removed.append(os.path.join(kind, name))
            except OSError:
                pass
    return removed


def _list_files(base_dir: str) -> dict[str, int]:
    listing: dict[str, int] = {}
    for root, _dirs_, names in os.walk(base_dir):
        for name in names:
            path = os.path.join(root, name)
            try:
                listing[os.path.relpath(path, base_dir)] = os.path.getsize(path)
            except OSError:
                pass
    return listing


def _match_files(
    base_dir: str, patterns: list[str], max_bytes_per_file: int | None,
) -> tuple[list[tuple[str, str, int]], dict[str, int]]:
    """Resolve globs relative to *base_dir* into (relpath, abspath, size).

    Same glob/dedup/cap semantics as ``chia.vlsi.hammer._match_files`` so the
    two nodes behave identically for callers that use both.
    """
    matches: list[tuple[str, str, int]] = []
    skipped: dict[str, int] = {}
    seen: set[str] = set()
    for pattern in patterns:
        for path in _glob.glob(os.path.join(base_dir, pattern), recursive=True):
            if not os.path.isfile(path):
                continue
            rel = os.path.relpath(path, base_dir)
            if rel in seen:
                continue
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            seen.add(rel)
            if max_bytes_per_file and size > max_bytes_per_file:
                skipped[rel] = size
                continue
            matches.append((rel, path, size))
    return matches, skipped


#: Where to check feasibility before committing to the expensive part of the
#: flow. Measured on gcd/sky130hd (91 s of tool time): everything through cts
#: is 11 s (12%), global+detailed routing is 68 s (75%), finishing is 9 s.
#: Routing's share only grows with design size, so gating at cts costs an
#: eighth and saves seven eighths whenever a configuration cannot build.
DEFAULT_GATE_STAGE = "cts"


def run_flow(
    run,
    work_home: str,
    design_config: str,
    knobs: dict | None = None,
    *,
    gate: str | None = DEFAULT_GATE_STAGE,
    target: str = "finish",
    **kwargs,
) -> list[OrfsStageResult]:
    """Run to *target*, stopping early if the *gate* stage fails.

    An agent proposing physical-design knobs has no way to know this design's
    feasible band — ``CORE_UTILIZATION=50`` is unremarkable in general but
    infeasible on gcd/sky130hd, where CTS buffer insertion pushes utilization
    past 100%. Those proposals are going to happen, and paying for a full
    route on each one wastes both compute budget and the tool-invocation
    measurement the evaluation depends on.

    So: build to the gate first, and only continue when it succeeds.

    Args:
        run: A callable ``(stage, **kwargs) -> OrfsStageResult``. Passing it in
            rather than binding to a node keeps this usable both through
            ``node.run_stage.chia_remote`` and as a direct local call.
        gate: Stage to prove first. None runs straight to *target*. Ignored if
            it is not earlier than *target*.
        target: The stage actually wanted.

    Returns the results in order. If the gate fails, that is the only element,
    and its ``.failure`` says why.
    """
    common = dict(work_home=work_home, design_config=design_config,
                  knobs=knobs, **kwargs)
    stages = [target]
    if gate and gate in STAGE_PREFIX and STAGE_PREFIX[gate] < STAGE_PREFIX[target]:
        stages = [gate, target]

    results: list[OrfsStageResult] = []
    for stage in stages:
        result = run(stage, **common)
        results.append(result)
        if not result.success:
            logger.info("flow stopped at %s: %s", stage,
                        result.failure.as_hint() if result.failure else "no reason found")
            break
    return results


# ---------------------------------------------------------------------------
# OpenROADNode
# ---------------------------------------------------------------------------

class OpenROADNode(ColocatedNode):
    """ORFS run / collect primitives sharing one placement.

    Members are ``@staticmethod @ChiaFunction(resources={"orfs": 1})``;
    ``__init__`` re-binds each into a pinned form so ``node.<fn>.chia_remote(...)``
    lands on this node's bundle, while ``OpenROADNode.<fn>.chia_remote(...)``
    (the class attribute) stays unpinned::

        with OpenROADNode() as node:                 # reserves {"CPU":1,"orfs":1}
            r = get(node.run_stage.chia_remote(
                "finish",
                work_home="/scratch/cand-0017",
                design_config="./designs/sky130hd/gcd/config.mk",
                knobs={"CORE_UTILIZATION": 45, "PLACE_DENSITY": 0.55},
            ))
            print(r.summary["clock_skew_setup"], r.checkpoint)

    Per-call resource overrides layer on top of the pinning, which is how one
    parameterized member still gives each stage its own footprint — detailed
    routing is the memory high-water mark and can ask for more::

        node.run_stage.options(memory=8 * 1024**3).chia_remote("route", ...)

    One node instance per *candidate*: the placement group is what guarantees
    every stage of that candidate sees the same WORK_HOME.

    Not here yet: generating ``config.mk``/``constraint.sdc`` for RTL that is
    not already an ORFS design. Deferred until there is a non-stock design to
    test it against, rather than shipped untested.
    """

    _MEMBER_FNS = ("run_stage", "read_metrics", "collect", "list_matches")
    _DEFAULT_BUNDLE = {"CPU": 1, "orfs": 1}

    @staticmethod
    @ChiaFunction(resources={"orfs": 1})
    def run_stage(
        stage: str,
        work_home: str,
        design_config: str,
        knobs: dict | None = None,
        *,
        orfs_home: str = DEFAULT_ORFS_HOME,
        variant: str = "base",
        lec_check: bool = False,
        extra_make_args: list[str] | None = None,
        force_rebuild: bool = False,
        allow_unknown_knobs: bool = False,
        timeout_seconds: int = 86400,
    ) -> OrfsStageResult:
        """Run ``make <stage>`` for one design/knob combination on a worker.

        Because make resolves each stage's prerequisites, asking for a late
        stage runs (or resumes) everything before it. ``run_stage("finish")``
        on an empty WORK_HOME is a full RTL-to-GDS flow.

        Args:
            stage: One of :data:`STAGES`.
            work_home: ORFS ``WORK_HOME`` on the worker — the root of this
                candidate's results/logs/reports/objects. Use one per candidate
                so candidates never share a checkpoint tree.
            design_config: ``DESIGN_CONFIG``, a ``config.mk`` path. Relative
                paths resolve against ``<orfs_home>/flow`` (make's directory),
                matching how ORFS's own docs write it.
            knobs: ORFS make variables, e.g. ``{"CORE_UTILIZATION": 45}``.
                Keys must be UPPER_SNAKE; values are stringified.
            orfs_home: ORFS checkout root on the worker.
            variant: ``FLOW_VARIANT``; ORFS defaults to ``base``.
            lec_check: ORFS's optional kepler-formal equivalence check. Off by
                default because that binary uses AVX and dies under Rosetta —
                fine on real x86 workers, fatal on an Apple Silicon dev box.
            extra_make_args: Appended verbatim, for flags a knob can't express.
            force_rebuild: Discard every artifact and rerun the whole flow,
                regardless of what the knob manifest says.
            allow_unknown_knobs: Accept knob names absent from the generated
                ORFS variable table — for a newer ORFS than the table was built
                from. Off by default, because an unknown knob is usually a typo
                that make would ignore in silence.
            timeout_seconds: Wall-clock limit for the whole make invocation.
        """
        if stage not in STAGES:
            raise ValueError(f"unknown stage {stage!r}; expected one of {STAGES}")

        flow_dir = os.path.join(orfs_home, "flow")
        work_home = os.path.abspath(work_home)
        os.makedirs(work_home, exist_ok=True)

        config_path = (design_config if os.path.isabs(design_config)
                       else os.path.normpath(os.path.join(flow_dir, design_config)))
        platform, design = _parse_design_config(config_path)
        dirs = _dirs(work_home, platform, design, variant)

        clean_knobs = _validate_knobs(knobs, allow_unknown=allow_unknown_knobs)

        # Make decides staleness by file timestamp, and a knob is not a file —
        # verified with `make -n`, which reports "all up to date" after any knob
        # change, including a knob that does not exist. So compare against the
        # knobs that actually produced this WORK_HOME and drop the stages they
        # invalidate. Changing only a CTS knob keeps synth/floorplan/place,
        # which is what makes a CTS sweep cheap.
        invalidated_from = None
        invalidated: list[str] = []
        if not force_rebuild:
            prior = _read_knob_manifest(work_home)
            changed = {k for k in set(prior) | set(clean_knobs)
                       if prior.get(k) != clean_knobs.get(k)}
            if changed and prior:
                invalidated_from = earliest_affected_stage(changed)
                if invalidated_from:
                    invalidated = _invalidate_from(dirs, invalidated_from)
                    logger.info("knobs changed (%s) -> invalidated from %s, "
                                "removed %d artifact(s)",
                                ", ".join(sorted(changed)), invalidated_from,
                                len(invalidated))
        else:
            invalidated_from = STAGE_ORDER[0]
            invalidated = _invalidate_from(dirs, invalidated_from)

        # -j1 is deliberate: parallel make races on the shared ODB targets.
        # Parallelism comes from running many candidates, not from within one.
        cmd = [
            "make", "-C", flow_dir, "-j1", stage,
            f"WORK_HOME={work_home}",
            f"DESIGN_CONFIG={design_config}",
            f"FLOW_VARIANT={variant}",
            f"LEC_CHECK={1 if lec_check else 0}",
        ]
        cmd += [f"{k}={v}" for k, v in clean_knobs.items()]
        cmd += extra_make_args or []

        env = dict(_orfs_env(orfs_home))
        logger.info("ORFS %s: %s (%d knob(s))", stage, design, len(clean_knobs))

        started = time.monotonic()
        # start_new_session puts the whole tool tree in one process group so
        # chia's pid_registry can kill it as a unit on cancellation.
        proc = subprocess.Popen(
            cmd, cwd=flow_dir, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate()
            stderr = (stderr or "") + f"\nORFS {stage} timed out after {timeout_seconds}s"
            logger.error("ORFS %s timed out after %ss", stage, timeout_seconds)
        elapsed = time.monotonic() - started

        # Record what produced this tree, so the next call can diff against it.
        # Written even on failure: the artifacts that *did* get built came from
        # these knobs, and a later call must invalidate against them correctly.
        _write_knob_manifest(work_home, clean_knobs)

        metrics, stage_metrics = _read_metrics(dirs["logs"])
        kind, filename = STAGE_CHECKPOINT[stage]
        checkpoint = os.path.join(dirs[kind], filename)

        # DRC and LVS report their verdict in the report, not the exit code.
        signoff = _read_signoff(stage, dirs)
        success = proc.returncode == 0
        if signoff is not None and signoff.clean is not True:
            success = False
            logger.error("ORFS %s did not pass: %s (make exited %s)",
                         stage, signoff.detail, proc.returncode)

        failure = None
        if proc.returncode != 0:
            failure = _extract_failure(stage, dirs, clean_knobs, stdout, stderr)
            logger.error("ORFS %s failed (rc=%s): %s", stage, proc.returncode,
                         failure.as_hint() if failure else "(no recognisable error)")

        if not success and failure is None and signoff is not None:
            failure = OrfsFailure(stage=stage, step=stage,
                                  message=signoff.detail, knobs=dict(clean_knobs))

        return OrfsStageResult(
            success=success,
            returncode=proc.returncode,
            stage=stage,
            work_home=work_home,
            design_config=design_config,
            platform=platform,
            design=design,
            variant=variant,
            knobs=clean_knobs,
            elapsed_s=elapsed,
            checkpoint=checkpoint if os.path.exists(checkpoint) else None,
            metrics=metrics,
            stage_metrics=stage_metrics,
            summary=_summarize(metrics, stage_metrics),
            signoff=signoff,
            failure=failure,
            invalidated_from=invalidated_from,
            invalidated=invalidated,
            # Tails only: a full ORFS log is megabytes and would go through the
            # object store on every call. Use `collect` for the real logs.
            stdout_tail=(stdout or "")[-4000:],
            stderr_tail=(stderr or "")[-4000:],
        )

    @staticmethod
    @ChiaFunction(resources={"orfs": 1})
    def read_metrics(
        work_home: str,
        design_config: str,
        *,
        orfs_home: str = DEFAULT_ORFS_HOME,
        variant: str = "base",
    ) -> OrfsStageResult:
        """Re-read a WORK_HOME's metrics without running anything.

        For inspecting a candidate produced by an earlier call (or recovered
        from cache) without paying for the flow again. ``returncode`` is 0 and
        ``stage`` is ``"read"``; only the metric fields are meaningful.
        """
        flow_dir = os.path.join(orfs_home, "flow")
        work_home = os.path.abspath(work_home)
        config_path = (design_config if os.path.isabs(design_config)
                       else os.path.normpath(os.path.join(flow_dir, design_config)))
        platform, design = _parse_design_config(config_path)
        dirs = _dirs(work_home, platform, design, variant)

        metrics, stage_metrics = _read_metrics(dirs["logs"])
        gds = os.path.join(dirs["results"], "6_final.gds")
        return OrfsStageResult(
            success=True, returncode=0, stage="read",
            work_home=work_home, design_config=design_config,
            platform=platform, design=design, variant=variant,
            knobs={}, elapsed_s=0.0,
            checkpoint=gds if os.path.exists(gds) else None,
            metrics=metrics, stage_metrics=stage_metrics,
            summary=_summarize(metrics, stage_metrics),
        )

    @staticmethod
    @ChiaFunction(resources={"orfs": 1})
    def collect(
        work_home: str,
        patterns: list[str],
        max_bytes_per_file: int | None = 4 * 1024 * 1024,
    ) -> OrfsCollectResult:
        """Fetch text files out of a WORK_HOME on this worker.

        Dispatch through the pinned member (``node.collect.chia_remote``) so it
        lands on the worker that owns work_home. Text only — GDS and ODB stay
        put; ``list_matches`` will tell you they exist.

        Args:
            work_home: The run root a previous stage used.
            patterns: Globs relative to work_home (``**`` recursive), e.g.
                ``["logs/**/*.log", "reports/**/*.rpt"]``.
            max_bytes_per_file: Files above this are reported in ``skipped``
                rather than shipped through the object store. Defaults to 4 MiB
                so a stray glob cannot pull a netlist into the driver.
        """
        work_home = os.path.abspath(work_home)
        matches, skipped = _match_files(work_home, patterns, max_bytes_per_file)
        files: dict[str, str] = {}
        for rel, path, _size in matches:
            with open(path, errors="replace") as f:
                files[rel] = f.read()
        if skipped:
            logger.warning("ORFS collect skipped %d file(s) over %s bytes: %s",
                           len(skipped), max_bytes_per_file, sorted(skipped)[:5])
        return OrfsCollectResult(work_home=work_home, files=files, skipped=skipped)

    @staticmethod
    @ChiaFunction(resources={"orfs": 1})
    def list_matches(
        work_home: str,
        patterns: list[str] | None = None,
        max_bytes_per_file: int | None = None,
    ) -> OrfsMatchResult:
        """List what a WORK_HOME holds, without shipping any of it.

        Defaults to everything, which is how you find out that a GDS exists and
        how big it is before deciding whether to move it.
        """
        work_home = os.path.abspath(work_home)
        matches, skipped = _match_files(
            work_home, patterns or ["**/*"], max_bytes_per_file)
        return OrfsMatchResult(
            work_home=work_home,
            matches=[(rel, size) for rel, _path, size in matches],
            skipped=skipped,
        )
