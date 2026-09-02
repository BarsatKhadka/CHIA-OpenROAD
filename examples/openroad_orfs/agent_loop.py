"""Agent-in-the-loop physical design: Gemini proposes ORFS knobs, ORFS decides.

The agent's whole surface is :class:`ORFSAgentTool` — seven MCP tools. It can
list the legal knobs, propose a configuration, poll it, compare results, and
read past failures. It cannot run DRC or LVS, parse a report, or judge whether
a configuration is buildable. That split is the point of the experiment, not an
implementation detail.

    python agent_loop.py --design gcd --turns 6 --model gemini-2.5-pro
"""
import argparse
import json
import logging
import os
import sys
import time

import ray
from chia.models.vertex import VertexGeminiLLM

import chia_openroad
from chia_openroad.candidate_store import CandidateStore
from chia_openroad.failure_log import FailureLog
from chia_openroad.knob_policy import KnobPolicy
from chia_openroad.orfs_tools import ORFSAgentTool

HERE = os.path.dirname(os.path.abspath(__file__))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("agent_loop")


def load_policy(path: str) -> KnobPolicy:
    """Nominal policy, with measured ranges folded in where we have them."""
    policy = KnobPolicy()
    if not os.path.exists(path):
        log.warning("no calibration at %s — every range is nominal, and the "
                    "agent will be told so", path)
        return policy
    with open(path) as f:
        raw = json.load(f)
    measured = {k: (v["low"], v["high"]) for k, v in raw.items()
                if v.get("low") is not None and v.get("high") is not None}
    log.info("loaded calibration for %d knob(s): %s", len(measured), ", ".join(measured))
    return policy.with_calibration(measured)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--design", default="gcd")
    ap.add_argument("--platform", default="sky130hd")
    ap.add_argument("--model", default="gemini-2.5-pro")
    ap.add_argument("--turns", type=int, default=6,
                    help="agent turns; each may propose and poll several candidates")
    ap.add_argument("--work-root", default="/tmp/agent")
    ap.add_argument("--calibration",
                    default=os.path.join(HERE, "calibration_gcd_sky130hd.json"))
    ap.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT"))
    ap.add_argument("--location", default=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"))
    args = ap.parse_args()

    design_config = f"./designs/{args.platform}/{args.design}/config.mk"
    ray.init(address="auto", runtime_env={"py_modules": [chia_openroad]})
    log.info("cluster: %s", {k: v for k, v in ray.cluster_resources().items()
                             if k in ("CPU", "orfs")})

    policy = load_policy(args.calibration)
    store = CandidateStore(os.path.join(HERE, f"candidates_{args.design}.db"))
    failures = FailureLog(os.path.join(HERE, f"failures_{args.design}.jsonl"))

    # Pin the tool actor to the head. It owns driver-side state — the SQLite
    # ledger and the failure log — which live on the head's filesystem. Left to
    # schedule freely it lands inside a worker container, where those paths do
    # not exist ("unable to open database file"). Only run_stage belongs on a
    # worker. timing_opt pins its tool actor to the head for the same reason.
    from ray.util.scheduling_strategies import NodeAffinitySchedulingStrategy
    head_node_id = ray.get_runtime_context().get_node_id()
    tool = ORFSAgentTool(
        "orfs", policy=policy, store=store, failures=failures,
        design_config=design_config, work_root=args.work_root, arm="agent+gemini",
        task_options={"scheduling_strategy":
                      NodeAffinitySchedulingStrategy(head_node_id, soft=False)})

    system = ("You are an expert physical-design engineer tuning an ASIC block. "
              "You may only act through the provided tools. Never claim a result "
              "you have not seen returned by candidate_status.")
    llm = VertexGeminiLLM(model=args.model, system_message=system,
                          project=args.project, location=args.location,
                          timeout_seconds=1800, max_tool_iterations=60)

    with open(os.path.join(HERE, "prompts", "explore_pd.md")) as f:
        task = f.read()
    task += (f"\n\n## This run\n\nDesign: {args.design} on {args.platform}.\n"
             f"You have about {args.turns} turns. Aim to improve worst_slack "
             f"without inflating area or power. Start by reading the legal knobs "
             f"and any past failures.\n")

    t0 = time.monotonic()
    try:
        result = llm.prompt(task, tools=[tool])
        log.info("agent finished rc=%s in %.1f min", getattr(result, "returncode", "?"),
                 (time.monotonic() - t0) / 60)
        text = getattr(result, "stdout", None) or str(result)
        print("\n=== agent's closing summary ===\n" + text[-4000:])
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
