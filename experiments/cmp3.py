"""v1 (explore-only) vs v2 (fixed refine) vs v3 (adaptive refine)."""
import json, os, sqlite3, statistics
def read(path, base):
    p = os.path.expanduser(path)
    if not os.path.exists(p): return None
    c = sqlite3.connect(p); c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute("select * from candidates order by id")]
    per, seen = [], []
    for lo, hi in ((1,4),(5,8),(9,12)):
        g = [json.loads(r["metrics"] or "{}").get("worst_slack") for r in rows
             if lo <= r["id"] <= hi and r["status"] == "built"]
        g = [x for x in g if x is not None]; seen += g
        per.append(f"{max(g):+.4f}" if g else "  none ")
    if not seen: return None
    pv = [float(x) for x in per if x.strip() != "none"]
    prog = ("improving" if len(pv)==3 and pv[0]<pv[1]<pv[2] else
            "worsening" if len(pv)==3 and pv[0]>pv[1]>pv[2] else
            "mixed" if len(pv)==3 else f"{len(pv)} turns")
    return dict(per=per, best=max(seen), med=statistics.median(seen),
                beat=sum(1 for x in seen if x > base), n=len(seen), prog=prog)

for d in ("cb_aes","cb_picorv32","cb_sha256","cb_ethmac"):
    bp = os.path.expanduser(f"~/results/default_{d}.json")
    if not os.path.exists(bp): continue
    base = json.load(open(bp))["finish__timing__setup__ws"]
    rows = [("v1 explore", read(f"~/results/full_{d}.db", base)),
            ("v2 refine ", read(f"~/results_v2/full_{d}.db", base)),
            ("v3 adaptive", read(f"~/results_v3/full_{d}.db", base))]
    if not any(r for _, r in rows[2:]): continue
    print(f"\n=== {d}   default {base:+.4f} ===")
    for tag, r in rows:
        if not r: print(f"  {tag}: n/a"); continue
        print(f"  {tag}  {'  '.join(r['per'])} | best {r['best']:+.4f} "
              f"median {r['med']:+.4f} beat {r['beat']}/{r['n']}  [{r['prog']}]")
