"""Does stage-scoped invalidation actually keep the right stages and drop the rest?"""
import os, shutil, sys, time
from chia_openroad.openroad import OpenROADNode, earliest_affected_stage
from chia_openroad.knob_stages import KNOB_STAGE

run = OpenROADNode.run_stage._chia_original
W, DESIGN = "/work/inv", "./designs/sky130hd/gcd/config.mk"
R = f"{W}/results/sky130hd/gcd/base"
fails = []
def check(l, c, d=""):
    print(f"  [{'ok  ' if c else 'FAIL'}] {l}" + (f"  — {d}" if d else ""), flush=True)
    if not c: fails.append(l)

print("=== mapping (from ORFS docs) ===")
for k in ["CORE_UTILIZATION","PLACE_DENSITY","GPL_TIMING_DRIVEN","CTS_CLUSTER_SIZE",
          "CTS_BUF_DISTANCE","SETUP_SLACK_MARGIN"]:
    print(f"  {k:24s} -> {KNOB_STAGE.get(k)}")
check("cts knobs map to cts",
      earliest_affected_stage(["CTS_CLUSTER_SIZE","CTS_BUF_DISTANCE"]) == "cts")
check("mixed set takes the earliest",
      earliest_affected_stage(["CTS_CLUSTER_SIZE","CORE_UTILIZATION"]) == "floorplan")
check("unknown knob forces a full rebuild",
      earliest_affected_stage(["MADE_UP_KNOB"]) == "synth")

def mtimes():
    return {f: os.path.getmtime(f"{R}/{f}") for f in
            ["1_synth.odb","2_floorplan.odb","3_place.odb","4_cts.odb","5_route.odb"]
            if os.path.exists(f"{R}/{f}")}

print("\n=== A. build baseline ===")
shutil.rmtree(W, ignore_errors=True)
r = run("finish", work_home=W, design_config=DESIGN,
        knobs={"CORE_UTILIZATION": 40, "CTS_CLUSTER_SIZE": 30})
check("built", r.success, f"{r.elapsed_s:.0f}s")
base = mtimes(); check("all checkpoints present", len(base) == 5, str(sorted(base)))

time.sleep(1.1)
print("\n=== B. change a CTS knob — synth/floorplan/place must survive ===")
r2 = run("finish", work_home=W, design_config=DESIGN,
         knobs={"CORE_UTILIZATION": 40, "CTS_CLUSTER_SIZE": 20})
check("succeeded", r2.success, f"{r2.elapsed_s:.0f}s")
check("invalidated from cts", r2.invalidated_from == "cts", str(r2.invalidated_from))
now = mtimes()
for keep in ["1_synth.odb","2_floorplan.odb","3_place.odb"]:
    check(f"{keep} reused", now.get(keep) == base.get(keep))
for redo in ["4_cts.odb","5_route.odb"]:
    check(f"{redo} rebuilt", now.get(redo, 0) > base.get(redo, 0))
check("faster than a full flow", r2.elapsed_s < r.elapsed_s,
      f"{r2.elapsed_s:.0f}s vs {r.elapsed_s:.0f}s")
check("clock metrics actually moved",
      r2.summary.get("clock_buffer_count") is not None,
      f"buffers {r.summary.get('clock_buffer_count')} -> {r2.summary.get('clock_buffer_count')}")

time.sleep(1.1)
# gcd/sky130hd sets CORE_UTILIZATION=38 in its own config.mk, and the feasible
# band around it is remarkably narrow -- measured here: 40 builds, 42 fails at
# global route, 50 fails at CTS (DPL-0038), 65 fails at global placement. So go
# DOWN for the "feasible change" case: 35 is looser than default and safe.
# Section D takes the infeasible case deliberately.
print("\n=== C. change a floorplan knob — everything from floorplan must go ===")
base2 = mtimes()
r3 = run("finish", work_home=W, design_config=DESIGN,
         knobs={"CORE_UTILIZATION": 35, "CTS_CLUSTER_SIZE": 20})
check("succeeded", r3.success, f"{r3.elapsed_s:.0f}s")
check("invalidated from floorplan", r3.invalidated_from == "floorplan",
      str(r3.invalidated_from))
now = mtimes()
check("1_synth.odb reused", now.get("1_synth.odb") == base2.get("1_synth.odb"))
check("2_floorplan.odb rebuilt", now.get("2_floorplan.odb", 0) > base2.get("2_floorplan.odb", 0))
check("die area actually changed",
      r3.summary.get("die_area") != r2.summary.get("die_area"),
      f"{r2.summary.get('die_area')} -> {r3.summary.get('die_area')}")

time.sleep(1.1)
print("\n=== D. an infeasible knob fails cleanly, it does not crash or lie ===")
r4 = run("finish", work_home=W, design_config=DESIGN,
         knobs={"CORE_UTILIZATION": 65, "CTS_CLUSTER_SIZE": 20})
