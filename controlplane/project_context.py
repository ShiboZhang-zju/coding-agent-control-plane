"""Durable, versioned, per-project knowledge store (M1).

Why this exists
---------------
A coding-agent session is disposable: it dies with the process, the machine, or
the next `codebuddy --serve` restart. What must *not* die is what the project
already knows — its background, its hard constraints, the conclusions previous
sessions paid for. This module is that memory, and it is deliberately **not** an
agent: there is no resident LLM per project, only rows in the same SQLite ledger
the control plane already uses.

Design rules
------------
1. **Project isolation.** Every read and write is keyed by ``project_id``. There
   is no API that returns entries across projects, so a builder bug cannot leak
   project A's constraints into project B's prompt.
2. **Optimistic concurrency.** Updates carry ``expected_version`` and are applied
   with a compare-and-swap; a stale writer gets ``None`` instead of silently
   overwriting a newer fact. Every successful update also writes a revision row
   holding the *previous* snapshot, so history is auditable.
3. **Provenance.** Each entry records ``source`` (who/what produced it),
   ``evidence_refs`` (paths under ``evidence/``) and timestamps.
4. **Verification state.** ``verified`` / ``candidate`` / ``unverified``. Only
   ``verified`` entries may be injected as hard constraints — see
   :func:`is_hard_constraint`. An agent's guess must never become policy.
5. **Security domain.** ``local`` (restricted to this machine/network) vs
   ``shareable`` (a summary safe to hand to another node). Nothing here syncs
   anything anywhere; the flag only lets a future M5 sync refuse to move
   restricted rows.

Progress is *not* stored here as authoritative task state: task status is derived
from the ledger by :mod:`controlplane.project_service`. ``progress`` entries hold
the human/agent narrative (blockers, next steps), never a second source of truth
for "did task X succeed".
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from controlplane.store import TaskStore

# --------------------------------------------------------------------------- #
# controlled vocabularies
# --------------------------------------------------------------------------- #
#: Knowledge kinds. Fixed set: a free-form kind would make the builder's budget
#: allocation and the "hard constraint" rule impossible to reason about.
KINDS = ("background", "architecture", "constraints", "progress",
         "knowledge", "decisions")

#: Verification states, weakest to strongest.
VERIFICATIONS = ("unverified", "candidate", "verified")

#: Security domains. ``local`` must not leave the machine; ``shareable`` is a
#: summary that a future distributed stage may replicate.
SECURITY_DOMAINS = ("local", "shareable")

#: Kinds whose entries are *always* injected when verified, because dropping a
#: hard constraint silently is worse than exceeding the prompt budget.
MANDATORY_KINDS = ("constraints",)


class ContextError(ValueError):
    """Invalid context operation (bad kind, cross-project access, ...)."""


class ContextVersionConflict(ContextError):
    """The caller's ``expected_version`` no longer matches the stored entry."""

    def __init__(self, entry_id: str, expected: int, actual: int):
        super().__init__(
            f"context entry {entry_id} is at version {actual}, not {expected}; "
            "re-read the entry and re-apply the change")
        self.entry_id = entry_id
        self.expected = expected
        self.actual = actual


@dataclass
class ContextEntry:
    """One unit of project knowledge."""

    project_id: str
    kind: str
    title: str
    body: str
    tags: list[str] = field(default_factory=list)
    verification: str = "unverified"
    security_domain: str = "local"
    source: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    id: str = ""
    version: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id, "project_id": self.project_id, "kind": self.kind,
            "title": self.title, "body": self.body, "tags": list(self.tags),
            "verification": self.verification,
            "security_domain": self.security_domain, "source": self.source,
            "evidence_refs": list(self.evidence_refs), "version": self.version,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "ContextEntry":
        return cls(
            project_id=raw["project_id"], kind=raw["kind"], title=raw["title"],
            body=raw["body"], tags=list(raw.get("tags") or []),
            verification=raw.get("verification") or "unverified",
            security_domain=raw.get("security_domain") or "local",
            source=raw.get("source") or "",
            evidence_refs=list(raw.get("evidence_refs") or []),
            id=raw.get("id") or "", version=int(raw.get("version") or 0),
            created_at=float(raw.get("created_at") or 0.0),
            updated_at=float(raw.get("updated_at") or 0.0))

    def validate(self) -> None:
        if self.kind not in KINDS:
            raise ContextError(f"unknown context kind {self.kind!r}; expected one of {KINDS}")
        if self.verification not in VERIFICATIONS:
            raise ContextError(
                f"unknown verification {self.verification!r}; expected one of {VERIFICATIONS}")
        if self.security_domain not in SECURITY_DOMAINS:
            raise ContextError(
                f"unknown security_domain {self.security_domain!r}; "
                f"expected one of {SECURITY_DOMAINS}")
        if not self.title.strip():
            raise ContextError("context entry title must not be empty")
        if not self.body.strip():
            raise ContextError("context entry body must not be empty")


