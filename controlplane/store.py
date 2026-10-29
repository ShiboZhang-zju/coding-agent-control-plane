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
projects(id, name, root UNIQUE, default_adapter, policy_json,
         concurrency_limit, active, created_at, updated_at)
workspaces(id, project_id, node_id, local_root, adapter, capabilities_json,
           is_default, active, created_at, updated_at)
session_references(id, project_id, workspace_id, worker_instance_id, adapter,
                   session_id, work_item_id, attempt_id, created_at, updated_at)
project_context_entries(id, project_id, kind, title, body, tags_json,
                        verification, security_domain, source, evidence_refs_json,
                        version, created_at, updated_at)
project_context_revisions(id, entry_id, project_id, version, snapshot_json,
                          changed_at, changed_by, note)
project_context_state(project_id PRIMARY KEY, version, updated_at)
context_update_receipts(idempotency_key PRIMARY KEY, project_id, work_item_id,
                        attempt_id, entry_kind, entry_title, entry_id, status,
                        owner, payload_json, created_at, updated_at)
work_items(id, title, prompt, cwd, mode, isolation, idempotency_key UNIQUE,
           state, verdict, policy_json, assertions_json, baseline_json,
           warnings_json, metadata_json, current_attempt_id, adapter,
           queue_status, priority, task_digest, dispatch_claim_owner,
           dispatch_claimed_at, project_id, workspace_id,
           original_task, effective_prompt, context_version,
           context_entry_refs_json,
           created_at, updated_at)
attempts(id, work_item_id, worker_job_id, state, reason, created_at, ended_at)
events(id, work_item_id, attempt_id, type, payload_json, created_at)
evidence(id, work_item_id, attempt_id, kind, payload_json, created_at)
approvals(id, work_item_id, attempt_id, requested_at, decision, decided_at, note)

