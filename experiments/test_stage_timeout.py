"""A timed-out stage must name itself, not degrade to 'unknown'."""
import shutil
from chia_openroad.openroad import OpenROADNode
run = OpenROADNode.run_stage._chia_original
W, DESIGN = "/work/tmo", "./designs/sky130hd/gcd/config.mk"
shutil.rmtree(W, ignore_errors=True)
fails = []
def ck(l, c, d=""):
    print(f"  [{'ok  ' if c else 'FAIL'}] {l}" + (f"  — {d}" if d else ""), flush=True)
    if not c: fails.append(l)

r = run("finish", work_home=W, design_config=DESIGN, knobs={}, timeout_seconds=12)
ck("a timed-out stage does not succeed", not r.success, f"rc={r.returncode}")
ck("a failure object exists", r.failure is not None)
if r.failure:
    ck("coded as TIMEOUT", r.failure.code == "TIMEOUT", r.failure.code)
    ck("names the step that ran long", bool(r.failure.step), r.failure.step)
    ck("hint is usable by an agent", "exceeded" in r.failure.as_hint(),
       r.failure.as_hint()[:110])
print("\n" + ("FAILED: " + ", ".join(fails) if fails else "TIMEOUT PATH PASSED"))
