"""Persistent LD goal state and bounded multi-decision advancement (M4-B).

A goal is the smallest durable unit above the Ledger: it records what the
Human Owner asked for once, how many decisions and tasks the LD has spent on
it, which task is currently in flight, and what the last decision was.  It is
deliberately NOT a second task system:

* task truth (state, verdict, attempts) always lives in the WorkItem ledger;
* the goal never overrides or reinterprets an acceptance verdict;
* each goal row carries a time-boxed lease so two processes cannot advance
  the same goal concurrently;
* every decision and its outcome are appended to an immutable history table,
  keyed by (goal_id, step), so retries and restarts replay instead of
  duplicating work.

Tables live in the same SQLite ledger file as the control-plane store; the
goal store opens its own connection (WAL, like every other writer).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from typing import Any

GOAL_STATUSES = ("active", "waiting", "needs_human", "completed", "failed")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ld_goals (
    goal_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    goal_text TEXT NOT NULL,
    goal_ref TEXT NOT NULL,
    status TEXT NOT NULL,
    current_task_id TEXT,
    decision_count INTEGER NOT NULL DEFAULT 0,
    task_count INTEGER NOT NULL DEFAULT 0,
    last_decision TEXT,
    handoff_task_ids TEXT NOT NULL DEFAULT '[]',
    claim_owner TEXT,
    claim_until REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ld_goal_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id TEXT NOT NULL,
    step INTEGER NOT NULL,
    decision TEXT NOT NULL,
    outcome TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(goal_id, step)
);
CREATE INDEX IF NOT EXISTS idx_ld_goal_decisions_goal
    ON ld_goal_decisions(goal_id, step);
"""


def goal_identity(goal: str, project_id: str) -> tuple[str, str]:
    """Deterministic (goal_id, goal_ref) for one goal text inside one project.

    The same Human Owner goal re-submitted to the same project resumes the
    same goal row instead of creating a second one; the sha256 goal_ref is
    what LD submissions stamp onto their WorkItems.
    """
    digest = hashlib.sha256(
        json.dumps([project_id, goal.strip()], ensure_ascii=False,
                   separators=(",", ":")).encode("utf-8")).hexdigest()
    return f"goal-{digest[:12]}", f"sha256:{digest}"


class GoalStoreError(RuntimeError):
    """Goal persistence is inconsistent with the requested operation."""


class GoalLeaseHeld(RuntimeError):
    """Another process holds the goal's advancement lease."""

    def __init__(self, goal_id: str, owner: str, until: float):
        super().__init__(
            f"goal {goal_id} is being advanced by {owner!r} until {until}")
        self.goal_id = goal_id
        self.owner = owner
        self.until = until


