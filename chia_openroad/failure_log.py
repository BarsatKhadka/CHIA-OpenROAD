"""A record of configurations that did not build, and why.

An agent proposing physical-design knobs has no calibration for a particular
design and PDK. ``CORE_UTILIZATION=50`` is an ordinary number in general ASIC
terms; on gcd/sky130hd it is infeasible, because CTS buffer insertion pushes
utilization past 100% and detailed placement cannot legalize (``DPL-0038``).
The agent cannot know that in advance — but it only has to learn it once.

This keeps failures so they can be:

  * **fed back** — :meth:`FailureLog.hints` renders them as prompt lines, so
    the agent stops proposing neighbours of a known-bad point;
  * **not repeated** — :meth:`FailureLog.known_bad` answers an exact repeat
    without spending a run at all.

Lives on the driver, not the worker: failures accumulate across every
candidate and every worker, and it is the agent's context they feed.

Append-only JSONL, one record per line, so a crashed run loses at most the
record it was writing.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict

from chia_openroad.openroad import OrfsFailure, OrfsStageResult


def knob_key(knobs: dict) -> str:
    """Canonical form of a knob set, so equal configurations hash equal.

    Values are stringified the same way ``run_stage`` stringifies them before
    they reach make, so ``{"PLACE_DENSITY": 0.60}`` and ``"0.6"`` are the same
    configuration — because they produce the same command line.
    """
    return json.dumps({str(k): str(v) for k, v in sorted((knobs or {}).items())},
                      sort_keys=True)


class FailureLog:
    """Append-only store of failed configurations for one design.

    ::

        log = FailureLog("experiments/gcd_sky130hd.failures.jsonl")

        prior = log.known_bad(knobs)
        if prior:
            ...                       # don't spend a run; we've seen this
        else:
            results = run_flow(run, work_home, design_config, knobs)
            log.record_all(results)

        prompt += "\\n".join(log.hints())
    """

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._by_knobs: dict[str, dict] = {}
        self._load()

    # Same reasoning as CandidateStore.__getstate__: a threading.Lock cannot
    # be pickled, and a ChiaTool is pickled to reach its Ray actor.
    def __getstate__(self):
        return {"path": self.path}

    def __setstate__(self, state):
        self.__init__(state["path"])

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a torn final line from a killed run
                self._by_knobs[record["knob_key"]] = record

    def record(self, result: OrfsStageResult) -> bool:
        """Store one failed stage. Returns False for a success (nothing stored).

        A failure with no recognisable error is still worth recording — that a
        configuration does not build is useful even when the reason is unclear.
        """
        if result.success:
            return False
        failure = result.failure or OrfsFailure(
            stage=result.stage, message="failed with no recognisable error",
            knobs=result.knobs)
        record = {
            "knob_key": knob_key(result.knobs),
            "knobs": result.knobs,
            "design": result.design,
            "platform": result.platform,
            "returncode": result.returncode,
            "elapsed_s": round(result.elapsed_s, 1),
            "failure": asdict(failure),
        }
        with self._lock:
            self._by_knobs[record["knob_key"]] = record
            with open(self.path, "a") as f:
                f.write(json.dumps(record) + "\n")
        return True

    def record_all(self, results) -> int:
        """Record every failure in a :func:`run_flow` result list."""
        return sum(1 for r in results if self.record(r))

    def known_bad(self, knobs: dict) -> dict | None:
        """The stored record for this exact configuration, if we've failed it.

        Exact match only. Nothing here claims that a *neighbouring* value fails
        — inferring that is the agent's job, which is what `hints` is for.
        """
        return self._by_knobs.get(knob_key(knobs))

    def hints(self, limit: int = 10) -> list[str]:
        """Recent failures as one-line prompt fragments, newest last."""
        records = list(self._by_knobs.values())[-limit:]
        return [OrfsFailure(**r["failure"]).as_hint() for r in records]

    def __len__(self) -> int:
        return len(self._by_knobs)
