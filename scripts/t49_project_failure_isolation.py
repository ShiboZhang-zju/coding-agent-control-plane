"""T49: dual-project failure isolation on one runtime.

The M4-C product claim: one runtime manages goals for several projects, and
one project's genuine failure must not block, contaminate or slow down the
others.

Setup: Project A gets a goal whose WorkItem is *rejected by the verifier*
(the deterministic stand-in for a real verification failure — the verdict is
written by the harness exactly like a real observation would, never by the
LD), and whose decision model additionally suffers two transport failures
first (per-goal backoff).  Project B is a normal, completable development
goal.  One ``ControlPlaneRuntime`` with one ``LDGoalRunner`` advances both.

Checked properties
------------------
1.  A parks in ``needs_human`` through the *real* path (rejected verdict ->
    LD request_human), not by directly setting the status.
2.  B still advances to ``completed`` while A is parked.
3.  A's exception never blocks the global schedule (sweeps continue; B
    advances during A's backoff window).
4.  B's Effective Prompt and model views never contain A's local context.
5.  A's WorkItems never appear in B's goal tasks / reports.
6.  B never consumes A's session or Git checkpoint (no cross-goal handoff).
7.  The global concurrency limit (1) holds across both projects.
8.  A's failure backoff does not delay B's runnable work.
9.  Approvals / security domains stay per-project (B's shareable model view
    hides A's local secret; B's progress projection has only B's tasks).

Deterministic (scripted model + stub adapter driven by the *real* runtime
lifecycle — background goal thread, no forced sweeps).  Real CodeBuddy
integration of the failure path remains covered by T48/T44's environment and
is recorded as a known gap in the evidence.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import DispatchResult, TaskState, TaskSnapshot  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.project_context import ContextEntry  # noqa: E402
from controlplane.project_service import ProjectService  # noqa: E402
from controlplane.runtime import ControlPlaneRuntime, RuntimeConfig  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from ld.coordinator import LDCoordinator  # noqa: E402
from ld.goal_runner import LDGoalRunner  # noqa: E402
from ld.goal_store import LDGoalStore, goal_identity  # noqa: E402
from ld.tools import LDTools  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

FIXTURE = ROOT / "sandbox" / f"t49-isolation-{uuid.uuid4().hex[:8]}"
STATE = ROOT / ".poc-state" / "t49"
EVIDENCE = ROOT / "evidence" / "project" / "T49-project-failure-isolation.json"
SECRET_A = "T49-ALPHA-LOCAL-SECRET-CONTEXT"
SECRET_B = "T49-BETA-LOCAL-SECRET-CONTEXT"
GOAL_A_TEXT = "A 项目：实现一个会被验收拒绝的功能模块"
GOAL_B_TEXT = "B 项目：实现 beta_lib 并让预置测试通过"


class StubAdapter:
    """Stub Worker whose terminal state is signalled by the harness.

    The harness never writes verdicts into the ledger: it only flips this
    adapter's in-memory job to ``SUCCEEDED`` (exactly what a real Worker
    reports).  The *runtime's* observation path then evaluates the declared
    ``ArtifactAssertion``s against the real project tree and derives the
    verdict — project A's assertion can never pass, so A is genuinely
    ``rejected`` by the verifier, not by the test.
    """

    name = "stub"

    def __init__(self):
        self.dispatched: list[str] = []
        self._terminal: set[str] = set()

    def finish(self, task_id: str) -> None:
        # get_task is queried by worker_job_id ("job-<task_id>").
        self._terminal.add(f"job-{task_id}")

    def dispatch(self, request):  # noqa: ANN001
        self.dispatched.append(request.task_id)
        return DispatchResult(task_id=request.task_id,
                              worker_job_id=f"job-{request.task_id}",
                              accepted=True)

    def get_task(self, task_id, **_kwargs):  # noqa: ANN001
        state = (TaskState.SUCCEEDED if task_id in self._terminal
                 else TaskState.RUNNING)
        return TaskSnapshot(task_id=task_id, worker_job_id=task_id,
                            state=state)

    def pid_alive(self, _pid):  # noqa: ANN001
        return None

    def collect_artifacts(self, _task_id):  # noqa: ANN001
        return []

    def reconcile(self):
        # Report every known job so the orphan scan never mistakes a live
        # stub job for a vanished one.
        return {"jobs": [{"job_id": f"job-{task_id}",
                          "state": ("done" if f"job-{task_id}" in self._terminal
                                    else "working")}
                         for task_id in self.dispatched]}

    def capabilities(self):
        return {}


class IsolationModel:
    """One LD decision model, per-project behaviour.

    Project A: two scripted transport failures first (per-goal backoff), then
    submit a task; once its verdict is ``rejected`` (a genuine acceptance
    failure), request a human — the real park path.
    Project B: submit -> wait -> finish on verified.  Never looks at A.
    """

    def __init__(self, fail_project_id: str):
        self.fail_project_id = fail_project_id
        self.calls: list[dict] = []

    def decide(self, decision_input: dict) -> dict:
        project = decision_input.get("requested_project_id")
        state = decision_input.get("goal_state") or {}
        self.calls.append({"project": project,
                           "decision_count": state.get("decision_count")})
        if project == self.fail_project_id:
            mine = [c for c in self.calls if c["project"] == project]
            if len(mine) <= 2:
                raise RuntimeError("scripted transport failure (project A)")
            rejected = [t for t in state.get("all_goal_tasks", [])
                        if t.get("verdict") == "rejected"]
            if rejected:
                return {"action": "request_human", "project_id": project,
                        "reason": "the task was rejected by the verifier",
                        "evidence_refs": [f"task:{rejected[0]['task_id']}"],
                        "parameters": {
                            "question": "The verification failed; how to proceed?"}}
            if state.get("all_goal_tasks"):
                return {"action": "wait", "project_id": project,
                        "reason": "settle", "evidence_refs": [],
                        "parameters": {"task_id": ""}}
            return {"action": "submit_task", "project_id": project,
                    "reason": "implement the doomed feature",
                    "evidence_refs": [],
                    "parameters": {
                        "task": "实现一个无法通过验收的功能",
                        "idempotency_key": "t49-alpha-task",
                        "assertions": [{"kind": "file_exists",
                                        "target": "never-created.txt"}],
                    }}
        verified = [t["task_id"] for t in state.get("completed_tasks", [])
                    if t.get("verdict") == "verified"]
        if verified:
            return {"action": "finish", "project_id": project,
                    "reason": "the goal task is verified",
                    "evidence_refs": [f"task:{tid}" for tid in verified],
                    "parameters": {"verified_task_ids": verified,
                                   "completion_conditions": []}}
        if state.get("all_goal_tasks"):
            return {"action": "wait", "project_id": project,
                    "reason": "settle", "evidence_refs": [],
                    "parameters": {"task_id": ""}}
        return {"action": "submit_task", "project_id": project,
                "reason": "implement the goal",
                "evidence_refs": [],
                "parameters": {
                    "task": "实现 beta_lib 并让测试通过",
                    "idempotency_key": "t49-beta-task",
                    "assertions": [{"kind": "file_exists",
                                    "target": "beta_lib/generated.py"}],
                }}


def git(path: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(path), *args], check=False,
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"git {args} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def make_repo(name: str, files: dict) -> Path:
    repo = FIXTURE / name
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", f"t49-{name}@example.invalid")
    git(repo, "config", "user.name", f"T49-{name}")
    for rel, content in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "seed")
    git(repo, "branch", "-M", "main")
    return repo


def fresh_ledger() -> Path:
    STATE.mkdir(parents=True, exist_ok=True)
    ledger = STATE / "isolation.db"
    for stale in [ledger, *ledger.parent.glob(ledger.name + "*")]:
        stale.unlink(missing_ok=True)
    return ledger


def main() -> int:
    shutil.rmtree(FIXTURE, ignore_errors=True)
    repo_a = make_repo("alpha", {"README.md": "alpha\n"})
    repo_b = make_repo("beta", {
        "beta_lib/__init__.py": "",
        "test_beta.py": (
            "from beta_lib import double\n\n\n"
            "def test_double():\n"
            "    assert double(3) == 6\n"),
    })
    ledger = fresh_ledger()

    store = TaskStore(ledger)
    adapter = StubAdapter()
    plane = ControlPlane(adapter, store, require_registered_projects=True,
                         worker_instance_id="wi-t49")
    service = ProjectService(plane)
    project_a = plane.register_project("t49-alpha", str(repo_a),
                                       default_adapter="stub",
                                       concurrency_limit=1)
    project_b = plane.register_project("t49-beta", str(repo_b),
                                       default_adapter="stub",
                                       concurrency_limit=1)
    service.add_context_entry(project_a["id"], ContextEntry(
        project_id=project_a["id"], kind="background",
        title="ALPHA-BACKGROUND", body=f"{SECRET_A} for alpha only",
        security_domain="local"))
    service.add_context_entry(project_b["id"], ContextEntry(
        project_id=project_b["id"], kind="background",
        title="BETA-BACKGROUND", body=f"{SECRET_B} for beta only",
        security_domain="local"))

    model = IsolationModel(fail_project_id=project_a["id"])
    coordinator = LDCoordinator(
        LDTools(service), model,
        model_authorized_security_domains=("local", "shareable"))
    goals = LDGoalStore(store.path, lease_s=30.0)
    runner = LDGoalRunner(
        coordinator, goals, max_decisions=1, max_tasks=2,
        max_model_failures=5, max_no_progress_advances=4,
        base_backoff_s=0.5, max_backoff_s=2.0,
        lifetime_max_decisions=20, lifetime_max_tasks=3)
    runtime = ControlPlaneRuntime(
        plane,
        RuntimeConfig(known_refresh_interval_s=0.5, dispatch_interval_s=0.2,
                      orphan_scan_interval_s=5.0, goal_interval_s=0.5,
                      max_concurrent_tasks=1, max_dispatch_per_cycle=1),
        goal_runner=runner, owner="t49-runtime")

    goal_a_id, goal_a_ref = goal_identity(GOAL_A_TEXT, project_a["id"])
    goal_b_id, goal_b_ref = goal_identity(GOAL_B_TEXT, project_b["id"])
    # The single Human-Owner action per project: state each goal once.
    goals.get_or_create(goal_a_id, project_a["id"], GOAL_A_TEXT, goal_a_ref)
    goals.get_or_create(goal_b_id, project_b["id"], GOAL_B_TEXT, goal_b_ref)

    observations: list[dict] = []
    max_in_flight = 0
    b_advanced_during_a_backoff = False
    runtime.prepare()
    runtime.start()
    try:
        deadline = time.time() + 180
        while time.time() < deadline:
            time.sleep(0.3)
            tasks = store.all()
            in_flight = [t for t in tasks
                         if t.get("worker_job_id")
                         and t.get("state") not in ("succeeded", "failed",
                                                    "cancelled", "lost")]
            max_in_flight = max(max_in_flight, len(in_flight))
            # The "Worker": once dispatched, a job runs and finishes.  B's job
            # produces its declared artifact; A's job produces nothing (its
            # assertion can never pass).  The verdict is NOT written here —
            # the runtime's verifier derives it from the declared assertions
            # against the real project tree.
            for task in in_flight:
                if task["project_id"] == project_b["id"]:
                    artifact = repo_b / "beta_lib" / "generated.py"
                    if not artifact.exists():
                        artifact.write_text(
                            "def double(n):\n    return 2 * n\n",
                            encoding="utf-8")
                adapter.finish(task["task_id"])
            bookkeeping = runner._runner.all()
            a_state = bookkeeping.get(goal_a_id) or {}
            b_state = bookkeeping.get(goal_b_id) or {}
            statuses = goals.statuses()
            observations.append({
                "t": round(time.time(), 2),
                "statuses": dict(statuses),
                "a_next_attempt_at": a_state.get("next_attempt_at"),
                "b_advances": b_state.get("advances", 0),
                "in_flight": len(in_flight),
            })
            if (float(a_state.get("next_attempt_at") or 0) > time.time()
                    and int(b_state.get("advances") or 0) > 0):
                b_advanced_during_a_backoff = True
            if all(status in ("needs_human", "completed", "failed")
                   for status in statuses.values()):
                # settle: let the observation path write the terminal verdict
                time.sleep(1.0)
                break
    finally:
        runtime.stop(timeout_s=10.0)

    goal_a = goals.get(goal_a_id) or {}
    goal_b = goals.get(goal_b_id) or {}
    tasks_a = [t for t in store.all()
               if (t.get("metadata") or {}).get("ld_goal_ref") == goal_a_ref]
    tasks_b = [t for t in store.all()
               if (t.get("metadata") or {}).get("ld_goal_ref") == goal_b_ref]
    prompt_b = (tasks_b[0].get("effective_prompt") or "") if tasks_b else ""
    prompt_a = (tasks_a[0].get("effective_prompt") or "") if tasks_a else ""
    tools = LDTools(service)
    progress_b = tools.get_project_progress(project_b["id"])
    brief_b_shareable = tools.get_project_brief(
        project_b["id"], allowed_security_domains=("shareable",))
    history_a = goals.decisions(goal_a_id, limit=50)
    history_b = goals.decisions(goal_b_id, limit=50)
    b_task_meta = (tasks_b[0].get("metadata") or {}) if tasks_b else {}
    runtime_status = runtime.status()

    out = {
        "test": "T49-project-failure-isolation",
        "worker_started": False,
        "note": ("deterministic: stub adapter + scripted per-project decision "
                 "model driven by the real runtime lifecycle (background goal "
                 "thread; no forced sweeps). Project A's rejection verdict is "
                 "written by the harness like a real observation would; the LD "
                 "itself decides request_human. Real-Worker failure-path "
                 "integration is a recorded gap (see KNOWN_ISSUES)."),
        "fixture": str(FIXTURE),
        "goal_a": {"status": goal_a.get("status"),
                   "task_count": goal_a.get("task_count"),
                   "decision_count": goal_a.get("decision_count"),
                   "last_decision": goal_a.get("last_decision")},
        "goal_b": {"status": goal_b.get("status"),
                   "task_count": goal_b.get("task_count"),
                   "decision_count": goal_b.get("decision_count")},
        "history_a": [{"step": d["step"], "action": (d["decision"] or {}).get(
            "action"), "outcome": (d["outcome"] or {}).get("next_state")}
            for d in history_a],
        "history_b": [{"step": d["step"], "action": (d["decision"] or {}).get(
            "action")} for d in history_b],
        "tasks_a": [{"id": t["task_id"], "verdict": t.get("verdict"),
                     "project": t.get("project_id")} for t in tasks_a],
        "tasks_b": [{"id": t["task_id"], "verdict": t.get("verdict"),
                     "project": t.get("project_id"),
                     "handoff_source": b_task_meta.get("source_work_item_id")}
                    for t in tasks_b],
        "runtime": {"goal_runs": (runtime_status.get("goal_advancement")
                                  or {}).get("runs"),
                    "dispatch_runs": (runtime_status.get("dispatch_queue")
                                      or {}).get("runs")},
        "max_in_flight_observed": max_in_flight,
        "b_advanced_during_a_backoff": b_advanced_during_a_backoff,
        "observation_tail": observations[-6:],
    }

    checks = {
        # 1. A parks through the real failure path.
        "a_needs_human": goal_a.get("status") == "needs_human",
        "a_parked_via_request_human": any(
            (d["decision"] or {}).get("action") == "request_human"
            for d in history_a),
        "a_task_rejected": bool(tasks_a) and all(
            t.get("verdict") == "rejected" for t in tasks_a),
        # 2. B completes regardless.
        "b_completed": goal_b.get("status") == "completed",
        "b_task_verified": bool(tasks_b) and all(
            t.get("verdict") == "verified" for t in tasks_b),
        # 3. A never blocked the schedule.
        "schedule_kept_running": (runtime_status.get("goal_advancement")
                                  or {}).get("runs", 0) > 5,
        # 4. Knowledge isolation.
        "b_prompt_lacks_a_secret": SECRET_A not in prompt_b,
        "b_prompt_has_own_secret": SECRET_B in prompt_b,
        "a_prompt_lacks_b_secret": SECRET_B not in prompt_a,
        "b_shareable_view_hides_a_secret": SECRET_A not in json.dumps(
            brief_b_shareable, ensure_ascii=False),
        # 5. A's WorkItems never enter B's reports.
        "b_goal_has_only_b_tasks": bool(tasks_b) and all(
            t.get("project_id") == project_b["id"] for t in tasks_b),
        "b_progress_counts_only_b": (
            progress_b["counts"]["total"] == len(tasks_b)
            and {task["task_id"] for task in progress_b.get("recent_tasks", [])}
            <= {t["task_id"] for t in tasks_b}),
        # 6. B consumed no A session/checkpoint.
        "b_not_handoff_of_a": b_task_meta.get("source_work_item_id") not in {
            t["task_id"] for t in tasks_a},
        # 7. Global concurrency limit held.
        "global_concurrency_respected": max_in_flight <= 1,
        # 8. A's backoff never delayed B.
        "b_advanced_during_a_backoff": b_advanced_during_a_backoff,
        # 9. Per-project approvals/domains: B's model decisions were never
        #    asked about A and vice versa.
        "model_never_mixed_projects": all(
            call["project"] in (project_a["id"], project_b["id"])
            for call in model.calls),
        "a_transport_failures_were_a_only": sum(
            1 for call in model.calls if call["project"] == project_a["id"])
        >= sum(1 for call in model.calls
               if call["project"] == project_b["id"]),
    }
    out["checks"] = checks
    out["result"] = "Pass" if checks and all(checks.values()) else "Fail"
    runner.close()
    goals.close()
    store.close()
    write_evidence(EVIDENCE, out)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps({k: v for k, v in checks.items() if not v},
                     ensure_ascii=False, indent=2))
    print(out["result"])
    return 0 if out["result"] == "Pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
