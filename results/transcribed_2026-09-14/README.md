# Results transcribed from run output, 2026-09-14

GCP credits ran out mid-matrix and the project's billing was disabled, so the
VM became unreachable with these ledgers still on its disk. The numbers below
were read directly from the runs' own output before that happened and are
recorded here so the findings survive. **The raw `.db` ledgers are not in this
directory** -- they are on the instance's persistent disk and should be
recoverable once billing is restored, since suspending billing stops compute
but does not immediately delete disks.

Everything here used the final agent: measured baseline, knob inheritance on
`from`, per-knob evidence table, thread-scaled stage timeouts.

## Multi-fidelity arm (LLM + surrogate), 4 turns x 8 quick, top 2 promoted

| design | clock | default | best | delta | median | beat | quick | routed |
|---|---|---|---|---|---|---|---|---|
| cb_sha256 | 10.4 | -0.3921 | -0.1513 | +0.2408 | -0.1982 | 7/8 | 24 | 8 |
| cb_picorv32 | 4.5 | -0.2352 | -0.1874 | +0.0478 | -0.2201 | 6/8 | 24 | 8 |
| cb_ethmac | 6.5 | +0.1106 | +0.4131 | +0.3025 | +0.3749 | 8/8 | 24 | 8 |
| cb_aes | 6.5 | -0.2755 | -0.1651 | +0.1104 | -0.2308 | 5/6 | 23 | 6 |

## Agent arm (LLM, no surrogate) -- only cb_picorv32 completed

| design | default | best | delta | median | beat |
|---|---|---|---|---|---|
| cb_picorv32 | -0.2352 | -0.1526 | +0.0827 | -0.1664 | 7/8 |

On cb_picorv32 the agent without the surrogate beat the agent with it
(+0.0827 vs +0.0478, median -0.1664 vs -0.2201, 7/8 vs 6/8). That is the third
independent run on this design showing the same direction.

## Multi-fidelity cost, measured

| design | quick evals | quick time | routed | routed time | actual | if all routed | saving |
|---|---|---|---|---|---|---|---|
| cb_sha256 | 24 | 27 min | 8 | 336 min | 363 min | 1007 min | 2.8x |
| cb_picorv32 | 24 | 26 min | 8 | 189 min | 215 min | 566 min | 2.6x |

A quick evaluation at CTS averaged 1.0 min against 22.9 min for a full build on
cb_picorv32 -- about 4% -- because detailed routing is roughly three quarters
of a flow.

## cb_sha256 at the two clocks

At 10.0 ns, across 89 builds spanning five agent versions, only 3 candidates
ever beat the default and by at most 0.044 ns. At 10.4 ns the same agent beats
it in the first turn. The design sat 0.58 ns from closure with a critical path
that placement and clock-tree knobs cannot reach; nearer closure the same knobs
have leverage.

| configuration | best | median | beat |
|---|---|---|---|
| 10.0 ns, 6 turns x 4, all-full | -0.5930 | -0.6701 | 0/20 |
| 10.4 ns, 6 turns x 4, all-full | -0.2617 | -0.3877 | 13/23 |
| 10.4 ns, multi-fidelity | -0.1513 | -0.1982 | 7/8 |

## Earlier all-full runs with the evidence table (v5)

| design | best | median | beat |
|---|---|---|---|
| cb_aes | -0.0606 | -0.2244 | 3/6 |
| cb_picorv32 | -0.1482 | -0.2460 | 8/23 |
| cb_ethmac | +0.2963 | +0.2592 | 7/9 |

cb_aes and cb_ethmac were cut short by the stage-timeout bug (6 built/10 timed
out and 9/11 respectively), so their counts are lower than the 24 attempted.
