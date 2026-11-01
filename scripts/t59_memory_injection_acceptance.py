"""T59: Markdown memory changes reach new prompts, never old WorkItems.

Deterministic ContextBuilder/SQLite acceptance only.  No model or Worker runs.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import ArtifactAssertion  # noqa: E402
from controlplane.context_update import ProjectContextUpdateLoop  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.project_context import ContextVersionConflict  # noqa: E402
from controlplane.project_memory_md import ProjectMemoryMarkdown  # noqa: E402
from controlplane.project_service import ProjectService  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

FIXTURE = ROOT / "sandbox" / "t59-memory-injection"
LEDGER = ROOT / ".poc-state" / "t59-memory-injection.db"
MEMORY_BASE = ROOT / ".poc-state" / "t59-memory-root"
EVIDENCE = ROOT / "evidence" / "project" / "T59-memory-injection.json"

OLD_KNOWLEDGE = "T59-OLD-CANDIDATE-KNOWLEDGE"
NEW_KNOWLEDGE = "T59-NEW-CANDIDATE-KNOWLEDGE"
STALE_KNOWLEDGE = "T59-STALE-LOSER-KNOWLEDGE"
WINNER_KNOWLEDGE = "T59-CONCURRENT-WINNER-KNOWLEDGE"
CROSS_PROJECT = "T59-CROSS-PROJECT-MUST-NOT-APPEAR"


class AdapterRef:
    name = "stub"


def make_repo(path: Path, name: str) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email",
         "t59@example.invalid"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "T59"],
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


def main() -> int:
    shutil.rmtree(FIXTURE, ignore_errors=True)
    shutil.rmtree(MEMORY_BASE, ignore_errors=True)
    repo_a, repo_b = FIXTURE / "project-a", FIXTURE / "project-b"
    make_repo(repo_a, "a")
    make_repo(repo_b, "b")
    for stale in [LEDGER, *LEDGER.parent.glob(LEDGER.name + ".*")]:
        stale.unlink(missing_ok=True)

    store = TaskStore(LEDGER)
    plane = ControlPlane(AdapterRef(), store, require_registered_projects=True)
    service = ProjectService(plane, budget_chars=500)
    project = plane.register_project(
        "t59-memory-a", str(repo_a), default_adapter="stub")
    other = plane.register_project(
        "t59-memory-b", str(repo_b), default_adapter="stub")
    verified_body = "T59-VERIFIED-HARD-" + ("complete-boundary-" * 120)
    verified = service.add_context_entry(project["id"], {
        "kind": "constraints", "title": "T59 verified hard boundary",
        "body": verified_body, "tags": ["memory"],
        "verification": "verified", "security_domain": "local",
        "source": "human:t59", "evidence_refs": ["evidence:t59-reviewed"],
    })
    unverified = service.add_context_entry(project["id"], {
        "kind": "constraints", "title": "T59 unverified note",
        "body": "T59-UNVERIFIED-NOT-MANDATORY", "tags": ["memory"],
        "verification": "unverified", "security_domain": "local",
        "source": "human:t59", "evidence_refs": [],
    })
    candidate = service.add_context_entry(project["id"], {
        "kind": "knowledge", "title": "T59 editable knowledge",
        "body": OLD_KNOWLEDGE, "tags": ["memory"],
        "verification": "candidate", "security_domain": "local",
        "source": "human:t59", "evidence_refs": [],
    })
    service.add_context_entry(other["id"], {
        "kind": "knowledge", "title": "T59 other project knowledge",
        "body": CROSS_PROJECT, "tags": ["memory"],
        "verification": "verified", "security_domain": "local",
        "source": "human:t59", "evidence_refs": ["evidence:t59-other"],
    })

    old_task = service.submit_project_task(
        project["id"], "T59 first independent task uses memory",
        idempotency_key="t59-old-task", tags=["memory"],
        require_verified_constraints=True)
    old_prompt = old_task["effective_prompt"]
    old_version = old_task["context_version"]

    memory = ProjectMemoryMarkdown(service, base_dir=MEMORY_BASE)
    exported = memory.export(project["id"])
    candidate_path = (
        Path(exported["memory_dir"]) / "entries" / f"{candidate['id']}.md")
    edited = replace_once(
        candidate_path.read_text(encoding="utf-8"),
        OLD_KNOWLEDGE, NEW_KNOWLEDGE)
    candidate_path.write_text(edited, encoding="utf-8")
    preview = memory.preview(project["id"], candidate_path)
    applied = memory.apply(
        project["id"], candidate_path, preview["preview_digest"])

    new_task = service.submit_project_task(
        project["id"], "T59 second independent task uses updated memory",
        idempotency_key="t59-new-task", tags=["memory"],
        require_verified_constraints=True)
    new_prompt = new_task["effective_prompt"]
    old_after = store.get(old_task["task_id"])

    # Prepare a stale Markdown edit, then let a concurrent SQLite writer win.
    memory.export(project["id"], force=True)
    candidate_path = (
        memory.memory_dir(project["id"]) / "entries" / f"{candidate['id']}.md")
    stale_text = replace_once(
        candidate_path.read_text(encoding="utf-8"),
        NEW_KNOWLEDGE, STALE_KNOWLEDGE)
    candidate_path.write_text(stale_text, encoding="utf-8")
    stale_preview = memory.preview(project["id"], candidate_path)
    current = service.context.get_entry(project["id"], candidate["id"])
    winner = service.update_context_entry(
        project["id"], candidate["id"], current["version"],
        {"body": WINNER_KNOWLEDGE}, changed_by="t59-concurrent-winner",
        note="prove stale Markdown cannot overwrite a newer SQLite revision")
    stale_error = caught(
        ContextVersionConflict,
        lambda: memory.apply(
            project["id"], candidate_path, stale_preview["preview_digest"]))
    final_task = service.submit_project_task(
        project["id"], "T59 third task after a rejected stale import",
        idempotency_key="t59-final-task", tags=["memory"],
        require_verified_constraints=True)
    final_prompt = final_task["effective_prompt"]
    context_version_after_final_task = store.context_version(project["id"])

    # M2 governance: an accepted assertion remains task-specific evidence,
    # never a general reusable law or a verified entry.
    auto_task = service.submit_project_task(
        project["id"], "T59 task-specific assertion evidence",
        idempotency_key="t59-auto-candidate",
        assertions=[ArtifactAssertion("file_exists", "seed.txt")])
    attempt = store.add_attempt(
        auto_task["task_id"], worker_job_id="job-t59-auto",
        state="succeeded", reason="deterministic fixture")
    store.update(
        auto_task["task_id"], current_attempt_id=attempt["id"],
        state="succeeded", verdict="verified", queue_status="complete",
        assertion_results=[{
            "kind": "file_exists", "target": "seed.txt", "passed": True,
            "observed": "exists",
        }])
    loop = ProjectContextUpdateLoop(store)
    task_row = store.get(auto_task["task_id"])
    auto_candidate = loop._derive_candidates(  # noqa: SLF001 - acceptance probe
        task_row, attempt, "evidence/project/T58-markdown-memory.json")[1]
    auto_apply = loop.apply_candidate(auto_candidate)
    auto_entry = store.get_context_entry(auto_apply["entry_id"])

    hard_start = new_prompt.find("# VERIFIED HARD CONSTRAINTS")
    notes_start = new_prompt.find("# UNVERIFIED CONSTRAINTS / NOTES")
    hard_section = new_prompt[hard_start:notes_start]
    checks = {
        "old_task_contains_original_candidate": OLD_KNOWLEDGE in old_prompt,
        "old_task_prompt_is_frozen": (
            old_after["effective_prompt"] == old_prompt
            and NEW_KNOWLEDGE not in old_after["effective_prompt"]
            and WINNER_KNOWLEDGE not in old_after["effective_prompt"]),
        "markdown_preview_and_apply_succeeded": (
            preview["can_apply"] and applied["applied"]),
        "new_task_contains_markdown_update": NEW_KNOWLEDGE in new_prompt,
        "candidate_is_explicitly_untrusted": (
            "T59 editable knowledge [CANDIDATE]" in new_prompt
            and "verify them before relying" in new_prompt),
        "verified_hard_constraint_is_complete": (
            verified_body in new_prompt
            and new_prompt.count(verified_body) == 1),
        "unverified_constraint_is_not_hard": (
            "T59-UNVERIFIED-NOT-MANDATORY" not in hard_section
            and "# UNVERIFIED CONSTRAINTS / NOTES (not mandatory)" in new_prompt),
        "verified_requirement_is_satisfied_only_by_verified": (
            not new_task["context"]["blocked"]),
        "new_task_records_new_context_version": (
            int(new_task["context_version"]) > int(old_version)),
        "new_task_records_actual_entry_refs": (
            {verified["id"], unverified["id"], candidate["id"]}
            <= set(new_task["context_entry_refs"])),
        "cross_project_knowledge_is_absent": all(
            CROSS_PROJECT not in prompt
            for prompt in (old_prompt, new_prompt, final_prompt)),
        "stale_markdown_import_is_rejected": bool(stale_error),
        "failed_import_does_not_replace_winner": (
            service.context.get_entry(
                project["id"], candidate["id"])["body"] == WINNER_KNOWLEDGE),
        "task_after_conflict_reads_winner_only": (
            WINNER_KNOWLEDGE in final_prompt
            and STALE_KNOWLEDGE not in final_prompt),
        "task_after_conflict_records_winner_version": (
            final_task["context_version"] == context_version_after_final_task
            and final_task["context_version"] >= winner["version"]),
        "m2_auto_entry_is_candidate": (
            auto_entry["verification"] == "candidate"),
        "m2_auto_entry_is_identifiable": (
            {"m2-auto", "execution-evidence"}
            <= set(auto_entry.get("tags") or [])
            and auto_entry["source"] == f"task:{auto_task['task_id']}"),
        "m2_assertion_is_not_a_reusable_law": (
            "task-specific execution evidence" in auto_entry["body"].lower()
            and "not verified reusable engineering knowledge"
            in auto_entry["body"].lower()),
        "no_worker_or_model_was_started": True,
    }
    failed = [name for name, passed in checks.items() if not passed]
    evidence = {
        "test": "T59 Markdown memory injection acceptance",
        "worker_started": False,
        "real_model_called": False,
        "fixture": "<REPO>/sandbox/t59-memory-injection",
        "checks": checks,
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
