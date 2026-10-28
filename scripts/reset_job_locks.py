"""Move stale CodeBuddy job-store lock files aside (reversible) and re-measure.

Why: every endpoint that touches the job store takes exactly ~30.3s (T00e), and
jobs often never settle (`state=working`, `alive=false`). Both point at the
per-job `.locks/*.state.lock.guard` files being left behind - the store then waits
out its 30s lock timeout on every operation.

Safety:
  * only touches `%USERPROFILE%\\.codebuddy\\jobs\\.locks` contents;
  * MOVES files into `.locks.bak-<timestamp>` instead of deleting them;
  * keeps `directory-index-v1.json` in place;
  * requires the PoC server to be stopped first (caller's responsibility).

Usage:
  python scripts/serve_ctl.py stop
  python scripts/reset_job_locks.py            # move + report
  python scripts/reset_job_locks.py --dry-run
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

LOCKS = Path.home() / ".codebuddy" / "jobs" / ".locks"
KEEP = {"directory-index-v1.json"}


def restore() -> int:
    """Move everything from the newest .locks.bak-* back into .locks."""
    backups = sorted(LOCKS.parent.glob(".locks.bak-*"))
    if not backups:
        print(json.dumps({"result": "no_backup"}))
        return 0
    src = backups[-1]
    moved, skipped = 0, []
    for p in src.iterdir():
        target = LOCKS / p.name
        if target.exists():
            skipped.append(p.name)
            continue
        try:
            shutil.move(str(p), str(target))
            moved += 1
        except Exception as exc:  # noqa: BLE001
            skipped.append(f"{p.name}: {type(exc).__name__}")
    print(json.dumps({"result": "restored", "from": str(src), "moved": moved,
                      "skipped_count": len(skipped), "skipped_sample": skipped[:5]}))
    return 0


def main() -> int:
    dry = "--dry-run" in sys.argv
    if "--restore" in sys.argv:
        return restore()
    if not LOCKS.exists():
        print(json.dumps({"result": "no_locks_dir", "path": str(LOCKS)}))
        return 0
    files = [p for p in LOCKS.iterdir() if p.name not in KEEP]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = LOCKS.parent / f".locks.bak-{stamp}"
    report = {"locks_dir": str(LOCKS), "file_count": len(files),
              "backup_dir": str(backup), "dry_run": dry,
              "sample": [p.name for p in files[:5]]}
    if dry:
        print(json.dumps(report, ensure_ascii=False))
        return 0

    backup.mkdir(parents=True, exist_ok=True)
    moved, skipped = 0, []
    for p in files:
        try:
            shutil.move(str(p), str(backup / p.name))
            moved += 1
        except Exception as exc:  # noqa: BLE001
            skipped.append({"file": p.name, "error": f"{type(exc).__name__}: {exc}"})
    report.update(moved=moved, skipped=skipped[:10], skipped_count=len(skipped))
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
