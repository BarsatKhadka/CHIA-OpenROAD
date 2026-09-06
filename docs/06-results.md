# Surrogate-guided agentic RTL-to-GDS: first full run

aes on sky130hd, Gemini 2.5 Flash, 3 iterations, 9 candidates, 0 failures.

## The loop

One shared placement is built. SwiftCTS is calibrated against it with a single
reference run (K=1). A fresh agent is called each iteration with the ledger
rendered into its prompt; it proposes configurations; Python builds them in
parallel, branching from the shared placement; results are persisted and become
the next iteration's memory. The agent is never inside a build.

## Result

| # | BUF_DIST | DIAMETER | SIZE | worst_slack | skew | power | area |
|---|---|---|---|---|---|---|---|
| 7 | 110 | 45 | 30 | -0.4316 | 0.0941 | 0.5709 | 1.285e5 |
| 8 | 110 | 63 | 20 | -0.3882 | 0.0957 | 0.5680 | 1.274e5 |
| 9 | 110 | 35 | 12 | -0.1574 | 0.0968 | 0.5716 | 1.294e5 |
| 10 | 70 | 35 | 12 | -0.1020 | 0.0824 | 0.5736 | 1.296e5 |
| 11 | 130 | 45 | 20 | -0.2129 | 0.0687 | 0.5663 | 1.277e5 |
| 12 | 70 | 63 | 20 | -0.1337 | 0.0716 | 0.5651 | 1.269e5 |
| 13 | 90 | 35 | 12 | -0.2610 | 0.0795 | 0.5730 | 1.296e5 |
| **14** | **70** | **45** | **20** | **-0.0628** | **0.0665** | **0.5666** | 1.278e5 |
| 15 | 150 | 35 | 12 | -0.1031 | 0.0796 | 0.5769 | 1.300e5 |

Best-per-iteration: **-0.1574 -> -0.1020 -> -0.0628**, a 60% improvement in
worst slack. Against the region the surrogate ranked first (-0.4316), the final
configuration is roughly 7x better.

`#14` is not a negotiated trade-off: it beats the previous best on slack, skew
**and** power **and** area simultaneously.

Cost: 18 ORFS invocations, 95,903 s of cumulative candidate wall clock, 6
proposals rejected by the knob policy before running.

## Two findings

### The surrogate's ranking pointed away from the objective

SwiftCTS ranked `DIAMETER=63, SIZE=30` first by predicted clock wirelength.
That region measured **-0.43** slack — the worst of everything built. The best
configuration sat well down the ranking.

Its wirelength predictions are accurate (2.9%, 3.1% against measurement). The
problem is not accuracy, it is that **on this design clock wirelength does not
track timing**, so an accurate wirelength model is a misleading guide for a
timing objective.

### The agent found the strongest lever the surrogate cannot see

`CTS_BUF_DISTANCE` is the knob SwiftCTS predicts identically for every value —
it does not move its objective. Measured, holding the other two knobs fixed:

    BUF_DISTANCE = 70   ->  -0.1020
    BUF_DISTANCE = 90   ->  -0.2610
    BUF_DISTANCE = 150  ->  -0.1031

It is a strong lever with a sharp optimum near 70. The agent reached it by
reasoning rather than by ranking, and stated the mechanism before testing it:

> "I will reduce CTS_BUF_DISTANCE to its minimum (70), which means more buffers
> and potentially better skew/slack, but might increase power/wirelength."

Slack improved 35%, skew improved, power rose slightly — all three as predicted.

## What this implies for the design

The two mechanisms fail differently, and that is the useful part:

| | surrogate | agent |
|---|---|---|
| throughput | 216 configs in 0.73 s | 3 configs per ~20 s |
| accuracy where fitted | 2.9% on wirelength | - |
| outside its feature space | silently flat | reasons about mechanism |
| objective alignment | its own, which may not be yours | the one you asked for |

A fitted model interpolates what it was trained on; a reasoner can act outside
it. So a screen should **prune and inform** rather than rank-and-be-obeyed:
filter for feasibility and cost across thousands of points, and hand the agent
the model's blind spots as evidence — "this model ignores CTS_BUF_DISTANCE, and
its top 40 entries tie" is more useful to a reasoner than a sorted list.

That is a sharper claim than the proposal's original framing, and it is what the
measurements support.

## Caveats

* One design, one PDK, three iterations. Nothing here establishes generality.
* `#13` (BUF=90) is out of line with 70 and 150, which suggests routing-driven
  variance of the same order as some of the differences being compared. Repeat
  runs would be needed before treating small gaps as real.
* No arm comparison yet: this shows the loop optimises, not that the surrogate
  helped it. On this evidence the screen may be a net negative for a timing
  objective, which is exactly what the four-arm evaluation should measure.
