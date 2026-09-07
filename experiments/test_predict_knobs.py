"""predict_knobs must be honest: legality, ignored knobs, extrapolation."""
import types
from chia_openroad.orfs_tools import ORFSAgentTool
from chia_openroad.knob_policy import KnobPolicy

class FakePred:
    def __init__(self, v): self.values = v; self.cost_s = 0.001
class FakeSurrogate:
    name = "fake"
    domain = {"CTS_CLUSTER_SIZE": (12.0, 30.0), "CTS_CLUSTER_DIAMETER": (35.0, 70.0)}
    def predict(self, state, cands):
        # deliberately insensitive to everything but CLUSTER_SIZE
        return [FakePred({"clock_wirelength_um": 10000.0 + float(c.get("CTS_CLUSTER_SIZE", 20))})
                for c in cands]

me = types.SimpleNamespace(policy=KnobPolicy(), surrogate=FakeSurrogate(), state=object())
call = lambda k: ORFSAgentTool.predict_knobs(me, k)
fails = []
def ck(l, c, d=""):
    print(f"  [{'ok  ' if c else 'FAIL'}] {l}" + (f"  — {d}" if d else ""))
    if not c: fails.append(l)

out = call({"CTS_CLUSTER_SIZE": 20})
ck("predicts a single dict", "10020" in out, out.splitlines()[-2].strip()[:60])

out = call([{"CTS_CLUSTER_SIZE": 12}, {"CTS_CLUSTER_SIZE": 30}])
ck("predicts a batch", "10012" in out and "10030" in out)

out = call({"CTS_CLUSTER_SIZE": 20, "CORE_UTILIZATION": 40})
ck("names knobs the model ignores", "ignored by this model" in out and "CORE_UTILIZATION" in out)

# The realistic case for a socket: the policy allows a range wider than what
# this particular model was fitted on. 30 is legal, but outside fake's domain.
me.surrogate.domain = {"CTS_CLUSTER_SIZE": (12.0, 20.0)}
out = call({"CTS_CLUSTER_SIZE": 30})
ck("flags legal-but-unfitted as extrapolation", "OUTSIDE" in out,
   [l.strip() for l in out.splitlines() if "OUTSIDE" in l][:1])
me.surrogate.domain = {"CTS_CLUSTER_SIZE": (12.0, 30.0), "CTS_CLUSTER_DIAMETER": (35.0, 70.0)}

out = call({"NOT_A_KNOB": 1})
ck("rejects an illegal knob", "nothing predictable" in out, out.splitlines()[-1].strip()[:60])

out = call([{"CTS_CLUSTER_SIZE": 20}] * 21)
ck("caps batch size", "at most 20" in out)

out = call("nonsense")
ck("rejects a non-dict", "must be a dict" in out)
print("\n" + ("FAILED: " + ", ".join(fails) if fails else "PREDICT_KNOBS PASSED"))
