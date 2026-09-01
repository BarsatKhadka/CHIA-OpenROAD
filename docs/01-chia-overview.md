# CHIA — Source Notes

Compiled from https://chialoops.ai, https://github.com/ucb-bar/chia, and https://docs.chialoops.ai.

## What CHIA is

**CHIA** — *Co-designing Hardware and software with Intelligent Agents* — is an open-source
framework from UC Berkeley (ucb-bar) for **principled, agentic AI-driven hardware/software
co-design research**. Tagline on the site: *"An open framework for designing and deploying
custom AI-driven HW/SW co-design flows, fast."*

The problem it targets: individual steps of co-design can be accelerated by AI, but existing
research has been stuck on small, isolated examples because **assembling complex experiments is
too hard**. CHIA lets users express a *whole* co-design workflow as a graph, using the tools they
already have, and executes it on a feature-rich distributed runtime (Ray).

- License: BSD-3-Clause
- Python 3.10.19 (matches the Docker images); `pip install -e /path/to/chia`
- Paper: https://openreview.net/pdf?id=lLxEUReWHG · arXiv:2606.27350 (local copy: `chia-paper.pdf`)
- Docs: https://docs.chialoops.ai · Site: https://chialoops.ai · Repo: https://github.com/ucb-bar/chia
- Lead developers / PI: Angela Cui, Ferran Hermida-Rivera, Jack Toubes, Sagar Karandikar

## Core abstractions

| Concept | Role |
|---|---|
| **CHIA Loop** | The iterative design cycle an agent runs in; composed, deployed, and shared as a unit |
| **`@ChiaFunction`** | The computational unit of a workflow step — distributed execution on Ray workers, declared resource requirements, setup/cleanup hooks, custom node definitions, `chia_actor` wrapping |
| **`ChiaTool`** | MCP (Model Context Protocol) integration layer exposing external tools/services to the agent; lifecycle management, bridging tools back to nodes, resource placement, LLM backend connection |
| **CHIA cluster** | Coordinates computation across cloud / on-prem / hybrid infrastructure; node types, Tailscale networking, command ordering, multiple heads per machine |
| **Runtime** | Caching + bypass for result reuse, `chia_kv_store`, `llm_call`, colocated execution, PID registry |
| **Caching & bypass** | Tag-based management of intermediate results — "populate, then replay" to resume from the last valid stage |
| **Profiling** | Execution metrics, timing, custom metadata, visualization |

Design emphases: **fault tolerance & reproducibility** (checkpointing), and **infrastructure
abstraction** so the same flow runs on cloud, on-prem, or hybrid.

Entry point in practice: a single `chia job submit -- my-flow.py` can drive agentic
implementation, debugging, simulation, and PPA analysis end-to-end.

## Ecosystem it composes

- **Agents / models**: Claude Code, OpenAI Codex, GitHub Copilot, Google Antigravity, AlphaEvolve, OpenEvolve, AdaEvolve, SkyDiscover
- **RTL / SoC**: Chisel, Chipyard, CIRCT
- **Simulation**: gem5, ChampSim, Verilator, FireSim, Spike
- **Physical design / CAD**: Hammer (ASIC & FPGA CAD, incl. commercial tools)
- **Backend**: Ray

> "Don't see your tool here? Adding a new CHIA node is easy!"

## Published case studies (arXiv paper §5)

1. **§5.4 — Agentic architectural discovery.** Evolutionary coding-agent discovery flows (ArchAgent), now portable across microarchitectural simulators and down into RTL + ASIC EDA feedback.
2. **§5.2 — Automatic ISA-extension implementation.** LLM implements RISC-V Bitmanip, Crypto, Zicond in 4-wide MegaBOOM inside a full Chipyard SoC; ~5.6% and ~3.5% SPEC CPU2006 speedups (25.5T instructions), up to 10× on OpenSSL crypto, no timing regressions, modest area in Sky130 and a commercial 16nm PDK. **CHIA isolates the agent from golden-reference verification** (Spike co-sim, riscv-dv).
3. **§5.1 — Simulator↔RTL alignment.** Agent edits gem5 core microarchitecture (not just parameters) to match RTL; 202 iterations → ~3% cycle-count error vs. a 2-wide MediumBOOM, <7% on a hidden Embench holdout.
4. **§5.3 — IPC-aware critical-path optimization.** Agent reads gate-level timing reports (Sky130) and edits MegaBOOM; CHIA re-builds/re-synthesizes/re-simulates **with no AI involved** to confirm. <5 active days → >2× frequency for 3.3% IPC loss ≈ **1.97× iron-law speedup**, validated over 25T SPEC instructions.
5. **§5.5 — Agentic GitHub issues in CIRCT.** Triage → confirm real bug → reproduce → patch, then hand back for a no-AI full regression run before human review. 16 issues in parallel in <45 min; 5 real fixes, 3 PRs merged upstream.

**The recurring pattern:** the agent proposes and edits; CHIA runs the trusted, AI-free
evaluation that decides whether the result is accepted.

## Context: the hackathon

CHIA is running a hackathon leading up to the **A³ Workshop at MICRO 2026** — free compute and
LLM models, cash prizes. One-page proposals were due **Aug 25, 2026**. This repository's work is
the proposal in `02-proposal-chia-openroad.md`.
