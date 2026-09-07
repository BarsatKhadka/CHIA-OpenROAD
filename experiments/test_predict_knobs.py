"""predict_knobs must be honest: legality, ignored knobs, extrapolation."""
import types
from chia_openroad.orfs_tools import ORFSAgentTool
from chia_openroad.knob_policy import KnobPolicy

class FakePred:
    def __init__(self, v): self.values = v; self.cost_s = 0.001
class FakeSurrogate:
    name = "fake"; observes_stage = "place"
    domain = {"CTS_CLUSTER_SIZE": (12.0, 30.0), "CTS_CLUSTER_DIAMETER": (35.0, 70.0)}
    def predict(self, state, cands):
        # deliberately insensitive to everything but CLUSTER_SIZE
        return [FakePred({"clock_wirelength_um": 10000.0 + float(c.get("CTS_CLUSTER_SIZE", 20))})
                for c in cands]

class SecondSurrogate:
    """A different stage entirely — the case a real user hits: one model for
    the clock tree, another answering 'will this even build?'."""
    name = "feasible"; observes_stage = None; domain = {"CORE_UTILIZATION": (20.0, 60.0)}
    def predict(self, state, cands):
        return [FakePred({"p_builds": 0.9 if float(c.get("CORE_UTILIZATION", 35)) < 50 else 0.2})
                for c in cands]

me = types.SimpleNamespace(policy=KnobPolicy(),
                           surrogates=[FakeSurrogate(), SecondSurrogate()],
                           state=object())
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
ck("names knobs a model ignores", "ignores:" in out and "CORE_UTILIZATION" in out,
   [l.strip() for l in out.splitlines() if "ignores:" in l][:1])

# The realistic case for a socket: the policy allows a range wider than what
# this particular model was fitted on. 30 is legal, but outside fake's domain.
me.surrogates[0].domain = {"CTS_CLUSTER_SIZE": (12.0, 20.0)}
out = call({"CTS_CLUSTER_SIZE": 30})
ck("flags legal-but-unfitted as extrapolation", "OUTSIDE" in out,
   [l.strip() for l in out.splitlines() if "OUTSIDE" in l][:1])
me.surrogates[0].domain = {"CTS_CLUSTER_SIZE": (12.0, 30.0), "CTS_CLUSTER_DIAMETER": (35.0, 70.0)}

out = call({"NOT_A_KNOB": 1})
ck("rejects an illegal knob", "nothing predictable" in out, out.splitlines()[-1].strip()[:60])

out = call([{"CTS_CLUSTER_SIZE": 20}] * 21)
ck("caps batch size", "at most 20" in out)

out = call("nonsense")
ck("rejects a non-dict", "must be a dict" in out)
out = call({"CTS_CLUSTER_SIZE": 20, "CORE_UTILIZATION": 40})
ck("both models answer, each labelled with its stage",
   "fake [place]" in out and "feasible [any stage]" in out)
ck("each model reports its own blind spots separately",
   out.count("ignores:") == 2)
out = call({"CORE_UTILIZATION": 55})
ck("stage-specific models disagree usefully", "p_builds=0.2" in out,
   [l.strip() for l in out.splitlines() if "p_builds" in l][:1])

# The lifecycle point: a place-stage model reads a FINISHED placement, so a
# knob that changes the placement invalidates what it read.
out = call({"CORE_UTILIZATION": 25, "CTS_CLUSTER_SIZE": 20})
ck("warns when a knob changes the stage the model reads",
   "would change that place" in out,
   [l.strip()[:78] for l in out.splitlines() if "NOTE:" in l][:1])
ck("no such warning for a model that reads no stage",
   out.count("NOTE:") == 1)
out = call({"CTS_CLUSTER_SIZE": 20})
ck("a post-place-only knob gets no warning", "NOTE:" not in out)

desc = ORFSAgentTool.describe_surrogates(me)
ck("describe names the stage each model reads", "reads a finished place" in desc
   and "needs no design state" in desc)
ck("describe says which knobs it is fully valid for",
   "fully valid only for knobs that act after place" in desc)

print("\n" + ("FAILED: " + ", ".join(fails) if fails else "PREDICT_KNOBS PASSED"))
