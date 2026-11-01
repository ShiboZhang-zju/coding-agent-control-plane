"""T58: Markdown Project Memory export, preview and explicit CAS apply.

Deterministic only.  No model or Worker is started.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.project_context import (  # noqa: E402
    ContextError,
    ContextVersionConflict,
)
from controlplane.project_memory_md import (  # noqa: E402
    ProjectMemoryAccessError,
    ProjectMemoryFormatError,
    ProjectMemoryMarkdown,
    ProjectMemoryPolicyError,
)
from controlplane.project_service import ProjectService  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from ld.goal_store import LDGoalStore, goal_identity  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

FIXTURE = ROOT / "sandbox" / "t58-markdown-memory"
LEDGER = ROOT / ".poc-state" / "t58-markdown-memory.db"
MEMORY_BASE = ROOT / ".poc-state" / "t58-memory-root"
EVIDENCE = ROOT / "evidence" / "project" / "T58-markdown-memory.json"


class AdapterRef:
    name = "stub"


def make_repo(path: Path, name: str) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email",
         "t58@example.invalid"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "T58"],
        check=True)
    (path / "seed.txt").write_text(f"{name}\n", encoding="utf-8")
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


def replace_once(text: str, before: str, after: str) -> str:
    if text.count(before) != 1:
        raise AssertionError(f"expected exactly one {before!r}")
    return text.replace(before, after, 1)


def file_snapshot(directory: Path) -> dict[str, str]:
    return {
        str(path.relative_to(directory)): path.read_text(encoding="utf-8")
        for path in sorted(directory.rglob("*.md"))
        if path.is_file() and not path.is_symlink()
    }


def main() -> int:
    shutil.rmtree(FIXTURE, ignore_errors=True)
    shutil.rmtree(MEMORY_BASE, ignore_errors=True)
    repo1, repo2 = FIXTURE / "one", FIXTURE / "two"
    make_repo(repo1, "one")
    make_repo(repo2, "two")
    for stale in [LEDGER, *LEDGER.parent.glob(LEDGER.name + ".*")]:
        stale.unlink(missing_ok=True)

    store = TaskStore(LEDGER)
    plane = ControlPlane(AdapterRef(), store, require_registered_projects=True)
    service = ProjectService(plane)
    project = plane.register_project(
        "t58-memory-one", str(repo1), default_adapter="stub")
    other = plane.register_project(
        "t58-memory-two", str(repo2), default_adapter="stub")
    goals = LDGoalStore(LEDGER)
    goal_id, goal_ref = goal_identity("T58 durable goal", project["id"])
    goals.get_or_create(
        goal_id, project["id"], "T58 durable goal", goal_ref)

    candidate = service.add_context_entry(project["id"], {
        "kind": "knowledge", "title": "Editable candidate",
        "body": "candidate body before", "tags": ["memory"],
        "verification": "candidate", "security_domain": "local",
        "source": "human:t58", "evidence_refs": ["task:t58-source"],
    })
    duplicate_target = service.add_context_entry(project["id"], {
        "kind": "knowledge", "title": "Duplicate target",
        "body": "second candidate body", "tags": ["memory", "second"],
        "verification": "candidate", "security_domain": "local",
        "source": "human:t58", "evidence_refs": [],
    })
    verified = service.add_context_entry(project["id"], {
        "kind": "constraints", "title": "Verified boundary",
        "body": "verified body cannot be edited via Markdown", "tags": ["hard"],
        "verification": "verified", "security_domain": "local",
        "source": "human:t58", "evidence_refs": ["evidence:t58-reviewed"],
    })
    cli_candidate = service.add_context_entry(project["id"], {
        "kind": "knowledge", "title": "CLI candidate",
        "body": "cli body before", "tags": ["cli"],
        "verification": "unverified", "security_domain": "local",
        "source": "human:t58", "evidence_refs": [],
    })
    service.add_context_entry(other["id"], {
        "kind": "knowledge", "title": "Other project entry",
        "body": "other project body", "tags": ["other"],
        "verification": "candidate", "security_domain": "local",
        "source": "human:t58", "evidence_refs": [],
    })

    memory = ProjectMemoryMarkdown(
        service, goal_store=goals, base_dir=MEMORY_BASE)
    before_export = {
        "context_version": store.context_version(project["id"]),
        "revision_count": sum(
            len(service.context.revisions(project["id"], entry["id"]))
            for entry in service.context.list_entries(project["id"])),
        "task_count": len(store.work_items_for_project(project["id"])),
        "goal_updated_at": goals.get(goal_id)["updated_at"],
    }
    exported1 = memory.export(project["id"])
    memory_dir = Path(exported1["memory_dir"])
    snapshot1 = file_snapshot(memory_dir)
    memory.export(project["id"])
    snapshot2 = file_snapshot(memory_dir)
    after_export = {
        "context_version": store.context_version(project["id"]),
        "revision_count": sum(
            len(service.context.revisions(project["id"], entry["id"]))
            for entry in service.context.list_entries(project["id"])),
        "task_count": len(store.work_items_for_project(project["id"])),
        "goal_updated_at": goals.get(goal_id)["updated_at"],
    }

    candidate_path = memory_dir / "entries" / f"{candidate['id']}.md"
    baseline_text = candidate_path.read_text(encoding="utf-8")
    desired_text = replace_once(
        baseline_text, 'title: "Editable candidate"',
        'title: "Editable candidate updated"')
    desired_text = replace_once(
        desired_text, "candidate body before", "candidate body after")
    desired_text = replace_once(
        desired_text, '  - "memory"', '  - "memory"\n  - "reviewed"')
    candidate_path.write_text(desired_text, encoding="utf-8")
    preview = memory.preview(project["id"], candidate_path)
    preview_repeat = memory.preview(project["id"], candidate_path)
    candidate_path.write_text(
        desired_text.replace("candidate body after", "changed after preview"),
        encoding="utf-8")
    changed_after_preview = caught(
        ProjectMemoryPolicyError,
        lambda: memory.apply(
            project["id"], candidate_path, preview["preview_digest"]))
    candidate_path.write_text(desired_text, encoding="utf-8")
    confirmed = memory.preview(project["id"], candidate_path)
    protected_before = {
        key: candidate.get(key) for key in (
            "id", "project_id", "kind", "verification", "security_domain",
            "source", "evidence_refs")}
    applied = memory.apply(
        project["id"], candidate_path, confirmed["preview_digest"])
    updated = service.context.get_entry(project["id"], candidate["id"])
    protected_after = {key: updated.get(key) for key in protected_before}
    repeated_apply = caught(
        ContextVersionConflict,
        lambda: memory.apply(
            project["id"], candidate_path, confirmed["preview_digest"]))

    # Re-export the newly authoritative versions before negative edits.
    memory.export(project["id"])
    candidate_path = memory_dir / "entries" / f"{candidate['id']}.md"
    current_text = candidate_path.read_text(encoding="utf-8")

    candidate_path.write_text(replace_once(
        current_text, 'verification: "candidate"',
        'verification: "verified"'), encoding="utf-8")
    verification_preview = memory.preview(project["id"], candidate_path)
    candidate_path.write_text(replace_once(
        current_text, 'security_domain: "local"',
        'security_domain: "shareable"'), encoding="utf-8")
    security_preview = memory.preview(project["id"], candidate_path)
    candidate_path.write_text(replace_once(
        current_text, f"version: {updated['version']}",
        f"version: {int(updated['version']) + 10}"), encoding="utf-8")
    version_preview = memory.preview(project["id"], candidate_path)

    duplicate_front = replace_once(
        current_text, 'title: "Editable candidate updated"',
        'title: "Editable candidate updated"\ntitle: "duplicate"')
    candidate_path.write_text(duplicate_front, encoding="utf-8")
    duplicate_field_error = caught(
        ProjectMemoryFormatError,
        lambda: memory.preview(project["id"], candidate_path))
    unknown_front = replace_once(
        current_text, f'id: "{candidate["id"]}"',
        f'id: "{candidate["id"]}"\nunknown_field: "bad"')
    candidate_path.write_text(unknown_front, encoding="utf-8")
    unknown_field_error = caught(
        ProjectMemoryFormatError,
        lambda: memory.preview(project["id"], candidate_path))
    candidate_path.write_text(
        current_text.replace("\n---\n\n", "\n", 1), encoding="utf-8")
    malformed_error = caught(
        ProjectMemoryFormatError,
        lambda: memory.preview(project["id"], candidate_path))
    candidate_path.write_text(current_text, encoding="utf-8")

    unknown_text = replace_once(
        current_text, f'id: "{candidate["id"]}"',
        'id: "ctx-t58-unknown"')
    unknown_path = memory_dir / "entries" / "ctx-t58-unknown.md"
    unknown_path.write_text(unknown_text, encoding="utf-8")
    unknown_id_error = caught(
        KeyError, lambda: memory.preview(project["id"], unknown_path))
    unknown_path.unlink()

    memory.export(other["id"])
    other_memory = memory.memory_dir(other["id"])
    wrong_project_path = other_memory / "entries" / f"{candidate['id']}.md"
    wrong_project_path.write_text(replace_once(
        current_text, f'project_id: "{project["id"]}"',
        f'project_id: "{other["id"]}"'), encoding="utf-8")
    wrong_project_error = caught(
        ContextError,
        lambda: memory.preview(other["id"], wrong_project_path))
    traversal_error = caught(
        ProjectMemoryAccessError,
        lambda: memory.preview(project["id"], memory_dir / "CURRENT.md"))
    outside = MEMORY_BASE / "outside.md"
    outside.write_text(current_text, encoding="utf-8")
    symlink = memory_dir / "entries" / "ctx-link.md"
    symlink.symlink_to(outside)
    symlink_error = caught(
        ProjectMemoryAccessError,
        lambda: memory.preview(project["id"], symlink))
    symlink.unlink()

    verified_path = memory_dir / "entries" / f"{verified['id']}.md"
    verified_text = verified_path.read_text(encoding="utf-8")
    verified_path.write_text(replace_once(
        verified_text, "verified body cannot be edited via Markdown",
        "attempted verified edit"), encoding="utf-8")
    verified_preview = memory.preview(project["id"], verified_path)

    duplicate_path = memory_dir / "entries" / f"{duplicate_target['id']}.md"
    duplicate_text = duplicate_path.read_text(encoding="utf-8")
    duplicate_text = replace_once(
        duplicate_text, 'title: "Duplicate target"',
        'title: "Editable candidate updated"')
    duplicate_path.write_text(duplicate_text, encoding="utf-8")
    duplicate_preview = memory.preview(project["id"], duplicate_path)
    before_failed_apply = {
        "context_version": store.context_version(project["id"]),
        "entry": service.context.get_entry(project["id"], duplicate_target["id"]),
        "revisions": len(service.context.revisions(
            project["id"], duplicate_target["id"])),
    }
    duplicate_title_error = caught(
        sqlite3.IntegrityError,
        lambda: memory.apply(
            project["id"], duplicate_path,
            duplicate_preview["preview_digest"]))
    after_failed_apply = {
        "context_version": store.context_version(project["id"]),
        "entry": service.context.get_entry(project["id"], duplicate_target["id"]),
        "revisions": len(service.context.revisions(
            project["id"], duplicate_target["id"])),
    }

    # Exercise the public CLI end to end with a separate unverified entry.
    cli_export = json.loads(subprocess.run([
        sys.executable, str(ROOT / "scripts" / "controlplane_cli.py"),
        "--ledger", str(LEDGER), "--compact", "memory-export",
        project["id"],
    ], check=True, text=True, capture_output=True).stdout)
    cli_path = next(
        Path(path) for path in cli_export["entry_files"]
        if Path(path).name == f"{cli_candidate['id']}.md")
    cli_text = cli_path.read_text(encoding="utf-8")
    cli_path.write_text(replace_once(
        cli_text, "cli body before", "cli body after"), encoding="utf-8")
    cli_preview = json.loads(subprocess.run([
        sys.executable, str(ROOT / "scripts" / "controlplane_cli.py"),
        "--ledger", str(LEDGER), "--compact", "memory-preview",
        project["id"], "--entry", str(cli_path),
    ], check=True, text=True, capture_output=True).stdout)
    cli_apply = json.loads(subprocess.run([
        sys.executable, str(ROOT / "scripts" / "controlplane_cli.py"),
        "--ledger", str(LEDGER), "--compact", "memory-apply",
        project["id"], "--entry", str(cli_path),
        "--confirm-preview", cli_preview["preview_digest"],
    ], check=True, text=True, capture_output=True).stdout)

    checks = {
        "export_writes_minimal_layout": (
            set(snapshot1) == {
                "PROJECT.md", "CURRENT.md",
                *{f"entries/{entry['id']}.md" for entry in (
                    candidate, duplicate_target, verified, cli_candidate)}}),
        "repeated_export_is_byte_stable": snapshot1 == snapshot2,
        "export_has_zero_ledger_side_effects": before_export == after_export,
        "local_memory_stays_under_poc_state": memory_dir.is_relative_to(
            ROOT / ".poc-state"),
        "current_is_declared_read_only": (
            "GENERATED READ-ONLY VIEW" in snapshot1["CURRENT.md"]),
        "project_view_declares_sqlite_authority": (
            "SQLite Project Context is authoritative" in snapshot1["PROJECT.md"]),
        "preview_lists_only_editable_changes": (
            preview["can_apply"]
            and preview["changed_fields"] == ["body", "tags", "title"]
            and not preview["protected_changes"]),
        "preview_digest_is_stable": (
            preview["preview_digest"] == preview_repeat["preview_digest"]),
        "file_change_after_preview_is_rejected": bool(
            changed_after_preview and "does not match" in changed_after_preview),
        "confirmed_apply_updates_once": (
            applied["applied"] and updated["title"] == "Editable candidate updated"
            and updated["body"] == "candidate body after"
            and updated["tags"] == ["memory", "reviewed"]
            and applied["revision_count"] == 1),
        "protected_fields_survive_apply": protected_before == protected_after,
        "repeated_old_version_is_conflict": bool(repeated_apply),
        "verification_change_is_refused": (
            not verification_preview["can_apply"]
            and "verification" in verification_preview["protected_changes"]),
        "security_domain_change_is_refused": (
            not security_preview["can_apply"]
            and "security_domain" in security_preview["protected_changes"]),
        "version_tamper_is_conflict": (
            not version_preview["can_apply"]
            and version_preview["version_conflict"]),
        "duplicate_front_matter_is_rejected": bool(duplicate_field_error),
        "unknown_front_matter_is_rejected": bool(unknown_field_error),
        "malformed_markdown_is_rejected": bool(malformed_error),
        "unknown_entry_id_is_rejected": bool(unknown_id_error),
        "wrong_project_is_rejected": bool(wrong_project_error),
        "current_view_cannot_be_imported": bool(traversal_error),
        "symlink_escape_is_rejected": bool(symlink_error),
        "verified_content_edit_is_refused": (
            not verified_preview["can_apply"]
            and any("verified entries" in item
                    for item in verified_preview["policy_errors"])),
        "failed_import_is_atomic": (
            bool(duplicate_title_error)
            and before_failed_apply == after_failed_apply),
        "cli_export_preview_apply_works": (
            cli_apply["applied"]
            and service.context.get_entry(
                project["id"], cli_candidate["id"])["body"] == "cli body after"),
        "markdown_never_changes_goal_or_workitem": (
            goals.get(goal_id)["updated_at"] == before_export["goal_updated_at"]
            and len(store.work_items_for_project(project["id"])) == 0),
    }
    failed = [name for name, passed in checks.items() if not passed]
    evidence = {
        "test": "T58 Markdown Project Memory export/preview/CAS apply",
        "worker_started": False,
        "real_model_called": False,
        "fixture": "<REPO>/sandbox/t58-markdown-memory",
        "checks": checks,
        "result": "PASS" if not failed else "FAIL",
        "failed": failed,
    }
    write_evidence(EVIDENCE, evidence)
    goals.close()
    store.close()
    print(json.dumps(
        {"result": evidence["result"], "failed": failed},
        ensure_ascii=False))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
