# Writing a surrogate

A surrogate is a cheap model that screens configurations so ORFS runs on fewer
of them. This is the socket; the models are plugins.

## The minimum

```python
from chia_openroad.surrogate import SurrogateEvaluator

class MySkewModel(SurrogateEvaluator):
    predicts = {"clock_skew_setup": "orfs_metric"}

    def _predict(self, state, candidates):
        return [{"clock_skew_setup": my_model(c["CTS_CLUSTER_SIZE"])}
                for c in candidates]
```

That is a complete surrogate. `_predict` is the only required method — return
one plain dict per candidate, in order. The framework times, validates and
wraps them.

## Check it before wiring it in

```python
>>> from chia_openroad.surrogate import conformance_check
>>> conformance_check(MySkewModel())
```

Run this first. A mis-spelled metric name is otherwise invisible: it is never
compared against ground truth, so the model looks perfect because nothing ever
checked it.

## Declaring more, when you need it

| Attribute | Default | Meaning |
|---|---|---|
| `predicts` | `{}` | `name -> kind`; kinds are `orfs_metric`, `probability`, `score` |
| `observes_stage` | `None` | the ORFS stage you need to see; the loop branches there |
| `requires` | `()` | artifact names you need, e.g. `("def", "timing_rpt")` |
| `name` | class name | how the registry addresses you |
| `calibrate()` | no-op | adapt to this design; `run` lets you spend real ORFS runs |

`requires` is how the loop learns what to produce. Declaring `("def",)` makes it
dump DEF after placement — you do not arrange that yourself.

## Metric names are not free-form

A prediction of something ORFS measures **must** use ORFS's own name, from
`ORFS_METRICS`:

```python
>>> from chia_openroad.surrogate import ORFS_METRICS
>>> ORFS_METRICS["clock_skew_setup"]
'lower'
```

That one rule buys three things: comparison against ground truth needs no
adapter, ranking works generically because direction is known, and the loop
never has to know which model produced a number.

Predicting something ORFS does not measure is fine — declare it as
`probability` or `score`. It is then scored differently, and never silently
treated as validated.

## What you get back

The loop accumulates predicted-vs-actual as it runs:

```
surrogate: MySkewModel
  clock_skew_setup       n=  4  MAE=0.000225 (3.8% of mean)  rank_corr=+1.00
```

**Rank correlation is the number that matters.** A screen only has to order
candidates correctly; being biased high is harmless if the ordering holds. A
model with small MAE and poor rank correlation is worse than the reverse.

## Two worked examples

`chia_openroad/surrogates/feasibility.py` predicts a probability, observes no
stage, needs no artifacts, and fits on the failure log — no ORFS runs at all.
SwiftCTS observes `place`, needs DEF and a timing report, predicts three ORFS
metrics, and fits on one or two real runs. Both implement the same class
without it being widened for either.

That was the test of the design: the feasibility screen was written *after* the
interface and required no change to it.

---

# SwiftCTS integration: status

`chia_openroad/surrogates/swiftcts.py` is written and the model loads
(`saved_models/model.pkl`, needing numpy, pandas, scikit-learn, xgboost,
lightgbm). Four gaps were found between what SwiftCTS needs and what ORFS
provides. Two are solved, two are not.

## Solved

**Knob mapping.** `cd`/`cs`/`bd` map to `CTS_CLUSTER_DIAMETER`,
`CTS_CLUSTER_SIZE`, `CTS_BUF_DISTANCE`. SwiftCTS's fourth knob `mw`
(max wire length) has no ORFS equivalent — `CTS_CLK_MAX_WIRE_LENGTH` was
removed from OpenROAD, and the lever is `-distance_between_buffers`, already
covered by `CTS_BUF_DISTANCE` (`orfs_knob_map.md`). It is held constant.

**Placement DEF.** `OpenROADNode.emit_artifacts` produces it by running
`write_def` against the stage checkpoint. Verified: 146 KB for gcd/sky130hd.
This is what `requires = ("def", ...)` drives — the loop reads a surrogate's
declaration and produces what it asked for.

