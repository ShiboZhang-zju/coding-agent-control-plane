"""T21: approval security — risk cannot be lowered, reservation is atomic.

Cases
  1 risk="low" cannot launder an un-isolated shell job
  2 8 threads / 8 idempotency keys racing one approval -> exactly 1 worker dispatch
  3 approval id + mismatched digest -> rejected
  4 a rejected approval cannot be approved afterwards
  5 worker returns 500/maybe-created -> approval frozen, never reusable
  6 crash right after reservation -> reservation survives a plane restart
  7 changing prompt / repo / policy / assertions invalidates the approval
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import (ArtifactAssertion, DispatchPolicy, DispatchResult,  # noqa: E402
                           TaskSnapshot, TaskState)
from controlplane.plane import ApprovalRequired, ControlPlane, task_digest  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402

CWD = str(ROOT / "sandbox" / "repo-a-t21")
HIGH = DispatchPolicy(mode="shell", isolation="none", allow_unisolated_shell=True)


class CountingAdapter:
    name = "counting"

    def __init__(self, cwd: str, uncertain: bool = False, crash: bool = False):
        self.cwd = cwd
        self.calls = 0
        self.uncertain = uncertain
        self.crash = crash
        self.lock = threading.Lock()

    def dispatch(self, request) -> DispatchResult:
        with self.lock:
            self.calls += 1
            n = self.calls
        time.sleep(0.05)
        if self.crash:
            raise RuntimeError("simulated plane crash right after the POST")
        if self.uncertain:
            return DispatchResult(task_id=request.task_id, worker_job_id=None,
                                  accepted=False, uncertain=True, warnings=[],
                                  raw={"dispatch_http": 500})
        return DispatchResult(task_id=request.task_id, worker_job_id=f"job-{n}",
                              accepted=True, raw={"dispatch_http": 200})

    def get_task(self, job_id: str, timeout_s: float = 20.0) -> TaskSnapshot:
        return TaskSnapshot(task_id=job_id, worker_job_id=job_id,
                            state=TaskState.RUNNING, cwd=self.cwd, detail="stub")

    def collect_artifacts(self, task_id):
        return []

    def reconcile(self):
        return {"source": "stub", "total": 0, "new": [], "changed": [], "lost": [], "jobs": []}


def fresh(db_name: str, adapter: CountingAdapter) -> tuple[ControlPlane, TaskStore]:
    store = TaskStore(ROOT / ".poc-state" / db_name)
    store.clear()
    return ControlPlane(adapter, store, policy=HIGH), store


def main() -> int:
    Path(CWD).mkdir(parents=True, exist_ok=True)
    log = {"test": "T21-approval-security", "policy": HIGH.to_dict()}
    assertions = [ArtifactAssertion("file_exists", "t21.txt")]
    prompt = "echo t21 > t21.txt"

    # ---- 1 risk downgrade ------------------------------------------------
    laundered = DispatchPolicy(mode="shell", isolation="none",
                               allow_unisolated_shell=True, risk="low")
    adapter, _ = CountingAdapter(CWD), None
    plane, store = fresh("t21-a.db", adapter)
    try:
        plane.dispatch(prompt, CWD, assertions=assertions, policy=laundered,
                       idempotency_key="t21-risk")
        blocked = "ALLOWED (bad)"
    except ApprovalRequired:
        blocked = "refused"
    log["1_risk_downgrade"] = {
        "declared_risk": laundered.risk, "intrinsic": laundered.intrinsic_risk(),
        "effective": laundered.effective_risk(),
        "requires_approval": laundered.requires_approval(),
        "dispatch": blocked, "worker_calls": adapter.calls,
        "pass": blocked == "refused" and laundered.effective_risk() == "high"
                and adapter.calls == 0,
    }

    # ---- 2 8 threads, 8 keys, one approval -------------------------------
    adapter2 = CountingAdapter(CWD)
    plane2, store2 = fresh("t21-b.db", adapter2)
    req = plane2.request_approval_for(prompt, CWD, assertions=assertions, note="human")
    plane2.approve(req["digest"], note="human ok")
    outcomes, barrier = [], threading.Barrier(8)

    def race(i: int) -> None:
        try:
            barrier.wait(timeout=10)
            rec = plane2.dispatch(prompt, CWD, assertions=assertions,
                                  idempotency_key=f"t21-race-{i}")
            outcomes.append({"i": i, "ok": bool(rec.get("worker_job_id"))})
        except ApprovalRequired:
            outcomes.append({"i": i, "ok": False, "refused": "approval_taken"})
        except Exception as exc:  # noqa: BLE001
            outcomes.append({"i": i, "ok": False, "error": type(exc).__name__})

    threads = [threading.Thread(target=race, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()                      # join without timeout: counters must be final
    time.sleep(0.2)
    ok_count = len([o for o in outcomes if o.get("ok")])
    approvals = store2.all_approvals()
    log["2_one_approval_eight_keys"] = {
        "worker_dispatches": adapter2.calls, "successful_attempts": ok_count,
        "refused": len([o for o in outcomes if o.get("refused")]),
        "approvals": [{"id": a["id"], "consumed": bool(a.get("consumed_at")),
                       "reserved_by": a.get("reserved_by")} for a in approvals],
        "outcomes": outcomes,
        "attempts": [{"task": t["task_id"], "attempts": store2.attempts(t["task_id"])}
                     for t in store2.all() if store2.attempts(t["task_id"])],
        "pass": adapter2.calls == 1 and ok_count == 1,
    }

    # ---- 3 id + mismatched digest ---------------------------------------
    adapter3 = CountingAdapter(CWD)
    plane3, store3 = fresh("t21-c.db", adapter3)
    req3 = plane3.request_approval_for(prompt, CWD, assertions=assertions)
    other = task_digest(prompt + " # different", HIGH, assertions, CWD)
    try:
        plane3.approve(other, approval_id=req3["approval_id"])
        r3 = "ALLOWED (bad)"
    except ValueError as exc:
        r3 = f"refused: {str(exc)[:60]}"
    log["3_digest_mismatch"] = {"result": r3, "pass": r3.startswith("refused")}

    # ---- 4 rejected then approved ---------------------------------------
    req4 = plane3.request_approval_for(prompt + " #4", CWD, assertions=assertions)
    plane3.deny(req4["digest"], "no")
    try:
        plane3.approve(req4["digest"], "changing my mind", approval_id=req4["approval_id"])
        r4 = "ALLOWED (bad)"
    except ValueError as exc:
        r4 = f"refused: {str(exc)[:60]}"
    log["4_revive_rejected"] = {"result": r4, "pass": r4.startswith("refused")}

    # ---- 5 uncertain worker -> frozen -----------------------------------
    adapter5 = CountingAdapter(CWD, uncertain=True)
    plane5, store5 = fresh("t21-d.db", adapter5)
    req5 = plane5.request_approval_for(prompt + " #5", CWD, assertions=assertions)
    plane5.approve(req5["digest"])
    rec5 = plane5.dispatch(prompt + " #5", CWD, assertions=assertions, idempotency_key="t21-u")
    ap5 = store5.approval_by_id(req5["approval_id"])
    try:
        plane5.dispatch(prompt + " #5", CWD, assertions=assertions, idempotency_key="t21-u2")
        r5 = "ALLOWED (bad)"
    except ApprovalRequired:
        r5 = "refused"
    log["5_uncertain_dispatch"] = {
        "state": rec5["state"], "reserved_by": ap5.get("reserved_by"),
        "consumed": bool(ap5.get("consumed_at")), "second_dispatch": r5,
        "worker_calls": adapter5.calls,
        "frozen_warning": any("APPROVAL_FROZEN" in w for w in (rec5.get("warnings") or [])),
        "pass": (rec5["state"] == "dispatch_unknown" and ap5.get("reserved_by") is not None
                 and not ap5.get("consumed_at") and r5 == "refused" and adapter5.calls == 1),
    }

    # ---- 6 crash after reservation --------------------------------------
    adapter6 = CountingAdapter(CWD, crash=True)
    plane6, store6 = fresh("t21-e.db", adapter6)
    req6 = plane6.request_approval_for(prompt + " #6", CWD, assertions=assertions)
    plane6.approve(req6["digest"])
    crashed = None
    try:
        plane6.dispatch(prompt + " #6", CWD, assertions=assertions, idempotency_key="t21-c")
    except RuntimeError:
        crashed = "RuntimeError"
    # "restart": new plane over the same ledger
    adapter6b = CountingAdapter(CWD)
    plane6b = ControlPlane(adapter6b, TaskStore(ROOT / ".poc-state" / "t21-e.db"), policy=HIGH)
    ap6 = plane6b.store.approval_by_id(req6["approval_id"])
    try:
        plane6b.dispatch(prompt + " #6", CWD, assertions=assertions, idempotency_key="t21-c2")
        r6 = "ALLOWED (bad)"
    except ApprovalRequired:
        r6 = "refused"
    log["6_crash_after_reservation"] = {
        "crashed": crashed, "reserved_by": ap6.get("reserved_by"),
        "consumed": bool(ap6.get("consumed_at")), "after_restart": r6,
        "worker_calls_after_restart": adapter6b.calls,
        "pass": (ap6.get("reserved_by") is not None and not ap6.get("consumed_at")
                 and r6 == "refused" and adapter6b.calls == 0),
    }

    # ---- 7 content drift invalidates ------------------------------------
    adapter7 = CountingAdapter(CWD)
    plane7, store7 = fresh("t21-f.db", adapter7)
    base = plane7.request_approval_for(prompt, CWD, assertions=assertions)
    plane7.approve(base["digest"])
    variants = {
        "prompt": task_digest(prompt + " & echo x", HIGH, assertions, CWD),
        "repo": task_digest(prompt, HIGH, assertions, str(ROOT / "sandbox" / "repo-b-t11")),
        "policy": task_digest(prompt, DispatchPolicy(mode="shell", isolation="none",
                                                    allow_unisolated_shell=True,
                                                    permission_mode="default"),
                              assertions, CWD),
        "assertions": task_digest(prompt, HIGH, [ArtifactAssertion("file_exists", "other.txt")], CWD),
    }
    drift = {k: v != base["digest"] for k, v in variants.items()}
    blocked_variants = {}
    for name, digest in variants.items():
        try:
            plane7.dispatch(
                prompt + " & echo x" if name == "prompt" else prompt,
                str(ROOT / "sandbox" / "repo-b-t11") if name == "repo" else CWD,
                assertions=[ArtifactAssertion("file_exists", "other.txt")] if name == "assertions"
                else assertions,
                policy=DispatchPolicy(mode="shell", isolation="none",
                                      allow_unisolated_shell=True,
                                      permission_mode="default") if name == "policy" else HIGH,
                idempotency_key=f"t21-drift-{name}")
            blocked_variants[name] = "ALLOWED (bad)"
        except ApprovalRequired:
            blocked_variants[name] = "refused"
    log["7_content_drift"] = {"digest_differs": drift, "dispatch": blocked_variants,
                              "worker_calls": adapter7.calls,
                              "pass": all(drift.values())
                                      and all(v == "refused" for v in blocked_variants.values())
                                      and adapter7.calls == 0}

    keys = ("1_risk_downgrade", "2_one_approval_eight_keys", "3_digest_mismatch",
            "4_revive_rejected", "5_uncertain_dispatch", "6_crash_after_reservation",
            "7_content_drift")
    log["pass"] = {k: log[k]["pass"] for k in keys}
    log["result"] = "Pass" if all(log["pass"].values()) else "Fail"
    (ROOT / "evidence" / "permissions").mkdir(parents=True, exist_ok=True)
    (ROOT / "evidence" / "permissions" / "T21-approval-security.json").write_text(
        json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: log[k] for k in keys}, ensure_ascii=False, indent=1)[:2400])
    print("RESULT:", log["result"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
