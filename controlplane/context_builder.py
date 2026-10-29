"""Turn a project task into the exact prompt a coding agent will receive (M1).

The problem this solves
-----------------------
`ControlPlane.enqueue()` takes a prompt string. A fresh coding-agent session has
no idea what the project already knows, so it re-explores settled facts, or worse,
violates a constraint nobody told it about. This module builds the prompt *before*
the task enters the queue, from the project's durable context, and freezes it.

Contract
--------
* **Bounded.** A fixed section layout with a character budget. We never dump the
  whole project knowledge base into a prompt.
* **Deterministic.** Same context version + same task => byte-identical prompt.
  That is what makes the recorded ``effective_prompt`` auditable.
* **Fail-closed on prerequisites.** If a declared prerequisite (a required tag, a
  verified constraint, an existing workspace) is missing, the build returns
  ``blocked=True`` with explicit gaps. It never invents the missing fact.
* **Traceable.** The result carries ``context_version`` and the exact entry ids
  used, both of which are persisted on the work item.
* **Frozen.** The prompt is built once, at submit time. Later context edits do
  not rewrite queued work — see :func:`controlplane.project_service.submit_project_task`.

This module knows nothing about CodeBuddy or any other worker: it produces text
and metadata, and the control plane dispatches it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from controlplane.project_context import (
    KINDS,
    MANDATORY_KINDS,
    ProjectContextStore,
    is_hard_constraint,
)
from controlplane.store import TaskStore

#: Default character budget for the injected context (not counting the task
#: itself). ~6k chars is roughly 1.5k tokens: enough for background + constraints
#: + a handful of knowledge entries, small enough to stay cheap per dispatch.
DEFAULT_CONTEXT_BUDGET = 6000

#: Per-kind share of the budget. Constraints are exempt (always complete).
KIND_BUDGET_SHARE = {
    "background": 0.20,
    "architecture": 0.15,
    "constraints": 0.25,
    "progress": 0.15,
    "knowledge": 0.20,
    "decisions": 0.05,
}

#: Order sections appear in the prompt. Stable on purpose.
SECTION_ORDER = ("background", "constraints", "architecture", "knowledge",
                 "progress", "decisions")

_WORD_RE = re.compile(r"[A-Za-z0-9_\-]{3,}|[\u4e00-\u9fff]{2,}")


class ContextBuildBlocked(RuntimeError):
    """The task cannot be dispatched: required project knowledge is missing."""

    def __init__(self, gaps: list[str], built: "BuiltContext | None" = None):
        super().__init__("context build blocked: " + "; ".join(gaps))
        self.gaps = gaps
        self.built = built


@dataclass
class BuiltContext:
    """Result of a context build — everything the ledger needs to stay honest."""

    project_id: str
    original_task: str
    effective_prompt: str
    context_version: int
    entry_refs: list[str] = field(default_factory=list)
    sections: dict[str, list[str]] = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    blocked: bool = False
    workspace_id: str | None = None
    workspace_root: str | None = None

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "original_task": self.original_task,
            "effective_prompt": self.effective_prompt,
            "context_version": self.context_version,
            "context_entry_refs": list(self.entry_refs),
            "sections": {k: list(v) for k, v in self.sections.items()},
            "gaps": list(self.gaps),
            "warnings": list(self.warnings),
            "blocked": self.blocked,
            "workspace_id": self.workspace_id,
            "workspace_root": self.workspace_root,
        }


def _tokens(text: str) -> set[str]:
    return {m.group(0).lower() for m in _WORD_RE.finditer(text or "")}


def _entry_text(entry: dict) -> str:
    return f"{entry.get('title', '')} {entry.get('body', '')} " \
           f"{' '.join(entry.get('tags') or [])}"


class ContextBuilder:
    """Builds the effective prompt for one project task."""

    def __init__(self, store: TaskStore, *,
                 budget_chars: int = DEFAULT_CONTEXT_BUDGET):
        self.store = store
        self.context = ProjectContextStore(store)
        self.budget_chars = budget_chars

    # ------------------------------------------------------------------ #
    # relevance
    # ------------------------------------------------------------------ #
    def _score(self, entry: dict, task_tokens: set[str],
               wanted_tags: set[str]) -> float:
        """Cheap, explainable relevance: tags first, then keyword overlap.

        No embeddings, no vector store. A wrong ranking costs a slightly less
        useful prompt, never a wrong decision, because hard constraints bypass
        ranking entirely.
        """
        score = 0.0
        tags = {t.lower() for t in (entry.get("tags") or [])}
        if wanted_tags:
            score += 3.0 * len(tags & wanted_tags)
        overlap = _tokens(_entry_text(entry)) & task_tokens
        score += min(len(overlap), 8) * 0.5
        if entry.get("verification") == "verified":
            score += 1.0
        return score

    def _select(self, entries: list[dict], task_tokens: set[str],
                wanted_tags: set[str], budget: int) -> tuple[list[dict], list[str]]:
        """Pick entries for one kind within ``budget`` characters."""
        warnings: list[str] = []
        if not entries:
            return [], warnings
        ranked = sorted(
            entries,
            key=lambda e: (-self._score(e, task_tokens, wanted_tags),
                           e.get("title") or ""))
        chosen: list[dict] = []
        used = 0
        for entry in ranked:
            cost = len(entry.get("title") or "") + len(entry.get("body") or "") + 8
            if used + cost > budget and chosen:
                warnings.append(
                    f"context budget reached for kind={entry['kind']}: "
                    f"{len(ranked) - len(chosen)} entry(ies) omitted")
                break
            chosen.append(entry)
            used += cost
        return chosen, warnings

    # ------------------------------------------------------------------ #
    # build
    # ------------------------------------------------------------------ #
    def build(self, project_id: str, task: str, *,
              tags: Iterable[str] = (),
              prerequisites: Iterable[str] = (),
              require_verified_constraints: bool = False,
              workspace_id: str | None = None,
              extra_references: Iterable[str] = (),
              budget_chars: int | None = None) -> BuiltContext:
        """Build the effective prompt, or report why it cannot be built.

        ``prerequisites`` are tags that must exist as *verified* entries. A
        missing one is a gap, not something to paper over.
        """
        project = self.store.get_project(project_id)
        if not project:
            raise KeyError(f"no project {project_id}")
        if not project.get("active"):
            raise ValueError(f"project is disabled: {project.get('name')}")

        budget = int(budget_chars or self.budget_chars)
        wanted_tags = {t.lower() for t in tags if str(t).strip()}
        task_tokens = _tokens(task)
        ctx = self.context.get_project_context(project_id)
        by_kind = ctx["by_kind"]

        gaps: list[str] = []
        warnings: list[str] = []

        # -- workspace prerequisite (real, checkable) -------------------- #
        workspace = self._resolve_workspace(project, workspace_id)
        root = Path(workspace["local_root"])
        if not root.is_dir():
            gaps.append(f"workspace root does not exist: {root}")
        if workspace.get("node_id") != "local":
            gaps.append(
                f"workspace {workspace['id']} is on node {workspace.get('node_id')!r}; "
                "remote execution is not implemented (M5)")

        # -- declared prerequisites ------------------------------------- #
        verified_entries = [e for e in ctx["entries"] if e["verification"] == "verified"]
        verified_tags = {t.lower() for e in verified_entries for t in (e.get("tags") or [])}
        for tag in prerequisites:
            if str(tag).lower() not in verified_tags:
                gaps.append(
                    f"prerequisite {tag!r} has no verified project context entry")

        hard = [e for e in ctx["entries"] if is_hard_constraint(e)]
        if require_verified_constraints and not hard:
            gaps.append(
                "task declares require_verified_constraints but the project has no "
                "verified constraint entry")

        # -- selection --------------------------------------------------- #
        sections: dict[str, list[dict]] = {}
        for kind in SECTION_ORDER:
            entries = by_kind.get(kind, [])
            if kind in MANDATORY_KINDS:
                # Hard constraints are never truncated away: dropping one is a
                # silent policy change, which is worse than a long prompt.
                sections[kind] = list(entries)
                continue
            share = KIND_BUDGET_SHARE.get(kind, 0.1)
            chosen, kind_warnings = self._select(
                entries, task_tokens, wanted_tags, max(200, int(budget * share)))
            sections[kind] = chosen
            warnings.extend(kind_warnings)

        used_refs: list[str] = []
        for kind, entries in sections.items():
            for entry in entries:
                if entry["id"] not in used_refs:
                    used_refs.append(entry["id"])

        prompt = self._render(task, sections, extra_references, gaps, warnings)
        built = BuiltContext(
            project_id=project_id,
            original_task=task,
            effective_prompt=prompt,
            context_version=ctx["version"],
            entry_refs=used_refs,
            sections={k: [e["id"] for e in v] for k, v in sections.items()},
            gaps=gaps,
            warnings=warnings,
            blocked=bool(gaps),
            workspace_id=workspace["id"],
            workspace_root=str(root),
        )
        return built

    def _resolve_workspace(self, project: dict, workspace_id: str | None) -> dict:
        if workspace_id:
            for ws in self.store.workspaces(project["id"]):
                if ws["id"] == workspace_id:
                    return ws
            raise KeyError(f"no workspace {workspace_id} for project {project['id']}")
        ws = self.store.default_workspace(project["id"])
        if ws:
            return ws
        # A ledger written before the workspace table existed: derive it now.
        return self.store.ensure_default_workspace(project["id"])

    # ------------------------------------------------------------------ #
    # rendering
    # ------------------------------------------------------------------ #
    def _render(self, task: str, sections: dict[str, list[dict]],
                extra_references: Iterable[str], gaps: list[str],
                warnings: list[str]) -> str:
        """Fixed layout. Section headers are ASCII so the prompt is stable."""
        out: list[str] = []
        out.append("# TASK")
        out.append(task.strip())
        out.append("")

        titles = {
            "background": "PROJECT BACKGROUND",
            "constraints": "HARD CONSTRAINTS (must be obeyed)",
            "architecture": "ARCHITECTURE",
            "knowledge": "VERIFIED KNOWLEDGE",
            "progress": "PROJECT PROGRESS / PRECONDITIONS",
            "decisions": "DECISIONS AND RATIONALE",
        }
        for kind in SECTION_ORDER:
            entries = sections.get(kind) or []
            if not entries:
                continue
            out.append(f"# {titles[kind]}")
            for entry in entries:
                label = entry.get("verification")
                marker = "" if label == "verified" else f" [{label.upper()}]"
                out.append(f"- {entry['title']}{marker}: {entry['body'].strip()}")
            out.append("")

        refs = [str(r) for r in extra_references if str(r).strip()]
        if refs:
            out.append("# REFERENCES")
            out.extend(f"- {r}" for r in refs)
            out.append("")

        if gaps:
            out.append("# BLOCKING GAPS (do not guess these)")
            out.extend(f"- {g}" for g in gaps)
            out.append("")

        if warnings:
            out.append("# CONTEXT NOTES")
            out.extend(f"- {w}" for w in warnings)
            out.append("")

        out.append("# RULES")
        out.append("- Obey every HARD CONSTRAINT above; if one conflicts with the "
                   "task, stop and report the conflict instead of working around it.")
        out.append("- Entries marked [CANDIDATE] or [UNVERIFIED] are unconfirmed: "
                   "verify them before relying on them.")
        out.append("- Do not invent project facts that are not stated above.")
        return "\n".join(out).rstrip() + "\n"


def build_context(store: TaskStore, project_id: str, task: str, **kwargs) -> BuiltContext:
    """Module-level convenience wrapper."""
    return ContextBuilder(store).build(project_id, task, **kwargs)