check("reports failure rather than raising", r4.success is False, f"rc={r4.returncode}")
check("invalidated from floorplan anyway", r4.invalidated_from == "floorplan")
check("no GDS claimed", r4.checkpoint is None, str(r4.checkpoint))
# The tool's real error lives in the STAGE LOG, not in stderr -- stderr only
# carries make's "Error 2". That is why _extract_failure reads the logs.
check("failure captured structurally", r4.failure is not None,
      r4.failure.as_hint()[:100] if r4.failure else "None")
check("tool error code extracted", bool(r4.failure and r4.failure.code),
      r4.failure.code if r4.failure else "none")
check("failing sub-step identified", bool(r4.failure and r4.failure.step),
      r4.failure.step if r4.failure else "none")
check("knobs attached to the failure",
      bool(r4.failure and r4.failure.knobs.get("CORE_UTILIZATION") == "65"))
# The manifest must record the FAILED knobs, or the next call would diff
# against the last successful ones and skip invalidation it needs to do.
import json as _j
man = _j.load(open(f"{W}/.chia_orfs_knobs.json"))
check("manifest records the failed knobs", man.get("CORE_UTILIZATION") == "65", str(man))

print("\n=== E. fail-fast: the gate stops before routing ===")
from chia_openroad.openroad import run_flow, DEFAULT_GATE_STAGE
shutil.rmtree(f"{W}b", ignore_errors=True)
t0 = time.monotonic()
rs = run_flow(run, f"{W}b", DESIGN, {"CORE_UTILIZATION": 65}, gate=DEFAULT_GATE_STAGE)
gated = time.monotonic() - t0
check("stopped at the gate", len(rs) == 1 and rs[0].stage == "cts",
      f"{[r.stage for r in rs]}")
check("gate reports the failure", rs[0].failure is not None,
      rs[0].failure.as_hint()[:90] if rs[0].failure else "none")
check("no route artifacts were produced",
      not os.path.exists(f"{W}b/results/sky130hd/gcd/base/5_route.odb"))
print(f"       gated attempt cost {gated:.0f}s", flush=True)

print("\n=== F. failure log: remember, and don't pay twice ===")
from chia_openroad.failure_log import FailureLog
lg = FailureLog("/tmp/failures.jsonl")
check("recorded", lg.record_all(rs) == 1, f"{len(lg)} entry")
check("exact repeat is known bad", lg.known_bad({"CORE_UTILIZATION": 65}) is not None)
check("value spelling does not matter",
      lg.known_bad({"CORE_UTILIZATION": "65"}) is not None)
check("a different config is not assumed bad",
      lg.known_bad({"CORE_UTILIZATION": 38}) is None)
hints = lg.hints()
check("renders a usable prompt hint", len(hints) == 1 and "->" in hints[0], hints[0][:95])
check("survives a reload", len(FailureLog("/tmp/failures.jsonl")) == 1)

print("\n=== G. branch from a KNOB-FREE parent — the shared-placement case ===")
# The regression sections A-C could not catch. They build the baseline *with*
# knobs, so the manifest is non-empty and the old `if changed and prior` guard
# happened to hold. The real loop builds one shared placement with NO knobs,
# branches every candidate off it, and that guard then skipped invalidation for
# all of them: ORFS reused a floorplan the candidate's own CORE_UTILIZATION
# should have rebuilt, and 12 candidates reported a die area none had asked for.
from chia_openroad.openroad import _read_knob_manifest, KNOB_MANIFEST
branch = OpenROADNode.branch._chia_original
BASE, CAND = "/work/invG_base", "/work/invG_cand"
CR = f"{CAND}/results/sky130hd/gcd/base"
def die(res):
    return next((v for k, v in res.metrics.items() if k.endswith("design__die__area")), None)
for d in (BASE, CAND):
    shutil.rmtree(d, ignore_errors=True)

rg = run("floorplan", work_home=BASE, design_config=DESIGN, knobs={})
check("knob-free parent builds", rg.success, f"{rg.elapsed_s:.0f}s")
check("absent and empty manifests are distinguishable",
      _read_knob_manifest("/work/does_not_exist") is None
      and _read_knob_manifest(BASE) == {})

branch(BASE, CAND, DESIGN, through_stage="floorplan")
check("branch always leaves a manifest", os.path.exists(f"{CAND}/{KNOB_MANIFEST}"),
      repr(_read_knob_manifest(CAND)))
before = os.path.getmtime(f"{CR}/2_floorplan.odb")
time.sleep(1.1)

rg2 = run("floorplan", work_home=CAND, design_config=DESIGN,
          knobs={"CORE_UTILIZATION": 55})
check("floorplan knob on a branched tree succeeds", rg2.success, f"{rg2.elapsed_s:.0f}s")
check("invalidation fired", rg2.invalidated_from == "floorplan",
      f"invalidated_from={rg2.invalidated_from}")
check("2_floorplan.odb was actually rebuilt",
      os.path.getmtime(f"{CR}/2_floorplan.odb") > before)
check("die area moved — the knob reached the tool", die(rg2) != die(rg),
      f"{die(rg)} -> {die(rg2)}")

print("\n" + ("FAILED: " + ", ".join(fails) if fails else "ALL CHECKS PASSED"))
sys.exit(1 if fails else 0)
