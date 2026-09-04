"""Copy this file to add your own surrogate. Delete what you don't need.

    cp chia_openroad/surrogates/template.py chia_openroad/surrogates/mymodel.py

Then:

    from chia_openroad.surrogate import conformance_check
    from chia_openroad.surrogates.mymodel import MyModel
    conformance_check(MyModel())

That check runs in seconds and catches the mistakes that are otherwise
invisible — chiefly a mis-spelled metric name, which is never compared against
ground truth, so the model looks perfect because nothing ever checked it.
"""

from chia_openroad.surrogate import DesignState, SurrogateEvaluator, register


@register("mymodel")                       # how a loop selects you by config string
class MyModel(SurrogateEvaluator):
    """One line on what this predicts and roughly how."""

    # ---- required -------------------------------------------------------
    #: name -> kind. Kinds: "orfs_metric" | "probability" | "score".
    #: An "orfs_metric" name MUST be a key of chia_openroad.surrogate.ORFS_METRICS
    #: (>>> from chia_openroad.surrogate import ORFS_METRICS) — that is what
    #: lets the scorecard compare your prediction against a real ORFS run with
    #: no adapter in between. Predicting something ORFS does not measure is
    #: fine: declare it "score" or "probability" and it simply is not scored.
    predicts = {"clock_skew_setup": "orfs_metric"}

    # ---- optional: delete any you don't use -----------------------------
    #: The ORFS stage you need to observe. The loop branches the flow here and
    #: hands you a DesignState from it. None if you need no design state.
    observes_stage = "place"

    #: Artifact names you need in DesignState.artifacts. The loop reads this and
    #: produces them — declaring ("def",) is what makes it dump DEF after
    #: placement. You do not arrange that yourself.
    #: Available: "def", "timing_rpt", "odb", "clock_period", "gds", "final_def".
    requires = ("def",)

    #: Knob ranges you were fitted over, {knob: (low, high)}. The loop
    #: intersects this with the legal knob ranges so you are never asked about a
    #: configuration outside your training domain — extrapolation you would have
    #: no way to flag.
    domain = {"CTS_CLUSTER_SIZE": (12.0, 30.0)}

    def calibrate(self, state: DesignState, run=None, budget_runs: int = 0) -> None:
        """Adapt to this design. Delete if there is nothing to do.

        `run(stage, work_home=..., design_config=..., knobs=...)` is the loop's
        own ORFS callable — use it if you need real reference runs, and they
        land in the same ledger and count against the same budget. `budget_runs`
        is how many you may spend.

        Ground truth arrives on the result's `.summary`, keyed by ORFS metric
        names.
        """
        print(state.describe())            # what is actually available here
        return None

    # ---- the one method you must write ----------------------------------
    def _predict(self, state: DesignState, candidates: list[dict]) -> list[dict]:
        """One {metric: value} dict per candidate, in the same order.

        Plain dicts — the framework times, validates and wraps them. Raising is
        fine; returning a wrong-length list or an undeclared key is caught.
        """
        return [{"clock_skew_setup": 0.004 + 0.0001 * c.get("CTS_CLUSTER_SIZE", 20)}
                for c in candidates]
