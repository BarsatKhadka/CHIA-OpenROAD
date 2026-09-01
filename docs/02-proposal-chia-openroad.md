# CHIA-OpenROAD: Surrogate-Guided Agentic RTL-to-GDS

**Barsat Khadka | University of Southern Mississippi**
A³ CHIA Hackathon Proposal, 2026 — source PDF: `CHIA-OpenROAD_Proposal_Barsat_Khadka.pdf`

**Problem tracks:** Agent-in-the-loop RTL-to-GDS flows; compatibility for new platforms in CHIA

## Overview

Agentic hardware-design systems require fast feedback during exploration and trustworthy
verification before accepting a result. CHIA provides infrastructure for constructing and
executing such workflows, but it does not currently include a first-class, stage-addressable
integration with **OpenROAD-flow-scripts (ORFS)**. Its existing VLSI examples primarily use
Hammer and commercial synthesis tools.

I propose to add OpenROAD as a reusable execution and evaluation backend within CHIA. The
resulting loop will allow an agent to inspect physical-design reports, explore bounded
configuration changes, recover from failed stages, and produce a DRC/LVS-clean layout through an
open-source flow. The integration will also provide a general interface for inexpensive learned
surrogate models. During CTS exploration, I will use my previous work, **SwiftCTS**, as a
demonstration of this interface.

## Methodology

I will implement containerized CHIA nodes for preparing an ORFS design and executing synthesis,
floorplanning, placement, clock-tree synthesis, routing, and physical verification. Each node
will expose its resource requirements through `@ChiaFunction` and return structured metrics,
reports, and design checkpoints. CHIA's asynchronous execution will allow candidates to run in
parallel, while caching and bypassing will allow experiments to resume from the most recent valid
stage.

The workflow will separate **agentic decisions** from **trusted evaluation**. The agent will
receive tools for inspecting timing, congestion, clock-tree, power, area, and violation
summaries. It may select from a bounded set of legal configuration changes, launch candidate
runs, compare results, and choose the next action. Execution, report parsing, constraint
checking, DRC, and LVS will remain programmatic and inaccessible for modification by the agent.

During CTS exploration, the loop will invoke SwiftCTS to provide inexpensive estimates of which
configurations are promising. SwiftCTS predicts clock power, clock wirelength, and timing skew
using lightweight, physics-informed models. It can adapt to a new placement using one or two
OpenROAD calibration runs and evaluate 100,000 CTS configurations in under ten seconds. The agent
will use these predictions to select a small, diverse set of Pareto candidates for actual
OpenROAD CTS and routing. **OpenROAD results will remain the ground truth** used to accept or
reject configurations.

SwiftCTS will be integrated as a **replaceable CHIA evaluator** rather than being embedded
directly in the agent. This interface will allow future users to insert learned models for
timing, congestion, routing, or power estimation and extend the same surrogate-guided approach
beyond CTS to other stages of physical design, such as floorplanning, placement, and routing, as
needed.

The loop will be evaluated on a small and a medium open-source ORFS-compatible design using an
open PDK. Four configurations will be compared under similar compute budgets:

1. default ORFS
2. ORFS AutoTuner
3. the report-aware agent **without** surrogate screening
4. the complete agent **with** SwiftCTS

Measurements will include tool invocations, wall-clock time, final quality of results, and
DRC/LVS status.

## Expected results

1. Reusable, documented CHIA nodes and MCP tools for OpenROAD/ORFS.
2. An agentic RTL-to-GDS demonstration producing at least one DRC/LVS-clean layout.
3. A general interface for incorporating learned surrogate evaluators into CHIA.
4. A quantitative evaluation of the value of surrogate models within agentic physical-design loops.
5. An upstream-ready implementation and reproducible experiment configuration.

## Future work

Future work will extend the surrogate interface across additional physical-design stages and
compose multiple low-cost evaluators within a single RTL-to-GDS loop. OpenROAD results collected
by CHIA could also support continual calibration across new designs and PDKs. In the longer term,
this could enable agents to learn when a surrogate is sufficient and when an expensive
physical-design tool must be invoked.

## Cost estimate

| Item | Cost |
|---|---|
| Parallel CPU instances for OpenROAD experiments | $450 |
| Gemini model and API usage | $180 |
| Cloud storage for checkpoints and artifacts | $30 |
| Additional runs and failure contingency | $90 |
| **Total estimated maximum cost** | **$750** |
