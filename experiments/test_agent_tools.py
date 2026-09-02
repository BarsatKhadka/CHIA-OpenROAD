"""Store + tool facade, with ORFS faked. The point is the trust boundary."""
import inspect, os, sys, tempfile
from dataclasses import dataclass, field
from chia_openroad.candidate_store import CandidateStore
from chia_openroad.failure_log import FailureLog
from chia_openroad.knob_policy import KnobPolicy
from chia_openroad import orfs_tools

fails = []
def check(l, c, d=""):
    print(f"  [{'ok  ' if c else 'FAIL'}] {l}" + (f"  — {d}" if d else ""))
    if not c: fails.append(l)

tmp = tempfile.mkdtemp()

print("=== candidate store ===")
st = CandidateStore(os.path.join(tmp, "c.db"))
@dataclass
class R:
    success: bool; stage: str; elapsed_s: float = 10.0
    summary: dict = field(default_factory=dict); failure=None
    design: str = "gcd"; platform: str = "sky130hd"; returncode: int = 0
    knobs: dict = field(default_factory=dict); checkpoint=None
a = st.propose({"CORE_UTILIZATION": 40}, arm="agent")
st.record(a, [R(True, "cts", 20, {"worst_slack": -1.4}), R(True, "finish", 150, {"worst_slack": -1.2})])
b = st.propose({"CORE_UTILIZATION": 65}, arm="agent")
st.record(b, [R(False, "cts", 47, {})])
c = st.propose({"CORE_UTILIZATION": 999}, arm="agent"); st.reject(c, "out of range")
check("built recorded", st.get(a).status == "built")
check("tool_runs counts ORFS invocations, not turns", st.get(a).tool_runs == 2, str(st.get(a).tool_runs))
check("gated failure cost only one run", st.get(b).tool_runs == 1, str(st.get(b).tool_runs))
check("rejection recorded, not silently dropped", st.get(c).status == "rejected")
s = st.stats()
check("stats separate proposals from runs",
      s["proposed"] == 3 and s["built"] == 1 and s["rejected"] == 1 and s["tool_runs"] == 3, str(s))
check("seen() finds an exact repeat", st.seen({"CORE_UTILIZATION": 40}) is not None)
check("seen() ignores a different config", st.seen({"CORE_UTILIZATION": 41}) is None)

print("\n=== the trust boundary ===")
names = [n for n, _ in inspect.getmembers(orfs_tools.ORFSAgentTool, inspect.isfunction)
         if not n.startswith("_") and n not in ("setup",)]
print(f"       exposed tools: {sorted(names)}")
forbidden = ("run_stage", "drc", "lvs", "verify", "force_rebuild", "make", "collect")
check("no tool names a forbidden capability",
      not any(f in n for n in names for f in forbidden),
      f"{len(names)} tools")
src = open("chia_openroad/orfs_tools.py").read()
for bad in ("force_rebuild", "allow_unknown_knobs", "extra_make_args"):
    check(f"agent cannot pass {bad}", bad not in src.split('"""')[-1])
check("run_flow target is hard-coded, not agent-chosen", '"finish"' in src)
check("gate is set at construction, not per call",
      "gate=self.gate" in src or "self.gate," in src)

print("\n=== policy gate is enforced before anything runs ===")
pol = KnobPolicy()
fl = FailureLog(os.path.join(tmp, "f.jsonl"))
# Constructed without ChiaTool.__init__ so the test needs no Ray/MCP server;
# setup() is called directly, which is exactly what the base class does.
tool = orfs_tools.ORFSAgentTool.__new__(orfs_tools.ORFSAgentTool)
tool.name = "orfs"
class _FakeMCP:
    def __init__(self): self.added = []
    def add_tool(self, fn, name=None): self.added.append(name)
tool.mcp = _FakeMCP()
tool.setup(policy=pol, store=CandidateStore(os.path.join(tmp, "c2.db")), failures=fl,
           design_config="cfg.mk", work_root=tmp, arm="agent", gate="cts")
check("setup registers every tool with the MCP server", len(tool.mcp.added) == 7,
      f"{len(tool.mcp.added)} registered")
out = tool.propose_candidate({"CORE_UTILIZATION": 999})
check("illegal value rejected without running", "REJECTED" in out, out[:70])
out = tool.propose_candidate({"SYNTH_HIERARCHICAL": 1})
check("knob outside the legal set rejected", "REJECTED" in out, out[:70])
check("rejections are in the ledger", tool.store.stats()["rejected"] == 2, str(tool.store.stats()))
check("legal knob listing is offered", "CORE_UTILIZATION" in tool.list_legal_knobs())
check("listing warns ranges are unmeasured", "NOMINAL" in tool.list_legal_knobs())

print("\n" + ("FAILED: " + ", ".join(fails) if fails else "ALL CHECKS PASSED"))
sys.exit(1 if fails else 0)