**Clock power and clock wirelength.** ORFS's JSONs carry only *total* power and
no wirelength breakdown, so two of SwiftCTS's three outputs looked unscoreable.
OpenROAD reports both:

- `report_power` groups power, and one group is **Clock**
- `report_wire_length -net <clock nets> -detailed_route` gives routed clock length

`OpenROADNode.measure_clock` extracts them. All three SwiftCTS predictions are
now `orfs_metric` and get checked against reality.

**The slack CSV.** Now agrees with ORFS's reported worst slack to 11% — the
residual is that our 85-path sample does not contain the single worst path ORFS
finds. `emit_artifacts` still cross-checks and withholds the file if they
diverge.

## The bug behind all three

All of the above were broken by one thing, and it is worth recording because it
would recur on any new platform.

sky130hd ships **two** liberty files, and ORFS reads only one:

```
sky130_dummy_io.lib                  <- sorts first alphabetically
sky130_fd_sc_hd__tt_025C_1v80.lib    <- the only one ORFS reads
```

Globbing the lib directory and reading everything produced a *plausible* STA
session that measured something else. Symptoms, all from this one cause:

| | glob (wrong) | ORFS's own liberty |
|---|---|---|
| worst path slack | -2243.9 ns | -1.36 ns (ORFS: -1.54) |
| clock power share | 34.0% | 10.5% (report_power: 10.4%) |
| power units | varied between runs, both labelled "Watts" | consistent |

The units point deserves emphasis: the same design reported clock power as
`8.56e-04` in one run and `8.56e-01` in another, with the column labelled
"Watts" both times, purely because Tcl's `glob` and Python's `sorted(glob)`
returned the two liberty files in different orders.

`_orfs_liberty()` now recovers the liberty files from ORFS's own logs
(`read_liberty <path>` lines) so the extraction session matches what the flow
actually did, on any platform. Two guards remain in place regardless: the slack
CSV is cross-checked against the stage's reported worst slack, and clock power
is taken as a *share* of `report_power`'s total and scaled by the watts ORFS
reports, so units cancel.

## Still open

## Not solved

**ORFS reports no clock power and no clock wirelength.** `6_report.json`
carries `finish__clock__skew__setup`, `finish__power__total` (*total*, not
clock) and `..._class:clock_buffer` area/count. So of SwiftCTS's three outputs
only skew has ground truth. Skew is declared `orfs_metric` and gets validated;
clock power and wirelength are declared `score`, meaning they rank candidates
and are never claimed to be checked. Fixing this needs a POST-CTS Tcl hook
reporting clock-net power and wirelength. Claiming an accuracy number for them
before that exists would be inventing one.

**The extracted slack CSV does not match ORFS.** SwiftCTS parses timing as
`pd.read_csv(path)["slack"]`, so the node emits one. Mechanically it works —
85 paths, well-formed. But its worst path reads **-2243.9 ns** where ORFS
reports **-1.538 ns** against a 1.1 ns clock. A hand-rolled
`read_liberty`/`read_db`/`read_sdc` session does not reproduce ORFS's STA
setup (corners, derates, link options), and gcd's `OPENROAD_HIERARCHICAL=1`
build presents two mangled clock objects.

`emit_artifacts` therefore **cross-checks the CSV against the stage's reported
worst slack and withholds it when they disagree.** A wrong-but-plausible timing
file would feed SwiftCTS's features and surface later as "the model is
inaccurate" — the error would be ours, attributed to the model.

The fix is to extract from inside ORFS's own configured STA session via
`source_step_tcl POST PLACE`, rather than building a session by hand.

## What that leaves

The adapter, the artifact mechanism, and the DEF path are done. SwiftCTS cannot
predict until the timing CSV is trustworthy, and its power/wirelength
predictions cannot be scored until ORFS reports clock-net figures. Both are
concrete, and both are ORFS-side work rather than interface work — which is
some evidence the interface is in the right place.
