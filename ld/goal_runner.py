"""Deterministic goal scheduler that wakes the LD when there is work to do (M4-C).

This module is **not** another agent.  It holds no model, makes no decision and
never talks to a Worker.  Its whole job is to answer one question, cheaply and
deterministically:

    "For which goals has the world changed since the LD last looked at it?"

``LDCoordinator.run_goal`` still performs every decision, every submission and
every goal transition; this runner only decides *when* to call it.  That split
is what keeps a single global LD: the runner can poll every few seconds, while
the model is called only on a real state change.

Why a scheduler is needed at all
--------------------------------
Before M4-C the only thing that advanced a goal was a human running
``ld run-goal`` again after each Worker finished.  The runtime could dispatch
and reconcile, but nothing re-woke the LD, so "one Human Owner goal" still
needed repeated manual invocations.  The runner closes that loop:

    goal waiting + WorkItem running     -> no model call, keep waiting
    goal waiting + verdict ready        -> wake the LD once
    goal active after a query decision  -> wake the LD again (continuation),
                                           bounded by the no-progress streak
    goal needs_human / completed / failed -> never woken automatically

Budgets (M4-C reliability closure)
----------------------------------
Each wake-up grants the LD a small per-call *quantum* (``max_decisions`` /
``max_tasks``, default 1/1).  The *lifetime* bound — the maximum cumulative
model decisions and WorkItem submissions for the whole goal — is persisted on
the goal row itself (``ld_goals.max_decisions_total`` / ``max_tasks_total``),
stamped once at the first advancement and never enlarged by scheduling,
restarts or a human ``resume``.  Exhausting a quantum just returns control;
exhausting a lifetime parks the goal in ``needs_human``.

Not a second source of truth
----------------------------
Task state and verdicts are read from the Ledger on every poll
(``get_task_status``); goal state is read from ``LDGoalStore``.  The runner's
own table only stores *scheduling* bookkeeping — which observed state has
already been acted on, how many consecutive failures there were and when the
next attempt is allowed.  It is deliberately not a copy of any verdict, and
losing it can at most cause one extra (idempotent) decision: every goal step is
keyed by ``(goal_id, step)`` and reconciled from the Ledger.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import threading
import time
from typing import Any

from ld.goal_store import GoalLeaseHeld, GoalLeaseLost
#: Goal statuses the runner is allowed to consider at all.  ``needs_human``,
#: ``completed`` and ``failed`` are terminal for automation: only a Human Owner
#: (or an explicit ``--resume`` run) may move them, so the runner never calls the
#: model for them — that is the whole point of those states.
ADVANCEABLE_STATUSES = ("active", "waiting")

#: Task states that still occupy an execution slot.  While the current WorkItem
#: is in one of these, waking the LD would only burn tokens to be told "wait".
_IN_FLIGHT_TASK_STATES = frozenset({
    "queued", "starting", "running", "waiting_for_approval", "dispatch_unknown",
})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ld_goal_runner (
    goal_id TEXT PRIMARY KEY,
    last_signature TEXT,
    last_observed_at REAL,
    last_advanced_at REAL,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    consecutive_no_progress INTEGER NOT NULL DEFAULT 0,
    runs INTEGER NOT NULL DEFAULT 0,
    advances INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    last_outcome TEXT,
    updated_at REAL NOT NULL
);
"""


