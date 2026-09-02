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
