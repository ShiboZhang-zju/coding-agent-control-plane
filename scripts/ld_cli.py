"""Minimal local CLI for the single-turn LD coordinator (M4-A, unverified).

The CLI shares the Runtime's SQLite ledger.  It never imports a Worker Adapter,
never dispatches directly, and cannot approve/deny work.  ``--decision-json``
injects structured data for local development; it does not load Python code or
shell commands and still passes through the strict decision validator.
"""
from __future__ import annotations

import argparse
import json
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
    return parser


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

        model = load_decision(args.decision_json)
        # A JSON decision is read locally and does not transmit project content.
        domains = ("local", "shareable") if model else ()
        coordinator = LDCoordinator(
            tools, model, model_authorized_security_domains=domains)
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
