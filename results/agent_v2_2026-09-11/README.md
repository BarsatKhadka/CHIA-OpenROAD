# Agent v2: gains over turns, 2026-09-11

Same four designs, same 12-build budget, same model as
`results/matrix_2026-09-10`. Only the prompt and the rendered state changed.

## What was wrong

Across three turns on four designs the v1 per-turn best was flat or
oscillating in seven of eight runs, with turn 3 frequently worse than turn 2.
The agent was sampling, not searching. Two causes, both in what it was told:

1. **The prompt forbade refinement.** It required every candidate be "genuinely
   different from each other and from everything already built", which makes
   refining a good configuration illegal and restarts the search every turn.
2. **The agent never saw the baseline.** The ledger held only candidates the
   loop built, never the default it is meant to beat, so a run where all 12
   candidates lost looked identical to one where all 12 won. On cb_picorv32 all
   12 were below the default and nothing said so.

## What changed

Turn 1 still explores widely. Every later turn spends at least half its budget
refining the current best by one or two knobs, so a change can be attributed.
The loop now measures the default itself -- continuing from the shared
placement, so it costs the post-place stages rather than a full flow -- and
renders it with each candidate's delta against it.

## Result

| design | best v1 -> v2 | median v1 -> v2 | progression v1 -> v2 |
|---|---|---|---|
| cb_aes | -0.1618 -> -0.1173 | -0.1987 -> -0.1940 | improving -> mixed |
| cb_picorv32 | -0.2878 -> -0.2351 | -0.3723 -> -0.2769 | worsening -> improving |
| cb_sha256 | -0.5342 -> -0.5342 | -0.7125 -> -0.6716 | mixed -> mixed |

Best improved or tied everywhere; median improved everywhere. cb_picorv32 is
the clean case: a search that got worse every turn now gets better every turn.
cb_aes is "mixed" only because v2 found its optimum in turn 1 and plateaued,
which is a better outcome rather than a worse search.

## Three bugs found while doing this

* The timing-agreement check compared a placement-stage extraction against a
  post-route number, because it took whichever metrics JSON sorted first and
  the new baseline advances the work directory to `finish`.
* Corrected once, it then compared against *global* placement rather than
  *detailed* placement: a stage writes one JSON per sub-step and only the last
  matches the checkpoint being read. This cost cb_aes two whole runs.
* A 429 from the model provider ended a run outright, since run_iterations
  breaks on any model-call failure. cb_sha256 lost turn 3 that way and the
  two-turn run looked like an agent that had stopped improving. Transient
  errors now back off and retry.

Progression is measured on the **per-turn** best. An earlier version of the
measurement used the cumulative best, which is a running maximum and therefore
non-decreasing by construction -- it labelled a worsening run "monotonic".
