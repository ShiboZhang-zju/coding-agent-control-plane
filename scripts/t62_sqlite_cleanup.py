"""Guarded removal of a *scratch* SQLite ledger - never a live one.

Why this exists
---------------
T62-F07: an earlier cleanup did `LEDGER.unlink()` on a ledger whose `-wal` still
held the finished contrast rows. Deleting only the main file left `-wal`/`-shm`
behind pointing at a database that no longer had them applied, so the ledger
silently "reverted" to its queued state and the evidence file no longer matched
any live row.

The rules encoded here:

  * a ledger that holds a verdict, holds a non-pending row, has a `-wal` with
    content, or has an open handle is NOT scratch, and this module refuses to
    touch it. **The refusal is unconditional** - it is not a parameter a caller
    can relax, because the one caller that forgot to pass it would recreate
    exactly the incident this module exists to prevent;
  * SQLite never has its `-wal`/`-shm` deleted by hand - they are removed by
    SQLite itself through `PRAGMA wal_checkpoint(TRUNCATE)` followed by closing
    every connection cleanly;
  * the caller may only delete the *main* file, and only when the checkpoint
    reported that nothing is left in the WAL;
  * a clean checkpoint proves "no connection is holding this file **right now**",
    not "no Worker is running against it". It is one signal among several, never
    the deciding one.

Preferred path: don't delete at all. `fresh_ledger_path()` gives each run its
own file and the old one stays as an archive.

Nothing here reads or rewrites historical data; it only decides whether a file
is safe to drop.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

# A verdict means a task was accepted or rejected: that is evidence, not scratch.
TERMINAL_VERDICTS = ("verified", "rejected", "inconclusive")

# Rows that are allowed to exist in a ledger we may still delete: never started,
# nothing decided. Everything else - running, lost, dispatched-unknown, or a
# state we do not recognise - blocks deletion.
SCRATCH_SAFE_STATES = ("", "queued", "ready", "pending", "needs_approval")

# States that prove work is (or may be) in flight right now.
ACTIVE_STATES = ("starting", "running", "waiting_for_approval", "dispatch_unknown")


class LedgerInUse(RuntimeError):
    """The ledger is live (or may be): deleting it would lose real history."""


@dataclass
class LedgerInspection:
    path: str
    exists: bool = True
    total_rows: int = 0
    verdict_rows: int = 0
    active_rows: int = 0
    pending_rows: int = 0
    unrecognised_rows: int = 0
    states: dict[str, int] = field(default_factory=dict)
    queue_statuses: dict[str, int] = field(default_factory=dict)
    schema_readable: bool = True

    @property
    def is_scratch(self) -> bool:
        """True only when every row is undecided and nothing is in flight."""
        return (self.exists and self.schema_readable
                and self.verdict_rows == 0
                and self.active_rows == 0
                and self.unrecognised_rows == 0)

    def blockers(self) -> list[str]:
        out: list[str] = []
        if not self.exists:
            return out
        if not self.schema_readable:
            out.append("schema not understood (no readable work_items/verdict)")
        if self.verdict_rows:
            out.append(f"{self.verdict_rows} row(s) carry a verdict")
        if self.active_rows:
            in_flight = sorted(k for k, v in self.states.items()
                               if v and k in ACTIVE_STATES)
            detail = f" ({', '.join(in_flight)})" if in_flight else ""
            note = self.dispatch_note()
            out.append(f"{self.active_rows} row(s) are in flight{detail}"
                       + (f"; {note}" if note else ""))
        if self.unrecognised_rows:
            out.append(f"{self.unrecognised_rows} row(s) have an unrecognised state")
        return out

    def dispatch_note(self) -> str:
        """Describe queue_status, which `state` alone does not capture."""
        dispatching = self.queue_statuses.get("dispatching", 0)
        if not dispatching:
            return ""
        return (f"{dispatching} row(s) are queue_status='dispatching' - claimed "
                "for dispatch while still state='queued'; this is in-flight work")


@dataclass
class CleanupReport:
    path: str
    actions: list[str] = field(default_factory=list)
    deleted: bool = False
    wal_bytes: int = 0
    inspection: LedgerInspection | None = None

    @property
    def live_rows(self) -> int:
        """Rows that make the ledger evidence: verdicts plus in-flight work."""
        if self.inspection is None:
            return 0
        return self.inspection.verdict_rows + self.inspection.active_rows


def sidecars(ledger: Path) -> list[Path]:
    return [Path(f"{ledger}{suffix}") for suffix in ("-wal", "-shm")]


def wal_size(ledger: Path) -> int:
    wal = Path(f"{ledger}-wal")
    return wal.stat().st_size if wal.exists() else 0


def inspect_ledger(ledger: Path) -> LedgerInspection:
    """Read-only census of a ledger: what is in it, and is any of it evidence?"""
    ledger = Path(ledger)
    report = LedgerInspection(path=str(ledger))
    if not ledger.exists():
        report.exists = False
        return report

    try:
        conn = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        raise LedgerInUse(f"cannot open {ledger.name} read-only: {exc}") from exc
    try:
        # queue_status matters: `claim_next_ready()` moves it to 'dispatching'
        # while `state` is still 'queued', so a census on (state, verdict) alone
        # would call an already-claimed ledger scratch. It is a separate column
        # and may be absent in older/partial schemas, hence the fallback.
        try:
            rows = conn.execute(
                "select state, verdict, queue_status from work_items").fetchall()
        except sqlite3.Error:
            rows = [(state, verdict, None) for state, verdict in
                    conn.execute("select state, verdict from work_items").fetchall()]
    except sqlite3.Error as exc:
        # Unknown schema: refuse to call it scratch rather than assume it is.
        report.schema_readable = False
        raise LedgerInUse(
            f"{ledger.name} has no readable work_items/verdict columns: {exc}"
        ) from exc
    finally:
        conn.close()

    for state, verdict, queue_status in rows:
        state = (state or "").strip()
        verdict = (verdict or "").strip()
        queue_status = (queue_status or "").strip()
        report.total_rows += 1
        report.states[state or "<empty>"] = report.states.get(state or "<empty>", 0) + 1
        if queue_status:
            report.queue_statuses[queue_status] = (
                report.queue_statuses.get(queue_status, 0) + 1)
        if verdict and verdict != "pending":
            report.verdict_rows += 1
        elif queue_status == "dispatching":
            # Claimed for dispatch but not dispatched yet: in flight.
            report.active_rows += 1
        elif state in ACTIVE_STATES:
            report.active_rows += 1
        elif state in SCRATCH_SAFE_STATES:
            report.pending_rows += 1
        else:
            # succeeded/failed/cancelled/lost with no verdict, or a state we do
            # not know: either way, real work happened here.
            report.unrecognised_rows += 1
    return report


def live_row_count(ledger: Path) -> int:
    """Rows that make the ledger evidence (verdicts + in-flight + unknown)."""
    report = inspect_ledger(ledger)
    return report.verdict_rows + report.active_rows + report.unrecognised_rows


def checkpoint(ledger: Path) -> tuple[bool, int]:
    """Flush the WAL into the main file through SQLite, then close cleanly.

    Returns (busy, wal_bytes). `busy` is SQLite's own answer to "could this
    connection take the write lock and truncate?". It catches an open *handle*;
    it does NOT prove no Worker is running (a Worker between two calls holds no
    handle), so it is never the only guard.
    """
    conn = sqlite3.connect(str(ledger), timeout=15)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        busy = bool(row and row[0])
    finally:
        conn.close()
    return busy, wal_size(ledger)


def remove_scratch_ledger(ledger: Path) -> CleanupReport:
    """Delete a scratch ledger safely, or raise `LedgerInUse`.

    The safety check has no opt-out: a ledger holding a verdict, holding
    in-flight work, or holding a state this module does not understand is
    refused on every call path, including the default one. Use
    `fresh_ledger_path()` when you want a new ledger and want to keep the old.
    """
    ledger = Path(ledger)
    report = CleanupReport(path=str(ledger))
    if not ledger.exists():
        report.actions.append("nothing to do: ledger does not exist")
        return report

    try:
        report.inspection = inspect_ledger(ledger)
    except LedgerInUse as exc:
        report.actions.append("refused: schema not understood")
        raise LedgerInUse(str(exc)) from exc

    blockers = report.inspection.blockers()
    if blockers:
        report.actions.append("refused: " + "; ".join(blockers))
        raise LedgerInUse(
            f"{ledger.name} is evidence, not scratch ({'; '.join(blockers)}). "
            "Archive it and start the next pair on a fresh ledger "
            "(`fresh_ledger_path()`); never delete a ledger that carries a "
            "verdict or in-flight work.")

    busy, report.wal_bytes = checkpoint(ledger)
    if busy or report.wal_bytes:
        report.actions.append(
            f"refused: checkpoint busy={busy}, WAL holds {report.wal_bytes} bytes")
        raise LedgerInUse(
            f"{ledger.name}: checkpoint could not take the write lock "
            f"(busy={busy}, wal={report.wal_bytes} bytes). An open connection or "
            "a running Worker is holding it. Stop them first - never delete the "
            "-wal by hand.")

    # Only the main file is removed. -wal/-shm are gone because SQLite truncated
    # them at checkpoint time, which is the only supported way.
    try:
        ledger.unlink()
    except OSError as exc:
        report.actions.append(f"refused: main file is still locked ({exc})")
        raise LedgerInUse(
            f"{ledger.name} cannot be removed: {exc}. A handle is still open."
        ) from exc
    report.deleted = True
    report.actions.append("checkpointed, then removed the main file only")
    for leftover in sidecars(ledger):
        if leftover.exists() and leftover.stat().st_size == 0:
            report.actions.append(
                f"left {leftover.name} in place (0 bytes, already truncated)")
    return report


def fresh_ledger_path(ledger: Path) -> Path:
    """Preferred path: a new per-run ledger, so nothing has to be deleted."""
    ledger = Path(ledger)
    for index in range(1, 1000):
        candidate = ledger.with_name(f"{ledger.stem}-run{index}{ledger.suffix}")
        if not candidate.exists() and not Path(f"{candidate}-wal").exists():
            return candidate
    raise RuntimeError(f"cannot find a free ledger name next to {ledger}")
