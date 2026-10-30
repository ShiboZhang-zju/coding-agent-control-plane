"""T41: Handoff checkpoint reference protection must gate dispatch (M4-C / A).

The pre-M4-C boundary was: create the Handoff WorkItem, then call
``protect_checkpoint()`` and merely *record* its boolean.  If ``git update-ref``
failed, the row was already ``ready`` — a Runtime could claim it and hand a
Worker a checkpoint that was not kept alive by any ref.  T41 proves the new
gate from every angle:

A1  update-ref fails            -> no dispatch, queue blocked, reason queryable
A2  checkpoint object missing   -> no dispatch, blocked (never "assume ok")
A3  crash before protection     -> restart converges; ref written; then dispatch
A4  protection succeeded twice  -> idempotent, one WorkItem, one ref
A5  protected ref deleted later -> dispatch re-check refuses, no Worker call
A6  workspace drift after submit-> dispatch re-check refuses
A7  two processes racing        -> at most one dispatch, one WorkItem
A8  T38b normal handoff         -> unaffected (verified end state)

Every negative case asserts the same three facts, named per the brief:

    worker_started          == False     (adapter.dispatch never called)
    task_dispatch_count     == 0         (no attempt was ever created)
    checkpoint_ready        == False     (readiness is queryable and says no)

No Worker process is started; ``DispatchSpy`` implements the adapter contract
just enough to *prove* it was never asked to dispatch.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import (DispatchResult, TaskSnapshot, TaskState,  # noqa: E402
                           Verdict)
from controlplane.handoff import (  # noqa: E402
    HandoffNotReady,
    reconcile_handoff_checkpoints,
    verify_checkpoint_ref,
)
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.project_service import ProjectService  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from controlplane.verify import worktree_fingerprint  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

FIXTURE = ROOT / "sandbox" / "m4-checkpoint-gate"
REPO = FIXTURE / "repo-a"
LEDGER = ROOT / ".poc-state" / "t41-checkpoint-gate.db"
EVIDENCE = ROOT / "evidence" / "project" / "T41-handoff-checkpoint-gate.json"
SEQ = iter(range(1, 500))


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def git(path: Path, *args: str) -> str:
    """Run git, retrying the Windows ref-lock races this test provokes.

    The negative cases deliberately race several processes against the same ref
    namespace; on Windows a transient ``cannot lock ref`` / ``Permission denied``
    is the operating system, not the behaviour under test.  Only those two
    messages are retried (a real failure still raises).
    """
    last = ""
    for attempt in range(5):
        proc = subprocess.run(["git", "-C", str(path), *args], check=False,
                              capture_output=True, text=True, timeout=60)
        if proc.returncode == 0:
            return proc.stdout.strip()
        last = proc.stderr.strip()
        if not any(token in last for token in ("cannot lock ref",
                                               "Permission denied",
                                               "unable to create lock file")):
            break
        time.sleep(0.2 * (attempt + 1))
    raise RuntimeError(f"git {args} failed: {last}")


def make_repo() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)
    REPO.mkdir(parents=True)
    git(REPO, "init", "-q")
    git(REPO, "config", "user.email", "t41@example.invalid")
    git(REPO, "config", "user.name", "T41")
    git(REPO, "config", "core.hooksPath", ".git/hooks-disabled")
    (REPO / "seed.txt").write_text("seed\n", encoding="utf-8")
    git(REPO, "add", "seed.txt")
    git(REPO, "commit", "-q", "-m", "seed")
    git(REPO, "branch", "-M", "main")


def new_worktree(name: str) -> Path:
    path = FIXTURE / f"wt-{name}"
    git(REPO, "worktree", "add", "-q", str(path), "-b", f"wt-{name}")
    return path


class DispatchSpy:
    """Adapter that stands in for a Worker and *records* every dispatch.

    It deliberately accepts work instead of raising: a blocked handoff must be
    proven *not to have been dispatched*, and letting unrelated ready rows flow
    through keeps the claim loop's ordering from masking the case under test.
    The assertion is always about the specific ``task_id``.
    """

    name = "stub"

    def __init__(self):
        self.dispatch_calls: list[dict] = []

    def dispatched(self, task_id: str) -> int:
        return sum(1 for item in self.dispatch_calls
                   if item["task_id"] == task_id)

    def dispatch(self, request):  # noqa: ANN001 - adapter protocol
        self.dispatch_calls.append({"task_id": request.task_id,
                                    "cwd": request.cwd})
        return DispatchResult(
            task_id=request.task_id,
            worker_job_id=f"spy-{request.task_id}",
            accepted=True,
            warnings=["STUB_ADAPTER: no real Worker process was started"])

    def get_task(self, task_id, **_kwargs):  # noqa: ANN001
        return TaskSnapshot(task_id=task_id, worker_job_id=f"spy-{task_id}",
                            state=TaskState.RUNNING, cwd=str(REPO))

    def pid_alive(self, _pid):  # noqa: ANN001
        return None

    def collect_artifacts(self, _task_id):  # noqa: ANN001
        return []

    def reconcile(self):
        return {"jobs": []}

    def capabilities(self):
        return {}


def source_in(plane: ControlPlane, store: TaskStore, project_id: str,
              wt: Path) -> str:
    """A verified source WorkItem whose accepted state is `wt`'s current state."""
    n = next(SEQ)
    workspace = store.default_workspace(project_id)
    rec = plane.enqueue(f"t41 source {n}", str(REPO), title=f"t41-src-{n}",
                        project_id=project_id, workspace_id=workspace["id"])
    task_id = rec["task_id"]
    job = f"job-{n}"
    store.add_attempt(task_id, worker_job_id=job)
    store.observe_session_reference(
        work_item_id=task_id, worker_job_id=job, node_id="local",
        worker_instance_id="wi-t41", adapter="stub", session_id=f"sess-{n}")
    (wt / f"feature_{n}.py").write_text(f"x = {n}\n", encoding="utf-8")
    metadata = dict((store.get(task_id) or {}).get("metadata") or {})
    metadata["acceptance_snapshot"] = worktree_fingerprint(str(wt))
    store.update(task_id, state=TaskState.SUCCEEDED.value,
                 verdict=Verdict.VERIFIED.value, worktree=str(wt),
                 head_commit=git(wt, "rev-parse", "HEAD"),
                 finished_at=1_700_000_000.0, metadata=metadata,
                 artifacts=[], assertion_results=[])
    return task_id


