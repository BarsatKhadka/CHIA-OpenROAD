"""Why do the arms differ? Compare what each actually searched."""
import json, os, sqlite3
from collections import Counter

CTS = {"CTS_CLUSTER_SIZE", "CTS_CLUSTER_DIAMETER", "CTS_BUF_DISTANCE"}
DESIGNS = ["cb_aes", "cb_picorv32", "cb_sha256", "cb_ethmac"]
ARMS = ["screen", "agent", "full"]

for d in DESIGNS:
    base = None
    bp = os.path.expanduser(f"~/results/default_{d}.json")
    if os.path.exists(bp):
        base = json.load(open(bp)).get("finish__timing__setup__ws")
    print(f"\n=== {d}  (default slack {base}) ===")
    for arm in ARMS:
        p = os.path.expanduser(f"~/results/{arm}_{d}.db")
        if not os.path.exists(p):
            print(f"  {arm:7s} (pending)"); continue
        c = sqlite3.connect(p); c.row_factory = sqlite3.Row
        rows = [dict(r) for r in c.execute("select * from candidates")]
        built = [(json.loads(r["metrics"] or "{}"), json.loads(r["knobs"] or "{}"))
                 for r in rows if r["status"] == "built"]
        built = [(m, k) for m, k in built if m.get("worst_slack") is not None]
        if not built:
            print(f"  {arm:7s} nothing built"); continue
        slacks = sorted(m["worst_slack"] for m, _ in built)
        # how many candidates touched a knob read before CTS?
        pre_cts = sum(1 for _, k in built if set(k) - CTS)
        knob_use = Counter(kk for _, k in built for kk in k)
        best_m, best_k = max(built, key=lambda t: t[0]["worst_slack"])
        n_better = sum(1 for s in slacks if base is not None and s > base)
        print(f"  {arm:7s} best={best_m['worst_slack']:+.4f}  "
              f"median={slacks[len(slacks)//2]:+.4f}  worst={slacks[0]:+.4f}  "
              f"beat_default={n_better}/{len(slacks)}  pre-CTS knobs in {pre_cts}/{len(built)}")
        print(f"          best knobs: {', '.join(f'{a}={b}' for a,b in sorted(best_k.items()))}")
        print(f"          knob usage: {', '.join(f'{a}x{b}' for a,b in knob_use.most_common(6))}")
