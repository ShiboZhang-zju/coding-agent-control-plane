"""Deterministic Project Context update loop (M2).

This module turns a terminal, evidence-backed work item into two *candidate*
context writes:

* a narrative ``progress`` entry containing the blocker/next step; and
* a ``knowledge`` entry containing reusable facts extracted from assertions,
  attempts, event types and warning codes.

There is deliberately no LLM here. Automatic writes can never create
``constraints`` or ``verified`` knowledge. Every write has a database-enforced
idempotency receipt and uses the M1 CAS path. A stale writer is preserved as a
separate ``needs_merge`` candidate instead of silently overwriting the winner.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from adapters.base import Verdict
from controlplane.project_context import (
    ContextError,
    ContextVersionConflict,
    ProjectContextStore,
)
from controlplane.store import TaskStore
from scripts.sanitize_evidence import sanitize_text, write_evidence

ROOT = Path(__file__).resolve().parent.parent
TERMINAL_VERDICTS = {
    Verdict.VERIFIED.value,
    Verdict.REJECTED.value,
    Verdict.INCONCLUSIVE.value,
}
AUTO_KINDS = {"progress", "knowledge"}


def context_update_key(task_id: str, attempt_id: str, kind: str, title: str) -> str:
    """Stable key required by the M2 replay contract."""
    payload = json.dumps(
        [task_id, attempt_id or "", kind, title],
        ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ContextCandidate:
    """One deterministic automatic write.

    ``expected_version`` is normally omitted, meaning "update the version I just
    read". It is exposed for callers replaying a concurrently prepared proposal:
    if that version is stale, the candidate becomes ``needs_merge``.
    """

    project_id: str
    task_id: str
    attempt_id: str
    kind: str
    title: str
    body: str
    evidence_refs: tuple[str, ...]
    tags: tuple[str, ...] = field(default_factory=tuple)
    source: str = ""
    expected_version: int | None = None

    @property
    def idempotency_key(self) -> str:
        return context_update_key(
            self.task_id, self.attempt_id, self.kind, self.title)


class ProjectContextUpdateLoop:
    """Apply terminal task evidence to one project's durable context."""

    def __init__(self, store: TaskStore, *, repo_root: Path | str | None = None,
                 owner: str | None = None, claim_lease_s: float = 60.0):
        self.store = store
        self.context = ProjectContextStore(store)
        self.repo_root = Path(repo_root or ROOT).resolve()
        self.owner = owner or "context-update-" + uuid.uuid4().hex[:10]
        self.claim_lease_s = claim_lease_s

    def process_task(self, task_id: str, *, attempt_id: str | None = None) -> dict:
        """Generate and apply M2 candidates for one terminal project task.

        Replaying the same task/attempt is a read-only operation: completed
        receipts are returned and no context version is bumped.
        """
        task = self.store.get(task_id)
        if not task:
            raise KeyError(f"no task {task_id}")
        project_id = task.get("project_id")
        if not project_id:
            return {"task_id": task_id, "status": "skipped",
                    "reason": "task is not bound to a project", "updates": []}
        verdict = task.get("verdict")
        if verdict not in TERMINAL_VERDICTS:
            return {"task_id": task_id, "project_id": project_id,
                    "status": "skipped", "reason": f"verdict {verdict!r} is not terminal",
                    "updates": []}

        attempt = self._select_attempt(task, attempt_id)
        selected_attempt_id = str(attempt["id"])

        evidence_ref = self._evidence_ref(task, attempt)
        # Evidence is append-only: create it when absent, never rewrite an
        # existing snapshot. A replay also repairs an accidentally missing file
        # before returning its completed receipts.
        self._write_task_evidence(task, attempt, evidence_ref)
        candidates = self._derive_candidates(task, attempt, evidence_ref)
        prior = {row["idempotency_key"]: row
                 for row in self.store.context_update_receipts(task_id)}
        if all(candidate.idempotency_key in prior
               and prior[candidate.idempotency_key].get("status")
               in ("applied", "needs_merge", "skipped")
               for candidate in candidates):
            updates = [{
                "status": prior[candidate.idempotency_key]["status"],
                "idempotency_key": candidate.idempotency_key,
                "entry_id": prior[candidate.idempotency_key].get("entry_id"),
                "replayed": True,
            } for candidate in candidates]
            return {
                "task_id": task_id, "project_id": project_id,
                "attempt_id": selected_attempt_id, "verdict": verdict,
                "status": ("needs_merge" if any(
                    update["status"] == "needs_merge" for update in updates)
                           else "applied"),
                "evidence_ref": evidence_ref, "updates": updates,
            }
        updates = [self.apply_candidate(candidate) for candidate in candidates]
        statuses = {item.get("status") for item in updates}
        overall = ("needs_merge" if "needs_merge" in statuses
                   else "busy" if "busy" in statuses
                   else "applied")
        return {
            "task_id": task_id,
            "project_id": project_id,
            "attempt_id": selected_attempt_id,
            "verdict": verdict,
            "status": overall,
            "evidence_ref": evidence_ref,
            "updates": updates,
        }

    def reconcile_pending(self, *, limit: int = 100) -> dict:
        """Retry terminal project tasks whose current attempt lacks final receipts.

        This closes the crash window where the verdict was committed but the
        process died during evidence/context writing. Completed tasks are not
        replayed on every poll; only missing, failed or expired claims are visited.
        """
        results = []
        for task in self.store.all():
            if len(results) >= limit:
                break
            if (not task.get("project_id")
                    or task.get("verdict") not in TERMINAL_VERDICTS):
                continue
            attempt = self._select_attempt(task, None)
            attempt_id = str(attempt["id"])
            expected = {
                context_update_key(
                    task["task_id"], attempt_id, "progress",
                    f"Follow-up for task {task['task_id']}"),
                context_update_key(
                    task["task_id"], attempt_id, "knowledge",
                    f"Execution evidence for task {task['task_id']} attempt {attempt_id}"),
            }
            receipts = {row["idempotency_key"]: row
                        for row in self.store.context_update_receipts(task["task_id"])}
            complete = all(
                key in receipts and receipts[key].get("status")
                in ("applied", "needs_merge", "skipped")
                for key in expected)
            if complete:
                continue
            try:
                results.append(self.process_task(task["task_id"]))
            except Exception as exc:  # noqa: BLE001 - retry sweep is best-effort
                results.append({"task_id": task["task_id"], "status": "failed",
                                "error": f"{type(exc).__name__}: {exc}"})
        return {
            "checked": len(results),
            "applied": sum(1 for result in results
                           if result.get("status") in ("applied", "needs_merge")),
            "busy": sum(1 for result in results if result.get("status") == "busy"),
            "failed": sum(1 for result in results if result.get("status") == "failed"),
            "results": results,
        }

    def apply_candidate(self, candidate: ContextCandidate) -> dict:
        """Apply one candidate with a durable replay receipt."""
        self._validate_candidate(candidate)
        key = candidate.idempotency_key
        claim = self.store.claim_context_update(
            key, project_id=candidate.project_id,
            work_item_id=candidate.task_id, attempt_id=candidate.attempt_id,
            entry_kind=candidate.kind, entry_title=candidate.title,
            owner=self.owner, lease_s=self.claim_lease_s)
        if not claim.get("claimed"):
            if claim.get("busy"):
                return {"status": "busy", "idempotency_key": key,
                        "owner": claim.get("owner")}
            return {
                "status": claim.get("status"),
                "idempotency_key": key,
                "entry_id": claim.get("entry_id"),
                "replayed": True,
            }

        try:
            status, entry, detail = self._apply_claimed(candidate)
            receipt = self.store.complete_context_update(
                key, owner=self.owner, status=status,
                entry_id=(entry or {}).get("id"), payload=detail)
            return {
                "status": status,
                "idempotency_key": key,
                "entry_id": receipt.get("entry_id"),
                "entry_version": (entry or {}).get("version"),
                "replayed": False,
                **detail,
            }
        except Exception as exc:
            self.store.fail_context_update(
                key, owner=self.owner,
                error=f"{type(exc).__name__}: {exc}")
            raise

    def receipts(self, task_id: str) -> list[dict]:
        return self.store.context_update_receipts(task_id)

    def _select_attempt(self, task: dict, attempt_id: str | None) -> dict:
        attempts = self.store.attempts(task["task_id"])
        selected = (attempt_id or task.get("current_attempt_id")
                    or ((attempts[-1].get("id") if attempts else None)))
        selected = str(selected or "no-attempt")
        return next(
            (row for row in attempts if row.get("id") == selected),
            {"id": selected, "state": None, "reason": "no attempt row"})

    # ------------------------------------------------------------------ #
    # candidate derivation
    # ------------------------------------------------------------------ #
    def _derive_candidates(self, task: dict, attempt: dict,
                           evidence_ref: str) -> list[ContextCandidate]:
        task_id = task["task_id"]
        attempt_id = str(attempt.get("id") or "no-attempt")
        source = f"task:{task_id}"
        common_tags = ("m2-auto", f"task:{task_id}", f"attempt:{attempt_id}")
        progress = ContextCandidate(
            project_id=task["project_id"], task_id=task_id,
            attempt_id=attempt_id, kind="progress",
            title=f"Follow-up for task {task_id}",
            body=self._progress_body(task.get("verdict")),
            evidence_refs=(evidence_ref,), tags=common_tags,
            source=source)
        knowledge = ContextCandidate(
            project_id=task["project_id"], task_id=task_id,
            attempt_id=attempt_id, kind="knowledge",
            title=f"Execution evidence for task {task_id} attempt {attempt_id}",
            body=self._knowledge_body(task, attempt),
            evidence_refs=(evidence_ref,), tags=common_tags + ("execution-evidence",),
            source=source)
        return [progress, knowledge]

    @staticmethod
    def _progress_body(verdict: str | None) -> str:
        # Narrative only: authoritative state/verdict remains in the Ledger.
        if verdict == Verdict.VERIFIED.value:
            return ("Declared deliverables were accepted. Next step: review the accepted "
                    "artifacts and choose the next project objective.")
        if verdict == Verdict.REJECTED.value:
            return ("The declared outcome was not accepted. Blocker: inspect failed "
                    "assertions and execution warnings before planning a retry.")
        return ("The available evidence was insufficient to accept or reject the outcome. "
                "Next step: add measurable artifact assertions or collect the missing evidence.")

    def _knowledge_body(self, task: dict, attempt: dict) -> str:
        lines = ["Candidate facts extracted deterministically from execution evidence:"]
        assertions = task.get("assertion_results") or []
        for result in assertions[:20]:
            outcome = "passed" if result.get("passed") else "failed"
            observed = self._clean_text(str(result.get("observed") or ""), 240)
            fact = (f"- assertion {result.get('kind') or 'unknown'} on "
                    f"{result.get('target') or '<unspecified>'}: {outcome}")
            if observed:
                fact += f" ({observed})"
            lines.append(fact)
        warning_codes = []
        for warning in task.get("warnings") or []:
            code = str(warning).split(":", 1)[0].strip() or "warning"
            if code not in warning_codes:
                warning_codes.append(code)
        if warning_codes:
            lines.append("- warning codes: " + ", ".join(warning_codes[:20]))
        state = attempt.get("state") or "unknown"
        reason = self._clean_text(str(attempt.get("reason") or ""), 160)
        lines.append(f"- attempt lifecycle: state={state}; reason={reason or 'unspecified'}")
        event_types = []
        for event in self.store.events(task["task_id"], limit=100):
            type_ = str(event.get("type") or "")
            if type_ and type_ not in event_types:
                event_types.append(type_)
        if event_types:
            lines.append("- observed event types: " + ", ".join(event_types[:30]))
        if not assertions:
            lines.append("- no artifact assertion result was available; no technical claim is proven")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # CAS application and conflict preservation
    # ------------------------------------------------------------------ #
    def _apply_claimed(self, candidate: ContextCandidate
                       ) -> tuple[str, dict, dict]:
        key = candidate.idempotency_key
        marker = "m2-update:" + key[:16]
        existing = self.store.context_entry_by_title(
            candidate.project_id, candidate.kind, candidate.title)
        if existing and marker in (existing.get("tags") or []):
            return "applied", existing, {"recovered_after_partial_commit": True}

        if candidate.expected_version is not None:
            actual = int((existing or {}).get("version") or 0)
            if actual != candidate.expected_version:
                return self._preserve_conflict(candidate, existing, marker, actual)

        if not existing:
            try:
                created = self.context.add_context_entry(candidate.project_id, {
                    "kind": candidate.kind,
                    "title": candidate.title,
                    "body": candidate.body,
                    "tags": self._tags(candidate.tags, marker),
                    "verification": "candidate",
                    "security_domain": "local",
                    "source": candidate.source or f"task:{candidate.task_id}",
                    "evidence_refs": list(candidate.evidence_refs),
                })
                return "applied", created, {"operation": "insert"}
            except (ContextError, sqlite3.IntegrityError):
                existing = self.store.context_entry_by_title(
                    candidate.project_id, candidate.kind, candidate.title)
                if existing and marker in (existing.get("tags") or []):
                    return "applied", existing, {"recovered_after_partial_commit": True}
                return self._preserve_conflict(
                    candidate, existing, marker,
                    int((existing or {}).get("version") or 0))

        # Automatic updates never overwrite human/verified material. That is a
        # merge proposal, even if the title happens to match our deterministic one.
        if ("m2-auto" not in (existing.get("tags") or [])
                or existing.get("verification") != "candidate"):
            return self._preserve_conflict(
                candidate, existing, marker, int(existing.get("version") or 0))

        expected = (candidate.expected_version if candidate.expected_version is not None
                    else int(existing.get("version") or 0))
        try:
            updated = self.context.update_context_entry(
                candidate.project_id, existing["id"], expected,
                {"body": candidate.body,
                 "tags": self._tags(existing.get("tags") or [], *candidate.tags, marker),
                 "verification": "candidate",
                 "security_domain": "local",
                 "source": candidate.source or f"task:{candidate.task_id}",
                 "evidence_refs": self._unique(
                     [*(existing.get("evidence_refs") or []), *candidate.evidence_refs])},
                changed_by=candidate.source or f"task:{candidate.task_id}",
                note="M2 deterministic context update")
            return "applied", updated, {"operation": "update"}
        except ContextVersionConflict as conflict:
            latest = self.store.get_context_entry(existing["id"])
            return self._preserve_conflict(
                candidate, latest, marker, conflict.actual)

    def _preserve_conflict(self, candidate: ContextCandidate,
                           target: dict | None, marker: str,
                           actual_version: int) -> tuple[str, dict, dict]:
        key = candidate.idempotency_key
        title = f"{candidate.title} [needs-merge {key[:8]}]"
        existing = self.store.context_entry_by_title(
            candidate.project_id, candidate.kind, title)
        if existing:
            return "needs_merge", existing, {
                "operation": "preserve_conflict", "target_entry_id": (target or {}).get("id"),
                "actual_version": actual_version}
        conflict = self.context.add_context_entry(candidate.project_id, {
            "kind": candidate.kind,
            "title": title,
            "body": candidate.body,
            "tags": self._tags(candidate.tags, marker, "needs_merge",
                               "merge-target:" + str((target or {}).get("id") or "missing")),
            "verification": "candidate",
            "security_domain": "local",
            "source": candidate.source or f"task:{candidate.task_id}",
            "evidence_refs": list(candidate.evidence_refs),
        })
        return "needs_merge", conflict, {
            "operation": "preserve_conflict", "target_entry_id": (target or {}).get("id"),
            "actual_version": actual_version}

    # ------------------------------------------------------------------ #
    # evidence
    # ------------------------------------------------------------------ #
    def _evidence_ref(self, task: dict, attempt: dict) -> str:
        task_part = self._safe_part(task["task_id"])
        attempt_part = self._safe_part(str(attempt.get("id") or "no-attempt"))
        relative = PurePosixPath("evidence") / "project-updates" / task_part / \
            f"{attempt_part}.json"
        return relative.as_posix()

    def _write_task_evidence(self, task: dict, attempt: dict,
                             evidence_ref: str) -> None:
        relative = PurePosixPath(evidence_ref)
        path = self.repo_root.joinpath(*relative.parts)
        if not path.exists():
            write_evidence(path, {
                "schema": "project-context-update/v1",
                "task": {
                    "task_id": task["task_id"],
                    "project_id": task.get("project_id"),
                    "title": task.get("title"),
                    "state": task.get("state"),
                    "verdict": task.get("verdict"),
                    "assertion_results": task.get("assertion_results") or [],
                    "warnings": task.get("warnings") or [],
                    "finished_at": task.get("finished_at"),
                },
                "attempt": {
                    "id": attempt.get("id"),
                    "worker_job_id": attempt.get("worker_job_id"),
                    "state": attempt.get("state"),
                    "reason": attempt.get("reason"),
                    "created_at": attempt.get("created_at"),
                    "ended_at": attempt.get("ended_at"),
                },
                "events": self.store.events(task["task_id"], limit=100),
            })

    def _validate_candidate(self, candidate: ContextCandidate) -> None:
        if candidate.kind not in AUTO_KINDS:
            raise ContextError(
                "automatic context updates may only write progress or knowledge")
        if not candidate.body.strip() or not candidate.title.strip():
            raise ContextError("automatic context candidate must have a title and body")
        task = self.store.get(candidate.task_id)
        if not task:
            raise KeyError(f"no task {candidate.task_id}")
        if task.get("project_id") != candidate.project_id:
            raise ContextError(
                f"task {candidate.task_id} belongs to project {task.get('project_id')}, "
                f"not {candidate.project_id}")
        if not candidate.evidence_refs:
            raise ContextError("automatic context candidates require evidence_refs")
        for ref in candidate.evidence_refs:
            posix = PurePosixPath(ref)
            if not posix.parts or posix.parts[0] != "evidence" or ".." in posix.parts:
                raise ContextError(f"evidence reference must stay under evidence/: {ref}")
            if not self.repo_root.joinpath(*posix.parts).is_file():
                raise ContextError(f"evidence reference does not exist: {ref}")

    @staticmethod
    def _safe_part(value: str) -> str:
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in value)
        return safe[:120] or "unknown"

    @staticmethod
    def _unique(values: Iterable[str]) -> list[str]:
        return list(dict.fromkeys(str(value) for value in values if str(value)))

    @classmethod
    def _tags(cls, *groups: Any) -> list[str]:
        flat: list[str] = []
        for group in groups:
            if isinstance(group, str):
                flat.append(group)
            else:
                flat.extend(str(value) for value in group)
        return cls._unique(flat)

    @staticmethod
    def _clean_text(value: str, limit: int) -> str:
        return sanitize_text(value)[0].replace("\n", " ").strip()[:limit]
