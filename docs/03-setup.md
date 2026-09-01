# Setup

Environment for developing the CHIA-OpenROAD nodes. Verified on this machine:
macOS 15.5, Apple Silicon (arm64), 10 cores, 16 GB RAM, Docker Desktop 29.2.

## Repository layout

```
CHIA-OpenROAD/
├── .venv/                       # Python 3.10.19 (uv-managed)          [gitignored]
├── chia_openroad/               # OUR code: ORFS nodes, MCP tools, surrogate iface
├── docs/                        # objective, CHIA notes, proposal, this file
├── experiments/                 # flow scripts + run configs for the 4-arm eval
└── external/                    # cloned/linked dependencies           [gitignored]
    ├── chia/                    # ucb-bar/chia            (11 MB)
    ├── OpenROAD-flow-scripts/   # ORFS, shallow clone     (1.5 GB)
    └── SwiftCTS -> ~/SwiftCTS   # symlink to your existing clone
```

`external/` is deliberately **not** inside the import path as `chia/` — a top-level
directory named `chia/` in the repo root shadows the installed `chia` package.

## What we cloned / pulled

| Thing | Source | Size | Why |
|---|---|---|---|
| CHIA | `github.com/ucb-bar/chia` | 11 MB | The framework. Installed editable. |
| ORFS | `github.com/The-OpenROAD-Project/OpenROAD-flow-scripts` (`--depth 1`) | 1.5 GB | Host-side `flow/designs/`, `flow/platforms/`, and `flow/docs/user/FlowVariables.md` (the knob list we expose to the agent) |
| SwiftCTS | `github.com/BarsatKhadka/SwiftCTS` | symlink | The surrogate demo |
| ORFS image | `openroad/orfs:latest` (docker) | 1.5 GB compressed | Runs the actual flow; contains a full `/OpenROAD-flow-scripts` |

Full-history ORFS is ~900 MB of git objects; `--depth 1` avoids that. Submodules
(`tools/OpenROAD`, `tools/yosys`) are **not** initialized — the binaries come from the
Docker image, so there is nothing to build from source.

## Reproduce

```bash
cd ~/CHIA-OpenROAD

# 1. Dependencies
mkdir -p external
git clone https://github.com/ucb-bar/chia.git external/chia
git clone --depth 1 https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts.git \
          external/OpenROAD-flow-scripts
ln -s ~/SwiftCTS external/SwiftCTS

# 2. Python 3.10.19 — pinned to match the Python inside CHIA's Docker workers
uv venv --python 3.10.19 .venv
VIRTUAL_ENV=.venv uv pip install -e ./external/chia

# 3. The ORFS container (amd64 — see note below)
docker pull --platform linux/amd64 openroad/orfs:latest
```

CHIA's docs recommend conda; `uv` is used here instead because it pins 3.10.19 exactly
without installing a second package manager. Ray only requires the driver and workers to
agree on the Python *minor* version, so a conda env works identically if you prefer it.

### Verify

```bash
.venv/bin/python -c "from chia.base.ChiaFunction import ChiaFunction; print('ok')"
.venv/bin/chia --help
```

## Architecture note: amd64 under Rosetta

`openroad/orfs` publishes **amd64 only** — there is no arm64 tag. Every `docker run` needs
`--platform linux/amd64` and executes under Rosetta emulation.

**The full RTL-to-GDS flow works on this Mac**, with one flag. Verified end-to-end:

```
gcd / nangate45, LEC_CHECK=0 -> 6_final.gds   99 s wall, 58 s of tool time
```

### The one flag: `LEC_CHECK=0`

With defaults, the flow dies at CTS:

```
Error: cts.tcl, 83 child killed: illegal instruction
```

This is **not** OpenROAD and **not** CTS. Rosetta exposes only `sse4_1 sse4_2` — no AVX/AVX2/FMA
— but `objdump` finds zero AVX instructions in the `openroad` binary. The message says *child
killed*: `flow/scripts/lec_check.tcl:61` `run_lec_test` shells out to **kepler-formal**, a
third-party closed binary (an ORFS submodule), and *that* uses AVX.

It is optional and gated:

