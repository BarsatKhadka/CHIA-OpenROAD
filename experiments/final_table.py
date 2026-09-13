"""The paper's results table, from the final matrix.

One agent version, one set of parameters, multi-fidelity throughout, so every
cell is comparable. Each run measures its own default, so the baseline is the
one that run optimised against rather than a number from a different build.
"""
import json, os, sqlite3, statistics

ARMS = [("screen", "surrogate ranking, no LLM"),
        ("agent", "LLM, no surrogate"),
        ("full", "LLM + surrogate")]
DESIGNS = ["cb_aes", "cb_picorv32", "cb_sha256", "cb_ethmac"]
CLOCK = {"cb_aes": 6.5, "cb_picorv32": 4.5, "cb_sha256": 10.4, "cb_ethmac": 6.5}
CTS = {"CTS_CLUSTER_SIZE", "CTS_CLUSTER_DIAMETER", "CTS_BUF_DISTANCE"}

def load(arm, d):
    p = os.path.expanduser(f"~/results_final/{arm}/{d}.db")
    if not os.path.exists(p):
        return None
    c = sqlite3.connect(p); c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute("select * from candidates order by id")]
    full, quick = [], []
    for r in rows:
        if r["status"] != "built":
            continue
        m = json.loads(r["metrics"] or "{}")
        v = m.get("worst_slack")
        if not isinstance(v, (int, float)):
            continue
        (full if r["stage_reached"] == "finish" else quick).append((v, m, r))
    return dict(full=full, quick=quick, rows=rows)

def baseline(arm, d):
    """The default this run measured for itself."""
    p = os.path.expanduser(f"~/logs_final/{arm}/{d}.log")
    if not os.path.exists(p):
        return None
    for line in open(p, errors="replace"):
        if "slack=" in line and "skew=" in line:
            try:
                return float(line.split("slack=")[1].split()[0])
            except (IndexError, ValueError):
                return None
    return None

hdr = (f"{'design':12s} {'clk':>5s} {'arm':8s} {'default':>9s} {'best':>9s} "
       f"{'delta':>8s} {'median':>9s} {'beat':>7s} {'full':>5s} {'quick':>6s} {'preCTS':>7s}")
print(hdr); print("-" * len(hdr))
for d in DESIGNS:
    for arm, _ in ARMS:
        r = load(arm, d)
        if not r or not r["full"]:
            print(f"{d:12s} {CLOCK[d]:5.1f} {arm:8s} {'(pending)':>9s}")
            continue
        base = baseline(arm, d)
        vals = [v for v, _, _ in r["full"]]
        best = max(vals)
        pre = sum(1 for _, _, row in r["full"]
                  if set(json.loads(row["knobs"] or "{}")) - CTS)
        delta = f"{best - base:+.4f}" if base is not None else "   --   "
        beat = f"{sum(1 for v in vals if base is not None and v > base)}/{len(vals)}"
        print(f"{d:12s} {CLOCK[d]:5.1f} {arm:8s} "
              f"{(f'{base:+.4f}' if base is not None else '--'):>9s} {best:+9.4f} "
              f"{delta:>8s} {statistics.median(vals):+9.4f} {beat:>7s} "
              f"{len(r['full']):5d} {len(r['quick']):6d} {f'{pre}/{len(vals)}':>7s}")
    print()

print("full  = candidates routed to GDS;  quick = scored at CTS only (~7% of a flow)")
print("preCTS = full builds that varied a knob read before clock-tree synthesis")
