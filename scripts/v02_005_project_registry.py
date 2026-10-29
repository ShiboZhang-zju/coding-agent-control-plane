"""V02-005: registered Git boundaries and per-project concurrency are enforced."""
from __future__ import annotations

import json
import shutil
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import winproc  # noqa: E402
from adapters.base import DispatchResult, TaskSnapshot, TaskState  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.policies import agent_worktree  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402


class ProjectAdapter:
    name = "project-stub"

    def __init__(self):
        self._lock = threading.Lock()
        self.dispatches: list[dict] = []
        self.states: dict[str, TaskState] = {}
        self.cwd: dict[str, str] = {}

    def dispatch(self, request) -> DispatchResult:
        with self._lock:
            job_id = f"project-job-{len(self.dispatches) + 1}"
            self.dispatches.append({
                "job_id": job_id,
                "task_id": request.task_id,
                "title": request.title,
                "cwd": request.cwd,
            })
            self.states[job_id] = TaskState.RUNNING
            self.cwd[job_id] = request.cwd
        return DispatchResult(
            task_id=request.task_id, worker_job_id=job_id, accepted=True,
            raw={"dispatch_http": 200})

    def get_task(self, job_id: str, timeout_s: float = 20.0) -> TaskSnapshot:
        del timeout_s
        with self._lock:
            state = self.states[job_id]
            cwd = self.cwd[job_id]
        return TaskSnapshot(
            task_id=job_id, worker_job_id=job_id, state=state, cwd=cwd,
            detail=state.value,
            finished_at=time.time() if state.terminal else None)

    def collect_artifacts(self, _job_id: str) -> list[str]:
        return []

    def finish(self, job_id: str) -> None:
        with self._lock:
            self.states[job_id] = TaskState.SUCCEEDED


def make_repo(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    (path / "README.md").write_text(f"# {path.name}\n", encoding="utf-8")
    for argv in (["git", "init", "-q", "-b", "main"],
                 ["git", "config", "user.name", "control-plane-test"],
                 ["git", "config", "user.email", "test@local"],
                 ["git", "add", "."],
                 ["git", "commit", "-q", "-m", "init"]):
        winproc.run(argv, cwd=str(path), check=True, capture_output=True, text=True)


def rejected(call) -> str | None:
    try:
        call()
    except (KeyError, ValueError) as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def main() -> int:
    sandbox = ROOT / "sandbox" / "v02-005-projects"
    repo_a = sandbox / "project-a"
    repo_b = sandbox / "project-b"
    repo_c = sandbox / "unregistered"
    for repo in (repo_a, repo_b, repo_c):
        make_repo(repo)

    store = TaskStore(ROOT / ".poc-state" / "v02-005-projects.db")
    store.clear()
    adapter = ProjectAdapter()
    plane = ControlPlane(
        adapter, store, policy=agent_worktree(), require_registered_projects=True)

    project_a = plane.register_project(
        "project-a", str(repo_a), default_adapter=adapter.name,
        policy=agent_worktree(), concurrency_limit=1)
    project_b = plane.register_project(
        "project-b", str(repo_b), default_adapter=adapter.name,
        policy=agent_worktree(), concurrency_limit=2)

    unregistered_error = rejected(
        lambda: plane.enqueue("outside", str(repo_c), title="outside"))
    traversal_error = rejected(
        lambda: plane.enqueue(
            "escape", str(repo_a / ".." / "project-b"), title="escape",
            project_id=project_a["id"]))
    nested_root_error = rejected(
        lambda: plane.register_project("nested", str(repo_a / ".git")))

    store.set_project_active(project_b["id"], False)
    disabled_error = rejected(
        lambda: plane.enqueue(
            "disabled", str(repo_b), title="disabled",
            project_id=project_b["id"]))
    store.set_project_active(project_b["id"], True)

    queued = [
        plane.enqueue("a1", str(repo_a), title="a1", project_id=project_a["id"]),
        plane.enqueue("a2", str(repo_a), title="a2", project_id=project_a["id"]),
        plane.enqueue("b1", str(repo_b), title="b1", project_id=project_b["id"]),
        plane.enqueue("b2", str(repo_b), title="b2", project_id=project_b["id"]),
    ]
    first_wave = [plane.dispatch_next("v02-005", concurrency_limit=3)
                  for _ in range(3)]
    capacity_blocked = plane.dispatch_next("v02-005", concurrency_limit=3)
    first_titles = [item["title"] for item in first_wave if item]
    a2_before_slot = store.get(queued[1]["task_id"])

    first_a = next(item for item in first_wave if item and item["title"] == "a1")
    adapter.finish(first_a["worker_job_id"])
    plane.refresh_known_jobs()
    second_a = plane.dispatch_next("v02-005", concurrency_limit=3)

    by_title = {item["title"]: item for item in store.all()}
    checks = {
        "git_roots_registered": (
            project_a["root"] == str(repo_a.resolve())
            and project_b["root"] == str(repo_b.resolve())),
        "unregistered_cwd_rejected": bool(unregistered_error),
        "explicit_project_path_traversal_rejected": bool(traversal_error),
        "non_top_level_root_rejected": bool(nested_root_error),
        "disabled_project_rejects_new_work": bool(disabled_error),
        "work_items_bound_to_projects": (
            by_title["a1"]["project_id"] == project_a["id"]
            and by_title["b1"]["project_id"] == project_b["id"]),
        "project_default_policy_applied": all(
            item["mode"] == "agent" and item["isolation"] == "worktree"
            for item in queued),
        "global_three_project_limits_first_wave": (
            first_titles == ["a1", "b1", "b2"]),
        "project_a_second_item_waits": (
            capacity_blocked is None and a2_before_slot["queue_status"] == "ready"),
        "project_slot_release_dispatches_a2": (
            second_a is not None and second_a["title"] == "a2"),
    }
    log = {
        "test": "V02-005-project-registry",
        "checks": checks,
        "projects": [project_a, project_b],
        "boundary_rejections": {
            "unregistered": unregistered_error,
            "path_traversal": traversal_error,
            "non_top_level": nested_root_error,
            "disabled": disabled_error,
        },
        "dispatch_order": [item["title"] for item in adapter.dispatches],
        "queue": {
            title: {
                "project_id": item["project_id"],
                "queue_status": item["queue_status"],
                "worker_job_id": item["worker_job_id"],
            }
            for title, item in by_title.items()
        },
        "result": "Pass" if all(checks.values()) else "Fail",
    }
    evidence = ROOT / "evidence" / "jobs" / "V02-005-project-registry.json"
    evidence.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(log, ensure_ascii=False, indent=2))
    store.close()
    return 0 if log["result"] == "Pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
