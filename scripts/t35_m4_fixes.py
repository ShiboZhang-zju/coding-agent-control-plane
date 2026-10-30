"""T35: M4 reliability fixes — shell/context refusal, session consistency,
deterministic handoff checkpoints (no Worker is started).

Covers the three fixes that unblock real LD integration:

A1  ProjectService refuses shell policy for context-injected submissions
    (submit_project_task and submit_handoff), while raw ControlPlane.enqueue
    keeps working for shell jobs.
A2  _save_snapshot keeps the WorkItem session identity identical to the
    immutable Session Registry first binding, and a late conflicting snapshot
    event names the Attempt it actually came from.
A3  HandoffPlanner.build_source_checkpoint assembles a commit identical to the
    *accepted* worktree state in the object store only (source worktree,
    branch and main untouched; deterministic id); checkpoint_mode "reachable"
    allows a not-yet-integrated checkpoint and renders an explicit merge
    instruction, while "integrated" keeps the strict M3 blocker; the
    checkpoint gets a private protective ref only after a successful submit;
    context-only handoffs stay labelled as such.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import TaskSnapshot, TaskState  # noqa: E402
from controlplane.handoff import HandoffNotReady  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.policies import poc_shell  # noqa: E402
from controlplane.project_service import ProjectService  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from controlplane.verify import worktree_fingerprint  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

FIXTURE = ROOT / "sandbox" / "m4-fixes"
REPO = FIXTURE / "repo-a"
WORKTREE = FIXTURE / "repo-a-wt"
LEDGER = ROOT / ".poc-state" / "t35-m4-fixes.db"
JOB_SEQ = iter(range(1, 1000))


class AdapterRef:
    name = "stub"


def git(path: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(path), *args], check=False,
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"git {args} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def make_repo() -> None:
    if (REPO / ".git").exists():
        # Reset any leftover linked worktree/branch from a previous run.
        subprocess.run(["git", "-C", str(REPO), "worktree", "remove",
                        "--force", str(WORKTREE)], capture_output=True, timeout=30)
        subprocess.run(["git", "-C", str(REPO), "branch", "-D", "feat-a"],
                       capture_output=True, timeout=30)
        shutil.rmtree(WORKTREE, ignore_errors=True)
        git(REPO, "checkout", "-q", "-f", "main")
        git(REPO, "reset", "-q", "--hard", "main")
        git(REPO, "clean", "-qfd")
        return
    shutil.rmtree(FIXTURE, ignore_errors=True)
    REPO.mkdir(parents=True)
    git(REPO, "init", "-q")
    git(REPO, "config", "user.email", "t35@example.invalid")
    git(REPO, "config", "user.name", "T35")
    (REPO / "seed.txt").write_text("seed\n", encoding="utf-8")
    git(REPO, "add", "seed.txt")
    git(REPO, "commit", "-q", "-m", "seed")
    git(REPO, "branch", "-M", "main")


def make_source(plane: ControlPlane, store: TaskStore, project_id: str,
                *, worktree: str | None, head_commit: str | None,
                session: str) -> str:
    n = next(JOB_SEQ)
    workspace = store.default_workspace(project_id)
    rec = plane.enqueue(f"t35 source task {n}", str(REPO),
                        title=f"t35-source-{n}", project_id=project_id,
                        workspace_id=workspace["id"])
    task_id = rec["task_id"]
    job = f"job-{n}"
    store.add_attempt(task_id, worker_job_id=job)
    store.observe_session_reference(
        work_item_id=task_id, worker_job_id=job, node_id="local",
        worker_instance_id="wi-t35", adapter="stub", session_id=session)
    store.update(
        task_id, state=TaskState.SUCCEEDED.value, verdict="verified",
        head_commit=head_commit, worktree=worktree,
        finished_at=1.0, artifacts=[], assertion_results=[])
    return task_id


def a1_shell_refusal(service: ProjectService, plane: ControlPlane,
                     store: TaskStore, project_id: str) -> dict:
    refused_submit = None
    try:
        service.submit_project_task(
            project_id, "echo hi", policy=poc_shell(), allow_blocked=True)
    except ValueError as exc:
        refused_submit = str(exc)
    refused_handoff = None
    source = make_source(plane, store, project_id,
                         worktree=str(WORKTREE),
                         head_commit=git(WORKTREE, "rev-parse", "HEAD"),
                         session="sess-a1")
    try:
        service.submit_handoff(source, "continue", policy=poc_shell(),
                               require_checkpoint=False)
    except ValueError as exc:
        refused_handoff = str(exc)
    # Raw enqueue keeps the pre-M1 shell path alive (no context injection).
    raw_ok = bool(plane.enqueue(
        "echo t35 > raw.txt", str(REPO), policy=poc_shell(),
        idempotency_key=f"t35-raw-{uuid.uuid4().hex[:6]}",
        project_id=project_id,
        workspace_id=store.default_workspace(project_id)["id"]).get("task_id"))
    return {
        "submit_project_task_refuses_shell": refused_submit is not None,
        "submit_handoff_refuses_shell": refused_handoff is not None,
        "refusal_names_reason": all([
            "shell" in (refused_submit or ""),
            "shell" in (refused_handoff or "")]),
        "raw_enqueue_shell_still_works": raw_ok,
    }


def a2_session_consistency(plane: ControlPlane, store: TaskStore,
                           project_id: str) -> dict:
    task = make_source(plane, store, project_id,
                       worktree=str(WORKTREE),
                       head_commit=git(WORKTREE, "rev-parse", "HEAD"),
                       session="sess-first")
    attempt_id = store.get(task)["current_attempt_id"]
    plane._save_snapshot(task, TaskSnapshot(
        task_id=task, worker_job_id=store.get(task)["worker_job_id"] or "",
        state=TaskState.RUNNING, session_id="sess-first"),
        patch={"state": TaskState.RUNNING.value, "session_id": "sess-first"})
    first_item = store.get(task)

    saved = plane._save_snapshot(task, TaskSnapshot(
        task_id=task, worker_job_id=store.get(task)["worker_job_id"] or "",
        state=TaskState.SUCCEEDED, session_id="sess-other"),
        patch={"state": TaskState.SUCCEEDED.value, "session_id": "sess-other"})
    registry = store.session_reference_for_attempt(attempt_id)
    events = [e for e in store.events(task, limit=50)
              if e.get("type") == "session_binding_conflict"]
    return {
        "work_item_keeps_first_session": saved.get("session_id") == "sess-first",
        "work_item_matches_registry": (
            saved.get("session_id") == first_item.get("session_id")
            == (registry or {}).get("session_id")),
        "conflict_escalated": saved.get("queue_status") == "needs_human",
        "registry_immutable": (registry or {}).get("session_id") == "sess-first",
        "event_names_source_attempt": (
            len(events) == 1 and events[0].get("attempt_id") == attempt_id
            and events[0]["payload"].get("source_attempt_id") == attempt_id),
    }


def a3_checkpoint_capture(service: ProjectService, plane: ControlPlane,
                          store: TaskStore, project_id: str) -> dict:
    main_before = git(REPO, "rev-parse", "main")
    wt_head = git(WORKTREE, "rev-parse", "HEAD")
    (WORKTREE / "feature-a.txt").write_text("work in progress\n", encoding="utf-8")
    source = make_source(plane, store, project_id,
                         worktree=str(WORKTREE), head_commit=wt_head,
                         session="sess-a3")
    # What the Verifier records at the first verified observation
    # (ControlPlane._snapshot_patch): the exact accepted worktree state.
    rec = store.get(source) or {}
    store.update(source, metadata={**(rec.get("metadata") or {}),
                                   "acceptance_snapshot": worktree_fingerprint(str(WORKTREE))})
    capture = service.handoffs.build_source_checkpoint(source)
    checkpoint = capture.get("commit")
    capture_again = service.handoffs.build_source_checkpoint(source)
    status_after = git(WORKTREE, "status", "--porcelain")

    prepared_reachable = service.prepare_handoff(
        source, "continue with A's code", require_checkpoint=True,
        checkpoint_mode="reachable")
    # prepare_handoff reports blockers in its return value; submit_handoff is
    # the one that raises HandoffNotReady.
    blocked_integrated = (service.prepare_handoff(
        source, "continue with A's code", require_checkpoint=True,
        checkpoint_mode="integrated").get("blockers"))
    prompt_reachable = prepared_reachable.get("effective_prompt") or ""

    # A source with no recorded checkpoint stays explicitly context-only; it
    # must never be reported as a code-state continuation.
    checkpointless = make_source(plane, store, project_id,
                                  worktree=None, head_commit=None,
                                  session="sess-a3-ctx")
    prepared_context_only = service.prepare_handoff(
        checkpointless, "continue context only", require_checkpoint=False,
        checkpoint_mode="reachable")
    checkpoint_only = prepared_context_only.get("checkpoint") or {}

    record = service.submit_handoff(
        source, "implement part B on top of A's code",
        require_checkpoint=True, checkpoint_mode="reachable",
        idempotency_key=f"t35-a3-{uuid.uuid4().hex[:6]}")
    submitted_prompt = record.get("effective_prompt") or ""
    protected = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse",
         f"refs/control-plane/handoff/{record.get('task_id')}"],
        capture_output=True, text=True, timeout=30).stdout.strip()

    main_after = git(REPO, "rev-parse", "main")
    return {
        "checkpoint_is_new_commit": checkpoint not in (None, wt_head),
        "checkpoint_parent_is_accepted_head": (
            bool(checkpoint) and git(WORKTREE, "rev-parse", f"{checkpoint}^") == wt_head),
        "checkpoint_contains_accepted_file": (
            bool(checkpoint)
            and git(WORKTREE, "show", f"{checkpoint}:feature-a.txt") == "work in progress"),
        "checkpoint_is_deterministic": capture_again.get("commit") == checkpoint,
        "source_worktree_untouched": (
            git(WORKTREE, "rev-parse", "HEAD") == wt_head
            and "feature-a.txt" in status_after),
        "source_head_commit_unchanged": (store.get(source) or {}).get("head_commit") == wt_head,
        "capture_never_touches_main": main_before == main_after,
        "reachable_mode_ready": prepared_reachable.get("ready") is True,
        "reachable_checkpoint_available": (
            prepared_reachable.get("checkpoint", {}).get("available") is True),
        "reachable_not_claiming_integrated": (
            prepared_reachable.get("checkpoint", {}).get("integrated") is False),
        "integrated_mode_still_blocks": any(
            "not contained in target workspace HEAD" in b
            or "uncommitted changes" in b
            for b in (blocked_integrated or [])),
        "reachable_prompt_has_merge_instruction": (
            f"git merge {checkpoint}" in prompt_reachable),
        "context_only_still_labelled": checkpoint_only.get("kind") == "context_only",
        "submit_reachable_succeeds": bool(record.get("task_id")),
        "submitted_prompt_has_merge_instruction": (
            f"git merge {checkpoint}" in submitted_prompt),
        "checkpoint_protected_after_submit": protected == checkpoint,
    }


def main() -> int:
    make_repo()
    for stale in [LEDGER, *LEDGER.parent.glob(LEDGER.name + ".*")]:
        stale.unlink(missing_ok=True)
    # A linked worktree on its own branch, like the CodeBuddy isolation layout.
    git(REPO, "worktree", "add", "-q", str(WORKTREE), "-b", "feat-a")

    store = TaskStore(LEDGER)
    try:
        plane = ControlPlane(AdapterRef(), store, require_registered_projects=True,
                             worker_instance_id="wi-t35")
        service = ProjectService(plane)
        project = plane.register_project("t35-m4", str(REPO),
                                         default_adapter="stub")
        r_a1 = a1_shell_refusal(service, plane, store, project["id"])
        r_a2 = a2_session_consistency(plane, store, project["id"])
        r_a3 = a3_checkpoint_capture(service, plane, store, project["id"])

        sections = {"A1": r_a1, "A2": r_a2, "A3": r_a3}
        checks = {f"{name}.{key}": bool(value)
                  for name, result in sections.items()
                  for key, value in result.items()}
        log = {
            "test": "T35-m4-reliability-fixes",
            "worker_started": False,
            "sections": sections,
            "checks": checks,
            "result": "Pass" if all(checks.values()) else "Fail",
        }
        write_evidence(ROOT / "evidence" / "project" / "T35-m4-reliability-fixes.json",
                       log)
        print(json.dumps(log, ensure_ascii=False, indent=2, default=str))
        return 0 if log["result"] == "Pass" else 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
