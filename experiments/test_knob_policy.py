"""Policy + calibration, tested without touching ORFS (a fake `run`)."""
import sys
from dataclasses import dataclass
from chia_openroad.knob_policy import KnobPolicy, KnobSpec, DEFAULT_KNOBS, EXCLUDED
from chia_openroad.calibrate import calibrate_knob, apply

fails = []
def check(l, c, d=""):
    print(f"  [{'ok  ' if c else 'FAIL'}] {l}" + (f"  — {d}" if d else ""))
    if not c: fails.append(l)

print("=== policy gate ===")
p = KnobPolicy()
check("accepts an in-range knob", p.check({"CORE_UTILIZATION": 45})[0])
check("rejects out of range", not p.check({"CORE_UTILIZATION": 95})[0],
      p.check({"CORE_UTILIZATION": 95})[1][:60])
check("rejects a knob outside the legal set", not p.check({"SYNTH_HIERARCHICAL": 1})[0],
      p.check({"SYNTH_HIERARCHICAL": 1})[1][:55])
check("explains a documented exclusion",
      "no OpenROAD equivalent" in p.check({"CTS_CLK_MAX_WIRE_LENGTH": 200})[1])
check("rejects a non-member of a choice knob", not p.check({"CORE_ASPECT_RATIO": 3.0})[0])
check("int knob rejects a fractional value", not p.check({"CTS_CLUSTER_SIZE": 20.5})[0])
check("flags everything as uncalibrated", "not been measured" in p.describe_for_agent())

print("\n=== policy audits itself against ORFS ===")
try:
    KnobPolicy((KnobSpec("NOT_A_REAL_ORFS_VAR", "cts", "int", low=1, high=2),))
    check("rejects a policy naming a non-ORFS variable", False, "no exception")
except ValueError as e:
    check("rejects a policy naming a non-ORFS variable", True, str(e)[:55])
try:
    KnobPolicy((KnobSpec("CTS_CLUSTER_SIZE", "route", "int", low=1, high=2),))
    check("rejects a wrong stage claim", False, "no exception")
except ValueError as e:
    check("rejects a wrong stage claim", True, str(e)[:60])

print("\n=== calibration finds the real edge ===")
# Fake ORFS: builds only for 30 <= CORE_UTILIZATION <= 44, like gcd's narrow band.
@dataclass
class FakeResult:
    success: bool; stage: str = "cts"; failure = None; returncode: int = 0
    elapsed_s: float = 1.0; checkpoint = None; summary = None
calls = []
def fake_run(stage, work_home=None, design_config=None, knobs=None, **kw):
    v = float(knobs["CORE_UTILIZATION"]); calls.append(v)
    return FakeResult(success=30.0 <= v <= 44.0)

spec = [k for k in DEFAULT_KNOBS if k.name == "CORE_UTILIZATION"][0]
cal = calibrate_knob(fake_run, spec, "cfg.mk", "/tmp/cal", budget=14)
print(f"       probed {sorted(set(calls))}")
check("found the upper edge", cal.high is not None and 42 <= cal.high <= 44.5, str(cal.high))
check("found the lower edge", cal.low is not None and 29 <= cal.low <= 32, str(cal.low))
check("narrower than nominal", cal.high - cal.low < 40, f"{cal.low}-{cal.high} vs 20-60")
check("stayed inside budget", cal.probes <= 14, f"{cal.probes} probes")
check("recorded what failed", len(cal.failures) > 0, f"{len(cal.failures)} failures")

print("\n=== calibrated policy is marked and enforced ===")
p2 = apply(KnobPolicy((spec,)), {"CORE_UTILIZATION": cal})
check("marked calibrated", p2.knobs["CORE_UTILIZATION"].calibrated)
check("no uncalibrated warning", "not been measured" not in p2.describe_for_agent())
check("now rejects a value the nominal range allowed", not p2.check({"CORE_UTILIZATION": 55})[0],
      p2.check({"CORE_UTILIZATION": 55})[1][:60])

print("\n" + ("FAILED: " + ", ".join(fails) if fails else "ALL CHECKS PASSED"))
sys.exit(1 if fails else 0)
