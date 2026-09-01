"""The gcd tree has a CLEAN drc report and a FAILING lvs log — real fixtures."""
import sys
from chia_openroad.openroad import OpenROADNode
run = OpenROADNode.run_stage._chia_original
W, D = "/work", "./designs/sky130hd/gcd/config.mk"
fails = []
def check(l, c, d=""):
    print(f"  [{'ok  ' if c else 'FAIL'}] {l}" + (f"  — {d}" if d else "")); fails.append(l) if not c else None

r = run("drc", work_home=W, design_config=D)
check("DRC verdict read from the report", r.signoff is not None and r.signoff.clean is True,
      r.signoff.detail if r.signoff else "none")
check("DRC success", r.success, f"rc={r.returncode}")
check("violation count exposed", r.signoff and r.signoff.violations == 0, str(r.signoff.violations))

r2 = run("lvs", work_home=W, design_config=D)
print(f"       make exited {r2.returncode}  (0 means it did NOT signal failure)")
check("LVS verdict read from the log", r2.signoff is not None and r2.signoff.clean is False,
      r2.signoff.detail if r2.signoff else "none")
check("success is False despite make exiting 0", r2.success is False,
      f"success={r2.success} returncode={r2.returncode}")
check("failure explains it", r2.failure is not None,
      r2.failure.as_hint()[:80] if r2.failure else "none")
print("\n" + ("FAILED: " + ", ".join(fails) if fails else "ALL CHECKS PASSED"))
sys.exit(1 if fails else 0)
