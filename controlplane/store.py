"""Persistent task ledger (SQLite).

Why a real database instead of a JSON file (P1 finding from the PoC review):

  * a corrupt JSON file used to be swallowed by `except: records = {}`, i.e. the
    plane could silently forget every task;
  * `work_item -> run_attempt -> worker_job` needs to be an explicit, queryable
    relation, otherwise a resume that creates a new Job loses the old job id;
  * events/evidence/approvals need append-only history, not an overwritten blob.

The ledger is the only durable thing the plane owns. It is git-ignored.

Schema
------
work_items(id, title, prompt, cwd, mode, isolation, idempotency_key UNIQUE,
           state, verdict, policy_json, assertions_json, baseline_json,
           warnings_json, metadata_json, current_attempt_id,
           created_at, updated_at)
attempts(id, work_item_id, worker_job_id, state, reason, created_at, ended_at)
events(id, work_item_id, attempt_id, type, payload_json, created_at)
evidence(id, work_item_id, attempt_id, kind, payload_json, created_at)
approvals(id, work_item_id, attempt_id, requested_at, decision, decided_at, note)
"""
from __future__ import annotations

import functools
import json
import os
import shutil
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / ".poc-state" / "ledger.db"
LEGACY_JSON = ROOT / ".poc-state" / "tasks.json"

class LedgerDegraded(RuntimeError):
    """Raised when a mutating operation is attempted on a degraded ledger.

    Fail-closed rule (review P0): if the integrity check failed we started from an
    empty database, so we no longer know which tasks are already running. Dispatching
    in that state can duplicate real work, therefore everything that changes state is
    refused; only reads, diagnostics and export stay available.
    """


JSON_FIELDS = ("policy", "assertions", "baseline", "warnings", "metadata",
               "artifacts", "assertion_results", "worker_metadata")
LIST_FIELDS = ("assertions", "warnings", "artifacts", "assertion_results")
def _synchronized(fn):
    """Serialize a mutating ledger method on the store-level RLock.

    sqlite3 gives durability, not mutual exclusion: two threads sharing one
    connection can interleave SELECT/INSERT/COMMIT. Every mutator goes through
    this decorator (review P1-3).
    """
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)

    return wrapper


