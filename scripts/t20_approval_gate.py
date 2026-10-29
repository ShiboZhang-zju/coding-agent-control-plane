"""T20: approval must gate execution, bind to content, and be single-use.

High-risk policy (un-isolated shell) cannot dispatch without an approval whose
digest matches the exact prompt+policy+assertions+cwd:
  A. no approval            -> ApprovalRequired, worker never called
  B. approve then dispatch   -> allowed once, approval consumed
  C. second dispatch         -> ApprovalRequired again (single use)
  D. tampered prompt         -> digest mismatch -> ApprovalRequired
  E. denied request          -> still blocked
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import (ArtifactAssertion, DispatchPolicy, DispatchResult,  # noqa: E402
                           TaskSnapshot, TaskState)
from controlplane.plane import ApprovalRequired, ControlPlane, task_digest  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402


class CountingAdapter:
    name = "counting"

    def __init__(self, cwd: str):
        self.cwd = cwd
        self.calls = 0

    def dispatch(self, request) -> DispatchResult:
        self.calls += 1
        return DispatchResult(task_id=request.task_id, worker_job_id=f"job-{self.calls}",
                              accepted=True, raw={"dispatch_http": 200})

    def get_task(self, job_id: str, timeout_s: float = 20.0) -> TaskSnapshot:
        return TaskSnapshot(task_id=job_id, worker_job_id=job_id, state=TaskState.RUNNING,
                            cwd=self.cwd, detail="stub")

    def collect_artifacts(self, task_id):
        return []

    def reconcile(self):
        return {"source": "stub", "total": 0, "new": [], "changed": [], "lost": [], "jobs": []}


def main() -> int:
    cwd = str(ROOT / "sandbox" / "repo-a-t11")
    store = TaskStore(ROOT / ".poc-state" / "t20.db")
    store.clear()
    adapter = CountingAdapter(cwd)
    # high risk on purpose: shell without worktree isolation
    policy = DispatchPolicy(mode="shell", isolation="none", allow_unisolated_shell=True)
    plane = ControlPlane(adapter, store, policy=policy)
    assertions = [ArtifactAssertion("file_exists", "t20.txt")]
    prompt = "echo t20 > t20.txt"

    log = {"test": "T20-approval-gate", "policy": policy.to_dict(),
           "requires_approval": policy.requires_approval()}

    # A: no approval
    try:
        plane.dispatch(prompt, cwd, assertions=assertions, idempotency_key="t20-a")
        a = "ALLOWED (bad)"
    except ApprovalRequired:
        a = "refused"
    log["A_without_approval"] = {"result": a, "worker_calls": adapter.calls}

    # B: request + approve -> allowed, consumed
    req = plane.request_approval_for(prompt, cwd, assertions=assertions, note="poc")
    plane.approve(req["digest"], note="approved for the exact prompt")
    rec = plane.dispatch(prompt, cwd, assertions=assertions, idempotency_key="t20-b")
    consumed = [r for r in store.all_approvals() if r["digest"] == req["digest"]][0]
    log["B_after_approval"] = {"dispatched": bool(rec.get("worker_job_id")),
                               "worker_calls": adapter.calls,
                               "approval_consumed": consumed.get("consumed_at") is not None,
                               "consumed_by": consumed.get("consumed_by")}

    # C: single use
    try:
        plane.dispatch(prompt, cwd, assertions=assertions, idempotency_key="t20-c")
        c = "ALLOWED (bad)"
    except ApprovalRequired:
        c = "refused"
    log["C_reuse_approval"] = {"result": c, "worker_calls": adapter.calls}

    # D: tampered prompt after approval
    req2 = plane.request_approval_for(prompt, cwd, assertions=assertions, note="poc")
    plane.approve(req2["digest"])
    tampered = prompt + " & echo pwned >> ../outside.txt"
    d_digest_differs = task_digest(tampered, policy, assertions, cwd) != req2["digest"]
    try:
        plane.dispatch(tampered, cwd, assertions=assertions, idempotency_key="t20-d")
        d = "ALLOWED (bad)"
    except ApprovalRequired:
        d = "refused"
    log["D_tampered_prompt"] = {"digest_differs": d_digest_differs, "result": d,
                                "worker_calls": adapter.calls}

    # E: denied (use distinct content: the approved-but-unused record from D is
    # still valid for the *original* prompt by design, so E needs its own digest)
    prompt_e = "echo t20e > t20e.txt"
    assertions_e = [ArtifactAssertion("file_exists", "t20e.txt")]
    req3 = plane.request_approval_for(prompt_e, cwd, assertions=assertions_e, note="poc")
    plane.deny(req3["digest"], note="not allowed")
    try:
        plane.dispatch(prompt_e, cwd, assertions=assertions_e, idempotency_key="t20-e")
        e = "ALLOWED (bad)"
    except ApprovalRequired:
        e = "refused"
    log["E_denied"] = {"result": e, "worker_calls": adapter.calls}

    log["pass"] = {
        "blocked_without_approval": a == "refused" and log["A_without_approval"]["worker_calls"] == 0,
        "allowed_once_with_approval": log["B_after_approval"]["dispatched"] and adapter.calls >= 1,
        "approval_consumed": log["B_after_approval"]["approval_consumed"],
        "single_use_enforced": c == "refused",
        "tampered_prompt_blocked": d == "refused" and d_digest_differs,
        "denied_blocked": e == "refused",
    }
    log["result"] = "Pass" if all(log["pass"].values()) else "Fail"
    (ROOT / "evidence" / "permissions").mkdir(parents=True, exist_ok=True)
    (ROOT / "evidence" / "permissions" / "T20-approval-gate.json").write_text(
        json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(log, ensure_ascii=False, indent=1)[:1800])
    return 0


if __name__ == "__main__":
    sys.exit(main())
