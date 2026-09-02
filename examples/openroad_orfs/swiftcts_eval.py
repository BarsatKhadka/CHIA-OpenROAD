"""SwiftCTS end to end on a design it was trained on.

Builds one aes placement, hands SwiftCTS the DEF and timing paths it needs,
calibrates at K=1, predicts across the CTS knob grid, then runs a few of those
configurations for real and scores the predictions against ORFS.

    python swiftcts_eval.py --design aes --candidates 4
"""
import argparse, itertools, json, logging, os, sys, time

import ray
from chia.base.ChiaFunction import get

import chia_openroad
from chia_openroad.openroad import OpenROADNode, run_flow
from chia_openroad.surrogate import DesignState, Scorecard, conformance_check
from chia_openroad.surrogates.swiftcts import SwiftCTSEvaluator, TRAINED_DOMAIN

HERE = os.path.dirname(os.path.abspath(__file__))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("swiftcts_eval")


def grid(n_per_knob=8):
    """CTS configurations spanning the range SwiftCTS was fitted over."""
    axes = []
    for name, (low, high) in TRAINED_DOMAIN.items():
        step = (high - low) / (n_per_knob - 1)
        vals = [round(low + i * step, 3) for i in range(n_per_knob)]
        if name == "CTS_CLUSTER_SIZE":
            vals = sorted({int(round(v)) for v in vals})
        axes.append([(name, v) for v in vals])
    return [dict(combo) for combo in itertools.product(*axes)]


