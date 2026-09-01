# CHIA-OpenROAD

Surrogate-guided agentic RTL-to-GDS — adding OpenROAD/ORFS as a first-class execution and
evaluation backend inside [CHIA](https://chialoops.ai).

A³ CHIA Hackathon 2026 · Barsat Khadka · University of Southern Mississippi

- **[docs/00-objective.md](docs/00-objective.md)** — what we're building and why
- **[docs/03-setup.md](docs/03-setup.md)** — environment, clones, reproduce steps
- [docs/01-chia-overview.md](docs/01-chia-overview.md) · [docs/02-proposal-chia-openroad.md](docs/02-proposal-chia-openroad.md)

## Quick start

```bash
uv venv --python 3.10.19 .venv
VIRTUAL_ENV=.venv uv pip install -e ./external/chia
docker pull --platform linux/amd64 openroad/orfs:latest
```

See `docs/03-setup.md` for the full list of what to clone.

## The ORFS invocation this project is built around

Every stage node is a container run with a per-candidate `WORK_HOME`, which is what makes
runs isolated, resumable, and cacheable under CHIA:

```bash
docker run --rm --platform linux/amd64 -v "$OUT:/work" openroad/orfs:latest bash -lc '
  source /OpenROAD-flow-scripts/env.sh
  cd /OpenROAD-flow-scripts/flow
  make -j1 LEC_CHECK=0 WORK_HOME=/work DESIGN_CONFIG=./designs/nangate45/gcd/config.mk <KNOB>=<VALUE> ...
'
```

`LEC_CHECK=0` is required on Apple Silicon (the LEC child binary uses AVX, which Rosetta
lacks); drop it on x86. `-j1` is deliberate — parallel `make` races on shared ODB targets. Parallelism comes from
running many candidates concurrently via CHIA, not from within one flow.

## Status

Verified end-to-end on this machine: `gcd`/`nangate45` -> `6_final.gds` in 99 s under Rosetta.
sky130hd is the platform for the DRC/LVS-clean deliverable — it is the only open platform
shipping both decks. See `docs/03-setup.md`.
