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
from controlplane.context_builder import ContextBuildBlocked  # noqa: E402
from controlplane.handoff import HandoffNotReady  # noqa: E402
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


_PROJECT_CONTEXT_COMMANDS = (
    "brief", "progress", "tasks", "context", "workspaces", "sessions",
    "context-add", "context-update", "context-verify", "context-sync-task",
    "context-update-log", "submit", "handoff-prepare", "handoff-submit")


def _run_project_command(args, plane: ControlPlane, store: TaskStore) -> int:
    """Project-context subcommands: read-side projections plus two writes.

    Everything goes through :class:`ProjectService`, so the CLI and any future LD
    share one deterministic implementation instead of two drifting copies.
    """
    from controlplane.project_context import ContextEntry
    from controlplane.project_service import ProjectService

    project = plane.project(args.project)
    # Ledger-only CLI commands never call the Worker, but queued work must still
    # be routed to the project's real Adapter name for the Runtime to claim it.
    service_plane = ControlPlane(
        AdapterRef(project.get("default_adapter") or "codebuddy"), store)
    service = ProjectService(service_plane)
    cmd = args.project_command
    if cmd in ("handoff-prepare", "handoff-submit"):
        source = store.get(args.source_task_id)
        if not source:
            raise KeyError(f"no task {args.source_task_id}")
        if source.get("project_id") != project["id"]:
            raise ValueError(
                f"source task {args.source_task_id} belongs to project "
                f"{source.get('project_id')}, not {project['id']}")

    if cmd == "brief":
        emit(service.get_project_brief(project["id"]), compact=args.compact)
    elif cmd == "progress":
        emit(service.get_project_progress(project["id"]), compact=args.compact)
    elif cmd == "tasks":
        emit(service.get_project_tasks(project["id"]), compact=args.compact)
    elif cmd == "context":
        emit(service.get_project_context(project["id"]), compact=args.compact)
    elif cmd == "workspaces":
        emit(service.list_workspaces(project["id"]), compact=args.compact)
    elif cmd == "sessions":
        emit(service.list_project_sessions(project["id"]), compact=args.compact)
    elif cmd == "context-add":
        entry = ContextEntry(
            project_id=project["id"], kind=args.kind, title=args.title,
            body=args.body, tags=args.tag, verification=args.verification,
            security_domain=args.security_domain, source=args.source,
            evidence_refs=args.evidence)
        emit(service.add_context_entry(project["id"], entry), compact=args.compact)
    elif cmd == "context-update":
        changes: dict = {}
        if args.title is not None:
            changes["title"] = args.title
        if args.body is not None:
            changes["body"] = args.body
        if args.tag is not None:
            changes["tags"] = args.tag
        if args.verification is not None:
            changes["verification"] = args.verification
        if args.security_domain is not None:
            changes["security_domain"] = args.security_domain
        if not changes:
            raise ValueError("context-update needs at least one change")
        emit(service.update_context_entry(
            project["id"], args.entry_id, args.expected_version, changes,
            changed_by=args.changed_by, note=args.note), compact=args.compact)
    elif cmd == "context-verify":
        from controlplane.project_context import ProjectContextStore

        emit(ProjectContextStore(store).verify_entry(
            project["id"], args.entry_id, args.expected_version,
            evidence_refs=args.evidence, changed_by=args.changed_by),
            compact=args.compact)
    elif cmd in ("context-sync-task", "context-update-log"):
        task = store.get(args.task_id)
        if not task:
            raise KeyError(f"no task {args.task_id}")
        if task.get("project_id") != project["id"]:
            raise ValueError(
                f"task {args.task_id} belongs to project {task.get('project_id')}, "
                f"not {project['id']}")
        result = (service.update_context_from_task(args.task_id)
                  if cmd == "context-sync-task"
                  else service.context_update_receipts(args.task_id))
        emit(result, compact=args.compact)
    elif cmd == "submit":
        build_kwargs = dict(
            tags=args.tag, prerequisites=args.prerequisite,
            workspace_id=args.workspace,
            require_verified_constraints=args.require_verified_constraints)
        if args.preview:
            built = service.build_task_context(project["id"], args.task, **build_kwargs)
            emit({"effective_prompt": built.effective_prompt,
                  "context_version": built.context_version,
                  "context_entry_refs": built.entry_refs,
                  "gaps": built.gaps, "warnings": built.warnings,
                  "blocked": built.blocked}, compact=args.compact)
        else:
            rec = service.submit_project_task(
                project["id"], args.task,
                title=args.title, idempotency_key=args.idempotency_key,
                priority=args.priority, timeout_s=args.timeout,
                assertions=assertions_from_args(args),
                allow_blocked=args.allow_blocked, **build_kwargs)
            emit({**item_view(rec),
                  "deduplicated": rec.get("deduplicated", False),
                  "context_version": rec.get("context_version"),
                  "context_entry_refs": rec.get("context_entry_refs"),
                  "original_task": rec.get("original_task"),
                  "effective_prompt": rec.get("effective_prompt"),
                  "workspace_id": rec.get("workspace_id"),
                  "context": rec.get("context")}, compact=args.compact)
    elif cmd == "handoff-prepare":
        emit(service.prepare_handoff(
            args.source_task_id, args.task,
            target_workspace_id=args.workspace,
            tags=args.tag, prerequisites=args.prerequisite,
            require_verified_constraints=args.require_verified_constraints,
            require_checkpoint=not args.allow_context_only),
            compact=args.compact)
    elif cmd == "handoff-submit":
        rec = service.submit_handoff(
            args.source_task_id, args.task,
            title=args.title, idempotency_key=args.idempotency_key,
            priority=args.priority, timeout_s=args.timeout,
            assertions=assertions_from_args(args),
            target_workspace_id=args.workspace,
            tags=args.tag, prerequisites=args.prerequisite,
            require_verified_constraints=args.require_verified_constraints,
            require_checkpoint=not args.allow_context_only,
            expected_handoff_signature=args.expected_signature)
        emit({**item_view(rec),
              "deduplicated": rec.get("deduplicated", False),
              "workspace_id": rec.get("workspace_id"),
              "context_version": rec.get("context_version"),
              "context_entry_refs": rec.get("context_entry_refs"),
              "handoff_context": rec.get("handoff_context")},
             compact=args.compact)
    return 0


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

    # -- project-centric surface (M1) ---------------------------------- #
    for name, help_text in (
            ("brief", "compact project brief (context + derived task counts)"),
            ("progress", "progress derived from the ledger"),
            ("tasks", "work items of one project"),
            ("context", "list project context entries"),
            ("workspaces", "list workspaces of one project"),
            ("sessions", "list recorded session references")):
        p = project_sub.add_parser(name, help=help_text)
        p.add_argument("project")

    context_add = project_sub.add_parser(
        "context-add", help="add one project context entry")
    context_add.add_argument("project")
    context_add.add_argument("--kind", required=True,
                             choices=["background", "architecture", "constraints",
                                      "progress", "knowledge", "decisions"])
    context_add.add_argument("--title", required=True)
    context_add.add_argument("--body", required=True)
    context_add.add_argument("--tag", action="append", default=[])
    context_add.add_argument("--verification", default="unverified",
                             choices=["unverified", "candidate", "verified"])
    context_add.add_argument("--security-domain", default="local",
                             choices=["local", "shareable"])
    context_add.add_argument("--source", default="human")
    context_add.add_argument("--evidence", action="append", default=[])

    context_update = project_sub.add_parser(
        "context-update", help="CAS update of one entry (fails on stale version)")
    context_update.add_argument("project")
    context_update.add_argument("entry_id")
    context_update.add_argument("--expected-version", type=int, required=True)
    context_update.add_argument("--title", default=None)
    context_update.add_argument("--body", default=None)
    context_update.add_argument("--tag", action="append", default=None)
    context_update.add_argument("--verification", default=None,
                                choices=["unverified", "candidate", "verified"])
    context_update.add_argument("--security-domain", default=None,
                                choices=["local", "shareable"])
    context_update.add_argument("--note", default="")
    context_update.add_argument("--changed-by", default="human")

    context_verify = project_sub.add_parser(
        "context-verify", help="promote an entry to verified (evidence required)")
    context_verify.add_argument("project")
    context_verify.add_argument("entry_id")
    context_verify.add_argument("--expected-version", type=int, required=True)
    context_verify.add_argument("--evidence", action="append", required=True)
    context_verify.add_argument("--changed-by", default="human")

    context_sync = project_sub.add_parser(
        "context-sync-task",
        help="replay the idempotent M2 context update for one terminal task")
    context_sync.add_argument("project")
    context_sync.add_argument("task_id")

    context_log = project_sub.add_parser(
        "context-update-log", help="show M2 idempotency/merge receipts for one task")
    context_log.add_argument("project")
    context_log.add_argument("task_id")

    submit = project_sub.add_parser(
        "submit", help="build project context, freeze the prompt, then enqueue")
    submit.add_argument("project")
    submit.add_argument("task")
    submit.add_argument("--title", default="")
    submit.add_argument("--idempotency-key", default="")
    submit.add_argument("--priority", type=int, default=0)
    submit.add_argument("--timeout", type=int, default=900)
    submit.add_argument("--tag", action="append", default=[])
    submit.add_argument("--prerequisite", action="append", default=[])
    submit.add_argument("--workspace", default=None)
    submit.add_argument("--require-verified-constraints", action="store_true")
    submit.add_argument("--allow-blocked", action="store_true",
                        help="enqueue even when a prerequisite is missing")
    submit.add_argument("--preview", action="store_true",
                        help="print the effective prompt without enqueueing")
    submit.add_argument("--assert-file", action="append", default=[])
    submit.add_argument("--assert-contains", action="append", default=[])
    submit.add_argument("--assert-absent", action="append", default=[])

    handoff_prepare = project_sub.add_parser(
        "handoff-prepare", help="read-only readiness check for a new Session")
    handoff_prepare.add_argument("project")
    handoff_prepare.add_argument("source_task_id")
    handoff_prepare.add_argument("task")
    handoff_prepare.add_argument("--workspace", default=None)
    handoff_prepare.add_argument("--tag", action="append", default=[])
    handoff_prepare.add_argument("--prerequisite", action="append", default=[])
    handoff_prepare.add_argument("--require-verified-constraints", action="store_true")
    handoff_prepare.add_argument(
        "--allow-context-only", action="store_true",
        help="explicitly allow a handoff with no recorded Git checkpoint")

    handoff_submit = project_sub.add_parser(
        "handoff-submit", help="enqueue a prepared handoff as a normal WorkItem")
    handoff_submit.add_argument("project")
    handoff_submit.add_argument("source_task_id")
    handoff_submit.add_argument("task")
    handoff_submit.add_argument("--workspace", default=None)
    handoff_submit.add_argument("--tag", action="append", default=[])
    handoff_submit.add_argument("--prerequisite", action="append", default=[])
    handoff_submit.add_argument("--require-verified-constraints", action="store_true")
    handoff_submit.add_argument("--allow-context-only", action="store_true")
    handoff_submit.add_argument(
        "--expected-signature", default=None,
        help="prepared handoff signature; submit fails if context/checkpoint drifted")
    handoff_submit.add_argument("--title", default="")
    handoff_submit.add_argument("--idempotency-key", default="")
    handoff_submit.add_argument("--priority", type=int, default=0)
    handoff_submit.add_argument("--timeout", type=int, default=900)
    handoff_submit.add_argument("--assert-file", action="append", default=[])
    handoff_submit.add_argument("--assert-contains", action="append", default=[])
    handoff_submit.add_argument("--assert-absent", action="append", default=[])

    provenance = sub.add_parser(
        "provenance", help="original task, effective prompt and context refs")
    provenance.add_argument("task_id")

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

    recover = sub.add_parser(
        "recover-ledger",
        help="lift a persistent degraded state (manual, audited)")
    recover.add_argument("--note", required=True,
                         help="why the degraded state may be lifted (audited)")
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
                return 0
            if args.project_command == "list":
                emit([project_view(project) for project in store.all_projects()],
                     compact=args.compact)
                return 0
            if args.project_command in _PROJECT_CONTEXT_COMMANDS:
                return _run_project_command(args, plane, store)
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
        elif args.command == "provenance":
            from controlplane.project_service import ProjectService

            emit(ProjectService(plane).get_task_provenance(args.task_id),
                 compact=args.compact)
        elif args.command == "recover-ledger":
            # Manual, audited recovery of a persistent degraded ledger. The
            # operator must have reviewed the *.corrupt-* quarantine copy first.
            emit(store.recover_degraded(args.note), compact=args.compact)
        return 0
    except HandoffNotReady as exc:
        emit({"error": f"{type(exc).__name__}: {exc}",
              "blockers": exc.blockers, "prepared": exc.prepared},
             compact=args.compact)
        return 3
    except (KeyError, ValueError) as exc:
        emit({"error": f"{type(exc).__name__}: {exc}"}, compact=args.compact)
        return 2
    except ContextBuildBlocked as exc:
        # A blocked build is not a crash: report the gaps so the caller can fix
        # the project context instead of guessing what was missing.
        emit({"error": f"{type(exc).__name__}: {exc}", "gaps": exc.gaps},
             compact=args.compact)
        return 3
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
