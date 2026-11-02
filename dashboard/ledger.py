"""Read-only wiring for the UI-0 dashboard.

Why this module exists
----------------------
``ProjectService`` is the right place to *compute* project state, but it is
constructed around a ``ControlPlane``, which in turn expects a ``TaskStore``
whose historical constructor creates the schema, migrates columns, backfills
default workspaces and imports legacy JSON.  Instantiating that on every HTTP
request would (a) write to the ledger and (b) race a live Runtime.

So the dashboard wires the *same* objects with a read-only store:

    TaskStore(read_only=True)          -> mode=ro + PRAGMA query_only=ON
      -> ControlPlane(NameOnlyAdapter) -> never touches a Worker
        -> ProjectService              -> the exact projections the CLI uses
    LDGoalStore(read_only=True)        -> pre-existing read-only path

``NameOnlyAdapter`` mirrors ``AdapterRef`` in ``scripts/controlplane_cli.py``:
the control plane needs an adapter *name* for a few query paths, and a name is
all it can ever get here.  Every method that would call a Worker is left
unimplemented, so an accidental dispatch attempt raises instead of doing I/O.

Connections are short-lived
---------------------------
The server opens one read-only "session" per HTTP request and closes it in a
``finally``.  A long-lived connection would hold a WAL read snapshot open and
could stop the Runtime's checkpoints from advancing; per-request connections
mean a refresh always sees the newest committed state and leaves nothing
behind.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from controlplane.plane import ControlPlane
from controlplane.project_service import ProjectService
from controlplane.store import LedgerSchemaError, TaskStore
from ld.goal_store import LDGoalStore

#: Tables a ledger must have for the dashboard to render honest numbers.
#: ``ld_goals`` is checked separately because a ledger that predates M4-B is
#: still readable — it simply has no goals, which the UI shows as "no goals".
REQUIRED_TABLES = ("projects", "work_items", "workspaces", "session_references",
                   "attempts", "project_context_entries", "project_context_state")


class DashboardError(RuntimeError):
    """Base class for errors the HTTP layer maps to a specific status."""


class LedgerMissing(DashboardError):
    """The configured ledger file does not exist (it is never created)."""


class LedgerUnreadable(DashboardError):
    """The ledger exists but cannot be opened or read."""


class ProjectNotFound(DashboardError):
    """No project with that id in this ledger."""


class SchemaIncompatible(DashboardError):
    """The ledger carries an unexpected/incomplete schema."""


class LedgerBusy(DashboardError):
    """The database is locked by a writer right now; retrying may help."""


class NameOnlyAdapter:
    """An adapter that can only report its name.

    Any call that would reach a Worker is absent by construction: there is no
    ``dispatch``/``reconcile``/``collect_artifacts`` here, so a code path that
    tried to use one fails loudly with ``AttributeError`` during development
    rather than quietly contacting a real Worker in production.
    """

    def __init__(self, name: str = "dashboard-readonly"):
        self.name = name


class ReadOnlyLedger:
    """Factory for short-lived read-only control-plane sessions."""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    # -- error translation ------------------------------------------------ #
    def _translate(self, exc: BaseException) -> DashboardError:
        if isinstance(exc, DashboardError):
            return exc
        text = str(exc)
        if isinstance(exc, LedgerSchemaError):
            return SchemaIncompatible(text)
        if isinstance(exc, sqlite3.OperationalError):
            if "locked" in text or "busy" in text:
                return LedgerBusy(text)
            return LedgerUnreadable(text)
        if isinstance(exc, sqlite3.DatabaseError):
            # "file is not a database", "malformed", "disk image is malformed"
            return LedgerUnreadable(text)
        return LedgerUnreadable(f"{type(exc).__name__}: {text}")

    # -- session ---------------------------------------------------------- #
    @contextmanager
    def session(self) -> Iterator["LedgerSession"]:
        """Open one read-only session; close it no matter what happens."""
        if not self.path.exists():
            # Explicit: SQLite's mode=ro would raise "unable to open database
            # file", which does not distinguish "missing" from "bad path".
            raise LedgerMissing(f"ledger does not exist: {self.path}")
        store: TaskStore | None = None
        goals: LDGoalStore | None = None
        try:
            store = TaskStore(self.path, read_only=True)
            store.require_schema(REQUIRED_TABLES)
            plane = ControlPlane(NameOnlyAdapter(), store)
            service = ProjectService(plane)
            goals = self._open_goals()
            yield LedgerSession(store=store, service=service, goals=goals,
                                path=self.path)
        except DashboardError:
            raise
        except (sqlite3.Error, OSError, LedgerSchemaError, ValueError) as exc:
            raise self._translate(exc) from None
        finally:
            for closable in (goals, store):
                if closable is not None:
                    try:
                        closable.close()
                    except Exception:  # noqa: BLE001 - closing must not mask errors
                        pass

    def _open_goals(self) -> LDGoalStore | None:
        """Goal store, or ``None`` when this ledger predates the M4-B tables.

        A missing ``ld_goals`` table is not an error: it means the ledger has no
        goals yet, and the UI must show "no goals" rather than "incompatible".
        """
        goals = LDGoalStore(self.path, read_only=True)
        try:
            tables = {row["name"] for row in goals._conn.execute(  # noqa: SLF001
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        except sqlite3.Error:
            goals.close()
            raise
        if "ld_goals" not in tables:
            goals.close()
            return None
        return goals


class LedgerSession:
    """One read-only view of the ledger for the duration of a request."""

    def __init__(self, *, store: TaskStore, service: ProjectService,
                 goals: LDGoalStore | None, path: Path):
        self.store = store
        self.service = service
        self.goals = goals
        self.path = path

    def has_goals_table(self) -> bool:
        return self.goals is not None

    def goal_rows(self) -> list[dict]:
        return self.goals.all_goals() if self.goals else []

    def goal_rows_for(self, project_id: str) -> list[dict]:
        return self.goals.find_by_project(project_id) if self.goals else []


__all__ = [
    "DashboardError",
    "LedgerBusy",
    "LedgerMissing",
    "LedgerSession",
    "LedgerUnreadable",
    "NameOnlyAdapter",
    "ProjectNotFound",
    "ReadOnlyLedger",
    "SchemaIncompatible",
]