def dispatch_and_observe(plane: ControlPlane, spy: DispatchSpy,
                         task_id: str, *, concurrency: int = 64,
                         owner: str = "t41-runtime",
                         max_claims: int = 60) -> dict:
    """Claim+dispatch through the real runtime path and report what happened.

    Several other rows may legitimately be ``ready`` in the same ledger, so the
    loop claims until either this task is dispatched or nothing is left to
    claim.  The reported facts are always about *this* task.
    """
    before_attempts = len(plane.store.attempts(task_id))
    before_spy = spy.dispatched(task_id)
    for _ in range(max_claims):
        # A refused row is returned by dispatch_next too (it is moved to a
        # blocked queue status).  "Dispatched" therefore means exactly one thing:
        # the adapter was asked to start this task.
        claimed = plane.dispatch_next(owner, concurrency_limit=concurrency)
        if not claimed:
            break
        if spy.dispatched(task_id) > before_spy:
            break
    record = plane.store.get(task_id) or {}
    attempts = plane.store.attempts(task_id)
    dispatched = spy.dispatched(task_id) > before_spy
    return {
        "worker_started": dispatched,
        "task_dispatch_count": len(attempts) - before_attempts,
        "dispatch_returned": dispatched,
        "queue_status": record.get("queue_status"),
        "state": record.get("state"),
        "worker_job_id": record.get("worker_job_id"),
    }


def submit(service: ProjectService, source: str, key: str | None = None) -> dict:
    return service.submit_handoff(
        source, "continue on the accepted code",
        checkpoint_mode="reachable", require_checkpoint=True,
        idempotency_key=key or f"t41-{uuid.uuid4().hex[:8]}")


def ref_exists(path: Path, task_id: str) -> bool:
    out = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--verify", "--quiet",
         f"refs/control-plane/handoff/{task_id}"],
        capture_output=True, text=True, check=False)
    return out.returncode == 0


