"""T34: M3 Session Registry / Handoff against a REAL CodeBuddy Worker.

Covers the two M3 items that T33 could not (both need a real Worker):

M3-01  the worker's ``sessionId`` is observed consistently across the four
       snapshot paths: right after dispatch, via refresh, at terminal state,
       and after a Worker service restart (including a restart while the job
       is still running).
M3-02  a full session handoff: Session A finishes a real agent task, its
       acceptance generates project context, and Session B is dispatched as a
       handoff WorkItem that never reads Session A's transcript and still
       passes its own artifact acceptance (verdict=verified).

The Worker is the loopback CodeBuddy HTTP service managed by
``scripts/serve_ctl.py`` (start it before running this test). The ledger and
sandbox fixtures are throw-away. Evidence goes to
``evidence/project/T34-m3-real-worker.json``.

Usage:
    python scripts/t34_m3_real_worker.py --part m01   # session id paths (shell)
    python scripts/t34_m3_real_worker.py --part m02   # agent handoff e2e
    python scripts/t34_m3_real_worker.py --part all
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import ArtifactAssertion, Verdict  # noqa: E402
from adapters.codebuddy.adapter import CodeBuddyAdapter  # noqa: E402
from controlplane.handoff import HandoffError  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.policies import AGENT_WORKTREE, poc_shell  # noqa: E402
from controlplane.project_service import ProjectService  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

LEDGER = ROOT / ".poc-state" / "t34-m3-real.db"
FIXTURE = ROOT / "sandbox" / f"m3-real-{uuid.uuid4().hex[:8]}"
REPO = FIXTURE / "repo-a"
EVIDENCE = ROOT / "evidence" / "project" / "T34-m3-real-worker.json"
POLL_S = 5.0
TERMINAL = {"succeeded", "failed", "cancelled", "lost"}


def git(path: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(path), *args], check=False,
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"git {args} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def make_repo() -> None:
    FIXTURE.mkdir(parents=True, exist_ok=True)
    REPO.mkdir(parents=True, exist_ok=True)
    git(REPO, "init", "-q")
    git(REPO, "config", "user.email", "t34@example.invalid")
    git(REPO, "config", "user.name", "T34")
    (REPO / "seed.txt").write_text("seed\n", encoding="utf-8")
    git(REPO, "add", "seed.txt")
    git(REPO, "commit", "-q", "-m", "seed")
    git(REPO, "branch", "-M", "main")


def serve(*action: str) -> dict:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "serve_ctl.py"), *action],
        capture_output=True, text=True, timeout=180, cwd=str(ROOT))
    try:
        return json.loads(proc.stdout.strip() or "{}")
    except json.JSONDecodeError:
        return {"_raw": proc.stdout, "_stderr": proc.stderr}


def approve_and_dispatch(plane: ControlPlane, rec: dict,
                         owner: str = "t34-owner") -> dict:
    if rec.get("queue_status") == "needs_approval":
        plane.approve(rec["task_digest"], note="approved_by=T34")
    claimed = plane.dispatch_next(owner, concurrency_limit=2)
    if not claimed:
        raise RuntimeError("dispatch_next claimed nothing after approval")
    return claimed


def wait_terminal(plane: ControlPlane, task_id: str, timeout_s: float) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        plane.refresh_known_jobs()
        item = plane.store.get(task_id) or {}
        if item.get("state") in TERMINAL:
            return item
        time.sleep(POLL_S)
    item = plane.store.get(task_id) or {}
    raise RuntimeError(f"task {task_id} not terminal: {item.get('state')}")


def session_view(store: TaskStore, task_id: str) -> dict:
    item = store.get(task_id) or {}
    attempt_id = item.get("current_attempt_id")
    ref = store.session_reference_for_attempt(attempt_id) if attempt_id else None
    return {
        "work_item_session_id": item.get("session_id"),
        "worker_job_id": item.get("worker_job_id"),
        "attempt_id": attempt_id,
        "session_reference": ({k: ref.get(k) for k in
                               ("id", "session_id", "worker_instance_id",
                                "node_id", "work_item_id", "attempt_id")}
                              if ref else None),
        "state": item.get("state"),
        "verdict": item.get("verdict"),
    }


def m01_session_id_paths(store: TaskStore) -> dict:
    adapter = CodeBuddyAdapter(autologin=True)
    plane = ControlPlane(adapter, store, require_registered_projects=True)
    service = ProjectService(plane)
    project = plane.register_project("t34-m3-real", str(REPO),
                                     default_adapter="codebuddy")
    workspace = store.default_workspace(project["id"])
    log: dict = {"project_id": project["id"], "workspace_id": workspace["id"]}

    # ---- job 1: dispatch / refresh / terminal without restart ------------- #
    # NOTE (T34 finding): shell jobs execute the *frozen prompt itself* as a
    # script. submit_project_task() injects a multi-line project context into
    # the prompt, so a shell policy there produces a rejected task whose first
    # prompt line fails as a command. Raw plane.enqueue() keeps the prompt a
    # bare command while still binding project_id + workspace_id, which the
    # Session Registry requires.
    cmd1 = "timeout /t 40 /nobreak >nul & echo m3-real-1 > m3-session-1.txt"
    rec1 = plane.enqueue(
        cmd1, str(REPO), title="T34-m01-job1",
        idempotency_key=f"t34-m01-1-{uuid.uuid4().hex[:6]}",
        assertions=[ArtifactAssertion("file_exists", "m3-session-1.txt")],
        policy=poc_shell(), project_id=project["id"],
        workspace_id=workspace["id"])
    claimed1 = approve_and_dispatch(plane, rec1)
    job1 = claimed1["worker_job_id"]

    # Path 1: immediately after dispatch (STARTING, before any refresh).
    snap_early = adapter.get_task(job1)
    log["dispatch_path"] = {
        "worker_job_id": job1,
        "state_after_dispatch": claimed1.get("state"),
        "session_id_in_first_snapshot": bool(snap_early.session_id),
    }

    # Path 2: refresh persists the snapshot and registers the SessionReference.
    plane.refresh_known_jobs()
    view_refresh = session_view(store, claimed1["task_id"])
    log["refresh_path"] = {
        "session_id_persisted": bool(view_refresh["work_item_session_id"]),
        "session_reference_registered": view_refresh["session_reference"] is not None,
        "reference_matches_work_item": (
            view_refresh["session_reference"] is not None
            and view_refresh["session_reference"]["session_id"]
            == view_refresh["work_item_session_id"]),
        "view": view_refresh,
    }

    # Path 3: terminal refresh keeps the identity stable and acceptance runs.
    terminal1 = wait_terminal(plane, claimed1["task_id"], timeout_s=180)
    view_terminal = session_view(store, claimed1["task_id"])
    log["terminal_path"] = {
        "state": terminal1.get("state"),
        "verdict": terminal1.get("verdict"),
        "session_id_stable": (
            view_terminal["work_item_session_id"]
            == view_refresh["work_item_session_id"]),
        "reference_immutable": (
            view_terminal["session_reference"] is not None
            and view_terminal["session_reference"]["id"]
            == view_refresh["session_reference"]["id"]),
        "assertion_passed": terminal1.get("verdict") == Verdict.VERIFIED.value,
    }

    # ---- job 2: restart the Worker while the job is still running --------- #
    cmd2 = "timeout /t 75 /nobreak >nul & echo m3-real-2 > m3-session-2.txt"
    rec2 = plane.enqueue(
        cmd2, str(REPO), title="T34-m01-job2",
        idempotency_key=f"t34-m01-2-{uuid.uuid4().hex[:6]}",
        assertions=[ArtifactAssertion("file_exists", "m3-session-2.txt")],
        policy=poc_shell(), project_id=project["id"],
        workspace_id=workspace["id"])
    claimed2 = approve_and_dispatch(plane, rec2)
    plane.refresh_known_jobs()
    view_before_restart = session_view(store, claimed2["task_id"])

    stopped = serve("stop")
    started = serve("start")
    log["restart"] = {"stop": stopped, "start": started}

    # Path 4: a fresh adapter + plane reconcile the in-flight job after the
    # Worker service restarted underneath them.
    adapter2 = CodeBuddyAdapter(autologin=True)
    plane2 = ControlPlane(adapter2, store)
    snap_after = adapter2.get_task(claimed2["worker_job_id"])
    plane2.refresh_known_jobs()
    view_after_restart = session_view(store, claimed2["task_id"])
    terminal2 = wait_terminal(plane2, claimed2["task_id"], timeout_s=240)
    view_terminal2 = session_view(store, claimed2["task_id"])

    log["restart_path"] = {
        "session_id_same_after_restart": (
            view_after_restart["work_item_session_id"]
            == view_before_restart["work_item_session_id"]),
        "snapshot_session_id_same": (
            snap_after.session_id == view_before_restart["work_item_session_id"]),
        "reference_survives_restart": (
            view_after_restart["session_reference"] is not None
            and view_after_restart["session_reference"]["id"]
            == view_before_restart["session_reference"]["id"]),
        "terminal_state": terminal2.get("state"),
        "terminal_verdict": terminal2.get("verdict"),
        "session_id_stable_to_terminal": (
            view_terminal2["work_item_session_id"]
            == view_before_restart["work_item_session_id"]),
        "before_restart": view_before_restart,
        "after_restart": view_after_restart,
    }

    log["checks"] = {
        "dispatch_snapshot_has_session": log["dispatch_path"]["session_id_in_first_snapshot"],
        "refresh_persists_and_registers": (
            log["refresh_path"]["session_id_persisted"]
            and log["refresh_path"]["session_reference_registered"]
            and log["refresh_path"]["reference_matches_work_item"]),
        "terminal_identity_stable": log["terminal_path"]["session_id_stable"],
        "terminal_reference_immutable": log["terminal_path"]["reference_immutable"],
        "terminal_acceptance_verified": log["terminal_path"]["assertion_passed"],
        "restart_keeps_session_id": log["restart_path"]["session_id_same_after_restart"],
        "restart_snapshot_session_id_same": log["restart_path"]["snapshot_session_id_same"],
        "restart_reference_survives": log["restart_path"]["reference_survives_restart"],
        "restart_terminal_stable": log["restart_path"]["session_id_stable_to_terminal"],
    }
    log["result"] = "Pass" if all(log["checks"].values()) else "Fail"
    return log


def m02_agent_handoff(store: TaskStore) -> dict:
    adapter = CodeBuddyAdapter(autologin=True)
    plane = ControlPlane(adapter, store, require_registered_projects=True)
    service = ProjectService(plane)
    project = plane.store.get_project_by_name("t34-m3-real") if hasattr(
        plane.store, "get_project_by_name") else None
    if not project:
        for p in store.all_projects():
            if p.get("name") == "t34-m3-real":
                project = p
                break
    if not project:
        project = plane.register_project("t34-m3-real", str(REPO),
                                         default_adapter="codebuddy")
    project_id = project["id"]
    workspace = store.default_workspace(project_id)
    log: dict = {"project_id": project_id}

    # ---- Session A: a real agent task in its own worktree ----------------- #
    task_a_text = ("在仓库根目录创建文件 session-a.txt，内容为一行文本"
                   " hello-from-session-a。除了创建这个文件外不要做任何其他修改，"
                   "不要提交 git。")
    rec_a = service.submit_project_task(
        project_id, task_a_text, title="T34-m02-session-a",
        idempotency_key=f"t34-m02-a-{uuid.uuid4().hex[:6]}",
        assertions=[ArtifactAssertion("file_exists", "session-a.txt")],
        policy=AGENT_WORKTREE, workspace_id=workspace["id"],
        allow_blocked=True)
    claimed_a = approve_and_dispatch(plane, rec_a)
    terminal_a = wait_terminal(plane, claimed_a["task_id"], timeout_s=900)
    view_a = session_view(store, claimed_a["task_id"])
    log["session_a"] = {
        "task_id": claimed_a["task_id"],
        "state": terminal_a.get("state"),
        "verdict": terminal_a.get("verdict"),
        "worker_job_id": view_a["worker_job_id"],
        "worker_session_id": view_a["work_item_session_id"],
        "session_reference_registered": view_a["session_reference"] is not None,
        "head_commit_recorded": bool(terminal_a.get("head_commit")),
        "worktree_recorded": bool(terminal_a.get("worktree")),
    }

    # ---- handoff prepare (context-only: real jobs record no head_commit) --- #
    task_b_text = ("在仓库根目录创建文件 session-b.txt，内容为一行文本"
                   " hello-from-session-b。除了创建这个文件外不要做任何其他修改，"
                   "不要提交 git。")
    try:
        prepared = service.prepare_handoff(
            claimed_a["task_id"], task_b_text, require_checkpoint=False)
        log["handoff_prepare"] = {
            "ready": prepared["ready"],
            "blockers": prepared["blockers"],
            "checkpoint_kind": prepared["checkpoint"]["kind"],
        }
        if not prepared["ready"]:
            log["result"] = "Fail"
            log["checks"] = {"prepare_ready": False}
            return log
        record_b = service.submit_handoff(
            claimed_a["task_id"], task_b_text,
            assertions=[ArtifactAssertion("file_exists", "session-b.txt")],
            policy=AGENT_WORKTREE, require_checkpoint=False,
            title="T34-m02-session-b")
    except HandoffError as exc:
        log["handoff_error"] = f"{type(exc).__name__}: {exc}"
        log["result"] = "Fail"
        log["checks"] = {"handoff_no_error": False}
        return log

    # ---- Session B: dispatched fresh, never resumes Session A -------------- #
    claimed_b = approve_and_dispatch(plane, record_b)
    prompt_b = (store.get(claimed_b["task_id"]) or {}).get("effective_prompt") or ""
    terminal_b = wait_terminal(plane, claimed_b["task_id"], timeout_s=900)
    view_b = session_view(store, claimed_b["task_id"])
    refs = {r["attempt_id"]: r for r in store.session_references(project_id)}
    session_a_prompt = (store.get(claimed_a["task_id"]) or {}).get("effective_prompt") or ""

    log["session_b"] = {
        "task_id": claimed_b["task_id"],
        "state": terminal_b.get("state"),
        "verdict": terminal_b.get("verdict"),
        "worker_job_id": view_b["worker_job_id"],
        "worker_session_id": view_b["work_item_session_id"],
        "handoff_prompt_rendered": "# SESSION HANDOFF" in prompt_b,
        "no_resume_directive_present": "do not resume" in prompt_b,
        "distinct_session": (
            view_b["work_item_session_id"]
            != view_a["work_item_session_id"]),
        "distinct_worker_job": (
            view_b["worker_job_id"] != view_a["worker_job_id"]),
        "session_reference_registered": view_b["session_reference"] is not None,
        "prompt_is_not_session_a_prompt": prompt_b != session_a_prompt,
        "registered_attempts": sorted(refs),
    }

    log["checks"] = {
        "session_a_verified": terminal_a.get("verdict") == Verdict.VERIFIED.value,
        "session_a_reference_registered": view_a["session_reference"] is not None,
        "prepare_ready_context_only": log["handoff_prepare"]["ready"],
        "session_b_verified": terminal_b.get("verdict") == Verdict.VERIFIED.value,
        "handoff_prompt_rendered": log["session_b"]["handoff_prompt_rendered"],
        "fresh_session_no_resume": (
            log["session_b"]["distinct_session"]
            and log["session_b"]["distinct_worker_job"]),
        "session_b_reference_registered": view_b["session_reference"] is not None,
        "two_distinct_attempts_registered": len(refs) >= 2,
    }
    log["result"] = "Pass" if all(log["checks"].values()) else "Fail"
    return log


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--part", choices=["m01", "m02", "all"], default="all")
    ap.add_argument("--keep-serve", action="store_true",
                    help="do not stop the Worker service at the end")
    args = ap.parse_args()

    status = serve("status")
    if not status.get("listening"):
        print("Worker service not listening on 8787; run "
              "`python scripts/serve_ctl.py start` first", file=sys.stderr)
        return 2

    for stale in [LEDGER, *LEDGER.parent.glob(LEDGER.name + ".*")]:
        stale.unlink(missing_ok=True)
    make_repo()

    log: dict = {"test": "T34-m3-real-worker", "worker_started": True,
                 "fixture": str(FIXTURE.relative_to(ROOT))}
    store = TaskStore(LEDGER)
    try:
        if args.part in ("m01", "all"):
            log["M3-01"] = m01_session_id_paths(store)
        if args.part in ("m02", "all"):
            log["M3-02"] = m02_agent_handoff(store)
    finally:
        store.close()

    checks = {f"{name}.{key}": value
              for name in ("M3-01", "M3-02") if name in log
              for key, value in (log[name].get("checks") or {}).items()}
    log["checks"] = checks
    log["result"] = ("Pass" if checks and all(checks.values()) else
                     "Fail" if checks else "Incomplete")
    write_evidence(EVIDENCE, log)
    print(json.dumps(log, ensure_ascii=False, indent=2, default=str))

    if not args.keep_serve:
        print("stopping Worker service:", serve("stop"))
    return 0 if log["result"] == "Pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