```tcl
proc lec_check_enabled { } {
  return [expr { [env_var_equals LEC_CHECK 1]
                 && [info exists ::env(KEPLER_FORMAL_EXE)] && ... }]
}
```

So `LEC_CHECK=0` locally; run LEC on the x86 machine. **First entry in the environment matrix** —
flow variables that must differ between the dev box and the experiment machine.

Expect roughly 2-4x slowdown versus native x86. gcd/sky130hd is ~3.5 min; a medium design
will be far more.

### Ray does not run under Rosetta — verified

A plain `ray.init()` inside the amd64 container, with no chia and no project code involved,
dies with:

```
Failed to register worker to Raylet: IOError: Failed to read data from the socket: End of file
```

Raising `/dev/shm` from Docker's default 64 MB to 2 GB and then 4 GB does not help (the shm
warning is real but is not the cause).

This reaches further than it first appears: `ChiaFunction._wrapper` calls `get_profiler()`,
which calls `get_collector()`, which looks up a Ray actor — so **even a local, non-remote
ChiaFunction call initializes Ray**. On this Mac the decorator is unusable in both directions.

Consequence for local development: test node *bodies* through `._chia_original` (the raw
undecorated function `ChiaFunction` stores as an attribute) and defer every dispatch check —
placement groups, `.chia_remote`, `get()` — to a native x86 worker. That is what the two modes
of `experiments/test_openroad_node.py` are for. The split is acceptable because the decorator
is CHIA's own code, exercised by `hammer.py` and every published case study; what is new and
unproven here is the body.

This Mac is a development machine, not the experiment machine.

## Compute target — GCP (decided)

The 4-arm evaluation runs on **GCP** — CHIA has a first-class `gcp_nodes:` provider
(`chia/cluster/gcp_nodes.py`), so this is the native path. See `04-plan.md` Phase 0.

The alternative, not taken:

- **Existing SLURM cluster** — `~/ChipDreamer/datagen/orfs_run.py` already drives ORFS via
  Singularity (`~/singularity/orfs.sif`) on SLURM. Proven, but CHIA's cluster module
  (`chia/cluster/`) ships `aws_nodes.py` and `gcp_nodes.py` — **no SLURM provider**. Using
  it would mean a Ray-on-SLURM launcher or a new provider.
  CHIA has no SLURM provider, so this would mean writing a Ray-on-SLURM launcher.

## Platform choice is forced by the DRC/LVS deliverable

Expected result #2 is "at least one DRC/LVS-clean layout". ORFS has `drc` and `lvs` make
targets, but only some platforms ship decks:

| Platform | DRC deck | LVS deck |
|---|---|---|
| **sky130hd** | yes (`sky130hd.lydrc`) | yes (`sky130hd.lylvs`) |
| ihp-sg13g2 | yes | yes |
| asap7 | yes | no |
| nangate45 | yes | no |
| gf180, gf55, gt2n, sky130hs | no | no |

**sky130hd is the platform for the headline result.** nangate45 is fine for fast iteration
(it is what the smoke test used) but cannot produce an LVS-clean claim.

## What the smoke run proves about the node design

`WORK_HOME` over a bind mount works, which is the whole architecture in one flag:

```bash
docker run --rm --platform linux/amd64 -v "$OUT:/work" openroad/orfs:latest bash -lc '
  source /OpenROAD-flow-scripts/env.sh
  cd /OpenROAD-flow-scripts/flow
  make -j1 LEC_CHECK=0 WORK_HOME=/work DESIGN_CONFIG=./designs/nangate45/gcd/config.mk
'
```

- **Per-stage ODB checkpoints** land on the host: `1_synth.odb`, `2_floorplan.odb`,
  `3_place.odb`, `4_1_cts.odb`, `5_2_route.odb`, `6_final.gds`. One `WORK_HOME` per candidate
  gives isolation and resumability — the substrate for CHIA caching/bypass.
- **Per-stage JSON metrics** land beside them (`logs/<plat>/<design>/base/*.json`). These are
  the structured returns for `@ChiaFunction`; no log scraping.
- **`make` resumes**: the failed run left `3_place.odb` valid, and the rerun restarted at CTS.
  ORFS already has the incremental behavior the caching story depends on.