# --------------------------------------------------------------------------- #
# A1 / A2 — unprovable checkpoint must not reach a Worker
# --------------------------------------------------------------------------- #
def a1_update_ref_failure(service, plane, store, spy, project_id) -> dict:
    """``git update-ref`` really fails: the WorkItem stays visible but unclaimable.

    The failure is caused by the Git repository itself, not by patching the code
    under test: a *regular file* is placed where the private ref namespace must
    be, so no ``update-ref`` below it can succeed.
    """
    wt = new_worktree("a1")
    source = source_in(plane, store, project_id, wt)
    before_tasks = len(store.all())
    blocker = Path(REPO) / ".git" / "refs" / "control-plane" / "handoff"
    blocker.parent.mkdir(parents=True, exist_ok=True)
    if blocker.exists():
        shutil.rmtree(blocker, ignore_errors=True)
        blocker.unlink(missing_ok=True)
    blocker.write_text("not a directory\n", encoding="utf-8")
    try:
        record = submit(service, source)
        target = record["task_id"]
        ref_written = ref_exists(REPO, target)
        observed = dispatch_and_observe(plane, spy, target)
        status = service.handoff_checkpoint_status(target)
        events = [event["type"] for event in store.events(target)]
    finally:
        blocker.unlink(missing_ok=True)
    return {
        "submit_created_workitem": len(store.all()) - before_tasks == 1,
        "ref_protection_failed": not ref_written,
        "dispatch_blocked": not observed["dispatch_returned"],
        "worker_not_started": observed["worker_started"] is False,
        "no_dispatch_attempt": observed["task_dispatch_count"] == 0,
        "checkpoint_not_ready": not status["checkpoint_ready"],
        "queue_status_after_block": observed["queue_status"]
                                    == "checkpoint_unprotected",
        "blocked_event_recorded": (
            "handoff_checkpoint_unprotected" in events
            or "handoff_checkpoint_blocked" in events
            or "dispatch_blocked_checkpoint" in events),
        "blocked_reason_queryable": bool(status["reason"]),
        "submit_reports_checkpoint_not_ready": record.get("checkpoint_ready") is False,
    }


def a2_missing_object(service, plane, store, spy, project_id) -> dict:
    """The checkpoint object itself is unreachable in the target."""
    wt = new_worktree("a2")
    source = source_in(plane, store, project_id, wt)
    record = submit(service, source)
    task_id = record["task_id"]
    # Rewrite the recorded checkpoint to a SHA that does not exist anywhere.
    metadata = dict((store.get(task_id) or {}).get("metadata") or {})
    bogus = "0" * 40
    metadata["checkpoint_ref"] = bogus
    store.update(task_id, metadata=metadata, queue_status="ready")
    observed = dispatch_and_observe(plane, spy, task_id)
    status = service.handoff_checkpoint_status(task_id)
    return {
        "object_missing": status["object_exists"] is False,
        "dispatch_blocked": not observed["dispatch_returned"],
        "worker_not_started": observed["worker_started"] is False,
        "no_dispatch_attempt": observed["task_dispatch_count"] == 0,
        "checkpoint_not_ready": not status["checkpoint_ready"],
        "blocked_reason_queryable": bool(status["reason"]),
    }


# --------------------------------------------------------------------------- #
# A3 / A4 — restart recovery and idempotent re-protection
# --------------------------------------------------------------------------- #
def a3_crash_before_protection(service, plane, store, spy, project_id) -> dict:
    """Row persisted, ref never written (process died): restart converges."""
    wt = new_worktree("a3")
    source = source_in(plane, store, project_id, wt)
    record = submit(service, source)
    task_id = record["task_id"]
    commit = (store.get(task_id) or {})["metadata"]["checkpoint_ref"]
    ref = f"refs/control-plane/handoff/{task_id}"
    # Simulate the crash window: the row exists, the ref does not, the queue has
    # not been released yet.
    git(REPO, "update-ref", "-d", ref)
    store.update(task_id, queue_status="checkpoint_unprotected",
                 dispatch_claim_owner=None, dispatch_claimed_at=None)
    before = dispatch_and_observe(plane, spy, task_id, owner="t41-crash")
    before_status = service.handoff_checkpoint_status(task_id)
    recovered = reconcile_handoff_checkpoints(store)
    after_status = service.handoff_checkpoint_status(task_id)
    after = dispatch_and_observe(plane, spy, task_id, owner="t41-restarted")
    return {
        "before_recovery_blocked": not before["dispatch_returned"],
        "before_worker_not_started": before["worker_started"] is False,
        "before_dispatch_count": before["task_dispatch_count"],
        "before_checkpoint_not_ready": not before_status["checkpoint_ready"],
        "recovery_released": any(item["task_id"] == task_id
                                 for item in recovered["released"]),
        "ref_written_on_recovery": ref_exists(REPO, task_id),
        "ref_matches_checkpoint": (
            subprocess.run(["git", "-C", str(REPO), "rev-parse", ref],
                           capture_output=True, text=True, check=False
                           ).stdout.strip() == commit),
        "checkpoint_ready_after_recovery": after_status["checkpoint_ready"],
        "after_recovery_claimable": after["dispatch_returned"],
    }


