"""Guarded removal of a *scratch* SQLite ledger - never a live one.

Why this exists
---------------
T62-F07: an earlier cleanup did `LEDGER.unlink()` on a ledger whose `-wal` still
held the finished contrast rows. Deleting only the main file left `-wal`/`-shm`
behind pointing at a database that no longer had them applied, so the ledger
silently "reverted" to its queued state and the evidence file no longer matched
any live row.

The rules encoded here:

  * a ledger that still has live rows, a `-wal` with content, or an open handle
    is NOT scratch, and this module refuses to touch it;
  * SQLite never has its `-wal`/`-shm` deleted by hand - they are removed by
    SQLite itself through `PRAGMA wal_checkpoint(TRUNCATE)` followed by closing
    every connection cleanly;
  * the caller may only delete the *main* file, and only when the checkpoint
    reported that nothing is left in the WAL.

Nothing here reads or rewrites historical data; it only decides whether a file
is safe to drop.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


class LedgerInUse(RuntimeError):
    """The ledger is live (or may be): deleting it would lose real history."""


@dataclass
class CleanupReport:
    path: str
    actions: list[str] = field(default_factory=list)
    deleted: bool = False
    wal_bytes: int = 0
    live_rows: int = 0


def sidecars(ledger: Path) -> list[Path]:
    return [Path(f"{ledger}{suffix}") for suffix in ("-wal", "-shm")]


def wal_size(ledger: Path) -> int:
    wal = Path(f"{ledger}-wal")
    return wal.stat().st_size if wal.exists() else 0


def live_row_count(ledger: Path) -> int:
    """Number of work items in a terminal-with-verdict or running state."""
    if not ledger.exists():
        return 0
    try:
        conn = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        raise LedgerInUse(f"cannot open {ledger.name} read-only: {exc}") from exc
    try:
        return int(conn.execute(
            "select count(*) from work_items where verdict is not null "
            "and verdict not in ('', 'pending')").fetchone()[0])
    except sqlite3.Error:
        # Unknown schema: refuse to call it scratch rather than assume it is.
        raise LedgerInUse(
            f"{ledger.name} has no readable work_items/verdict column")
    finally:
        conn.close()


def checkpoint(ledger: Path) -> tuple[bool, int]:
    """Flush the WAL into the main file through SQLite, then close cleanly.

    Returns (busy, wal_bytes). `busy` is SQLite's own answer to "could this
    connection take the write lock and truncate?" - a live reader/writer (an open
    runtime or a running Worker) makes it non-zero, which is exactly the signal
    we need instead of guessing from file sizes.
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


def remove_scratch_ledger(ledger: Path, *, allow_pending: bool = True) -> CleanupReport:
    """Delete a scratch ledger safely, or raise `LedgerInUse`.

    `allow_pending` permits queued/unverified rows (a scratch ledger created for
    the next run), but never a ledger that holds verdicts.
    """
    ledger = Path(ledger)
    report = CleanupReport(path=str(ledger))
    if not ledger.exists():
        report.actions.append("nothing to do: ledger does not exist")
        return report

    try:
        report.live_rows = live_row_count(ledger)
    except LedgerInUse:
        report.actions.append("refused: schema not understood")
        raise
    if report.live_rows and not allow_pending:
        report.actions.append(
            f"refused: {report.live_rows} row(s) carry a verdict")
        raise LedgerInUse(
            f"{ledger.name} holds {report.live_rows} verdict(s); it is evidence, "
            "not scratch - archive it instead of deleting it")

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
            f"{ledger.name} cannot be removed: {exc}. A handle is still open.")
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
