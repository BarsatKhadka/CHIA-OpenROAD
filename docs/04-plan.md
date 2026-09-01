# Implementation Plan

Written after reading the CHIA source (`external/chia`), not just its docs. Compute target
is **GCP**, decided. See `03-setup.md` for the environment and `00-objective.md` for the goal.

## What reading the code changed

Five things in CHIA's source constrain the design more than the proposal anticipated.

### 1. `ColocatedNode` is mandatory, not optional

`chia/vlsi/hammer.py` opens with the exact problem we have:

> obj_dir is PATH-BASED: it lives on the worker that ran the action, so chained actions
> (syn -> syn-to-par -> par) and report fetches must land on the SAME worker.

ORFS `WORK_HOME` is path-based in precisely the same way. Synthesis → floorplan → placement →
CTS → routing → verification must all land on one worker, or the ODB checkpoints aren't there.
`HammerNode` solves it by subclassing `chia.base.colocated.ColocatedNode`, which pins a family
of `@ChiaFunction` members to one placement-group bundle (`STRICT_PACK`).

**`OpenROADNode(ColocatedNode)` — one node instance per candidate, one placement group per
candidate.** Parallelism is across candidates, never within a flow (which also matches `-j1`).

### 2. Long tool calls need a start/poll split

`examples/timing_opt/timing_experiment_tool.py` documents why:

> a sub-block Genus run on MegaBoom typically takes 5-30 min, which exceeds Claude's MCP HTTP
> timeout. By returning a handle in sub-seconds and letting Claude poll, we get arbitrary
> synth durations without disconnects.

Our CTS+route candidates are minutes to tens of minutes. The agent-facing MCP tool must be
`start_candidate() -> handle` / `candidate_status(handle, max_wait_seconds)`, not a blocking
call. Keep `max_wait_seconds` under ~200.

### 3. `timing_opt` is already the shape of our loop

It is the paper's §5.3 critical-path study, and structurally it is what we are building: the
LLM reads a timing report it is too large to inline (staged on the worker, grepped through a
`bash` tool), proposes edits, and validates them with **fast sub-block syntheses** before
anything expensive runs — a cheap evaluator screening candidates for an expensive ground truth.
That is the same role SwiftCTS plays. Follow its structure; swap sub-block synthesis for the
surrogate.

It also keeps a **SQLite branch tree** of design variants (`db.py`). We want the same for
candidate configurations — it is how the 4-arm comparison gets its tool-invocation counts.

### 4. GCP is first-class, and the head is always local

`chia/cluster/gcp_nodes.py` + `config.py` give a `gcp_nodes:` section. Notable:

- `provider.type` is **deprecated and ignored** — "CHIA always runs a local head node, with
  cloud workers added via aws_nodes / gcp_nodes."
- Auth is Application Default Credentials (`gcloud auth application-default login`).
- `spot: true` is supported per node type — the main cost lever against the $450 line.
- Cloud IPs are unknown until provisioning, so node types reference them by
  `@<node_type>:<index>` placeholders in `compatible_ips`.
- `tailnet:` (Tailscale) is the **recommended** transport; without it, workers fall back to
  reverse SSH tunnels routed through the head.

### 5. The worker image needs conda, because of a 7-patch Python gap

| Image | Python |
|---|---|
| `openroad/orfs:latest` (Ubuntu 22.04) | 3.10.12 |
| `rayproject/ray:2.54.0-cpu` | 3.10.19 |

CHIA pins 3.10.19 to match its workers. `dockerfiles/install-conda.sh` exists for exactly this.
Build `FROM openroad/orfs:latest`, add conda 3.10.19, then `ray[default]==2.54.0` + `chia`.
Going the other way (Ray base + copy ORFS in) means rebuilding the PDK/tool tree — don't.

---

## Where the code lives

Deliverable #5 is "upstream-ready". So write it where it would land upstream:

