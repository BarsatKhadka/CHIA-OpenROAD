# Four-arm ablation, 2026-09-10

Four designs ported from CTS-Bench into ORFS (`cb_*`), each run through four
arms at an equal budget of 12 builds. ~22 h of compute on a 32-vCPU host.

| Arm | How | Question |
|---|---|---|
| default | shipped config, one build | the baseline every claim is measured against |
| screen | `--no-agent` | does the LLM add anything over ranking by surrogate? |
| agent | `--no-tools --no-consult` | does the agent need the surrogate? |
| full | (no flags) | the full loop |

`TABLE.txt` is the result; `ANALYSIS.txt` breaks down what each arm searched.
Regenerate either with `experiments/make_table.py` / `experiments/analyse.py`
against the `.db` ledgers here.

## Reading it

Two columns exist because the conclusion depends on which you read.
**best** is best-of-12 and rewards variance: on `cb_picorv32` the screen arm
has the best single candidate while only 2 of its 12 beat the default and its
median is worse than doing nothing. **median** and **beat** are the robust
statistics.

**preCTS** counts candidates that varied a knob read before clock-tree
synthesis. The screen arm is `0/12` throughout because the precomputed grid
covers only CTS knobs, so it is not a clean "no-LLM" control -- it differs from
the agent arms in both the LLM and the search space.

## Clock periods

Retuned from CTS-Bench's originals so each design starts near timing closure
and the loop has room to work: aes 6.5 ns, picorv32 4.5 ns, sha256 10.0 ns,
ethmac 6.5 ns. At CTS-Bench's periods, ethmac had +2.14 ns of slack and could
not have demonstrated a timing loop at all.
