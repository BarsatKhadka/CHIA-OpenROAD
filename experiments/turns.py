"""Does the agent improve across iterations? Candidates are built 4 per turn."""
import json, os, sqlite3
DESIGNS = ["cb_aes", "cb_picorv32", "cb_sha256", "cb_ethmac"]
for d in DESIGNS:
    bp = os.path.expanduser(f"~/results/default_{d}.json")
    base = json.load(open(bp)).get("finish__timing__setup__ws") if os.path.exists(bp) else None
    print(f"\n=== {d}  default={base:+.4f} ===" if base else f"\n=== {d} ===")
    for arm in ("agent", "full"):
        p = os.path.expanduser(f"~/results/{arm}_{d}.db")
        if not os.path.exists(p): continue
        c = sqlite3.connect(p); c.row_factory = sqlite3.Row
        rows = [dict(r) for r in c.execute("select * from candidates order by id")]
        out = []
        for lo, hi in ((1,4),(5,8),(9,12)):
            grp = [r for r in rows if lo <= r["id"] <= hi]
            sl = [json.loads(r["metrics"] or "{}").get("worst_slack") for r in grp
                  if r["status"] == "built"]
            sl = [s for s in sl if s is not None]
            out.append(f"{max(sl):+.4f}" if sl else "  none ")
        # cumulative best after each turn
        cum, seen = [], []
        for lo, hi in ((1,4),(5,8),(9,12)):
            grp = [r for r in rows if lo <= r["id"] <= hi]
            seen += [json.loads(r["metrics"] or "{}").get("worst_slack") for r in grp
                     if r["status"] == "built"]
            s = [x for x in seen if x is not None]
            cum.append(f"{max(s):+.4f}" if s else "  none ")
        print(f"  {arm:6s} per-turn best: {'  '.join(out)}   |  cumulative: {'  '.join(cum)}")
