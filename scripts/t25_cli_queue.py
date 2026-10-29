"""T25: the local CLI can enqueue, inspect and approve without Worker access."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import winproc  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402


def run_cli(db: Path, *args: str):
    result = winproc.run(
        [sys.executable, str(ROOT / "scripts" / "controlplane_cli.py"),
         "--ledger", str(db), *args],
        cwd=str(ROOT), capture_output=True, text=True, timeout=20)
    if result.returncode != 0:
        raise RuntimeError(f"CLI failed ({result.returncode}): {result.stderr or result.stdout}")
    return json.loads(result.stdout)


def make_repo(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    (path / "README.md").write_text("# T25 CLI sandbox\n", encoding="utf-8")
    for argv in (["git", "init", "-q", "-b", "main"],
                 ["git", "config", "user.name", "poc"],
                 ["git", "config", "user.email", "poc@local"],
                 ["git", "add", "."],
                 ["git", "commit", "-q", "-m", "init"]):
        winproc.run(argv, cwd=str(path), check=True, capture_output=True, text=True)


def main() -> int:
    db = ROOT / ".poc-state" / "t25-cli.db"
    store = TaskStore(db)
    store.clear()
    store.close()
    repo = ROOT / "sandbox" / "cli-t25"
    make_repo(repo)
    project = run_cli(
        db, "project", "add", "cli-t25", str(repo),
        "--adapter", "codebuddy", "--concurrency", "2")
    disabled_project = run_cli(db, "project", "disable", "cli-t25")
    enabled_project = run_cli(db, "project", "enable", project["id"])
    projects = run_cli(db, "project", "list")
    shown_project = run_cli(db, "project", "show", "cli-t25")

    safe = run_cli(
        db, "enqueue", "create result.txt", "--cwd", str(repo), "--title", "safe",
        "--idempotency-key", "t25-safe", "--priority", "10",
        "--assert-file", "result.txt")
    risky = run_cli(
        db, "enqueue", "echo risky", "--cwd", str(repo), "--title", "risky",
        "--idempotency-key", "t25-risky", "--priority", "20",
        "--mode", "shell", "--isolation", "none", "--allow-unisolated-shell")
    approvals = run_cli(db, "approval", "list")
    approval = run_cli(db, "approval", "approve", str(approvals[0]["id"]),
                       "--note", "T25 exact-content approval")
    items = run_cli(db, "list")
    shown = run_cli(db, "show", safe["task_id"])

    by_title = {item["title"]: item for item in items}
    checks = {
        "safe_item_ready": safe["queue_status"] == "ready",
        "project_registered_and_bound": (
            project["active"] and safe["project_id"] == project["id"]),
        "project_cli_lifecycle": (
            not disabled_project["active"] and enabled_project["active"]
            and len(projects) == 1 and shown_project["id"] == project["id"]),
        "risky_item_waits_for_approval": (
            risky["queue_status"] == "needs_approval" and risky["approval_required"]),
        "approval_inbox_created": (
            len(approvals) == 1 and approvals[0]["decision"] == "pending"),
        "approval_releases_item": approval.get("queued_items_released") == 1,
        "risky_item_now_ready": by_title["risky"]["queue_status"] == "ready",
        "cli_never_called_worker": all(item["worker_job_id"] is None for item in items),
        "show_includes_audit_history": len(shown.get("events") or []) >= 1,
    }
    log = {
        "test": "T25-cli-queue",
        "checks": checks,
        "project": project,
        "safe": safe,
        "risky_before_approval": risky,
        "approval": {"id": approval.get("id"), "decision": approval.get("decision"),
                     "queued_items_released": approval.get("queued_items_released")},
        "items_after_approval": items,
        "result": "Pass" if all(checks.values()) else "Fail",
    }
    path = ROOT / "evidence" / "jobs" / "T25-cli-queue.json"
    path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(log, ensure_ascii=False, indent=2))
    return 0 if log["result"] == "Pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
