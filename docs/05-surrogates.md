# Writing a surrogate

A surrogate is a cheap model that screens configurations so ORFS runs on fewer
of them. This is the socket; the models are plugins.

## Start here

```bash
cp chia_openroad/surrogates/template.py chia_openroad/surrogates/mymodel.py
```

Delete the parts you do not need, fill in `_predict`, then:

```python
from chia_openroad.surrogate import conformance_check
from chia_openroad.surrogates.mymodel import MyModel
conformance_check(MyModel())
```

Two seconds, and it catches the mistakes that are otherwise invisible.

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

---

# What SwiftCTS's paper changed

Reading arXiv:2606.11348 rather than the code alone changed four things.

**1. It was trained on Sky130 + OpenROAD.** 5,400 CTS runs over 540 placements
across AES, PicoRV32, SHA-256 and ETHMAC (CTS-Bench). ORFS ships
`sky130hd/aes`, so aes is a design the model knows and the right first target —
`gcd` is neither in its training set nor large enough to have a clock tree
worth optimising.

**2. The knob ranges it was fitted over are narrower than ORFS accepts**
(Table II):

| knob | fitted range | our old nominal |
|---|---|---|
| Cluster Size | 12–30 sinks | 10–40 |
| Sink Max Diameter | 35–70 um | 10–100 |
| Buffer Distance | 70–150 um | 30–200 |
| Max Wire Length | 130–280 um | (no ORFS knob) |

Asking a learned model about a configuration outside its training domain is
extrapolation, and the model cannot tell you it is happening. So the interface
gained an optional `domain` declaration and an `in_domain()` check, and the
default knob policy was tightened to those bounds. This is general, not
SwiftCTS-specific: every fitted surrogate has a domain.

**3. K-shot calibration is not optional for skew.** Table III's footnote: *"K=0
yields relative z-scores; absolute ns extraction requires K>=1 to anchor the
distribution."* Our adapter already omitted `clock_skew_setup` when
`skew_ns is None`, which turns out to be exactly right — at K=0 there is no
absolute skew to report. The paper's numbers for going from K=0 to K=1: power
error 24.5% -> 3.3%, wirelength 56.6% -> 0.6%.

The mechanism is a multiplicative offset, not retraining:

    k_cal = exp( (1/K) * sum log(y_true / y_pred) )

`_k_shot_calibrate` implements it against real ORFS runs through the loop's own
`run` callable, so those runs land in the same ledger and count against the same
budget.

**4. Max Wire Length is held at 200 um**, the middle of its fitted range, rather
than an arbitrary constant — `CTS_CLK_MAX_WIRE_LENGTH` has no ORFS equivalent,
so the model must see *some* value and it should be an in-domain one.

---

# Building a trustworthy STA session by hand

Three separate bugs stood between "the extraction runs" and "the extraction is
correct". All three produced plausible output, and none announced itself.

**1. The wrong liberty.** sky130hd ships two `.lib` files; ORFS reads one.
Globbing read both, and which came first changed the reported power *units*
between runs (`8.56e-04` vs `8.56e-01`, both labelled "Watts").
`_orfs_liberty()` now recovers the set from ORFS's own `read_liberty` log lines.

**2. Unsorted path search.** `find_timing_paths -group_count N` returns paths
per group in arbitrary order, so a truncated set can miss the critical path.
`-sort_by_slack` is not optional.

**3. No parasitics — the worst of the three.** A session with liberty, ODB and
SDC runs fine and reports confident numbers. But ORFS's own stage scripts also
source the platform RC file, estimate parasitics, and propagate clocks
(`detail_place.tcl`, `cts.tcl`). Without parasitics the nets carry no RC, wire
delay is zero, and every path looks fast. On aes the extracted worst slack came
out **+0.51 ns against ORFS's -0.88 ns** — the wrong *sign*.

`_timing_setup()` now adds all three:

```tcl
source <platform>/setRC.tcl
estimate_parasitics -placement      # or -global_routing once routed
set_propagated_clock [all_clocks]
```

The lesson worth keeping: reproducing an EDA tool's measurement means
reproducing its whole setup, not just loading the same database. The guard
that catches this — cross-checking extracted slack against the stage's own
reported worst slack — earned its place three times over.

## Artifacts must cross the worker boundary

`emit_artifacts` runs in the worker container; a driver-side surrogate cannot
read a path that exists only there. The loop fetches them to the head with
`collect` before building `DesignState`. On aes that is a 4.9 MB DEF, which is
why `collect`'s default 4 MB cap has to be raised deliberately for this.

---

# First real result: SwiftCTS on aes/sky130hd

K=1 calibration, 512 predicted configurations, 4 built and scored.

```
K=1 calibration        4171 s   (one real aes flow)
predict 512 configs       1.7 s  (3.32 ms each)
                                 60,277x faster than building them
```

## Scorecard

```
clock_wirelength_um   n=4  MAE=289.4   (2.6% of mean)   rank_corr=+0.40
clock_power_w         n=4  MAE=0.00188 (16.1% of mean)  rank_corr=+0.00
clock_skew_setup      not produced
```

**Wirelength works.** Per-candidate error 0.7%, 0.7%, 2.2%, 6.3%; the paper
reports 0.6% at K=1 on its base designs, so 2.6% mean on a fresh ORFS placement
is consistent.

**Power carries no signal here, and the scorecard is what showed it.** The
prediction was *byte-identical* across all four candidates — one distinct value
against four distinct wirelength values — so `rank_corr` is exactly 0.00. Two
causes, both real:

1. **No SAIF.** ORFS runs no gate-level simulation, so every switching-activity
   feature falls back to a default. Power is dominated by activity.
2. **The power head sees only one knob.** `_build_pw_features(d, s, t, f_ghz,
   sa, cd)` takes cluster *diameter* and not cluster size, max wire or buffer
   distance — and it was insensitive to diameter over 35-65 um on this design.

This is exactly why the scorecard reports rank correlation and not just error.
A 16% MAE looks survivable; `rank_corr=0.00` says the model cannot order
candidates at all, which for a screen is the only thing that matters. Reporting
MAE alone would have hidden it.

**Skew was not produced at all.** `skew_ns` is None, so the adapter omits the
key rather than reporting a z-score in nanosecond units. Absolute skew needs a
per-placement anchor (paper, Table III footnote: *"K=0 yields relative
z-scores; absolute ns extraction requires K>=1"*) — but the shipped API exposes
only `calibrate_power` and `calibrate_wl`. There is **no public skew
calibration hook**, so a fresh placement cannot get absolute skew through the
supported interface.

## What this says about Step 8

For the four-arm evaluation, SwiftCTS is currently a **wirelength** screen on
ORFS placements, not a three-objective one. Making power useful needs a SAIF —
which means a gate-level simulation ORFS does not run. Making skew useful needs
an anchoring entry point SwiftCTS does not expose.

Both are tractable and neither is an interface problem: the socket carried a
model whose outputs disagreed with reality, scored it, and said which part was
wrong. That is what it was built to do.

## Caveat on this run

Candidate selection was flawed: evenly-spaced indices into the grid held
cluster size and buffer distance at their minimums, so only diameter varied.
Fixed (greedy max-min in normalised knob space), but these four numbers
describe one axis. Rerunning with diverse candidates is the first thing to do
when checkpoint branching makes it cheap.
