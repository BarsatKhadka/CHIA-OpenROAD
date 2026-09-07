"""Every configuration tried, what happened to it, and what it cost.

Three things need this, and they need the same records:

* **The agent** — to compare what it has already tried, and to avoid
  re-proposing something already known to fail.
* **The evaluation** — one of the four measures is *tool invocations*. That
  number has to come from a ledger, not from a guess.
* **Us** — a post-mortem on a loop that went nowhere is only possible if every
  proposal was written down, including the rejected ones.

Rejections are recorded too, and that is deliberate. A proposal the policy
turned down still consumed an agent turn; counting only the runs that reached
ORFS would flatter an agent that spends its budget proposing illegal
configurations.

SQLite because ``timing_opt/db.py`` does the same for its branch tree: it
survives a crash, it is queryable after the fact, and it needs no service.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass

from chia_openroad.failure_log import knob_key

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    arm           TEXT NOT NULL,      -- which evaluation arm proposed it
    design        TEXT,
    platform      TEXT,
    parent_id     INTEGER,            -- which candidate this was derived from
    knobs         TEXT NOT NULL,      -- json
    knob_key      TEXT NOT NULL,      -- canonical form, for dedup
    status        TEXT NOT NULL,      -- proposed|rejected|running|built|failed
    reject_reason TEXT,
    stage_reached TEXT,
    elapsed_s     REAL DEFAULT 0,
    tool_runs     INTEGER DEFAULT 0,  -- ORFS invocations this candidate cost
    metrics       TEXT,               -- json summary
    failure       TEXT,               -- json
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_knob_key ON candidates(knob_key);
CREATE INDEX IF NOT EXISTS idx_status   ON candidates(status);
"""


@dataclass
class Candidate:
    id: int
    arm: str
    knobs: dict
    status: str
    stage_reached: str | None = None
    elapsed_s: float = 0.0
    tool_runs: int = 0
    metrics: dict | None = None
    failure: dict | None = None
    reject_reason: str | None = None
    #: The candidate this was derived from — None means it started from the
    #: shared placement. Makes the exploration a visible tree rather than a
    #: flat list, so the agent can say "vary this, starting from #14".
    parent_id: int | None = None

    def one_line(self) -> str:
        knobs = ", ".join(f"{k}={v}" for k, v in sorted(self.knobs.items())) or "(defaults)"
        if self.status == "built":
            m = self.metrics or {}
            bits = [f"{k}={m[k]:.4g}" for k in
                    ("worst_slack", "power_total", "instance_area", "clock_skew_setup")
                    if isinstance(m.get(k), (int, float))]
            return f"#{self.id} {knobs} -> built ({', '.join(bits)})"
        if self.status == "rejected":
            return f"#{self.id} {knobs} -> rejected: {self.reject_reason}"
        if self.status == "failed":
            reason = (self.failure or {}).get("message", "unknown")
            step = (self.failure or {}).get("step") or self.stage_reached
            return f"#{self.id} {knobs} -> FAILED at {step}: {reason}"
        return f"#{self.id} {knobs} -> {self.status}"


