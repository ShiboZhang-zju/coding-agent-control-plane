"""Deterministic, project-local Session handoff preparation (M3).

A handoff is not a second task system.  It is a normal WorkItem whose frozen
prompt and metadata name the source Attempt, observed Session, usable Git
checkpoint and exact Project Context version.  This module performs no Worker
calls and writes nothing; :class:`controlplane.project_service.ProjectService`
owns the eventual enqueue.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from adapters.base import TaskState, Verdict
from controlplane.context_builder import BuiltContext, ContextBuilder
from controlplane.store import TaskStore


class HandoffError(ValueError):
    """Invalid or unsafe handoff request."""


class HandoffNotReady(HandoffError):
    """Preparation found blockers that require an explicit human action."""

    def __init__(self, blockers: list[str], prepared: dict | None = None):
        super().__init__("handoff is not ready: " + "; ".join(blockers))
        self.blockers = blockers
        self.prepared = prepared or {}


CHECKPOINT_MODES = {"integrated", "reachable"}


class HandoffIdempotencyConflict(HandoffError):
    """An idempotency key already names different handoff content."""


class HandoffStale(HandoffError):
    """The Project Context/checkpoint changed after the caller prepared it."""


def handoff_signature(payload: dict) -> str:
    """Content signature used for prepare/submit drift and idempotency checks."""
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:32]


@dataclass
class PreparedHandoff:
    ready: bool
    requires_human: bool
    blockers: list[str]
    project_id: str
    source_work_item_id: str
    source_attempt_id: str | None
    source_session_reference: dict | None
    source_facts: dict
    checkpoint_ref: str | None
    checkpoint: dict
    target_workspace_id: str | None
    target_workspace_root: str | None
    context_version: int | None
    context_entry_refs: list[str] = field(default_factory=list)
    verified_facts: list[dict] = field(default_factory=list)
    candidate_knowledge: list[dict] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    original_task: str = ""
    effective_prompt: str = ""
    handoff_signature: str = ""

    def to_dict(self) -> dict:
        return {
            "ready": self.ready,
            "requires_human": self.requires_human,
            "blockers": list(self.blockers),
            "project_id": self.project_id,
            "source_work_item_id": self.source_work_item_id,
            "source_attempt_id": self.source_attempt_id,
            "source_session_reference": dict(self.source_session_reference or {}),
            "source_facts": dict(self.source_facts),
            "checkpoint_ref": self.checkpoint_ref,
            "checkpoint": dict(self.checkpoint),
            "target_workspace_id": self.target_workspace_id,
            "target_workspace_root": self.target_workspace_root,
            "context_version": self.context_version,
            "context_entry_refs": list(self.context_entry_refs),
            "verified_facts": [dict(item) for item in self.verified_facts],
            "candidate_knowledge": [dict(item) for item in self.candidate_knowledge],
            "unresolved": list(self.unresolved),
            "original_task": self.original_task,
            "effective_prompt": self.effective_prompt,
            "handoff_signature": self.handoff_signature,
        }


class HandoffPlanner:
    """Read-only preparation over the Ledger, Git and Project Context."""

    def __init__(self, store: TaskStore, builder: ContextBuilder):
        self.store = store
        self.builder = builder

    def prepare(self, source_work_item_id: str, task: str, *,
                target_workspace_id: str | None = None,
                tags: Iterable[str] = (),
                prerequisites: Iterable[str] = (),
                require_verified_constraints: bool = False,
                extra_references: Iterable[str] = (),
                require_checkpoint: bool = True,
                checkpoint_mode: str = "integrated") -> PreparedHandoff:
        source = self.store.get(source_work_item_id)
        if not source:
            raise KeyError(f"no work item {source_work_item_id}")
        if checkpoint_mode not in CHECKPOINT_MODES:
            raise HandoffError(
                f"checkpoint_mode must be one of {sorted(CHECKPOINT_MODES)}")
        project_id = source.get("project_id")
        if not project_id:
            raise HandoffError(
                f"source work item {source_work_item_id} is not project-bound")
        project = self.store.get_project(project_id)
        if not project:
            raise HandoffError(f"source project was deleted: {project_id}")

        blockers: list[str] = []
        unresolved: list[str] = []
        if not project.get("active"):
            blockers.append(f"project is disabled: {project.get('name')}")

        state = source.get("state")
        if state in {
                TaskState.QUEUED.value, TaskState.STARTING.value,
                TaskState.RUNNING.value, TaskState.WAITING_FOR_APPROVAL.value}:
            blockers.append(f"source work item is still active: state={state}")
        elif state in {TaskState.DISPATCH_UNKNOWN.value, TaskState.UNKNOWN.value}:
            blockers.append(f"source work item requires reconciliation: state={state}")
        elif state == TaskState.LOST.value:
            blockers.append("source work item is lost; checkpoint provenance is not trusted")
        elif state not in {
                TaskState.SUCCEEDED.value, TaskState.FAILED.value,
                TaskState.CANCELLED.value}:
            blockers.append(f"source work item has no handoff-safe terminal state: {state}")
        if (source.get("metadata") or {}).get("suspected_lost"):
            blockers.append("source work item is suspected_lost and needs human reconciliation")
        source_verdict = source.get("verdict")
        if source_verdict == Verdict.PENDING.value:
            blockers.append("source acceptance verdict is still pending")
        elif source_verdict not in {
                Verdict.VERIFIED.value, Verdict.REJECTED.value,
                Verdict.INCONCLUSIVE.value}:
            blockers.append(
                f"source has no recognized acceptance verdict: {source_verdict}")

        attempt_id = source.get("current_attempt_id")
        attempts = {item["id"]: item for item in self.store.attempts(source_work_item_id)}
        attempt = attempts.get(attempt_id)
        if not attempt:
            blockers.append("source work item has no current durable Attempt")
        session_ref = (self.store.session_reference_for_attempt(attempt_id)
                       if attempt_id else None)
        if not session_ref:
            blockers.append("source Attempt has no observed Session binding")
        elif (session_ref.get("project_id") != project_id
              or session_ref.get("workspace_id") != source.get("workspace_id")
              or session_ref.get("work_item_id") != source_work_item_id
              or session_ref.get("attempt_id") != attempt_id):
            blockers.append("source Session binding has mismatched ownership")
        elif (not session_ref.get("session_id")
              or not session_ref.get("worker_instance_id")
              or session_ref.get("node_id") != "local"):
            blockers.append(
                "source Session binding is incomplete or not on the supported local node")

        target = self._target_workspace(project_id, target_workspace_id)
        if target is None:
            blockers.append(
                f"target workspace does not belong to project {project_id}: "
                f"{target_workspace_id}")
            target_root = None
        else:
            target_root = target.get("local_root")
            if not target.get("active"):
                blockers.append(f"target workspace is inactive: {target['id']}")
            if target.get("node_id") != "local":
                blockers.append(
                    f"target workspace is on node {target.get('node_id')!r}; "
                    "remote execution is not implemented")
            if not target_root or not Path(target_root).is_dir():
                blockers.append(f"target workspace root does not exist: {target_root}")
            elif not self._within_project(project["root"], target_root):
                blockers.append(
                    f"target workspace escapes project boundary: {target_root}")

        checkpoint_ref = source.get("head_commit")
        checkpoint = self._checkpoint(
            checkpoint_ref, target_root, source.get("worktree"), require_checkpoint,
            mode=checkpoint_mode)
        checkpoint["mode"] = checkpoint_mode
        blockers.extend(checkpoint.pop("blockers"))
        unresolved.extend(source.get("warnings") or [])
        if source.get("verdict") != Verdict.VERIFIED.value:
            unresolved.append(
                f"source verdict is {source.get('verdict')}; it is preserved, not promoted")

        built: BuiltContext | None = None
        if target is not None and project.get("active"):
            try:
                built = self.builder.build(
                    project_id, task, tags=tags, prerequisites=prerequisites,
                    require_verified_constraints=require_verified_constraints,
                    workspace_id=target["id"], extra_references=[
                        f"handoff source work item: {source_work_item_id}",
                        f"handoff source attempt: {attempt_id or 'missing'}",
                        f"handoff checkpoint: {checkpoint_ref or 'none'}",
                        *list(extra_references),
                    ])
            except (KeyError, ValueError) as exc:
                blockers.append(f"context build failed: {type(exc).__name__}: {exc}")
        if built:
            blockers.extend(built.gaps)
            unresolved.extend(built.warnings)

        entries = {
            entry["id"]: entry
            for entry in self.store.context_entries(project_id)
        }
        selected = [entries[entry_id] for entry_id in (built.entry_refs if built else [])
                    if entry_id in entries]
        needs_merge = [entry for entry in selected
                       if "needs_merge" in (entry.get("tags") or [])]
        if needs_merge:
            blockers.append(
                "selected Project Context contains needs_merge candidates: "
                + ", ".join(entry["id"] for entry in needs_merge))
        verified_facts = [self._fact(entry) for entry in selected
                          if entry.get("verification") == "verified"]
        candidate_knowledge = [self._fact(entry) for entry in selected
                               if entry.get("verification") != "verified"]
        if candidate_knowledge:
            unresolved.append(
                "candidate/unverified context remains non-authoritative: "
                + ", ".join(item["id"] for item in candidate_knowledge))

        effective_prompt = ""
        source_facts = self._source_facts(source)
        if built:
            effective_prompt = self._render_prompt(
                built.effective_prompt, source, attempt_id, session_ref,
                checkpoint_ref, target["id"] if target else None,
                source_facts, checkpoint_mode=checkpoint_mode)

        # Stable de-duplication while preserving the order of the first reason.
        blockers = list(dict.fromkeys(blockers))
        unresolved = list(dict.fromkeys(str(item) for item in unresolved if str(item)))
        signature_payload = {
            "project_id": project_id,
            "source_work_item_id": source_work_item_id,
            "source_attempt_id": attempt_id,
            "source_session_binding": {
                key: (session_ref or {}).get(key)
                for key in ("id", "node_id", "worker_instance_id", "adapter",
                            "session_id", "workspace_id", "work_item_id", "attempt_id")
            },
            "source_facts": source_facts,
            "checkpoint_ref": checkpoint_ref,
            "checkpoint": checkpoint,
            "target_workspace_id": target["id"] if target else target_workspace_id,
            "context_version": built.context_version if built else None,
            "context_entry_refs": built.entry_refs if built else [],
            "original_task": task,
            "effective_prompt": effective_prompt,
            "unresolved": unresolved,
            "require_checkpoint": require_checkpoint,
        }
        return PreparedHandoff(
            ready=not blockers,
            requires_human=bool(blockers),
            blockers=blockers,
            project_id=project_id,
            source_work_item_id=source_work_item_id,
            source_attempt_id=attempt_id,
            source_session_reference=session_ref,
            source_facts=source_facts,
            checkpoint_ref=checkpoint_ref,
            checkpoint=checkpoint,
            target_workspace_id=target["id"] if target else target_workspace_id,
            target_workspace_root=target_root,
            context_version=built.context_version if built else None,
            context_entry_refs=list(built.entry_refs if built else []),
            verified_facts=verified_facts,
            candidate_knowledge=candidate_knowledge,
            unresolved=unresolved,
            original_task=task,
            effective_prompt=effective_prompt,
            handoff_signature=handoff_signature(signature_payload),
        )

    def _target_workspace(self, project_id: str,
                          workspace_id: str | None) -> dict | None:
        if workspace_id:
            workspace = self.store.get_workspace(workspace_id)
            if workspace and workspace.get("project_id") == project_id:
                return workspace
            return None
        # prepare() is read-only.  Migration/registration is responsible for
        # creating the default row; never repair the Ledger as a side effect of
        # merely previewing a handoff.
        return self.store.default_workspace(project_id)

    @staticmethod
    def _fact(entry: dict) -> dict:
        return {
            "id": entry["id"], "kind": entry["kind"],
            "title": entry["title"], "body": entry["body"],
            "verification": entry["verification"], "version": entry["version"],
        }

    @staticmethod
    def _source_facts(source: dict) -> dict:
        return {
            "title": source.get("title"),
            "original_task": source.get("original_task"),
            "state": source.get("state"),
            "verdict": source.get("verdict"),
            "assertion_results": list(source.get("assertion_results") or []),
            "artifacts": list(source.get("artifacts") or []),
            "warnings": list(source.get("warnings") or []),
        }

    @staticmethod
    def _within_project(project_root: str, workspace_root: str) -> bool:
        try:
            Path(workspace_root).expanduser().resolve().relative_to(Path(project_root))
            return True
        except ValueError:
            return False

    @staticmethod
    def _git(root: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", root, *args], check=False, capture_output=True,
            text=True, timeout=10)

    def _checkpoint(self, checkpoint_ref: str | None,
                    target_root: str | None, source_worktree: str | None,
                    required: bool, *, mode: str = "integrated") -> dict:
        result = {
            "kind": "git_commit" if checkpoint_ref else "context_only",
            "mode": mode,
            "source_worktree": source_worktree,
            "source_head": None,
            "source_checkpoint": None,
            "source_clean": None,
            "target_head": None,
            "available": False,
            "integrated": False,
            "blockers": [],
        }
        if not checkpoint_ref:
            if required:
                result["blockers"].append(
                    "source has no recorded Git checkpoint (head_commit)")
            else:
                result["available"] = True
                result["integrated"] = True
            return result
        if not target_root or not Path(target_root).is_dir():
            result["blockers"].append("cannot validate checkpoint without a local target workspace")
            return result
        try:
            if source_worktree and Path(source_worktree).is_dir():
                source_head = self._git(source_worktree, "rev-parse", "HEAD")
                source_checkpoint = self._git(
                    source_worktree, "rev-parse", checkpoint_ref)
                source_status = self._git(source_worktree, "status", "--porcelain")
                if source_head.returncode == 0:
                    result["source_head"] = source_head.stdout.strip()
                if source_checkpoint.returncode == 0:
                    result["source_checkpoint"] = source_checkpoint.stdout.strip()
                result["source_clean"] = (
                    source_status.returncode == 0 and not source_status.stdout.strip())
            head = self._git(target_root, "rev-parse", "HEAD")
            exists = self._git(target_root, "cat-file", "-e", f"{checkpoint_ref}^{{commit}}")
            integrated = self._git(
                target_root, "merge-base", "--is-ancestor", checkpoint_ref, "HEAD")
        except (OSError, subprocess.SubprocessError) as exc:
            result["blockers"].append(
                f"checkpoint validation failed: {type(exc).__name__}: {exc}")
            return result
        if source_worktree and Path(source_worktree).is_dir():
            if (not result["source_checkpoint"]
                    or result["source_head"] != result["source_checkpoint"]):
                result["blockers"].append(
                    "source worktree HEAD no longer matches the recorded checkpoint")
            if result["source_clean"] is not True:
                result["blockers"].append(
                    "source worktree has uncommitted changes not captured by the checkpoint")
        if head.returncode == 0:
            result["target_head"] = head.stdout.strip()
        result["available"] = exists.returncode == 0
        result["integrated"] = integrated.returncode == 0
        if not result["available"]:
            result["blockers"].append(
                f"checkpoint {checkpoint_ref} is not available in target workspace")
        elif mode == "integrated" and not result["integrated"]:
            result["blockers"].append(
                f"checkpoint {checkpoint_ref} is not contained in target workspace HEAD; "
                "merge it or select a compatible workspace explicitly")
        return result

    def capture_source_checkpoint(self, source_work_item_id: str) -> str | None:
        """Deterministically turn the source session's worktree into a commit.

        The control plane — not the Worker and not a model — creates the
        checkpoint: uncommitted changes in the source worktree are committed on
        that worktree's own isolation branch, and the resulting HEAD is stored
        as the source's ``head_commit``.  This never touches the project's
        default branch.  Returns the checkpoint commit, or None when there is
        no usable worktree (context-only handoff).
        """
        source = self.store.get(source_work_item_id)
        if not source:
            raise KeyError(f"no work item {source_work_item_id}")
        worktree = source.get("worktree")
        if not worktree or not Path(worktree).is_dir():
            return source.get("head_commit")
        try:
            status = self._git(worktree, "status", "--porcelain")
            head = self._git(worktree, "rev-parse", "HEAD")
        except (OSError, subprocess.SubprocessError):
            return source.get("head_commit")
        if status.returncode != 0 or head.returncode != 0:
            return source.get("head_commit")
        checkpoint = head.stdout.strip()
        if status.stdout.strip():
            args = ["-c", "user.name=control-plane",
                    "-c", "user.email=control-plane@handoff.local",
                    "commit", "-q", "-m",
                    f"control-plane handoff checkpoint for {source_work_item_id}"]
            for step in (["add", "-A"], args):
                proc = self._git(worktree, *step)
                if proc.returncode != 0:
                    raise HandoffError(
                        f"cannot capture handoff checkpoint in {worktree}: "
                        f"git {step[:2]} failed: {proc.stderr.strip()}")
            new_head = self._git(worktree, "rev-parse", "HEAD")
            if new_head.returncode != 0:
                raise HandoffError(
                    f"cannot read checkpoint HEAD in {worktree}: "
                    f"{new_head.stderr.strip()}")
            checkpoint = new_head.stdout.strip()
        self.store.update(source_work_item_id, head_commit=checkpoint)
        return checkpoint

    @staticmethod
    def _render_prompt(base_prompt: str, source: dict,
                       attempt_id: str | None, session_ref: dict | None,
                       checkpoint_ref: str | None,
                       target_workspace_id: str | None,
                       source_facts: dict, *, checkpoint_mode: str = "integrated") -> str:
        lines = [base_prompt.rstrip(), "", "# SESSION HANDOFF",
                 "- Start a new Session; do not resume or depend on the old chat transcript.",
                 f"- Source WorkItem: {source['task_id']}",
                 f"- Source Attempt: {attempt_id or 'missing'}",
                 f"- Source SessionReference: {(session_ref or {}).get('id') or 'missing'}",
                 f"- Source task: {source_facts.get('original_task') or source.get('title')}",
                 f"- Source ledger state/verdict: {source.get('state')} / {source.get('verdict')}",
                 "- Source assertion results: "
                 + json.dumps(source_facts.get("assertion_results") or [],
                              ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                 f"- Git checkpoint: {checkpoint_ref or 'context-only (explicit)'}"
                 + (f" (mode: {checkpoint_mode})" if checkpoint_ref else ""),
                 f"- Target Workspace: {target_workspace_id or 'missing'}",
                 "- The Ledger verdict and Project Context are authoritative; "
                 "candidate/unverified entries remain non-authoritative.",
                 "- If the checkpoint or stated context is unavailable, stop and report the gap."]
        if checkpoint_ref and checkpoint_mode == "reachable":
            lines.append(
                f"- Before starting, run `git merge {checkpoint_ref}` in your working "
                "branch so the previously accepted code from the source session is "
                "available. Resolve conflicts conservatively and report them if you "
                "cannot. Never push or merge into the project's default branch.")
        return "\n".join(lines).rstrip() + "\n"
