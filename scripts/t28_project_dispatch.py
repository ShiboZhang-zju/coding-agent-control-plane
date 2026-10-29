"""T28: project task dispatch — context injection, frozen prompt, approval binding.

Cases
-----
A  injection      A submitted task carries the project background and hard
                  constraints into the effective prompt, and the ledger keeps both
                  the original task and the effective prompt plus the context
                  version and entry ids.
B  freeze         Editing project context AFTER enqueue does not change the queued
                  task's prompt (no silent mutation of work in the queue).
C  approval       A high-risk project task needs an approval bound to the exact
                  effective prompt. Changing the injected content (context version)
                  or the target workspace invalidates that approval: the old
                  approval cannot be reused.
D  blocked        A missing prerequisite produces a blocking gap and refuses to
                  submit; nothing is enqueued and no worker is called.
E  compat         A plain `enqueue`/`dispatch` without project context still works
                  and produces the same digest as before this change (no
                  `scope` in the digest payload).

Everything runs against a stub adapter; no real worker is contacted.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import (ArtifactAssertion, DispatchPolicy, DispatchResult,  # noqa: E402
                           TaskSnapshot, TaskState)
from controlplane.context_builder import ContextBuildBlocked  # noqa: E402
from controlplane.plane import ApprovalRequired, ControlPlane, task_digest  # noqa: E402
from controlplane.project_context import ProjectContextStore  # noqa: E402
from controlplane.project_service import ProjectService  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

REPO_A = str(ROOT / "sandbox" / "repo-a")
LOW = DispatchPolicy(mode="agent", isolation="worktree")
HIGH = DispatchPolicy(mode="shell", isolation="none", allow_unisolated_shell=True)


class StubAdapter:
    name = "stub"

    def __init__(self):
        self.calls = 0
        self.prompts: list[str] = []

    def dispatch(self, request) -> DispatchResult:
        self.calls += 1
        self.prompts.append(request.prompt)
        return DispatchResult(task_id=request.task_id,
                              worker_job_id=f"job-{self.calls}",
                              accepted=True, raw={"dispatch_http": 200})

    def get_task(self, job_id: str, timeout_s: float = 20.0) -> TaskSnapshot:
        return TaskSnapshot(task_id=job_id, worker_job_id=job_id,
                            state=TaskState.RUNNING, cwd="", detail="stub")

    def collect_artifacts(self, _task_id):
        return []

    def reconcile(self) -> dict:
        return {"source": "stub", "total": 0, "new": [], "changed": [], "lost": [],
                "jobs": []}

    def pid_alive(self, pid):
        return None


def fresh(name: str) -> tuple[TaskStore, ControlPlane, ProjectService, dict]:
    db = ROOT / ".poc-state" / f"t28-{name}.db"
    for stale in [db, *db.parent.glob(f"t28-{name}.db.*")]:
        stale.unlink(missing_ok=True)
    store = TaskStore(db)
    adapter = StubAdapter()
    plane = ControlPlane(adapter, store, policy=LOW)
    project = plane.register_project(f"t28-{name}", REPO_A, default_adapter="stub",
                                     policy=LOW, concurrency_limit=2)
    return store, plane, ProjectService(plane), project


def seed(store: TaskStore, project_id: str) -> None:
    ctx = ProjectContextStore(store)
    ctx.add_context_entry(project_id, {
        "kind": "background", "title": "what this is",
        "body": "A demo repository used by the control-plane tests.",
        "verification": "verified", "source": "human:owner"})
    ctx.add_context_entry(project_id, {
        "kind": "constraints", "title": "no network",
        "body": "Do not add network calls to this repository.",
        "verification": "verified", "source": "human:owner", "tags": ["safety"]})
    ctx.add_context_entry(project_id, {
        "kind": "knowledge", "title": "test command",
        "body": "Run `python -m pytest` before claiming success.",
        "verification": "verified", "source": "human:owner", "tags": ["testing"]})


# --------------------------------------------------------------------------- #
def case_a_injection() -> dict:
    store, plane, service, project = fresh("a")
    seed(store, project["id"])
    rec = service.submit_project_task(
        project["id"], "add a health endpoint",
        assertions=[ArtifactAssertion(kind="file_exists", target="health.txt")],
        idempotency_key="t28-a-1")
    task = store.get(rec["task_id"])
    provenance = service.get_task_provenance(rec["task_id"])
    store.close()

    prompt = task["effective_prompt"] or ""
    return {
        "task_id": task["task_id"],
        "original_task": task["original_task"],
        "has_background": "A demo repository" in prompt,
        "has_constraint": "Do not add network calls" in prompt,
        "has_task": "add a health endpoint" in prompt,
        "context_version": task["context_version"],
        "entry_refs": task["context_entry_refs"],
        "workspace_id": task["workspace_id"],
        "prompt_equals_effective": task["prompt"] == task["effective_prompt"],
        "provenance_entries": len(provenance["context_entries"]),
        "pass": (task["original_task"] == "add a health endpoint"
                 and "A demo repository" in prompt
                 and "Do not add network calls" in prompt
                 and "add a health endpoint" in prompt
                 and task["context_version"] >= 1
                 and len(task["context_entry_refs"]) >= 2
                 and bool(task["workspace_id"])
                 and task["prompt"] == task["effective_prompt"]
                 and len(provenance["context_entries"]) == len(task["context_entry_refs"])),
    }


# --------------------------------------------------------------------------- #
def case_b_freeze() -> dict:
    store, plane, service, project = fresh("b")
    seed(store, project["id"])
    rec = service.submit_project_task(project["id"], "refactor the parser",
                                      idempotency_key="t28-b-1")
    before = store.get(rec["task_id"])
    prompt_before = before["effective_prompt"]
    version_before = before["context_version"]

    ctx = ProjectContextStore(store)
    entry = ctx.list_entries(project["id"], kind="constraints")[0]
    ctx.update_context_entry(project["id"], entry["id"], entry["version"],
                             {"body": "CHANGED-AFTER-ENQUEUE: never touch the parser."},
                             changed_by="human:owner")
    ctx.add_context_entry(project["id"], {
        "kind": "constraints", "title": "added later",
        "body": "LATE-CONSTRAINT: added after the task was queued.",
        "verification": "verified", "source": "human:owner"})

    after = store.get(rec["task_id"])
    project_version_now = store.context_version(project["id"])
    store.close()

    return {
        "prompt_unchanged": after["effective_prompt"] == prompt_before,
        "context_version_unchanged": after["context_version"] == version_before,
        "late_constraint_absent": "LATE-CONSTRAINT" not in (after["effective_prompt"] or ""),
        "changed_body_absent": "CHANGED-AFTER-ENQUEUE" not in (after["effective_prompt"] or ""),
        "project_version_advanced": project_version_now > version_before,
        "pass": (after["effective_prompt"] == prompt_before
                 and after["context_version"] == version_before
                 and "LATE-CONSTRAINT" not in (after["effective_prompt"] or "")
                 and "CHANGED-AFTER-ENQUEUE" not in (after["effective_prompt"] or "")
                 and project_version_now > version_before),
    }


# --------------------------------------------------------------------------- #
def case_c_approval_binding() -> dict:
    store, plane, service, project = fresh("c")
    seed(store, project["id"])
    ctx = ProjectContextStore(store)

    # 1. submit a high-risk project task: it must land in needs_approval
    rec = service.submit_project_task(
        project["id"], "write a marker file", policy=HIGH,
        idempotency_key="t28-c-1")
    queued = store.get(rec["task_id"])
    needs_approval = queued["queue_status"] == "needs_approval"
    digest_original = queued["task_digest"]

    # 2. approve exactly that content
    plane.approve(digest_original, note="human approved the original content")
    released = store.get(rec["task_id"])
    released_ok = released["queue_status"] == "ready"

    # 3. now change the project context and re-submit the same task under a new key:
    #    the injected content differs, so the digest differs and the old approval
    #    must NOT authorise it.
    entry = ctx.list_entries(project["id"], kind="constraints")[0]
    ctx.update_context_entry(project["id"], entry["id"], entry["version"],
                             {"body": "Do not add network calls. ALSO: no new files."},
                             changed_by="human:owner")
    rec2 = service.submit_project_task(
        project["id"], "write a marker file", policy=HIGH,
        idempotency_key="t28-c-2")
    second = store.get(rec2["task_id"])
    digest_second = second["task_digest"]
    second_needs_approval = second["queue_status"] == "needs_approval"

    # 4. a task whose stored digest is edited after approval must be re-gated at
    #    dispatch time (content changed -> recomputed digest -> no approval).
    tampered = store.get(rec["task_id"])
    store.update(tampered["task_id"], prompt=tampered["prompt"] + "\n# extra injected line\n")
    adapter_calls_before = plane.adapter.calls
    try:
        # dispatch_next re-gates internally: it returns the item in
        # `needs_approval` instead of raising, and must not call the worker.
        plane.dispatch_next("t28-owner", concurrency_limit=4)
        tamper_outcome = "returned"
    except ApprovalRequired:
        tamper_outcome = "refused"
    except Exception as exc:  # noqa: BLE001
        tamper_outcome = f"{type(exc).__name__}"
    tampered_after = store.get(tampered["task_id"])
    worker_calls_on_tamper = plane.adapter.calls - adapter_calls_before
    store.close()

    return {
        "first_needs_approval": needs_approval,
        "released_after_approval": released_ok,
        "digest_changed_with_context": digest_second != digest_original,
        "second_needs_approval": second_needs_approval,
        "tampered_dispatch": tamper_outcome,
        "tampered_queue_status": tampered_after["queue_status"],
        "worker_calls_on_tamper": worker_calls_on_tamper,
        "pass": (needs_approval and released_ok
                 and digest_second != digest_original
                 and second_needs_approval
                 and tamper_outcome == "returned"
                 and worker_calls_on_tamper == 0
                 and tampered_after["queue_status"] == "needs_approval"),
    }


# --------------------------------------------------------------------------- #
def case_d_blocked() -> dict:
    store, plane, service, project = fresh("d")
    seed(store, project["id"])
    adapter_calls_before = plane.adapter.calls
    try:
        service.submit_project_task(
            project["id"], "deploy to production",
            prerequisites=["deploy-credentials"], idempotency_key="t28-d-1")
        outcome = "submitted (bad)"
        gaps = []
    except ContextBuildBlocked as exc:
        outcome = "blocked"
        gaps = exc.gaps
    tasks_after = store.work_items_for_project(project["id"])
    store.close()
    return {
        "outcome": outcome,
        "gaps": gaps,
        "tasks_enqueued": len(tasks_after),
        "worker_calls": plane.adapter.calls - adapter_calls_before,
        "pass": (outcome == "blocked" and bool(gaps) and len(tasks_after) == 0
                 and plane.adapter.calls == adapter_calls_before),
    }


# --------------------------------------------------------------------------- #
def case_e_compat() -> dict:
    store, plane, service, project = fresh("e")
    adapter = plane.adapter
    # legacy path: no project context at all
    legacy = plane.enqueue("legacy task without context", REPO_A,
                           idempotency_key="t28-e-legacy")
    legacy_task = store.get(legacy["task_id"])

    # digest of a legacy task must equal the pre-M1 formula (no "scope" key)
    policy = DispatchPolicy(**(legacy_task.get("policy") or {}))
    recomputed = task_digest(legacy_task["prompt"], policy, [], legacy_task["cwd"])
    legacy_digest_matches = recomputed == legacy_task["task_digest"]

    dispatched = plane.dispatch("legacy direct dispatch", REPO_A,
                                idempotency_key="t28-e-dispatch")
    store.close()
    return {
        "legacy_enqueued": bool(legacy.get("task_id")),
        "legacy_has_no_context": legacy_task.get("context_version") is None,
        "legacy_has_no_original_task": legacy_task.get("original_task") is None,
        "legacy_digest_matches_pre_m1": legacy_digest_matches,
        "legacy_dispatch_ok": bool(dispatched.get("worker_job_id")),
        "worker_calls": adapter.calls,
        "pass": (bool(legacy.get("task_id"))
                 and legacy_task.get("context_version") is None
                 and legacy_task.get("original_task") is None
                 and legacy_digest_matches
                 and bool(dispatched.get("worker_job_id"))
                 and adapter.calls == 1),
    }


def main() -> int:
    log = {
        "test": "T28-project-dispatch",
        "A_context_injection": case_a_injection(),
        "B_prompt_frozen": case_b_freeze(),
        "C_approval_binding": case_c_approval_binding(),
        "D_blocked_prerequisite": case_d_blocked(),
        "E_legacy_compat": case_e_compat(),
    }
    keys = ("A_context_injection", "B_prompt_frozen", "C_approval_binding",
            "D_blocked_prerequisite", "E_legacy_compat")
    log["pass"] = {k: log[k]["pass"] for k in keys}
    log["result"] = "Pass" if all(log["pass"].values()) else "Fail"
    write_evidence(ROOT / "evidence" / "project" / "T28-project-dispatch.json", log)
    print(json.dumps(log, ensure_ascii=False, indent=1)[:5000])
    print("RESULT:", log["result"])
    return 0 if log["result"] == "Pass" else 1


if __name__ == "__main__":
    sys.exit(main())
