"""Persistent LD goal state and bounded multi-decision advancement (M4-B).

A goal is the smallest durable unit above the Ledger: it records what the
Human Owner asked for once, how many decisions and tasks the LD has spent on
it, which task is currently in flight, and what the last decision was.  It is
deliberately NOT a second task system:

* task truth (state, verdict, attempts) always lives in the WorkItem ledger;
* the goal never overrides or reinterprets an acceptance verdict.

Reliability contract (M4 reliability closure):

* **Fencing.**  ``acquire_lease`` atomically bumps a monotonic
  ``lease_token``.  Every state-changing write (step intent, step commit,
  stop, renewal, release) is conditioned on that token inside one SQLite
  ``BEGIN IMMEDIATE`` transaction, so a holder whose lease expired and was
  taken over can never write again — even if it is still running.
* **Two-phase steps.**  A side-effecting step (task submission) is first
  persisted as ``pending`` together with its validated decision, *before* the
  side effect.  The outcome and the goal transition are then committed in a
  single transaction.  A crash anywhere in between leaves a ``pending`` step
  that the next holder reconciles from the Ledger instead of asking the model
  again.
* **Contiguous history.**  ``decision_count`` always equals the highest
  committed step; human resumes are steps in the same sequence and are
  committed in the same transaction that increments the counter.

Tables live in the same SQLite ledger file as the control-plane store; the
goal store opens its own connection (WAL, explicit transactions).
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
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
    resume_count INTEGER NOT NULL DEFAULT 0,
    task_count INTEGER NOT NULL DEFAULT 0,
    last_decision TEXT,
    handoff_task_ids TEXT NOT NULL DEFAULT '[]',
    claim_owner TEXT,
    claim_until REAL,
    lease_token INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ld_goal_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id TEXT NOT NULL,
    step INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'committed',
    decision TEXT NOT NULL,
    outcome TEXT NOT NULL,
    lease_token INTEGER,
    created_at REAL NOT NULL,
    UNIQUE(goal_id, step)
);
CREATE INDEX IF NOT EXISTS idx_ld_goal_decisions_goal
    ON ld_goal_decisions(goal_id, step);
"""

_MIGRATIONS = {
    "ld_goals": {"resume_count": "INTEGER NOT NULL DEFAULT 0",
                 "lease_token": "INTEGER NOT NULL DEFAULT 0"},
    "ld_goal_decisions": {"state": "TEXT NOT NULL DEFAULT 'committed'",
                          "lease_token": "INTEGER"},
}

_GOAL_FIELDS = {"status", "current_task_id", "task_count", "last_decision",
                "handoff_task_ids"}


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


def step_idempotency_key(goal_id: str, step: int) -> str:
    """System-generated dispatch key for one goal step (never model-chosen)."""
    return f"ldg-{goal_id}-s{int(step)}"


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


class GoalLeaseLost(RuntimeError):
    """The caller's fencing token is stale: another holder took the goal over."""

    def __init__(self, goal_id: str, token: int | None, current: int | None):
        super().__init__(
            f"lease on goal {goal_id} was lost (token {token}, current {current}); "
            "this holder must stop without writing goal state")
        self.goal_id = goal_id
        self.token = token
        self.current = current


