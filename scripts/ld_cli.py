"""Minimal local CLI for the LD coordinator (M4-A single turn, M4-B goals).

The CLI shares the Runtime's SQLite ledger.  It never imports a Worker Adapter,
never dispatches directly, and cannot approve/deny work.  ``--decision-json``
injects structured data for local development; it does not load Python code or
shell commands and still passes through the strict decision validator.

A real model can be attached with ``--model-base-url/--model-name/--model-api-key``
or the ``LD_MODEL_BASE_URL`` / ``LD_MODEL_NAME`` / ``LD_MODEL_API_KEY``
environment variables.  Without either, the coordinator reports
``model_not_configured`` and executes nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.project_service import ProjectService  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from ld.coordinator import LDCoordinator  # noqa: E402
from ld.model import OpenAIChatModel  # noqa: E402
from ld.tools import LDTools  # noqa: E402


class AdapterRef:
    """Name-only Adapter reference; this process never calls a Worker."""

    def __init__(self, name: str):
        self.name = name


class StaticDecisionModel:
    """Return one local JSON decision through the normal model boundary."""

    def __init__(self, decision: Mapping[str, Any]):
        self._decision = dict(decision)

    def decide(self, _decision_input: Mapping[str, Any]) -> Mapping[str, Any]:
        return dict(self._decision)


def emit(value: Any, *, compact: bool = False) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=None if compact else 2,
                     default=str))


def load_decision(path: str | None) -> StaticDecisionModel | None:
    if path is None:
        return None
    raw_text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    raw = json.loads(raw_text)
    if not isinstance(raw, Mapping):
        raise ValueError("decision JSON must contain one object")
    return StaticDecisionModel(raw)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ld",
        description="minimal single-turn LD over the existing control-plane ledger")
    parser.add_argument("--ledger", type=Path, default=None,
                        help="SQLite ledger path (default: .poc-state/ledger.db)")
    parser.add_argument("--compact", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list-projects", help="list registered projects")
    brief = sub.add_parser("brief", help="show one deterministic project brief")
    brief.add_argument("project", help="project id or name")

    for name, help_text in (
        ("preview", "validate and display one LD decision without executing it"),
        ("run-once", "validate and execute one allowed LD action"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("goal")
        command.add_argument("--project", default=None, help="project id or name")
        command.add_argument(
            "--decision-json", default=None, metavar="PATH",
            help=("local structured decision file; use '-' for stdin. If omitted, "
                  "the coordinator reports model_not_configured"))
        _add_model_args(command)

    goal_cmd = sub.add_parser(
        "run-goal", help="advance one persistent goal with bounded decisions")
    goal_cmd.add_argument("goal")
    goal_cmd.add_argument("--project", required=True, help="project id or name")
    goal_cmd.add_argument("--max-decisions", type=int, default=10)
    goal_cmd.add_argument("--max-tasks", type=int, default=3)
    goal_cmd.add_argument(
        "--poll-interval-s", type=float, default=None,
        help="poll the in-flight task and keep advancing until it settles")
    goal_cmd.add_argument("--max-wait-s", type=float, default=1800.0)
    _add_model_args(goal_cmd)
    return parser


def _add_model_args(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--model-base-url", default=None,
        help="OpenAI-compatible endpoint (or LD_MODEL_BASE_URL)")
    command.add_argument(
        "--model-name", default=None,
        help="model id at the endpoint (or LD_MODEL_NAME)")
    command.add_argument(
        "--model-api-key", default=None,
        help="endpoint API key; never logged (or LD_MODEL_API_KEY)")
    command.add_argument(
        "--model-timeout-s", type=float, default=None,
        help="model call deadline in seconds (or LD_MODEL_TIMEOUT_S)")
    command.add_argument(
        "--model-domains", default=None,
        help="comma-separated security domains the model may see "
             "(default: local,shareable when a real model is configured)")


def main() -> int:
    args = build_parser().parse_args()
    store = TaskStore(args.ledger)
    try:
        read_plane = ControlPlane(AdapterRef("ledger-only"), store)
        read_service = ProjectService(read_plane)

        def submission_service(project_id: str) -> ProjectService:
            project = read_plane.project(project_id)
            adapter_name = project.get("default_adapter")
            if not adapter_name:
                raise ValueError(f"project {project_id} has no default_adapter")
            plane = ControlPlane(
                AdapterRef(adapter_name), store, require_registered_projects=True)
            return ProjectService(plane)

        tools = LDTools(
            read_service, submission_service_factory=submission_service)
        if args.command == "list-projects":
            emit(tools.list_projects(), compact=args.compact)
            return 0
        if args.command == "brief":
            project = read_plane.project(args.project)
            emit(tools.get_project_brief(project["id"]), compact=args.compact)
            return 0

        model = load_decision(getattr(args, "decision_json", None))
        if model is None:
            base_url = args.model_base_url or os.environ.get("LD_MODEL_BASE_URL", "")
            model_name = args.model_name or os.environ.get("LD_MODEL_NAME", "")
            if base_url.strip() and model_name.strip():
                model = OpenAIChatModel(
                    base_url=base_url,
                    model=model_name,
                    api_key=args.model_api_key or os.environ.get("LD_MODEL_API_KEY"),
                    timeout_s=(args.model_timeout_s
                               or float(os.environ.get("LD_MODEL_TIMEOUT_S", "60"))))
        # A JSON decision is local and a real endpoint is explicit configuration;
        # both authorize the same bounded view of project context.
        domains = tuple(
            item.strip() for item in (args.model_domains or "local,shareable").split(",")
            if item.strip()) if model else ()
        coordinator = LDCoordinator(
            tools, model, model_authorized_security_domains=domains)

        if args.command == "run-goal":
            if model is None:
                emit({"error": "run-goal requires a real decision model; "
                               "configure --model-base-url/--model-name or "
                               "LD_MODEL_BASE_URL/LD_MODEL_NAME"},
                     compact=args.compact)
                return 3
            from ld.goal_store import LDGoalStore
            goal_store = LDGoalStore(store.path)
            try:
                report = coordinator.run_goal(
                    args.goal, args.project, goal_store,
                    max_decisions=args.max_decisions,
                    max_tasks=args.max_tasks,
                    poll_interval_s=args.poll_interval_s,
                    max_wait_s=args.max_wait_s)
            finally:
                goal_store.close()
            emit(report, compact=args.compact)
            return (0 if report.get("status") == "completed" else
                    3 if report.get("status") in ("waiting", "needs_human") else 1)

        result = coordinator.run_once(
            args.goal,
            project_id=args.project,
            dry_run=args.command == "preview",
        )
        emit(result, compact=args.compact)
        return (3 if result.get("next_state") in {
            "model_not_configured", "invalid_model_decision", "human_required",
        } else 0)
    except (OSError, json.JSONDecodeError, KeyError, RuntimeError, ValueError) as exc:
        emit({"error": f"{type(exc).__name__}: {exc}"}, compact=args.compact)
        return 2
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