def spread(candidates, k):
    """k configurations spaced through the list, rather than k adjacent ones."""
    if k >= len(candidates):
        return candidates
    step = len(candidates) / k
    return [candidates[int(i * step)] for i in range(k)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--design", default="aes")
    ap.add_argument("--platform", default="sky130hd")
    ap.add_argument("--candidates", type=int, default=4, help="real ORFS runs to score against")
    ap.add_argument("--k-shot", type=int, default=1)
    ap.add_argument("--work-root", default="/tmp/swiftcts")
    ap.add_argument("--swiftcts-dir", default=os.environ.get(
        "SWIFTCTS_DIR", os.path.expanduser("~/SwiftCTS/SwiftCTS")),
        help="checkout containing swiftcts.py and saved_models/model.pkl")
    args = ap.parse_args()

    design_config = f"./designs/{args.platform}/{args.design}/config.mk"
    ray.init(address="auto", runtime_env={"py_modules": [chia_openroad]})
    log.info("cluster: %s", {k: v for k, v in ray.cluster_resources().items()
                             if k in ("CPU", "orfs")})

    surrogate = SwiftCTSEvaluator(
        model_path=os.path.join(args.swiftcts_dir, "saved_models", "model.pkl"),
        swiftcts_dir=args.swiftcts_dir)
    print("\n=== conformance ===")
    conformance_check(surrogate)

    with OpenROADNode() as node:
        def run(stage, **kw):
            """The loop's ORFS callable. Attaches measured clock metrics to a
            completed flow so both K-shot calibration and the scorecard see the
            same ground truth."""
            r = get(node.run_stage.chia_remote(stage, **kw))
            if r.success and stage in ("finish", "route"):
                clock = get(node.measure_clock.chia_remote(
                    kw["work_home"], kw["design_config"], stage="route"))
                r.clock_metrics = clock
                r.summary.update(clock)
            return r

        base = f"{args.work_root}/{args.design}-base"
        print(f"\n=== 1. build the placement ({args.design}) ===", flush=True)
        t0 = time.monotonic()
        placed = run("place", work_home=base, design_config=design_config, knobs={})
        print(f"    place: success={placed.success} {placed.elapsed_s:.0f}s", flush=True)
        if not placed.success:
            print("    ", placed.failure.as_hint() if placed.failure else "no reason")
            return 1

        print("\n=== 2. emit what the surrogate declared it requires ===", flush=True)
        remote = get(node.emit_artifacts.chia_remote(
            base, design_config, list(surrogate.requires) + ["clock_period"], stage="place"))
        print(f"    produced on the worker: {sorted(remote)}", flush=True)

        # The surrogate runs driver-side; the artifacts live in the worker
        # container, which has no shared filesystem with the head. Fetch them.
        local_dir = os.path.join(HERE, f"artifacts_{args.design}")
        os.makedirs(local_dir, exist_ok=True)
        patterns = [os.path.relpath(v, base) for v in remote.values()]
        fetched = get(node.collect.chia_remote(base, patterns,
                                               max_bytes_per_file=256 * 1024 * 1024))
        arts = {}
        for name, remote_path in remote.items():
            rel = os.path.relpath(remote_path, base)
            if rel not in fetched.files:
                print(f"    could not fetch {name} ({rel})")
                continue
            dest = os.path.join(local_dir, os.path.basename(remote_path))
            with open(dest, "w") as f:
                f.write(fetched.files[rel])
            arts[name] = dest
        for k, v in sorted(arts.items()):
            print(f"    {k:14s} {os.path.getsize(v):>10,d} B  (fetched to head)")
        missing = [a for a in surrogate.requires if a not in arts]
        if missing:
            print(f"    MISSING {missing} — cannot proceed")
            return 1

        state = DesignState(design=args.design, platform=args.platform, work_home=base,
                            design_config=design_config, stage="place",
                            artifacts=arts, metrics=placed.metrics, knobs={})
        print("\n" + state.describe())

        print(f"\n=== 3. K={args.k_shot} calibration ===", flush=True)
        t1 = time.monotonic()
        surrogate.calibrate(state, run=run, budget_runs=args.k_shot)
        print(f"    k_shot={surrogate.k_shot}  ({time.monotonic()-t1:.0f}s)", flush=True)

        print("\n=== 4. predict across the fitted grid ===", flush=True)
        cands = grid()
        t2 = time.monotonic()
        preds = surrogate.predict(state, cands)
        dt = time.monotonic() - t2
        print(f"    {len(cands)} configurations in {dt:.2f}s "
              f"({1000*dt/len(cands):.2f} ms each)", flush=True)
        print(f"    a real ORFS flow is ~200s, so this screens "
              f"{200*len(cands)/max(dt,1e-9):,.0f}x faster than building them", flush=True)

        chosen = spread(cands, args.candidates)
        print(f"\n=== 5. build {len(chosen)} of them for real ===", flush=True)
        card = Scorecard(surrogate.name)
        rows = []
        for i, knobs in enumerate(chosen):
            pred = surrogate.predict(state, [knobs])[0]
            work = f"{args.work_root}/{args.design}-c{i}"
            res = run_flow(run, work, design_config, knobs, gate=None, target="finish")
            last = res[-1]
            ok = last.success
            print(f"    [{i}] {knobs} -> {'built' if ok else 'FAILED'} "
                  f"{sum(r.elapsed_s for r in res):.0f}s", flush=True)
            if ok:
                card.observe(pred, last.summary, built=True)
                rows.append((knobs, pred.values, last.summary))
            elif last.failure:
                print(f"        {last.failure.as_hint()[:110]}", flush=True)

        print("\n=== 6. predicted vs actual ===")
        for knobs, pv, av in rows:
            print(f"  {json.dumps(knobs)}")
            for key in sorted(pv):
                a = av.get(key)
                if isinstance(a, (int, float)):
                    err = 100 * abs(pv[key] - a) / (abs(a) or 1)
                    print(f"     {key:22s} pred={pv[key]:<12.6g} actual={a:<12.6g} {err:6.1f}%")
        print("\n=== scorecard ===")
        print(card.summary())
        with open(os.path.join(HERE, f"swiftcts_scorecard_{args.design}.json"), "w") as f:
            json.dump({"k_shot": surrogate.k_shot, "grid": len(cands),
                       "predict_s": dt,
                       "rows": [{"knobs": k, "pred": p, "actual": {kk: vv for kk, vv in a.items()
                                 if isinstance(vv, (int, float))}} for k, p, a in rows]}, f, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
