"""Measure the buildable range of each knob on one design, on the cluster.

Mechanical: no LLM involved. Produces the calibrated ranges an agent is later
handed, so it must run before any agent does.
"""
import json, logging, sys, time
import ray
from chia.base.ChiaFunction import get
import chia_openroad
from chia_openroad import cluster
from chia_openroad.calibrate import calibrate, apply
from chia_openroad.knob_policy import KnobPolicy
from chia_openroad.openroad import OpenROADNode

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
DESIGN = "./designs/sky130hd/gcd/config.mk"
# A subset first: the two floorplan knobs whose narrow band we already saw, and
# the two CTS knobs a clock-tree surrogate would screen. Whole-set calibration
# can follow once we know what a probe really costs.
KNOBS = ["CORE_UTILIZATION", "PLACE_DENSITY", "CTS_CLUSTER_SIZE", "CTS_BUF_DISTANCE"]

cluster.init(ray, chia_openroad)
print("resources:", {k: v for k, v in ray.cluster_resources().items()
                     if k in ("CPU", "orfs")}, flush=True)

policy = KnobPolicy()
t0 = time.monotonic()
with OpenROADNode() as node:
    def run(stage, **kw):
        return get(node.run_stage.chia_remote(stage, **kw))
    cals = calibrate(run, policy, DESIGN, "/tmp/calib", names=KNOBS, budget=9)

print(f"\n=== calibration took {(time.monotonic()-t0)/60:.1f} min ===", flush=True)
for name, c in cals.items():
    print(f"  {c.summary()}")
    for value, why in c.failures[:3]:
        print(f"      {value}: {why[:100]}")

out = {n: {"low": c.low, "high": c.high, "nominal": list(c.nominal),
           "probes": c.probes,
           "failures": [[v, w] for v, w in c.failures]} for n, c in cals.items()}
with open("calibration_gcd_sky130hd.json", "w") as f:
    json.dump(out, f, indent=1)
print("\nwrote calibration_gcd_sky130hd.json")

calibrated = apply(policy, cals)
print("\n=== what the agent would now be shown ===")
print(calibrated.describe_for_agent())
