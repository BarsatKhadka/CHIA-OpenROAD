"""v1 vs v2 per-turn progression, for every design that has both."""
import json, os, sqlite3, statistics
def turns(path, base):
    p = os.path.expanduser(path)
    if not os.path.exists(p): return None
    c = sqlite3.connect(p); c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute("select * from candidates order by id")]
    per, cum, seen = [], [], []
    for lo, hi in ((1,4),(5,8),(9,12)):
        g = [json.loads(r["metrics"] or "{}").get("worst_slack") for r in rows
             if lo <= r["id"] <= hi and r["status"] == "built"]
        g = [x for x in g if x is not None]; seen += g
        per.append(f"{max(g):+.4f}" if g else "  none ")
        cum.append(f"{max(seen):+.4f}" if seen else "  none ")
    allb = [x for x in seen if x is not None]
    if not allb: return None
    return dict(per=per, cum=cum, best=max(allb), med=statistics.median(allb),
                beat=sum(1 for x in allb if x > base), n=len(allb))

for d in ("cb_aes", "cb_picorv32", "cb_sha256", "cb_ethmac"):
    bp = os.path.expanduser(f"~/results/default_{d}.json")
    if not os.path.exists(bp): continue
    base = json.load(open(bp))["finish__timing__setup__ws"]
    v1 = turns(f"~/results/full_{d}.db", base)
    v2 = turns(f"~/results_v2/full_{d}.db", base)
    if not v2: continue
    print(f"\n=== {d}   default {base:+.4f} ===")
    for tag, r in (("v1", v1), ("v2", v2)):
        if not r: print(f"  {tag}: n/a"); continue
        # Per-turn best, not the cumulative best: the cumulative series is a
        # running maximum and is non-decreasing by construction, so calling it
        # "monotonic" says nothing about whether the agent is learning.
        pv = [float(x) for x in r["per"] if x.strip() != "none"]
        r["mono"] = ("improving" if len(pv) == 3 and pv[0] < pv[1] < pv[2] else
                     "worsening" if len(pv) == 3 and pv[0] > pv[1] > pv[2] else
                     "mixed" if len(pv) == 3 else f"only {len(pv)} turns")
        mono = r["mono"]
        print(f"  {tag}  per-turn {'  '.join(r['per'])} | best {r['best']:+.4f} "
              f"median {r['med']:+.4f} beat {r['beat']}/{r['n']}"
              + f"  [{mono}]")