class LDGoalStore:
    """Durable goal rows + two-phase decision history in the ledger file."""

    def __init__(self, ledger_path, *, lease_s: float = 120.0,
                 fault_hook: Callable[[str], None] | None = None):
        self._lease_s = float(lease_s)
        self._fault_hook = fault_hook
        self._lock = threading.RLock()
        self.path = str(ledger_path)
        self._conn = sqlite3.connect(str(ledger_path), timeout=15,
                                     check_same_thread=False,
                                     isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=15000")
        self._conn.executescript(_SCHEMA)
        self._migrate()

    @property
    def lease_s(self) -> float:
        return self._lease_s

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _migrate(self) -> None:
        for table, wanted in _MIGRATIONS.items():
            have = {row["name"] for row in
                    self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for column, sql_type in wanted.items():
                if column not in have:
                    self._conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")

    @contextlib.contextmanager
    def _txn(self):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _hook(self, point: str) -> None:
        if self._fault_hook is not None:
            self._fault_hook(point)

    # ------------------------------------------------------------------ #
    # goal rows
    # ------------------------------------------------------------------ #
    def get_or_create(self, goal_id: str, project_id: str, goal_text: str,
                      goal_ref: str) -> dict:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO ld_goals (goal_id, project_id, goal_text,"
                " goal_ref, status, decision_count, task_count, handoff_task_ids,"
                " created_at, updated_at) VALUES (?,?,?,?, 'active', 0, 0, '[]', ?, ?)",
                (goal_id, project_id, goal_text.strip(), goal_ref, now, now))
        return self.get(goal_id) or {}

    def get(self, goal_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM ld_goals WHERE goal_id=?", (goal_id,)).fetchone()
        return self._row_to_goal(row) if row else None

    def find_by_project(self, project_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM ld_goals WHERE project_id=? ORDER BY updated_at DESC",
                (project_id,)).fetchall()
        return [self._row_to_goal(row) for row in rows]

    def all_goals(self) -> list[dict]:
        """Every goal row, oldest first (deterministic scheduling order)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM ld_goals ORDER BY created_at ASC, goal_id ASC"
            ).fetchall()
        return [self._row_to_goal(row) for row in rows]

    def statuses(self) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT goal_id, status FROM ld_goals").fetchall()
        return {row["goal_id"]: row["status"] for row in rows}

    # ------------------------------------------------------------------ #
    # lease (cross-process advancement claim with fencing token)
    # ------------------------------------------------------------------ #
    @staticmethod
    def default_owner() -> str:
        """Unique per process and call; never shared between processes."""
        return f"ld-goal-{os.getpid()}-{uuid.uuid4().hex[:8]}"

    def acquire_lease(self, goal_id: str, owner: str | None = None) -> dict:
        """Atomically take the lease and a fresh fencing token, or raise."""
        owner = owner or self.default_owner()
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE ld_goals SET claim_owner=?, claim_until=?, "
                "lease_token=lease_token+1, updated_at=? "
                "WHERE goal_id=? AND (claim_until IS NULL OR claim_until < ?)",
                (owner, now + self._lease_s, now, goal_id, now))
        if cur.rowcount != 1:
            goal = self.get(goal_id) or {}
            raise GoalLeaseHeld(
                goal_id, goal.get("claim_owner") or "?",
                goal.get("claim_until") or 0.0)
        return self.get(goal_id) or {}

    def renew_lease(self, goal_id: str, owner: str, token: int | None = None) -> bool:
        now = time.time()
        sql = ("UPDATE ld_goals SET claim_until=?, updated_at=? "
               "WHERE goal_id=? AND claim_owner=?")
        args: list[Any] = [now + self._lease_s, now, goal_id, owner]
        if token is not None:
            sql += " AND lease_token=?"
            args.append(token)
        with self._lock:
            cur = self._conn.execute(sql, args)
        return cur.rowcount == 1

    def release_lease(self, goal_id: str, owner: str, token: int | None = None) -> None:
        sql = ("UPDATE ld_goals SET claim_owner=NULL, claim_until=NULL, updated_at=? "
               "WHERE goal_id=? AND claim_owner=?")
        args: list[Any] = [time.time(), goal_id, owner]
        if token is not None:
            sql += " AND lease_token=?"
            args.append(token)
        with self._lock:
            self._conn.execute(sql, args)

    # ------------------------------------------------------------------ #
    # fenced, transactional state transitions
    # ------------------------------------------------------------------ #
    def _fenced_row(self, conn, goal_id: str, token: int | None) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM ld_goals WHERE goal_id=?", (goal_id,)).fetchone()
        if row is None:
            raise GoalStoreError(f"no goal {goal_id}")
        if token is not None and row["lease_token"] != token:
            raise GoalLeaseLost(goal_id, token, row["lease_token"])
        return row

    @staticmethod
    def _encode_fields(fields: dict[str, Any]) -> dict[str, Any]:
        unknown = set(fields) - _GOAL_FIELDS
        if unknown:
            raise GoalStoreError("unknown goal fields: " + ", ".join(sorted(unknown)))
        out = dict(fields)
        if out.get("last_decision") is not None and not isinstance(
                out["last_decision"], str):
            out["last_decision"] = json.dumps(
                out["last_decision"], ensure_ascii=False, default=str)
        if "handoff_task_ids" in out and not isinstance(out["handoff_task_ids"], str):
            out["handoff_task_ids"] = json.dumps(out["handoff_task_ids"],
                                                 ensure_ascii=False)
        return out

    def _apply_goal_fields(self, conn, goal_id: str, fields: dict[str, Any],
                           *, decision_count: int | None = None,
                           resume_increment: bool = False) -> None:
        encoded = self._encode_fields(fields)
        sets = [f"{key}=?" for key in encoded]
        values = list(encoded.values())
        if decision_count is not None:
            sets.append("decision_count=?")
            values.append(decision_count)
        if resume_increment:
            sets.append("resume_count=resume_count+1")
        sets.append("updated_at=?")
        values.append(time.time())
        conn.execute(f"UPDATE ld_goals SET {', '.join(sets)} WHERE goal_id=?",
                     [*values, goal_id])

    def pending_step(self, goal_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM ld_goal_decisions WHERE goal_id=? AND state='pending' "
                "ORDER BY step DESC LIMIT 1", (goal_id,)).fetchone()
        return self._row_to_decision(row) if row else None

    def begin_step(self, goal_id: str, step: int, decision: dict,
                   token: int | None) -> None:
        """Durably record a validated decision *before* its side effect."""
        with self._txn() as conn:
            row = self._fenced_row(conn, goal_id, token)
            if row["decision_count"] != step - 1:
                raise GoalStoreError(
                    f"goal {goal_id} is at step {row['decision_count']}; "
                    f"cannot open step {step}")
            if conn.execute(
                    "SELECT 1 FROM ld_goal_decisions WHERE goal_id=? AND state='pending'",
                    (goal_id,)).fetchone():
                raise GoalStoreError(f"goal {goal_id} already has a pending step")
            conn.execute(
                "INSERT INTO ld_goal_decisions (goal_id, step, state, decision,"
                " outcome, lease_token, created_at) VALUES (?,?, 'pending', ?, '{}', ?, ?)",
                (goal_id, step, json.dumps(decision, ensure_ascii=False, default=str),
                 token, time.time()))

    def commit_step(self, goal_id: str, step: int, outcome: dict,
                    fields: dict[str, Any], token: int | None) -> dict:
        """Close a pending step and apply its goal transition atomically."""
        with self._txn() as conn:
            row = self._fenced_row(conn, goal_id, token)
            if row["decision_count"] != step - 1:
                raise GoalStoreError(
                    f"goal {goal_id} is at step {row['decision_count']}; "
                    f"cannot commit step {step}")
            cur = conn.execute(
                "UPDATE ld_goal_decisions SET state='committed', outcome=? "
                "WHERE goal_id=? AND step=? AND state='pending'",
                (json.dumps(outcome, ensure_ascii=False, default=str), goal_id, step))
            if cur.rowcount != 1:
                raise GoalStoreError(f"goal {goal_id} step {step} is not pending")
            self._hook("commit_mid")
            self._apply_goal_fields(conn, goal_id, fields, decision_count=step)
        return self.get(goal_id) or {}

    def record_step(self, goal_id: str, step: int, decision: dict, outcome: dict,
                    fields: dict[str, Any], token: int | None) -> dict:
        """Record a side-effect-free step and its transition atomically."""
        with self._txn() as conn:
            row = self._fenced_row(conn, goal_id, token)
            if row["decision_count"] != step - 1:
                raise GoalStoreError(
                    f"goal {goal_id} is at step {row['decision_count']}; "
                    f"cannot record step {step}")
            try:
                conn.execute(
                    "INSERT INTO ld_goal_decisions (goal_id, step, state, decision,"
                    " outcome, lease_token, created_at) VALUES (?,?, 'committed', ?, ?, ?, ?)",
                    (goal_id, step,
                     json.dumps(decision, ensure_ascii=False, default=str),
                     json.dumps(outcome, ensure_ascii=False, default=str),
                     token, time.time()))
            except sqlite3.IntegrityError as exc:
                raise GoalStoreError(
                    f"goal {goal_id} step {step} already exists") from exc
            self._hook("commit_mid")
            self._apply_goal_fields(conn, goal_id, fields, decision_count=step)
        return self.get(goal_id) or {}

    def stop_goal(self, goal_id: str, token: int | None, status: str,
                  last_decision: dict) -> dict:
        """Fenced status-only transition (budget/stall stops; adds no step)."""
        if status not in GOAL_STATUSES:
            raise GoalStoreError(f"unknown goal status {status!r}")
        with self._txn() as conn:
            self._fenced_row(conn, goal_id, token)
            self._apply_goal_fields(conn, goal_id, {
                "status": status, "last_decision": last_decision})
        return self.get(goal_id) or {}

    def resume(self, goal_id: str, note: str = "", token: int | None = None) -> dict:
        """Explicitly return a needs_human goal to active (human decision only).

        The resume is one step of the same contiguous history: the decision row
        and the ``decision_count`` increment commit in one transaction, so the
        next model step never collides with it.  Without a token the call is
        refused while another holder has a live lease.
        """
        note = (note or "").strip() or "resumed by human owner"
        with self._txn() as conn:
            row = self._fenced_row(conn, goal_id, token)
            if row["status"] != "needs_human":
                raise GoalStoreError(
                    f"goal {goal_id} is {row['status']!r}, not needs_human; "
                    "only a paused goal can be resumed")
            if (token is None and row["claim_until"] is not None
                    and row["claim_until"] >= time.time()):
                raise GoalLeaseHeld(goal_id, row["claim_owner"] or "?",
                                    row["claim_until"])
            if conn.execute(
                    "SELECT 1 FROM ld_goal_decisions WHERE goal_id=? AND state='pending'",
                    (goal_id,)).fetchone():
                raise GoalStoreError(
                    f"goal {goal_id} has a pending step; reconcile before resuming")
            step = int(row["decision_count"]) + 1
            decision = {"action": "human_resume", "reason": note}
            conn.execute(
                "INSERT INTO ld_goal_decisions (goal_id, step, state, decision,"
                " outcome, lease_token, created_at) VALUES (?,?, 'committed', ?, ?, ?, ?)",
                (goal_id, step, json.dumps(decision, ensure_ascii=False),
                 json.dumps({"action": "human_resume", "next_state": "active",
                             "task_id": None, "executed": False,
                             "blocking_reasons": []}),
                 token, time.time()))
            self._apply_goal_fields(
                conn, goal_id,
                {"status": "active",
                 "last_decision": {"step": step, "action": "human_resume",
                                   "reason": note}},
                decision_count=step, resume_increment=True)
        return self.get(goal_id) or {}

    def update_goal(self, goal_id: str, **fields: Any) -> dict:
        """Unfenced administrative update (never used by the advancement loop)."""
        with self._txn() as conn:
            self._fenced_row(conn, goal_id, None)
            self._apply_goal_fields(conn, goal_id, fields)
        return self.get(goal_id) or {}

    def decisions(self, goal_id: str, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM ld_goal_decisions WHERE goal_id=? "
                "ORDER BY step ASC LIMIT ?", (goal_id, limit)).fetchall()
        return [self._row_to_decision(row) for row in rows]

    # ------------------------------------------------------------------ #
    @staticmethod
    def _row_to_decision(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["decision"] = json.loads(item["decision"] or "{}")
        item["outcome"] = json.loads(item["outcome"] or "{}")
        return item

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
        goal["model_decision_count"] = (int(goal.get("decision_count") or 0)
                                        - int(goal.get("resume_count") or 0))
        return goal


__all__ = ["GOAL_STATUSES", "GoalLeaseHeld", "GoalLeaseLost", "GoalStoreError",
           "LDGoalStore", "goal_identity", "step_idempotency_key"]
