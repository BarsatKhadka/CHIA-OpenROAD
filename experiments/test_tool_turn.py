"""The agent may read freely inside a turn, but never build inside one."""
import json, types as pytypes
from chia_openroad.iterate import READ_ONLY_TOOLS, ask_with_tools
from chia_openroad.orfs_tools import ORFSAgentTool

fails = []
def ck(l, c, d=""):
    print(f"  [{'ok  ' if c else 'FAIL'}] {l}" + (f"  — {d}" if d else ""))
    if not c: fails.append(l)

ck("every advertised tool exists on the tool class",
   all(hasattr(ORFSAgentTool, n) for n in READ_ONLY_TOOLS),
   f"{len(READ_ONLY_TOOLS)} tools")
ck("the build tools are withheld",
   not ({"propose_candidate", "candidate_status"} & set(READ_ONLY_TOOLS)))

# A fake tool + fake client: no Gemini, no ORFS. Proves the turn mechanics —
# calls get dispatched, results fed back, and the loop stops on a text reply.
class FakeTool:
    def list_legal_knobs(self): return "CORE_UTILIZATION 20..60"
    def past_failures(self, limit: int = 10): return "none yet"
    def predict_knobs(self, knobs): return "predicted 10020"
seen = []
class FakeClient:
    class models:
        @staticmethod
        def generate_content(model=None, contents=None, config=None):
            from google.genai import types
            seen.append(len(contents))
            if len(contents) == 1:          # first turn: ask for two tools at once
                return pytypes.SimpleNamespace(candidates=[pytypes.SimpleNamespace(
                    content=types.Content(role="model", parts=[
                        types.Part.from_function_call(name="list_legal_knobs", args={}),
                        types.Part.from_function_call(name="past_failures", args={})]))])
            return pytypes.SimpleNamespace(candidates=[pytypes.SimpleNamespace(
                content=types.Content(role="model",
                                      parts=[types.Part(text='{"CORE_UTILIZATION": 30}')]))])

text, calls = ask_with_tools(FakeClient(), "m", "sys", "prompt", FakeTool())
ck("tool calls are dispatched", len(calls) == 2, ", ".join(calls))
ck("two calls answered in ONE message", seen == [1, 3], f"history sizes {seen}")
ck("final text is returned", "CORE_UTILIZATION" in text, text.strip())

class NoTools: pass
ck("falls back cleanly when no tools are exposed",
   ask_with_tools(FakeClient(), "m", "s", "p", NoTools())[1] == [])
print("\n" + ("FAILED: " + ", ".join(fails) if fails else "TOOL TURN PASSED"))