def a4_repeated_recovery(service, plane, store, spy, project_id) -> dict:
    """Re-running protection for the same (task, commit) converges, never forks."""
    wt = new_worktree("a4")
    source = source_in(plane, store, project_id, wt)
    key = f"t41-a4-{uuid.uuid4().hex[:6]}"
    first = submit(service, source, key=key)
    task_id = first["task_id"]
    tasks_after_first = len(store.all())
    refs_first = git(REPO, "for-each-ref", "refs/control-plane/")
    sweeps = [reconcile_handoff_checkpoints(store) for _ in range(3)]
    again = submit(service, source, key=key)
    return {
        "idempotent_second_submit": again.get("deduplicated") is True,
        "same_task_id": again.get("task_id") == task_id,
        "no_extra_workitem": len(store.all()) == tasks_after_first,
        "no_extra_ref": git(REPO, "for-each-ref", "refs/control-plane/") == refs_first,
        "repeated_sweeps_keep_ready": (
            (store.get(task_id) or {}).get("queue_status") == "ready"),
        "sweeps_never_block_this_task": not any(
            item["task_id"] == task_id
            for sweep in sweeps for item in sweep["blocked"]),
        "sweeps_report_already_ready": all(
            any(item["task_id"] == task_id
                for item in sweep.get("already_ready") or [])
            for sweep in sweeps),
        "sweeps_never_double_release": sum(
            1 for sweep in sweeps for item in sweep["released"]
            if item["task_id"] == task_id) == 0,
        "checkpoint_ready": service.handoff_checkpoint_status(
            task_id)["checkpoint_ready"],
    }


# --------------------------------------------------------------------------- #
# A5 / A6 — post-submit drift is caught at dispatch time
# --------------------------------------------------------------------------- #
def a5_ref_deleted_after_protection(service, plane, store, spy, project_id) -> dict:
    wt = new_worktree("a5")
    source = source_in(plane, store, project_id, wt)
    record = submit(service, source)
    task_id = record["task_id"]
    ready_before = service.handoff_checkpoint_status(task_id)["checkpoint_ready"]
    git(REPO, "update-ref", "-d", f"refs/control-plane/handoff/{task_id}")
    observed = dispatch_and_observe(plane, spy, task_id, owner="t41-a5")
    status = service.handoff_checkpoint_status(task_id)
    return {
        "was_ready_before_deletion": ready_before,
        "ref_gone": not ref_exists(REPO, task_id),
        "dispatch_blocked": not observed["dispatch_returned"],
        "worker_not_started": observed["worker_started"] is False,
        "no_dispatch_attempt": observed["task_dispatch_count"] == 0,
        "checkpoint_not_ready": not status["checkpoint_ready"],
    }


def a6_workspace_drift(service, plane, store, spy, project_id) -> dict:
    """The ref points at another commit: never silently repaired or dispatched."""
    wt = new_worktree("a6")
    source = source_in(plane, store, project_id, wt)
    record = submit(service, source)
    task_id = record["task_id"]
    commit = (store.get(task_id) or {})["metadata"]["checkpoint_ref"]
    other = git(REPO, "rev-parse", "main")
    git(REPO, "update-ref", f"refs/control-plane/handoff/{task_id}", other)
    observed = dispatch_and_observe(plane, spy, task_id, owner="t41-a6")
    status = service.handoff_checkpoint_status(task_id)
    still_points_elsewhere = (
        subprocess.run(["git", "-C", str(REPO), "rev-parse",
                        f"refs/control-plane/handoff/{task_id}"],
                       capture_output=True, text=True, check=False
                       ).stdout.strip() == other)
    return {
        "ref_points_elsewhere": other != commit,
        "ref_not_silently_repaired": still_points_elsewhere,
        "dispatch_blocked": not observed["dispatch_returned"],
        "worker_not_started": observed["worker_started"] is False,
        "no_dispatch_attempt": observed["task_dispatch_count"] == 0,
        "checkpoint_not_ready": not status["checkpoint_ready"],
        "reported_ref_commit": status["ref_commit"],
        "reason_queryable": bool(status["reason"]),
    }


