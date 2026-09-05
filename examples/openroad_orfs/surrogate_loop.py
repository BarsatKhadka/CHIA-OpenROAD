"""Surrogate-guided agentic RTL-to-GDS: the whole loop.

Build one placement. Calibrate a cheap model against it. Let the model rank
thousands of clock-tree configurations in seconds. Let an agent read that
ranking, choose what to build, and reason over what comes back. ORFS decides
everything.

    python surrogate_loop.py --design aes --turns 8

Three properties this is built to hold, all enforced in code rather than asked
for in a prompt:

* **Candidates branch from the shared placement.** A clock-tree screen only
  makes sense against a fixed placement, so candidates seed from it rather than
  rebuild it. On aes that saves about 7%: placement is only 1.4% of the flow and
  detailed routing is 86%. The saving is real but small here — the lever that
  matters on this design is not routing candidates that are not worth routing.
* **The agent never certifies anything.** It proposes knobs and reads summaries.
  Execution, report parsing, DRC/LVS and the feasibility verdict stay
  programmatic and out of reach.
* **The screen is advice, not evidence.** Its ranking is offered as prediction;
  every candidate is still built, and the scorecard reports afterwards how well
  it ordered them.
"""
import argparse, json, logging, os, sys, time

import ray
from chia.base.ChiaFunction import get
from chia.models.vertex import VertexGeminiLLM

import chia_openroad
from chia_openroad.candidate_store import CandidateStore
from chia_openroad.failure_log import FailureLog
from chia_openroad.knob_policy import KnobPolicy
from chia_openroad.openroad import OpenROADNode
from chia_openroad.orfs_tools import ORFSAgentTool
from chia_openroad.surrogate import DesignState, Scorecard, conformance_check
from chia_openroad.surrogates.swiftcts import SwiftCTSEvaluator

HERE = os.path.dirname(os.path.abspath(__file__))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("loop")


def load_policy(path):
    policy = KnobPolicy()
    if not os.path.exists(path):
        log.warning("no calibration at %s; every range is nominal", path)
        return policy
    raw = json.load(open(path))
    measured = {k: (v["low"], v["high"]) for k, v in raw.items()
                if v.get("low") is not None}
    log.info("calibrated ranges for %d knob(s)", len(measured))
    return policy.with_calibration(measured)


def build_screen(surrogate, state, policy, per_axis=6):
    """Rank every in-domain CTS configuration, once.

    The placement is fixed for the whole loop, so the surrogate's ordering is
    fixed too. Computing it here rather than inside the agent's tool keeps the
    fitted model on the driver — where its Python dependencies live — instead of
    trying to pickle it into a Ray actor that cannot import them.
    """
    import itertools
    from chia_openroad.surrogate import ORFS_METRICS

    axes = []
    for name, spec in sorted(policy.knobs.items()):
        if spec.stage != "cts" or spec.low is None:
            continue
        low, high = spec.low, spec.high
        fitted = getattr(surrogate, "domain", {}).get(name)
        if fitted:                       # never screen outside the training domain
            low, high = max(low, fitted[0]), min(high, fitted[1])
        if high <= low:
            continue
        step = (high - low) / (per_axis - 1)
        vals = [round(low + i * step, 3) for i in range(per_axis)]
        if spec.kind == "int":
            vals = sorted({int(round(v)) for v in vals})
        axes.append([(name, v) for v in vals])
    grid = [dict(c) for c in itertools.product(*axes)] if axes else []

    preds = surrogate.predict(state, grid)
    # Rank on the one objective this surrogate actually predicts usefully.
    # Measured on aes: wirelength tracks reality (2.6% MAE, rank_corr +0.40);
    # clock power does not (rank_corr 0.00, no SAIF) and skew is not produced.
    objective = "clock_wirelength_um"
    usable = [p for p in preds if objective in p.values]
    if not usable:
        objective = next((k for k, v in surrogate.predicts.items()
                          if v == "orfs_metric"), None)
        usable = [p for p in preds if objective and objective in p.values]
    better = ORFS_METRICS.get(objective, "lower")
    usable.sort(key=lambda p: p.values[objective], reverse=(better == "higher"))
    ranked = _break_ties([(p.knobs, p.values[objective]) for p in usable])
    return {"name": surrogate.name, "metric": objective, "better": better,
            "cost_s": sum(p.cost_s for p in preds), "ranked": ranked}


