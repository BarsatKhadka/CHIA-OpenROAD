# Objective

*Derived from the CHIA framework (chialoops.ai, ucb-bar/chia, docs.chialoops.ai) and the
CHIA-OpenROAD hackathon proposal.*

## The one-sentence objective

**Make OpenROAD a first-class, stage-addressable backend inside CHIA, and use it to build an
agent-in-the-loop RTL-to-GDS flow in which an LLM agent explores bounded physical-design
configuration changes, cheap learned surrogates screen the search space, and OpenROAD itself
remains the only authority that accepts or rejects a result — ending in a DRC/LVS-clean layout.**

## Why this is the objective (reading it out of the sources)

CHIA's whole thesis is that AI can accelerate individual co-design steps, but the field is stuck
on isolated toy examples because *assembling* the full experiment is too hard. Its answer is:
express the workflow as a graph of `@ChiaFunction` nodes, expose tools to the agent through
`ChiaTool`/MCP, and run it on a Ray-backed runtime with caching, bypass, checkpointing, and
parallel execution.

Across every published CHIA case study the same discipline appears: **the agent proposes; a
trusted, AI-free path evaluates.** The ISA-extension loop walls the agent off from Spike
co-simulation and riscv-dv. The critical-path loop re-synthesizes and re-simulates with no AI
involved. The CIRCT loop hands control back for a clean regression run before a human sees the
patch. That separation is what makes the results citable rather than anecdotal.

The gap: CHIA's VLSI story currently runs through **Hammer and commercial tools**. There is no
open, reproducible, stage-by-stage physical-design backend. That closes the door on anyone
without commercial CAD licenses and blocks fully reproducible RTL-to-GDS research.

So the objective decomposes into three things, in order:

### 1. Platform compatibility — OpenROAD as a CHIA backend
Containerized CHIA nodes for ORFS: design prep, synthesis, floorplan, placement, CTS, routing,
physical verification. Each declares its resources via `@ChiaFunction` and returns **structured
metrics, reports, and checkpoints** — not log scrapings. Async execution runs candidates in
parallel; caching/bypass resumes a broken experiment from the last valid stage.

### 2. The agentic loop — with a hard trust boundary
The agent gets MCP tools to *read* timing, congestion, clock-tree, power, area, and violation
summaries, and to pick from a **bounded set of legal configuration changes**, launch candidates,
compare, and decide the next move. Execution, report parsing, constraint checking, DRC, and LVS
stay programmatic and **out of the agent's reach**. This is CHIA's own pattern applied to
physical design.

### 3. The general contribution — a surrogate-evaluator interface
The reusable idea is not SwiftCTS; it is the **socket** SwiftCTS plugs into. A replaceable CHIA
evaluator that returns cheap predictions (SwiftCTS: clock power, clock wirelength, skew — 100k
CTS configurations in <10 s, calibrated with one or two real OpenROAD runs) so the agent spends
its expensive tool invocations only on a small, diverse Pareto set. Anyone can later drop in a
learned model for timing, congestion, routing, or power, at floorplanning, placement, or routing
stages, without touching the agent.

## What "done" looks like

- [ ] Reusable, documented CHIA nodes + MCP tools for OpenROAD/ORFS
- [ ] An agentic RTL-to-GDS demo producing **at least one DRC/LVS-clean layout**
- [ ] A general, documented interface for learned surrogate evaluators in CHIA
- [ ] A quantitative answer to *"do surrogates actually pay for themselves in an agentic PD loop?"*
- [ ] Upstream-ready code and a reproducible experiment configuration

## How it gets judged

One small and one medium open-source ORFS-compatible design on an open PDK. Four arms, equal
compute budget:

| Arm | What it tests |
|---|---|
| Default ORFS | Baseline |
| ORFS AutoTuner | Existing automated tuning |
| Report-aware agent, **no** surrogate | Value of the agent alone |
| Full agent **+ SwiftCTS** | Value of surrogate screening on top |

Metrics: tool invocations, wall-clock time, final QoR, DRC/LVS status.

## Constraints to hold onto

- **Open only.** Open-source flow, open PDK — reproducibility is the point.
- **Ground truth is OpenROAD.** Surrogates steer the search; they never decide it.
- **The agent never touches verification.** Non-negotiable, and inherited from CHIA's design.
- **Surrogates are pluggable, not baked in.** Otherwise contribution #3 doesn't exist.
- **Budget ≈ $750** and a hackathon-scale timeline — scope accordingly.

## Longer horizon

Compose several cheap evaluators in one RTL-to-GDS loop; use OpenROAD results CHIA collects to
continually recalibrate surrogates across new designs and PDKs; ultimately let the agent *learn
when a surrogate suffices and when the expensive tool must be called.*