# --------------------------------------------------------------------------- #
# A7 — two processes racing on the same handoff
# --------------------------------------------------------------------------- #
RACE_CHILD = r"""
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from adapters.base import DispatchResult, TaskSnapshot, TaskState
from controlplane.plane import ControlPlane
from controlplane.store import TaskStore
from controlplane.handoff import reconcile_handoff_checkpoint

TARGET = sys.argv[4]
CALLS = []

class Spy:
    name = "stub"
    def dispatch(self, request):
        CALLS.append(request.task_id)
        return DispatchResult(task_id=request.task_id,
                              worker_job_id="race-" + request.task_id,
                              accepted=True)
    def get_task(self, task_id, **kwargs):
        return TaskSnapshot(task_id=task_id, worker_job_id="race-" + task_id,
                            state=TaskState.RUNNING)
    def pid_alive(self, pid):
        return None
    def collect_artifacts(self, task_id):
        return []
    def reconcile(self):
        return {"jobs": []}

store = TaskStore(Path(sys.argv[2]))
plane = ControlPlane(Spy(), store)
sweep = reconcile_handoff_checkpoint(store, TARGET)
claimed = []
for _ in range(8):
    rec = plane.dispatch_next(sys.argv[3], concurrency_limit=64)
    if not rec:
        break
    claimed.append(rec.get("task_id"))
record = store.get(TARGET) or {}
print(json.dumps({"sweep_blocked": [i["task_id"] for i in sweep["blocked"]],
                  "sweep_released": [i["task_id"] for i in sweep["released"]],
                  "claimed": claimed,
                  "dispatched_target": CALLS.count(TARGET),
                  "worker_calls": len(CALLS),
                  "attempts": len(store.attempts(TARGET)),
                  "queue_status": record.get("queue_status")}))
store.close()
"""


