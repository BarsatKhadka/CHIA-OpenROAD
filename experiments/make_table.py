"""Arms x designs PPA table.

Reports best, median and how many candidates beat the default. Ranking arms by
their single best result rewards variance rather than quality: on cb_picorv32
the screen arm's best beat every other arm while only 2 of its 12 candidates
were better than the default and its median was worse than doing nothing.
"""
import json, os, sqlite3, statistics

DESIGNS = ["cb_aes", "cb_picorv32", "cb_sha256", "cb_ethmac"]
ARMS    = [("default", None), ("screen", "screen"), ("agent", "agent"), ("full", "full")]
M = {"slack": "worst_slack", "skew": "clock_skew_setup",
     "power": "power_total", "area": "instance_area"}
R = {"slack": "finish__timing__setup__ws", "skew": "finish__clock__skew__setup",
     "power": "finish__power__total", "area": "finish__design__instance__area"}
# knobs the CTS-only grid can reach; everything else is a pre-CTS decision
CTS = {"CTS_CLUSTER_SIZE", "CTS_CLUSTER_DIAMETER", "CTS_BUF_DISTANCE"}

def default_row(d):
    p = os.path.expanduser(f"~/results/default_{d}.json")
    if not os.path.exists(p):
        return None
    m = json.load(open(p))
    return {k: m.get(v) for k, v in R.items()}

def arm_row(arm, d, base):
    p = os.path.expanduser(f"~/results/{arm}_{d}.db")
    if not os.path.exists(p):
        return None
    c = sqlite3.connect(p); c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute("select * from candidates")]
    built = [(json.loads(r["metrics"] or "{}"), json.loads(r["knobs"] or "{}"))
             for r in rows if r["status"] == "built"]
    built = [(m, k) for m, k in built if m.get("worst_slack") is not None]
    nf = sum(1 for r in rows if r["status"] == "failed")
    if not built:
        return {"n_built": 0, "n_failed": nf}
    best_m, _ = max(built, key=lambda t: t[0]["worst_slack"])
    slacks = [m["worst_slack"] for m, _ in built]
    bd = sum(1 for s in slacks if base and base.get("slack") is not None and s > base["slack"])
    out = {k: best_m.get(v) for k, v in M.items()}
    out.update(median=statistics.median(slacks), n_built=len(built), n_failed=nf,
               beat=bd, pre_cts=sum(1 for _, k in built if set(k) - CTS))
    return out

def f(v, w=8, p=4):
    return f"{v:{w}.{p}g}" if isinstance(v, (int, float)) else f"{'-':>{w}}"

hdr = (f"{'design':12s} {'arm':8s} {'slack':>8s} {'median':>8s} {'skew':>7s} "
       f"{'power':>8s} {'area':>9s} {'beat':>6s} {'b/f':>6s} {'preCTS':>7s}")
print(hdr); print("-" * len(hdr))
for d in DESIGNS:
    base = default_row(d)
    for label, arm in ARMS:
        row = base if arm is None else arm_row(arm, d, base)
        if row is None:
            print(f"{d:12s} {label:8s} {'(pending)':>8s}"); continue
        if arm is None:
            print(f"{d:12s} {label:8s} {f(row.get('slack'))} {'-':>8s} {f(row.get('skew'),7)} "
                  f"{f(row.get('power'))} {f(row.get('area'),9)} {'-':>6s} {'-':>6s} {'-':>7s}")
        else:
            n = row.get("n_built", 0)
            print(f"{d:12s} {label:8s} {f(row.get('slack'))} {f(row.get('median'))} "
                  f"{f(row.get('skew'),7)} {f(row.get('power'))} {f(row.get('area'),9)} "
                  f"{str(row.get('beat','-'))+'/'+str(n):>6s} "
                  f"{str(n)+'/'+str(row.get('n_failed',0)):>6s} "
                  f"{str(row.get('pre_cts','-'))+'/'+str(n):>7s}")
    print()
