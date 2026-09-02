"""Can the node produce what SwiftCTS declares in `requires`?"""
import os, sys
from chia_openroad.openroad import OpenROADNode
emit = OpenROADNode.emit_artifacts._chia_original
W, D = "/work", "./designs/sky130hd/gcd/config.mk"

got = emit(W, D, ["def", "timing_rpt", "clock_period", "odb"], stage="place")
print("produced:")
for k, v in sorted(got.items()):
    print(f"  {k:14s} {os.path.getsize(v):>9,d} B  {v.split('/')[-1]}")

fails = []
for need in ("def", "timing_rpt"):
    ok = need in got
    print(f"  [{'ok  ' if ok else 'FAIL'}] {need} produced")
    if not ok: fails.append(need)

if "timing_rpt" in got:
    with open(got["timing_rpt"]) as f:
        head = [next(f, "").strip() for _ in range(4)]
    print(f"  slack csv head: {head}")
    ok = head[0] == "slack" and len(head) > 1 and head[1]
    print(f"  [{'ok  ' if ok else 'FAIL'}] csv has a slack column with rows")
    if not ok: fails.append("csv shape")
    # the format SwiftCTS actually parses
    try:
        import pandas as pd
        sl = pd.read_csv(got["timing_rpt"])["slack"].values
        print(f"  [ok  ] pandas parses it — {len(sl)} paths, min={sl.min():.4f} max={sl.max():.4f}")
    except Exception as e:
        print(f"  [FAIL] pandas cannot parse it: {e}")
        fails.append("pandas parse")

print("\n" + ("FAILED: " + ", ".join(fails) if fails else "ALL CHECKS PASSED"))
sys.exit(1 if fails else 0)
