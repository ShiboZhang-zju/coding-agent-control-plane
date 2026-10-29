"""T26: reviewer round 3 — the two P0s and the P1 residue fixes.

  A (P0)  corrupted ledger -> degraded -> process exit -> restart: the dispatch
          ban SURVIVES the restart (persisted marker), and only an explicit,
          audited `recover_degraded()` lifts it.
  B (P0)  a job missing from two consecutive full scans while its pid is still
          alive must NOT become `lost`: it is `suspected_lost`, keeps its state
          and its concurrency slot. Death is only concluded from independent
          evidence (pid confirmed dead).
  B2 (P0) an adapter-reported `lost` with no pid / no probe stays unconfirmed:
          state kept, capacity retained.
  C (P1)  `recover_inflight` no longer steals a dispatch claim that another
          runtime is actively using; only expired claims are recovered.
  D (P1)  attempts that never reached a Worker get explicit terminal states
          (`blocked`, `aborted_before_dispatch`) instead of dangling history.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import (DispatchPolicy, DispatchResult, TaskSnapshot,  # noqa: E402
                           TaskState)
from controlplane.plane import (ApprovalRequired, ControlPlane,  # noqa: E402
                                DISPATCH_CLAIM_LEASE_S)
from controlplane.policies import agent_worktree  # noqa: E402
from controlplane.store import LedgerDegraded, TaskStore  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

CWD = str(ROOT / "sandbox" / "repo-a-t11")
LOW = agent_worktree()
HIGH = DispatchPolicy(mode="shell", isolation="none", allow_unisolated_shell=True)
ALIVE_PID = 4321


class ProbeAdapter:
    """Stub whose liveness answers and scan visibility are scripted per case."""

    name = "probe"

    def __init__(self, cwd: str):
        self.cwd = cwd
        self.jobs: dict[str, TaskState] = {}
        self.report_lost: set[str] = set()
        self.visible: set[str] | None = None        # None = report everything
        self.alive: dict[int, bool] = {}
        self.calls = 0

    # -- worker surface -------------------------------------------------- #
    def dispatch(self, request) -> DispatchResult:
        self.calls += 1
        job = f"job-{self.calls}"
        self.jobs[job] = TaskState.RUNNING
        return DispatchResult(task_id=request.task_id, worker_job_id=job,
                              accepted=True, raw={"dispatch_http": 200})

    def get_task(self, job_id: str, timeout_s: float = 20.0) -> TaskSnapshot:
        state = TaskState.LOST if job_id in self.report_lost else \
            self.jobs.get(job_id, TaskState.UNKNOWN)
        return TaskSnapshot(task_id=job_id, worker_job_id=job_id, state=state,
                            cwd=self.cwd, detail="probe-stub")

    def collect_artifacts(self, _task_id):
        return []

    def reconcile(self) -> dict:
        visible = self.visible if self.visible is not None else set(self.jobs)
        return {"source": "stub", "total": len(visible), "new": [], "changed": [],
                "lost": [], "jobs": [{"id": j, "state": "done"} for j in visible]}

    # -- independent liveness evidence ------------------------------------ #
    def pid_alive(self, pid: int | None):
        if pid is None:
            return None
        return self.alive.get(pid)


def corrupt_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"NOT-A-SQLITE-HEADER" + b"\x00" * 512)


def case_a_restart_persistence() -> dict:
    db = ROOT / ".poc-state" / "t26-a.db"
    for stale in [db, *db.parent.glob("t26-a.db.corrupt-*"),
                  *db.parent.glob("t26-a.db.DEGRADED")]:
        stale.unlink(missing_ok=True)
    corrupt_file(db)

    first = TaskStore(db)
    first_degraded = first.degraded
    first.close()

    # "restart": the truncated file now passes integrity_check, but the marker
    # written by the first process must keep the ban in force.
    second = TaskStore(db)
    second_degraded = second.degraded
    adapter = ProbeAdapter(CWD)
    plane = ControlPlane(adapter, second, policy=LOW)
    try:
        plane.dispatch("echo revived", CWD, idempotency_key="t26-a")
        dispatch_after_restart = "ALLOWED (bad)"
    except LedgerDegraded:
        dispatch_after_restart = "refused"
    except Exception as exc:  # noqa: BLE001
        dispatch_after_restart = f"{type(exc).__name__} (bad)"
    worker_calls_before_recovery = adapter.calls

    # recovery is manual and audited: no note -> refused
    try:
        second.recover_degraded("")
        empty_note = "ALLOWED (bad)"
    except ValueError:
        empty_note = "refused"
    recovered = second.recover_degraded("operator reviewed the quarantine copy")
    post_recovery = ControlPlane(adapter, second, policy=LOW)
    try:
        rec = post_recovery.dispatch("echo revived", CWD, idempotency_key="t26-a-2")
        dispatch_after_recovery = "allowed"
        job = rec.get("worker_job_id")
    except Exception as exc:  # noqa: BLE001
        dispatch_after_recovery, job = f"{type(exc).__name__} (bad)", None
    second.close()

    # a fresh process over the recovered ledger must not be degraded either
    third = TaskStore(db)
    marker_gone = not (ROOT / ".poc-state" / "t26-a.db.DEGRADED").exists()
    third_clean = not third.degraded
    third.close()

    return {
        "degraded_on_corruption": first_degraded,
        "degraded_after_restart": second_degraded,
        "dispatch_after_restart": dispatch_after_restart,
        "worker_calls_before_recovery": worker_calls_before_recovery,
        "recovery_without_note": empty_note,
        "recovery_with_note": recovered,
        "dispatch_after_recovery": dispatch_after_recovery,
        "worker_job_after_recovery": job,
        "marker_deleted": marker_gone,
        "fresh_process_not_degraded": third_clean,
        "pass": (first_degraded and second_degraded
                 and dispatch_after_restart == "refused"
                 and worker_calls_before_recovery == 0
                 and empty_note == "refused"
                 and recovered.get("recovered") is True
                 and dispatch_after_recovery == "allowed"
                 and marker_gone and third_clean),
    }


def case_b_scan_miss_with_live_pid() -> dict:
    db = ROOT / ".poc-state" / "t26-b.db"
    store = TaskStore(db)
    store.clear()
    adapter = ProbeAdapter(CWD)
    plane = ControlPlane(adapter, store, policy=LOW)

    live = plane.dispatch("echo live", CWD, idempotency_key="t26-b-live",
                          title="pid stays alive")
    live_id, job_live = live["task_id"], live["worker_job_id"]
    store.update(live_id, pid=ALIVE_PID)
    adapter.alive[ALIVE_PID] = True

    adapter.visible = set()          # the worker stops listing both jobs
    plane.discover_orphan_jobs()     # miss 1
    plane.discover_orphan_jobs()     # miss 2: enough misses, but pid is ALIVE
    after_two = store.get(live_id)
    active_with_live_pid = store.active_count(adapter.name)

    adapter.alive[ALIVE_PID] = False  # independent evidence of death arrives
    third = plane.discover_orphan_jobs()
    after_three = store.get(live_id)
    active_after_death = store.active_count(adapter.name)

    # B2: adapter itself reports lost, but there is no pid to probe
    unconfirmed = plane.dispatch("echo maybe", CWD, idempotency_key="t26-b-maybe",
                                 title="lost without pid")
    unconfirmed_id, job_maybe = unconfirmed["task_id"], unconfirmed["worker_job_id"]
    adapter.visible = {job_live, job_maybe}
    adapter.report_lost = {job_maybe}
    plane.refresh_known_jobs()
    after_report = store.get(unconfirmed_id)
    store.close()

    return {
        "two_misses_with_live_pid": {
            "state": after_two["state"], "verdict": after_two["verdict"],
            "queue_status": after_two["queue_status"],
            "suspected_lost": (after_two.get("metadata") or {}).get("suspected_lost"),
            "capacity_retained": active_with_live_pid == 1,
        },
        "third_scan_after_death": {
            "action": [v["action"] for v in third["vanished"] if v["task_id"] == live_id],
            "state": after_three["state"], "verdict": after_three["verdict"],
            "queue_status": after_three["queue_status"],
            "capacity_released": active_after_death == 0,
        },
        "adapter_lost_without_pid": {
            "state": after_report["state"],
            "warning": any("LOST_UNCONFIRMED" in w for w in after_report.get("warnings") or []),
            "suspected_lost": (after_report.get("metadata") or {}).get("suspected_lost"),
        },
        "pass": (after_two["state"] == TaskState.RUNNING.value
                 and after_two["queue_status"] == "running"
                 and (after_two.get("metadata") or {}).get("suspected_lost") is True
                 and active_with_live_pid == 1
                 and after_three["state"] == TaskState.LOST.value
                 and after_three["verdict"] == "rejected"
                 and after_three["queue_status"] == "complete"
                 and active_after_death == 0
                 and after_report["state"] != TaskState.LOST.value
                 and any("LOST_UNCONFIRMED" in w
                         for w in after_report.get("warnings") or [])),
    }


def case_c_claim_lease() -> dict:
    db = ROOT / ".poc-state" / "t26-c.db"
    store = TaskStore(db)
    store.clear()
    adapter = ProbeAdapter(CWD)
    plane = ControlPlane(adapter, store, policy=LOW)
    now = time.time()

    fresh_id = store.new_task_id()
    store.create({"task_id": fresh_id, "title": "claimed seconds ago", "prompt": "p",
                  "cwd": CWD, "mode": "shell", "isolation": "worktree",
                  "idempotency_key": "t26-c-fresh", "state": TaskState.QUEUED.value,
                  "verdict": "pending", "adapter": adapter.name,
                  "queue_status": "dispatching",
                  "dispatch_claim_owner": "runtime-other",
                  "dispatch_claimed_at": now - 5})
    stale_id = store.new_task_id()
    store.create({"task_id": stale_id, "title": "claim long expired", "prompt": "p",
                  "cwd": CWD, "mode": "shell", "isolation": "worktree",
                  "idempotency_key": "t26-c-stale", "state": TaskState.QUEUED.value,
                  "verdict": "pending", "adapter": adapter.name,
                  "queue_status": "dispatching",
                  "dispatch_claim_owner": "runtime-dead",
                  "dispatch_claimed_at": now - (DISPATCH_CLAIM_LEASE_S + 120)})

    result = plane.recover_inflight()
    fresh_after = store.get(fresh_id)
    stale_after = store.get(stale_id)
    store.close()

    return {
        "recovered": result["recovered"],
        "skipped_active_claims": result.get("skipped_active_claims"),
        "fresh_claim_state": fresh_after["state"],
        "fresh_claim_owner_kept": fresh_after.get("dispatch_claim_owner"),
        "stale_claim_state": stale_after["state"],
        "stale_claim_owner_cleared": stale_after.get("dispatch_claim_owner") is None,
        "pass": (result["recovered"] == [stale_id]
                 and len(result.get("skipped_active_claims") or []) == 1
                 and fresh_after["state"] == TaskState.QUEUED.value
                 and fresh_after.get("dispatch_claim_owner") == "runtime-other"
                 and stale_after["state"] == TaskState.DISPATCH_UNKNOWN.value
                 and stale_after.get("dispatch_claim_owner") is None),
    }


class RejectAdapter(ProbeAdapter):
    """Worker that definitively refuses the POST (no job created)."""

    def dispatch(self, request) -> DispatchResult:
        self.calls += 1
        return DispatchResult(task_id=request.task_id, worker_job_id=None,
                              accepted=False, raw={"dispatch_http": 400})


def case_d_attempt_terminal_states() -> dict:
    db = ROOT / ".poc-state" / "t26-d.db"
    store = TaskStore(db)
    store.clear()
    adapter = ProbeAdapter(CWD)
    plane = ControlPlane(adapter, store, policy=HIGH)
    prompt = "echo t26 > t26.txt"

    req = plane.request_approval_for(prompt, CWD)
    plane.approve(req["digest"], note="human ok")

    # Two concurrent dispatches race for the single approval: the loser's
    # pre-check sees the approval as unconsumed, but the atomic reservation
    # fails, so its attempt must end as `blocked`, never as execution history.
    outcomes: list[dict] = []
    barrier = threading.Barrier(2)

    def race(key: str) -> None:
        try:
            barrier.wait(timeout=10)
            rec = plane.dispatch(prompt, CWD, idempotency_key=key)
            outcomes.append({"key": key, "ok": bool(rec.get("worker_job_id"))})
        except ApprovalRequired:
            outcomes.append({"key": key, "ok": False, "refused": "approval_taken"})
        except Exception as exc:  # noqa: BLE001
            outcomes.append({"key": key, "ok": False, "error": type(exc).__name__})

    threads = [threading.Thread(target=race, args=(f"t26-d-{i}",)) for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    winner_key = next((o["key"] for o in outcomes if o.get("ok")), None)
    loser_key = next((o["key"] for o in outcomes if not o.get("ok")), None)
    winner = store.by_idempotency_key(winner_key)
    loser = store.by_idempotency_key(loser_key)

    # a dispatch that is definitively rejected by the worker (no job created)
    rejecting_plane = ControlPlane(RejectAdapter(CWD), store, policy=LOW)
    rejected = rejecting_plane.dispatch("echo no", CWD, idempotency_key="t26-d-3")

    def attempt_state(task_id: str) -> dict:
        rows = store.attempts(task_id)
        return {"state": rows[-1]["state"] if rows else None,
                "ended": bool(rows[-1]["ended_at"]) if rows else None}

    winner_attempt = attempt_state(winner["task_id"])
    loser_attempt = attempt_state(loser["task_id"])
    rejected_attempt = attempt_state(rejected["task_id"])
    store.close()

    return {
        "race_outcomes": outcomes,
        "winner_attempt": winner_attempt,
        "loser_attempt": loser_attempt,
        "rejected_attempt": rejected_attempt,
        "pass": (len([o for o in outcomes if o.get("ok")]) == 1
                 and adapter.calls == 1
                 and winner_attempt == {"state": "dispatched", "ended": False}
                 and loser_attempt["state"] == "blocked"
                 and loser_attempt["ended"] is True
                 and rejected_attempt["state"] == "aborted_before_dispatch"
                 and rejected_attempt["ended"] is True),
    }


def main() -> int:
    log = {
        "test": "T26-p0-fixes",
        "A_degraded_survives_restart": case_a_restart_persistence(),
        "B_scan_miss_live_pid": case_b_scan_miss_with_live_pid(),
        "C_claim_lease": case_c_claim_lease(),
        "D_attempt_terminal_states": case_d_attempt_terminal_states(),
    }
    keys = ("A_degraded_survives_restart", "B_scan_miss_live_pid",
            "C_claim_lease", "D_attempt_terminal_states")
    log["pass"] = {k: log[k]["pass"] for k in keys}
    log["result"] = "Pass" if all(log["pass"].values()) else "Fail"
    write_evidence(ROOT / "evidence" / "recovery" / "T26-p0-fixes.json", log)
    print(json.dumps(log, ensure_ascii=False, indent=1)[:3000])
    print("RESULT:", log["result"])
    return 0 if log["result"] == "Pass" else 1


if __name__ == "__main__":
    sys.exit(main())