def _break_ties(ranked, tol=1e-9):
    """Within equally-predicted configurations, order by diversity.

    A surrogate that ignores a knob predicts the same value for every setting of
    it, and the top of the ranking becomes a block of ties differing only in
    that knob. Measured on aes: SwiftCTS ignores CTS_BUF_DISTANCE, and the top
    five predictions were identical to six figures — so the first three picks
    were three configurations it could not tell apart. Two of them were built,
    at ~3700 s each, and their actual wirelengths differed by 0.2%.

    Reality was not wrong to be flat there; the waste is in spending the budget
    confirming a tie. So inside each tie group, emit greedily by max-min
    distance from what has already been emitted, which surfaces genuinely
    different configurations first while preserving the ranking between groups.
    """
    if not ranked:
        return ranked
    keys = sorted({k for knobs, _ in ranked for k in knobs})
    lo = {k: min(float(kn[k]) for kn, _ in ranked if k in kn) for k in keys}
    hi = {k: max(float(kn[k]) for kn, _ in ranked if k in kn) for k in keys}

    def dist(a, b):
        total = 0.0
        for k in keys:
            if k not in a or k not in b:
                continue
            span = (hi[k] - lo[k]) or 1.0
            total += ((float(a[k]) - float(b[k])) / span) ** 2
        return total ** 0.5

    out, group = [], []

    def flush():
        # Greedy max-min within the group, seeded by whatever is already out.
        pending = list(group)
        while pending:
            if out:
                nxt = max(pending, key=lambda g: min(dist(g[0], o[0]) for o in out))
            else:
                nxt = pending[0]
            pending.remove(nxt)
            out.append(nxt)
        group.clear()

    current = None
    for knobs, value in ranked:
        if current is None or abs(value - current) > tol * max(abs(value), 1.0):
            flush()
            current = value
        group.append((knobs, value))
    flush()
    return out


