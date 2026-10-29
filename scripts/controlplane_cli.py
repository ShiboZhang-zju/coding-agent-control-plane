"""Local CLI for the durable work queue and approval inbox.

This CLI intentionally talks to the SQLite ledger, not directly to a Worker.
It can enqueue/approve work while ``run_controlplane.py`` owns dispatch and
observation. A future HTTP API can preserve these command semantics.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import ArtifactAssertion, DispatchPolicy  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402


class AdapterRef:
    """Name-only reference: ledger commands never call the Worker."""

    def __init__(self, name: str):
        self.name = name


def emit(value, *, compact: bool = False) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=None if compact else 2,
                     default=str))


def policy_from_args(args, base: DispatchPolicy | None = None) -> DispatchPolicy:
    base = base or DispatchPolicy()
    execution_override = any(getattr(args, name, None) is not None for name in (
        "mode", "isolation", "permission_mode", "allow_unisolated_shell"))
    return DispatchPolicy(
        mode=args.mode if args.mode is not None else base.mode,
        isolation=(args.isolation if args.isolation is not None else base.isolation),
        permission_mode=(args.permission_mode if args.permission_mode is not None
                         else base.permission_mode),
        allow_unisolated_shell=(
            args.allow_unisolated_shell
            if args.allow_unisolated_shell is not None
            else base.allow_unisolated_shell),
        # Changing how a task executes must re-derive risk unless the caller
        # explicitly chose a risk. Do not inherit a stored "low" from an
        # agent/worktree project and accidentally apply it to an unisolated shell.
        risk=(args.risk if args.risk is not None
              else "auto" if execution_override else base.risk),
    )


def has_policy_override(args) -> bool:
    return any(getattr(args, name, None) is not None for name in (
        "mode", "isolation", "permission_mode", "allow_unisolated_shell", "risk"))


def assertions_from_args(args) -> list[ArtifactAssertion]:
    assertions = [ArtifactAssertion("file_exists", path)
                  for path in (args.assert_file or [])]
    for raw in args.assert_contains or []:
        if ":" not in raw:
            raise ValueError("--assert-contains must be PATH:TEXT")
        path, value = raw.split(":", 1)
        assertions.append(ArtifactAssertion("file_contains", path, value=value))
    for raw in args.assert_absent or []:
        assertions.append(ArtifactAssertion("path_absent", raw))
    return assertions


def item_view(item: dict) -> dict:
    return {
        "task_id": item.get("task_id"),
        "title": item.get("title"),
        "adapter": item.get("adapter") or (item.get("metadata") or {}).get("adapter"),
        "project_id": item.get("project_id"),
        "priority": item.get("priority") or 0,
        "queue_status": item.get("queue_status"),
        "state": item.get("state"),
        "verdict": item.get("verdict"),
        "worker_job_id": item.get("worker_job_id"),
        "cwd": item.get("cwd"),
        "warnings": item.get("warnings") or [],
        "updated_at": item.get("updated_at"),
    }


def project_view(project: dict) -> dict:
    return {
        "id": project.get("id"),
        "name": project.get("name"),
        "root": project.get("root"),
        "default_adapter": project.get("default_adapter"),
        "policy": project.get("policy") or {},
        "concurrency_limit": project.get("concurrency_limit"),
        "active": bool(project.get("active")),
        "updated_at": project.get("updated_at"),
    }


def approval_by_id(store: TaskStore, approval_id: int) -> dict:
    rows = [row for row in store.all_approvals() if row.get("id") == approval_id]
    if not rows:
        raise KeyError(f"no approval {approval_id}")
    return rows[0]


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="controlplane",
                                 description="durable local Coding Agent work queue")
    ap.add_argument("--ledger", type=Path, default=None)
    ap.add_argument("--compact", action="store_true")
    sub = ap.add_subparsers(dest="command", required=True)

    enqueue = sub.add_parser("enqueue", help="persist work for Runtime dispatch")
    enqueue.add_argument("prompt")
    enqueue.add_argument("--cwd", required=True)
    enqueue.add_argument("--title", default="")
    enqueue.add_argument("--project", default=None,
                         help="registered project id or name (auto-detected from cwd if omitted)")
    enqueue.add_argument("--adapter", default=None,
                         help="override the project's default Adapter")
    enqueue.add_argument("--allow-unregistered", action="store_true",
                         help="unsafe compatibility mode: allow cwd outside the registry")
    enqueue.add_argument("--idempotency-key", default="")
    enqueue.add_argument("--priority", type=int, default=0)
    enqueue.add_argument("--timeout", type=int, default=900)
    enqueue.add_argument("--mode", choices=["agent", "shell"], default=None)
    enqueue.add_argument("--isolation", choices=["worktree", "none"], default=None)
    enqueue.add_argument("--permission-mode", default=None)
    enqueue.add_argument("--risk", choices=["auto", "low", "medium", "high"],
                         default=None)
    enqueue.add_argument("--allow-unisolated-shell", action="store_true", default=None)
    enqueue.add_argument("--assert-file", action="append", default=[])
    enqueue.add_argument("--assert-contains", action="append", default=[])
    enqueue.add_argument("--assert-absent", action="append", default=[])

    project = sub.add_parser("project", help="register and enable/disable Git projects")
    project_sub = project.add_subparsers(dest="project_command", required=True)
    project_add = project_sub.add_parser("add")
    project_add.add_argument("name")
    project_add.add_argument("root")
    project_add.add_argument("--adapter", default="codebuddy")
    project_add.add_argument("--concurrency", type=int, default=2)
    project_add.add_argument("--mode", choices=["agent", "shell"], default="agent")
    project_add.add_argument("--isolation", choices=["worktree", "none"],
                             default="worktree")
    project_add.add_argument("--permission-mode", default="acceptEdits")
    project_add.add_argument("--risk", choices=["auto", "low", "medium", "high"],
                             default="auto")
    project_add.add_argument("--allow-unisolated-shell", action="store_true")
    project_sub.add_parser("list")
    project_show = project_sub.add_parser("show")
    project_show.add_argument("project")
    project_enable = project_sub.add_parser("enable")
    project_enable.add_argument("project")
    project_disable = project_sub.add_parser("disable")
    project_disable.add_argument("project")

    sub.add_parser("list", help="list work items")
    show = sub.add_parser("show", help="show one work item with attempts/events")
    show.add_argument("task_id")
    sub.add_parser("summary", help="show the Foreman summary")
    events = sub.add_parser("events", help="show append-only events for one item")
    events.add_argument("task_id")
    events.add_argument("--limit", type=int, default=100)

    approval = sub.add_parser("approval", help="manage the approval inbox")
    approval_sub = approval.add_subparsers(dest="approval_command", required=True)
    approval_sub.add_parser("list")
    approve = approval_sub.add_parser("approve")
    approve.add_argument("approval_id", type=int)
    approve.add_argument("--note", default="")
    deny = approval_sub.add_parser("deny")
    deny.add_argument("approval_id", type=int)
    deny.add_argument("--note", default="")
    return ap


def main() -> int:
    args = build_parser().parse_args()
    store = TaskStore(args.ledger)
    try:
        if args.command == "project":
            plane = ControlPlane(AdapterRef("ledger-only"), store)
            if args.project_command == "add":
                project = plane.register_project(
                    args.name, args.root, default_adapter=args.adapter,
                    policy=policy_from_args(args),
                    concurrency_limit=args.concurrency)
                emit(project_view(project), compact=args.compact)
            elif args.project_command == "list":
                emit([project_view(project) for project in store.all_projects()],
                     compact=args.compact)
            else:
                project = plane.project(args.project)
                if args.project_command == "show":
                    result = project
                else:
                    result = store.set_project_active(
                        project["id"], args.project_command == "enable")
                emit(project_view(result), compact=args.compact)
            return 0

        if args.command == "enqueue":
            cwd = Path(args.cwd).expanduser().resolve()
            if not cwd.is_dir():
                raise ValueError(f"cwd is not a directory: {cwd}")
            required = not args.allow_unregistered
            resolver = ControlPlane(
                AdapterRef(args.adapter or "project-default"), store,
                require_registered_projects=required)
            cwd_text, project = resolver.resolve_project_for_cwd(
                str(cwd), args.project, require_registered=required)
            adapter_name = args.adapter or (project or {}).get("default_adapter") or "codebuddy"
            base_policy = (DispatchPolicy(**project["policy"])
                           if project and project.get("policy") else DispatchPolicy())
            policy = policy_from_args(args, base_policy) if has_policy_override(args) else None
            plane = ControlPlane(
                AdapterRef(adapter_name), store,
                require_registered_projects=required)
            rec = plane.enqueue(
                args.prompt, cwd_text, title=args.title,
                idempotency_key=args.idempotency_key, priority=args.priority,
                timeout_s=args.timeout, assertions=assertions_from_args(args),
                policy=policy, project_id=(project or {}).get("id"))
            emit({**item_view(rec),
                  "task_digest": rec.get("task_digest"),
                  "approval_required": rec.get("approval_required", False)},
                 compact=args.compact)
            return 0

        # Read/approval commands only need a name so summary remains generic.
        plane = ControlPlane(AdapterRef("ledger-only"), store)
        if args.command == "list":
            emit([item_view(item) for item in store.all()], compact=args.compact)
        elif args.command == "show":
            item = store.get(args.task_id)
            if not item:
                raise KeyError(f"no work item {args.task_id}")
            emit({"item": item_view(item), "prompt": item.get("prompt"),
                  "policy": item.get("policy"), "assertions": item.get("assertions"),
                  "attempts": store.attempts(args.task_id),
                  "events": store.events(args.task_id)}, compact=args.compact)
        elif args.command == "summary":
            emit(plane.summary(), compact=args.compact)
        elif args.command == "events":
            emit(store.events(args.task_id, limit=args.limit), compact=args.compact)
        elif args.command == "approval":
            if args.approval_command == "list":
                emit(store.all_approvals(), compact=args.compact)
            else:
                row = approval_by_id(store, args.approval_id)
                digest = row.get("digest") or ""
                if args.approval_command == "approve":
                    result = plane.approve(digest, note=args.note,
                                           approval_id=args.approval_id)
                else:
                    result = plane.deny(digest, note=args.note,
                                        approval_id=args.approval_id)
                emit(result, compact=args.compact)
        return 0
    except (KeyError, ValueError) as exc:
        emit({"error": f"{type(exc).__name__}: {exc}"}, compact=args.compact)
        return 2
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