Project-centric evolution (M1)
------------------------------
`projects.root` is kept as the *default local workspace root* for backward
compatibility: every project gets exactly one `workspaces` row with
`node_id='local'` and `is_default=1`, created by `ensure_default_workspace()`.
Nothing in the existing dispatch path reads `workspaces` yet, so a ledger that
predates this table keeps working; the table is the seam for M5 (distributed
workspaces) and is never used to *pretend* remote execution exists.
"""
from __future__ import annotations

import functools
import json
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
               "artifacts", "assertion_results", "worker_metadata",
               "context_entry_refs")
LIST_FIELDS = ("assertions", "warnings", "artifacts", "assertion_results",
               "context_entry_refs")
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
           "worker_metadata", "adapter", "queue_status", "priority", "task_digest",
           "dispatch_claim_owner", "dispatch_claimed_at", "project_id",
           "workspace_id", "original_task", "effective_prompt", "context_version",
           "context_entry_refs")

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    root TEXT NOT NULL UNIQUE,
    default_adapter TEXT NOT NULL,
    policy TEXT,
    concurrency_limit INTEGER NOT NULL DEFAULT 2,
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
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
    worker_metadata TEXT,
    adapter TEXT,
    queue_status TEXT,
    priority INTEGER DEFAULT 0,
    task_digest TEXT,
    dispatch_claim_owner TEXT,
    dispatch_claimed_at REAL,
    project_id TEXT,
    workspace_id TEXT,
    original_task TEXT,
    effective_prompt TEXT,
    context_version INTEGER,
    context_entry_refs TEXT
);
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    local_root TEXT NOT NULL,
    adapter TEXT,
    capabilities TEXT,
    is_default INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_workspaces_project_root
    ON workspaces(project_id, node_id, local_root);
CREATE TABLE IF NOT EXISTS session_references (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    workspace_id TEXT,
    worker_instance_id TEXT,
    adapter TEXT,
    session_id TEXT,
    work_item_id TEXT,
    attempt_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_refs_project
    ON session_references(project_id, created_at DESC);
CREATE TABLE IF NOT EXISTS project_context_entries (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    tags TEXT,
    verification TEXT NOT NULL,
    security_domain TEXT NOT NULL,
    source TEXT,
    evidence_refs TEXT,
    version INTEGER NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_context_project_title
    ON project_context_entries(project_id, kind, title);
CREATE INDEX IF NOT EXISTS idx_context_project_kind
    ON project_context_entries(project_id, kind, updated_at DESC);
CREATE TABLE IF NOT EXISTS project_context_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    snapshot TEXT NOT NULL,
    changed_at REAL NOT NULL,
    changed_by TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_context_revisions_entry
    ON project_context_revisions(entry_id, version DESC);
CREATE TABLE IF NOT EXISTS project_context_state (
    project_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS context_update_receipts (
    idempotency_key TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    work_item_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    entry_kind TEXT NOT NULL,
    entry_title TEXT NOT NULL,
    entry_id TEXT,
    status TEXT NOT NULL,
    owner TEXT,
    payload TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_context_updates_work_item
    ON context_update_receipts(work_item_id, created_at ASC);
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
    consumed_by TEXT,
    reserved_by TEXT,
    reserved_at REAL
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
        # per-db marker: a shared "LEDGER_DEGRADED.txt" in .poc-state/ would
        # wrongly degrade every other test ledger in the same directory
        self.degraded_marker = base.with_name(base.name + ".DEGRADED")
        self.degraded = False
        self.degraded_reason = ""
        #: Non-empty when the degraded marker could not be written/removed. A
        #: non-empty value means the fail-closed guarantee is process-local only.
        self.degraded_marker_error = ""
        self._lock = threading.RLock()   # parallel dispatch (T11) writes from threads
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._open()
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._migrate_columns()
        self._restore_degraded_state()
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

        Review P0 (fail-closed across restarts): the degraded state is persisted in
        a marker file next to the db. A restart passes integrity_check on the now
        empty file, but the marker re-degrades it, so the dispatch ban survives the
        process that discovered the corruption. Only an explicit
        `recover_degraded()` (human operation) lifts it.
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
        persisted = self._write_degraded_marker(
            f"{time.strftime('%Y-%m-%d %H:%M:%S')} ledger corrupted, started empty\n"
            f"reason: {self.degraded_reason}\n"
            "manual recovery required: review the *.corrupt-* copy, then call\n"
            "recover_degraded(operator_note=...) or `controlplane recover-ledger`.\n")
        if not persisted:
            # Do not claim a cross-restart guarantee we could not establish.
            self.degraded_reason += (
                "; WARNING: the degraded marker could not be persisted "
                f"({self.degraded_marker_error}), so the dispatch ban is "
                "process-local only and will NOT survive a restart")
        print(f"[LEDGER DEGRADED] {self.degraded_reason}", file=sys.stderr)
        conn = sqlite3.connect(str(self.path), timeout=15, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def _write_degraded_marker(self, text: str) -> bool:
        """Persist the degraded marker. Returns True only when it is on disk.

        Review P0 (T27): a swallowed ``OSError`` used to let the process claim
        "the dispatch ban survives a restart" while nothing was written. The
        caller must be able to tell the difference, so the result is returned and
        a failure is recorded in ``degraded_marker_error``.
        """
        try:
            self.degraded_marker.write_text(text, encoding="utf-8")
        except OSError as exc:
            self.degraded_marker_error = f"{type(exc).__name__}: {exc}"
            print(f"[LEDGER DEGRADED] cannot persist marker "
                  f"{self.degraded_marker}: {self.degraded_marker_error}",
                  file=sys.stderr)
            return False
        self.degraded_marker_error = ""
        return True

    def _restore_degraded_state(self) -> None:
        """Re-degrade a ledger that was corrupted by a previous process.

        Without this, the fail-closed guarantee was process-local: the truncated
        database passes integrity_check on the next start and dispatch was allowed
        again while we still do not know what was running when the data was lost.

        A marker that exists but cannot be read is treated as *degraded*: an
        unreadable marker is not evidence that the previous process was healthy.
        """
        if self.degraded:
            return
        try:
            if not self.degraded_marker.exists():
                return
        except OSError as exc:
            self.degraded = True
            self.degraded_reason = (
                f"cannot stat degraded marker {self.degraded_marker.name}: "
                f"{type(exc).__name__}: {exc}")
            print(f"[LEDGER DEGRADED] {self.degraded_reason}", file=sys.stderr)
            return
        try:
            prior = self.degraded_marker.read_text(
                encoding="utf-8", errors="replace").strip()
        except OSError as exc:
            self.degraded = True
            self.degraded_reason = (
                f"degraded marker {self.degraded_marker.name} exists but is unreadable "
                f"({type(exc).__name__}: {exc}); assuming the previous process was degraded")
            print(f"[LEDGER DEGRADED] {self.degraded_reason}", file=sys.stderr)
            return
        last_line = prior.splitlines()[-1] if prior else "unknown"
        self.degraded = True
        self.degraded_reason = (
            "degraded in a previous process (marker "
            f"{self.degraded_marker.name}); last record: {last_line}")
        print(f"[LEDGER DEGRADED] {self.degraded_reason}", file=sys.stderr)

    def _migrate_columns(self) -> None:
        """Add columns used by newer ledgers without discarding existing state."""
        tables = {
            "approvals": {"digest": "TEXT", "scope": "TEXT",
                          "consumed_at": "REAL", "consumed_by": "TEXT",
                          "reserved_by": "TEXT", "reserved_at": "REAL"},
            "work_items": {"adapter": "TEXT", "queue_status": "TEXT",
                           "priority": "INTEGER DEFAULT 0", "task_digest": "TEXT",
                           "dispatch_claim_owner": "TEXT", "dispatch_claimed_at": "REAL",
                           "project_id": "TEXT", "workspace_id": "TEXT",
                           "original_task": "TEXT", "effective_prompt": "TEXT",
                           "context_version": "INTEGER",
                           "context_entry_refs": "TEXT"},
        }
        for table, wanted in tables.items():
            have = {r["name"] for r in
                    self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for col, sql_type in wanted.items():
                if col not in have:
                    try:
                        self._conn.execute(
                            f"ALTER TABLE {table} ADD COLUMN {col} {sql_type}")
                    except sqlite3.Error:
                        pass
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_work_items_queue "
            "ON work_items(adapter, queue_status, priority DESC, created_at ASC)")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_work_items_digest ON work_items(task_digest)")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_work_items_project "
            "ON work_items(project_id, queue_status, priority DESC, created_at ASC)")
        self._conn.commit()
        self._backfill_default_workspaces()

    def _backfill_default_workspaces(self) -> None:
        """Map every pre-existing project onto its default local workspace.

        This is the compatibility half of the domain-model change: `projects.root`
        stays authoritative for path checks, and the workspace row is derived from
        it. A failure here must not lose data, so it is best-effort and idempotent;
        `ensure_default_workspace()` retries on demand.
        """
        try:
            rows = self._conn.execute(
                "SELECT id, root, default_adapter FROM projects").fetchall()
        except sqlite3.Error:
            return
        for row in rows:
            try:
                self._ensure_default_workspace_row(
                    row["id"], row["root"], row["default_adapter"])
            except sqlite3.Error:
                continue
        self._conn.commit()

    def _ensure_default_workspace_row(self, project_id: str, root: str,
                                      adapter: str | None) -> dict:
        existing = self._conn.execute(
            "SELECT * FROM workspaces WHERE project_id=? AND node_id='local' "
            "AND local_root=? ORDER BY is_default DESC LIMIT 1",
            (project_id, root)).fetchone()
        now = time.time()
        if existing:
            if not existing["is_default"]:
                self._conn.execute(
                    "UPDATE workspaces SET is_default=1, updated_at=? WHERE id=?",
                    (now, existing["id"]))
            return dict(existing)
        workspace_id = "ws-" + uuid.uuid4().hex[:10]
        self._conn.execute(
            "INSERT INTO workspaces (id, project_id, node_id, local_root, adapter,"
            " capabilities, is_default, active, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (workspace_id, project_id, "local", root, adapter,
             _dump({"execution": "local", "remote_dispatch": False}),
             1, 1, now, now))
        return {"id": workspace_id, "project_id": project_id, "node_id": "local",
                "local_root": root, "adapter": adapter, "is_default": 1,
                "active": 1, "created_at": now, "updated_at": now}

    def assert_writable(self, operation: str) -> None:
        """Fail closed: a degraded ledger may not mutate task state."""
        if self.degraded:
            raise LedgerDegraded(
                f"refusing {operation}: ledger is degraded ({self.degraded_reason}). "
                "Queries, diagnostics and export are still available; recover the ledger "
                "(recover_degraded) or start a new one before dispatching again.")

    @_synchronized
    def recover_degraded(self, operator_note: str = "") -> dict:
        """The ONLY operation that lifts a persistent degraded state.

        Intended as an explicit human action after reviewing the quarantined
        `*.corrupt-*` copy and accepting what was lost. An empty note is refused:
        lifting a fail-closed state must leave an auditable reason. The decision
        is appended to `LEDGER_RECOVERY.log` beside the ledger.
        """
        note = (operator_note or "").strip()
        if not note:
            raise ValueError("recover_degraded requires an operator note: the degraded "
                             "state exists because task history was lost; recovery must "
                             "be auditable")
        if not self.degraded:
            return {"recovered": False, "degraded": False, "note": note}
        audit = self.path.parent / "LEDGER_RECOVERY.log"
        # Review P0 (T27): recovery is only real if it is auditable AND the
        # persistent ban is actually gone. Both failures used to be swallowed,
        # which let the process report success while the next restart re-degraded
        # the ledger (marker still on disk) or while no audit trail existed.
        try:
            with audit.open("a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} recovered "
                         f"{self.path.name} note={note!r} "
                         f"prior_reason={self.degraded_reason!r}\n")
        except OSError as exc:
            raise LedgerDegraded(
                "refusing to lift the degraded state: the recovery audit log "
                f"{audit} is not writable ({type(exc).__name__}: {exc}). "
                "An unaudited recovery is indistinguishable from an accidental one."
            ) from exc
        try:
            self.degraded_marker.unlink(missing_ok=True)
        except OSError as exc:
            raise LedgerDegraded(
                "refusing to lift the degraded state: the persistent marker "
                f"{self.degraded_marker} cannot be removed "
                f"({type(exc).__name__}: {exc}). The ban would silently return on "
                "the next start, so the in-process state is left degraded too."
            ) from exc
        if self.degraded_marker.exists():
            raise LedgerDegraded(
                "refusing to lift the degraded state: the persistent marker "
                f"{self.degraded_marker} still exists after removal.")
        self.degraded = False
        self.degraded_reason = ""
        self.degraded_marker_error = ""
        return {"recovered": True, "degraded": False, "note": note}

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
            if not self._write_degraded_marker(
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')} legacy tasks.json unreadable: {exc}\n"
                    "manual recovery required: recover_degraded(operator_note=...).\n"):
                self.degraded_reason += (
                    "; WARNING: the degraded marker could not be persisted "
                    f"({self.degraded_marker_error}), so the dispatch ban is "
                    "process-local only and will NOT survive a restart")
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

    def new_project_id(self) -> str:
        return "prj-" + uuid.uuid4().hex[:10]

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

    @staticmethod
    def _row_to_project(row: sqlite3.Row) -> dict:
        project = dict(row)
        project["policy"] = _load(project.get("policy"), {})
        project["active"] = bool(project.get("active"))
        return project

    # ------------------------------------------------------------------ #
    # projects
    # ------------------------------------------------------------------ #
    @_synchronized
    def register_project(self, name: str, root: str, default_adapter: str,
                         *, policy: dict | None = None,
                         concurrency_limit: int = 2, active: bool = True) -> dict:
        """Create a project, or update the same name/root registration.

        Conflicting reuse of either a name or root is rejected explicitly. Path
        and Git validation belong to ``ControlPlane``; the store only guarantees
        durable uniqueness and atomic updates.
        """
        self.assert_writable("register_project")
        if concurrency_limit <= 0:
            raise ValueError("project concurrency_limit must be positive")
        by_name = self._conn.execute(
            "SELECT * FROM projects WHERE name=?", (name,)).fetchone()
        by_root = self._conn.execute(
            "SELECT * FROM projects WHERE root=?", (root,)).fetchone()
        if by_name and by_name["root"] != root:
            raise ValueError(f"project name already registered for another root: {name}")
        if by_root and by_root["name"] != name:
            raise ValueError(f"project root already registered as {by_root['name']}: {root}")

        now = time.time()
        existing = by_name or by_root
        if existing:
            project_id = existing["id"]
            self._conn.execute(
                "UPDATE projects SET default_adapter=?, policy=?, "
                "concurrency_limit=?, active=?, updated_at=? WHERE id=?",
                (default_adapter, _dump(policy or {}), concurrency_limit,
                 int(active), now, project_id))
        else:
            project_id = self.new_project_id()
            self._conn.execute(
                "INSERT INTO projects "
                "(id, name, root, default_adapter, policy, concurrency_limit, "
                "active, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (project_id, name, root, default_adapter, _dump(policy or {}),
                 concurrency_limit, int(active), now, now))
        self._ensure_default_workspace_row(project_id, root, default_adapter)
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        return self._row_to_project(row)

    @_synchronized
    def get_project(self, project_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        return self._row_to_project(row) if row else None

    @_synchronized
    def project_by_name(self, name: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM projects WHERE name=?", (name,)).fetchone()
        return self._row_to_project(row) if row else None

    @_synchronized
    def all_projects(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM projects ORDER BY name ASC").fetchall()
        return [self._row_to_project(row) for row in rows]

    @_synchronized
    def set_project_active(self, project_id: str, active: bool) -> dict:
        self.assert_writable("set_project_active")
        cur = self._conn.execute(
            "UPDATE projects SET active=?, updated_at=? WHERE id=?",
            (int(active), time.time(), project_id))
        if cur.rowcount != 1:
            self._conn.rollback()
            raise KeyError(f"no project {project_id}")
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        return self._row_to_project(row)

    # ------------------------------------------------------------------ #
    # workspaces / session references
    # ------------------------------------------------------------------ #
    @_synchronized
    def ensure_default_workspace(self, project_id: str) -> dict:
        """Return (creating if needed) the project's default local workspace."""
        project = self.get_project(project_id)
        if not project:
            raise KeyError(f"no project {project_id}")
        row = self._ensure_default_workspace_row(
            project_id, project["root"], project.get("default_adapter"))
        self._conn.commit()
        return self._row_to_workspace(row)

    @_synchronized
    def register_workspace(self, project_id: str, local_root: str, *,
                           node_id: str = "local", adapter: str | None = None,
                           capabilities: dict | None = None,
                           is_default: bool = False,
                           active: bool = True) -> dict:
        """Register an additional workspace for a project.

        Only ``node_id='local'`` is executable today: the control plane has no
        remote dispatch path, so a non-local node is stored as a *declaration*
        with ``remote_dispatch: False`` and is refused by
        :meth:`ControlPlane.resolve_workspace` until M5 lands.
        """
        self.assert_writable("register_workspace")
        if not self.get_project(project_id):
            raise KeyError(f"no project {project_id}")
        caps = {"execution": "local" if node_id == "local" else "declared",
                "remote_dispatch": False, **(capabilities or {})}
        now = time.time()
        existing = self._conn.execute(
            "SELECT * FROM workspaces WHERE project_id=? AND node_id=? AND local_root=?",
            (project_id, node_id, local_root)).fetchone()
        if existing:
            self._conn.execute(
                "UPDATE workspaces SET adapter=?, capabilities=?, active=?, updated_at=?"
                " WHERE id=?",
                (adapter, _dump(caps), int(active), now, existing["id"]))
            workspace_id = existing["id"]
        else:
            workspace_id = "ws-" + uuid.uuid4().hex[:10]
            self._conn.execute(
                "INSERT INTO workspaces (id, project_id, node_id, local_root, adapter,"
                " capabilities, is_default, active, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (workspace_id, project_id, node_id, local_root, adapter, _dump(caps),
                 0, int(active), now, now))
        if is_default:
            self._conn.execute(
                "UPDATE workspaces SET is_default=0 WHERE project_id=?", (project_id,))
            self._conn.execute(
                "UPDATE workspaces SET is_default=1 WHERE id=?", (workspace_id,))
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
        return self._row_to_workspace(row)

    @_synchronized
    def workspaces(self, project_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM workspaces WHERE project_id=? "
            "ORDER BY is_default DESC, created_at ASC", (project_id,)).fetchall()
        return [self._row_to_workspace(row) for row in rows]

    @_synchronized
    def default_workspace(self, project_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM workspaces WHERE project_id=? AND is_default=1 "
            "ORDER BY created_at ASC LIMIT 1", (project_id,)).fetchone()
        return self._row_to_workspace(row) if row else None

    @_synchronized
    def workspace_for_root(self, project_id: str, local_root: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM workspaces WHERE project_id=? AND local_root=? "
            "ORDER BY is_default DESC LIMIT 1", (project_id, local_root)).fetchone()
        return self._row_to_workspace(row) if row else None

    @staticmethod
    def _row_to_workspace(row: sqlite3.Row) -> dict:
        workspace = dict(row)
        workspace["capabilities"] = _load(workspace.get("capabilities"), {})
        workspace["is_default"] = bool(workspace.get("is_default"))
        workspace["active"] = bool(workspace.get("active"))
        return workspace

    @_synchronized
    def record_session_reference(self, project_id: str, *, workspace_id: str | None,
                                 worker_instance_id: str | None, adapter: str | None,
                                 session_id: str | None, work_item_id: str | None,
                                 attempt_id: str | None) -> dict:
        """Upsert the (project, session) link so continuity never depends on a live session."""
        self.assert_writable("record_session_reference")
        now = time.time()
        existing = None
        if session_id:
            existing = self._conn.execute(
                "SELECT * FROM session_references WHERE project_id=? AND session_id=? "
                "AND COALESCE(work_item_id,'')=? ORDER BY created_at DESC LIMIT 1",
                (project_id, session_id, work_item_id or "")).fetchone()
        if existing:
            self._conn.execute(
                "UPDATE session_references SET workspace_id=?, worker_instance_id=?,"
                " adapter=?, attempt_id=?, updated_at=? WHERE id=?",
                (workspace_id, worker_instance_id, adapter, attempt_id, now,
                 existing["id"]))
            ref_id = existing["id"]
        else:
            ref_id = "sr-" + uuid.uuid4().hex[:10]
            self._conn.execute(
                "INSERT INTO session_references (id, project_id, workspace_id,"
                " worker_instance_id, adapter, session_id, work_item_id, attempt_id,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ref_id, project_id, workspace_id, worker_instance_id, adapter,
                 session_id, work_item_id, attempt_id, now, now))
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM session_references WHERE id=?", (ref_id,)).fetchone()
        return dict(row)

    @_synchronized
    def session_references(self, project_id: str, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM session_references WHERE project_id=? "
            "ORDER BY created_at DESC LIMIT ?", (project_id, limit)).fetchall()
        return [dict(row) for row in rows]

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
            "adapter": record.get("adapter") or (record.get("metadata") or {}).get("adapter"),
            "queue_status": record.get("queue_status"),
            "priority": int(record.get("priority") or 0),
            "task_digest": record.get("task_digest"),
            "dispatch_claim_owner": record.get("dispatch_claim_owner"),
            "dispatch_claimed_at": record.get("dispatch_claimed_at"),
            "project_id": record.get("project_id"),
            "workspace_id": record.get("workspace_id"),
            "original_task": record.get("original_task"),
            "effective_prompt": record.get("effective_prompt"),
            "context_version": record.get("context_version"),
            "context_entry_refs": _dump(record.get("context_entry_refs") or []),
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
                # Approval-gated dispatch pre-creates an attempt so the approval
                # reservation has a durable owner before a Worker job id exists.
                if not self.bind_attempt_job(task_id, job_id):
                    self.add_attempt(task_id, job_id, reason="state-change")
        return self.get(task_id) or {}

    @_synchronized
    def get(self, task_id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM work_items WHERE id=?", (task_id,)).fetchone()
        return self._row_to_record(row) if row else None

    @_synchronized
    def all(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM work_items ORDER BY created_at ASC").fetchall()
        return [self._row_to_record(r) for r in rows]

    @_synchronized
    def by_job(self, worker_job_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT work_item_id FROM attempts WHERE worker_job_id=? ORDER BY created_at DESC LIMIT 1",
            (worker_job_id,)).fetchone()
        return self.get(row["work_item_id"]) if row else None

    @_synchronized
    def work_items_for_project(self, project_id: str, limit: int = 200) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM work_items WHERE project_id=? "
            "ORDER BY created_at DESC LIMIT ?", (project_id, limit)).fetchall()
        return [self._row_to_record(r) for r in rows]

    @_synchronized
    def by_idempotency_key(self, key: str) -> dict | None:
        if not key:
            return None
        row = self._conn.execute(
            "SELECT * FROM work_items WHERE idempotency_key=?", (key,)).fetchone()
        return self._row_to_record(row) if row else None

    @_synchronized
    def active_count(self, adapter: str, project_id: str | None = None) -> int:
        """Attempts occupying an Adapter concurrency slot."""
        sql = ("SELECT COUNT(*) FROM work_items WHERE adapter=? "
               "AND queue_status IN ('dispatching','running')")
        params: list[Any] = [adapter]
        if project_id:
            sql += " AND project_id=?"
            params.append(project_id)
        row = self._conn.execute(sql, params).fetchone()
        return int(row[0] if row else 0)

    @_synchronized
    def claim_next_ready(self, adapter: str, owner: str,
                         concurrency_limit: int) -> dict | None:
        """Atomically claim the highest-priority ready work item.

        ``BEGIN IMMEDIATE`` serializes this decision across independent control
        processes. The database uniqueness constraint still protects request
        idempotency; this method additionally prevents two runtimes from
        dispatching the same queued row.
        """
        if concurrency_limit <= 0:
            return None
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            active = self._conn.execute(
                "SELECT COUNT(*) FROM work_items WHERE adapter=? "
                "AND queue_status IN ('dispatching','running')", (adapter,)).fetchone()
            if int(active[0] if active else 0) >= concurrency_limit:
                self._conn.rollback()
                return None
            rows = self._conn.execute(
                "SELECT id, project_id FROM work_items "
                "WHERE adapter=? AND queue_status='ready' "
                "ORDER BY priority DESC, created_at ASC", (adapter,)).fetchall()
            selected = None
            for row in rows:
                project_id = row["project_id"]
                if project_id:
                    project = self._conn.execute(
                        "SELECT active, concurrency_limit FROM projects WHERE id=?",
                        (project_id,)).fetchone()
                    if not project or not bool(project["active"]):
                        continue
                    project_active = self._conn.execute(
                        "SELECT COUNT(*) FROM work_items WHERE project_id=? "
                        "AND queue_status IN ('dispatching','running')",
                        (project_id,)).fetchone()
                    if int(project_active[0] if project_active else 0) >= int(
                            project["concurrency_limit"]):
                        continue
                selected = row
                break
            if not selected:
                self._conn.rollback()
                return None
            claimed_at = time.time()
            cur = self._conn.execute(
                "UPDATE work_items SET queue_status='dispatching', "
                "dispatch_claim_owner=?, dispatch_claimed_at=?, updated_at=? "
                "WHERE id=? AND queue_status='ready'",
                (owner, claimed_at, claimed_at, selected["id"]))
            if cur.rowcount != 1:
                self._conn.rollback()
                return None
            self._conn.commit()
            return self.get(selected["id"])
        except Exception:
            self._conn.rollback()
            raise

    @_synchronized
    def release_approved_queue(self, digest: str) -> int:
        """Make approved, content-bound queued work eligible for dispatch."""
        cur = self._conn.execute(
            "UPDATE work_items SET queue_status='ready', updated_at=? "
            "WHERE task_digest=? AND queue_status='needs_approval'",
            (time.time(), digest))
        self._conn.commit()
        return cur.rowcount

    @_synchronized
    def clear(self) -> None:
        for table in ("approvals", "evidence", "events", "attempts",
                      "session_references", "workspaces",
                      "context_update_receipts",
                      "project_context_revisions", "project_context_entries",
                      "project_context_state", "work_items", "projects"):
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
        """Attach a Worker job to the current pre-dispatch attempt, if any."""
        rec = self.get(task_id) or {}
        attempt_id = rec.get("current_attempt_id")
        if not attempt_id:
            return False
        cur = self._conn.execute(
            "UPDATE attempts SET worker_job_id=? "
            "WHERE id=? AND worker_job_id IS NULL",
            (worker_job_id, attempt_id))
        self._conn.commit()
        return cur.rowcount == 1

    @_synchronized
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
    def mark_attempt(self, attempt_id: str, state: str) -> None:
        """Set an attempt's state without ending it (e.g. `dispatched`,
        `dispatch_unknown`): the outcome is known, the attempt is still open."""
        self._conn.execute("UPDATE attempts SET state=? WHERE id=?",
                           (state, attempt_id))
        self._conn.commit()

    @_synchronized
    def append_event(self, task_id: str, type_: str, payload: dict | None = None,
                     attempt_id: str | None = None) -> None:
        self._conn.execute(
            "INSERT INTO events (work_item_id, attempt_id, type, payload, created_at)"
            " VALUES (?,?,?,?,?)",
            (task_id, attempt_id, type_, _dump(payload or {}), time.time()))
        self._conn.commit()

    @_synchronized
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

    @_synchronized
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

    @_synchronized
    def all_approvals(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM approvals ORDER BY id ASC").fetchall()
        return [dict(r) for r in rows]

    @_synchronized
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
        """Decide only a still-pending, unconsumed approval."""
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

    @_synchronized
    def valid_approval_by_digest(self, digest: str) -> dict | None:
        """Approval is bound to content, not to a work item that does not exist yet."""
        row = self._conn.execute(
            "SELECT * FROM approvals WHERE digest=? AND decision='approved'"
            " AND consumed_at IS NULL AND reserved_at IS NULL "
            "ORDER BY id DESC LIMIT 1", (digest,)).fetchone()
        return dict(row) if row else None

    @_synchronized
    def valid_approval(self, task_id: str, digest: str) -> dict | None:
        """An unconsumed, unreserved approval matching this exact content."""
        row = self._conn.execute(
            "SELECT * FROM approvals WHERE work_item_id=? AND digest=? AND decision='approved'"
            " AND consumed_at IS NULL AND reserved_at IS NULL "
            "ORDER BY id DESC LIMIT 1", (task_id, digest)).fetchone()
        return dict(row) if row else None

    @_synchronized
    def approval_by_id(self, approval_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        return dict(row) if row else None

    @_synchronized
    def reserve_approval(self, digest: str, attempt_id: str) -> dict | None:
        """Atomically reserve one approved digest for one dispatch attempt."""
        row = self._conn.execute(
            "SELECT * FROM approvals WHERE digest=? AND decision='approved'"
            " AND consumed_at IS NULL AND reserved_at IS NULL "
            "ORDER BY id DESC LIMIT 1", (digest,)).fetchone()
        if not row:
            return None
        now = time.time()
        cur = self._conn.execute(
            "UPDATE approvals SET reserved_by=?, reserved_at=? "
            "WHERE id=? AND reserved_at IS NULL AND consumed_at IS NULL",
            (attempt_id, now, row["id"]))
        self._conn.commit()
        if cur.rowcount != 1:
            return None
        out = dict(row)
        out.update(reserved_by=attempt_id, reserved_at=now)
        return out

    @_synchronized
    def consume_reserved(self, approval_id: int, attempt_id: str,
                         worker_job_id: str | None) -> bool:
        """Consume a reservation; only its owning attempt may do so."""
        cur = self._conn.execute(
            "UPDATE approvals SET consumed_at=?, consumed_by=? "
            "WHERE id=? AND reserved_by=? AND consumed_at IS NULL",
            (time.time(), worker_job_id or attempt_id, approval_id, attempt_id))
        self._conn.commit()
        return cur.rowcount == 1

    @_synchronized
    def release_reservation(self, approval_id: int, attempt_id: str) -> bool:
        """Release only after it is proven that no Worker job was created."""
        cur = self._conn.execute(
            "UPDATE approvals SET reserved_by=NULL, reserved_at=NULL "
            "WHERE id=? AND reserved_by=? AND consumed_at IS NULL",
            (approval_id, attempt_id))
        self._conn.commit()
        return cur.rowcount == 1

    @_synchronized
    def consume_approval(self, approval_id: int, attempt_id: str | None) -> bool:
        """Single-use: returns False if it was already consumed (race-safe)."""
        cur = self._conn.execute(
            "UPDATE approvals SET consumed_at=?, consumed_by=? WHERE id=? AND consumed_at IS NULL",
            (time.time(), attempt_id, approval_id))
        self._conn.commit()
        return cur.rowcount == 1

    # ------------------------------------------------------------------ #
    # project context (low-level; the API lives in controlplane.project_context)
    # ------------------------------------------------------------------ #
    @_synchronized
    def context_version(self, project_id: str) -> int:
        """Monotonic per-project context version (0 when nothing was written)."""
        row = self._conn.execute(
            "SELECT version FROM project_context_state WHERE project_id=?",
            (project_id,)).fetchone()
        return int(row["version"]) if row else 0

    def _bump_context_version(self, project_id: str) -> int:
        now = time.time()
        row = self._conn.execute(
            "SELECT version FROM project_context_state WHERE project_id=?",
            (project_id,)).fetchone()
        version = (int(row["version"]) if row else 0) + 1
        if row:
            self._conn.execute(
                "UPDATE project_context_state SET version=?, updated_at=? "
                "WHERE project_id=?", (version, now, project_id))
        else:
            self._conn.execute(
                "INSERT INTO project_context_state (project_id, version, updated_at)"
                " VALUES (?,?,?)", (project_id, version, now))
        return version

    @_synchronized
    def insert_context_entry(self, entry: dict) -> dict:
        """Insert one entry and bump the project context version atomically."""
        self.assert_writable("insert_context_entry")
        now = time.time()
        entry_id = entry.get("id") or ("ctx-" + uuid.uuid4().hex[:12])
        version = self._bump_context_version(entry["project_id"])
        self._conn.execute(
            "INSERT INTO project_context_entries (id, project_id, kind, title, body,"
            " tags, verification, security_domain, source, evidence_refs, version,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (entry_id, entry["project_id"], entry["kind"], entry["title"],
             entry["body"], _dump(entry.get("tags") or []),
             entry["verification"], entry["security_domain"],
             entry.get("source") or "", _dump(entry.get("evidence_refs") or []),
             version, now, now))
        self._conn.commit()
        return self.get_context_entry(entry_id) or {}

    @_synchronized
    def get_context_entry(self, entry_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM project_context_entries WHERE id=?", (entry_id,)).fetchone()
        return self._row_to_context_entry(row) if row else None

    @_synchronized
    def context_entry_by_title(self, project_id: str, kind: str,
                               title: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM project_context_entries WHERE project_id=? AND kind=?"
            " AND title=?", (project_id, kind, title)).fetchone()
        return self._row_to_context_entry(row) if row else None

    @_synchronized
    def context_entries(self, project_id: str, *, kind: str | None = None,
                        include_shared: bool = True) -> list[dict]:
        sql = "SELECT * FROM project_context_entries WHERE project_id=?"
        params: list[Any] = [project_id]
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        if not include_shared:
            sql += " AND security_domain='local'"
        sql += " ORDER BY kind ASC, updated_at DESC"
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_context_entry(r) for r in rows]

    @_synchronized
    def update_context_entry(self, entry_id: str, expected_version: int,
                             changes: dict, *, changed_by: str = "",
                             note: str = "") -> dict | None:
        """Compare-and-swap update. Returns None when the version does not match.

        The revision row is written in the same transaction as the update, so a
        successful CAS always leaves an auditable snapshot of the previous state.
        """
        self.assert_writable("update_context_entry")
        row = self._conn.execute(
            "SELECT * FROM project_context_entries WHERE id=?", (entry_id,)).fetchone()
        if not row:
            raise KeyError(f"no context entry {entry_id}")
        if int(row["version"]) != int(expected_version):
            return None
        previous = self._row_to_context_entry(row)
        allowed = {"kind", "title", "body", "tags", "verification",
                   "security_domain", "source", "evidence_refs"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError("unsupported context fields: " + ", ".join(sorted(unknown)))
        now = time.time()
        new_version = self._bump_context_version(row["project_id"])
        sets, values = [], {}
        for key, value in changes.items():
            if key in ("tags", "evidence_refs"):
                values[key] = _dump(value or [])
            else:
                values[key] = value
            sets.append(f"{key} = :{key}")
        values["version"] = new_version
        values["updated_at"] = now
        values["id"] = entry_id
        sets += ["version = :version", "updated_at = :updated_at"]
        cur = self._conn.execute(
            f"UPDATE project_context_entries SET {', '.join(sets)} "
            "WHERE id = :id AND version = :expected", {**values,
                                                       "expected": expected_version})
        if cur.rowcount != 1:
            self._conn.rollback()
            return None
        self._conn.execute(
            "INSERT INTO project_context_revisions (entry_id, project_id, version,"
            " snapshot, changed_at, changed_by, note) VALUES (?,?,?,?,?,?,?)",
            (entry_id, row["project_id"], expected_version, _dump(previous),
             now, changed_by, note))
        self._conn.commit()
        return self.get_context_entry(entry_id)

    @_synchronized
    def context_revisions(self, entry_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM project_context_revisions WHERE entry_id=? "
            "ORDER BY version ASC", (entry_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["snapshot"] = _load(d.get("snapshot"), {})
            out.append(d)
        return out

    # ------------------------------------------------------------------ #
    # project context update loop (M2)
    # ------------------------------------------------------------------ #
    @_synchronized
    def claim_context_update(self, idempotency_key: str, *, project_id: str,
                             work_item_id: str, attempt_id: str,
                             entry_kind: str, entry_title: str, owner: str,
                             lease_s: float = 60.0) -> dict:
        """Claim one deterministic M2 write across threads and processes.

        Completed receipts are immutable replay guards. A crashed ``processing``
        claim may be taken over after ``lease_s``; the update loop also embeds a
        marker in the target entry, so recovery after "entry committed, receipt
        not committed" does not apply the same candidate twice.
        """
        self.assert_writable("claim_context_update")
        now = time.time()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT * FROM context_update_receipts WHERE idempotency_key=?",
                (idempotency_key,)).fetchone()
            if row:
                current = self._row_to_context_update_receipt(row)
                if current["status"] in ("applied", "needs_merge", "skipped"):
                    self._conn.commit()
                    return {**current, "claimed": False, "replayed": True}
                age = now - float(current.get("updated_at") or 0)
                if (current["status"] == "processing"
                        and current.get("owner") != owner and age < lease_s):
                    self._conn.commit()
                    return {**current, "claimed": False, "replayed": False,
                            "busy": True, "claim_age_s": round(age, 3)}
                self._conn.execute(
                    "UPDATE context_update_receipts SET status='processing', owner=?, "
                    "updated_at=? WHERE idempotency_key=?",
                    (owner, now, idempotency_key))
            else:
                self._conn.execute(
                    "INSERT INTO context_update_receipts "
                    "(idempotency_key, project_id, work_item_id, attempt_id, "
                    "entry_kind, entry_title, entry_id, status, owner, payload, "
                    "created_at, updated_at) VALUES (?,?,?,?,?,?,NULL,'processing',?,NULL,?,?)",
                    (idempotency_key, project_id, work_item_id, attempt_id,
                     entry_kind, entry_title, owner, now, now))
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        claimed = self._conn.execute(
            "SELECT * FROM context_update_receipts WHERE idempotency_key=?",
            (idempotency_key,)).fetchone()
        return {**self._row_to_context_update_receipt(claimed),
                "claimed": True, "replayed": False}

    @_synchronized
    def complete_context_update(self, idempotency_key: str, *, owner: str,
                                status: str, entry_id: str | None,
                                payload: dict | None = None) -> dict:
        if status not in ("applied", "needs_merge", "skipped"):
            raise ValueError(f"invalid context update status: {status}")
        now = time.time()
        cur = self._conn.execute(
            "UPDATE context_update_receipts SET status=?, entry_id=?, payload=?, "
            "owner=NULL, updated_at=? WHERE idempotency_key=? AND owner=? "
            "AND status='processing'",
            (status, entry_id, _dump(payload or {}), now, idempotency_key, owner))
        self._conn.commit()
        if cur.rowcount != 1:
            row = self._conn.execute(
                "SELECT * FROM context_update_receipts WHERE idempotency_key=?",
                (idempotency_key,)).fetchone()
            if not row:
                raise KeyError(f"no context update receipt {idempotency_key}")
            current = self._row_to_context_update_receipt(row)
            if current["status"] not in ("applied", "needs_merge", "skipped"):
                raise RuntimeError(
                    f"context update claim {idempotency_key} is no longer owned by {owner}")
            return current
        row = self._conn.execute(
            "SELECT * FROM context_update_receipts WHERE idempotency_key=?",
            (idempotency_key,)).fetchone()
        return self._row_to_context_update_receipt(row)

    @_synchronized
    def fail_context_update(self, idempotency_key: str, *, owner: str,
                            error: str) -> None:
        self._conn.execute(
            "UPDATE context_update_receipts SET status='failed', owner=NULL, "
            "payload=?, updated_at=? WHERE idempotency_key=? AND owner=?",
            (_dump({"error": error}), time.time(), idempotency_key, owner))
        self._conn.commit()

    @_synchronized
    def context_update_receipts(self, work_item_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM context_update_receipts WHERE work_item_id=? "
            "ORDER BY created_at ASC, entry_kind ASC",
            (work_item_id,)).fetchall()
        return [self._row_to_context_update_receipt(row) for row in rows]

    @staticmethod
    def _row_to_context_update_receipt(row: sqlite3.Row | None) -> dict:
        if row is None:
            return {}
        receipt = dict(row)
        receipt["payload"] = _load(receipt.get("payload"), {})
        return receipt

    @staticmethod
    def _row_to_context_entry(row: sqlite3.Row) -> dict:
        entry = dict(row)
        entry["tags"] = _load(entry.get("tags"), [])
        entry["evidence_refs"] = _load(entry.get("evidence_refs"), [])
        return entry

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