def fetch(node, base, remote, dest_dir):
    """Artifacts are produced in the worker container; the surrogate runs
    driver-side. Bring them across."""
    os.makedirs(dest_dir, exist_ok=True)
    rel = {name: os.path.relpath(p, base) for name, p in remote.items()}
    got = get(node.collect.chia_remote(base, list(rel.values()),
                                       max_bytes_per_file=256 * 1024 * 1024))
    out = {}
    for name, r in rel.items():
        if r in got.files:
            dest = os.path.join(dest_dir, os.path.basename(remote[name]))
            open(dest, "w").write(got.files[r])
            out[name] = dest
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--design", default="aes")
    ap.add_argument("--platform", default="sky130hd")
    ap.add_argument("--turns", type=int, default=8)
    ap.add_argument("--model", default="gemini-2.5-pro")
    ap.add_argument("--work-root", default="/tmp/loop")
    ap.add_argument("--swiftcts-dir", default=os.environ.get(
        "SWIFTCTS_DIR", os.path.expanduser("~/SwiftCTS/SwiftCTS")))
    ap.add_argument("--k-shot", type=int, default=1)
    ap.add_argument("--no-agent", action="store_true",
                    help="screen and build the top picks without an LLM")
    ap.add_argument("--picks", type=int, default=3,
                    help="candidates to build in --no-agent mode")
    args = ap.parse_args()

    design_config = f"./designs/{args.platform}/{args.design}/config.mk"
    ray.init(address="auto", runtime_env={"py_modules": [chia_openroad]})
    log.info("cluster: %s", {k: v for k, v in ray.cluster_resources().items()
                             if k in ("CPU", "orfs")})

    policy = load_policy(os.path.join(HERE, f"calibration_{args.design}_{args.platform}.json"))
    store = CandidateStore(os.path.join(HERE, f"loop_{args.design}.db"))
    failures = FailureLog(os.path.join(HERE, f"loop_{args.design}.failures.jsonl"))
    surrogate = SwiftCTSEvaluator(
        model_path=os.path.join(args.swiftcts_dir, "saved_models", "model.pkl"),
        swiftcts_dir=args.swiftcts_dir)

    print("\n=== surrogate conformance ===")
    conformance_check(surrogate)

    base = f"{args.work_root}/{args.design}-base"
    with OpenROADNode() as node:
        branched = set()

        def run(stage, **kw):
            """The loop's ORFS callable.

            Anything run outside the base tree is a variation on the shared
            placement, so seed it from the base first. This matters for the
            surrogate's own K-shot anchors as much as for candidates: on aes the
            anchor took 4646 s precisely because it rebuilt synth, floorplan and
            placement that already existed next door.
            """
            work = kw.get("work_home")
            if work and work != base and work not in branched:
                branched.add(work)
                get(node.branch.chia_remote(base, work, kw["design_config"],
                                            through_stage="place"))
            r = get(node.run_stage.chia_remote(stage, **kw))
            if r.success and stage in ("route", "finish"):
                r.summary.update(get(node.measure_clock.chia_remote(
                    kw["work_home"], kw["design_config"], stage="route")))
            return r

        print(f"\n=== 1. shared placement ({args.design}) ===", flush=True)
        t0 = time.monotonic()
        placed = run("place", work_home=base, design_config=design_config, knobs={})
        print(f"    {'built' if placed.success else 'FAILED'} in {placed.elapsed_s:.0f}s",
              flush=True)
        if not placed.success:
            print("   ", placed.failure.as_hint() if placed.failure else "?")
            return 1

        print("\n=== 2. artifacts the surrogate declared ===", flush=True)
        remote = get(node.emit_artifacts.chia_remote(
            base, design_config, list(surrogate.requires) + ["clock_period"],
            stage="place"))
        arts = fetch(node, base, remote, os.path.join(HERE, f"artifacts_{args.design}"))
        print("   ", {k: f"{os.path.getsize(v)//1024}KB" for k, v in sorted(arts.items())},
              flush=True)
        if [a for a in surrogate.requires if a not in arts]:
            print("    missing required artifacts — cannot screen")
            return 1

        state = DesignState(design=args.design, platform=args.platform, work_home=base,
                            design_config=design_config, stage="place",
                            artifacts=arts, metrics=placed.metrics, knobs={})

        print(f"\n=== 3. K={args.k_shot} calibration ===", flush=True)
        t1 = time.monotonic()
        surrogate.calibrate(state, run=run, budget_runs=args.k_shot)
        print(f"    k_shot={surrogate.k_shot} in {time.monotonic()-t1:.0f}s", flush=True)

        print("\n=== 4. screen (computed once, on the driver) ===", flush=True)
        screen = build_screen(surrogate, state, policy)
        print(f"    {surrogate.name} ranked {len(screen['ranked'])} configurations "
              f"by predicted {screen['metric']} in {screen['cost_s']:.2f}s", flush=True)
        for knobs, value in screen["ranked"][:5]:
            print(f"      {value:>12.6g}  "
                  f"{', '.join(f'{k}={v}' for k, v in sorted(knobs.items()))}", flush=True)

        tool = ORFSAgentTool(
            "orfs", policy=policy, store=store, failures=failures,
            design_config=design_config, work_root=args.work_root,
            arm=f"agent+{args.model}" if not args.no_agent else "screen-only",
            branch_from=base, branch_through="place",
            screen=screen, measure_clock=True,
            task_options={"scheduling_strategy": __import__(
                "ray.util.scheduling_strategies", fromlist=["x"]
            ).NodeAffinitySchedulingStrategy(
                ray.get_runtime_context().get_node_id(), soft=False)})

        try:
            print("\n=== 5. build ===", flush=True)
            if args.no_agent:
                # Screen-only arm: build the surrogate's top picks directly, no LLM.
                card = Scorecard(surrogate.name)
                for knobs, predicted in screen["ranked"][:args.picks]:
                    reply = tool.propose_candidate(dict(knobs))
                    print("   ", reply, flush=True)
                    if "started" not in reply:
                        continue
                    cid = int(reply.split()[1])
                    while True:
                        status = tool.candidate_status(cid, max_wait_seconds=170)
                        if "still running" not in status:
                            break
                    print("   ", status, flush=True)
                    row = store.get(cid)
                    if row and row.status == "built" and row.metrics:
                        actual = row.metrics.get(screen["metric"])
                        if isinstance(actual, (int, float)):
                            err = 100 * abs(predicted - actual) / (abs(actual) or 1)
                            print(f"        predicted {predicted:.6g} vs actual "
                                  f"{actual:.6g}  ({err:.1f}%)", flush=True)
                            card.pairs.setdefault(screen["metric"], []).append(
                                (predicted, actual))
                print("\n=== screen accuracy ===")
                print(card.summary())
            else:
                system = ("You are an expert physical-design engineer tuning a clock "
                          "tree. Act only through the tools. Never report a result you "
                          "have not seen returned by candidate_status.")
                llm = VertexGeminiLLM(model=args.model, system_message=system,
                                      timeout_seconds=3600, max_tool_iterations=80)
                task = open(os.path.join(HERE, "prompts", "explore_pd.md")).read()
                task += (f"\n\n## This run\n\nDesign: {args.design} on {args.platform}. "
                         f"A fast surrogate is available: call screen_candidates to see "
                         f"which clock-tree configurations it predicts will do well. "
                         f"Those are predictions — build the promising ones and check. "
                         f"You have about {args.turns} turns. Every candidate branches "
                         f"from a shared placement, so only clock-tree and later stages "
                         f"are rebuilt.\n")
                res = llm.prompt(task, tools=[tool])
                print("\n=== agent summary ===\n" + str(getattr(res, "result", res))[-3000:])
        finally:
            tool.stop()

    print("\n=== ledger ===")
    for c in reversed(store.list(limit=40)):
        print("  " + c.one_line())
    print("\n=== stats ===", json.dumps(store.stats(), indent=1))
    best = store.best("worst_slack")
    print("\n=== best by worst_slack ===\n  " + (best.one_line() if best else "none built"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
