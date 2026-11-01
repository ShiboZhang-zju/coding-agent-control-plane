"""T55: M4-E recall resource/safety/pagination closure.

Deterministic only: no decision model and no Worker are started.  The fixture
proves that relation pages are reachable, cursors fail closed after mutation,
external references are projected, unauthorized local Evidence IDs stay
hidden, and a deeply nested payload cannot exceed whole-response limits.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.project_service import ProjectService  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from ld.goal_store import LDGoalStore, goal_identity  # noqa: E402
from ld.recall import RecallAccessError, RecallError  # noqa: E402
from ld.tools import LDTools  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

FIXTURE = ROOT / "sandbox" / "t55-recall-closure"
LEDGER = ROOT / ".poc-state" / "t55-recall-closure.db"
EVIDENCE = ROOT / "evidence" / "project" / "T55-recall-resource-closure.json"
SECRET = "T55-LOCAL-REFERENCE-MUST-NOT-LEAK"


class AdapterRef:
    name = "stub"


def fresh() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)
    FIXTURE.mkdir(parents=True)
    subprocess.run(["git", "-C", str(FIXTURE), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(FIXTURE), "config", "user.email",
         "t55@example.invalid"], check=True)
    subprocess.run(
        ["git", "-C", str(FIXTURE), "config", "user.name", "T55"],
        check=True)
    (FIXTURE / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(FIXTURE), "add", "seed.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(FIXTURE), "commit", "-q", "-m", "seed"],
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
        worker_instance_id="wi-t55")
    service = ProjectService(plane)
    goals = LDGoalStore(LEDGER)
    tools = LDTools(service, goal_store=goals)
    project = plane.register_project(
        "t55-recall-closure", str(FIXTURE), default_adapter="stub")

    external_evidence_ref = (
        "file:///private/" + SECRET + "/" + ("long-local-segment-" * 300))
    checkpoint_ref = (
        "refs/private/" + SECRET + "/" + ("checkpoint-segment-" * 300))
    entry = service.context.add_context_entry(project["id"], {
        "kind": "knowledge",
        "title": "Shareable recall closure entry",
        "body": "The relation itself is shareable; its local path is not.",
        "verification": "candidate",
        "security_domain": "shareable",
        "source": "file:///private/" + SECRET + "/source.md",
        "evidence_refs": [external_evidence_ref],
        "tags": ["t55"],
    })

    goal_ids = []
    for index in range(47):
        text = f"T55 paged goal {index:03d}"
        goal_id, goal_ref = goal_identity(text, project["id"])
        goals.get_or_create(goal_id, project["id"], text, goal_ref)
        goal_ids.append(goal_id)

    task = service.submit_project_task(
        project["id"], "Exercise bounded recall closure",
        idempotency_key="t55-closure-task",
        metadata={
            "handoff": True,
            "checkpoint_ref": checkpoint_ref,
            "handoff_signature": "sha256:" + ("a" * 64),
        })
    attempt = store.add_attempt(
        task["task_id"], worker_job_id="job-t55", state="dispatched",
        reason="deterministic fixture")
    nested_payload = [
        [[number for number in range(25)] for _ in range(25)]
        for _ in range(25)
    ]
    store.add_evidence(
        task["task_id"], "nested_trace", {
            "security_domain": "shareable", "tree": nested_payload,
        }, attempt_id=attempt["id"])
    shareable_evidence = store.evidence(task["task_id"])[-1]
    store.add_evidence(
        task["task_id"], "local_trace", {
            "security_domain": "local", "secret": SECRET,
        }, attempt_id=attempt["id"])
    local_evidence = store.evidence(task["task_id"])[-1]
    return {
        "store": store, "goals": goals, "tools": tools,
        "project_id": project["id"], "goal_ids": goal_ids,
        "task_id": task["task_id"], "entry_id": entry["id"],
        "shareable_evidence_id": f"evidence:{shareable_evidence['id']}",
        "local_evidence_id": f"evidence:{local_evidence['id']}",
        "external_ref": external_evidence_ref,
        "checkpoint_ref": checkpoint_ref,
    }


def run_checks(ctx: dict) -> dict[str, bool]:
    tools: LDTools = ctx["tools"]
    project_id = ctx["project_id"]
    domains = ("shareable",)
    before = {
        "task_updated_at": ctx["store"].get(ctx["task_id"])["updated_at"],
        "goal_updated_at": ctx["goals"].get(ctx["goal_ids"][0])["updated_at"],
        "context_version": ctx["store"].context_version(project_id),
    }

    pages = []
    cursor = None
    first_cursor = None
    while True:
        page = tools.get_recall_summary(
            project_id, object_type="project", limit=7, cursor=cursor,
            char_budget=4_000, allowed_security_domains=domains)
        refs = page["object"]["refs"]
        pages.extend(refs)
        cursor = page["continuation"]["next_cursor"]
        if first_cursor is None:
            first_cursor = cursor
        if cursor is None:
            break

    identities = [
        (item["object_type"], item["object_id"], item["relation"])
        for item in pages]
    goal_ids_seen = {
        item["object_id"] for item in pages
        if item["object_type"] == "goal"}
    after_reads = {
        "task_updated_at": ctx["store"].get(ctx["task_id"])["updated_at"],
        "goal_updated_at": ctx["goals"].get(ctx["goal_ids"][0])["updated_at"],
        "context_version": ctx["store"].context_version(project_id),
    }

    wrong_scope = catches(
        RecallAccessError,
        lambda: tools.get_recall_summary(
            project_id, object_type="task", object_id=ctx["task_id"],
            limit=7, cursor=first_cursor,
            allowed_security_domains=domains))
    malformed_cursor = catches(
        RecallError,
        lambda: tools.get_recall_summary(
            project_id, object_type="project", cursor="not-a-cursor",
            allowed_security_domains=domains))

    safe_context_summary = tools.get_recall_summary(
        project_id, object_type="context_entry", object_id=ctx["entry_id"],
        allowed_security_domains=domains)
    safe_context_detail = tools.get_recall_detail(
        project_id, "context_entry", ctx["entry_id"],
        allowed_security_domains=domains)
    operator_context_detail = tools.get_recall_detail(
        project_id, "context_entry", ctx["entry_id"], char_budget=50_000)
    safe_task_summary = tools.get_recall_summary(
        project_id, object_type="task", object_id=ctx["task_id"],
        limit=100, allowed_security_domains=domains)
    safe_task_detail = tools.get_recall_detail(
        project_id, "task", ctx["task_id"], limit=100,
        allowed_security_domains=domains)
    safe_projection = json.dumps({
        "context_summary": safe_context_summary,
        "context_detail": safe_context_detail,
        "task_summary": safe_task_summary,
        "task_detail": safe_task_detail,
    }, ensure_ascii=False)
    operator_projection = json.dumps(
        operator_context_detail, ensure_ascii=False)

    bounded = tools.get_recall_detail(
        project_id, "evidence", ctx["shareable_evidence_id"],
        limit=100, char_budget=50_000, max_total_nodes=64,
        max_depth=3, max_output_bytes=1_200,
        allowed_security_domains=domains)
    bounded_bytes = len(json.dumps(
        bounded, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8"))

    cli_base = [
        sys.executable, str(ROOT / "scripts" / "ld_cli.py"),
        "--ledger", str(LEDGER), "--compact", "recall", project_id,
        "--limit", "5", "--domains", "shareable",
    ]
    cli_first = json.loads(subprocess.run(
        cli_base, check=True, text=True, capture_output=True).stdout)
    cli_cursor = cli_first["continuation"]["next_cursor"]
    cli_second = json.loads(subprocess.run(
        [*cli_base, "--cursor", cli_cursor], check=True, text=True,
        capture_output=True).stdout)
    cli_first_ids = {
        (item["object_type"], item["object_id"], item["relation"])
        for item in cli_first["object"]["refs"]}
    cli_second_ids = {
        (item["object_type"], item["object_id"], item["relation"])
        for item in cli_second["object"]["refs"]}

    def exceed_scan_limit() -> None:
        with patch("ld.recall.MAX_RELATION_SCAN", 5):
            tools.get_recall_summary(
                project_id, object_type="project", limit=5,
                allowed_security_domains=domains)

    scan_limit_error = catches(RecallError, exceed_scan_limit)

    stale_cursor = first_cursor
    new_text = "T55 mutation invalidates an old cursor"
    new_goal_id, new_goal_ref = goal_identity(new_text, project_id)
    ctx["goals"].get_or_create(
        new_goal_id, project_id, new_text, new_goal_ref)
    stale_error = catches(
        RecallError,
        lambda: tools.get_recall_summary(
            project_id, object_type="project", limit=7,
            cursor=stale_cursor, allowed_security_domains=domains))

    fingerprint_ids = [
        item["object_id"]
        for item in safe_context_summary["object"]["refs"]
        if item.get("reference_kind") == "external"]
    return {
        "multi_page_all_goal_ids_reachable": goal_ids_seen == set(ctx["goal_ids"]),
        "multi_page_has_no_duplicates": len(identities) == len(set(identities)),
        "multi_page_order_is_stable": identities == sorted(identities),
        "cursor_is_bounded": bool(first_cursor and len(first_cursor) <= 2_048),
        "cursor_scope_is_enforced": bool(
            wrong_scope and "does not belong" in wrong_scope),
        "malformed_cursor_is_rejected": bool(malformed_cursor),
        "stale_cursor_is_rejected": bool(
            stale_error and "relation set changed" in stale_error),
        "external_refs_use_fixed_fingerprint": bool(
            fingerprint_ids
            and all(item.startswith("external-ref:sha256:")
                    and len(item) == len("external-ref:sha256:") + 64
                    for item in fingerprint_ids)),
        "model_view_hides_external_raw_values": (
            ctx["external_ref"] not in safe_projection
            and ctx["checkpoint_ref"] not in safe_projection
            and SECRET not in safe_projection),
        "operator_detail_can_audit_raw_reference": (
            ctx["external_ref"] in operator_projection),
        "unauthorized_local_evidence_id_is_hidden": (
            ctx["local_evidence_id"] not in safe_projection
            and "local_trace" not in safe_projection),
        "whole_response_byte_limit_is_hard": bounded_bytes <= 1_200,
        "whole_response_node_limit_is_hard": (
            bounded["budget"]["total_nodes_used"] <= 64),
        "whole_response_depth_limit_is_hard": (
            bounded["budget"]["deepest_depth"] <= 3),
        "deep_payload_is_marked_truncated": (
            bounded["budget"]["truncated"] is True
            and bool(bounded["budget"]["truncation_reasons"])),
        "reported_output_bytes_are_exact": (
            bounded["budget"]["output_bytes"] == bounded_bytes),
        "relation_scan_limit_fails_closed": bool(
            scan_limit_error and "scan exceeds maximum" in scan_limit_error),
        "cli_cursor_reads_next_page": bool(
            cli_cursor and cli_first_ids and cli_second_ids
            and cli_first_ids.isdisjoint(cli_second_ids)),
        "queries_have_zero_state_side_effects": before == after_reads,
    }


def main() -> int:
    fresh()
    ctx = build_fixture()
    checks: dict[str, object] = {}
    try:
        checks.update(run_checks(ctx))
    except Exception as exc:  # noqa: BLE001 - evidence records section failure
        checks["section_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        ctx["goals"].close()
        ctx["store"].close()

    failed = [
        key for key, value in checks.items()
        if key == "section_error" or (isinstance(value, bool) and not value)]
    evidence = {
        "test": "T55 M4-E recall resource closure",
        "worker_started": False,
        "real_model_called": False,
        "fixture": "<REPO>/sandbox/t55-recall-closure",
        "note": (
            "Deterministic only. Proves hard output bounds, safe external "
            "reference projection and stable cursor pagination; it does not "
            "validate semantic retrieval by a real model."),
        "checks": checks,
        "result": "PASS" if not failed else "FAIL",
        "failed": failed,
    }
    write_evidence(EVIDENCE, evidence)
    print(json.dumps(
        {"result": evidence["result"], "failed": failed},
        ensure_ascii=False))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