class LDGoalStore:
    """Durable goal rows + immutable decision history in the ledger file."""

    def __init__(self, ledger_path, *, lease_s: float = 120.0):
        self._lease_s = lease_s
        self._conn = sqlite3.connect(str(ledger_path), timeout=15,
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=15000")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------ #
    # goal rows
    # ------------------------------------------------------------------ #
    def get_or_create(self, goal_id: str, project_id: str, goal_text: str,
                      goal_ref: str) -> dict:
        now = time.time()
        self._conn.execute(
            "INSERT OR IGNORE INTO ld_goals (goal_id, project_id, goal_text,"
            " goal_ref, status, decision_count, task_count, handoff_task_ids,"
            " created_at, updated_at) VALUES (?,?,?,?, 'active', 0, 0, '[]', ?, ?)",
            (goal_id, project_id, goal_text.strip(), goal_ref, now, now))
        self._conn.commit()
        return self.get(goal_id) or {}

    def get(self, goal_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM ld_goals WHERE goal_id=?", (goal_id,)).fetchone()
        return self._row_to_goal(row) if row else None

    def find_by_project(self, project_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM ld_goals WHERE project_id=? ORDER BY updated_at DESC",
            (project_id,)).fetchall()
        return [self._row_to_goal(row) for row in rows]

    # ------------------------------------------------------------------ #
    # lease (cross-process advancement claim)
    # ------------------------------------------------------------------ #
    def acquire_lease(self, goal_id: str, owner: str | None = None) -> dict:
        """Atomically take the advancement lease or raise GoalLeaseHeld."""
        owner = owner or ("ld-goal-" + uuid.uuid4().hex[:10])
        now = time.time()
        cur = self._conn.execute(
            "UPDATE ld_goals SET claim_owner=?, claim_until=?, updated_at=? "
            "WHERE goal_id=? AND (claim_until IS NULL OR claim_until < ?)",
            (owner, now + self._lease_s, now, goal_id, now))
        self._conn.commit()
        if cur.rowcount != 1:
            goal = self.get(goal_id) or {}
            raise GoalLeaseHeld(
                goal_id, goal.get("claim_owner") or "?",
                goal.get("claim_until") or 0.0)
        goal = self.get(goal_id) or {}
        goal["claim_owner"] = owner
        return goal

    def release_lease(self, goal_id: str, owner: str) -> None:
        self._conn.execute(
            "UPDATE ld_goals SET claim_owner=NULL, claim_until=NULL, updated_at=? "
            "WHERE goal_id=? AND claim_owner=?",
            (time.time(), goal_id, owner))
        self._conn.commit()

    def renew_lease(self, goal_id: str, owner: str) -> bool:
        now = time.time()
        cur = self._conn.execute(
            "UPDATE ld_goals SET claim_until=?, updated_at=? "
            "WHERE goal_id=? AND claim_owner=?",
            (now + self._lease_s, now, goal_id, owner))
        self._conn.commit()
        return cur.rowcount == 1

    # ------------------------------------------------------------------ #
    # state transitions
    # ------------------------------------------------------------------ #
    def record_decision(self, goal_id: str, step: int, decision: dict,
                        outcome: dict) -> None:
        """Append one decision outcome; the (goal_id, step) key is immutable."""
        now = time.time()
        try:
            self._conn.execute(
                "INSERT INTO ld_goal_decisions (goal_id, step, decision, outcome,"
                " created_at) VALUES (?,?,?,?,?)",
                (goal_id, step,
                 json.dumps(decision, ensure_ascii=False, default=str),
                 json.dumps(outcome, ensure_ascii=False, default=str), now))
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            raise GoalStoreError(
                f"goal {goal_id} step {step} already has a recorded decision; "
                "replay must resume from the next step") from exc

    def resume(self, goal_id: str, note: str = "") -> dict:
        """Explicitly return a needs_human goal to active.

        Only a human decision lifts the pause; this method records who/why as
        one decision-history entry and refuses anything that is not currently
        parked on a human.
        """
        note = (note or "").strip() or "resumed by human owner"
        goal = self.get(goal_id) or {}
        if not goal:
            raise GoalStoreError(f"no goal {goal_id}")
        if goal.get("status") != "needs_human":
            raise GoalStoreError(
                f"goal {goal_id} is {goal.get('status')!r}, not needs_human; "
                "only a paused goal can be resumed")
        step = int(goal.get("decision_count", 0)) + 1
        self.update_goal(goal_id, status="active",
                         last_decision={"step": step, "action": "human_resume",
                                        "reason": note})
        self.record_decision(goal_id, step,
                             {"action": "human_resume", "reason": note},
                             {"action": "human_resume", "next_state": "active",
                              "task_id": None, "executed": False,
                              "blocking_reasons": []})
        return self.get(goal_id) or {}

    def update_goal(self, goal_id: str, **fields: Any) -> dict:
        allowed = {"status", "current_task_id", "decision_count", "task_count",
                   "last_decision", "handoff_task_ids"}
        unknown = set(fields) - allowed
        if unknown:
            raise GoalStoreError("unknown goal fields: " + ", ".join(sorted(unknown)))
        if "last_decision" in fields and fields["last_decision"] is not None \
                and not isinstance(fields["last_decision"], str):
            fields["last_decision"] = json.dumps(
                fields["last_decision"], ensure_ascii=False, default=str)
        if "handoff_task_ids" in fields and not isinstance(
                fields["handoff_task_ids"], str):
            fields["handoff_task_ids"] = json.dumps(
                fields["handoff_task_ids"], ensure_ascii=False)
        sets = ", ".join(f"{key}=?" for key in fields)
        values = list(fields.values()) + [time.time(), goal_id]
        if sets:
            self._conn.execute(
                f"UPDATE ld_goals SET {sets}, updated_at=? WHERE goal_id=?",
                values)
            self._conn.commit()
        return self.get(goal_id) or {}

    def decisions(self, goal_id: str, limit: int = 100) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM ld_goal_decisions WHERE goal_id=? "
            "ORDER BY step ASC LIMIT ?", (goal_id, limit)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["decision"] = json.loads(item["decision"])
            item["outcome"] = json.loads(item["outcome"])
            out.append(item)
        return out

    # ------------------------------------------------------------------ #
    @staticmethod
    def _row_to_goal(row: sqlite3.Row) -> dict:
        goal = dict(row)
        goal["handoff_task_ids"] = json.loads(goal.get("handoff_task_ids") or "[]")
        if goal.get("last_decision"):
            try:
                goal["last_decision"] = json.loads(goal["last_decision"])
            except json.JSONDecodeError:
                goal["last_decision"] = None
        else:
            goal["last_decision"] = None
        return goal


__all__ = ["GOAL_STATUSES", "GoalLeaseHeld", "GoalStoreError",
           "LDGoalStore", "goal_identity"]
