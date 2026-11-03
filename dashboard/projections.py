"""Explicit, whitelisted JSON projections for the UI-0 dashboard.

Rules encoded here
------------------
1. **No domain logic.** Counts and states come from
   ``ProjectService.get_project_progress()`` / ``list_projects()`` /
   ``get_project_tasks()`` / ``list_project_sessions()`` and from
   ``LDGoalStore``.  This module only *selects and reshapes* fields.  If a
   number looks wrong, the bug is upstream — do not "fix" it here by
   recomputing state differently, that would create two truths.
2. **Whitelist, not blacklist.** Every field is named explicitly. A new ledger
   column therefore cannot leak by accident; someone has to add it here.
3. **Nothing derived that the ledger does not say.** In particular:
   * no project "completion percentage" (task counts are not project work);
   * no Worker liveness inference (a SessionReference is a binding, not a
     heartbeat — ``liveness`` is always ``"unknown"``);
   * no "last updated" timestamp invented from render time; ``null`` when the
     ledger has no timestamp to point at.
4. **No secrets, paths, prompts, bodies.** Project root paths, Effective
   Prompt, task prompt/original_task, context entry bodies, worker logs and
   arbitrary metadata are never included.

Timestamps are epoch seconds as stored by the ledger; the UI formats them.
"""
from __future__ import annotations

from typing import Any, Iterable

from adapters.base import TaskState, Verdict

from .ledger import LedgerSession, ProjectNotFound

#: Task states that occupy an execution slot.  Mirrors the constant used by
#: ``controlplane.project_service``; kept as a name here only for readability.
_ACTIVE_STATES = (TaskState.QUEUED.value, TaskState.STARTING.value,
                  TaskState.RUNNING.value, TaskState.WAITING_FOR_APPROVAL.value)

#: Goal statuses that mean "a human has to look at this".
_GOAL_HUMAN_STATUSES = ("needs_human",)

#: How much of a goal's own text the UI shows.  The goal is the Owner's own
#: sentence, so it is not a secret — but the dashboard is a summary surface,
#: not a document viewer, and an unbounded blob would break the layout.
GOAL_SUMMARY_CHARS = 220

#: Verification buckets reported by the memory summary.
_VERIFICATIONS = ("verified", "candidate", "unverified")

#: How many linked WorkItem ids one goal row may carry.  The count is always the
#: real count; only the id list is bounded, so a long-running goal cannot blow
#: up the JSON document.
LINKED_TASK_LIMIT = 50


def _goal_summary(text: str | None) -> dict:
    clean = (text or "").strip()
    if len(clean) <= GOAL_SUMMARY_CHARS:
        return {"summary": clean, "truncated": False}
    return {"summary": clean[:GOAL_SUMMARY_CHARS] + "…", "truncated": True}


def _max_ts(values: Iterable[Any]) -> float | None:
    """Largest numeric timestamp, or ``None`` when nothing is trustworthy.

    Strings, ``None`` and non-finite values are ignored instead of coerced:
    a display convenience must not fabricate a timestamp.
    """
    best: float | None = None
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        value = float(value)
        if best is None or value > best:
            best = value
    return best


# --------------------------------------------------------------------------- #
# goals
# --------------------------------------------------------------------------- #
def goal_view(goal: dict, *, project_id: str) -> dict:
    """One goal row, reduced to what a cockpit shows.

    ``last_decision`` is a free-form JSON blob written by the LD; only its
    ``stop``/``action``/``next_state`` keys are surfaced, and only as strings.
    """
    last = goal.get("last_decision") or {}
    if not isinstance(last, dict):
        last = {}
    stop_reason = last.get("stop") if isinstance(last.get("stop"), str) else None
    return {
        "goal_id": goal.get("goal_id"),
        "project_id": project_id,
        "status": goal.get("status"),
        **_goal_summary(goal.get("goal_text")),
        "goal_ref": goal.get("goal_ref"),
        "decision_count": int(goal.get("decision_count") or 0),
        "model_decision_count": int(goal.get("model_decision_count") or 0),
        "resume_count": int(goal.get("resume_count") or 0),
        "task_count": int(goal.get("task_count") or 0),
        "current_task_id": goal.get("current_task_id"),
        "handoff_task_ids": list(goal.get("handoff_task_ids") or []),
        "max_decisions_total": goal.get("max_decisions_total"),
        "max_tasks_total": goal.get("max_tasks_total"),
        "last_action": last.get("action") if isinstance(last.get("action"), str) else None,
        "last_next_state": (last.get("next_state")
                            if isinstance(last.get("next_state"), str) else None),
        "stop_reason": stop_reason,
        "created_at": goal.get("created_at"),
        "updated_at": goal.get("updated_at"),
        # A goal's lease is an internal scheduling fact, not something an
        # operator acts on in a read-only UI; only the fact of liveness is
        # useful ("is something advancing this right now?").
        "claim_until": goal.get("claim_until"),
        "claim_owner_present": bool(goal.get("claim_owner")),
    }