```
fork of ucb-bar/chia  (BarsatKhadka/chia, branch: openroad)
├── chia/vlsi/openroad.py          # OpenROADNode  — deliverable #1
├── chia/analysis/surrogate.py     # SurrogateEvaluator ABC — deliverable #3
├── dockerfiles/OrfsDockerfile     # the worker image
└── examples/openroad_orfs/        # the loop, prompts, cluster.yaml — deliverable #2

CHIA-OpenROAD  (this repo)
├── chia_openroad/swiftcts_evaluator.py   # SwiftCTS as a SurrogateEvaluator impl
├── experiments/                          # 4-arm configs, results DB, analysis — #4
└── docs/
```

Repoint `external/chia` at the fork. SwiftCTS stays out of the upstream PR — it is separate
prior work, and keeping it out is what proves the interface is genuinely replaceable.

---

## Phases

### Phase 0 — GCP cluster (you're setting this up)

Needed from you: project ID, a zone, ADC (`gcloud auth application-default login`), and an
SSH keypair. Then `cluster.yaml`:

```yaml
cluster_name: chia_openroad

gcp_nodes:
    project: <your-project>
    zone: us-central1-a
    orfs_worker:
        machine_type: n2-standard-8      # size against the medium design
        count: 4
        spot: true                       # the $450 lever
        disk_size_gb: 200                # ODB checkpoints per candidate add up
        ssh_user: chia
        ssh_public_key: ${HOME}/.ssh/id_ed25519.pub
        ssh_private_key: ${HOME}/.ssh/id_ed25519

available_node_types:
    orfs_worker:
        resources: {"orfs": 8}
        num_workers: 4
        compatible_ips: ["@orfs_worker:0", "@orfs_worker:1",
                         "@orfs_worker:2", "@orfs_worker:3"]
        docker:
            image: <registry>/chia-orfs:latest
            container_name: "chia-orfs-${USER}"
            pull_before_run: True

tailnet:
    # recommended over SSH tunnels; see cluster_config_reference.rst
```

Open sizing question: ORFS is largely single-threaded per flow (`-j1`), so the choice is many
small VMs vs. fewer large ones running several candidates each. Detailed routing on the medium
design is the memory high-water mark — gcd/nangate45 peaked at ~1 GB, but that is the small
end. **Measure the medium design's peak RSS before fixing `machine_type` and `count`.**

Spot preemption is survivable specifically because ORFS `make` resumes from the last valid
stage — the smoke test already demonstrated this when the LEC failure left `3_place.odb`
intact and the rerun restarted at CTS.

### Phase 1 — Worker image

`dockerfiles/OrfsDockerfile`: `FROM openroad/orfs:latest` → conda 3.10.19 →
`ray[default]==2.54.0` → `pip install chia`. Push to Artifact Registry in the same project
(egress and pull time both matter when 4+ workers pull a ~6.5 GB image).

Verify on a worker: `ray status`, then a `gcd`/`sky130hd` flow inside the container.
**Set `LEC_CHECK=1` here** — x86 has the AVX the LEC binary needs. That flag is the whole
dev/experiment environment matrix so far.

### Phase 2 — `OpenROADNode` (deliverable #1)

`ColocatedNode` subclass, `_MEMBER_FNS` covering:

| Member | Returns |
|---|---|
| `prepare(design, platform, rtl, sdc)` | generates `config.mk` + `constraint.sdc` in `WORK_HOME` |
| `run_stage(stage, knobs)` | `make -j1 <stage>` → structured metrics + checkpoint path |
| `metrics()` | parsed `logs/<plat>/<design>/base/*.json` |
| `verify()` | `make drc lvs` → violation counts |
| `collect` / `collect_fs` / `match` | copy reports back, mirroring `HammerNode`'s API |

Lift the run mechanics from `~/ChipDreamer/datagen/orfs_run.py` — it already encodes the
validated `make` sequence, the floorplan/route double-run quirk, `SYNTH_MEMORY_MAX_BITS`, and
metric-key extraction from `6_report.json`. Swap Singularity for the Docker/Ray worker.

Metrics are read from ORFS's own JSON, never scraped from logs. Cache tags key on
`(design, platform, stage, knob-hash)` so `chia.base.cache` can replay a populated tree.

### Phase 3 — Agent tools, and the trust boundary (deliverable #2)

Read-only inspection tools (timing, congestion, clock tree, power, area, violations) plus:

