"""Editable Markdown projection of the authoritative Project Context.

SQLite remains the only source of truth.  This module exports deterministic
files for a human operator and parses one entry back for a CAS preview; it does
not watch files, execute Markdown, or create a second memory state.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from controlplane.project_context import (
    KINDS,
    SECURITY_DOMAINS,
    VERIFICATIONS,
    ProjectContextStore,
)

ENTRY_FIELDS = (
    "id", "project_id", "kind", "title", "verification",
    "security_domain", "version", "source", "evidence_refs", "tags",
)
EDITABLE_FIELDS = ("title", "body", "tags")
PROTECTED_FIELDS = (
    "id", "project_id", "kind", "verification", "security_domain",
    "source", "evidence_refs",
)
_LIST_FIELDS = {"evidence_refs", "tags"}
_STRING_FIELDS = set(ENTRY_FIELDS) - _LIST_FIELDS - {"version"}
_SAFE_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class ProjectMemoryError(ValueError):
    """Invalid or unsafe Markdown memory operation."""


class ProjectMemoryFormatError(ProjectMemoryError):
    """The entry file is not in the strict exported Front Matter format."""


class ProjectMemoryAccessError(ProjectMemoryError):
    """A path is outside the selected project's memory directory."""


class ProjectMemoryPolicyError(ProjectMemoryError):
    """A requested Markdown change crosses the editable-field boundary."""


