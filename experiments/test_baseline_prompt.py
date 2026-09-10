"""The agent must be shown the default, and told to refine after turn 1."""
import types
from chia_openroad.iterate import render_state

fails = []
def ck(l, c, d=""):
    print(f"  [{'ok  ' if c else 'FAIL'}] {l}" + (f"  — {d}" if d else ""))
    if not c: fails.append(l)

BASE = {"worst_slack": -0.2352, "clock_skew_setup": 0.5596,
        "power_total": 0.03541, "instance_area": 103400.0}
cand = types.SimpleNamespace(
    id=1, parent_id=None, knobs={"CORE_UTILIZATION": 45},
    metrics={"worst_slack": -0.3100, "clock_skew_setup": 0.57,
             "power_total": 0.036, "instance_area": 104000.0})
store = types.SimpleNamespace(list=lambda **k: [cand])
fail = types.SimpleNamespace(hints=lambda limit: [])

out = render_state(store, None, fail, baseline=BASE)
ck("the default is stated", "must beat" in out and "-0.2352" in out)
ck("a losing candidate is marked as losing", "(-0.0748 vs default)" in out,
   [l for l in out.splitlines() if "vs default" in l][:1])

no_base = render_state(store, None, fail)
ck("still works with no baseline", "vs default" not in no_base and "#1" in no_base)

# the prompt half: after turn 1 the agent must be told to refine, not just explore
import inspect
from chia_openroad import iterate
src = inspect.getsource(iterate.run_iterations)
ck("turn>1 asks for refinement", "must REFINE the best result so far" in src)
ck("turn 1 asks for spread", "spread these configurations widely" in src)
ck("the exploration-only instruction is gone",
   "genuinely different from each other" not in src)
print("\n" + ("FAILED: " + ", ".join(fails) if fails else "BASELINE PROMPT PASSED"))