class CandidateStore:
    def __init__(self, path: str = "candidates.db"):
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # A ChiaTool is pickled to reach its Ray actor, and a sqlite3.Connection
    # and a threading.Lock are both unpicklable. Carry the path and reopen on
    # the far side. NOTE: this assumes the path is reachable there — true on a
    # single machine, and something a multi-node cluster would need to solve
    # with a shared filesystem or a database node.
    def __getstate__(self):
        return {"path": self.path}

    def __setstate__(self, state):
        self.__init__(state["path"])

    # -- writing ----------------------------------------------------------
    def propose(self, knobs: dict, *, arm: str = "agent", design: str = "",
                platform: str = "", parent_id: int | None = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO candidates (arm, design, platform, knobs, knob_key,"
                " status, parent_id) VALUES (?,?,?,?,?,'proposed',?)",
                (arm, design, platform, json.dumps(knobs, sort_keys=True),
                 knob_key(knobs), parent_id))
            self._conn.commit()
            return cur.lastrowid

    def reject(self, cid: int, reason: str) -> None:
        self._set(cid, status="rejected", reject_reason=reason)

    def record(self, cid: int, results) -> None:
        """Record the outcome of a :func:`chia_openroad.openroad.run_flow` list.

        ``tool_runs`` counts the ORFS invocations this candidate actually cost —
        two when a gated flow ran both stages, one when the gate stopped it.
        That is the number the evaluation reports, so it is counted here rather
        than inferred later.
        """
        from dataclasses import asdict
        results = list(results)
        last = results[-1]
        self._set(
            cid,
            status="built" if last.success else "failed",
            stage_reached=last.stage,
            elapsed_s=round(sum(r.elapsed_s for r in results), 1),
            tool_runs=len(results),
            metrics=json.dumps(last.summary or {}),
            failure=json.dumps(asdict(last.failure)) if last.failure else None,
            design=last.design, platform=last.platform,
        )

    def _set(self, cid: int, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(f"UPDATE candidates SET {cols} WHERE id=?",
                               (*fields.values(), cid))
            self._conn.commit()

    # -- reading ----------------------------------------------------------
    def _row(self, r) -> Candidate:
        return Candidate(
            id=r["id"], arm=r["arm"], knobs=json.loads(r["knobs"]), status=r["status"],
            stage_reached=r["stage_reached"], elapsed_s=r["elapsed_s"] or 0.0,
            tool_runs=r["tool_runs"] or 0,
            metrics=json.loads(r["metrics"]) if r["metrics"] else None,
            failure=json.loads(r["failure"]) if r["failure"] else None,
            reject_reason=r["reject_reason"],
            parent_id=r["parent_id"] if "parent_id" in r.keys() else None)

    def get(self, cid: int) -> Candidate | None:
        r = self._conn.execute("SELECT * FROM candidates WHERE id=?", (cid,)).fetchone()
        return self._row(r) if r else None

    def seen(self, knobs: dict) -> Candidate | None:
        """A previous candidate with exactly these knobs, newest first."""
        r = self._conn.execute(
            "SELECT * FROM candidates WHERE knob_key=? AND status IN ('built','failed')"
            " ORDER BY id DESC LIMIT 1", (knob_key(knobs),)).fetchone()
        return self._row(r) if r else None

    def list(self, status: str | None = None, arm: str | None = None,
             limit: int = 50) -> list[Candidate]:
        sql = "SELECT * FROM candidates WHERE 1=1"
        args: list = []
        if status:
            sql += " AND status=?"; args.append(status)
        if arm:
            sql += " AND arm=?"; args.append(arm)
        sql += " ORDER BY id DESC LIMIT ?"; args.append(limit)
        return [self._row(r) for r in self._conn.execute(sql, args)]

    def best(self, metric: str = "worst_slack", maximize: bool = True,
             arm: str | None = None) -> Candidate | None:
        built = [c for c in self.list(status="built", arm=arm, limit=10_000)
                 if isinstance((c.metrics or {}).get(metric), (int, float))]
        if not built:
            return None
        return (max if maximize else min)(built, key=lambda c: c.metrics[metric])

    def stats(self, arm: str | None = None) -> dict:
        """What the evaluation reports for one arm."""
        rows = self.list(arm=arm, limit=100_000)
        return {
            "proposed": len(rows),
            "rejected": sum(1 for c in rows if c.status == "rejected"),
            "built": sum(1 for c in rows if c.status == "built"),
            "failed": sum(1 for c in rows if c.status == "failed"),
            # The headline cost measure: ORFS invocations, not agent turns.
            "tool_runs": sum(c.tool_runs for c in rows),
            # Summed build time across candidates, NOT elapsed wall clock:
            # candidates run concurrently, so this exceeds real time by roughly
            # the parallel width. A 12-candidate run summing 22108s took 10080s
            # on the clock. Named wall_clock_s once, which overstated the cost
            # of the loop by 2x in exactly the direction that flatters nothing.
            "build_seconds_total": round(sum(c.elapsed_s for c in rows), 1),
        }
