# Build Steps

Ordered, one at a time. Each step has a **done-when** you can check before moving on.
Nothing gets written until it is the current step.

Settled and not re-litigated here: GCP for compute, sky130hd as the platform, DRC/LVS on
sky130hd known-good from prior projects. Rationale in `04-plan.md`.

---

## Step 0 — Plumbing (minutes, not a build step)

Fork `ucb-bar/chia` → `BarsatKhadka/chia`, branch `openroad`. Repoint `external/chia` at the
fork. This decides where every later file lands, so it happens before anything is written.

**Done when:** `git -C external/chia remote -v` shows the fork on branch `openroad`.

---

## Step 1 — The ORFS worker image  ✅ DONE

**Goal.** One container that is simultaneously the ORFS execution environment and a valid CHIA
Ray worker. Same image for local development and for every GCP worker.

**Writes.** `dockerfiles/OrfsDockerfile` (in the fork) + a build/push script.

Recipe: `FROM openroad/orfs:latest` → conda Python 3.10.19 (`dockerfiles/install-conda.sh`
exists for exactly this) → `ray[default]==2.54.0` → `pip install chia`.

**Why this is first.** Everything downstream executes inside it, and it is the one step that
is not blocked on your GCP project details.

It also forces the correct execution model. On a CHIA worker the `@ChiaFunction` body runs
*inside* the container and calls `make` directly. If we wrote `OpenROADNode` first, on this
Mac, the natural shape would be "host shells out to `docker run`" — which is wrong, and we
would rewrite it the moment it touched a real worker. Building the image first makes the local
and cluster execution models identical from the start.

**Done when.** Inside the image: `python --version` is 3.10.19, `import ray, chia` succeeds,
and `make -j1 LEC_CHECK=1 WORK_HOME=/work DESIGN_CONFIG=.../sky130hd/gcd/config.mk` reaches
`6_final.gds`.

**Result.** `chia-orfs:latest` built from `dockerfiles/OrfsDockerfile`. Verified in-image:
`python` = 3.10.19 (conda `chia_env`) with `ray 2.54.0` + `chia`; `PYTHON_EXE` = 3.10.12
(`/usr/bin/python3`) with `yaml`/`pandas` for ORFS's helper scripts; openroad 26Q3-1510,
yosys 0.68, klayout 0.30.7. gcd/sky130hd ran to `6_final.gds` in **3m27s** (91 s tool time,
914 MB peak, detailed routing dominating at 52 s).

**Open question this step resolves.** The image is amd64. The Mac's Ray head would be arm64.
Whether a cross-arch Ray driver/worker pair is viable is untested — if it is not, local
development runs head and worker both inside the amd64 container, or drives `OpenROADNode`
through `ChiaFunction`'s local (non-Ray) call path. Step 1 tells us which.

---

## Step 2 — `OpenROADNode`, local only  ✅ BODY DONE (dispatch pending Step 3)

**Goal.** ORFS stages as `@ChiaFunction` members on a `ColocatedNode`, returning structured
metrics — no cluster, no agent, no surrogate.

**Writes.** `chia/vlsi/openroad.py`.

Members: `prepare`, `run_stage`, `metrics`, `verify`, and `collect`/`collect_fs`/`match`
mirroring `HammerNode`. Run mechanics lifted from `~/ChipDreamer/datagen/orfs_run.py`.

**Done when.** A local run drives gcd/sky130hd synth → GDS through the node, returns parsed
metrics from ORFS's own JSON (never scraped from logs), and a second run with an unchanged
knob set resumes rather than recomputing.

**Result.** All checks green (`experiments/test_openroad_node.py`,
`experiments/test_invalidation.py`). Ray dispatch could NOT be tested here — Ray does not run
under Rosetta, and even a local `ChiaFunction` call initialises Ray, so the bodies are exercised
through `._chia_original`. Dispatch moves to Step 3.

Grew beyond the original scope, because the first version was quietly wrong:

- **Stage-scoped invalidation.** `make -n` proves ORFS invalidates *nothing* on a knob change —
  `CORE_UTILIZATION=45`, `CTS_CLUSTER_SIZE=20`, even a knob that does not exist, all report
  "all up to date". Make compares timestamps, and a knob is not a file. So `run_stage` records
  the knobs that built a tree (`.chia_orfs_knobs.json`), diffs on the next call, and deletes
  every artifact from the earliest affected stage onward. Verified: a CTS knob change reuses
  `1/2/3_*.odb` and rebuilds `4/5_*` (135 s vs 201 s).