def is_hard_constraint(entry: dict) -> bool:
    """Only a *verified* constraint may be injected as a hard rule.

    This is the guard behind "unverified knowledge must not become security
    policy": a ``candidate`` constraint is still shown to the agent, but labelled
    as unconfirmed, and the builder never counts it as a blocking prerequisite.
    """
    return (entry.get("kind") == "constraints"
            and entry.get("verification") == "verified")


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #
class ProjectContextStore:
    """Versioned project knowledge on top of the existing SQLite ledger."""

    def __init__(self, store: TaskStore):
        self.store = store

    # -- reads ---------------------------------------------------------- #
    def get_project_context(self, project_id: str, *,
                            include_shareable: bool = True) -> dict:
        """Full context of one project, grouped by kind.

        Returns ``{"project_id", "version", "entries", "by_kind"}``. ``version``
        is the project-wide monotonic counter that a dispatched task records, so
        "which knowledge did this prompt see" is answerable later.
        """
        self._require_project(project_id)
        entries = self.store.context_entries(
            project_id, include_shared=include_shareable)
        by_kind: dict[str, list[dict]] = {kind: [] for kind in KINDS}
        for entry in entries:
            by_kind.setdefault(entry["kind"], []).append(entry)
        return {
            "project_id": project_id,
            "version": self.store.context_version(project_id),
            "entries": entries,
            "by_kind": by_kind,
        }

    def get_project_brief(self, project_id: str) -> dict:
        """Compact, deterministic summary — the thing an LD reads first.

        Deliberately *not* an LLM summary: it is a projection of stored rows, so
        two calls with unchanged data return identical text.
        """
        self._require_project(project_id)
        project = self.store.get_project(project_id) or {}
        ctx = self.get_project_context(project_id)
        by_kind = ctx["by_kind"]

        def titles(kind: str, limit: int = 5) -> list[str]:
            return [e["title"] for e in by_kind.get(kind, [])[:limit]]

        verified = sum(1 for e in ctx["entries"] if e["verification"] == "verified")
        return {
            "project_id": project_id,
            "name": project.get("name"),
            "root": project.get("root"),
            "active": project.get("active"),
            "context_version": ctx["version"],
            "entry_count": len(ctx["entries"]),
            "verified_count": verified,
            "background": titles("background", 3),
            "constraints": titles("constraints", 10),
            "progress": titles("progress", 5),
            "open_questions": [e["title"] for e in ctx["entries"]
                               if e["verification"] != "verified"][:5],
        }

    def list_entries(self, project_id: str, *, kind: str | None = None) -> list[dict]:
        self._require_project(project_id)
        if kind is not None and kind not in KINDS:
            raise ContextError(f"unknown context kind {kind!r}")
        return self.store.context_entries(project_id, kind=kind)

    def get_entry(self, project_id: str, entry_id: str) -> dict:
        entry = self.store.get_context_entry(entry_id)
        if not entry:
            raise KeyError(f"no context entry {entry_id}")
        if entry["project_id"] != project_id:
            # Cross-project read is a hard error, not an empty result: silently
            # returning nothing would hide a caller bug that could later become
            # a cross-project *write*.
            raise ContextError(
                f"context entry {entry_id} belongs to project "
                f"{entry['project_id']}, not {project_id}")
        return entry

    def revisions(self, project_id: str, entry_id: str) -> list[dict]:
        self.get_entry(project_id, entry_id)
        return self.store.context_revisions(entry_id)

    # -- writes --------------------------------------------------------- #
    def add_context_entry(self, project_id: str, entry: ContextEntry | dict) -> dict:
        """Insert a new entry. Same (kind, title) in one project is a conflict.

        Different projects may reuse a title freely — the uniqueness index is
        ``(project_id, kind, title)``.
        """
        self._require_project(project_id)
        item = entry if isinstance(entry, ContextEntry) else ContextEntry.from_dict(
            {**entry, "project_id": project_id})
        if item.project_id != project_id:
            raise ContextError(
                f"entry.project_id={item.project_id} does not match {project_id}")
        item.validate()
        existing = self.store.context_entry_by_title(project_id, item.kind, item.title)
        if existing:
            raise ContextError(
                f"project {project_id} already has a {item.kind} entry titled "
                f"{item.title!r} (id={existing['id']}, version={existing['version']}); "
                "use update_context_entry to change it")
        return self.store.insert_context_entry(item.to_dict())

    def update_context_entry(self, project_id: str, entry_id: str,
                             expected_version: int, changes: dict, *,
                             changed_by: str = "", note: str = "") -> dict:
        """CAS update. Raises :class:`ContextVersionConflict` on a stale version."""
        current = self.get_entry(project_id, entry_id)
        if "project_id" in changes:
            raise ContextError("project_id is immutable; move knowledge by re-adding it")
        if "kind" in changes and changes["kind"] not in KINDS:
            raise ContextError(f"unknown context kind {changes['kind']!r}")
        if "verification" in changes and changes["verification"] not in VERIFICATIONS:
            raise ContextError(f"unknown verification {changes['verification']!r}")
        if "security_domain" in changes and \
                changes["security_domain"] not in SECURITY_DOMAINS:
            raise ContextError(
                f"unknown security_domain {changes['security_domain']!r}")
        updated = self.store.update_context_entry(
            entry_id, expected_version, changes,
            changed_by=changed_by, note=note)
        if updated is None:
            actual = self.store.get_context_entry(entry_id) or current
            raise ContextVersionConflict(entry_id, expected_version,
                                         int(actual.get("version") or 0))
        return updated

    def verify_entry(self, project_id: str, entry_id: str, expected_version: int,
                     *, evidence_refs: Iterable[str] = (), changed_by: str = "",
                     note: str = "") -> dict:
        """Promote an entry to ``verified`` — requires evidence.

        Verification without evidence is exactly the "agent said so" failure this
        store exists to prevent, so an empty ``evidence_refs`` is refused.
        """
        refs = [str(r) for r in evidence_refs if str(r).strip()]
        if not refs:
            raise ContextError(
                "verifying a context entry requires at least one evidence reference")
        return self.update_context_entry(
            project_id, entry_id, expected_version,
            {"verification": "verified", "evidence_refs": refs},
            changed_by=changed_by, note=note or "verified with evidence")

    # -- helpers -------------------------------------------------------- #
    def _require_project(self, project_id: str) -> dict:
        project = self.store.get_project(project_id)
        if not project:
            raise KeyError(f"no project {project_id}")
        return project


# --------------------------------------------------------------------------- #
# module-level convenience (thin, keeps call sites short)
# --------------------------------------------------------------------------- #
def get_project_context(store: TaskStore, project_id: str) -> dict:
    return ProjectContextStore(store).get_project_context(project_id)


def get_project_brief(store: TaskStore, project_id: str) -> dict:
    return ProjectContextStore(store).get_project_brief(project_id)


def add_context_entry(store: TaskStore, project_id: str,
                      entry: ContextEntry | dict) -> dict:
    return ProjectContextStore(store).add_context_entry(project_id, entry)


def update_context_entry(store: TaskStore, project_id: str, entry_id: str,
                         expected_version: int, changes: dict, **kwargs: Any) -> dict:
    return ProjectContextStore(store).update_context_entry(
        project_id, entry_id, expected_version, changes, **kwargs)


def now() -> float:
    return time.time()
