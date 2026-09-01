"""Acceptance test for chia_openroad.openroad.OpenROADNode.

Runs inside the chia-orfs image, because that is where ORFS lives.

Two modes, because Ray does not run under Rosetta on Apple Silicon (verified:
plain `ray.init()` with no chia involved dies with "Failed to register worker
to Raylet"). Note that even a *local* ChiaFunction call touches Ray —
`_wrapper` calls `get_profiler()`, which looks up a Ray actor — so on an
arm64 dev box the decorator is unusable in either direction.

  --no-ray   (default on this Mac)  exercise the function bodies directly via
             `._chia_original`, the raw undecorated function ChiaFunction
             keeps. Covers everything except CHIA dispatch.
  --ray      also exercise dispatch: placement group, .chia_remote, get().
             Run this on a native x86 worker (Step 3).

Usage:
    docker run --rm --platform linux/amd64 --shm-size=2g \
      -v "$PWD:/repo" -v "$PWD/experiments/smoke/step2:/work" \
      -e PYTHONPATH=/repo chia-orfs:latest \
      python -u /repo/experiments/test_openroad_node.py [--ray]
"""
import shutil
import sys
import time

from chia_openroad.openroad import OpenROADNode

USE_RAY = "--ray" in sys.argv
DESIGN = "./designs/sky130hd/gcd/config.mk"
WORK = "/work/cand-0001"
KNOBS = {"CORE_UTILIZATION": 40, "PLACE_DENSITY": 0.60}

failures = []
_node = None


def call(name, *args, **kwargs):
    """Invoke a member either raw (no Ray) or dispatched through CHIA."""
    if USE_RAY:
        from chia.base.ChiaFunction import get
        return get(getattr(_node, name).chia_remote(*args, **kwargs))
    return getattr(OpenROADNode, name)._chia_original(*args, **kwargs)


def check(label, condition, detail=""):
    print(f"  [{'ok  ' if condition else 'FAIL'}] {label}"
          + (f"  — {detail}" if detail else ""), flush=True)
    if not condition:
        failures.append(label)


def section(title):
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}", flush=True)


def run_all():
    section(f"A. run_stage('synth')   [{'ray dispatch' if USE_RAY else 'direct call'}]")
    shutil.rmtree(WORK, ignore_errors=True)
    t0 = time.monotonic()
    r = call("run_stage", "synth", work_home=WORK, design_config=DESIGN, knobs=KNOBS)
    check("success", r.success, f"rc={r.returncode}")
    check("platform/design parsed from config.mk",
          (r.platform, r.design) == ("sky130hd", "gcd"), f"{r.platform}/{r.design}")
    check("checkpoint written", r.checkpoint is not None,
          r.checkpoint.split("/")[-1] if r.checkpoint else "none")
    check("stage metrics parsed", "1_synth.json" in r.stage_metrics,
          f"{len(r.stage_metrics)} json file(s)")
    # Assert the property, not the spelling: str(0.60) is "0.6", and
    # PLACE_DENSITY=0.6 is what should reach make. Two knob values that mean
    # the same number must also stringify identically, or they would key two
    # different cache entries for the same run.
    check("knobs stringified, values preserved",
          all(isinstance(k, str) and isinstance(v, str) for k, v in r.knobs.items())
          and {k: float(v) for k, v in r.knobs.items()}
              == {k: float(v) for k, v in KNOBS.items()},
          str(r.knobs))
    check("log tail is capped", len(r.stdout_tail) <= 4000,
          f"{len(r.stdout_tail)} bytes")
    print(f"       elapsed {time.monotonic() - t0:.1f}s", flush=True)

    section("B. bad input rejected before make runs")
    for bad, why in [({"core_utilization": 40}, "lowercase knob name"),
                     ({"X; rm -rf /": 1}, "shell-ish knob name"),
                     ({"GOOD": "a\nb"}, "newline in knob value")]:
        try:
            call("run_stage", "synth", work_home=WORK, design_config=DESIGN, knobs=bad)
            check(f"rejects {why}", False, "no exception raised")
        except ValueError as e:
            check(f"rejects {why}", True, str(e)[:55])
    try:
        call("run_stage", "nonsense", work_home=WORK, design_config=DESIGN)
        check("rejects unknown stage", False, "no exception raised")
    except ValueError:
        check("rejects unknown stage", True)

    section("C. run_stage('finish') — full RTL-to-GDS, resuming from synth")
    t1 = time.monotonic()
    r2 = call("run_stage", "finish", work_home=WORK, design_config=DESIGN, knobs=KNOBS)
    first = time.monotonic() - t1
    check("success", r2.success, f"rc={r2.returncode}")
    check("GDS produced", bool(r2.checkpoint and r2.checkpoint.endswith("6_final.gds")))
    check("6_report.json read", bool(r2.metrics), f"{len(r2.metrics)} keys")
    for key in ("worst_slack", "power_total", "clock_skew_setup",
                "clock_buffer_count", "instance_area"):
        check(f"summary[{key}]", key in r2.summary, str(r2.summary.get(key)))
    print(f"       elapsed {first:.1f}s", flush=True)

    section("D. resume — same call again is a make no-op")
    t2 = time.monotonic()
    r3 = call("run_stage", "finish", work_home=WORK, design_config=DESIGN, knobs=KNOBS)
    second = time.monotonic() - t2
    check("success", r3.success)
    check("much faster than the first run", second < first / 5,
          f"{second:.1f}s vs {first:.1f}s")
    check("identical worst_slack",
          r3.summary.get("worst_slack") == r2.summary.get("worst_slack"),
          str(r3.summary.get("worst_slack")))

    section("E. read_metrics — no run, same numbers")
    r4 = call("read_metrics", WORK, DESIGN)
    check("summary matches the run that produced it",
          r4.summary == r2.summary, f"{len(r4.summary)} keys")

    section("F. collect / list_matches")
    listing = call("list_matches", WORK, ["results/**/6_final.gds"])
    check("GDS visible without shipping it", len(listing.matches) == 1, str(listing.matches))
    logs = call("collect", WORK, ["logs/**/6_report.json"])
    check("report collected as text", len(logs.files) == 1, str(list(logs.files)))
    big = call("collect", WORK, ["results/**/6_final.gds"], max_bytes_per_file=1024)
    check("oversized file skipped, not shipped",
          not big.files and len(big.skipped) == 1, str(big.skipped))


if USE_RAY:
    import ray
    ray.init(num_cpus=4, resources={"orfs": 2}, include_dashboard=False,
             logging_level="ERROR")
    with OpenROADNode() as node:
        _node = node
        run_all()
else:
    run_all()

print(f"\n{'=' * 70}")
print("FAILED: " + ", ".join(failures) if failures else "ALL CHECKS PASSED"
      + ("" if USE_RAY else "   (dispatch deferred to Step 3 — see module docstring)"))
print("=" * 70)
sys.exit(1 if failures else 0)