- **Knob validation.** Shape, existence against all 249 ORFS variables, deprecation, and type
  where ORFS declares one (15 of 249). Catches the silent-typo case that would otherwise
  corrupt a sweep.
- **Fail-fast.** `run_flow(gate="cts")` proves feasibility before paying for routing. Measured
  on gcd/sky130hd: everything through cts is 11 s of 91 s; routing is 68 s. A doomed config
  costs 47 s instead of ~200 s.
- **Failure memory.** `OrfsFailure` extracts the tool's own code and message from the stage log
  (`GPL-0301: Utilization 106.593 % exceeds 100%` at `3_3_place_gp`), and `FailureLog` stores
  them for feeding back to the agent and for answering exact repeats without a run.

**Measured, and it shapes Step 4.** `CORE_UTILIZATION` on gcd/sky130hd: 35 builds, 38 default,
40 builds, 42 fails at global route, 50 fails at CTS, 65 fails at global placement. A ±2 window
around the design's own default, failing at three different stages depending how far you
overshoot. No LLM will guess that.

---

## Step 3 — GCP cluster  ← NEXT

**Goal.** The same node running on real GCP workers.

**Writes.** `examples/openroad_orfs/cluster.yaml`.

**Needs from you.** Project ID, zone, ADC, SSH keypair.

**Done when.** `chia up` brings up workers, `ray status` shows the `orfs` resource, and the
Step 2 flow runs on a worker end to end with `LEC_CHECK=1` through `make drc lvs`.
Also: kill a worker mid-flow and confirm it resumes from the last valid stage.

---

## Step 4 — Agent tools and the trust boundary

**Goal.** The agent can read reports and propose bounded knob changes — and can do nothing else.

**Writes.** `examples/openroad_orfs/orfs_tools.py`, prompts.

Read-only inspection tools; `start_candidate` / `candidate_status` (start/poll split — blocking
calls exceed the MCP HTTP timeout); a candidate DB.

`list_legal_knobs()` from three sources: ORFS's own 18 `tunable` variables (in
`knob_specs.py`), unioned with `~/ChipDreamer/datagen/orfs_knob_map.md` (which adds
PLACE_DENSITY, GPL_*, ROUTING_LAYER_ADJUSTMENT — swept by AutoTuner but not marked tunable).

**Per-design range calibration.** ORFS publishes no ranges, and the feasible band is
design-specific (see the CORE_UTILIZATION measurement in Step 2). Probe each knob's buildable
band once per design and expose only that. Without it, the tool-invocation metric measures the
LLM's ignorance of the design rather than the value of surrogates.

**Knob-efficacy test.** `make FOO=bar` is not an error, and a knob that is wired but inert is
just as damaging. For each legal knob, run two extreme values and assert some metric moves;
drop any that does not, with a recorded reason. Use a design big enough for CTS knobs to bite —
on gcd, `CTS_CLUSTER_SIZE` 30→20 left the clock buffer count unchanged at 7.

**Done when.** The agent completes a candidate loop, and no tool it can call executes DRC/LVS,
parses a report, or checks a constraint.

---

## Step 5 — Surrogate interface

**Goal.** The replaceable evaluator socket. Deliverable #3 is this interface, not SwiftCTS.

**Writes.** `chia/analysis/surrogate.py` + a second implementation.

Make the second one a **feasibility** predictor ("will this build?"), not another quality model.
It plugs into the same socket, saves whole flows rather than shaving them, and demonstrates the
interface is general far better than two quality models would.

**Done when.** Two implementations satisfy it and the loop swaps between them by config alone.

---

## Step 6 — SwiftCTS evaluator

**Writes.** `chia_openroad/swiftcts_evaluator.py` (this repo — deliberately *not* upstream).

**Done when.** Calibrates on 1–2 OpenROAD runs, screens candidates, and its predictions are
compared against `finish__clock__skew__setup` / `..._class:clock_buffer` / `finish__power__total`
straight out of `6_report.json`.

---

## Step 7 — The loop

**Writes.** `examples/openroad_orfs/orfs_loop.py`.

**Done when.** One DRC/LVS-clean sky130hd layout produced end to end by the agent.

---

## Step 8 — Four-arm evaluation

Default ORFS / ORFS AutoTuner / agent without surrogate / agent with SwiftCTS. Equal compute
budget, small + medium design.

**Done when.** Tool invocations, wall-clock, QoR, and DRC/LVS status recorded for all four.

---

## Step 9 — Upstream

Clean the fork's diff to the parts that belong in `ucb-bar/chia`, with docs and an example.
SwiftCTS stays out — that separation is what demonstrates the interface is replaceable.
