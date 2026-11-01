"""T54: M4-E deterministic relational recall (no model, no Worker).

Covers the minimum Chain-of-Recall / Inline-and-Spill contract over the existing
Goal store, Ledger and Project Context.  The test intentionally creates long
local Evidence payloads, then proves that bounded summary reads retain ids while
unauthorized views cannot read the payloads.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import TaskState, Verdict  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.project_service import ProjectService  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from ld.coordinator import LDCoordinator  # noqa: E402
from ld.decision import DecisionPolicyError, LDDecision  # noqa: E402
from ld.goal_store import LDGoalStore, goal_identity  # noqa: E402
from ld.recall import RecallAccessError  # noqa: E402
from ld.tools import LDTools  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

FIXTURE = ROOT / "sandbox" / "t54-recall"
REPO_A = FIXTURE / "project-a"
REPO_B = FIXTURE / "project-b"
LEDGER = ROOT / ".poc-state" / "t54-relational-recall.db"
EVIDENCE = ROOT / "evidence" / "project" / "T54-relational-recall.json"
SECRET = "T54-LOCAL-ONLY-BLUE-WHALE"


class AdapterRef:
    name = "stub"


def fresh() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)
    REPO_A.mkdir(parents=True)
    REPO_B.mkdir(parents=True)
    for repo in (REPO_A, REPO_B):
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email",
             "t54@example.invalid"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.name", "T54"],
            check=True)
        (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(repo), "add", "seed.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-q", "-m", "seed"],
            check=True)
    for stale in [LEDGER, *LEDGER.parent.glob(LEDGER.name + ".*")]:
        stale.unlink(missing_ok=True)


def catches(exc_type, operation) -> str | None:
    try:
        operation()
    except exc_type as exc:
        return str(exc)
    return None


def build_fixture() -> dict:
    store = TaskStore(LEDGER)
    plane = ControlPlane(
        AdapterRef(), store, require_registered_projects=True,
        worker_instance_id="wi-t54")
    service = ProjectService(plane)
    goals = LDGoalStore(LEDGER)
    tools = LDTools(service, goal_store=goals)

    project_a = plane.register_project(
        "t54-a", str(REPO_A), default_adapter="stub")
    project_b = plane.register_project(
        "t54-b", str(REPO_B), default_adapter="stub")

    shareable = service.context.add_context_entry(project_a["id"], {
        "kind": "architecture", "title": "Public boundary",
        "body": "The verifier is the acceptance authority.",
        "verification": "verified", "security_domain": "shareable",
        "source": "human:owner",
        "evidence_refs": ["evidence/project/T54-relational-recall.json"],
        "tags": ["recall"],
    })
    local = service.context.add_context_entry(project_a["id"], {
        "kind": "knowledge", "title": "Internal codename",
        "body": SECRET + " " + ("private detail " * 300),
        "verification": "candidate", "security_domain": "local",
        "source": "human:owner", "evidence_refs": [],
        "tags": ["internal"],
    })

    goal_text = "Prove relational recall without copying task truth"
    goal_id, goal_ref = goal_identity(goal_text, project_a["id"])
    goals.get_or_create(goal_id, project_a["id"], goal_text, goal_ref)

    task = tools.submit_project_task(
        project_a["id"], "Produce the accepted recall fixture",
        assertions=[{"kind": "file_exists", "target": "result.txt"}],
        idempotency_key="t54-main-task", title="T54 accepted task",
        goal_ref=goal_ref)
    task_id = task["task_id"]
    attempt = store.add_attempt(
        task_id, worker_job_id="job-t54-main", reason="initial execution",
        state="dispatched")
    session = store.observe_session_reference(
        work_item_id=task_id, worker_job_id="job-t54-main", node_id="local",
        worker_instance_id="wi-t54", adapter="stub", session_id="sess-t54-main")
    store.append_event(
        task_id, "worker_result", {"summary": "fixture completed"},
        attempt_id=attempt["id"])
    store.add_evidence(
        task_id, "verifier", {
            "security_domain": "shareable", "accepted": True,
            "assertion": "result.txt exists",
        }, attempt_id=attempt["id"])
    shareable_evidence = store.evidence(task_id)[-1]
    store.add_evidence(
        task_id, "worker_log", {
            "security_domain": "local", "log": SECRET + ("-long" * 2_000),
        }, attempt_id=attempt["id"])
    local_evidence = store.evidence(task_id)[-1]
    store.add_evidence(
        task_id, "numeric_trace", {
            "security_domain": "shareable", "values": list(range(1_000)),
        }, attempt_id=attempt["id"])
    bounded_evidence = store.evidence(task_id)[-1]
    # Long history: the recall response must be bounded even though all rows are
    # durable and individually addressable.
    for index in range(30):
        store.add_evidence(
            task_id, "trace", {
                "security_domain": "local",
                "index": index, "detail": SECRET + ("x" * 2_000),
            }, attempt_id=attempt["id"])
    store.close_attempt(attempt["id"], "succeeded")
    store.update(
        task_id, state=TaskState.SUCCEEDED.value,
        verdict=Verdict.VERIFIED.value, queue_status="done",
        session_id="sess-t54-main", finished_at=100.0,
        assertion_results=[{
            "kind": "file_exists", "target": "result.txt", "passed": True,
        }])

    result_entry = service.context.add_context_entry(project_a["id"], {
        "kind": "progress", "title": "Accepted recall fixture",
        "body": "The declared fixture was accepted by the verifier.",
        "verification": "candidate", "security_domain": "shareable",
        "source": f"task:{task_id}",
        "evidence_refs": [
            f"ledger:evidence:{shareable_evidence['id']}",
            "evidence/project/T54-relational-recall.json",
        ],
        "tags": [f"task:{task_id}", f"attempt:{attempt['id']}"],
    })
    result_entry = service.context.verify_entry(
        project_a["id"], result_entry["id"], result_entry["version"],
        evidence_refs=result_entry["evidence_refs"], changed_by="t54",
        note="verified by deterministic fixture")

    goals.record_step(
        goal_id, 1,
        {"action": "finish", "project_id": project_a["id"],
         "reason": "the goal task is verified",
         "evidence_refs": [f"task:{task_id}"],
         "parameters": {"verified_task_ids": [task_id],
                        "completion_conditions": []}},
        {"action": "finish", "next_state": "finished", "task_id": None,
         "executed": True, "blocking_reasons": []},
        {"status": "completed", "current_task_id": task_id,
         "task_count": 1,
         "last_decision": {"action": "finish", "next_state": "finished"}},
        token=None)

    blocker_goal_text = "Wait for a human to resolve an observed binding conflict"
    blocker_goal_id, blocker_goal_ref = goal_identity(
        blocker_goal_text, project_a["id"])
    goals.get_or_create(
        blocker_goal_id, project_a["id"], blocker_goal_text, blocker_goal_ref)
    blocker = tools.submit_project_task(
        project_a["id"], "Blocked fixture",
        idempotency_key="t54-blocked-task", title="T54 blocked task",
        goal_ref=blocker_goal_ref)
    store.update(
        blocker["task_id"], state=TaskState.UNKNOWN.value,
        verdict=Verdict.PENDING.value, queue_status="needs_human",
        warnings=["SESSION_BINDING_CONFLICT: fixture conflict"])

    other_goal_id, other_goal_ref = goal_identity(
        "Other project goal", project_b["id"])
    goals.get_or_create(
        other_goal_id, project_b["id"], "Other project goal", other_goal_ref)
    other_task = tools.submit_project_task(
        project_b["id"], "Other project task",
        idempotency_key="t54-other-task", goal_ref=other_goal_ref)

    return {
        "store": store, "service": service, "goals": goals, "tools": tools,
        "project_a": project_a, "project_b": project_b,
        "goal_id": goal_id, "task_id": task_id, "attempt_id": attempt["id"],
        "session_id": session["id"], "shareable_entry_id": result_entry["id"],
        "local_entry_id": local["id"],
        "shareable_evidence_id": f"evidence:{shareable_evidence['id']}",
        "local_evidence_id": f"evidence:{local_evidence['id']}",
        "bounded_evidence_id": f"evidence:{bounded_evidence['id']}",
        "blocker_task_id": blocker["task_id"],
        "other_task_id": other_task["task_id"],
        "initial_shareable_entry_id": shareable["id"],
    }


def run_checks(ctx: dict) -> dict[str, object]:
    tools: LDTools = ctx["tools"]
    project_id = ctx["project_a"]["id"]

    before = {
        "task_updated_at": ctx["store"].get(ctx["task_id"])["updated_at"],
        "goal_updated_at": ctx["goals"].get(ctx["goal_id"])["updated_at"],
        "context_version": ctx["store"].context_version(project_id),
        "context_revisions": len(ctx["store"].context_revisions(
            ctx["shareable_entry_id"])),
        "task_count": len(ctx["store"].work_items_for_project(project_id)),
    }

    project_summary = tools.get_recall_summary(
        project_id, object_type="project", limit=100, char_budget=4_000)
    goal_summary = tools.get_recall_summary(
        project_id, object_type="goal", object_id=ctx["goal_id"],
        limit=100, char_budget=4_000)
    task_summary = tools.get_recall_summary(
        project_id, object_type="task", object_id=ctx["task_id"],
        limit=100, char_budget=4_000)
    task_detail = tools.get_recall_detail(
        project_id, "task", ctx["task_id"], limit=100, char_budget=8_000)
    attempt_detail = tools.get_recall_detail(
        project_id, "attempt", ctx["attempt_id"], limit=100,
        char_budget=4_000)
    context_summary = tools.get_recall_summary(
        project_id, object_type="context_entry",
        object_id=ctx["shareable_entry_id"], limit=20, char_budget=2_000)
    context_detail = tools.get_recall_detail(
        project_id, "context_entry", ctx["shareable_entry_id"],
        limit=20, char_budget=4_000)
    evidence_detail = tools.get_recall_detail(
        project_id, "evidence", ctx["shareable_evidence_id"],
        limit=20, char_budget=2_000,
        allowed_security_domains=("shareable",))
    bounded_evidence_detail = tools.get_recall_detail(
        project_id, "evidence", ctx["bounded_evidence_id"],
        limit=5, char_budget=2_000,
        allowed_security_domains=("shareable",))
    blocker_detail = tools.get_recall_detail(
        project_id, "task", ctx["blocker_task_id"],
        limit=20, char_budget=2_000)

    project_refs = project_summary["object"]["refs"]
    goal_refs = goal_summary["object"]["refs"]
    task_refs = task_summary["object"]["refs"]
    context_refs = context_summary["object"]["refs"]
    attempt_refs = attempt_detail["object"]["refs"]

    safe_project = tools.get_recall_summary(
        project_id, object_type="project", limit=100, char_budget=4_000,
        allowed_security_domains=("shareable",))
    safe_task = tools.get_recall_detail(
        project_id, "task", ctx["task_id"], limit=100,
        char_budget=4_000, allowed_security_domains=("shareable",))
    safe_text = json.dumps(
        {"project": safe_project, "task": safe_task}, ensure_ascii=False)
    local_context_error = catches(
        RecallAccessError,
        lambda: tools.get_recall_detail(
            project_id, "context_entry", ctx["local_entry_id"],
            allowed_security_domains=("shareable",)))
    local_evidence_error = catches(
        RecallAccessError,
        lambda: tools.get_recall_detail(
            project_id, "evidence", ctx["local_evidence_id"],
            allowed_security_domains=("shareable",)))

    unknown_error = catches(
        KeyError,
        lambda: tools.get_recall_detail(project_id, "task", "task-missing"))
    cross_project_error = catches(
        RecallAccessError,
        lambda: tools.get_recall_detail(
            project_id, "task", ctx["other_task_id"]))

    bounded = tools.get_recall_summary(
        project_id, object_type="task", object_id=ctx["task_id"],
        limit=5, char_budget=160)

    class RecallDecision:
        def decide(self, _decision_input):  # noqa: ANN001
            return {
                "action": "inspect", "project_id": project_id,
                "reason": "expand the accepted task from durable ids",
                "evidence_refs": [f"task:{ctx['task_id']}"],
                "parameters": {
                    "tool": "get_recall_detail", "object_type": "task",
                    "object_id": ctx["task_id"], "limit": 10,
                    "char_budget": 2_000,
                },
            }

    coordinator_result = LDCoordinator(
        tools, RecallDecision(),
        model_authorized_security_domains=("shareable",),
        goal_security_domain="shareable").run_once(
            "inspect accepted task", project_id=project_id)
    coordinator_text = json.dumps(coordinator_result, ensure_ascii=False)

    wrong_tool_parameter = catches(
        DecisionPolicyError,
        lambda: LDDecision.from_mapping({
            "action": "inspect", "project_id": project_id,
            "reason": "a non-recall tool must not accept recall parameters",
            "evidence_refs": [],
            "parameters": {
                "tool": "get_project_brief", "char_budget": 2_000,
            },
        }))

    after = {
        "task_updated_at": ctx["store"].get(ctx["task_id"])["updated_at"],
        "goal_updated_at": ctx["goals"].get(ctx["goal_id"])["updated_at"],
        "context_version": ctx["store"].context_version(project_id),
        "context_revisions": len(ctx["store"].context_revisions(
            ctx["shareable_entry_id"])),
        "task_count": len(ctx["store"].work_items_for_project(project_id)),
    }

    return {
        "project_lists_goal": any(
            ref["object_type"] == "goal" and ref["object_id"] == ctx["goal_id"]
            for ref in project_refs),
        "goal_lists_task": any(
            ref["object_type"] == "task" and ref["object_id"] == ctx["task_id"]
            for ref in goal_refs),
        "task_lists_attempt": any(
            ref["object_type"] == "attempt"
            and ref["object_id"] == ctx["attempt_id"] for ref in task_refs),
        "task_lists_session": any(
            ref["object_type"] == "session"
            and ref["object_id"] == ctx["session_id"] for ref in task_refs),
        "task_lists_evidence": any(
            ref["object_type"] == "evidence"
            and ref["object_id"] == ctx["shareable_evidence_id"]
            for ref in task_refs),
        "attempt_links_session_and_evidence": (
            any(ref["object_type"] == "session" for ref in attempt_refs)
            and any(ref["object_type"] == "evidence" for ref in attempt_refs)),
        "task_detail_reports_execution_result": (
            task_detail["detail"]["execution_result"]["verifier_verdict"]
            == Verdict.VERIFIED.value
            and task_detail["detail"]["attempt_count"] >= 1
            and task_detail["detail"]["evidence_count"] >= 2),
        "verified_result_expands_evidence": (
            evidence_detail["detail"]["kind"] == "verifier"
            and evidence_detail["detail"]["payload"]["accepted"] is True),
        "context_source_links_task": any(
            ref["object_type"] == "task" and ref["object_id"] == ctx["task_id"]
            for ref in context_refs),
        "context_exposes_evidence_references": any(
            ref["object_type"] == "evidence_ref" for ref in context_refs),
        "context_detail_has_source_and_verification": (
            context_detail["detail"]["source"] == f"task:{ctx['task_id']}"
            and context_detail["detail"]["verification"] == "verified"),
        "summary_detail_identity_consistent": (
            context_summary["object"]["object_id"]
            == context_detail["object"]["object_id"]
            == ctx["shareable_entry_id"]),
        "unknown_id_rejected": bool(unknown_error and "no task" in unknown_error),
        "cross_project_rejected": bool(
            cross_project_error and "belongs to project" in cross_project_error),
        "shareable_view_has_no_local_secret": SECRET not in safe_text,
        "shareable_view_omits_local_context_id": (
            ctx["local_entry_id"] not in safe_text),
        "direct_local_context_refused": bool(local_context_error),
        "direct_local_evidence_refused": bool(local_evidence_error),
        "effective_prompt_never_recalled": (
            "effective_prompt" not in safe_text
            or safe_task["detail"].get("effective_prompt_omitted") is True),
        "blocked_reason_is_ledger_derived": (
            "session_binding_conflict"
            in blocker_detail["detail"]["why_attention"]["reasons"]
            and blocker_detail["detail"]["why_attention"]["requires_human"]),
        "query_has_zero_state_side_effects": before == after,
        "bounded_reference_count": len(bounded["object"]["refs"]) <= 5,
        "bounded_content_chars": (
            bounded["budget"]["content_chars_used"] <= 160),
        "bounded_history_reports_continuation": (
            bounded["continuation"]["has_more"] is True
            and bounded["object"]["related_omitted"] > 0),
        "nested_payload_item_limit_enforced": (
            len(bounded_evidence_detail["detail"]["payload"]["values"]) <= 5
            and bounded_evidence_detail["budget"]["content_truncated"] is True),
        "recall_parameters_rejected_on_other_tools": bool(
            wrong_tool_parameter and "does not accept recall parameters"
            in wrong_tool_parameter),
        "ld_inspect_uses_recall_facade": (
            coordinator_result["action"] == "inspect"
            and coordinator_result["next_state"] == "inspection_complete"
            and coordinator_result["result"]["schema_version"]
            == "m4-e.recall.v1"),
        "ld_inspect_respects_security_domains": SECRET not in coordinator_text,
    }


def restart_checks(ctx: dict) -> dict[str, bool]:
    project_id = ctx["project_a"]["id"]
    task_id = ctx["task_id"]
    goal_id = ctx["goal_id"]
    ctx["store"].close()
    ctx["goals"].close()

    store = TaskStore(LEDGER)
    goals = LDGoalStore(LEDGER, read_only=True)
    plane = ControlPlane(AdapterRef(), store, require_registered_projects=True)
    tools = LDTools(ProjectService(plane), goal_store=goals)
    try:
        goal = tools.get_recall_detail(
            project_id, "goal", goal_id, limit=20, char_budget=2_000)
        task = tools.get_recall_detail(
            project_id, "task", task_id, limit=20, char_budget=2_000)
        return {
            "goal_found_after_restart": goal["object"]["object_id"] == goal_id,
            "task_found_after_restart": task["object"]["object_id"] == task_id,
            "task_verdict_persisted": (
                task["detail"]["execution_result"]["verifier_verdict"]
                == Verdict.VERIFIED.value),
        }
    finally:
        goals.close()
        store.close()


def main() -> int:
    fresh()
    ctx = build_fixture()
    checks: dict[str, object] = {}
    try:
        checks.update(run_checks(ctx))
        checks.update(restart_checks(ctx))
    except Exception as exc:  # noqa: BLE001 - section failure is explicit evidence
        checks["section_error"] = f"{type(exc).__name__}: {exc}"
        try:
            ctx["goals"].close()
        except Exception:
            pass
        try:
            ctx["store"].close()
        except Exception:
            pass

    failed = [key for key, value in checks.items()
              if key == "section_error" or (isinstance(value, bool) and not value)]
    evidence = {
        "test": "T54 M4-E relational recall",
        "worker_started": False,
        "real_model_called": False,
        "scripted_decision_called": True,
        "fixture": "<REPO>/sandbox/t54-recall",
        "note": (
            "Deterministic only: existing Goal/Ledger/Context ids are queried "
            "through the restricted read facade. No real model or Worker was "
            "started; this evidence does not validate semantic retrieval quality."),
        "checks": checks,
        "result": "PASS" if not failed else "FAIL",
        "failed": failed,
    }
    write_evidence(EVIDENCE, evidence)
    print(json.dumps({"result": evidence["result"], "failed": failed},
                     ensure_ascii=False))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
