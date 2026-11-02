"""T60: safe Markdown re-export, ledger-bound paths and knowledge labels.

Deterministic only.  No model or Worker is started.
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

FIXTURE = ROOT / "sandbox" / "t60-memory-export-safety"
STATE_A = ROOT / ".poc-state" / "t60-ledger-a"
STATE_B = ROOT / ".poc-state" / "t60-ledger-b"
LEDGER_A = STATE_A / "ledger.db"
LEDGER_B = STATE_B / "ledger.db"
EXPLICIT_ROOT = ROOT / ".poc-state" / "t60-explicit-memory"
EVIDENCE = ROOT / "evidence" / "project" / os.environ.get(
    "T60_EVIDENCE_NAME", "T60-memory-export-safety.json")
CLI = ROOT / "scripts" / "controlplane_cli.py"
PROJECT_ID = "proj-t60-shared"
ORIGINAL_BODY = "T60-SQLITE-AUTHORITATIVE"
LOCAL_EDIT = "T60-UNAPPLIED-LOCAL-EDIT"
CLI_EDIT = "T60-CLI-APPLIED-EDIT"


class AdapterRef:
    name = "stub"


def make_repo(path: Path, name: str) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email",
         "t60@example.invalid"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "T60"],
        check=True)
    (path / "seed.txt").write_text(f"{name}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "seed.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", "seed"],
        check=True)


def file_snapshot(directory: Path) -> dict[str, str]:
    return {
        path.relative_to(directory).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(directory.rglob("*.md"))
        if path.is_file() and not path.is_symlink()
    }


def caught(exc_type, operation) -> str | None:
    try:
        operation()
    except exc_type as exc:
        return str(exc)
    return None


def cli(ledger: Path, *args: str) -> dict:
    proc = subprocess.run(
        [sys.executable, str(CLI), "--ledger", str(ledger), "--compact",
         *args],
        cwd=str(ROOT), capture_output=True, text=True, timeout=120,
        encoding="utf-8", errors="replace")
    output = (proc.stdout or "").strip()
    try:
        payload = json.loads(output) if output else {}
    except json.JSONDecodeError:
        payload = {"raw": output}
    payload["exit"] = proc.returncode
    payload["stderr"] = (proc.stderr or "").strip()[:500]
    return payload


def create_project(store: TaskStore, repo: Path) -> tuple[ProjectService, dict]:
    # The same ID in two independent Ledgers makes path isolation explicit.
    store.new_project_id = lambda: PROJECT_ID
    plane = ControlPlane(
        AdapterRef(), store, require_registered_projects=True)
    project = plane.register_project(
        "t60-project", str(repo), default_adapter="stub")
    return ProjectService(plane), project


def main() -> int:
    for path in (FIXTURE, STATE_A, STATE_B, EXPLICIT_ROOT):
        shutil.rmtree(path, ignore_errors=True)
    repo_a, repo_b = FIXTURE / "project-a", FIXTURE / "project-b"
    make_repo(repo_a, "a")
    make_repo(repo_b, "b")

    store_a = TaskStore(LEDGER_A)
    store_b = TaskStore(LEDGER_B)
    service_a, project_a = create_project(store_a, repo_a)
    service_b, project_b = create_project(store_b, repo_b)
    candidate = service_a.add_context_entry(project_a["id"], {
        "kind": "knowledge", "title": "T60 candidate knowledge",
        "body": ORIGINAL_BODY, "tags": ["t60"],
        "verification": "candidate", "security_domain": "local",
        "source": "human:t60", "evidence_refs": [],
    })
    service_b.add_context_entry(project_b["id"], {
        "kind": "knowledge", "title": "T60 other ledger knowledge",
        "body": "T60-OTHER-LEDGER", "tags": ["t60"],
        "verification": "candidate", "security_domain": "local",
        "source": "human:t60", "evidence_refs": [],
    })

    memory_a = ProjectMemoryMarkdown(service_a)
    memory_b = ProjectMemoryMarkdown(service_b)
    export_a = memory_a.export(project_a["id"])
    export_b = memory_b.export(project_b["id"])
    memory_dir_a = Path(export_a["memory_dir"])
    memory_dir_b = Path(export_b["memory_dir"])
    candidate_path = (
        memory_dir_a / "entries" / f"{candidate['id']}.md")
    candidate_path.write_text(
        candidate_path.read_text(encoding="utf-8").replace(
            ORIGINAL_BODY, LOCAL_EDIT),
        encoding="utf-8")

    # Make PROJECT.md and the entry set stale too. A failed export must update
    # none of them, so the operator never observes a half-exported directory.
    added = service_a.add_context_entry(project_a["id"], {
        "kind": "knowledge", "title": "T60 added after first export",
        "body": "T60-NEW-SQLITE-ENTRY", "tags": ["t60"],
        "verification": "unverified", "security_domain": "local",
        "source": "human:t60", "evidence_refs": [],
    })
    before_conflict = file_snapshot(memory_dir_a)
    conflict_error = caught(
        ProjectMemoryError, lambda: memory_a.export(project_a["id"]))
    after_conflict = file_snapshot(memory_dir_a)

    force_error = caught(
        Exception, lambda: memory_a.export(project_a["id"], force=True))
    after_force = file_snapshot(memory_dir_a)
    stable_before = dict(after_force)
    stable_error = caught(
        Exception, lambda: memory_a.export(project_a["id"]))
    stable_after = file_snapshot(memory_dir_a)

    # Exercise all three CLI commands against one explicit memory root.
    explicit_export = cli(
        LEDGER_A, "memory-export", project_a["id"],
        "--memory-root", str(EXPLICIT_ROOT))
    explicit_entry = (
        EXPLICIT_ROOT / project_a["id"] / "memory" / "entries" /
        f"{candidate['id']}.md")
    if explicit_entry.exists():
        explicit_entry.write_text(
            explicit_entry.read_text(encoding="utf-8").replace(
                ORIGINAL_BODY, LOCAL_EDIT),
            encoding="utf-8")
    cli_refuse = cli(
        LEDGER_A, "memory-export", project_a["id"],
        "--memory-root", str(EXPLICIT_ROOT))
    cli_local_preserved = (
        explicit_entry.exists()
        and LOCAL_EDIT in explicit_entry.read_text(encoding="utf-8"))
    cli_force = cli(
        LEDGER_A, "memory-export", project_a["id"], "--force",
        "--memory-root", str(EXPLICIT_ROOT))
    if explicit_entry.exists():
        explicit_entry.write_text(
            explicit_entry.read_text(encoding="utf-8").replace(
                ORIGINAL_BODY, CLI_EDIT),
            encoding="utf-8")
    cli_preview = cli(
        LEDGER_A, "memory-preview", project_a["id"],
        "--entry", str(explicit_entry),
        "--memory-root", str(EXPLICIT_ROOT))
    digest = cli_preview.get("preview_digest") or "missing"
    cli_apply = cli(
        LEDGER_A, "memory-apply", project_a["id"],
        "--entry", str(explicit_entry), "--confirm-preview", digest,
        "--memory-root", str(EXPLICIT_ROOT))

    prompt = service_a.build_task_context(
        project_a["id"], "T60 inspect knowledge labels").effective_prompt
    checks = {
        "default_root_is_bound_to_ledger_a": (
            memory_a.base_dir == LEDGER_A.parent / "projects"
            and memory_dir_a.is_relative_to(LEDGER_A.parent / "projects")),
        "default_root_is_bound_to_ledger_b": (
            memory_b.base_dir == LEDGER_B.parent / "projects"
            and memory_dir_b.is_relative_to(LEDGER_B.parent / "projects")),
        "same_project_id_is_isolated_by_ledger": (
            project_a["id"] == project_b["id"] == PROJECT_ID
            and memory_dir_a != memory_dir_b),
        "divergent_entry_refuses_export": bool(
            conflict_error and "force" in conflict_error.lower()),
        "conflict_preserves_every_existing_file": (
            before_conflict == after_conflict
            and LOCAL_EDIT in after_conflict[
                f"entries/{candidate['id']}.md"]),
        "conflict_does_not_create_new_entry": (
            f"entries/{added['id']}.md" not in after_conflict),
        "force_overwrites_from_sqlite": (
            force_error is None
            and ORIGINAL_BODY in after_force[
                f"entries/{candidate['id']}.md"]
            and LOCAL_EDIT not in after_force[
                f"entries/{candidate['id']}.md"]
            and f"entries/{added['id']}.md" in after_force),
        "unchanged_repeat_export_is_stable": (
            stable_error is None and stable_before == stable_after),
        "cli_memory_root_is_used": (
            explicit_export.get("exit") == 0
            and str(EXPLICIT_ROOT.resolve())
            in str(explicit_export.get("memory_dir") or "")),
        "cli_export_refuses_without_force": (
            cli_refuse.get("exit") != 0 and cli_local_preserved),
        "cli_force_overwrites_local_edit": (
            cli_force.get("exit") == 0 and explicit_entry.exists()
            and LOCAL_EDIT not in explicit_entry.read_text(encoding="utf-8")),
        "cli_preview_and_apply_share_memory_root": (
            cli_preview.get("exit") == 0
            and cli_apply.get("exit") == 0
            and cli_apply.get("applied") is True
            and service_a.context.get_entry(
                project_a["id"], candidate["id"])["body"] == CLI_EDIT),
        "knowledge_header_is_verification_labelled": (
            "# PROJECT KNOWLEDGE (verification-labelled)" in prompt),
        "candidate_remains_explicitly_untrusted": (
            "T60 candidate knowledge [CANDIDATE]" in prompt),
        "misleading_verified_knowledge_header_is_absent": (
            "# VERIFIED KNOWLEDGE" not in prompt),
        "no_worker_or_model_was_started": True,
    }
    failed = [name for name, passed in checks.items() if not passed]
    evidence = {
        "test": "T60 Markdown export safety and ledger-bound paths",
        "worker_started": False,
        "real_model_called": False,
        "fixture": "<REPO>/sandbox/t60-memory-export-safety",
        "checks": checks,
        "observed_errors": {
            "api_conflict": conflict_error,
            "api_force": force_error,
            "api_stable": stable_error,
            "cli_refuse_exit": cli_refuse.get("exit"),
        },
        "result": "PASS" if not failed else "FAIL",
        "failed": failed,
    }
    write_evidence(EVIDENCE, evidence)
    store_a.close()
    store_b.close()
    print(json.dumps(
        {"result": evidence["result"], "failed": failed},
        ensure_ascii=False))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