def goal_task_links(goal: dict, tasks: list[dict]) -> dict:
    """Which WorkItems the LD actually submitted for this goal, and their state.

    Membership is the ledger's own ``metadata.ld_goal_ref`` stamp written by
    ``LDTools.submit_project_task`` — never "any task that looks related".  If a
    goal has no linked task, that is reported as an empty list: it means the LD
    has not forwarded anything yet, which is different from "the task is
    running".

    This adds no progress semantics: the counts are ledger rows and the verdict
    values are copied verbatim, so the console can show the forwarding chain
    without ever claiming a goal is "50% done".
    """
    linked = [task for task in tasks if task.get("goal_ref")
              and task.get("goal_ref") == goal.get("goal_ref")]
    counts: dict[str, int] = {}
    for task in linked:
        key = str(task.get("verdict") or task.get("state") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return {
        "linked_task_ids": [task.get("task_id") for task in linked][:LINKED_TASK_LIMIT],
        "linked_task_count": len(linked),
        "linked_task_counts": counts,
        "linked_verified_count": sum(
            1 for task in linked if task.get("verdict") == "verified"),
    }


def goal_status_counts(goals: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for goal in goals:
        status = str(goal.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


# --------------------------------------------------------------------------- #
# work items
# --------------------------------------------------------------------------- #
def task_view(task: dict, *, project_id: str, row: dict | None = None) -> dict:
    """One WorkItem, reduced to execution facts a cockpit needs.

    ``task`` is the ``ProjectService.get_project_tasks()`` projection (the same
    one the CLI prints).  ``row`` is the raw ledger record for the *same*
    work item, used only for two facts that projection does not carry
    (``current_attempt_id``, ``updated_at``) and for the attention flags that
    ``ProjectService`` already computes.  Nothing is recomputed: the join
    exists so the dashboard does not have to widen a shared CLI projection.

    Deliberately absent: ``prompt``, ``original_task``, ``effective_prompt``,
    ``cwd``, ``metadata``, ``policy``, ``assertions`` payloads, worker logs.
    ``has_effective_prompt`` answers "was it frozen?" without shipping it.
    """
    from controlplane.project_service import _task_attention_flags  # local import

    raw = row or {}
    if raw.get("task_id") != task.get("task_id"):
        raw = {}
    # The LD stamps ``metadata.ld_goal_ref`` on every WorkItem it submits.  Only
    # that stamp links a task to a goal: nothing here searches titles or prompt
    # text for a resemblance.
    metadata = raw.get("metadata") or {}
    return {
        "task_id": task.get("task_id"),
        "project_id": project_id,
        "title": task.get("title"),
        "state": task.get("state"),
        "verdict": task.get("verdict"),
        "queue_status": task.get("queue_status"),
        "current_attempt_id": raw.get("current_attempt_id"),
        "workspace_id": task.get("workspace_id"),
        "context_version": task.get("context_version"),
        "has_effective_prompt": bool(task.get("has_effective_prompt")),
        # Goal membership as the ledger states it (None = not an LD submission).
        "goal_ref": metadata.get("ld_goal_ref"),
        "goal_id": metadata.get("ld_goal_id"),
        "submitted_by": metadata.get("submitted_by"),
        "attention_flags": _task_attention_flags(raw) if raw else [],
        "warnings": [str(w) for w in (raw.get("warnings") or [])][:20],
        "created_at": task.get("created_at"),
        "updated_at": raw.get("updated_at"),
    }


# --------------------------------------------------------------------------- #
# sessions
# --------------------------------------------------------------------------- #
def session_view(ref: dict) -> dict:
    """One SessionReference.

    ``liveness`` is hard-coded ``"unknown"``.  The ledger records *bindings*,
    written when a Snapshot was observed; it has no heartbeat.  Rendering that
    as "online" would be the exact mistake this task forbids.
    """
    return {
        "session_reference_id": ref.get("id"),
        "project_id": ref.get("project_id"),
        "work_item_id": ref.get("work_item_id"),
        "attempt_id": ref.get("attempt_id"),
        "workspace_id": ref.get("workspace_id"),
        "node_id": ref.get("node_id"),
        "worker_instance_id": ref.get("worker_instance_id"),
        "adapter": ref.get("adapter"),
        # The Worker's own session/job identity: enough to correlate in the
        # Worker's own UI, nothing that authenticates.
        "session_id": ref.get("session_id"),
        "binding_status": ref.get("binding_status"),
        "observed_at": ref.get("created_at"),
        "updated_at": ref.get("updated_at"),
        "liveness": "unknown",
        "liveness_note": ("ledger stores bindings, not heartbeats; "
                          "presence of a row does not mean the worker is alive"),
    }


# --------------------------------------------------------------------------- #
# memory
# --------------------------------------------------------------------------- #
def memory_summary(entries: list[dict], *, context_version: int) -> dict:
    """Counts + entry metadata.  Bodies stay in the ledger.

    The entry list exists so the Owner can see *what kinds of knowledge* exist
    (`verified` constraints vs `candidate` guesses); the body is withheld
    because a dashboard is not a knowledge editor (that is UI-1).
    """
    buckets = {name: 0 for name in _VERIFICATIONS}
    other = 0
    needs_merge = 0
    kinds: dict[str, int] = {}
    rows = []
    for entry in entries:
        verification = str(entry.get("verification") or "unknown")
        if verification in buckets:
            buckets[verification] += 1
        else:
            other += 1
        tags = entry.get("tags") or []
        if "needs_merge" in tags:
            needs_merge += 1
        kind = str(entry.get("kind") or "unknown")
        kinds[kind] = kinds.get(kind, 0) + 1
        rows.append({
            "entry_id": entry.get("id"),
            "kind": kind,
            "title": entry.get("title"),
            "verification": verification,
            "security_domain": entry.get("security_domain"),
            "version": entry.get("version"),
            "needs_merge": "needs_merge" in tags,
            "updated_at": entry.get("updated_at"),
        })
    return {
        "context_version": context_version,
        "total": len(entries),
        "verified": buckets["verified"],
        "candidate": buckets["candidate"],
        "unverified": buckets["unverified"],
        "other_verification": other,
        "needs_merge": needs_merge,
        "by_kind": kinds,
        "entries": rows,
    }


# --------------------------------------------------------------------------- #
# attention
# --------------------------------------------------------------------------- #
def attention_items(*, project_id: str, project_name: str,
                    progress: dict, goals: list[dict]) -> list[dict]:
    """Everything that needs a human, with its *original* status type kept.

    Sources, in the order they are appended:

    * goal rows in ``needs_human``;
    * ``ProjectService`` blocked work items (approval / dispatch_unknown /
      suspected_lost / session binding conflict / unknown);
    * rejected and inconclusive verdicts, which block nothing but still need a
      human answer.

    Each item carries ``kind`` (what sort of thing) and ``status`` (the raw
    ledger value).  They are never folded into a single "failed" bucket: an
    approval wait and a lost worker require different actions.
    """
    items: list[dict] = []

    for goal in goals:
        if goal.get("status") not in _GOAL_HUMAN_STATUSES:
            continue
        last = goal.get("last_decision") if isinstance(goal.get("last_decision"), dict) else {}
        items.append({
            "kind": "goal",
            "status": goal.get("status"),
            "project_id": project_id,
            "project_name": project_name,
            "object_id": goal.get("goal_id"),
            "title": _goal_summary(goal.get("goal_text"))["summary"],
            "detail": last.get("stop") if isinstance(last.get("stop"), str) else None,
            "flags": [],
            "updated_at": goal.get("updated_at"),
        })

    #: verdict values that a human still has to adjudicate.  A verified task
    #: never lands here; a pending one is still running, not waiting on us.
    for task in progress.get("blocked_tasks") or []:
        flags = list(task.get("attention_flags") or [])
        items.append({
            "kind": "work_item",
            "status": task.get("state"),
            "project_id": project_id,
            "project_name": project_name,
            "object_id": task.get("task_id"),
            "title": None,
            "detail": (task.get("queue_status")
                       if task.get("queue_status") in
                       ("needs_approval", "needs_human") else None),
            "flags": flags,
            "verdict": task.get("verdict"),
            "updated_at": None,
        })

    seen = {item["object_id"] for item in items if item["kind"] == "work_item"}
    for task in progress.get("recent_tasks") or []:
        verdict = task.get("verdict")
        if verdict not in (Verdict.REJECTED.value, Verdict.INCONCLUSIVE.value):
            continue
        if task.get("task_id") in seen:
            continue
        seen.add(task.get("task_id"))
        items.append({
            "kind": "work_item",
            "status": task.get("state"),
            "project_id": project_id,
            "project_name": project_name,
            "object_id": task.get("task_id"),
            "title": task.get("title"),
            "detail": None,
            "flags": [f"verdict:{verdict}"],
            "verdict": verdict,
            "updated_at": task.get("created_at"),
        })

    return items


# --------------------------------------------------------------------------- #
# project rows
# --------------------------------------------------------------------------- #
def project_row(session: LedgerSession, project: dict, *,
                progress: dict | None = None,
                goals: list[dict] | None = None) -> dict:
    """One project as shown in the left navigation and the overview table.

    ``progress``/``goals`` may be passed in when the caller already computed
    them (the overview does), so one HTTP request does not re-derive the same
    project twice.
    """
    project_id = project["project_id"]
    if progress is None:
        progress = session.service.get_project_progress(project_id)
    if goals is None:
        goals = session.goal_rows_for(project_id)
    counts = progress["counts"]

    # The most reliable "last thing that happened" is a real timestamp on a
    # task, a goal update, or a context entry update, whichever is newest.
    # Never a render timestamp: if the ledger has none, the UI shows "unknown".
    last_activity = _max_ts([
        *(t.get("created_at") for t in progress.get("recent_tasks") or []),
        *(g.get("updated_at") for g in goals),
        *(e.get("updated_at") for e in progress.get("narrative_progress") or []),
    ])
    attention = attention_items(project_id=project_id,
                               project_name=project["name"],
                               progress=progress, goals=goals)
    recent_tasks = progress.get("recent_tasks") or []
    return {
        "project_id": project_id,
        "name": project["name"],
        "active": bool(project.get("active")),
        "default_adapter": project.get("default_adapter"),
        "context_version": progress.get("context_version"),
        "task_count": counts["total"],
        "active_task_count": counts["active"],
        "task_counts": counts,
        "goal_count": len(goals),
        "goal_status_counts": goal_status_counts(goals),
        # "Which goal is this project working on?" must be answerable without
        # opening the project.  Preference order: an actively advancing goal,
        # then one waiting on a human, then the most recently updated one —
        # all of them real ledger rows, none inferred.
        "current_goal": _current_goal(goals, project_id),
        "recent_task": _recent_task(recent_tasks, project_id),
        "attention_count": len(attention),
        "has_blocker": bool(attention),
        "last_activity_at": last_activity,
        "last_activity_source": "ledger" if last_activity is not None else None,
    }


#: Goal statuses in "how much does this matter right now" order.
_GOAL_PRIORITY = ("active", "needs_human", "waiting", "failed", "completed")


def _current_goal(goals: list[dict], project_id: str) -> dict | None:
    """The goal a project is currently concerned with, or ``None``.

    No goal is invented when the project has none; the UI prints "无 Goal".
    """
    if not goals:
        return None
    ranked = sorted(
        goals,
        key=lambda goal: (
            _GOAL_PRIORITY.index(goal.get("status"))
            if goal.get("status") in _GOAL_PRIORITY else len(_GOAL_PRIORITY),
            -(goal.get("updated_at") or 0),
        ))
    return goal_view(ranked[0], project_id=project_id)


def _recent_task(tasks: list[dict], project_id: str) -> dict | None:
    """Most recent work item (the ledger orders them newest-first)."""
    if not tasks:
        return None
    task = tasks[0]
    return {
        "task_id": task.get("task_id"),
        "project_id": project_id,
        "title": task.get("title"),
        "state": task.get("state"),
        "verdict": task.get("verdict"),
        "queue_status": task.get("queue_status"),
        "created_at": task.get("created_at"),
    }


def overview(session: LedgerSession) -> dict:
    """Top strip + project table + global attention queue."""
    projects = session.service.list_projects()
    rows: list[dict] = []
    attention: list[dict] = []
    for project in projects:
        project_id = project["project_id"]
        progress = session.service.get_project_progress(project_id)
        goals = session.goal_rows_for(project_id)
        rows.append(project_row(session, project, progress=progress, goals=goals))
        attention.extend(attention_items(
            project_id=project_id, project_name=project["name"],
            progress=progress, goals=goals))
    all_goals = session.goal_rows()

    # Deterministic ordering: project, then kind, then object id.
    attention.sort(key=lambda item: (item["project_name"] or "",
                                     item["kind"], item["object_id"] or ""))

    active_goals = sum(1 for goal in all_goals if goal.get("status") == "active")
    return {
        "generated_at": _now(),
        "ledger": {"has_goals_table": session.has_goals_table(),
                   "projects": len(rows)},
        "totals": {
            "projects": len(rows),
            "active_projects": sum(1 for row in rows if row["active"]),
            "goals": len(all_goals),
            "active_goals": active_goals,
            "goals_needs_human": sum(1 for goal in all_goals
                                     if goal.get("status") == "needs_human"),
            "work_items": sum(row["task_count"] for row in rows),
            "active_work_items": sum(row["active_task_count"] for row in rows),
            "needs_human_work_items": sum(row["task_counts"]["needs_human"]
                                          for row in rows),
            "needs_approval_work_items": sum(row["task_counts"]["needs_approval"]
                                             for row in rows),
            "rejected": sum(row["task_counts"]["rejected"] for row in rows),
            "inconclusive": sum(row["task_counts"]["inconclusive"] for row in rows),
            "dispatch_unknown": sum(row["task_counts"]["dispatch_unknown"]
                                    for row in rows),
            "suspected_lost": sum(row["task_counts"]["suspected_lost"]
                                  for row in rows),
            "session_binding_conflict": sum(
                row["task_counts"]["session_binding_conflict"] for row in rows),
            "attention_items": len(attention),
        },
        "projects": rows,
        "attention": attention,
    }


def project_detail(session: LedgerSession, project_id: str) -> dict:
    """Everything the detail pane needs, in one round trip."""
    project = session.store.get_project(project_id)
    if not project:
        raise ProjectNotFound(f"no project {project_id}")
    progress = session.service.get_project_progress(project_id)
    raw_by_id = {task["task_id"]: task
                 for task in session.store.work_items_for_project(project_id)}
    tasks = [task_view(task, project_id=project_id,
                       row=raw_by_id.get(task["task_id"]))
             for task in session.service.get_project_tasks(project_id)]
    goal_rows = session.goal_rows_for(project_id)
    # ``linked_tasks`` is the console's forwarding trail: Goal -> the WorkItems
    # the LD itself submitted for it.  It is attached here (not recomputed in the
    # browser) so exactly one place reads ``metadata.ld_goal_ref``.
    goals = []
    for goal in goal_rows:
        view = goal_view(goal, project_id=project_id)
        view.update(goal_task_links(goal, tasks))
        goals.append(view)
    sessions = [session_view(ref) for ref in
                session.service.list_project_sessions(project_id)]
    entries = session.store.context_entries(project_id)
    attention = attention_items(project_id=project_id,
                               project_name=project["name"],
                               progress=progress, goals=session.goal_rows_for(project_id))

    last_activity = _max_ts([
        *(task.get("created_at") for task in tasks),
        *(task.get("updated_at") for task in tasks),
        *(goal.get("updated_at") for goal in goals),
        *(entry.get("updated_at") for entry in entries),
    ])
    return {
        "generated_at": _now(),
        "project": {
            "project_id": project_id,
            "name": project["name"],
            "active": bool(project.get("active")),
            "default_adapter": project.get("default_adapter"),
            "concurrency_limit": project.get("concurrency_limit"),
            "context_version": progress.get("context_version"),
            "created_at": project.get("created_at"),
            "updated_at": project.get("updated_at"),
            "last_activity_at": last_activity,
            # No root path: it is a local machine path and the dashboard
            # never needs it to answer an operator's question.
        },
        # Same fields as the shared progress projection, already whitelisted
        # upstream (task_id/state/verdict/queue_status/flags/warnings only).
        "blocked_tasks": [
            {"task_id": task.get("task_id"), "state": task.get("state"),
             "verdict": task.get("verdict"),
             "queue_status": task.get("queue_status"),
             "attention_flags": list(task.get("attention_flags") or []),
             "warnings": [str(w) for w in (task.get("warnings") or [])][:20]}
            for task in progress.get("blocked_tasks") or []
        ],
        "task_counts": progress["counts"],
        "goal_status_counts": goal_status_counts(goals),
        "goals": goals,
        "tasks": tasks,
        "sessions": sessions,
        "memory": memory_summary(entries,
                                 context_version=progress.get("context_version") or 0),
        "attention": attention,
        # Narrative progress entries are *shown as titles only*.  Their body is
        # human prose about blockers/next steps and may quote internal detail;
        # a cockpit does not need it, and the whitelist rule applies.
        "narrative_progress": [
            {"title": entry.get("title"),
             "verification": entry.get("verification"),
             "version": entry.get("version"),
             "updated_at": entry.get("updated_at")}
            for entry in progress.get("narrative_progress") or []
        ],
        "needs_merge_context": progress.get("needs_merge_context") or [],
        "derived_from": "ledger",
    }


def _now() -> float:
    import time
    return time.time()
