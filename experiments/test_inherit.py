"""A candidate derived from a parent must inherit that parent's knobs.

The agent writes {"from": 6, "CTS_BUF_DISTANCE": 70} meaning "#6 with this one
knob changed". Before this, only the listed knob was applied and every other
knob #6 set reverted to the design default, so a refinement was actually a
fresh near-default build. Measured on cb_picorv32: #6 carried one knob instead
of its parent's four, and #9 and #10 each carried exactly one.
"""
import types, json
from chia_openroad.orfs_tools import ORFSAgentTool
from chia_openroad.knob_policy import KnobPolicy

fails = []
def ck(l, c, d=""):
    print(f"  [{'ok  ' if c else 'FAIL'}] {l}" + (f"  — {d}" if d else ""))
    if not c: fails.append(l)

PARENT = {"CORE_UTILIZATION": 40, "CTS_CLUSTER_DIAMETER": 70.0,
          "GPL_TIMING_DRIVEN": 1, "SETUP_SLACK_MARGIN": 0.1}

class Store:
    def __init__(self):
        self.rows = {6: types.SimpleNamespace(knobs=dict(PARENT))}
        self.last = None
    def get(self, i): return self.rows.get(i)
    def propose(self, knobs, arm=None, parent_id=None):
        self.last = dict(knobs); return 9
    def reject(self, cid, why): pass

def call(knobs, parent):
    me = types.SimpleNamespace(store=Store(), policy=KnobPolicy(),
                               failures=types.SimpleNamespace(known_bad=lambda k: None),
                               arm="t", _pending={}, run_token="x")
    try:
        ORFSAgentTool.propose_candidate(me, knobs, parent_id=parent)
    except Exception:
        pass                      # dispatch needs Ray; we only inspect the merge
    return me.store.last

got = call({"CTS_BUF_DISTANCE": 70.0}, 6)
ck("the parent's knobs are inherited", set(PARENT) <= set(got), f"{len(got)} knobs")
ck("the delta is applied", got.get("CTS_BUF_DISTANCE") == 70.0)
ck("untouched parent knobs keep their values",
   got.get("CORE_UTILIZATION") == 40 and got.get("SETUP_SLACK_MARGIN") == 0.1)

over = call({"CORE_UTILIZATION": 25}, 6)
ck("the delta overrides the parent", over.get("CORE_UTILIZATION") == 25)
ck("overriding does not drop siblings", over.get("CTS_CLUSTER_DIAMETER") == 70.0)

fresh = call({"CORE_UTILIZATION": 30}, 0)
ck("with no parent it stays a full specification", fresh == {"CORE_UTILIZATION": 30},
   json.dumps(fresh))
print("\n" + ("FAILED: " + ", ".join(fails) if fails else "INHERITANCE PASSED"))
