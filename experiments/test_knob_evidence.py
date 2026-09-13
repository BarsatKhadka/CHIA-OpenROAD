"""The agent must be shown the aggregate per-knob signal, not just candidates."""
import types
from chia_openroad.iterate import knob_evidence, render_state

fails = []
def ck(l, c, d=""):
    print(f"  [{'ok  ' if c else 'FAIL'}] {l}" + (f"  — {d}" if d else ""))
    if not c: fails.append(l)

def cand(i, knobs, slack):
    return types.SimpleNamespace(id=i, parent_id=None, knobs=knobs,
                                 metrics={"worst_slack": slack})

# the cb_sha256 shape: high utilisation good, setup margin bad -- the opposite
# of what helps cb_aes, which is why the agent kept getting it wrong
built = [cand(1, {"CORE_UTILIZATION": 30, "SETUP_SLACK_MARGIN": 0.1}, -0.86),
         cand(2, {"CORE_UTILIZATION": 30, "SETUP_SLACK_MARGIN": 0.1}, -0.87),
         cand(3, {"CORE_UTILIZATION": 45, "SETUP_SLACK_MARGIN": 0.0}, -0.67),
         cand(4, {"CORE_UTILIZATION": 45, "SETUP_SLACK_MARGIN": 0.0}, -0.68)]

ev = knob_evidence(built)
names = [n for _, n, _, _, _ in ev]
ck("both varying knobs are reported", set(names) == {"CORE_UTILIZATION", "SETUP_SLACK_MARGIN"})
byname = {n: (b, w) for _, n, b, w, _ in ev}
ck("the better value is named as better", byname["CORE_UTILIZATION"][0][0] == "45",
   f"best={byname['CORE_UTILIZATION'][0][0]}")
ck("the worse value is named as worse", byname["SETUP_SLACK_MARGIN"][1][0] == "0.1",
   f"worst={byname['SETUP_SLACK_MARGIN'][1][0]}")

# a knob seen at only one value carries no evidence and must not be reported
one = [cand(1, {"CTS_CLUSTER_SIZE": 20}, -0.5), cand(2, {"CTS_CLUSTER_SIZE": 20}, -0.6)]
ck("a knob with one observed value is omitted", knob_evidence(one) == [])

# a single observation per value is not evidence either
thin = [cand(1, {"CORE_UTILIZATION": 30}, -0.9), cand(2, {"CORE_UTILIZATION": 45}, -0.6)]
ck("single observations are not reported", knob_evidence(thin) == [])

store = types.SimpleNamespace(list=lambda **k: built)
fail = types.SimpleNamespace(hints=lambda limit: [])
out = render_state(store, None, fail, baseline={"worst_slack": -0.5778})
ck("the table reaches the prompt", "What the runs so far say about each knob" in out)
ck("it warns the evidence is design-specific", "may disagree with what usually works" in out)
ck("nothing breaks with no builds",
   "What the runs so far say" not in render_state(
       types.SimpleNamespace(list=lambda **k: []), None, fail))
print("\n" + ("FAILED: " + ", ".join(fails) if fails else "KNOB EVIDENCE PASSED"))
