"""Vendor-neutral Worker Adapter contract for the coding-agent control plane.

Design rules (see docs/adapter-contract.md):
  1. Nothing in this module may reference a specific vendor. No CodeBuddy types,
     no vendor field names, no vendor state strings.
  2. A worker's *reported* terminal state is not an acceptance decision.
     `TaskSnapshot.worker_state` is what the worker claims; `TaskSnapshot.verdict`
     is what the control plane verified. They are deliberately separate fields.
  3. Every adapter must be able to answer "what do I actually know" without the
     worker being reachable (`reconcile()` + `lost`).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterable, Protocol, runtime_checkable


class TaskState(str, enum.Enum):
    """Normalized lifecycle state shared by every worker adapter."""

    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LOST = "lost"
    UNKNOWN = "unknown"
    # dispatch outcome is genuinely unknown (the worker errored but may have
    # created the job). Never retried blindly: reconcile first, then decide.
    DISPATCH_UNKNOWN = "dispatch_unknown"

    @property
    def terminal(self) -> bool:
        return self in (TaskState.SUCCEEDED, TaskState.FAILED,
                        TaskState.CANCELLED, TaskState.LOST)


class Verdict(str, enum.Enum):
    """Control-plane acceptance verdict, independent of worker state."""

    PENDING = "pending"          # still running / no assertions evaluated yet
    VERIFIED = "verified"        # all artifact assertions passed AND worker did not fail
    REJECTED = "rejected"        # assertions failed, worker failed, or approval denied
    INCONCLUSIVE = "inconclusive"  # cannot decide (e.g. no observable artifact)


class CapabilityLevel(str, enum.Enum):
    NATIVE = "native"                      # provided by the worker as-is
    CONFIG = "config"                      # native but needs a flag/setting
    ADAPTER = "adapter"                    # needs adapter-side code
    PLANE = "plane"                        # needs control-plane-side code
    UNSUPPORTED = "unsupported"            # worker cannot do it
    UNVERIFIED = "unverified"              # not tested on this build


@dataclass
class Capability:
    name: str
    level: CapabilityLevel
    evidence: str = ""
    notes: str = ""


class UnsupportedOperation(RuntimeError):
    """Raised when a worker genuinely cannot perform the operation."""


# --------------------------------------------------------------------------- #
# data carried across the boundary
# --------------------------------------------------------------------------- #
@dataclass
class ArtifactAssertion:
    """A machine-checkable acceptance criterion declared at dispatch time."""

    kind: str                 # "file_exists" | "file_contains" | "git_commit" | "git_clean" | "path_absent"
    target: str               # relative path (repo-relative) or branch name
    value: str | None = None  # expected substring / commit subject fragment
    description: str = ""

    def to_dict(self) -> dict:
        return {"kind": self.kind, "target": self.target,
                "value": self.value, "description": self.description}


@dataclass
class DispatchRequest:
    prompt: str
    cwd: str
    task_id: str = ""
    title: str = ""
    mode: str = "agent"              # "agent" (model session) | "shell" (direct command)
    isolation: str = "worktree"      # "worktree" | "none"
    assertions: list[ArtifactAssertion] = field(default_factory=list)
    idempotency_key: str = ""
    timeout_s: int = 900
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskSnapshot:
    """One task as the control plane sees it."""

    task_id: str
    worker_job_id: str | None
    state: TaskState
    verdict: Verdict = Verdict.PENDING
    title: str = ""
    cwd: str = ""
    pid: int | None = None
    session_id: str | None = None
    worktree: str | None = None
    branch: str | None = None
    commit: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    detail: str = ""
    artifacts: list[str] = field(default_factory=list)
    assertion_results: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)   # vendor-only fields live here

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["state"] = self.state.value
        d["verdict"] = self.verdict.value
        return d


@dataclass
class TaskEvent:
    """Normalized progress event. `event_id`/`cursor` are vendor-provided or None."""

    type: str                     # dispatched|state_changed|log|approval_requested|terminal
    task_id: str
    worker_job_id: str | None = None
    session_id: str | None = None
    state: TaskState | None = None
    ts: float | None = None
    event_id: str | None = None
    cursor: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["state"] = self.state.value if self.state else None
        return d


@dataclass
class DispatchResult:
    task_id: str
    worker_job_id: str | None
    accepted: bool
    # True when the adapter cannot prove whether a job was created. The plane
    # must reconcile (look for the job) before it may dispatch again.
    uncertain: bool = False
    warnings: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


@dataclass
class DispatchPolicy:
    """Explicit dispatch policy. There is no implicit "just run it" default.

    Automated dispatch (Foreman or any unattended caller) must run as an agent
    session inside its own workspace. Un-isolated shell execution has to be
    requested deliberately, and it is recorded as a policy risk in the ledger.
    """

    mode: str = "agent"          # "agent" | "shell"
    isolation: str = "worktree"  # "worktree" | "none"
    permission_mode: str = "acceptEdits"
    allow_unisolated_shell: bool = False
    risk: str = "auto"           # "auto" | "low" | "medium" | "high"

    def validate(self) -> list[str]:
        """Return policy risks; raise-free so callers decide."""
        risks = []
        if self.mode == "shell" and self.isolation != "worktree":
            if not self.allow_unisolated_shell:
                raise ValueError(
                    "refusing dispatch: mode=shell without isolation. Shell jobs are not "
                    "filesystem-isolated by the worker; pass allow_unisolated_shell=True "
                    "explicitly if you accept that risk (see capability-matrix.md).")
            risks.append("UNISOLATED_SHELL: shell job has no filesystem isolation")
        if self.permission_mode in ("bypassPermissions",):
            raise ValueError("refusing dispatch: bypassPermissions is forbidden by policy")
        if self.downgrade_attempted():
            risks.append(
                f"RISK_DOWNGRADE_IGNORED: declared risk={self.risk} ignored, "
                f"effective={self.effective_risk()} (intrinsic={self.intrinsic_risk()}); "
                "approval is still required")
        return risks

    def intrinsic_risk(self) -> str:
        """What the system derives from the policy, regardless of what was declared.

        High risk = the task can touch things outside a disposable worktree
        (un-isolated shell) or bypass the approval prompt.
        """
        if self.mode == "shell" and self.isolation != "worktree":
            return "high"
        if self.permission_mode in ("bypassPermissions", "dontAsk"):
            return "high"
        return "low"

    def effective_risk(self) -> str:
        """Declared risk can only *raise* the intrinsic one, never lower it.

        Otherwise a caller could launder an un-isolated shell job with
        `risk="low"` and skip approval entirely (review T21).
        """
        intrinsic = self.intrinsic_risk()
        declared = intrinsic if self.risk == "auto" else self.risk
        return max((intrinsic, declared), key=RISK_ORDER.__getitem__)

    def downgrade_attempted(self) -> bool:
        if self.risk == "auto":
            return False
        return RISK_ORDER.get(self.risk, 0) < RISK_ORDER[self.intrinsic_risk()]

    def requires_approval(self) -> bool:
        return self.effective_risk() == "high"

    def to_dict(self) -> dict:
        return {"mode": self.mode, "isolation": self.isolation,
                "permission_mode": self.permission_mode,
                "allow_unisolated_shell": self.allow_unisolated_shell,
                "risk": self.effective_risk()}


# --------------------------------------------------------------------------- #
# the contract
# --------------------------------------------------------------------------- #
@runtime_checkable
class WorkerAdapter(Protocol):
    """Minimal adapter surface required by the PoC control plane."""

    name: str

    def capabilities(self) -> dict[str, Capability]:
        """Static capability matrix, each entry pointing at evidence."""

    def dispatch(self, request: DispatchRequest) -> DispatchResult:
        """Start a task. Must be idempotent on `request.idempotency_key` if supported,
        otherwise must report the missing guarantee in `DispatchResult.warnings`."""

    def get_task(self, task_id: str) -> TaskSnapshot:
        """Current snapshot for one task."""

    def list_tasks(self, include_terminal: bool = True) -> list[TaskSnapshot]:
        """All tasks the control plane knows about."""

    def stream_events(self, task_id: str | None = None) -> Iterable[TaskEvent]:
        """Progress events. If the worker cannot replay dropped events the adapter
        must set `TaskEvent.cursor` to None rather than fake resumability."""

    def cancel(self, task_id: str) -> TaskSnapshot:
        """Ask the worker to stop; return the post-cancel snapshot."""

    def resume(self, task_id: str) -> TaskSnapshot:
        """Re-attach to an archived conversation (conversation recovery)."""

    def respawn(self, task_id: str) -> TaskSnapshot:
        """Start a new worker process bound to the same conversation."""

    def request_approval(self, task_id: str, decision: str, note: str = "") -> dict:
        """Record a human decision. Adapters that cannot deliver a decision through
        an API must raise UnsupportedOperation instead of pretending."""

    def collect_artifacts(self, task_id: str) -> list[str]:
        """Enumerate observable artifacts (files, commits, logs)."""

    def reconcile(self) -> dict:
        """Re-query the worker and repair local state. Reports what changed and
        which tasks are now `lost`."""