class GoalRunnerStore:
    """Scheduling bookkeeping only (one row per goal) in the shared ledger file.

    Nothing written here is authoritative about a task: it records what the
    runner has already observed and when it may try again.  Deleting a row can
    cause at most one redundant, idempotent LD decision.
    """

    def __init__(self, ledger_path):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(ledger_path), timeout=15,
                                     check_same_thread=False,
                                     isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=15000")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextlib.contextmanager
    def _txn(self):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def get(self, goal_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM ld_goal_runner WHERE goal_id=?", (goal_id,)).fetchone()
        return dict(row) if row else None

    def all(self) -> dict[str, dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM ld_goal_runner").fetchall()
        return {row["goal_id"]: dict(row) for row in rows}

    def record_success(self, goal_id: str, *, signature: str, outcome: str,
                       advanced: bool, no_progress: bool,
                       acknowledge: bool = True) -> dict:
        """Record a successful advance.

        ``acknowledge=False`` deliberately keeps the previous
        ``last_signature``: it marks "the LD's flow continues — the current
        observation is *not* fully handled yet" (e.g. an inspection whose
        follow-up decision is still pending).  The next sweep therefore still
        sees an unacknowledged state and wakes the model again, bounded by the
        runner's no-progress escalation.  Losing the row can cause at most one
        extra (idempotent) decision.
        """
        now = time.time()
        with self._txn() as conn:
            row = conn.execute(
                "SELECT consecutive_no_progress, last_signature "
                "FROM ld_goal_runner WHERE goal_id=?",
                (goal_id,)).fetchone()
            streak = ((int(row["consecutive_no_progress"]) if row else 0) + 1
                      if no_progress else 0)
            stored = (signature if acknowledge
                      else (row["last_signature"] if row else None))
            conn.execute(
                "INSERT INTO ld_goal_runner (goal_id, last_signature,"
                " last_observed_at, last_advanced_at, next_attempt_at,"
                " consecutive_failures, consecutive_no_progress, runs, advances,"
                " last_error, last_outcome, updated_at)"
                " VALUES (?,?,?,?,0,0,?,1,?,NULL,?,?)"
                " ON CONFLICT(goal_id) DO UPDATE SET"
                " last_signature=excluded.last_signature,"
                " last_observed_at=excluded.last_observed_at,"
                " last_advanced_at=COALESCE(excluded.last_advanced_at,"
                "   ld_goal_runner.last_advanced_at),"
                " next_attempt_at=0, consecutive_failures=0,"
                " consecutive_no_progress=excluded.consecutive_no_progress,"
                " runs=ld_goal_runner.runs+1,"
                " advances=ld_goal_runner.advances+excluded.advances,"
                " last_error=NULL, last_outcome=excluded.last_outcome,"
                " updated_at=excluded.updated_at",
                (goal_id, stored, now, now if advanced else None,
                 streak, 1 if advanced else 0, outcome, now))
        return self.get(goal_id) or {}

    def record_skip(self, goal_id: str, *, signature: str, outcome: str,
                    acknowledge: bool = True) -> dict:
        """Record a non-advancing observation.

        ``acknowledge=False`` is for skips where the observed state was *not*
        handled by this executor — another holder is (or was) advancing it
        (lease held/lost) or has already moved the goal on (stale failure).
        Writing the signature there would make a still-unhandled failure look
        like "no observable state change" and silently disable the retry
        policy; whoever actually handles the state acknowledges it.
        """
        now = time.time()
        with self._txn() as conn:
            if acknowledge:
                stored = signature
            else:
                row = conn.execute(
                    "SELECT last_signature FROM ld_goal_runner WHERE goal_id=?",
                    (goal_id,)).fetchone()
                stored = row["last_signature"] if row else None
            conn.execute(
                "INSERT INTO ld_goal_runner (goal_id, last_signature,"
                " last_observed_at, next_attempt_at, runs, last_outcome, updated_at)"
                " VALUES (?,?,?,0,0,?,?)"
                " ON CONFLICT(goal_id) DO UPDATE SET"
                " last_signature=excluded.last_signature,"
                " last_observed_at=excluded.last_observed_at,"
                " last_outcome=excluded.last_outcome, updated_at=excluded.updated_at",
                (goal_id, stored, now, outcome, now))
        return self.get(goal_id) or {}

    def record_failure(self, goal_id: str, *, signature: str, error: str,
                       next_attempt_at: float) -> dict:
        """Record a failure WITHOUT acknowledging the observed state.

        ``last_signature`` deliberately stays at the last *successfully advanced*
        observation: a failed attempt handled nothing, so the next sweep must
        still consider the same state due.  Writing the signature here would make
        every retry look like "no observable state change" and silently disable
        the retry policy.  ``last_observed_at`` is still bumped so an operator
        can see liveness.
        """
        now = time.time()
        with self._txn() as conn:
            conn.execute(
                "INSERT INTO ld_goal_runner (goal_id, last_signature,"
                " last_observed_at, next_attempt_at, consecutive_failures,"
                " last_error, last_outcome, updated_at)"
                " VALUES (?,NULL,?,?,1,?, 'failed', ?)"
                " ON CONFLICT(goal_id) DO UPDATE SET"
                " last_observed_at=excluded.last_observed_at,"
                " next_attempt_at=excluded.next_attempt_at,"
                " consecutive_failures=ld_goal_runner.consecutive_failures+1,"
                " last_error=excluded.last_error, last_outcome='failed',"
                " updated_at=excluded.updated_at",
                (goal_id, now, next_attempt_at, error, now))
        return self.get(goal_id) or {}

    def reset(self, goal_id: str) -> None:
        with self._txn() as conn:
            conn.execute("DELETE FROM ld_goal_runner WHERE goal_id=?", (goal_id,))

    def clear_backoff(self, goal_id: str) -> None:
        """Skip only the wait window; keeps failure/progress streaks intact."""
        with self._txn() as conn:
            conn.execute("UPDATE ld_goal_runner SET next_attempt_at=0 "
                         "WHERE goal_id=?", (goal_id,))


def goal_wake_signature(goal: dict, current_task: dict | None,
                        project_counts: dict | None) -> str:
    """Fingerprint of "the facts the LD would decide on".

    Model-decision counters are deliberately excluded: recording a step must
    not, by itself, look like a reason to call the model again.  What is
    included is exactly what can make the next decision different — the goal's
    own task bookkeeping, the current WorkItem's observable state/verdict, and
    the project's derived verdict counts.
    """
    payload = {
        "status": goal.get("status"),
        "task_count": goal.get("task_count"),
        "current_task_id": goal.get("current_task_id"),
        "handoff_task_ids": sorted(goal.get("handoff_task_ids") or []),
        "current_task": None if current_task is None else {
            key: current_task.get(key) for key in (
                "task_id", "state", "verdict", "queue_status",
                "context_updates_completed", "attention_flags")},
        "counts": None if project_counts is None else {
            key: project_counts.get(key) for key in (
                "total", "active", "verified", "rejected", "inconclusive",
                "needs_approval", "needs_human")},
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:32]


class LDGoalRunner:
    """Poll goals and wake the LD exactly when a decision could differ.

    One instance is a scheduler, not a worker: it never blocks on a Worker, it
    never loops on a single goal, and every call is bounded by ``limit``.  The
    embedded ``LDCoordinator.run_goal`` still owns leases, fencing, two-phase
    steps and reconciliation.
    """

    def __init__(self, coordinator, goal_store, *, runner_store: GoalRunnerStore | None = None,
                 max_decisions: int = 1, max_tasks: int = 1,
                 max_model_failures: int = 3,
                 max_no_progress_advances: int = 3,
                 base_backoff_s: float = 5.0, max_backoff_s: float = 300.0,
                 lifetime_max_decisions: int | None = None,
                 lifetime_max_tasks: int | None = None,
                 owner: str | None = None,
                 wall_clock=time.time):
        if max_decisions < 1 or max_tasks < 1:
            raise ValueError("max_decisions and max_tasks must be >= 1")
        if max_model_failures < 1:
            raise ValueError("max_model_failures must be >= 1")
        self._coordinator = coordinator
        self._goals = goal_store
        if runner_store is None:
            path = getattr(goal_store, "path", None)
            if not path:
                raise ValueError(
                    "goal_store must expose its ledger path to share bookkeeping")
            runner_store = GoalRunnerStore(path)
        self._runner = runner_store
        self._max_decisions = max_decisions
        self._max_tasks = max_tasks
        self._max_model_failures = max_model_failures
        self._max_no_progress = max_no_progress_advances
        self._base_backoff_s = base_backoff_s
        self._max_backoff_s = max_backoff_s
        # Lifetime budgets stamped on the goal at its first advancement (see
        # ``LDGoalStore.ensure_lifetime_budget``): repeated wake-ups, restarts
        # and human resumes never enlarge them.  ``None`` falls back to the
        # store defaults.
        self._lifetime_max_decisions = lifetime_max_decisions
        self._lifetime_max_tasks = lifetime_max_tasks
        self._owner = owner
        self._clock = wall_clock

    def close(self) -> None:
        try:
            self._runner.close()
        except Exception:  # noqa: BLE001 - closing must not surface
            pass

    # ------------------------------------------------------------------ #
    # observation
    # ------------------------------------------------------------------ #
    def observe(self, goal: dict) -> dict:
        """Read the facts a decision would use; no model, no writes."""
        goal_id = goal["goal_id"]
        current = None
        current_id = goal.get("current_task_id")
        if current_id:
            try:
                status = self._coordinator._tools.get_task_status(current_id)
                current = {key: status.get(key) for key in (
                    "task_id", "state", "verdict", "queue_status",
                    "attention_flags")}
                receipts = self._coordinator._tools.service.context_update_receipts(
                    current_id)
                current["context_updates_completed"] = bool(receipts)
            except KeyError:
                current = {"task_id": current_id, "state": "missing",
                           "verdict": None, "queue_status": None,
                           "attention_flags": [], "context_updates_completed": False}
        counts = None
        project_id = goal.get("project_id")
        if project_id:
            try:
                counts = self._coordinator._tools.get_project_progress(
                    project_id)["counts"]
            except KeyError:
                counts = None
        return {
            "goal_id": goal_id,
            "status": goal.get("status"),
            "current_task": current,
            "counts": counts,
            "signature": goal_wake_signature(goal, current, counts),
        }

    def plan(self, goal: dict, observed: dict) -> dict[str, Any]:
        """Decide whether this goal needs the LD right now (deterministic)."""
        status = goal.get("status")
        if status not in ADVANCEABLE_STATUSES:
            return {"goal_id": goal["goal_id"], "action": "skip",
                    "reason": f"goal status is {status}"}
        if self._goals.pending_step(goal["goal_id"]) is not None:
            # A step was persisted before its side effect: reconcile it from the
            # Ledger (the coordinator does that without asking the model).
            return {"goal_id": goal["goal_id"], "action": "advance",
                    "pending_step": True,
                    "reason": "pending step needs reconciliation"}
        current = observed["current_task"]
        if current is not None:
            flags = [flag for flag in current.get("attention_flags") or []
                     if flag in ("needs_approval", "dispatch_unknown",
                                 "suspected_lost", "session_binding_conflict")]
            if flags:
                # Existing gate: let the coordinator turn this into an explicit
                # human request instead of inventing a retry here.
                return {"goal_id": goal["goal_id"], "action": "advance",
                        "reason": "current task needs human attention: "
                                  + ",".join(sorted(flags))}
            state = current.get("state")
            if state in _IN_FLIGHT_TASK_STATES:
                return {"goal_id": goal["goal_id"], "action": "wait",
                        "reason": f"current task is {state}"}
            if current.get("verdict") in ("pending", None):
                # Terminal worker state without a verdict is an inconsistency the
                # LD must see (never assumed to be a pass).
                return {"goal_id": goal["goal_id"], "action": "advance",
                        "reason": "current task has no terminal verdict"}
        return {"goal_id": goal["goal_id"], "action": "advance",
                "reason": "state changed"}

    # ------------------------------------------------------------------ #
    # advancement
    # ------------------------------------------------------------------ #
    def advance_due(self, *, limit: int | None = None,
                    now: float | None = None) -> dict[str, Any]:
        """One bounded sweep over all goals; never raises for a single goal."""
        now = self._clock() if now is None else now
        bookkeeping = self._runner.all()
        results: list[dict] = []
        acknowledged_only: list[dict] = []
        for goal in self._goals.all_goals():
            goal_id = goal["goal_id"]
            if goal.get("status") not in ADVANCEABLE_STATUSES:
                # Terminal for automation: report it so an operator can see the
                # scheduler is not silently ignoring a parked goal.
                results.append({"goal_id": goal_id, "action": "skip",
                                "reason": f"goal status is {goal.get('status')}"})
                continue
            state = bookkeeping.get(goal_id) or {}
            if float(state.get("next_attempt_at") or 0) > now:
                results.append({"goal_id": goal["goal_id"], "action": "backoff",
                                "reason": "backoff after a failure",
                                "retry_in_s": round(
                                    float(state["next_attempt_at"]) - now, 1),
                                "consecutive_failures":
                                    state.get("consecutive_failures", 0)})
                continue
            try:
                observed = self.observe(goal)
            except Exception as exc:  # noqa: BLE001 - one goal must not stop the sweep
                self._runner.record_failure(
                    goal_id, signature=str(goal.get("last_decision") or ""),
                    error=f"{type(exc).__name__}: {exc}",
                    next_attempt_at=now + self._backoff(
                        int(state.get("consecutive_failures") or 0) + 1))
                results.append({"goal_id": goal_id, "action": "error",
                                "reason": f"{type(exc).__name__}: {exc}"})
                continue
            plan = self.plan(goal, observed)
            if plan["action"] == "skip":
                self._runner.record_skip(goal_id,
                                         signature=observed["signature"],
                                         outcome=plan["reason"])
                results.append({**plan, "signature": observed["signature"]})
                continue
            if plan["action"] == "wait":
                self._runner.record_skip(goal_id,
                                         signature=observed["signature"],
                                         outcome=plan["reason"])
                results.append({**plan, "signature": observed["signature"]})
                continue
            if (plan.get("pending_step") is not True
                    and state.get("last_signature") == observed["signature"]):
                # Same observed facts as the last complete advance: waking the
                # model again cannot produce a different decision.  A pending
                # step is exempt: it is unfinished work, and the signature does
                # not include it (a crash must not look like "nothing changed").
                self._runner.record_skip(goal_id,
                                         signature=observed["signature"],
                                         outcome="no observable state change")
                acknowledged_only.append({
                    "goal_id": goal_id, "action": "skip",
                    "reason": "no observable state change since the last advance",
                    "signature": observed["signature"]})
                continue
            results.append(self._advance_one(goal, observed, state, now,
                                             plan_reason=plan["reason"]))
            if limit is not None and len(results) >= limit:
                break
        return {
            "ok": all(item.get("action") != "error" for item in results),
            "checked": len(results) + len(acknowledged_only),
            "advanced": [item for item in results
                         if item.get("action") == "advanced"],
            "waiting": [item for item in results if item.get("action") == "wait"],
            "skipped": [item for item in results if item.get("action") == "skip"]
                       + acknowledged_only,
            "failures": [item for item in results if item.get("action") == "error"],
            "items": results + acknowledged_only,
        }

    def _advance_one(self, goal: dict, observed: dict, state: dict,
                     now: float, *, plan_reason: str = "") -> dict[str, Any]:
        goal_id = goal["goal_id"]
        try:
            report = self._coordinator.run_goal(
                goal["goal_text"], goal["project_id"], self._goals,
                max_decisions=self._max_decisions,
                max_tasks=self._max_tasks,
                poll_interval_s=None, max_wait_s=None,
                lease_owner=self._owner,
                # Each wake-up grants a per-call quantum; the *overall* bound
                # is the goal's persisted lifetime budget, stamped once and
                # never enlarged by scheduling, restarts or resumes.
                budget_is_per_call=True,
                lifetime_max_decisions=self._lifetime_max_decisions,
                lifetime_max_tasks=self._lifetime_max_tasks,
                # If the previous wake-up failed only because the model's
                # output did not satisfy the decision schema, hand that
                # validator error back so the retry can correct the decision
                # instead of resampling blind.  Transport errors carry no
                # correctable payload and are not fed back.
                retry_feedback=self._retry_feedback(observed))
        except GoalLeaseHeld as exc:
            # Another runtime/CLI is advancing this goal right now: not a failure,
            # and not an acknowledgement — the other holder owns this state.
            self._runner.record_skip(goal_id, signature=observed["signature"],
                                     outcome="lease held by another executor",
                                     acknowledge=False)
            return {"goal_id": goal_id, "action": "skip",
                    "reason": f"lease held: {exc}",
                    "signature": observed["signature"]}
        except GoalLeaseLost as exc:
            self._runner.record_skip(goal_id, signature=observed["signature"],
                                     outcome="lease lost during advancement",
                                     acknowledge=False)
            return {"goal_id": goal_id, "action": "skip",
                    "reason": f"lease lost: {exc}",
                    "signature": observed["signature"]}
        except Exception as exc:  # noqa: BLE001 - transport/tooling failure
            return self._record_advance_failure(
                goal, observed, state, now,
                error=f"{type(exc).__name__}: {exc}")

        status = report.get("status")
        last_result = report.get("last_result") or {}
        model_failed = bool(last_result.get("model_call_failed"))
        if model_failed:
            return self._record_advance_failure(
                goal, observed, state, now,
                error=str(last_result.get("reason") or "model call failed"))

        after = self.observe(self._goals.get(goal_id) or goal)
        # A decision that leaves the goal ``active`` means the LD's flow is
        # unfinished (e.g. it inspected something and the follow-up decision —
        # submit, wait or finish — is still to come).  Such an advance is
        # deliberately NOT acknowledged, so the next sweep wakes the model
        # again instead of treating the unchanged observation as "handled".
        # Bounded: every unacknowledged advance counts toward the no-progress
        # streak, which escalates to a human after ``max_no_progress``.
        continuation_pending = status == "active"
        entry = self._runner.record_success(
            goal_id, signature=after["signature"],
            outcome=f"{status}:{last_result.get('action')}",
            advanced=True, no_progress=continuation_pending,
            acknowledge=not continuation_pending)
        escalated = None
        if (continuation_pending
                and int(entry.get("consecutive_no_progress") or 0)
                >= self._max_no_progress):
            escalated = self._escalate(
                goal_id, int(entry.get("consecutive_no_progress") or 0),
                "repeated decisions without observable progress",
                observed_decision_count=(
                    self._goals.get(goal_id) or {}).get("decision_count"))
        return {"goal_id": goal_id, "action": "advanced",
                "reason": "LD advanced the goal",
                "plan_reason": plan_reason,
                "goal_status": status,
                "decision_action": last_result.get("action"),
                "next_state": last_result.get("next_state"),
                "task_id": last_result.get("task_id"),
                "continuation_pending": continuation_pending,
                "escalated_to_human": escalated,
                "signature": after["signature"]}

    def _record_advance_failure(self, goal: dict, observed: dict, state: dict,
                                now: float, *, error: str) -> dict[str, Any]:
        """Count and back off a failed advance — unless the observation is stale.

        If another executor advanced the goal while our model call was failing,
        our failure belongs to a world that no longer exists: it must not bump
        the failure streak, must not extend the backoff of a healthy goal and
        must never escalate over the other executor's progress.
        """
        goal_id = goal["goal_id"]
        if self._observation_is_stale(goal):
            self._runner.record_skip(
                goal_id, signature=observed["signature"],
                outcome=f"stale failure ignored (goal advanced elsewhere): "
                        f"{error[:300]}",
                acknowledge=False)
            return {"goal_id": goal_id, "action": "skip",
                    "reason": "stale failure ignored: the goal advanced elsewhere",
                    "detail": error[:300],
                    "signature": observed["signature"]}
        failures = int(state.get("consecutive_failures") or 0) + 1
        delay = self._backoff(failures)
        self._runner.record_failure(
            goal_id, signature=observed["signature"],
            error=error, next_attempt_at=now + delay)
        escalated = None
        if failures >= self._max_model_failures:
            escalated = self._escalate(
                goal_id, failures, error,
                observed_decision_count=goal.get("decision_count"))
        return {"goal_id": goal_id, "action": "error",
                "reason": error,
                "consecutive_failures": failures,
                "retry_in_s": round(delay, 1),
                "escalated_to_human": escalated,
                "signature": observed["signature"]}

    def _retry_feedback(self, observed: Mapping[str, Any]) -> str | None:
        """Validator error of the previous wake-up, when there is one to fix.

        Only ``model output rejected`` errors qualify: those are generation
        defects the model can correct.  Transport failures and stale
        observations have no correctable payload.
        """
        state = self._runner.get(observed["goal_id"]) or {}
        last_error = (state.get("last_error") or "").strip()
        if last_error.startswith("model output rejected:"):
            return last_error
        return None

    def _observation_is_stale(self, goal: dict) -> bool:
        """Did the goal move on since this sweep observed it?"""
        current = self._goals.get(goal["goal_id"]) or {}
        if not current:
            return True
        if current.get("status") not in ADVANCEABLE_STATUSES:
            return True
        return (int(current.get("decision_count") or 0)
                != int(goal.get("decision_count") or 0))

    def _backoff(self, failures: int) -> float:
        if failures <= 0:
            return self._base_backoff_s
        return min(self._max_backoff_s,
                   self._base_backoff_s * (2 ** min(failures - 1, 8)))

    def _escalate(self, goal_id: str, failures: int, error: str, *,
                  observed_decision_count: int | None = None) -> dict:
        """Stop automatic advancement after repeated failures (revalidated).

        The stop is a *conditional* write (``LDGoalStore.stop_goal_if``): it
        applies only when the goal is still advanceable, no other executor
        holds a live lease, and — when provided — the goal's decision count
        still matches our observation.  A runner whose model call failed while
        another runner advanced or completed the goal therefore cannot
        overwrite that progress with ``needs_human``.  This writes only the
        goal's ``status`` — never a task verdict — and the reason is persisted
        so the Human Owner sees why automation gave up.
        """
        note = {
            "stop": "repeated LD failures; automatic advancement disabled",
            "consecutive_failures": failures,
            "error": error[:600],
        }
        try:
            stopped = self._goals.stop_goal_if(
                goal_id, "needs_human", note,
                allow_owner=self._owner,
                expected_decision_count=observed_decision_count)
        except Exception as exc:  # noqa: BLE001 - escalation must not crash a sweep
            return {"escalated": False, "error": f"{type(exc).__name__}: {exc}"}
        if stopped is None:
            return {"escalated": False,
                    "reason": "goal moved on (advanced/completed/leased "
                              "elsewhere); escalation skipped"}
        return {"escalated": True, "reason": note["stop"]}

    def status(self) -> dict[str, Any]:
        goals = {goal["goal_id"]: goal for goal in self._goals.all_goals()}
        bookkeeping = self._runner.all()
        out = []
        for goal_id, goal in sorted(goals.items()):
            state = bookkeeping.get(goal_id) or {}
            out.append({
                "goal_id": goal_id,
                "project_id": goal.get("project_id"),
                "status": goal.get("status"),
                "decision_count": goal.get("decision_count"),
                "model_decision_count": goal.get("model_decision_count"),
                "task_count": goal.get("task_count"),
                "current_task_id": goal.get("current_task_id"),
                "lifetime_max_decisions": goal.get("max_decisions_total"),
                "lifetime_max_tasks": goal.get("max_tasks_total"),
                "runs": state.get("runs", 0),
                "advances": state.get("advances", 0),
                "consecutive_failures": state.get("consecutive_failures", 0),
                "consecutive_no_progress":
                    state.get("consecutive_no_progress", 0),
                "next_attempt_at": state.get("next_attempt_at"),
                "last_error": state.get("last_error"),
                "last_outcome": state.get("last_outcome"),
            })
        return {"goals": out, "count": len(out),
                "advanceable": sum(1 for goal in goals.values()
                                   if goal.get("status") in ADVANCEABLE_STATUSES)}


__all__ = ["ADVANCEABLE_STATUSES", "GoalRunnerStore", "LDGoalRunner",
           "goal_wake_signature"]
