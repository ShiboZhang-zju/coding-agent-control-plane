"""T56: recall pagination budget safety and durable inspect summaries.

Deterministic only.  No model or Worker is started.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.project_service import ProjectService  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from ld.coordinator import LDCoordinator  # noqa: E402
from ld.goal_store import LDGoalStore, goal_identity  # noqa: E402
from ld.recall import RecallError  # noqa: E402
from ld.tools import LDTools  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

FIXTURE = ROOT / "sandbox" / "t56-recall-pagination"
LEDGER = ROOT / ".poc-state" / "t56-recall-pagination.db"
EVIDENCE = ROOT / "evidence" / "project" / "T56-recall-pagination-inspect.json"


class AdapterRef:
    name = "stub"


def fresh() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)
    FIXTURE.mkdir(parents=True)
    subprocess.run(["git", "-C", str(FIXTURE), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(FIXTURE), "config", "user.email",
         "t56@example.invalid"], check=True)
    subprocess.run(
        ["git", "-C", str(FIXTURE), "config", "user.name", "T56"],
        check=True)
    (FIXTURE / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(FIXTURE), "add", "seed.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(FIXTURE), "commit", "-q", "-m", "seed"],
        check=True)
    for stale in [LEDGER, *LEDGER.parent.glob(LEDGER.name + ".*")]:
        stale.unlink(missing_ok=True)


def caught(operation) -> str | None:
    try:
        operation()
    except RecallError as exc:
        return str(exc)
    return None


def main() -> int:
    fresh()
    store = TaskStore(LEDGER)
    plane = ControlPlane(
        AdapterRef(), store, require_registered_projects=True,
        worker_instance_id="wi-t56")
    service = ProjectService(plane)
    project = plane.register_project(
        "t56-recall-pagination", str(FIXTURE), default_adapter="stub")
    goals = LDGoalStore(LEDGER)
    tools = LDTools(service, goal_store=goals)
    expected_goal_ids = []
    for index in range(36):
        text = f"T56 paged goal {index:03d}"
        goal_id, goal_ref = goal_identity(text, project["id"])
        goals.get_or_create(goal_id, project["id"], text, goal_ref)
        expected_goal_ids.append(goal_id)

    first = tools.get_recall_summary(
        project["id"], object_type="project", limit=5,
        char_budget=50_000, max_output_bytes=65_536,
        allowed_security_domains=("shareable",))
    cursor = first["continuation"]["next_cursor"]
    normal_all = tools.get_recall_summary(
        project["id"], object_type="project", limit=100,
        char_budget=50_000, max_output_bytes=65_536,
        allowed_security_domains=("shareable",))
    normal_goal_ids = [
        item["object_id"] for item in normal_all["object"]["refs"]
        if item["object_type"] == "goal"]
    budget_error = caught(lambda: tools.get_recall_summary(
        project["id"], object_type="project", limit=20, cursor=cursor,
        char_budget=50_000, max_total_nodes=10_000, max_depth=32,
        max_output_bytes=1_024,
        allowed_security_domains=("shareable",)))
    retry = tools.get_recall_summary(
        project["id"], object_type="project", limit=20, cursor=cursor,
        char_budget=50_000, max_total_nodes=10_000, max_depth=32,
        max_output_bytes=65_536,
        allowed_security_domains=("shareable",))
    retry_goal_ids = [
        item["object_id"] for item in retry["object"]["refs"]
        if item["object_type"] == "goal"]

    task = service.submit_project_task(
        project["id"], "Create long evidence for inspect continuation",
        idempotency_key="t56-long-evidence")
    attempt = store.add_attempt(
        task["task_id"], worker_job_id="job-t56", state="succeeded",
        reason="deterministic fixture")
    store.add_evidence(
        task["task_id"], "long_trace", {
            "security_domain": "shareable",
            "payload": "x" * 8_000,
            "critical": "T56-CRITICAL-EVIDENCE",
        }, attempt_id=attempt["id"])
    evidence_id = store.evidence(task["task_id"])[-1]["id"]
    detail = tools.get_recall_detail(
        project["id"], "evidence", f"evidence:{evidence_id}",
        char_budget=12_000, max_output_bytes=32_000,
        allowed_security_domains=("shareable",))
    inspect_summary = LDCoordinator._inspection_summary({
        "action": "inspect", "result": detail})
    encoded_summary = json.dumps(
        inspect_summary, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"))
    restored_summary = json.loads(encoded_summary)
    restored_mapping = restored_summary if isinstance(restored_summary, dict) else {}
    restored_object = restored_mapping.get("object") or {}
    restored_detail = restored_mapping.get("detail") or {}

    checks = {
        "normal_first_page_has_cursor": bool(cursor),
        "budget_degraded_middle_page_fails_closed": bool(
            budget_error and "reduce limit" in budget_error),
        "failed_page_returns_no_cursor_object": bool(budget_error),
        "retry_from_original_cursor_recovers_expected_ids": (
            retry_goal_ids == normal_goal_ids[5:25]
            and set(normal_goal_ids) == set(expected_goal_ids)),
        "inspect_summary_is_structured": isinstance(inspect_summary, dict),
        "inspect_summary_is_bounded": len(encoded_summary.encode("utf-8")) <= 1_600,
        "inspect_summary_keeps_object_identity": (
            restored_object.get("object_type") == "evidence"
            and restored_object.get("object_id") == f"evidence:{evidence_id}"),
        "inspect_summary_keeps_status": (
            restored_object.get("status") == "long_trace"),
        "inspect_summary_keeps_evidence_identity": (
            restored_detail.get("evidence_id") == f"evidence:{evidence_id}"
            and restored_detail.get("work_item_id") == task["task_id"]),
        "inspect_summary_marks_truncation": (
            restored_mapping.get("truncated") is True),
        "inspect_summary_survives_json_roundtrip": (
            json.loads(encoded_summary) == inspect_summary),
    }
    failed = [name for name, passed in checks.items() if not passed]
    evidence = {
        "test": "T56 recall pagination budget and inspect continuity",
        "worker_started": False,
        "real_model_called": False,
        "fixture": "<REPO>/sandbox/t56-recall-pagination",
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
