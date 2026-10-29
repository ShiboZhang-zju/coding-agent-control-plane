"""Minimal control plane facade: dispatch, reconcile, verify, summarize.

Everything the worker does not guarantee lives here:

  * dispatch **policy** (no implicit un-isolated shell execution),
  * idempotency keys and the `dispatch_unknown` state (never blind-retry),
  * a pre-dispatch baseline so acceptance can prove provenance,
  * `work_item -> run_attempt -> worker_job` mapping (resume-safe),
  * the aggregated summary a Foreman can read instead of guessing.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from adapters.base import (
    ArtifactAssertion,
    DispatchPolicy,
    DispatchRequest,
    TaskSnapshot,
    TaskState,
    UnsupportedOperation,
    Verdict,
)
from controlplane.store import LedgerDegraded, TaskStore
from controlplane.verify import capture_baseline, check_all, side_effects, verdict_for


class ApprovalRequired(RuntimeError):
    """High-risk dispatch without a matching, unconsumed human approval."""


def task_digest(prompt: str, policy: DispatchPolicy,
                assertions: list[ArtifactAssertion], cwd: str) -> str:
    """Content digest of *exactly* what will run.

    An approval binds to this digest, so editing the prompt/assertions/target after
    the human said "yes" invalidates the approval instead of silently reusing it.
    """
    payload = json.dumps({
        "prompt": prompt.strip(),
        "cwd": str(Path(cwd).resolve()),
        "policy": policy.to_dict(),
        "assertions": [a.to_dict() for a in assertions],
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


class ControlPlane:
    def __init__(self, adapter, store: TaskStore | None = None,
                 policy: DispatchPolicy | None = None):
        self.adapter = adapter
        self.store = store or TaskStore()
        self.policy = policy or DispatchPolicy()

    # ------------------------------------------------------------------ #
    # dispatch
    # ------------------------------------------------------------------ #
    def dispatch(self, prompt: str, cwd: str, *, assertions: list[ArtifactAssertion] | None = None,
                 policy: DispatchPolicy | None = None, title: str = "",
                 idempotency_key: str = "", timeout_s: int = 900,
                 metadata: dict | None = None) -> dict:
        """Dispatch with an explicit policy.

        Refuses on: unsafe policy (ValueError), degraded ledger (LedgerDegraded),
        high-risk task without a matching approval (ApprovalRequired).
        """
        assertions = assertions or []
        policy = policy or self.policy
        risks = policy.validate()

        # fail closed: a degraded ledger no longer knows what is already running
        self.store.assert_writable("dispatch")

        digest = task_digest(prompt, policy, assertions, cwd)
        approval = None
        if policy.requires_approval():
            approval = self.store.valid_approval_by_digest(digest)
            if not approval:
                self.store.append_event(
                    "", "approval_required",
                    {"digest": digest, "risk": policy.effective_risk(), "cwd": str(cwd)})
                raise ApprovalRequired(
                    f"risk={policy.effective_risk()}: dispatch requires a human approval whose "
                    f"digest matches this exact task ({digest}). Call approve(digest=...) first; "
                    "changing the prompt/policy/assertions invalidates the approval.")

        if idempotency_key:
            existing = self.store.by_idempotency_key(idempotency_key)
            if existing:
                out = dict(existing)
                out["deduplicated"] = True
                return out

        task_id = self.store.new_task_id()
        baseline = capture_baseline(cwd, assertions)
        req = DispatchRequest(prompt=prompt, cwd=cwd, task_id=task_id, title=title,
                              mode=policy.mode, isolation=policy.isolation,
                              assertions=assertions, idempotency_key=idempotency_key,
                              timeout_s=timeout_s,
                              metadata={**(metadata or {}),
                                        "permissionMode": policy.permission_mode})

        record = {
            "task_id": task_id,
            "title": title or prompt[:60],
            "prompt": prompt,
            "cwd": cwd,
            "mode": policy.mode,
            "isolation": policy.isolation,
            "idempotency_key": idempotency_key,
            "policy": policy.to_dict(),
            "assertions": [a.to_dict() for a in assertions],
            "baseline": baseline,
            "state": TaskState.QUEUED.value,
            "verdict": Verdict.PENDING.value,
            "warnings": risks,
            "metadata": {"adapter": self.adapter.name},
        }
        record, created = self.store.create_work_item(record)
        task_id = record["task_id"]
        if not created:
            # another thread/process won the race on this idempotency key: it owns
            # the dispatch, so this caller must not POST a second job.
            out = dict(record)
            out["deduplicated"] = True
            self.store.append_event(task_id, "dispatch_deduplicated_at_insert",
                                    {"key": idempotency_key})
            return out

        # Pre-create the attempt so the approval reservation can bind to it, not to
        # a worker job id that does not exist yet.
        attempt = self.store.add_attempt(task_id, None, reason="dispatch")
        attempt_id = attempt["id"]

        approval = None
        if policy.requires_approval():
            approval = self.store.reserve_approval(digest, attempt_id)
            if not approval:
                # lost the race (or the human never approved this exact content)
                self.store.update(
                    task_id, state=TaskState.UNKNOWN.value,
                    verdict=Verdict.INCONCLUSIVE.value,
                    warnings=risks + ["APPROVAL_RACE_LOST: another attempt reserved this "
                                      "approval first, or no approval matches this digest"])
                self.store.append_event(task_id, "approval_reservation_failed",
                                        {"digest": digest})
                raise ApprovalRequired(
                    "another attempt already reserved the approval for this digest; "
                    "no worker call was made")

        self.store.append_event(task_id, "dispatch_requested",
                                {"policy": policy.to_dict(), "cwd": cwd,
                                 "attempt_id": attempt_id,
                                 "approval_id": (approval or {}).get("id")})

        result = self.adapter.dispatch(req)
        patch: dict[str, Any] = {
            "warnings": risks + list(result.warnings),
            "metadata": {"adapter": self.adapter.name, "dispatch_raw": result.raw},
        }

        if result.uncertain:
            # P0: the worker may or may not have created a job. Do not retry, and
            # keep the approval FROZEN (reserved, never reusable) until a human
            # resolves it - releasing it could authorise a second execution.
            patch["state"] = TaskState.DISPATCH_UNKNOWN.value
            patch["verdict"] = Verdict.PENDING.value
            patch["warnings"] = patch["warnings"] + [
                "DISPATCH_UNKNOWN: no blind retry. Call resolve_dispatch(task_id) first; "
                "it either binds the existing job or proves none was created.",
                "APPROVAL_FROZEN: reservation kept, it cannot authorise another attempt"]
            self.store.append_event(task_id, "dispatch_unknown",
                                    {**result.raw, "approval_frozen": bool(approval)})
            return self.store.update(task_id, **patch)

        if not result.accepted or not result.worker_job_id:
            if approval:
                # we know for certain no job was created, so the human's approval is
                # still good for a later, corrected attempt
                self.store.release_reservation(approval["id"], attempt_id)
            patch["state"] = TaskState.UNKNOWN.value
            patch["verdict"] = Verdict.INCONCLUSIVE.value
            self.store.append_event(task_id, "dispatch_failed",
                                    {**result.raw, "approval_released": bool(approval)})
            return self.store.update(task_id, **patch)

        snap = self.adapter.get_task(result.worker_job_id)
        patch.update(self._snapshot_patch(task_id, snap))
        patch["metadata"] = {**patch["metadata"], "task_digest": digest,
                             "approval_id": (approval or {}).get("id")}
        self.store.append_event(task_id, "dispatch_accepted",
                                {"worker_job_id": result.worker_job_id,
                                 "http": result.raw.get("dispatch_http")})
        out = self.store.update(task_id, worker_job_id=result.worker_job_id, **patch)

        if approval:
            # single use, bound to this attempt; a second dispatch with the same
            # digest must ask a human again.
            consumed = self.store.consume_reserved(approval["id"], attempt_id,
                                                   result.worker_job_id)
            self.store.append_event(task_id, "approval_consumed",
                                    {"approval_id": approval["id"], "ok": consumed,
                                     "attempt_id": attempt_id})
            if not consumed:
                # reservation vanished mid-flight: the job exists but the approval
                # bookkeeping is inconsistent - surface it instead of hiding it
                self.store.update(task_id, warnings=list(out.get("warnings") or []) +
                                  ["APPROVAL_CONSUMPTION_MISMATCH: reservation was lost "
                                   "before consumption; audit this attempt"])
        return out

    # ------------------------------------------------------------------ #
    # approval gating
    # ------------------------------------------------------------------ #
    def request_approval_for(self, prompt: str, cwd: str, *,
                             assertions: list[ArtifactAssertion] | None = None,
                             policy: DispatchPolicy | None = None,
                             note: str = "") -> dict:
        """Create a pending approval for the exact content that would be dispatched."""
        assertions = assertions or []
        policy = policy or self.policy
        self.store.assert_writable("request_approval")
        digest = task_digest(prompt, policy, assertions, cwd)
        scope = {"risk": policy.effective_risk(), "cwd": str(Path(cwd).resolve()),
                 "policy": policy.to_dict(),
                 "assertions": [a.to_dict() for a in assertions]}
        rec = self.store.request_approval("", digest, scope, note=note)
        return {"approval_id": rec["id"], "digest": digest, "scope": scope,
                "status": "pending"}

    def _decide(self, digest: str, decision: str, note: str,
                approval_id: int | None) -> dict:
        """Shared validation for approve/deny.

        An explicit approval id must still match the digest, must be `pending`, and
        must not be consumed - otherwise a rejected or spent approval could be
        revived by passing its id.
        """
        self.store.assert_writable(decision)
        if approval_id is not None:
            row = self.store.approval_by_id(approval_id)
            if not row:
                raise KeyError(f"no approval #{approval_id}")
            if row.get("digest") != digest:
                raise ValueError(
                    f"approval #{approval_id} is bound to digest {row.get('digest')}, "
                    f"not {digest}: content changed, the approval no longer applies")
            if row.get("consumed_at"):
                raise ValueError(f"approval #{approval_id} is already consumed")
            if row.get("decision") != "pending":
                raise ValueError(
                    f"approval #{approval_id} is already '{row.get('decision')}'")
            out = self.store.decide_approval(approval_id, decision, note, expect_digest=digest)
            if not out:
                raise ValueError(f"approval #{approval_id} is no longer pending (race)")
        else:
            pending = [a for a in self.store.all_approvals()
                       if a.get("digest") == digest and a.get("decision") == "pending"
                       and not a.get("consumed_at")]
            if not pending:
                raise KeyError(f"no pending approval for digest {digest}")
            out = self.store.decide_approval(pending[-1]["id"], decision, note,
                                             expect_digest=digest)
            if not out:
                raise ValueError(f"approval for digest {digest} is no longer pending")
        self.store.append_event("", f"approval_{'granted' if decision == 'approved' else 'denied'}",
                                {"digest": digest, "id": (out or {}).get("id")})
        return out or {}

    def approve(self, digest: str, note: str = "", approval_id: int | None = None) -> dict:
        """Human decision: approve the content digest (not "the agent in general")."""
        return self._decide(digest, "approved", note, approval_id)

    def deny(self, digest: str, note: str = "", approval_id: int | None = None) -> dict:
        return self._decide(digest, "rejected", note, approval_id)

    # ------------------------------------------------------------------ #
    def dispatch_approved(self, prompt: str, cwd: str, *, assertions=None,
                          policy: DispatchPolicy | None = None, title: str = "",
                          idempotency_key: str = "", metadata: dict | None = None,
                          approved_by: str = "harness") -> dict:
        """PoC/test convenience: request + approve + dispatch in one call.

        The approval note records who approved it, so an automated approval is
        auditable in the ledger and never looks like a human decision. Production
        callers must use request_approval_for() + a trusted human approve().
        """
        assertions = assertions or []
        policy = policy or self.policy
        req = self.request_approval_for(prompt, cwd, assertions=assertions, policy=policy,
                                        note=f"approved_by={approved_by}")
        self.approve(req["digest"], note=f"approved_by={approved_by}", approval_id=req["approval_id"])
        return self.dispatch(prompt, cwd, assertions=assertions, policy=policy, title=title,
                             idempotency_key=idempotency_key, metadata=metadata)

    # ------------------------------------------------------------------ #
    def resolve_dispatch(self, task_id: str, confirm_absent: bool = False) -> dict:
        """Try to bind a `dispatch_unknown` work item to a concrete worker job.

        Honesty rule (review P0-2): a miss only proves "not visible in this query
        window with this matcher" — the global job list is ~30s slow and SSE has no
        replay. So a miss keeps the item in `dispatch_unknown`; it never becomes
        "safe to re-dispatch" automatically. Only an explicit human/operator
        confirmation (`confirm_absent=True`) closes the uncertainty, and that
        decision is recorded as an event.
        """
        rec = self.store.get(task_id)
        if not rec:
            raise KeyError(f"no work item {task_id}")
        if rec.get("state") != TaskState.DISPATCH_UNKNOWN.value:
            return rec

        find = getattr(self.adapter, "find_job", None)
        job_id = None
        if callable(find):
            job_id = find(rec["prompt"], rec["cwd"], since=rec.get("created_at") or 0)

        if job_id:
            snap = self.adapter.get_task(job_id)
            self.store.append_event(task_id, "dispatch_resolved",
                                    {"worker_job_id": job_id, "note": "job existed"})
            return self.store.update(task_id, worker_job_id=job_id,
                                     **self._snapshot_patch(task_id, snap))

        if not confirm_absent:
            self.store.append_event(task_id, "dispatch_unresolved",
                                    {"note": "not visible in the query window; still uncertain"})
            return self.store.update(
                task_id,
                warnings=list(rec.get("warnings") or []) +
                ["DISPATCH_STILL_UNKNOWN: 未发现不等于未创建（列表有 ~30s 延迟、无事件重放）；"
                 "禁止自动重派，需人工 confirm_absent"])

        self.store.append_event(task_id, "dispatch_absent_confirmed",
                                {"by": "operator", "note": "human confirmed no job exists"})
        return self.store.update(
            task_id, state=TaskState.UNKNOWN.value, verdict=Verdict.INCONCLUSIVE.value,
            warnings=list(rec.get("warnings") or []) +
            ["DISPATCH_ABSENT_CONFIRMED_BY_OPERATOR: 人工确认未创建，允许新 attempt"])

    # ------------------------------------------------------------------ #
    # refresh: two independent paths
    # ------------------------------------------------------------------ #
    def refresh_known_jobs(self, timeout_s: float = 20.0) -> dict:
        """High-frequency path: one query per ledger job, partial success allowed.

        A failed/timeout query is an *observation anomaly*, never evidence that the
        task died (`lost` needs independent process/state evidence).
        """
        updated, anomalies = [], []
        for task in self.store.all():
            jid = task.get("worker_job_id")
            if not jid:
                continue
            try:
                try:
                    snap = self.adapter.get_task(jid, timeout_s=timeout_s)
                except TypeError:            # adapter without a timeout parameter
                    snap = self.adapter.get_task(jid)
            except Exception as exc:  # noqa: BLE001
                anomalies.append(self._note_observation_failure(task, exc))
                continue
            patch = self._snapshot_patch(task["task_id"], snap)
            if patch["state"] == TaskState.LOST.value:
                patch = self._confirm_lost(task, snap, patch)
            self.store.update(task["task_id"], **patch)
            updated.append({"task_id": task["task_id"], "worker_job_id": jid,
                            "state": patch["state"], "verdict": patch["verdict"]})
        return {"updated": updated, "anomalies": anomalies,
                "ok": len(anomalies) == 0, "checked": len(updated) + len(anomalies)}

    def discover_orphan_jobs(self) -> dict:
        """Low-frequency full scan. Failure here must not disturb known tasks."""
        try:
            rec = self.adapter.reconcile()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                    "note": "full scan failed; known-task state left untouched"}
        worker_ids = {j.get("id") for j in rec["jobs"]}
        known = {t.get("worker_job_id") for t in self.store.all() if t.get("worker_job_id")}
        orphans = sorted(worker_ids - known)
        vanished = []
        for task in self.store.all():
            jid = task.get("worker_job_id")
            if not jid or jid in worker_ids:
                continue
            misses = int((task.get("metadata") or {}).get("scan_misses", 0)) + 1
            self.store.update(task["task_id"],
                              metadata={**(task.get("metadata") or {}), "scan_misses": misses})
            if misses < 2:
                vanished.append({"task_id": task["task_id"], "job": jid, "misses": misses,
                                 "action": "pending second scan"})
                continue
            patch = self._mark_lost(task, jid, "job id absent from two consecutive full scans")
            vanished.append({"task_id": task["task_id"], "job": jid, "misses": misses,
                             "action": patch["state"]})
        return {"ok": True, "total": len(rec["jobs"]), "orphans": orphans[:20],
                "orphan_count": len(orphans), "vanished": vanished}

    def refresh(self, timeout_s: float = 20.0) -> dict:
        """Known jobs first (cheap, partial), then a best-effort orphan scan."""
        known = self.refresh_known_jobs(timeout_s=timeout_s)
        scan = self.discover_orphan_jobs()
        return {"known_jobs": known, "orphan_scan": scan, "tasks": self.summary()}

    def _note_observation_failure(self, task: dict, exc: Exception) -> dict:
        count = int((task.get("metadata") or {}).get("observe_failures", 0)) + 1
        self.store.update(task["task_id"],
                          metadata={**(task.get("metadata") or {}),
                                    "observe_failures": count,
                                    "last_observe_error": f"{type(exc).__name__}"})
        self.store.append_event(task["task_id"], "observation_failed",
                                {"error": f"{type(exc).__name__}: {exc}", "count": count})
        return {"task_id": task["task_id"], "error": f"{type(exc).__name__}",
                "count": count, "state_unchanged": True}

    def _confirm_lost(self, task: dict, snap: TaskSnapshot, patch: dict) -> dict:
        """Adapter says lost: demand independent evidence before accepting it."""
        alive = None
        probe = getattr(self.adapter, "pid_alive", None)
        if callable(probe):
            try:
                alive = probe(snap.pid)
            except Exception:  # noqa: BLE001
                alive = None
        if alive is False or (snap.pid is None and alive is None):
            return patch
        # cannot prove death: keep the previous state, record the doubt
        prev = task.get("state") or TaskState.UNKNOWN.value
        return {**patch, "state": prev,
                "warnings": list(patch.get("warnings") or []) +
                [f"LOST_UNCONFIRMED: adapter reports working/lost but pid={snap.pid} alive={alive}"]}

    def _mark_lost(self, task: dict, jid: str, reason: str) -> dict:
        patch = {"state": TaskState.LOST.value, "verdict": Verdict.REJECTED.value,
                 "warnings": list(task.get("warnings") or []) + [f"LOST: {reason}"]}
        self.store.update(task["task_id"], **patch)
        self.store.append_event(task["task_id"], "lost", {"worker_job_id": jid, "reason": reason})
        return patch

    def recover_inflight(self) -> dict:
        """Close the crash window between "intent persisted" and "job id stored".

        Any work item still `queued` with no worker job id means we died after
        writing intent (possibly after the POST). Treat it exactly like
        dispatch_unknown: reject blind re-dispatch, require resolve_dispatch().
        """
        recovered = []
        for task in self.store.all():
            if task.get("worker_job_id") or task.get("state") != TaskState.QUEUED.value:
                continue
            self.store.update(
                task["task_id"], state=TaskState.DISPATCH_UNKNOWN.value,
                warnings=list(task.get("warnings") or []) +
                ["DISPATCH_INFLIGHT_AT_RESTART: intent was persisted but no job id; "
                 "run resolve_dispatch() before any new attempt"])
            self.store.append_event(task["task_id"], "recovered_inflight", {})
            recovered.append(task["task_id"])
        return {"recovered": recovered, "count": len(recovered)}

    def _snapshot_patch(self, task_id: str, snap: TaskSnapshot) -> dict:
        # task_id is passed explicitly: after an adapter/plane restart the adapter
        # has no in-memory job->task map, so `snap.task_id` degrades to the worker
        # job id and assertions/baseline would silently be lost (review P0-1).
        rec = self.store.get(task_id) or {}
        assertions = [ArtifactAssertion(**a) for a in rec.get("assertions") or []]
        baseline = rec.get("baseline") or {}

        # Agent tasks run inside a git worktree: artifacts must be verified there,
        # not in the (possibly stale) main checkout.
        wt = snap.worktree
        in_worktree = bool(wt) and Path(wt).exists()
        eval_cwd = wt if in_worktree else snap.cwd
        eval_baseline = baseline
        if in_worktree and baseline:
            eval_baseline = {**baseline, "cwd": wt, "status": "",
                             "siblings": {}}  # peer worktrees are isolated by construction

        results = check_all(eval_cwd, assertions, eval_baseline) if eval_cwd else []
        violations: list[dict] = []
        if snap.cwd:
            declared = [a.target for a in assertions if a.kind in ("file_exists", "file_contains")]
            violations += side_effects(snap.cwd, baseline, allowed_paths=declared,
                                       allowed_prefixes=(".codebuddy/",))
        verdict = verdict_for(snap.state, snap.state is not TaskState.UNKNOWN, results, violations)

        patch = {
            "state": snap.state.value,
            "verdict": verdict.value,
            "session_id": snap.session_id,
            "pid": snap.pid,
            "worktree": snap.worktree,
            "branch": snap.branch,
            "head_commit": snap.commit,
            "detail": snap.detail,
            "started_at": snap.started_at,
            "finished_at": snap.finished_at,
            "artifacts": self.adapter.collect_artifacts(snap.worker_job_id or snap.task_id),
            "assertion_results": results,
            "worker_metadata": snap.metadata.get("codebuddy", {}),
        }
        if violations:
            patch["warnings"] = list(rec.get("warnings") or []) + [
                "SIDE_EFFECT:" + v["type"] + ":" + str(v.get("repo") or v.get("cwd"))
                for v in violations]
        return patch

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def cancel(self, task_id: str) -> dict:
        rec = self.store.get(task_id) or {}
        jid = rec.get("worker_job_id")
        if not jid:
            raise KeyError(f"no worker job for {task_id}")
        snap = self.adapter.cancel(jid)
        self.store.append_event(task_id, "cancel_requested", {"worker_job_id": jid})
        return self.store.update(task_id, **self._snapshot_patch(task_id, snap))

    def resume(self, task_id: str) -> dict:
        """Re-attach by conversation. A new worker job becomes a new attempt."""
        self.store.assert_writable("resume")
        rec = self.store.get(task_id) or {}
        old_job = rec.get("worker_job_id")
        snap = self.adapter.resume(old_job or task_id)
        new_job = snap.worker_job_id
        self.store.append_event(task_id, "resume", {"from_job": old_job, "to_job": new_job})
        self.store.add_attempt(task_id, new_job, reason=f"resume from {old_job}")
        return self.store.update(task_id, worker_job_id=new_job,
                                 **self._snapshot_patch(task_id, snap))

    def respawn(self, task_id: str) -> dict:
        self.store.assert_writable("respawn")
        rec = self.store.get(task_id) or {}
        old_job = rec.get("worker_job_id")
        snap = self.adapter.respawn(old_job or task_id)
        new_job = snap.worker_job_id
        self.store.append_event(task_id, "respawn", {"from_job": old_job, "to_job": new_job})
        if new_job and new_job != old_job:
            self.store.add_attempt(task_id, new_job, reason=f"respawn from {old_job}")
        return self.store.update(task_id, worker_job_id=new_job or old_job,
                                 **self._snapshot_patch(task_id, snap))

    def request_approval(self, task_id: str, decision: str, note: str = "") -> dict:
        """Record a human decision in the ledger.

        The worker cannot receive a decision for a background job (see
        evidence/permissions/T09-permission-denied.json), so approval is modelled
        as "stop this attempt, record the decision, run a new attempt later".
        """
        self.store.assert_writable("request_approval")
        rec = self.store.get(task_id) or {}
        self.store.record_approval(task_id, decision, note,
                                   attempt_id=rec.get("current_attempt_id"))
        delivered = False
        reason = None
        try:
            self.adapter.request_approval(rec.get("worker_job_id") or task_id, decision, note)
            delivered = True
        except UnsupportedOperation as exc:
            reason = str(exc)
        self.store.append_event(task_id, "approval_recorded",
                                {"decision": decision, "delivered_to_worker": delivered})
        return {"recorded": True, "delivered_to_worker": delivered, "reason": reason,
                "next_step": ("stop the current attempt, then dispatch a new attempt with the "
                              "approved scope") if not delivered else "decision delivered"}

    # ------------------------------------------------------------------ #
    def summary(self) -> dict:
        """The Foreman-facing view: one structured call, no per-task window."""
        tasks = self.store.all()
        counts: dict[str, int] = {}
        for t in tasks:
            counts[t.get("state", "unknown")] = counts.get(t.get("state", "unknown"), 0) + 1

        waiting = [t for t in tasks if t.get("state") == TaskState.WAITING_FOR_APPROVAL.value]
        rejected = [t for t in tasks if t.get("verdict") == Verdict.REJECTED.value]
        running = [t for t in tasks if t.get("state") in
                   (TaskState.RUNNING.value, TaskState.STARTING.value, TaskState.QUEUED.value)]
        verified = [t for t in tasks if t.get("verdict") == Verdict.VERIFIED.value]
        inconclusive = [t for t in tasks if t.get("verdict") == Verdict.INCONCLUSIVE.value]
        unknown_dispatch = [t for t in tasks
                            if t.get("state") == TaskState.DISPATCH_UNKNOWN.value]

        next_actions = []
        if unknown_dispatch:
            next_actions.append("派发结果不确定，先 resolve_dispatch: "
                                + ", ".join(t["task_id"] for t in unknown_dispatch))
        if waiting:
            next_actions.append("等待人工审批: " + ", ".join(t["task_id"] for t in waiting))
        if rejected:
            next_actions.append("未通过验收: " + ", ".join(t["task_id"] for t in rejected))
        if inconclusive:
            next_actions.append("验收不确定（缺机读断言）: "
                                + ", ".join(t["task_id"] for t in inconclusive))
        if running:
            next_actions.append(f"{len(running)} 个任务仍在运行，稍后 refresh")

        return {
            "generated_at": time.time(),
            "total": len(tasks),
            "ledger": {"path": str(self.store.path), "degraded": self.store.degraded},
            "counts": {
                "by_state": counts,
                "running": len(running),
                "waiting_for_approval": len(waiting),
                "verified": len(verified),
                "rejected": len(rejected),
                "inconclusive": len(inconclusive),
                "dispatch_unknown": len(unknown_dispatch),
            },
            "needs_human": [t["task_id"] for t in waiting],
            "tasks": [self._task_view(t) for t in tasks],
            "next_actions": next_actions,
        }

    def _task_view(self, t: dict) -> dict:
        attempts = self.store.attempts(t["task_id"])
        return {
            "task_id": t["task_id"],
            "worker_job_id": t.get("worker_job_id"),
            "attempts": [{"attempt_id": a["id"], "worker_job_id": a.get("worker_job_id"),
                          "reason": a.get("reason"), "at": a.get("created_at")}
                         for a in attempts],
            "title": t.get("title"),
            "cwd": t.get("cwd"),
            "mode": t.get("mode"),
            "isolation": t.get("isolation"),
            "worktree": t.get("worktree"),
            "state": t.get("state"),
            "verdict": t.get("verdict"),
            "detail": t.get("detail"),
            "session_id": t.get("session_id"),
            "pid": t.get("pid"),
            "commit": t.get("head_commit"),
            "artifacts": t.get("artifacts") or [],
            "assertions": {r["kind"] + ":" + r["target"]: r["passed"]
                           for r in t.get("assertion_results") or []},
            "warnings": t.get("warnings") or [],
        }
