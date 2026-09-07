"""End-to-end through a real CHIA cluster: driver on the head, ORFS in the
worker container. Proves the bring-up machinery, not just Ray."""
import ray
from chia.base.ChiaFunction import get
import chia_openroad
from chia_openroad import cluster
from chia_openroad.openroad import OpenROADNode, run_flow

# The worker runs inside a container that has chia and ORFS but not our code.
# Ship it with the job rather than baking it into the image -- that is what
# CHIA's own examples do, and it means code changes reach workers without a
# rebuild of a 8 GB image.
cluster.init(ray, chia_openroad)
print("cluster resources:", {k: v for k, v in ray.cluster_resources().items()
                             if k in ("CPU", "orfs")}, flush=True)

with OpenROADNode() as node:
    def run(stage, **kw):
        return get(node.run_stage.chia_remote(stage, **kw))

    # Gate at cts, then finish -- the fail-fast path, on a real cluster.
    results = run_flow(run, "/tmp/work/cand-a", "./designs/sky130hd/gcd/config.mk",
                       {"CORE_UTILIZATION": 40, "CTS_CLUSTER_SIZE": 30})
    for r in results:
        print(f"  {r.stage:8s} success={r.success} {r.elapsed_s:6.1f}s "
              f"ckpt={(r.checkpoint or '').split('/')[-1] or '-'}", flush=True)

    last = results[-1]
    print("\nsummary:", {k: last.summary.get(k) for k in
                        ("worst_slack", "power_total", "clock_skew_setup",
                         "clock_buffer_count")}, flush=True)
    print("VERDICT:", "PASS" if last.success and last.checkpoint else "FAIL")
