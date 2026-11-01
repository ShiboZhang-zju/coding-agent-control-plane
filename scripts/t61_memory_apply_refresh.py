"""T61: refresh only an applied Entry when its Markdown file is unchanged.

Deterministic only. No model or Worker is started.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.project_memory_md import (  # noqa: E402
    ProjectMemoryError,
    ProjectMemoryMarkdown,
)
from controlplane.project_service import ProjectService  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

FIXTURE = ROOT / "sandbox" / "t61-memory-apply-refresh"
LEDGER = ROOT / ".poc-state" / "t61-memory-apply-refresh.db"
MEMORY_ROOT = ROOT / ".poc-state" / "t61-memory-root"
EVIDENCE = ROOT / "evidence" / "project" / os.environ.get(
    "T61_EVIDENCE_NAME", "T61-memory-apply-refresh.json")


class AdapterRef:
    name = "stub"


def make_repo(path: Path) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email",
         "t61@example.invalid"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "T61"],
        check=True)
    (path / "seed.txt").write_text("t61\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "seed.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", "seed"],
        check=True)


def caught(exc_type, operation) -> str | None:
    try:
        operation()
    except exc_type as exc:
        return str(exc)
    return None


def main() -> int:
    shutil.rmtree(FIXTURE, ignore_errors=True)
    shutil.rmtree(MEMORY_ROOT, ignore_errors=True)
    for stale in [LEDGER, *LEDGER.parent.glob(LEDGER.name + ".*")]:
        stale.unlink(missing_ok=True)
    repo = FIXTURE / "project"
    make_repo(repo)

    store = TaskStore(LEDGER)
    plane = ControlPlane(AdapterRef(), store, require_registered_projects=True)
    service = ProjectService(plane)
    project = plane.register_project(
        "t61-memory", str(repo), default_adapter="stub")
    first = service.add_context_entry(project["id"], {
        "kind": "knowledge", "title": "T61 first candidate",
        "body": "T61-FIRST-BEFORE", "tags": ["t61"],
        "verification": "candidate", "security_domain": "local",
        "source": "human:t61", "evidence_refs": [],
    })
    second = service.add_context_entry(project["id"], {
        "kind": "knowledge", "title": "T61 second candidate",
        "body": "T61-SECOND-BEFORE", "tags": ["t61"],
        "verification": "candidate", "security_domain": "local",
        "source": "human:t61", "evidence_refs": [],
    })

    memory = ProjectMemoryMarkdown(service, base_dir=MEMORY_ROOT)
    exported = memory.export(project["id"])
    entries = Path(exported["memory_dir"]) / "entries"
    first_path = entries / f"{first['id']}.md"
    second_path = entries / f"{second['id']}.md"

    first_edited = first_path.read_text(encoding="utf-8").replace(
        "T61-FIRST-BEFORE", "T61-FIRST-APPLIED")
    first_path.write_text(first_edited, encoding="utf-8")
    other_before_apply = second_path.read_bytes()
    first_preview = memory.preview(project["id"], first_path)
    first_apply = memory.apply(
        project["id"], first_path, first_preview["preview_digest"])
    first_after = first_path.read_text(encoding="utf-8")
    other_unchanged_after_first_apply = (
        second_path.read_bytes() == other_before_apply)
    expected_first_after = first_edited.replace(
        f"version: {first_preview['markdown_version']}",
        f"version: {first_apply['updated_version']}", 1)
    normal_export_error = caught(
        ProjectMemoryError, lambda: memory.export(project["id"]))

    # Keep the initial-failure probe running on pre-fix code.
    if normal_export_error:
        memory.export(project["id"], force=True)

    second_path = entries / f"{second['id']}.md"
    second_edited = second_path.read_text(encoding="utf-8").replace(
        "T61-SECOND-BEFORE", "T61-SECOND-APPLIED")
    second_path.write_text(second_edited, encoding="utf-8")
    second_preview = memory.preview(project["id"], second_path)
    original_update = memory.context.update_context_entry

    def update_then_change_file(*args, **kwargs):
        updated = original_update(*args, **kwargs)
        second_path.write_text(
            second_path.read_text(encoding="utf-8").replace(
                "T61-SECOND-APPLIED", "T61-SECOND-LATE-EDIT"),
            encoding="utf-8")
        return updated

    memory.context.update_context_entry = update_then_change_file
    second_apply = memory.apply(
        project["id"], second_path, second_preview["preview_digest"])
    second_after = second_path.read_text(encoding="utf-8")

    checks = {
        "apply_updates_sqlite": (
            service.context.get_entry(project["id"], first["id"])["body"]
            == "T61-FIRST-APPLIED"),
        "unchanged_file_is_refreshed": (
            first_apply.get("markdown_refreshed") is True
            and first_apply.get("markdown_refresh_status") == "refreshed"
            and first_apply.get("markdown_requires_reexport") is False),
        "refresh_changes_only_version_line": first_after == expected_first_after,
        "refreshed_file_has_current_version": (
            f"version: {first_apply['updated_version']}" in first_after),
        "apply_does_not_touch_other_entry": other_unchanged_after_first_apply,
        "ordinary_export_after_apply_succeeds": normal_export_error is None,
        "late_edit_does_not_rollback_sqlite": (
            service.context.get_entry(project["id"], second["id"])["body"]
            == "T61-SECOND-APPLIED"),
        "late_edit_is_preserved": "T61-SECOND-LATE-EDIT" in second_after,
        "late_edit_is_reported": (
            second_apply.get("markdown_refreshed") is False
            and second_apply.get("markdown_refresh_status")
            == "preserved_file_changed_after_apply"
            and second_apply.get("markdown_requires_reexport") is True),
        "no_worker_or_model_was_started": True,
    }
    failed = [name for name, passed in checks.items() if not passed]
    evidence = {
        "test": "T61 applied Markdown Entry refresh",
        "worker_started": False,
        "real_model_called": False,
        "fixture": "<REPO>/sandbox/t61-memory-apply-refresh",
        "checks": checks,
        "observed": {
            "normal_export_error": normal_export_error,
            "first_refresh_status": first_apply.get("markdown_refresh_status"),
            "late_edit_status": second_apply.get("markdown_refresh_status"),
        },
        "result": "PASS" if not failed else "FAIL",
        "failed": failed,
    }
    write_evidence(EVIDENCE, evidence)
    store.close()
    print(json.dumps(
        {"result": evidence["result"], "failed": failed},
        ensure_ascii=False))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
