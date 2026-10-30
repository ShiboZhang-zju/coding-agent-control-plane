"""T32: deterministic M4-A LD boundaries (no Worker is started).

This test exercises the strict decision schema, project scope, dry-run behavior,
LD submission idempotency, Human Owner gates, and current-goal finish evidence.
It uses only a throw-away SQLite ledger and existing sandbox Git fixtures.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.project_context import ProjectContextStore  # noqa: E402
from controlplane.project_service import ProjectService  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from ld.coordinator import LDCoordinator  # noqa: E402
from ld.tools import LDSubmissionIdempotencyConflict, LDTools  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

LEDGER = ROOT / ".poc-state" / "t32-ld-m4a.db"
REPO_A = ROOT / "sandbox" / "repo-a-cli-smoke"
REPO_B = ROOT / "sandbox" / "repo-b-cli-smoke"
WORKSPACE_TWO = REPO_A / ".t32-workspace-two"


class AdapterRef:
    """Name-only Adapter: enqueue is tested, dispatch is never called."""

    name = "stub"


class CountingTools(LDTools):
    def __init__(self, service: ProjectService):
        super().__init__(service)
        self.provenance_calls = 0

    def get_task_provenance(self, task_id: str, **kwargs) -> dict:
        self.provenance_calls += 1
        return super().get_task_provenance(task_id, **kwargs)


def static(decision: dict[str, Any]):
    return lambda _decision_input: decision


def coordinator(tools: LDTools, decision: dict[str, Any] | None) -> LDCoordinator:
    return LDCoordinator(
        tools,
        static(decision) if decision is not None else None,
        model_authorized_security_domains=("local", "shareable"),
    )


def cli(*args: str, decision: dict[str, Any] | None = None) -> tuple[int, Any]:
    command = [
        sys.executable, str(ROOT / "scripts" / "ld_cli.py"),
        "--ledger", str(LEDGER), "--compact", *args,
    ]
    stdin = None
    if decision is not None:
        command.extend(["--decision-json", "-"])
        stdin = json.dumps(decision)
    proc = subprocess.run(
        command, input=stdin, text=True, capture_output=True, timeout=30,
        cwd=str(ROOT), encoding="utf-8", errors="replace")
    return proc.returncode, json.loads((proc.stdout or "null").strip())


def has_mapping_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(has_mapping_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(has_mapping_key(item, key) for item in value)
    return False


def inspect(task_id: str, *, project_id: str | None = None,
            tool: str = "get_task_provenance") -> dict[str, Any]:
    return {
        "action": "inspect",
        "project_id": project_id,
        "reason": "inspect deterministic task data",
        "evidence_refs": [],
        "parameters": {"tool": tool, "task_id": task_id},
    }


def submit(project_id: str, key: str, task: str = "implement goal") -> dict[str, Any]:
    return {
        "action": "submit_task",
        "project_id": project_id,
        "reason": "submit one ordinary project task",
        "evidence_refs": [],
        "parameters": {
            "task": task,
            "idempotency_key": key,
            "assertions": [{"kind": "file_exists", "target": "result.txt"}],
        },
    }


def finish(project_id: str, task_id: str) -> dict[str, Any]:
    return {
        "action": "finish",
        "project_id": project_id,
        "reason": "the current goal has verified task evidence",
        "evidence_refs": [f"task:{task_id}"],
        "parameters": {"verified_task_ids": [task_id]},
    }


def fresh() -> None:
    for stale in [LEDGER, *LEDGER.parent.glob(LEDGER.name + ".*")]:
        stale.unlink(missing_ok=True)
    for repo in [REPO_A, REPO_B]:
        if not (repo / ".git").exists():
            shutil.rmtree(repo, ignore_errors=True)
            repo.mkdir(parents=True)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True,
                           capture_output=True, timeout=30)
            subprocess.run(["git", "config", "user.email", "t32@example.invalid"],
                           cwd=repo, check=True, capture_output=True, timeout=30)
            subprocess.run(["git", "config", "user.name", "T32"], cwd=repo,
                           check=True, capture_output=True, timeout=30)
            (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
            subprocess.run(["git", "add", "seed.txt"], cwd=repo, check=True,
                           capture_output=True, timeout=30)
            subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo,
                           check=True, capture_output=True, timeout=30)
    shutil.rmtree(WORKSPACE_TWO, ignore_errors=True)
    WORKSPACE_TWO.mkdir(parents=True)


def main() -> int:
    fresh()
    store = TaskStore(LEDGER)
    try:
        plane = ControlPlane(AdapterRef(), store, require_registered_projects=True)
        service = ProjectService(plane)
        tools = CountingTools(service)
        project_a = plane.register_project(
            "t32-alpha", str(REPO_A), default_adapter="stub")
        project_b = plane.register_project(
            "t32-beta", str(REPO_B), default_adapter="stub")
        workspace_a = service.list_workspaces(project_a["id"])[0]
        workspace_two = service.register_workspace(
            project_a["id"], str(WORKSPACE_TWO), adapter="stub")

        task_b = service.submit_project_task(
            project_b["id"], "private project B task",
            idempotency_key="t32-project-b")

        # P1 scope: selected Project A is immutable even when the model omits it.
        cross = coordinator(tools, inspect(task_b["task_id"])).run_once(
            "inspect alpha only", project_id=project_a["id"])
        cross_provenance_calls = tools.provenance_calls
        task_a = tools.submit_project_task(
            project_a["id"], "inspectable alpha task",
            idempotency_key="t32-project-a",
            goal_ref=LDCoordinator._goal_ref("inspect alpha only"))
        same_scope = coordinator(tools, inspect(task_a["task_id"])).run_once(
            "inspect alpha only", project_id=project_a["id"])
        global_unscoped = coordinator(tools, inspect(task_a["task_id"])).run_once(
            "unscoped task lookup")
        read_decisions = [
            {
                "action": "inspect", "project_id": None,
                "reason": "list projects", "evidence_refs": [],
                "parameters": {"tool": "list_projects"},
            },
            *[
                {
                    "action": "inspect", "project_id": project_a["id"],
                    "reason": f"read {tool}", "evidence_refs": [],
                    "parameters": {"tool": tool},
                }
                for tool in ("get_project_brief", "get_project_progress",
                             "get_project_tasks")
            ],
            inspect(task_a["task_id"], project_id=project_a["id"],
                    tool="get_task_status"),
            inspect(task_a["task_id"], project_id=project_a["id"]),
        ]
        read_results = [
            coordinator(tools, decision).run_once(
                "exercise read whitelist", project_id=project_a["id"])
            for decision in read_decisions
        ]
        forbidden_tool = coordinator(tools, {
            "action": "inspect", "project_id": project_a["id"],
            "reason": "attempt approval", "evidence_refs": [],
            "parameters": {"tool": "approve"},
        }).run_once("forbidden tool", project_id=project_a["id"])

        # P1 idempotency: the signature ignores Context Version, but binds every
        # exposed execution-intent field including target workspace/assertions.
        idem_first = tools.submit_project_task(
            project_a["id"], "idempotent implementation",
            assertions=[{"kind": "file_exists", "target": "same.txt"}],
            workspace_id=workspace_a["id"], idempotency_key="t32-idem",
            goal_ref=LDCoordinator._goal_ref("idempotent goal"))
        ProjectContextStore(store).add_context_entry(project_a["id"], {
            "kind": "knowledge", "title": "context changed after submit",
            "body": "A retry must still return the frozen original WorkItem.",
            "verification": "candidate", "source": "test:t32",
        })
        idem_retry = tools.submit_project_task(
            project_a["id"], "idempotent implementation",
            assertions=[{"kind": "file_exists", "target": "same.txt"}],
            workspace_id=workspace_a["id"], idempotency_key="t32-idem",
            goal_ref=LDCoordinator._goal_ref("idempotent goal"))
        try:
            tools.submit_project_task(
                project_a["id"], "idempotent implementation",
                assertions=[{"kind": "file_exists", "target": "same.txt"}],
                workspace_id=workspace_two["id"], idempotency_key="t32-idem",
                goal_ref=LDCoordinator._goal_ref("idempotent goal"))
            workspace_conflict = False
        except LDSubmissionIdempotencyConflict:
            workspace_conflict = True
        try:
            tools.submit_project_task(
                project_a["id"], "idempotent implementation",
                assertions=[{"kind": "file_exists", "target": "different.txt"}],
                workspace_id=workspace_a["id"], idempotency_key="t32-idem",
                goal_ref=LDCoordinator._goal_ref("idempotent goal"))
            assertion_conflict = False
        except LDSubmissionIdempotencyConflict:
            assertion_conflict = True

        second_store = TaskStore(LEDGER)
        race_results: list[str] = []
        race_barrier = threading.Barrier(2)

        def race_submit(race_tools: LDTools, workspace: str, target: str) -> None:
            race_barrier.wait(timeout=10)
            try:
                race_tools.submit_project_task(
                    project_a["id"], "concurrent idempotent implementation",
                    assertions=[{"kind": "file_exists", "target": target}],
                    workspace_id=workspace, idempotency_key="t32-race",
                    goal_ref=LDCoordinator._goal_ref("concurrent goal"))
                race_results.append("success")
            except LDSubmissionIdempotencyConflict:
                race_results.append("conflict")
            except Exception as exc:  # captured as test failure, never hidden
                race_results.append(f"error:{type(exc).__name__}:{exc}")

        try:
            second_tools = LDTools(ProjectService(ControlPlane(
                AdapterRef(), second_store, require_registered_projects=True)))
            race_threads = [
                threading.Thread(
                    target=race_submit,
                    args=(tools, workspace_a["id"], "race-a.txt")),
                threading.Thread(
                    target=race_submit,
                    args=(second_tools, workspace_two["id"], "race-b.txt")),
            ]
            for thread in race_threads:
                thread.start()
            for thread in race_threads:
                thread.join(timeout=20)
        finally:
            second_store.close()

        # P2 read refinement: an unrelated attention flag may not suppress an
        # authorized inspection, while a new state-changing submit stays gated.
        attention_original = store.get(task_a["task_id"]) or {}
        store.update(
            task_a["task_id"], state="dispatch_unknown", queue_status="needs_approval",
            metadata={**(attention_original.get("metadata") or {}),
                      "suspected_lost": True},
            warnings=[*(attention_original.get("warnings") or []),
                      "SESSION_BINDING_CONFLICT:test"],
        )
        read_with_attention = coordinator(
            tools, inspect(task_a["task_id"], tool="get_task_status")).run_once(
                "inspect blocked task", project_id=project_a["id"])
        gated_submit = coordinator(
            tools, submit(project_a["id"], "t32-gated")).run_once(
                "submit while project needs attention", project_id=project_a["id"])
        store.update(
            task_a["task_id"], state=attention_original.get("state"),
            queue_status=attention_original.get("queue_status"),
            metadata=attention_original.get("metadata") or {},
            warnings=attention_original.get("warnings") or [],
        )

        # A shareable-only model view must not receive local context, roots or
        # frozen Effective Prompts.  The capture model still returns one valid
        # request_human decision and performs no external call.
        context = ProjectContextStore(store)
        context.add_context_entry(project_a["id"], {
            "kind": "background", "title": "LOCAL-SECRET-TITLE",
            "body": "LOCAL-SECRET-BODY", "verification": "candidate",
            "security_domain": "local", "source": "test:t32",
        })
        context.add_context_entry(project_a["id"], {
            "kind": "background", "title": "SHAREABLE-TITLE",
            "body": "SHAREABLE-BODY", "verification": "candidate",
            "security_domain": "shareable", "source": "test:t32",
        })
        captured_input: dict[str, Any] = {}

        def capture(decision_input):
            captured_input.update(decision_input)
            return {
                "action": "request_human", "project_id": project_a["id"],
                "reason": "security view captured", "evidence_refs": [],
                "parameters": {"question": "Continue?"},
            }

        security_result = LDCoordinator(
            tools, capture,
            model_authorized_security_domains=("shareable",),
            goal_security_domain="shareable",
        ).run_once("shareable goal", project_id=project_a["id"])
        captured_text = json.dumps(captured_input, ensure_ascii=False)
        effective_prompt_key_visible = has_mapping_key(
            captured_input, "effective_prompt")

        # P1 finish: counts alone are invalid.  A verified task must also carry
        # the stable fingerprint of the current Human Owner goal.
        project_count_only = {
            "action": "finish", "project_id": project_a["id"],
            "reason": "historical count is enough", 
            "evidence_refs": [f"project:{project_a['id']}:progress"],
            "parameters": {"completion_conditions": [{
                "kind": "project_count", "field": "verified",
                "operator": "gte", "value": 1,
            }]},
        }
        count_finish = coordinator(tools, project_count_only).run_once(
            "new goal", project_id=project_a["id"])

        goal = "deliver the current goal"
        submitted = coordinator(
            tools, submit(project_a["id"], "t32-current-goal")).run_once(
                goal, project_id=project_a["id"])
        goal_task_id = submitted["task_id"]
        store.update(goal_task_id, verdict="verified")
        same_goal_finish = coordinator(tools, finish(
            project_a["id"], goal_task_id)).run_once(goal, project_id=project_a["id"])
        other_goal_finish = coordinator(tools, finish(
            project_a["id"], goal_task_id)).run_once(
                "a different goal", project_id=project_a["id"])

        historical = service.submit_project_task(
            project_a["id"], "historical verified work",
            idempotency_key="t32-historical")
        store.update(historical["task_id"], verdict="verified")
        historical_finish = coordinator(tools, finish(
            project_a["id"], historical["task_id"])).run_once(
                "new unrelated goal", project_id=project_a["id"])

        # Remaining deterministic paths: dry-run, illegal control fields,
        # explicit request_human, wait, and no-model behavior.
        before_dry_run = len(service.get_project_tasks(project_a["id"]))
        dry_run = coordinator(
            tools, submit(project_a["id"], "t32-dry-run")).run_once(
                "preview only", project_id=project_a["id"], dry_run=True)
        after_dry_run = len(service.get_project_tasks(project_a["id"]))
        illegal = submit(project_a["id"], "t32-illegal")
        illegal["parameters"]["command"] = "rm -rf /"
        illegal_result = coordinator(tools, illegal).run_once(
            "reject arbitrary commands", project_id=project_a["id"])
        request_human = coordinator(tools, {
            "action": "request_human", "project_id": project_a["id"],
            "reason": "owner choice required", "evidence_refs": [],
            "parameters": {"question": "Choose the intended behavior."},
        }).run_once("ask owner", project_id=project_a["id"])
        wait_result = coordinator(tools, {
            "action": "wait", "project_id": project_a["id"],
            "reason": "task is still queued", "evidence_refs": [],
            "parameters": {"task_id": idem_first["task_id"]},
        }).run_once("idempotent goal", project_id=project_a["id"])
        no_model = coordinator(tools, None).run_once(
            "no model", project_id=project_a["id"])

        cli_list_code, cli_list = cli("list-projects")
        cli_brief_code, cli_brief = cli("brief", "t32-alpha")
        cli_preview_code, cli_preview = cli(
            "preview", "CLI preview", "--project", "t32-alpha",
            decision=submit(project_a["id"], "t32-cli-preview"))
        cli_run_code, cli_run = cli(
            "run-once", "CLI asks owner", "--project", "t32-alpha",
            decision={
                "action": "request_human", "project_id": project_a["id"],
                "reason": "CLI owner choice", "evidence_refs": [],
                "parameters": {"question": "Choose one."},
            })

        checks = {
            "cross_project_provenance_refused": (
                cross["next_state"] == "human_required"
                and cross_provenance_calls == 0
                and "effective_prompt" not in json.dumps(cross)),
            "selected_scope_fills_omitted_project": (
                same_scope["next_state"] == "inspection_complete"
                and same_scope["project_id"] == project_a["id"]),
            "global_task_read_requires_explicit_project": (
                global_unscoped["next_state"] == "invalid_model_decision"),
            "all_six_read_decisions_execute": all(
                result["next_state"] == "inspection_complete"
                for result in read_results),
            "forbidden_tool_rejected": (
                forbidden_tool["next_state"] == "invalid_model_decision"),
            "same_request_deduplicates_after_context_change": (
                idem_retry["task_id"] == idem_first["task_id"]
                and bool(idem_retry.get("deduplicated"))),
            "workspace_change_conflicts": workspace_conflict,
            "assertion_change_conflicts": assertion_conflict,
            "concurrent_signature_conflict_is_explicit": (
                sorted(race_results) == ["conflict", "success"]
                and store.by_idempotency_key("t32-race") is not None),
            "reads_allowed_with_project_attention": (
                read_with_attention["next_state"] == "inspection_complete"),
            "writes_still_gate_on_project_attention": (
                gated_submit["next_state"] == "human_required"
                and all(flag in gated_submit["blocking_reasons"] for flag in (
                    f"needs_approval:task:{task_a['task_id']}",
                    f"dispatch_unknown:task:{task_a['task_id']}",
                    f"suspected_lost:task:{task_a['task_id']}",
                    f"session_binding_conflict:task:{task_a['task_id']}",
                ))),
            "shareable_model_view_excludes_local_data": (
                security_result["next_state"] == "human_required"
                and "SHAREABLE-TITLE" in captured_text
                and "LOCAL-SECRET-TITLE" not in captured_text
                and "LOCAL-SECRET-BODY" not in captured_text
                and str(REPO_A) not in captured_text
                and "inspectable alpha task" not in captured_text
                and not effective_prompt_key_visible),
            "project_count_only_finish_rejected": (
                count_finish["next_state"] == "invalid_model_decision"),
            "same_goal_verified_task_finishes": (
                same_goal_finish["next_state"] == "finished"),
            "different_goal_cannot_reuse_verified_task": (
                other_goal_finish["next_state"] == "human_required"
                and "finish_evidence_not_linked_to_current_goal"
                in other_goal_finish["blocking_reasons"]),
            "historical_unlinked_task_cannot_finish": (
                historical_finish["next_state"] == "human_required"),
            "dry_run_does_not_enqueue": (
                dry_run["next_state"] == "planned"
                and before_dry_run == after_dry_run),
            "arbitrary_command_rejected": (
                illegal_result["next_state"] == "invalid_model_decision"),
            "request_human_action": request_human["next_state"] == "human_required",
            "wait_action": wait_result["next_state"] == "waiting",
            "no_model_is_explicit": no_model["next_state"] == "model_not_configured",
            "ld_cli_surfaces_work": (
                cli_list_code == 0 and len(cli_list) == 2
                and cli_brief_code == 0 and cli_brief["project_id"] == project_a["id"]
                and cli_preview_code == 0 and cli_preview["next_state"] == "planned"
                and cli_run_code == 3 and cli_run["next_state"] == "human_required"),
        }
        log = {
            "test": "T32-m4a-deterministic",
            "worker_started": False,
            "projects": {"alpha": project_a["id"], "beta": project_b["id"]},
            "scope": {
                "cross_result": cross["next_state"],
                "cross_provenance_calls": cross_provenance_calls,
                "same_scope_result": same_scope["next_state"],
                "global_unscoped_result": global_unscoped["next_state"],
                "read_whitelist_results": [item["next_state"] for item in read_results],
                "forbidden_tool": forbidden_tool["next_state"],
            },
            "idempotency": {
                "same_task": idem_retry["task_id"] == idem_first["task_id"],
                "deduplicated": bool(idem_retry.get("deduplicated")),
                "workspace_conflict": workspace_conflict,
                "assertion_conflict": assertion_conflict,
                "concurrent_results": sorted(race_results),
            },
            "finish": {
                "project_count_only": count_finish["next_state"],
                "same_goal": same_goal_finish["next_state"],
                "different_goal": other_goal_finish["next_state"],
                "historical_unlinked": historical_finish["next_state"],
            },
            "actions": {
                "inspect": read_with_attention["next_state"],
                "submit_task": submitted["next_state"],
                "wait": wait_result["next_state"],
                "request_human": request_human["next_state"],
                "finish": same_goal_finish["next_state"],
                "dry_run": dry_run["next_state"],
                "illegal": illegal_result["next_state"],
                "no_model": no_model["next_state"],
            },
            "security": {
                "result": security_result["next_state"],
                "shareable_title_visible": "SHAREABLE-TITLE" in captured_text,
                "local_title_visible": "LOCAL-SECRET-TITLE" in captured_text,
                "local_body_visible": "LOCAL-SECRET-BODY" in captured_text,
                "project_root_visible": str(REPO_A) in captured_text,
                "task_content_visible": "inspectable alpha task" in captured_text,
                "effective_prompt_key_visible": effective_prompt_key_visible,
            },
            "cli": {
                "list_projects_exit": cli_list_code,
                "brief_exit": cli_brief_code,
                "preview_exit": cli_preview_code,
                "run_once_exit": cli_run_code,
                "preview_state": cli_preview.get("next_state"),
                "run_once_state": cli_run.get("next_state"),
            },
            "checks": checks,
            "result": "Pass" if all(checks.values()) else "Fail",
        }
        write_evidence(ROOT / "evidence" / "project" / "T32-m4a-deterministic.json", log)
        print(json.dumps(log, ensure_ascii=False, indent=2))
        return 0 if log["result"] == "Pass" else 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