- **The CTS surrogate targets are already in `6_report.json`** —
  `finish__clock__skew__setup`, `finish__design__instance__area__class:clock_buffer`,
  `finish__design__instance__count__class:clock_buffer`, `finish__power__total`. SwiftCTS
  predicts clock power / wirelength / skew, so the ground-truth comparison needs no new
  extraction code.
- `-j1` is deliberate — parallel `make` races on shared ODB targets. Parallelism comes from
  many candidates, not from within one flow.

## Assets already on hand

These are not new dependencies, but they shortcut real work:

- `~/ChipDreamer/datagen/orfs_run.py` — a **validated** single-config ORFS driver: the exact
  `make` sequence, the floorplan/route double-run quirk, `-j1` (parallel make races on shared
  ODB targets), and metric extraction from `logs/*/6_report.json`. This is close to the body
  of the CHIA stage nodes.
- `~/ChipDreamer/datagen/orfs_knob_map.md` — a documented, validated ORFS knob table with
  which levers are relative vs. absolute and which have no OpenROAD equivalent. This is
  essentially the "bounded set of legal configuration changes" the agent is allowed.
- `~/SwiftCTS/SwiftCTS/` — models, caches, and eval harness for the surrogate.

## Confirmed gap

`external/chia/chia/vlsi/` contains exactly `hammer.py` and `sram_cacti/`. There is no
OpenROAD backend anywhere in the tree — the proposal's premise checks out against the
current `main`.

## Signoff: DRC works, LVS does not (sky130hd, ORFS 26Q3)

**DRC is clean.** gcd/sky130hd: 0 violations, ~8 s.

**LVS does not work as shipped**, and the investigation turned up something that
changed our own code.

### `make lvs` exits 0 when LVS fails

`platforms/sky130hd/lvs/sky130hd.lylvs` ends:

```ruby
if ! compare
  #raise "ERROR : Netlists don't match"     # <- commented out
  puts "ERROR : Netlists don't match"
end
```

So the process prints an error and exits 0. Verified: `make lvs` returned 0 on a
run whose log says the netlists do not match. Trusting the return code would
certify a layout as LVS-clean when it is not — a false pass inside the layer the
agent/verification split depends on being trustworthy.

`run_stage` therefore reads the *report* for `drc` and `lvs`
(`_read_signoff`): `<item>` count in `6_drc.lyrdb`, and the
match/no-match verdict in `6_lvs.log`. `success` reflects that, not the exit
code. DRC is the same shape — the report is written either way.

### Two further blockers, not resolved

1. **The CDL is in a dialect KLayout 0.30.7 cannot parse.**
   `platforms/sky130hd/cdl/sky130hd.cdl` uses Cadence conventions: `rI12 VGND LO
   short` (an R device with a model but no value) and `XI1 <nets> / <cell>` (the
   `/` master-name separator, read as an extra net). Both can be rewritten via
   the `CDL_FILE` make variable without touching the PDK, after which LVS runs.
2. **The comparison then fails.** gcd declares two `conb_1` tie cells that
   KLayout extracts zero devices for. `short` models a metal tie, not a
   component — giving it a value creates devices the layout lacks, and deleting
   it leaves `HI`/`LO` floating in the schematic while the layout ties them.
   Both directions mismatch; this needs deck support, not a text substitution.

### Why this looks upstream rather than ours

- No ORFS CI target runs `lvs`.
- The sky130 deck carries `NangateOpenCellLibrary` pin-equivalence rules.
- Its `raise` is suppressed, so failures are easy to miss.
- ORFS's own bundled KLayout cannot read ORFS's own CDL.

### Status

Parked. DRC works and is used. LVS is a stated deliverable (expected result #2)
and remains open — options are `ihp-sg13g2` (the other open platform with both
decks), a different KLayout, or reporting upstream. It blocks none of steps 4-7.

### Incidental: a memory figure

`chameleon` DRC peaked at **9.2 GB** and was OOM-killed under Docker Desktop's
9.7 GB ceiling. First real number for a non-trivial design, and an input to
cluster sizing — several concurrent candidates will not fit on a small box.
