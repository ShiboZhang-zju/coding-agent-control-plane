"""Minimal local CLI for the LD coordinator (M4-A turns, M4-B goals, M4-D chat).

The CLI shares the Runtime's SQLite ledger.  It never imports a Worker Adapter,
never dispatches directly, and cannot approve/deny work.  ``--decision-json``
injects structured data for local development; it does not load Python code or
shell commands and still passes through the strict decision validator.

A real model can be attached with ``--model-base-url/--model-name/--model-api-key``
or the ``LD_MODEL_BASE_URL`` / ``LD_MODEL_NAME`` / ``LD_MODEL_API_KEY``
environment variables.  Without either, the coordinator reports
``model_not_configured`` and executes nothing.

``ld chat`` is the conversational entry point (M4-D).  It is a routing layer
over the same deterministic tools: it can read status, submit a bounded goal and
stage a resume for explicit human confirmation — it can never approve work,
write project knowledge or dispatch a Worker.  With no model endpoint
configured it falls back to a deterministic keyword router, and every answer is
rendered from Ledger / Goal / Context rows.
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
from ld.chat import LDChat  # noqa: E402
from ld.chat_store import LDChatStore  # noqa: E402
from ld.coordinator import LDCoordinator  # noqa: E402
from ld.goal_store import LDGoalStore  # noqa: E402
from ld.model import OpenAIChatIntentModel, OpenAIChatModel  # noqa: E402
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
    goal_cmd.add_argument(
        "--resume", action="store_true",
        help="after human review, lift a needs_human goal back to active")
    _add_model_args(goal_cmd)

    chat_cmd = sub.add_parser(
        "chat", help="conversational project-management entry point (M4-D)")
    chat_cmd.add_argument(
        "message", nargs="*",
        help="one message; omit to start an interactive session")
    chat_cmd.add_argument("--conversation", default=None,
                          help="continue a specific conversation id")
    chat_cmd.add_argument("--new", action="store_true",
                          help="start a new conversation")
    chat_cmd.add_argument(
        "--continue-latest", action="store_true", dest="continue_latest",
        help=("continue the most recently updated conversation (single-operator "
              "convenience). Without it, a one-shot command refuses to guess; "
              "an interactive session always starts fresh until you name one"))
    chat_cmd.add_argument("--project", default=None, help="project id or name")
    chat_cmd.add_argument(
        "--no-model", action="store_true",
        help="skip the intent classifier and use the deterministic keyword router")
    _add_model_args(chat_cmd)

    conv = sub.add_parser("conversations", help="list stored conversations")
    conv.add_argument("--limit", type=int, default=20)
    show_conv = sub.add_parser("conversation", help="print one conversation")
    show_conv.add_argument("conversation")
    show_conv.add_argument("--limit", type=int, default=200)
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
        help="comma-separated security domains a real model endpoint may see "
             "(or LD_MODEL_DOMAINS). Default for an endpoint: none — the model "
             "is not called for a 'local' goal until 'local' is granted")


def _chat_model(args) -> OpenAIChatIntentModel | None:
    """The intent classifier endpoint, or None to use the keyword router."""
    if getattr(args, "no_model", False):
        return None
    base_url = (args.model_base_url or os.environ.get("LD_MODEL_BASE_URL", "")).strip()
    model_name = (args.model_name or os.environ.get("LD_MODEL_NAME", "")).strip()
    if not base_url or not model_name:
        return None
    return OpenAIChatIntentModel(
        base_url=base_url, model=model_name,
        api_key=args.model_api_key or os.environ.get("LD_MODEL_API_KEY"),
        timeout_s=(args.model_timeout_s
                   or float(os.environ.get("LD_MODEL_TIMEOUT_S", "60"))))


def _chat_append_message(emit_message, message: str, conversation_id: str | None,
                         project_id: str | None) -> tuple[str | None, dict]:
    """Append one turn to an open conversation; returns (conversation, turn)."""
    turn = emit_message(message, conversation_id=conversation_id,
                        project_id=project_id)
    return turn.get("conversation_id"), turn


def _run_chat(args, tools: LDTools, store: TaskStore) -> int:
    """One-shot or interactive chat over the existing deterministic core.

    Nothing here is a new execution path: every answer is rendered from rows
    the control plane already wrote, and the two state-changing intents
    (``submit_goal`` / ``resume_goal``) reuse the existing bounded goal
    machinery.  ``resume_goal`` still needs an explicit follow-up confirmation.
    """
    model = _chat_model(args)
    if model is not None:
        domain_spec = (args.model_domains
                       or os.environ.get("LD_MODEL_DOMAINS", "")).strip()
    else:
        domain_spec = "local,shareable"
    domains = tuple(item.strip() for item in domain_spec.split(",") if item.strip())

    goal_store = LDGoalStore(store.path)
    chats = LDChatStore(store.path)
    try:
        chat = LDChat(tools, goal_store, chats, model,
                      model_authorized_security_domains=domains)

        def send(message: str, conversation_id: str | None,
                 project_id: str | None) -> dict:
            return chat.handle(message, conversation_id=conversation_id,
                               project_id=project_id)

        conversation_id = args.conversation
        interactive = not args.message and sys.stdin.isatty()
        if conversation_id is None and not args.new and not interactive:
            # Continuing the most recent conversation is a *convenience for one
            # operator at one terminal*.  It is off unless asked for: with two
            # terminals (or two people) open, silently adopting whatever was
            # touched last would apply one context's staged action or project
            # selection to another.  An interactive session is already sticky,
            # so it just starts fresh when nothing was named.
            if not getattr(args, "continue_latest", False):
                raise SystemExit(
                    "ld chat: refusing to guess which conversation to continue.\n"
                    "  --new                start a new conversation\n"
                    "  --conversation ID    continue that conversation\n"
                    "  --continue-latest    continue the most recently updated one\n"
                    "Run `ld conversations` to list them.")
            recent = chats.list_conversations(limit=1)
            conversation_id = recent[0]["conversation_id"] if recent else None
        project_id = args.project
        if args.message:
            _cid, turn = _chat_append_message(
                send, " ".join(args.message), conversation_id, project_id)
            emit(turn, compact=args.compact)
            return 0

        if not sys.stdin.isatty():
            # Piped input: one message per line keeps scripting possible.
            for line in sys.stdin:
                if not line.strip():
                    continue
                conversation_id, turn = _chat_append_message(
                    send, line.strip(), conversation_id, project_id)
                emit(turn, compact=args.compact)
            return 0

        print("LD chat. Type a message, or 'exit' to leave. "
              "Conversation memory is separate from project memory; every answer "
              "is read from the ledger.")
        while True:
            try:
                line = input("ld> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not line:
                continue
            if line.lower() in ("exit", "quit", ":q"):
                return 0
            conversation_id, turn = _chat_append_message(
                send, line, conversation_id, project_id)
            print(turn.get("answer") or "")
            if turn.get("requires_confirmation"):
                target = (turn.get("pending_action") or {}).get("goal_id")
                print(f"[staged action on {target}; reply 'confirm' or 'cancel']")
            print()
    finally:
        chats.close()
        goal_store.close()


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
        if args.command in ("conversations", "conversation"):
            chats = LDChatStore(store.path)
            try:
                if args.command == "conversations":
                    emit(chats.list_conversations(limit=args.limit),
                         compact=args.compact)
                else:
                    conversation = chats.get_conversation(args.conversation)
                    if conversation is None:
                        emit({"error": f"no conversation {args.conversation}"},
                             compact=args.compact)
                        return 2
                    emit({"conversation": conversation,
                          "messages": chats.messages(args.conversation,
                                                     limit=args.limit)},
                         compact=args.compact)
            finally:
                chats.close()
            return 0
        if args.command == "chat":
            return _run_chat(args, tools, store)

        decision_file = getattr(args, "decision_json", None)
        model = load_decision(decision_file)
        endpoint_model = False
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
                endpoint_model = True
        # Security domains are an explicit grant.  A real endpoint receives
        # project context, so it gets NO domain unless the operator names one
        # (--model-domains or LD_MODEL_DOMAINS); without the goal's domain the
        # coordinator refuses to call it.  A --decision-json file is a static
        # local decision: nothing is sent anywhere, so it keeps the bounded
        # local view it always had.
        if endpoint_model:
            domain_spec = args.model_domains or os.environ.get("LD_MODEL_DOMAINS", "")
        elif model is not None:
            domain_spec = args.model_domains or "local,shareable"
        else:
            domain_spec = ""
        domains = tuple(item.strip() for item in domain_spec.split(",") if item.strip())
        coordinator = LDCoordinator(
            tools, model, model_authorized_security_domains=domains)

        if args.command == "run-goal":
            if model is None:
                emit({"error": "run-goal requires a real decision model; "
                               "configure --model-base-url/--model-name or "
                               "LD_MODEL_BASE_URL/LD_MODEL_NAME"},
                     compact=args.compact)
                return 3
            goal_store = LDGoalStore(store.path)
            try:
                report = coordinator.run_goal(
                    args.goal, args.project, goal_store,
                    max_decisions=args.max_decisions,
                    max_tasks=args.max_tasks,
                    poll_interval_s=args.poll_interval_s,
                    max_wait_s=args.max_wait_s,
                    resume=args.resume)
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