- `list_legal_knobs()` — from `~/ChipDreamer/datagen/orfs_knob_map.md`, which already
  distinguishes relative from absolute knobs and documents `CTS_CLK_MAX_WIRE_LENGTH` as
  having no OpenROAD equivalent. That table **is** the bounded action space.
- `start_candidate(knobs) -> handle` / `candidate_status(handle, max_wait_seconds)`
- `compare(handles)` — reads from the candidate DB

**Nothing the agent can call executes DRC/LVS, parses a report, or checks a constraint.**
Those run programmatically in `OpenROADNode`. The agent proposes knobs and reads summaries;
that is the entire surface. This mirrors how `timing_opt` gates its LLM behind an AI-free
re-synthesis, and how the ISA study walls the agent off from Spike.

### Phase 4 — Surrogate interface (deliverable #3)

```python
class SurrogateEvaluator(ABC):
    def calibrate(self, node: OpenROADNode, budget_runs: int = 2) -> None: ...
    def predict(self, knobs: list[dict]) -> list[dict]: ...     # cheap, vectorized
    def pareto_select(self, preds, k: int) -> list[int]: ...
```

`SwiftCTSEvaluator` implements it (clock power, clock wirelength, skew; 100k configs in <10 s;
1–2 OpenROAD calibration runs). The loop calls `predict` → `pareto_select` → real ORFS CTS+route
on the selected few. **OpenROAD stays ground truth.**

The comparison needs no new extraction code — `6_report.json` already carries
`finish__clock__skew__setup`, `finish__design__instance__area__class:clock_buffer`,
`finish__design__instance__count__class:clock_buffer`, and `finish__power__total`.

Ship a second trivial implementation (a random or nearest-neighbour selector) so the interface
is demonstrably not SwiftCTS-shaped. That is what makes deliverable #3 credible.

### Phase 5 — Evaluation (deliverable #4)

**Platform is forced: sky130hd.** It is the only conventional open PDK in ORFS shipping both a
DRC deck (`sky130hd.lydrc`) and an LVS deck (`sky130hd.lylvs`) — and expected result #2 is a
DRC/LVS-clean layout. nangate45 stays for fast iteration but cannot back an LVS claim.

Four arms, equal compute budget, small + medium design:

| Arm | Tests |
|---|---|
| default ORFS | baseline |
| ORFS AutoTuner | existing automated tuning |
| report-aware agent, no surrogate | value of the agent alone |
| agent + SwiftCTS | value of surrogate screening |

Metrics: tool invocations, wall-clock, final QoR, DRC/LVS status. Record every candidate in a
SQLite tree like `timing_opt/db.py` — invocation counts fall out of it for free.

---

## Risks

| Risk | Detail | Mitigation |
|---|---|---|
| **Vertex Gemini is experimental** | `chia/models/vertex.py` says so in its own docstring: "WARNING: experimental… Only exercised by… mocked unit tests… Not validated in production." The proposal budgets $180 of Gemini. | Prototype the loop against `chia/models/claude.py` (what every published case study used), and treat Gemini-on-Vertex as a swap to validate early, not on the critical path. |
| Spot preemption | Long medium-design flows get killed | ORFS resume + CHIA caching already cover this; verify a mid-flow kill actually resumes |
| Image pull cost | ~6.5 GB × N workers | Artifact Registry in-project, `pull_before_run` once |
| Medium-design memory | Detailed routing is the high-water mark | Measure before fixing `machine_type` |
| kepler-formal licensing | It sits under `tools/install/licenses` in the image | Confirm `LEC_CHECK=1` is actually usable on GCP before depending on it |

## Immediate next steps

1. You: GCP project + ADC + SSH key.
2. Me: `OrfsDockerfile` and a `cluster.yaml` filled in from your project details.
3. Me: fork `ucb-bar/chia`, repoint `external/chia`, stub `chia/vlsi/openroad.py`.
4. Both: one `gcd`/**sky130hd** flow on a GCP worker, end to end, `LEC_CHECK=1`, through to
   `make drc lvs`. That single run validates image, cluster, platform, and the DRC/LVS
   deliverable path in one shot — do it before writing any loop logic.