class ProjectMemoryMarkdown:
    """Deterministic export and single-entry preview over ProjectContextStore."""

    def __init__(self, service, *, goal_store=None,
                 base_dir: str | Path | None = None):
        self.service = service
        self.store = service.store
        self.context = ProjectContextStore(self.store)
        self.goals = goal_store
        self.base_dir = Path(
            base_dir if base_dir is not None else ".poc-state/projects")

    # ------------------------------------------------------------------ #
    # paths and atomic output
    # ------------------------------------------------------------------ #
    def memory_dir(self, project_id: str) -> Path:
        self._safe_component(project_id, "project_id")
        return self.base_dir / project_id / "memory"

    @staticmethod
    def _safe_component(value: str, label: str) -> None:
        if not isinstance(value, str) or not _SAFE_COMPONENT.fullmatch(value):
            raise ProjectMemoryAccessError(
                f"{label} is not a safe path component: {value!r}")

    def _prepare_export_dirs(self, project_id: str) -> tuple[Path, Path]:
        memory = self.memory_dir(project_id)
        base = self.base_dir
        if base.exists() and base.is_symlink():
            raise ProjectMemoryAccessError("memory base directory must not be a symlink")
        base.mkdir(parents=True, exist_ok=True)
        if base.is_symlink() or not base.is_dir():
            raise ProjectMemoryAccessError("memory base directory is unsafe")
        current = base
        for component in (project_id, "memory", "entries"):
            current = current / component
            if current.exists() and current.is_symlink():
                raise ProjectMemoryAccessError(
                    f"memory path component must not be a symlink: {current}")
            current.mkdir(exist_ok=True)
            if current.is_symlink() or not current.is_dir():
                raise ProjectMemoryAccessError(
                    f"memory path component is unsafe: {current}")
        entries = memory / "entries"
        base_resolved = base.resolve()
        if not memory.resolve().is_relative_to(base_resolved):
            raise ProjectMemoryAccessError("memory directory escapes configured base")
        return memory, entries

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        if path.exists() and path.is_symlink():
            raise ProjectMemoryAccessError(
                f"refusing to replace symlinked memory file: {path}")
        descriptor, raw_temp = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
        temp = Path(raw_temp)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)

    def _entry_path(self, project_id: str, raw_path: str | Path) -> Path:
        memory = self.memory_dir(project_id)
        entries = memory / "entries"
        if not entries.exists() or entries.is_symlink():
            raise ProjectMemoryAccessError(
                f"entry directory is missing or unsafe; export first: {entries}")
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        if candidate.is_symlink():
            raise ProjectMemoryAccessError("entry file must not be a symlink")
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ProjectMemoryAccessError(
                f"memory entry file does not exist: {candidate}") from exc
        entries_resolved = entries.resolve(strict=True)
        if not resolved.is_relative_to(entries_resolved):
            raise ProjectMemoryAccessError(
                "memory entry must stay inside the selected project's entries directory")
        relative = resolved.relative_to(entries_resolved)
        if len(relative.parts) != 1 or resolved.suffix != ".md":
            raise ProjectMemoryAccessError(
                "memory preview accepts one direct entries/<context-entry-id>.md file")
        if not resolved.is_file():
            raise ProjectMemoryAccessError("memory entry path is not a regular file")
        return resolved

    # ------------------------------------------------------------------ #
    # deterministic export
    # ------------------------------------------------------------------ #
    def export(self, project_id: str) -> dict[str, Any]:
        project = self.store.get_project(project_id)
        if not project:
            raise KeyError(f"no project {project_id}")
        memory, entries_dir = self._prepare_export_dirs(project_id)
        context = self.context.get_project_context(project_id)
        entries = sorted(
            context["entries"],
            key=lambda entry: (
                str(entry.get("kind") or ""),
                str(entry.get("title") or ""),
                str(entry.get("id") or "")),
        )
        project_text = self._render_project(project, context, entries)
        current_text = self._render_current(project_id)
        self._atomic_write(memory / "PROJECT.md", project_text)
        self._atomic_write(memory / "CURRENT.md", current_text)
        written = []
        for entry in entries:
            self._safe_component(str(entry["id"]), "context entry id")
            path = entries_dir / f"{entry['id']}.md"
            self._atomic_write(path, self._render_entry(entry))
            written.append(str(path.resolve()))
        return {
            "project_id": project_id,
            "authoritative_source": "sqlite_project_context",
            "memory_dir": str(memory.resolve()),
            "project_file": str((memory / "PROJECT.md").resolve()),
            "current_file": str((memory / "CURRENT.md").resolve()),
            "entry_files": written,
            "entry_count": len(written),
            "context_version": context["version"],
            "ledger_modified": False,
        }

    def _render_project(self, project: dict, context: dict,
                        entries: list[dict]) -> str:
        counts = {
            kind: sum(1 for entry in entries if entry.get("kind") == kind)
            for kind in KINDS
        }
        verification = {
            value: sum(1 for entry in entries
                       if entry.get("verification") == value)
            for value in VERIFICATIONS
        }
        lines = [
            "<!-- GENERATED VIEW. SQLite Project Context is authoritative. -->",
            "# Project Memory",
            "",
            f"- Project ID: `{self._md_inline(project['id'])}`",
            f"- Name: {self._md_inline(project.get('name') or '')}",
            f"- Active: `{str(bool(project.get('active'))).lower()}`",
            f"- Context version: `{context['version']}`",
            f"- Context entries: `{len(entries)}`",
            "",
            "## Entry counts",
            "",
        ]
        lines.extend(f"- {kind}: `{counts[kind]}`" for kind in KINDS)
        lines.extend(["", "## Verification counts", ""])
        lines.extend(
            f"- {value}: `{verification[value]}`" for value in VERIFICATIONS)
        lines.extend(["", "## Entries", ""])
        if entries:
            for entry in entries:
                lines.append(
                    f"- `{entry['id']}` — {entry['kind']} / "
                    f"{entry['verification']} / {entry['security_domain']} — "
                    f"{self._md_inline(entry['title'])}")
        else:
            lines.append("- None")
        return "\n".join(lines).rstrip() + "\n"

    def _render_current(self, project_id: str) -> str:
        progress = self.service.get_project_progress(project_id)
        tasks = self.service.get_project_tasks(project_id, limit=100)
        try:
            goals = (self.goals.find_by_project(project_id, limit=100)
                     if self.goals is not None else [])
        except sqlite3.OperationalError:
            # A pre-M4 ledger may not have goal tables.  Export is read-only,
            # so it must not create/migrate them merely to render CURRENT.md.
            goals = []
        lines = [
            "<!-- GENERATED READ-ONLY VIEW. Do not import this file. -->",
            "# Current Project State",
            "",
            "Task state and acceptance remain authoritative in the SQLite Ledger.",
            "",
            f"- Project ID: `{self._md_inline(project_id)}`",
            f"- Context version: `{progress['context_version']}`",
            "",
            "## Ledger counts",
            "",
        ]
        for key in sorted(progress["counts"]):
            lines.append(f"- {key}: `{progress['counts'][key]}`")
        lines.extend(["", "## Goals", ""])
        if goals:
            for goal in sorted(goals, key=lambda item: str(item.get("goal_id"))):
                lines.append(
                    f"- `{goal.get('goal_id')}` — status="
                    f"`{goal.get('status')}`; current_task="
                    f"`{goal.get('current_task_id') or ''}`")
        else:
            lines.append("- None")
        lines.extend(["", "## WorkItems", ""])
        if tasks:
            for task in sorted(tasks, key=lambda item: str(item.get("task_id"))):
                lines.append(
                    f"- `{task.get('task_id')}` — state=`{task.get('state')}`; "
                    f"verdict=`{task.get('verdict')}`; context_version="
                    f"`{task.get('context_version')}`")
        else:
            lines.append("- None")
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _md_inline(value: Any) -> str:
        return str(value).replace("\n", " ").replace("|", "\\|").replace("`", "'")

    @classmethod
    def _render_entry(cls, entry: Mapping[str, Any]) -> str:
        lines = ["---"]
        for field in ENTRY_FIELDS:
            value = entry.get(field)
            if field in _LIST_FIELDS:
                values = list(value or [])
                if values:
                    lines.append(f"{field}:")
                    lines.extend(
                        "  - " + json.dumps(str(item), ensure_ascii=False)
                        for item in values)
                else:
                    lines.append(f"{field}: []")
            elif field == "version":
                lines.append(f"{field}: {int(value)}")
            else:
                lines.append(
                    f"{field}: {json.dumps(str(value or ''), ensure_ascii=False)}")
        body = cls._normalize_body(str(entry.get("body") or ""))
        return "\n".join([*lines, "---", "", body]).rstrip() + "\n"

    # ------------------------------------------------------------------ #
    # strict parse and preview
    # ------------------------------------------------------------------ #
    def preview(self, project_id: str, entry_file: str | Path) -> dict[str, Any]:
        if not self.store.get_project(project_id):
            raise KeyError(f"no project {project_id}")
        path = self._entry_path(project_id, entry_file)
        document = self._parse_entry(path)
        fields = document["fields"]
        entry_id = fields["id"]
        if path.name != f"{entry_id}.md":
            raise ProjectMemoryFormatError(
                "entry filename must match the front matter id")
        current = self.context.get_entry(project_id, entry_id)
        protected_changes = [
            field for field in PROTECTED_FIELDS
            if fields[field] != current.get(field)
        ]
        changes: dict[str, Any] = {}
        if fields["title"] != current.get("title"):
            changes["title"] = {
                "before": current.get("title"), "after": fields["title"]}
        body = document["body"]
        current_body = self._normalize_body(str(current.get("body") or ""))
        if body != current_body:
            changes["body"] = {
                "before_sha256": self._sha256(current_body),
                "after_sha256": self._sha256(body),
                "before_chars": len(current_body), "after_chars": len(body),
            }
        if fields["tags"] != list(current.get("tags") or []):
            changes["tags"] = {
                "before": list(current.get("tags") or []),
                "after": fields["tags"],
            }
        markdown_version = int(fields["version"])
        sqlite_version = int(current.get("version") or 0)
        version_conflict = markdown_version != sqlite_version
        policy_errors = []
        if protected_changes:
            policy_errors.append(
                "protected fields changed: " + ", ".join(protected_changes))
        if current.get("verification") == "verified" and changes:
            policy_errors.append(
                "verified entries require a separate review/re-verification flow")
        can_apply = bool(changes) and not version_conflict and not policy_errors
        digest_payload = {
            "project_id": project_id,
            "entry_id": entry_id,
            "markdown_version": markdown_version,
            "sqlite_version": sqlite_version,
            "fields": {field: fields[field] for field in ENTRY_FIELDS},
            "body": body,
            "changes": changes,
        }
        digest = self._sha256(json.dumps(
            digest_payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"))) if can_apply else None
        return {
            "project_id": project_id,
            "entry_id": entry_id,
            "entry_file": str(path),
            "sqlite_version": sqlite_version,
            "markdown_version": markdown_version,
            "changed_fields": sorted(changes),
            "changes": changes,
            "version_conflict": version_conflict,
            "protected_changes": protected_changes,
            "policy_errors": policy_errors,
            "can_apply": can_apply,
            "preview_digest": digest,
            "expected_behavior": (
                "CAS update title/body/tags and append one revision"
                if can_apply else
                "refuse without modifying SQLite"),
        }

    @classmethod
    def _parse_entry(cls, path: Path) -> dict[str, Any]:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ProjectMemoryFormatError("memory entry must be UTF-8") from exc
        if not text.startswith("---\n"):
            raise ProjectMemoryFormatError("memory entry must start with '---'")
        closing = text.find("\n---\n", 4)
        if closing < 0:
            raise ProjectMemoryFormatError("memory entry has no closing '---'")
        front = text[4:closing]
        body = text[closing + 5:]
        if body.startswith("\n"):
            body = body[1:]
        fields = cls._parse_front_matter(front)
        normalized_body = cls._normalize_body(body)
        if not normalized_body.strip():
            raise ProjectMemoryFormatError("memory entry body must not be empty")
        return {"fields": fields, "body": normalized_body}

    @classmethod
    def _parse_front_matter(cls, front: str) -> dict[str, Any]:
        lines = front.split("\n") if front else []
        fields: dict[str, Any] = {}
        index = 0
        while index < len(lines):
            line = lines[index]
            if not line or line.startswith((" ", "\t")) or ":" not in line:
                raise ProjectMemoryFormatError(
                    f"invalid front matter line {index + 1}")
            key, raw = line.split(":", 1)
            if key not in ENTRY_FIELDS:
                raise ProjectMemoryFormatError(
                    f"unknown front matter field {key!r}")
            if key in fields:
                raise ProjectMemoryFormatError(
                    f"duplicate front matter field {key!r}")
            raw = raw.strip()
            if key in _LIST_FIELDS:
                values: list[str] = []
                if raw == "[]":
                    fields[key] = values
                    index += 1
                    continue
                if raw:
                    raise ProjectMemoryFormatError(
                        f"{key} must be [] or an indented list")
                index += 1
                while index < len(lines) and lines[index].startswith("  - "):
                    values.append(cls._parse_string(
                        lines[index][4:].strip(), f"{key} item"))
                    index += 1
                fields[key] = values
                continue
            if key == "version":
                if not raw.isdigit():
                    raise ProjectMemoryFormatError("version must be an integer")
                fields[key] = int(raw)
            else:
                fields[key] = cls._parse_string(raw, key)
            index += 1
        missing = set(ENTRY_FIELDS) - set(fields)
        if missing:
            raise ProjectMemoryFormatError(
                "missing front matter fields: " + ", ".join(sorted(missing)))
        if fields["kind"] not in KINDS:
            raise ProjectMemoryFormatError(f"unknown context kind {fields['kind']!r}")
        if fields["verification"] not in VERIFICATIONS:
            raise ProjectMemoryFormatError(
                f"unknown verification {fields['verification']!r}")
        if fields["security_domain"] not in SECURITY_DOMAINS:
            raise ProjectMemoryFormatError(
                f"unknown security_domain {fields['security_domain']!r}")
        if not fields["title"].strip():
            raise ProjectMemoryFormatError("title must not be empty")
        return fields

    @staticmethod
    def _parse_string(raw: str, label: str) -> str:
        if not raw:
            return ""
        if raw.startswith('"'):
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ProjectMemoryFormatError(
                    f"{label} is not a valid quoted string") from exc
            if not isinstance(value, str):
                raise ProjectMemoryFormatError(f"{label} must be a string")
            return value
        if (raw.startswith(("!", "&", "*", "[", "{", "|", ">"))
                or "\t" in raw or " #" in raw):
            raise ProjectMemoryFormatError(
                f"{label} uses unsupported YAML syntax; use a quoted string")
        return raw

    @staticmethod
    def _normalize_body(value: str) -> str:
        return value.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")

    @staticmethod
    def _sha256(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
