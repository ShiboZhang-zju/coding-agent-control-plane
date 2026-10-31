"""T47 helper: run one LDGoalRunner as a real OS process (M4-C concurrency).

This is a *test driver*, not a product entry point.  It exists so that
``t47_m4c_concurrency.py`` can exercise multi-runner races with two real
processes, two real SQLite connections and deterministic fault injection —
not just two threads inside one process.

Each invocation opens its own ``TaskStore`` / ``LDGoalStore`` /
``GoalRunnerStore`` on the shared ledger, builds an ``LDGoalRunner`` with the
requested model behaviour, runs the requested scenario and writes a JSON
result file the main test asserts on.

Scenarios
---------
``sweep``          advance goals in a loop; behaviour driven by ``--model``:
                  * ``fail``            the model always raises (transport)
                  * ``fail_project:P``  the model raises only for project P
                  * ``submit_finish``   submit once, wait, finish when verified
                  * ``inspect_once``    inspect on the first call, then submit
                                        and finish (continuation flow)
``stale_escalate`` observe the goal, sleep ``--hold-s``, then escalate with
                  the stale observation (tests fenced escalation).
``lease_then_write`` take the goal lease, publish it, sleep past expiry
                  (while another runner takes over), then attempt a fenced
                  write with the stale token (must lose).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import DispatchResult, TaskState, TaskSnapshot  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.project_service import ProjectService  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from ld.coordinator import LDCoordinator  # noqa: E402
from ld.goal_runner import LDGoalRunner  # noqa: E402
from ld.goal_store import GoalLeaseLost, LDGoalStore  # noqa: E402
from ld.tools import LDTools  # noqa: E402


class StubAdapter:
    name = "stub"

    def dispatch(self, request):  # noqa: ANN001
        return DispatchResult(task_id=request.task_id,
                              worker_job_id=f"job-{request.task_id}",
                              accepted=True)

    def get_task(self, task_id, **_kwargs):  # noqa: ANN001
        return TaskSnapshot(task_id=task_id, worker_job_id=f"job-{task_id}",
                            state=TaskState.RUNNING)

    def pid_alive(self, _pid):  # noqa: ANN001
        return None

    def collect_artifacts(self, _task_id):  # noqa: ANN001
        return []

    def reconcile(self):
        return {"jobs": []}

    def capabilities(self):
        return {}


class DriverModel:
    """Scripted model behaviour selected per project / call count."""

    def __init__(self, mode: str):
        self.mode = mode
        self.calls: list[dict] = []

    def decide(self, decision_input: dict) -> dict:
        project = decision_input.get("requested_project_id")
        state = decision_input.get("goal_state") or {}
        self.calls.append({"project": project})
        if self.mode.startswith("fail_project:"):
            if project == self.mode.split(":", 1)[1]:
                raise RuntimeError("scripted transport failure (per project)")
            return self._progress_decision(project, state)
        if self.mode == "fail":
            raise RuntimeError("scripted transport failure")
        return self._progress_decision(project, state)

    def _progress_decision(self, project, state) -> dict:
        verified = [t["task_id"] for t in state.get("completed_tasks", [])
                    if t.get("verdict") == "verified"]
        if verified:
            return {"action": "finish", "project_id": project,
                    "reason": "verified",
                    "evidence_refs": [f"task:{tid}" for tid in verified],
                    "parameters": {"verified_task_ids": verified,
                                   "completion_conditions": []}}
        if state.get("all_goal_tasks"):
            current = state.get("current_task") or {}
            return {"action": "wait", "project_id": project,
                    "reason": "settle", "evidence_refs": [],
                    "parameters": {"task_id": current.get("task_id") or ""}}
        if self.mode == "inspect_once" and len(self.calls) == 1:
            return {"action": "inspect", "project_id": project,
                    "reason": "inspect first",
                    "evidence_refs": [],
                    "parameters": {"tool": "get_project_progress"}}
        return {"action": "submit_task", "project_id": project,
                "reason": "implement the goal",
                "evidence_refs": [],
                "parameters": {
                    "task": "实现 feature 并让测试通过",
                    "idempotency_key": f"t47-{uuid.uuid4().hex[:6]}",
                    "assertions": [{"kind": "file_exists",
                                    "target": "feature.txt"}],
                }}


def build(ledger: Path, model_mode: str, *, lease_s: float, owner: str,
          base_backoff_s: float, max_model_failures: int):
    store = TaskStore(ledger)
    plane = ControlPlane(StubAdapter(), store, require_registered_projects=True,
                         worker_instance_id="wi-t47")
    service = ProjectService(plane)
    coordinator = LDCoordinator(
        LDTools(service), DriverModel(model_mode),
        model_authorized_security_domains=("local", "shareable"))
    goals = LDGoalStore(store.path, lease_s=lease_s)
    runner = LDGoalRunner(
        coordinator, goals, max_decisions=1, max_tasks=2,
        max_model_failures=max_model_failures,
        base_backoff_s=base_backoff_s, max_backoff_s=base_backoff_s * 4,
        lifetime_max_decisions=20, lifetime_max_tasks=3, owner=owner)
    return {"store": store, "plane": plane, "service": service,
            "coordinator": coordinator, "goals": goals, "runner": runner}


def scenario_sweep(ctx, args) -> dict:
    statuses_seen: list[dict] = []
    escalations: list[dict] = []
    deadline = time.monotonic() + args.duration_s
    sweeps = 0
    while sweeps < args.sweeps and time.monotonic() < deadline:
        sweeps += 1
        result = ctx["runner"].advance_due()
        escalations.extend(
            item.get("escalated_to_human")
            for item in result.get("items") or []
            if item.get("escalated_to_human"))
        statuses = ctx["goals"].statuses()
        statuses_seen.append(statuses)
        if all(status in ("needs_human", "completed", "failed")
               for status in statuses.values()) and statuses:
            break
        time.sleep(args.interval)
    return {
        "scenario": "sweep",
        "sweeps_run": sweeps,
        "model_calls": len(ctx["coordinator"]._model.calls),
        "final_statuses": ctx["goals"].statuses(),
        "escalations": escalations,
        "statuses_observed": statuses_seen[-3:],
        "bookkeeping": ctx["runner"]._runner.all(),
    }


def scenario_stale_escalate(ctx, args) -> dict:
    goal_id = args.goal_id
    observed = ctx["goals"].get(goal_id) or {}
    observed_count = int(observed.get("decision_count") or 0)
    time.sleep(args.hold_s)
    escalated = ctx["runner"]._escalate(
        goal_id, failures=args.failures, error="stale runner failure probe",
        observed_decision_count=observed_count)
    return {
        "scenario": "stale_escalate",
        "observed_decision_count": observed_count,
        "escalated": escalated,
        "final_status": (ctx["goals"].get(goal_id) or {}).get("status"),
        "final_decision_count": int(
            (ctx["goals"].get(goal_id) or {}).get("decision_count") or 0),
    }


def scenario_lease_then_write(ctx, args) -> dict:
    goal_id = args.goal_id
    owner = f"{args.owner}-lease"
    leased = ctx["goals"].acquire_lease(goal_id, owner)
    token = int(leased["lease_token"])
    Path(args.out).write_text(json.dumps({"leased": True, "token": token,
                                          "owner": owner}), encoding="utf-8")
    time.sleep(args.hold_s)
    # Attempt a fenced write with the (now stale) token.
    fenced_error = None
    renewed = None
    try:
        ctx["goals"].begin_step(
            goal_id, int(args.step or 1),
            {"action": "submit_task", "reason": "stale holder write",
             "evidence_refs": [], "parameters": {}}, token)
    except GoalLeaseLost as exc:
        fenced_error = f"GoalLeaseLost(token={exc.token},current={exc.current})"
    except Exception as exc:  # noqa: BLE001
        fenced_error = f"{type(exc).__name__}: {exc}"
    renewed = ctx["goals"].renew_lease(goal_id, owner, token)
    return {
        "scenario": "lease_then_write",
        "token": token,
        "fenced_write_rejected": fenced_error is not None
                                 and fenced_error.startswith("GoalLeaseLost"),
        "fenced_write_error": fenced_error,
        "stale_renew_rejected": renewed is False,
        "final_status": (ctx["goals"].get(goal_id) or {}).get("status"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="T47 multi-process goal-runner driver")
    ap.add_argument("--ledger", type=Path, required=True)
    ap.add_argument("--scenario", required=True,
                    choices=["sweep", "stale_escalate", "lease_then_write"])
    ap.add_argument("--model", default="submit_finish")
    ap.add_argument("--goal-id", default=None)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--sweeps", type=int, default=20)
    ap.add_argument("--duration-s", type=float, default=25.0)
    ap.add_argument("--interval", type=float, default=0.3)
    ap.add_argument("--hold-s", type=float, default=3.0)
    ap.add_argument("--failures", type=int, default=3)
    ap.add_argument("--step", type=int, default=1)
    ap.add_argument("--lease-s", type=float, default=2.0)
    ap.add_argument("--base-backoff-s", type=float, default=0.3)
    ap.add_argument("--max-model-failures", type=int, default=3)
    ap.add_argument("--owner", default=None)
    args = ap.parse_args()

    owner = args.owner or f"t47-{args.scenario}-{uuid.uuid4().hex[:6]}"
    ctx = build(args.ledger, args.model, lease_s=args.lease_s, owner=owner,
                base_backoff_s=args.base_backoff_s,
                max_model_failures=args.max_model_failures)
    try:
        if args.scenario == "sweep":
            result = scenario_sweep(ctx, args)
        elif args.scenario == "stale_escalate":
            result = scenario_stale_escalate(ctx, args)
        else:
            result = scenario_lease_then_write(ctx, args)
    finally:
        try:
            ctx["runner"].close()
            ctx["goals"].close()
            ctx["store"].close()
        except Exception:  # noqa: BLE001
            pass
    args.out.write_text(json.dumps({**result, "owner": owner},
                                   ensure_ascii=False, indent=1, default=str),
                        encoding="utf-8")
    print(json.dumps({"ok": True, "scenario": args.scenario, "owner": owner}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
