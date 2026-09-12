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

import chia_openroad
from chia_openroad import cluster
from chia_openroad.candidate_store import CandidateStore
from chia_openroad.failure_log import FailureLog
from chia_openroad.knob_policy import KnobPolicy
from chia_openroad.openroad import OpenROADNode
from chia_openroad.orfs_tools import ORFSAgentTool
from chia_openroad.iterate import run_iterations
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
    ap.add_argument("--llm-timeout", type=int, default=180,
                    help="per-request HTTP timeout, enforced by our own client")
    ap.add_argument("--resume", action="store_true",
                    help="continue a persisted transcript instead of starting over")
    ap.add_argument("--llm-deadline", type=int, default=2400,
                    help="hard wall-clock cap on the whole agent session, "
                         "enforced here rather than by the backend")
    ap.add_argument("--no-agent", action="store_true",
                    help="screen and build the top picks without an LLM")
    ap.add_argument("--no-tools", action="store_true",
                    help="deny the agent read-only tool calls inside a turn "
                         "(control arm: everything is pushed in the prompt)")
    ap.add_argument("--no-consult", action="store_true",
                    help="skip the surrogate consultation round each iteration "
                         "(the control arm: one model call instead of two)")
    ap.add_argument("--picks", type=int, default=3,
                    help="candidates to build in --no-agent mode")
    args = ap.parse_args()

    design_config = f"./designs/{args.platform}/{args.design}/config.mk"
    cluster.init(ray, chia_openroad)
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

        # Thread budget for work the driver runs itself -- the shared
        # placement, the measured default, and the surrogate's K-shot anchors.
        # run_stage falls back to os.cpu_count() when given nothing, so each of
        # those was taking all 32 threads. With four designs starting at once
        # that is 128 threads on 32 cores and a load average of 82: builds
        # thrash instead of finishing. Candidates already divide by the slot
        # count; this makes the driver's own calls do the same.
        _slots = max(1, int(ray.cluster_resources().get("orfs", 1)))
        # Ray's CPU total counts the head and the worker separately, and here
        # they are the same physical machine: it reports 64 on a 32-core host.
        # Dividing that by the slot count hands out twice the threads that
        # exist. Cap by the machine's real cores.
        _cpus = min(int(ray.cluster_resources().get("CPU", os.cpu_count() or 1)),
                    os.cpu_count() or 1)
        node_threads = max(1, _cpus // _slots)
        print(f"    driver threads per build: {node_threads} "
              f"({_cpus} cpu / {_slots} slots)", flush=True)

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
            r = get(node.run_stage.chia_remote(stage, num_cores=node_threads, **kw))
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

        # The default configuration, measured, so the agent has something to
        # beat. Without it the ledger shows only what the loop built, and a run
        # where every candidate is worse than doing nothing is indistinguishable
        # from one where every candidate is better. Continues from the shared
        # placement, so it costs the stages after place rather than a full flow.
        print("\n=== 1b. default configuration (the baseline) ===", flush=True)
        t_base = time.monotonic()
        based = run("finish", work_home=base, design_config=design_config, knobs={})
        baseline = dict(based.summary) if based.success else None
        if baseline:
            print(f"    slack={baseline.get('worst_slack')} "
                  f"skew={baseline.get('clock_skew_setup')} "
                  f"power={baseline.get('power_total')} "
                  f"area={baseline.get('instance_area')} "
                  f"in {time.monotonic()-t_base:.0f}s", flush=True)
        else:
            print("    default build FAILED; the agent will run without a "
                  "baseline to compare against", flush=True)

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
            # A list on purpose: a user with a floorplan feasibility model and
            # a CTS quality model plugs in both, and predict_knobs asks each
            # about the knobs it observes.
            surrogates=[surrogate], state=state,
            parallel_slots=int(ray.cluster_resources().get("orfs", 1)),
            local_calls=not args.no_agent,
            task_options={"scheduling_strategy": __import__(
                "ray.util.scheduling_strategies", fromlist=["x"]
            ).NodeAffinitySchedulingStrategy(
                ray.get_runtime_context().get_node_id(), soft=False)})

        try:
            print("\n=== 5. build ===", flush=True)
            if args.no_agent:
                # Screen-only arm: build the surrogate's top picks directly, no LLM.
                card = Scorecard(surrogate.name)
                # Dispatch every pick before polling any of them, so this arm
                # uses the same parallelism as the agent arms and the budgets
                # compare like for like.
                pending = []
                for knobs, predicted in screen["ranked"][:args.picks]:
                    reply = tool.propose_candidate(dict(knobs))
                    print("   ", reply, flush=True)
                    if "started" in reply:
                        pending.append((int(reply.split()[1]), knobs, predicted))
                for cid, knobs, predicted in pending:
                    while True:
                        status = tool.candidate_status(cid, max_wait_seconds=3000)
                        # The tool answers "is starting up" first and "is
                        # running" later. Testing for a string it never emits
                        # ends the wait immediately: an earlier version checked
                        # "still running", so every candidate was dispatched,
                        # none waited for, and the arm recorded 0 built while
                        # the builds were killed on exit.
                        if "is running" not in status and "starting up" not in status:
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
                system = ("You are an expert physical-design engineer tuning a "
                          "clock tree. Be concrete and brief. Never claim a result "
                          "you have not been shown.")

                # Programmatic loop, short agent calls, memory in the ledger —
                # the shape CHIA's own 202-iteration gem5 study uses (paper
                # Fig. 3). The agent decides what to build; Python builds it.
                # Nothing waits on a model across an hour-long flow.
                from google import genai
                client = genai.Client(
                    vertexai=True, project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
                    location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"))

                slots = int(ray.cluster_resources().get("orfs", 1))

                def build(proposals):
                    """Dispatch every proposal at once, then collect them all."""
                    ids = []
                    for item in proposals:
                        knobs = item["knobs"] if isinstance(item, dict) and "knobs" in item else item
                        parent = item.get("parent") or 0 if isinstance(item, dict) else 0
                        reply = tool.propose_candidate(dict(knobs), parent_id=parent)
                        print(f"    {reply}", flush=True)
                        if "started" in reply:
                            ids.append(int(reply.split()[1]))
                    for cid in ids:
                        while True:
                            status = tool.candidate_status(cid, max_wait_seconds=3000)
                            if "is running" not in status and "starting up" not in status:
                                break
                        print(f"    {status}", flush=True)

                def consult(knob_dicts):
                    """Price a shortlist with the fitted model, in milliseconds.

                    Goes through the same tool the trust boundary defines, so a
                    consultation cannot reach anything a proposal could not.
                    """
                    return tool.predict_knobs(list(knob_dicts))

                outcome = run_iterations(
                    client=client, model=args.model, system=system,
                    store=store, failures=failures, screen=screen, policy=policy,
                    build=build, iterations=args.turns,
                    consult=None if args.no_consult else consult,
                    baseline=baseline,
                    tool=None if args.no_tools else tool,
                    per_iteration=min(slots, args.picks),
                    transcript_path=os.path.join(HERE, f"transcript_{args.design}.json"))
                print(f"\n=== agent ran {outcome['iterations']} iteration(s) ===")
                for t in outcome["transcript"]:
                    print(f"\n-- iteration {t['iteration']} --")
                    print(t["reply"][:900])
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
