"""Is the socket general? Written from a plugin author's side."""
import sys
from chia_openroad.surrogate import (
    DesignState, Prediction, Scorecard, SurrogateEvaluator, conformance_check,
    get, register, registered, ORFS_METRICS)
from chia_openroad.surrogates.feasibility import FeasibilityScreen
from chia_openroad.knob_policy import KnobPolicy
from chia_openroad.calibrate import KnobCalibration, apply

fails = []
def check(l, c, d=""):
    print(f"  [{'ok  ' if c else 'FAIL'}] {l}" + (f"  — {d}" if d else ""))
    if not c: fails.append(l)

print("=== 1. the minimal plugin is five lines ===")
class MySkewModel(SurrogateEvaluator):
    predicts = {"clock_skew_setup": "orfs_metric"}
    def _predict(self, state, candidates):
        return [{"clock_skew_setup": 0.004 + 0.0001 * c.get("CTS_CLUSTER_SIZE", 20)}
                for c in candidates]

m = MySkewModel()
check("no calibrate needed", m.calibrate(DesignState("gcd", "sky130hd")) is None)
check("defaults: observes nothing, requires nothing",
      m.observes_stage is None and m.requires == ())
check("name defaults to the class name", m.name == "MySkewModel", m.name)
check("conformant with no extra work", conformance_check(m, verbose=False))

print("\n=== 2. a very different surrogate fits the SAME abc ===")
f = FeasibilityScreen()
check("declares a probability, not a metric", f.predicts == {"p_builds": "probability"})
check("needs no stage and no artifacts", f.observes_stage is None and f.requires == ())
check("both satisfy one interface",
      isinstance(m, SurrogateEvaluator) and isinstance(f, SurrogateEvaluator))
check("registry resolves by name", get("feasibility") is FeasibilityScreen, str(registered()))

print("\n=== 3. feasibility learns gcd's real measured band ===")
# The actual outcomes we measured on gcd/sky130hd.
for util, built in [(35, True), (38, True), (40, True), (42, False),
                    (45, False), (50, False), (65, False)]:
    f.observe({"CORE_UTILIZATION": util}, built)
state = DesignState("gcd", "sky130hd")
probe = [{"CORE_UTILIZATION": u} for u in (30, 38, 41, 48, 60)]
ps = [p.values["p_builds"] for p in f.predict(state, probe)]
print("       " + "  ".join(f"CU={u}:{p:.2f}" for u, p in zip((30,38,41,48,60), ps)))
check("inside the measured band scores high", ps[1] > 0.6, f"CU=38 -> {ps[1]}")
check("outside it scores low", ps[3] < 0.4, f"CU=48 -> {ps[3]}")
check("ordering is monotone across the edge", ps[1] > ps[2] > ps[3] > ps[4])
check("policy-illegal config is impossible, not improbable",
      f.predict(state, [{"CORE_UTILIZATION": 999}])[0].values["p_builds"] == 0.0)

print("\n=== 4. the checker catches what would otherwise be invisible ===")
class WrongCount(SurrogateEvaluator):
    predicts = {"power_total": "orfs_metric"}
    def _predict(self, s, c): return [{"power_total": 1.0}]
class FakeMetric(SurrogateEvaluator):
    predicts = {"clock_skwe": "orfs_metric"}          # typo
    def _predict(self, s, c): return [{"clock_skwe": 0.1} for _ in c]
class Undeclared(SurrogateEvaluator):
    predicts = {"power_total": "orfs_metric"}
    def _predict(self, s, c): return [{"power_total": 1.0, "surprise": 2.0} for _ in c]
class BadProb(SurrogateEvaluator):
    predicts = {"p_builds": "probability"}
    def _predict(self, s, c): return [{"p_builds": 1.7} for _ in c]

for cls, why in [(WrongCount, "wrong number of predictions"),
                 (FakeMetric, "metric name that is not an ORFS metric"),
                 (Undeclared, "returning an undeclared key"),
                 (BadProb, "probability outside [0,1]")]:
    check(f"rejects {why}", not conformance_check(cls(), verbose=False))

print("\n=== 5. scorecard: rank correlation, not just error ===")
sc = Scorecard("MySkewModel")
# A model that is biased high but orders candidates perfectly.
for cs, actual in [(12, 0.0050), (18, 0.0056), (24, 0.0061), (30, 0.0068)]:
    pred = m.predict(state, [{"CTS_CLUSTER_SIZE": cs}])[0]
    sc.observe(pred, {"clock_skew_setup": actual})
print("      " + sc.summary().replace("\n", "\n      "))
check("scored the metric", "clock_skew_setup" in sc.pairs)
check("perfect ordering shows as rank_corr +1.00", "+1.00" in sc.summary())

sc2 = Scorecard("feasibility")
for p, built in [(0.9, True), (0.8, True), (0.1, False), (0.2, False)]:
    sc2.observe(Prediction({}, {"p_builds": p}, {"p_builds": "probability"}), {}, built=built)
check("probabilities scored by calibration, not residual", "p(builds)" in sc2.summary())
print("      " + sc2.summary().replace("\n", "\n      "))

print("\n" + ("FAILED: " + ", ".join(fails) if fails else "ALL CHECKS PASSED"))
sys.exit(1 if fails else 0)