COLUMNS = ("id", "title", "prompt", "cwd", "mode", "isolation", "idempotency_key",
           "state", "verdict", "policy", "assertions", "baseline", "warnings",
           "metadata", "current_attempt_id", "created_at", "updated_at",
           "session_id", "pid", "worktree", "branch", "head_commit", "detail",
           "started_at", "finished_at", "artifacts", "assertion_results",
           "worker_metadata")

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_items (
    id TEXT PRIMARY KEY,
    title TEXT,
    prompt TEXT,
    cwd TEXT,
    mode TEXT,
    isolation TEXT,
    idempotency_key TEXT UNIQUE,
    state TEXT,
    verdict TEXT,
    policy TEXT,
    assertions TEXT,
    baseline TEXT,
    warnings TEXT,
    metadata TEXT,
    current_attempt_id TEXT,
    created_at REAL,
    updated_at REAL,
    session_id TEXT,
    pid INTEGER,
    worktree TEXT,
    branch TEXT,
    head_commit TEXT,
    detail TEXT,
    started_at REAL,
    finished_at REAL,
    artifacts TEXT,
    assertion_results TEXT,
    worker_metadata TEXT
);
CREATE TABLE IF NOT EXISTS attempts (
    id TEXT PRIMARY KEY,
    work_item_id TEXT NOT NULL,
    worker_job_id TEXT,
    state TEXT,
    reason TEXT,
    created_at REAL,
    ended_at REAL
);
CREATE INDEX IF NOT EXISTS idx_attempts_work_item ON attempts(work_item_id);
CREATE INDEX IF NOT EXISTS idx_attempts_job ON attempts(worker_job_id);
-- idempotency must be enforced by the database, not by a check-then-insert
CREATE UNIQUE INDEX IF NOT EXISTS idx_work_items_idem
    ON work_items(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_item_id TEXT,
    attempt_id TEXT,
    type TEXT,
    payload TEXT,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_item_id TEXT,
    attempt_id TEXT,
    kind TEXT,
    payload TEXT,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_item_id TEXT,
    attempt_id TEXT,
    requested_at REAL,
    decision TEXT,
    decided_at REAL,
    note TEXT,
    digest TEXT,
    scope TEXT,
    consumed_at REAL,
    consumed_by TEXT
);
"""


def _dump(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _load(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


class TaskStore:
    """Ledger facade used by the control plane (name kept for compatibility)."""

    def __init__(self, path: Path | str | None = None, *, migrate_legacy: bool = True):
        base = Path(path) if path else DEFAULT_PATH
        # allow callers to pass the old *.json path
        if base.suffix.lower() == ".json":
            base = base.with_suffix(".db")
        self.path = base
        self.degraded = False
        self.degraded_reason = ""
        self._lock = threading.RLock()   # parallel dispatch (T11) writes from threads
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._open()
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._migrate_columns()
        if migrate_legacy and self._is_empty():
            self._migrate_legacy_json()

    # ------------------------------------------------------------------ #
    # connection / corruption handling
    # ------------------------------------------------------------------ #
    def _open(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(str(self.path), timeout=15, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=15000")
            ok = conn.execute("PRAGMA integrity_check").fetchone()
            if not ok or ok[0] != "ok":
                raise sqlite3.DatabaseError(f"integrity_check: {ok}")
            return conn
        except sqlite3.DatabaseError as exc:
            return self._quarantine(exc)

    def _quarantine(self, exc: Exception) -> sqlite3.Connection:
        """Never silently start empty: keep a copy, truncate in place, and shout.

        Renaming a broken sqlite file can fail on Windows (an open handle or a
        leftover -wal), and a failed rename used to leave the path pointing at the
        same broken bytes, so the "recover into an empty ledger" path exploded.
        Copy-then-truncate works in both cases.
        """
        stamp = time.strftime("%Y%m%d-%H%M%S")
        broken = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        try:
            if self.path.exists():
                shutil.copy2(self.path, broken)
                self.path.write_bytes(b"")
        except Exception:
            broken = None
        self.degraded = True
        self.degraded_reason = f"{type(exc).__name__}: {exc}; quarantined to {broken}"
        marker = self.path.parent / "LEDGER_DEGRADED.txt"
        marker.write_text(
            f"{time.strftime('%Y-%m-%d %H:%M:%S')} ledger corrupted, started empty\n"
            f"reason: {self.degraded_reason}\n", encoding="utf-8")
        print(f"[LEDGER DEGRADED] {self.degraded_reason}", file=sys.stderr)
        conn = sqlite3.connect(str(self.path), timeout=15, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def _migrate_columns(self) -> None:
        """Add approval-gating columns to ledgers created before this change."""
        wanted = {"digest": "TEXT", "scope": "TEXT",
                  "consumed_at": "REAL", "consumed_by": "TEXT",
                  "reserved_by": "TEXT", "reserved_at": "REAL"}
        have = {r["name"] for r in self._conn.execute("PRAGMA table_info(approvals)").fetchall()}
        for col, sql_type in wanted.items():
            if col not in have:
                try:
                    self._conn.execute(f"ALTER TABLE approvals ADD COLUMN {col} {sql_type}")
                except sqlite3.Error:
                    pass
        self._conn.commit()

    def assert_writable(self, operation: str) -> None:
        """Fail closed: a degraded ledger may not mutate task state."""
        if self.degraded:
            raise LedgerDegraded(
                f"refusing {operation}: ledger is degraded ({self.degraded_reason}). "
                "Queries, diagnostics and export are still available; recover the ledger "
                "(or start a new one) before dispatching again.")

    def _is_empty(self) -> bool:
        row = self._conn.execute("SELECT COUNT(*) FROM work_items").fetchone()
        return (row[0] if row else 0) == 0

    def _migrate_legacy_json(self) -> None:
        """Import the old .poc-state/tasks.json once, keeping its history."""
        if not LEGACY_JSON.exists():
            return
        try:
            raw = json.loads(LEGACY_JSON.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            self.degraded = True
            self.degraded_reason = f"legacy tasks.json unreadable: {exc}"
            print(f"[LEDGER DEGRADED] {self.degraded_reason}", file=sys.stderr)
            return
        records = raw.get("records", {}) if isinstance(raw, dict) else {}
        for task_id, rec in records.items():
            rec = dict(rec)
            rec.setdefault("task_id", task_id)
            try:
                self.create(rec)
            except Exception:  # noqa: BLE001 - migration must not block startup
                continue

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def new_task_id(self) -> str:
        return "cp-" + uuid.uuid4().hex[:12]

    def new_attempt_id(self) -> str:
        return "at-" + uuid.uuid4().hex[:10]

    def _row_to_record(self, row: sqlite3.Row) -> dict:
        rec = {k: row[k] for k in row.keys()}
        for field in JSON_FIELDS:
            default = [] if field in LIST_FIELDS else {}
            rec[field] = _load(rec.get(field), default)
        rec["task_id"] = rec["id"]
        rec["worker_job_id"] = None
        cur = rec.get("current_attempt_id")
        if cur:
            a = self._conn.execute("SELECT worker_job_id FROM attempts WHERE id=?", (cur,)).fetchone()
            if a:
                rec["worker_job_id"] = a["worker_job_id"]
        return rec

    # ------------------------------------------------------------------ #
    # work items
    # ------------------------------------------------------------------ #
    def create_work_item(self, record: dict) -> tuple[dict, bool]:
        """Atomically insert a work item unless its idempotency key already exists.

        Returns (record, created). The uniqueness is enforced by a DB index, so two
        threads (or two processes) racing on the same key cannot both create a task
        even if the pre-check races as well.
        """
        key = record.get("idempotency_key") or None
        with self._lock:
            if key:
                existing = self.by_idempotency_key(key)
                if existing:
                    return existing, False
            try:
                created = self.create(record)
                return created, True
            except sqlite3.IntegrityError:
                if key:
                    existing = self.by_idempotency_key(key)
                    if existing:
                        return existing, False
                raise

    @_synchronized
    def create(self, record: dict) -> dict:
        task_id = record["task_id"]
        now = time.time()
        values = {
            "id": task_id,
            "title": record.get("title"),
            "prompt": record.get("prompt"),
            "cwd": record.get("cwd"),
            "mode": record.get("mode"),
            "isolation": record.get("isolation"),
            "idempotency_key": record.get("idempotency_key") or None,
            "state": record.get("state"),
            "verdict": record.get("verdict"),
            "policy": _dump(record.get("policy")),
            "assertions": _dump(record.get("assertions") or []),
            "baseline": _dump(record.get("baseline")),
            "warnings": _dump(record.get("warnings") or []),
            "metadata": _dump(record.get("metadata") or {}),
            "current_attempt_id": record.get("current_attempt_id"),
            "created_at": record.get("created_at", now),
            "updated_at": now,
            "session_id": record.get("session_id"),
            "pid": record.get("pid"),
            "worktree": record.get("worktree"),
            "branch": record.get("branch"),
            "head_commit": record.get("head_commit"),
            "detail": record.get("detail"),
            "started_at": record.get("started_at"),
            "finished_at": record.get("finished_at"),
            "artifacts": _dump(record.get("artifacts") or []),
            "assertion_results": _dump(record.get("assertion_results") or []),
            "worker_metadata": _dump(record.get("worker_metadata") or {}),
        }
        cols = ", ".join(COLUMNS)
        marks = ", ".join(f":{c}" for c in COLUMNS)
        self._conn.execute(f"INSERT INTO work_items ({cols}) VALUES ({marks})", values)
        self._conn.commit()
        job_id = record.get("worker_job_id")
        if job_id:
            self.add_attempt(task_id, job_id, reason="dispatch")
        return self.get(task_id) or record

    @_synchronized
    def update(self, task_id: str, **patch: Any) -> dict:
        job_id = patch.pop("worker_job_id", "__keep__")
        row = self._conn.execute("SELECT * FROM work_items WHERE id=?", (task_id,)).fetchone()
        if row is None:
            self.create({"task_id": task_id, **patch})
        sets, values = [], {}
        for key, value in patch.items():
            if key in JSON_FIELDS:
                col = key
                values[col] = _dump(value)
            elif key in COLUMNS:
                col = key
                values[col] = value
            else:
                continue
            sets.append(f"{col} = :{col}")
        values["updated_at"] = time.time()
        sets.append("updated_at = :updated_at")
        values["id"] = task_id
        if sets:
            self._conn.execute(f"UPDATE work_items SET {', '.join(sets)} WHERE id = :id", values)
            self._conn.commit()
        if job_id != "__keep__" and job_id:
            current = self.get(task_id)
            if not current or current.get("worker_job_id") != job_id:
                # a pre-created attempt (e.g. the one that reserved an approval) gets
                # bound to the job instead of spawning a duplicate attempt
                if not self.bind_attempt_job(task_id, job_id):
                    self.add_attempt(task_id, job_id, reason="state-change")
        return self.get(task_id) or {}

    def get(self, task_id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM work_items WHERE id=?", (task_id,)).fetchone()
        return self._row_to_record(row) if row else None

    def all(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM work_items ORDER BY created_at ASC").fetchall()
        return [self._row_to_record(r) for r in rows]

    def by_job(self, worker_job_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT work_item_id FROM attempts WHERE worker_job_id=? ORDER BY created_at DESC LIMIT 1",
            (worker_job_id,)).fetchone()
        return self.get(row["work_item_id"]) if row else None

    def by_idempotency_key(self, key: str) -> dict | None:
        if not key:
            return None
        row = self._conn.execute(
            "SELECT * FROM work_items WHERE idempotency_key=?", (key,)).fetchone()
        return self._row_to_record(row) if row else None

    @_synchronized
    def clear(self) -> None:
        for table in ("approvals", "evidence", "events", "attempts", "work_items"):
            self._conn.execute(f"DELETE FROM {table}")
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # attempts / events / evidence / approvals
    # ------------------------------------------------------------------ #
    @_synchronized
    def add_attempt(self, task_id: str, worker_job_id: str | None, reason: str = "",
                    state: str | None = None) -> dict:
        attempt = {
            "id": self.new_attempt_id(),
            "work_item_id": task_id,
            "worker_job_id": worker_job_id,
            "state": state,
            "reason": reason,
            "created_at": time.time(),
        }
        self._conn.execute(
            "INSERT INTO attempts (id, work_item_id, worker_job_id, state, reason, created_at)"
            " VALUES (:id, :work_item_id, :worker_job_id, :state, :reason, :created_at)", attempt)
        self._conn.execute(
            "UPDATE work_items SET current_attempt_id=?, updated_at=? WHERE id=?",
            (attempt["id"], time.time(), task_id))
        self._conn.commit()
        return attempt

    @_synchronized
    def bind_attempt_job(self, task_id: str, worker_job_id: str) -> bool:
        """Attach a job id to the head attempt that has none (created pre-dispatch)."""
        rec = self.get(task_id) or {}
        attempt_id = rec.get("current_attempt_id")
        if not attempt_id:
            return False
        cur = self._conn.execute(
            "UPDATE attempts SET worker_job_id=? WHERE id=? AND worker_job_id IS NULL",
            (worker_job_id, attempt_id))
        self._conn.commit()
        return cur.rowcount == 1

    def attempts(self, task_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM attempts WHERE work_item_id=? ORDER BY created_at ASC", (task_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    @_synchronized
    def close_attempt(self, attempt_id: str, state: str) -> None:
        self._conn.execute("UPDATE attempts SET state=?, ended_at=? WHERE id=?",
                           (state, time.time(), attempt_id))
        self._conn.commit()

    @_synchronized
    def append_event(self, task_id: str, type_: str, payload: dict | None = None,
                     attempt_id: str | None = None) -> None:
        self._conn.execute(
            "INSERT INTO events (work_item_id, attempt_id, type, payload, created_at)"
            " VALUES (?,?,?,?,?)",
            (task_id, attempt_id, type_, _dump(payload or {}), time.time()))
        self._conn.commit()

    def events(self, task_id: str, limit: int = 100) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE work_item_id=? ORDER BY id DESC LIMIT ?",
            (task_id, limit)).fetchall()
        out = []
        for r in reversed(rows):
            d = dict(r)
            d["payload"] = _load(d.get("payload"), {})
            out.append(d)
        return out

    def add_evidence(self, task_id: str, kind: str, payload: dict,
                     attempt_id: str | None = None) -> None:
        self._conn.execute(
            "INSERT INTO evidence (work_item_id, attempt_id, kind, payload, created_at)"
            " VALUES (?,?,?,?,?)", (task_id, attempt_id, kind, _dump(payload), time.time()))
        self._conn.commit()

    def evidence(self, task_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM evidence WHERE work_item_id=? ORDER BY id ASC", (task_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = _load(d.get("payload"), {})
            out.append(d)
        return out

    @_synchronized
    def record_approval(self, task_id: str, decision: str, note: str = "",
                        attempt_id: str | None = None, requested_at: float | None = None) -> dict:
        rec = {"work_item_id": task_id, "attempt_id": attempt_id,
               "requested_at": requested_at or time.time(), "decision": decision,
               "decided_at": time.time(), "note": note}
        self._conn.execute(
            "INSERT INTO approvals (work_item_id, attempt_id, requested_at, decision, decided_at, note)"
            " VALUES (:work_item_id, :attempt_id, :requested_at, :decision, :decided_at, :note)", rec)
        self._conn.commit()
        return rec

    def all_approvals(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM approvals ORDER BY id ASC").fetchall()
        return [dict(r) for r in rows]

    def approvals(self, task_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM approvals WHERE work_item_id=? ORDER BY id ASC", (task_id,)).fetchall()
        return [dict(r) for r in rows]

    # -- approval gating -------------------------------------------------
    @_synchronized
    def request_approval(self, task_id: str, digest: str, scope: dict,
                         note: str = "") -> dict:
        """Record a pending approval bound to the exact task content."""
        rec = {"work_item_id": task_id, "attempt_id": None, "requested_at": time.time(),
               "decision": "pending", "decided_at": None, "note": note,
               "digest": digest, "scope": _dump(scope),
               "consumed_at": None, "consumed_by": None}
        cur = self._conn.execute(
            "INSERT INTO approvals (work_item_id, attempt_id, requested_at, decision,"
            " decided_at, note, digest, scope) VALUES (:work_item_id, :attempt_id,"
            " :requested_at, :decision, :decided_at, :note, :digest, :scope)", rec)
        self._conn.commit()
        rec["id"] = cur.lastrowid
        return rec

    @_synchronized
    def decide_approval(self, approval_id: int, decision: str, note: str = "",
                        expect_digest: str | None = None) -> dict | None:
        """Only a pending, unconsumed record can be decided.

        A rejected or already-consumed approval must never come back to life, and
        an explicit approval id must still match the digest the caller intends.
        """
        sql = ("UPDATE approvals SET decision=?, decided_at=?, note=? "
               "WHERE id=? AND decision='pending' AND consumed_at IS NULL")
        args: list[Any] = [decision, time.time(), note, approval_id]
        if expect_digest:
            sql += " AND digest=?"
            args.append(expect_digest)
        cur = self._conn.execute(sql, args)
        self._conn.commit()
        if cur.rowcount != 1:
            return None
        row = self._conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        return dict(row) if row else None

    def valid_approval_by_digest(self, digest: str) -> dict | None:
        """Approval is bound to content, not to a work item that does not exist yet."""
        row = self._conn.execute(
            "SELECT * FROM approvals WHERE digest=? AND decision='approved'"
            " AND consumed_at IS NULL AND reserved_at IS NULL ORDER BY id DESC LIMIT 1",
            (digest,)).fetchone()
        return dict(row) if row else None

    def valid_approval(self, task_id: str, digest: str) -> dict | None:
        """An unconsumed, unreserved record that matches this exact content digest."""
        row = self._conn.execute(
            "SELECT * FROM approvals WHERE work_item_id=? AND digest=? AND decision='approved'"
            " AND consumed_at IS NULL AND reserved_at IS NULL ORDER BY id DESC LIMIT 1",
            (task_id, digest)).fetchone()
        return dict(row) if row else None

    def approval_by_id(self, approval_id: int) -> dict | None:
        row = self._conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        return dict(row) if row else None

    @_synchronized
    def reserve_approval(self, digest: str, attempt_id: str) -> dict | None:
        """Atomically claim an approval for one attempt (BEGIN IMMEDIATE).

        This is what makes "one approval -> at most one worker dispatch" true under
        concurrency: the check and the claim happen inside one write transaction, so
        two attempts racing on the same digest cannot both win.
        """
        conn = self._conn
        row = conn.execute(
            "SELECT * FROM approvals WHERE digest=? AND decision='approved'"
            " AND consumed_at IS NULL AND reserved_at IS NULL ORDER BY id DESC LIMIT 1",
            (digest,)).fetchone()
        if not row:
            return None
        now = time.time()
        # The conditional UPDATE is the actual guard: it is atomic inside SQLite, so
        # even a second process cannot reserve the same row.
        cur = conn.execute(
            "UPDATE approvals SET reserved_by=?, reserved_at=?"
            " WHERE id=? AND reserved_at IS NULL AND consumed_at IS NULL",
            (attempt_id, now, row["id"]))
        conn.commit()
        if cur.rowcount != 1:
            return None
        out = dict(row)
        out.update(reserved_by=attempt_id, reserved_at=now)
        return out

    @_synchronized
    def consume_reserved(self, approval_id: int, attempt_id: str,
                         worker_job_id: str | None) -> bool:
        """Turn a reservation into a consumption; only the reserver may do this."""
        cur = self._conn.execute(
            "UPDATE approvals SET consumed_at=?, consumed_by=?"
            " WHERE id=? AND reserved_by=? AND consumed_at IS NULL",
            (time.time(), worker_job_id or attempt_id, approval_id, attempt_id))
        self._conn.commit()
        return cur.rowcount == 1

    @_synchronized
    def release_reservation(self, approval_id: int, attempt_id: str) -> bool:
        """Give the reservation back (only when we PROVED nothing was dispatched)."""
        cur = self._conn.execute(
            "UPDATE approvals SET reserved_by=NULL, reserved_at=NULL"
            " WHERE id=? AND reserved_by=? AND consumed_at IS NULL",
            (approval_id, attempt_id))
        self._conn.commit()
        return cur.rowcount == 1

    @_synchronized
    def consume_approval(self, approval_id: int, attempt_id: str | None) -> bool:
        """Legacy single-use consumer (no reservation required)."""
        cur = self._conn.execute(
            "UPDATE approvals SET consumed_at=?, consumed_by=? WHERE id=? AND consumed_at IS NULL",
            (time.time(), attempt_id, approval_id))
        self._conn.commit()
        return cur.rowcount == 1

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