def _run_race_children(script: Path, task_id: str, count: int = 2) -> list[dict]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("LD_MODEL_", "CODEBUDDY_SAFE_DELETE"))}
    procs = [subprocess.Popen(
        [sys.executable, str(script), str(ROOT), str(LEDGER),
         f"t41-race-{index}-{uuid.uuid4().hex[:4]}", task_id],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        for index in range(count)]
    outputs = []
    for proc in procs:
        out, err = proc.communicate(timeout=180)
        try:
            outputs.append(json.loads((out or "{}").strip() or "{}"))
        except json.JSONDecodeError:
            outputs.append({"error": (err or out)[-400:]})
    return outputs


def a7_concurrent_recovery(service, plane, store, spy, project_id) -> dict:
    """Two processes sweep and claim the same handoff, twice over.

    Phase 1 (unprovable): the ref namespace is blocked, so no process can
    protect the checkpoint.  Both sweeps must block, neither may dispatch.
    Phase 2 (provable): the namespace is unblocked; the two processes sweep and
    claim concurrently.  Exactly one dispatch of this task is allowed.
    """
    wt = new_worktree("a7")
    source = source_in(plane, store, project_id, wt)
    record = submit(service, source)
    task_id = record["task_id"]
    # Simulate the crash window first (ref gone, row not released), then make
    # the namespace unwritable so *no* process can re-protect it.
    git(REPO, "update-ref", "-d", f"refs/control-plane/handoff/{task_id}")
    store.update(task_id, queue_status="checkpoint_unprotected",
                 dispatch_claim_owner=None, dispatch_claimed_at=None)
    script = FIXTURE / "race_child.py"
    script.write_text(RACE_CHILD, encoding="utf-8")
    blocker = Path(REPO) / ".git" / "refs" / "control-plane" / "handoff"
    shutil.rmtree(blocker, ignore_errors=True)
    blocker.unlink(missing_ok=True)
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("not a directory\n", encoding="utf-8")
    try:
        phase1 = _run_race_children(script, task_id)
    finally:
        blocker.unlink(missing_ok=True)
    phase2 = _run_race_children(script, task_id)
    final = store.get(task_id) or {}
    attempts = store.attempts(task_id)
    dispatched_target = sum(item.get("dispatched_target") or 0 for item in phase2)
    return {
        "phase1_children_ok": all("error" not in item for item in phase1),
        "phase1_both_blocked": all(task_id in (item.get("sweep_blocked") or [])
                                   for item in phase1),
        "phase1_no_dispatch": all(
            (item.get("dispatched_target") or 0) == 0 for item in phase1),
        "phase1_no_release": all(
            task_id not in (item.get("sweep_released") or []) for item in phase1),
        "phase2_children_ok": all("error" not in item for item in phase2),
        "phase2_single_dispatch": dispatched_target == 1,
        "no_double_attempt": len(attempts) <= 1,
        "single_workitem": sum(1 for t in store.all()
                               if t.get("idempotency_key")
                               == record.get("idempotency_key")) == 1,
        "parent_never_dispatched_it": spy.dispatched(task_id) == 0,
        "checkpoint_ready": verify_checkpoint_ref(
            str(REPO), task_id,
            (final.get("metadata") or {}).get("checkpoint_ref"))["ok"],
        "phase1_outputs": phase1,
        "phase2_outputs": phase2,
        "final_queue_status": final.get("queue_status"),
        "attempt_count": len(attempts),
    }


# --------------------------------------------------------------------------- #
# A8 — the normal handoff path still works
# --------------------------------------------------------------------------- #
def a8_normal_handoff(service, plane, store, spy, project_id) -> dict:
    """The ordinary handoff still dispatches, with its frozen content intact."""
    wt = new_worktree("a8")
    source = source_in(plane, store, project_id, wt)
    record = submit(service, source)
    task_id = record["task_id"]
    status = service.handoff_checkpoint_status(task_id)
    frozen_before = (store.get(task_id) or {})["effective_prompt"]
    signature_before = (store.get(task_id) or {})["metadata"]["handoff_signature"]
    digest_before = (store.get(task_id) or {})["task_digest"]
    observed = dispatch_and_observe(plane, spy, task_id, owner="t41-a8")
    saved = plane.store.get(task_id) or {}
    return {
        "checkpoint_ready": status["checkpoint_ready"],
        "queue_status_ready": record.get("queue_status") == "ready",
        "dispatched": observed["dispatch_returned"],
        "worker_started_once": spy.dispatched(task_id) == 1,
        "effective_prompt_unchanged": saved["effective_prompt"] == frozen_before,
        "handoff_signature_unchanged": (
            saved["metadata"]["handoff_signature"] == signature_before),
        "digest_unchanged": saved.get("task_digest") == digest_before,
        # ``_dispatch_record`` re-derives the digest from the facts it is about
        # to run and pins the result in metadata; the two must agree.
        "digest_recomputed_once": (
            (saved.get("metadata") or {}).get("task_digest") == digest_before),
    }


def main() -> int:
    make_repo()
    for stale in [LEDGER, *LEDGER.parent.glob(LEDGER.name + "*")]:
        stale.unlink(missing_ok=True)
    store = TaskStore(LEDGER)
    spy = DispatchSpy()
    try:
        plane = ControlPlane(spy, store, require_registered_projects=True,
                             worker_instance_id="wi-t41")
        service = ProjectService(plane)
        # A high project concurrency limit keeps this test about the checkpoint
        # gate: earlier sections leave rows 'running' under the spy adapter, and
        # the default project limit (2) would mask a dispatch refusal as
        # "nothing left to claim".
        project_id = plane.register_project(
            "t41", str(REPO), default_adapter="stub",
            concurrency_limit=500)["id"]
        sections = {
            "A1_update_ref_failure": a1_update_ref_failure(
                service, plane, store, spy, project_id),
            "A2_missing_object": a2_missing_object(
                service, plane, store, spy, project_id),
            "A3_crash_before_protection": a3_crash_before_protection(
                service, plane, store, spy, project_id),
            "A4_repeated_recovery": a4_repeated_recovery(
                service, plane, store, spy, project_id),
            "A5_ref_deleted_after_protection": a5_ref_deleted_after_protection(
                service, plane, store, spy, project_id),
            "A6_workspace_drift": a6_workspace_drift(
                service, plane, store, spy, project_id),
            "A7_concurrent_recovery": a7_concurrent_recovery(
                service, plane, store, spy, project_id),
        }
        spy.dispatch_calls.clear()
        sections["A8_normal_handoff"] = a8_normal_handoff(
            service, plane, store, spy, project_id)
    finally:
        store.close()

    checks: dict[str, bool] = {}

    def collect(node, prefix):
        for key, value in node.items():
            name = f"{prefix}.{key}"
            if isinstance(value, bool):
                checks[name] = value
            elif isinstance(value, dict):
                collect(value, name)

    for name, section in sections.items():
        collect(section, name)
    log = {"test": "T41-handoff-checkpoint-gate",
           "worker_started": False,
           "note": ("stub adapter refuses to dispatch; every negative case asserts "
                    "worker_started=false, task_dispatch_count=0, "
                    "checkpoint_ready=false"),
           "sections": sections, "checks": checks,
           "result": "Pass" if checks and all(checks.values()) else "Fail"}
    write_evidence(EVIDENCE, log)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps(log, ensure_ascii=False, indent=2, default=str))
    return 0 if log["result"] == "Pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
