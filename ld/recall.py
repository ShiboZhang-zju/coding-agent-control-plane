"""Deterministic, project-scoped relational recall (M4-E).

This module is intentionally *not* a memory database and not an agent.  It
walks the durable relations that already exist in ``LDGoalStore`` and the
control-plane Ledger:

    Project -> Goal -> WorkItem -> Attempt -> SessionReference / Evidence
                             and WorkItem -> Project Context entry

Level 1 returns small authoritative summaries plus ids that may be expanded.
Level 2 expands exactly one id.  No fact is synthesized by an LLM, no task or
context state is copied, and every lookup revalidates project ownership.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

from controlplane.project_service import ProjectService


RECALL_SCHEMA_VERSION = "m4-e.recall.v1"
OBJECT_TYPES = frozenset({
    "project", "goal", "task", "attempt", "session", "evidence",
    "context_entry",
})
SECURITY_DOMAINS = frozenset({"local", "shareable"})
DEFAULT_MAX_TOTAL_NODES = 1_000
DEFAULT_MAX_DEPTH = 8
DEFAULT_MAX_OUTPUT_BYTES = 65_536
MAX_RELATION_SCAN = 10_000
_MAX_CURSOR_CHARS = 2_048


class RecallError(ValueError):
    """Invalid or inconsistent recall request."""


class RecallAccessError(RecallError):
    """The requested object is outside the project or security scope."""


class _ContentBudget:
    """Bound content strings while leaving identity/status fields intact.

    ``max_chars`` is a content budget, not a byte limit for the JSON envelope.
    IDs, types, states and reference relations are deliberately not charged:
    those are the small spill pointers needed to continue the Chain of Recall.
    """

    def __init__(self, max_chars: int, *,
                 max_total_nodes: int = DEFAULT_MAX_TOTAL_NODES,
                 max_depth: int = DEFAULT_MAX_DEPTH,
                 max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES):
        if not isinstance(max_chars, int) or isinstance(max_chars, bool):
            raise RecallError("char_budget must be an integer")
        if not 128 <= max_chars <= 50_000:
            raise RecallError("char_budget must be between 128 and 50000")
        if (not isinstance(max_total_nodes, int)
                or isinstance(max_total_nodes, bool)
                or not 64 <= max_total_nodes <= 10_000):
            raise RecallError("max_total_nodes must be an integer from 64 to 10000")
        if (not isinstance(max_depth, int) or isinstance(max_depth, bool)
                or not 3 <= max_depth <= 32):
            raise RecallError("max_depth must be an integer from 3 to 32")
        if (not isinstance(max_output_bytes, int)
                or isinstance(max_output_bytes, bool)
                or not 1_024 <= max_output_bytes <= 1_048_576):
            raise RecallError(
                "max_output_bytes must be an integer from 1024 to 1048576")
        self.max_chars = max_chars
        self.max_total_nodes = max_total_nodes
        self.max_depth = max_depth
        self.max_output_bytes = max_output_bytes
        self.used = 0
        self.truncated = False
        self.nodes_used = 0
        self.output_nodes = 0
        self.deepest_depth = 0
        self.output_depth = 0
        self.output_bytes = 0
        self.truncation_reasons: set[str] = set()

    @property
    def remaining(self) -> int:
        return max(0, self.max_chars - self.used)

    def clip(self, value: Any, *, per_field: int = 1_200) -> str:
        text = "" if value is None else str(value)
        allowance = min(self.remaining, max(0, int(per_field)))
        if len(text) <= allowance:
            self.used += len(text)
            return text
        self._truncate("content_chars")
        if allowance <= 0:
            return ""
        if allowance == 1:
            self.used += 1
            return "…"
        out = text[: allowance - 1] + "…"
        self.used += len(out)
        return out

    def value(self, value: Any, *, per_string: int = 1_200,
              max_items: int = 100, _depth: int = 0) -> Any:
        """Project nested content without letting collection size bypass bounds.

        String content consumes ``char_budget``.  Collections also have a hard
        item ceiling and stop once the string budget is exhausted; otherwise a
        payload containing thousands of empty strings or numeric values could
        stay under the character counter while still creating a huge model
        input.
        """
        max_items = max(1, int(max_items))
        self.deepest_depth = max(self.deepest_depth, _depth)
        if _depth > self.max_depth:
            self._truncate("max_depth")
            return None
        if self.nodes_used >= self.max_total_nodes:
            self._truncate("max_total_nodes")
            return None
        self.nodes_used += 1
        if isinstance(value, str):
            return self.clip(value, per_field=per_string)
        if isinstance(value, Mapping):
            items = list(value.items())
            if len(items) > max_items:
                self._truncate("collection_items")
            out: dict[str, Any] = {}
            for index, (key, item) in enumerate(items[:max_items]):
                if (self.remaining <= 0
                        or self.nodes_used >= self.max_total_nodes):
                    self._truncate(
                        "content_chars" if self.remaining <= 0
                        else "max_total_nodes")
                    break
                projected_key = self.clip(
                    str(key), per_field=min(200, per_string))
                if projected_key in out:
                    projected_key = f"{projected_key}#{index}"
                out[projected_key] = self.value(
                    item, per_string=per_string, max_items=max_items,
                    _depth=_depth + 1)
            return out
        if isinstance(value, (list, tuple)):
            if len(value) > max_items:
                self._truncate("collection_items")
            out = []
            for item in value[:max_items]:
                if (self.remaining <= 0
                        or self.nodes_used >= self.max_total_nodes):
                    self._truncate(
                        "content_chars" if self.remaining <= 0
                        else "max_total_nodes")
                    break
                out.append(self.value(
                    item, per_string=per_string, max_items=max_items,
                    _depth=_depth + 1))
            return out
        return value

    def _truncate(self, reason: str) -> None:
        self.truncated = True
        self.truncation_reasons.add(reason)

    def note_final_shape(self, *, nodes: int, output_bytes: int,
                         depth: int) -> None:
        self.output_nodes = int(nodes)
        self.output_bytes = int(output_bytes)
        self.output_depth = int(depth)
        if nodes > self.max_total_nodes:
            self._truncate("max_total_nodes")
        if output_bytes > self.max_output_bytes:
            self._truncate("max_output_bytes")
        if depth > self.max_depth:
            self._truncate("max_depth")

    def report(self) -> dict[str, Any]:
        return {
            "max_content_chars": self.max_chars,
            "content_chars_used": self.used,
            "content_truncated": self.truncated,
            "max_total_nodes": self.max_total_nodes,
            "total_nodes_used": self.output_nodes,
            "projected_content_nodes": self.nodes_used,
            "max_depth": self.max_depth,
            "deepest_depth": self.output_depth,
            "projected_content_depth": self.deepest_depth,
            "max_output_bytes": self.max_output_bytes,
            "output_bytes": self.output_bytes,
            "truncated": self.truncated,
            "truncation_reasons": sorted(self.truncation_reasons),
        }


class ProjectRecall:
    """Read-only relational recall over existing Project and Goal stores."""

    def __init__(self, service: ProjectService, goal_store):
        if goal_store is None:
            raise ValueError("ProjectRecall requires an LDGoalStore")
        self.service = service
        self.store = service.store
        self.goals = goal_store

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def summary(
        self,
        project_id: str,
        object_type: str = "project",
        object_id: str | None = None,
        *,
        limit: int = 20,
        char_budget: int = 3_000,
        cursor: str | None = None,
        max_total_nodes: int = DEFAULT_MAX_TOTAL_NODES,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        allowed_security_domains: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Level 1: one object summary and bounded direct relations."""
        project = self._require_project(project_id)
        object_type = self._object_type(object_type)
        object_id = self._normalize_object_id(project_id, object_type, object_id)
        limit = self._limit(limit)
        domains = self._domains(allowed_security_domains)
        budget = _ContentBudget(
            char_budget, max_total_nodes=max_total_nodes,
            max_depth=max_depth, max_output_bytes=max_output_bytes)

        obj = self._load(project_id, object_type, object_id, domains)
        node = self._summary_node(project, object_type, obj, domains, budget)
        all_refs = self._references(project_id, object_type, obj, domains)
        refs, total, next_cursor = self._page_refs(
            project_id, object_type, object_id, all_refs, limit, cursor,
            domains)
        node["refs"] = refs
        node["related_total"] = total
        node["related_omitted"] = max(0, total - len(refs))

        related = []
        for ref in refs:
            if not ref.get("expandable") or budget.remaining <= 0:
                if budget.remaining <= 0:
                    budget._truncate("content_chars")
                continue
            child = self._load(
                project_id, ref["object_type"], ref["object_id"], domains)
            related.append(self._summary_node(
                project, ref["object_type"], child, domains, budget))

        response = {
            "schema_version": RECALL_SCHEMA_VERSION,
            "level": 1,
            "project_id": project_id,
            "object": node,
            "related": related,
            "continuation": {
                "expand_by": "object_type + object_id",
                "order": "object_type,object_id,relation",
                "has_more": next_cursor is not None,
                "next_cursor": next_cursor,
            },
        }
        return self._finalize_response(response, budget)

    def detail(
        self,
        project_id: str,
        object_type: str,
        object_id: str,
        *,
        limit: int = 50,
        char_budget: int = 8_000,
        cursor: str | None = None,
        max_total_nodes: int = DEFAULT_MAX_TOTAL_NODES,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        allowed_security_domains: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Level 2: expand one durable object after ownership/security checks."""
        project = self._require_project(project_id)
        object_type = self._object_type(object_type)
        object_id = self._normalize_object_id(project_id, object_type, object_id)
        limit = self._limit(limit)
        domains = self._domains(allowed_security_domains)
        budget = _ContentBudget(
            char_budget, max_total_nodes=max_total_nodes,
            max_depth=max_depth, max_output_bytes=max_output_bytes)

        obj = self._load(project_id, object_type, object_id, domains)
        node = self._summary_node(project, object_type, obj, domains, budget)
        all_refs = self._references(project_id, object_type, obj, domains)
        refs, total, next_cursor = self._page_refs(
            project_id, object_type, object_id, all_refs, limit, cursor,
            domains)
        node["refs"] = refs
        node["related_total"] = total
        node["related_omitted"] = max(0, total - len(refs))
        payload = self._detail_payload(
            project_id, object_type, obj, domains, budget, limit)
        response = {
            "schema_version": RECALL_SCHEMA_VERSION,
            "level": 2,
            "project_id": project_id,
            "object": node,
            "detail": payload,
            "continuation": {
                "expand_by": "object_type + object_id",
                "order": "object_type,object_id,relation",
                "has_more": next_cursor is not None,
                "next_cursor": next_cursor,
            },
        }
        return self._finalize_response(response, budget)

    # ------------------------------------------------------------------ #
    # scope / loading
    # ------------------------------------------------------------------ #
    def _require_project(self, project_id: str) -> dict:
        if not isinstance(project_id, str) or not project_id.strip():
            raise RecallError("project_id must be a non-empty string")
        project = self.store.get_project(project_id)
        if not project:
            raise KeyError(f"no project {project_id}")
        return project

    @staticmethod
    def _object_type(value: str) -> str:
        if not isinstance(value, str) or value not in OBJECT_TYPES:
            raise RecallError(
                "object_type must be one of " + ", ".join(sorted(OBJECT_TYPES)))
        return value

    @staticmethod
    def _normalize_object_id(
        project_id: str, object_type: str, object_id: str | None,
    ) -> str:
        if object_type == "project" and object_id is None:
            return project_id
        if not isinstance(object_id, str) or not object_id.strip():
            raise RecallError(f"{object_type} recall requires object_id")
        value = object_id.strip()
        if object_type == "project" and value != project_id:
            raise RecallAccessError(
                f"project object {value} is outside requested project {project_id}")
        return value

    @staticmethod
    def _limit(value: int) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 100:
            raise RecallError("limit must be an integer from 1 to 100")
        return value

    @staticmethod
    def _domains(value: Iterable[str] | None) -> set[str] | None:
        if value is None:
            return None  # local operator view
        if isinstance(value, (str, bytes)):
            raise RecallError("allowed_security_domains must be an iterable of domains")
        domains = {str(item) for item in value}
        unknown = domains - SECURITY_DOMAINS
        if unknown:
            raise RecallError(
                "unknown security domains: " + ", ".join(sorted(unknown)))
        return domains

    @staticmethod
    def _domain_allowed(domain: str, domains: set[str] | None) -> bool:
        return domains is None or domain in domains

    def _page_refs(
        self,
        project_id: str,
        object_type: str,
        object_id: str,
        refs: list[dict],
        limit: int,
        cursor: str | None,
        domains: set[str] | None,
    ) -> tuple[list[dict], int, str | None]:
        """Return one stable relation page and a mutation-detecting cursor."""
        ordered = sorted(
            self._dedupe_refs(refs),
            key=lambda ref: (
                str(ref.get("object_type") or ""),
                str(ref.get("object_id") or ""),
                str(ref.get("relation") or ""),
            ))
        if len(ordered) > MAX_RELATION_SCAN:
            raise RecallError(
                f"relation scan exceeds maximum {MAX_RELATION_SCAN}; narrow the "
                "object scope before recalling")
        identity = [[
            ref.get("object_type"), ref.get("object_id"), ref.get("relation"),
            ref.get("security_domain"), bool(ref.get("expandable")),
        ] for ref in ordered]
        digest = hashlib.sha256(json.dumps(
            identity, ensure_ascii=False, sort_keys=False,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        scope = hashlib.sha256(json.dumps({
            "project_id": project_id,
            "object_type": object_type,
            "object_id": object_id,
            "domains": None if domains is None else sorted(domains),
        }, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        offset = 0
        if cursor is not None:
            decoded = self._decode_cursor(cursor)
            if decoded.get("v") != 1 or decoded.get("scope") != scope:
                raise RecallAccessError(
                    "recall cursor does not belong to this object/security scope")
            if decoded.get("digest") != digest:
                raise RecallError(
                    "recall cursor is stale because the relation set changed")
            offset = decoded.get("offset")
            if (not isinstance(offset, int) or isinstance(offset, bool)
                    or not 0 <= offset <= len(ordered)):
                raise RecallError("recall cursor has an invalid offset")
        page = ordered[offset:offset + limit]
        next_offset = offset + len(page)
        next_cursor = None
        if next_offset < len(ordered):
            next_cursor = self._encode_cursor({
                "v": 1, "scope": scope, "digest": digest,
                "offset": next_offset,
            })
        return page, len(ordered), next_cursor

    @staticmethod
    def _encode_cursor(payload: Mapping[str, Any]) -> str:
        raw = json.dumps(
            dict(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str) -> dict[str, Any]:
        if (not isinstance(cursor, str) or not cursor
                or len(cursor) > _MAX_CURSOR_CHARS):
            raise RecallError("cursor must be a bounded non-empty string")
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            raw = base64.urlsafe_b64decode(padded.encode("ascii"))
            value = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError, json.JSONDecodeError,
                binascii.Error) as exc:
            raise RecallError("cursor is not a valid recall cursor") from exc
        if not isinstance(value, dict):
            raise RecallError("cursor is not a valid recall cursor")
        return value

    def _finalize_response(self, response: dict[str, Any],
                           budget: _ContentBudget) -> dict[str, Any]:
        """Enforce hard serialized-byte and whole-response node ceilings.

        Detailed projections already truncate content incrementally.  This
        final guard covers the envelope itself (IDs, refs and numeric leaves),
        which cannot safely be accounted for by ``_ContentBudget.value``.
        """
        response = dict(response)
        for _ in range(4):
            response["budget"] = budget.report()
            nodes = self._node_count(response)
            output_bytes = self._json_bytes(response)
            depth = self._json_depth(response)
            if (nodes > budget.max_total_nodes
                    or output_bytes > budget.max_output_bytes
                    or depth > budget.max_depth):
                return self._minimal_response(response, budget)
            previous = (
                budget.output_nodes, budget.output_bytes, budget.output_depth)
            budget.note_final_shape(
                nodes=nodes, output_bytes=output_bytes, depth=depth)
            if previous == (nodes, output_bytes, depth):
                return response
        response["budget"] = budget.report()
        return response

    def _minimal_response(self, response: dict[str, Any],
                          budget: _ContentBudget) -> dict[str, Any]:
        if self._node_count(response) > budget.max_total_nodes:
            budget._truncate("max_total_nodes")
        if self._json_bytes(response) > budget.max_output_bytes:
            budget._truncate("max_output_bytes")
        if self._json_depth(response) > budget.max_depth:
            budget._truncate("max_depth")
        node = response.get("object") or {}
        continuation = dict(response.get("continuation") or {})
        # A cursor advances past the current page.  If the hard response budget
        # forces us to erase that page's refs, returning the already-advanced
        # cursor would make those object IDs unreachable to a well-behaved
        # client.  Fail closed so it can retry the *same* cursor with a smaller
        # limit or a larger owner-authorized budget.
        if node.get("refs") and continuation.get("next_cursor") is not None:
            raise RecallError(
                "recall relation page cannot fit the response budget without "
                "skipping references; reduce limit or increase the allowed "
                "response budget, then retry the same cursor")
        total = int(node.get("related_total") or 0)
        minimal_node = {
            key: node.get(key)
            for key in ("object_type", "object_id", "status", "expandable")
        }
        minimal_node.update({
            "refs": [], "related_total": total,
            "related_omitted": total,
        })
        minimal = {
            "schema_version": response.get("schema_version"),
            "level": response.get("level"),
            "project_id": response.get("project_id"),
            "object": minimal_node,
            "continuation": continuation,
        }
        if response.get("level") == 1:
            minimal["related"] = []
        else:
            minimal["detail"] = {
                "omitted": True, "reason": "response_budget_exceeded"}
        for _ in range(4):
            minimal["budget"] = budget.report()
            nodes = self._node_count(minimal)
            output_bytes = self._json_bytes(minimal)
            depth = self._json_depth(minimal)
            previous = (
                budget.output_nodes, budget.output_bytes, budget.output_depth)
            budget.note_final_shape(
                nodes=nodes, output_bytes=output_bytes, depth=depth)
            if (nodes <= budget.max_total_nodes
                    and output_bytes <= budget.max_output_bytes
                    and depth <= budget.max_depth
                    and previous == (nodes, output_bytes, depth)):
                return minimal
        # The validated minimums make this practically unreachable; retaining
        # an explicit failure is safer than returning an object over budget.
        raise RecallError("response budget is too small for the recall envelope")

    @classmethod
    def _node_count(cls, value: Any) -> int:
        if isinstance(value, Mapping):
            return 1 + sum(cls._node_count(item) for item in value.values())
        if isinstance(value, (list, tuple)):
            return 1 + sum(cls._node_count(item) for item in value)
        return 1

    @staticmethod
    def _json_bytes(value: Any) -> int:
        return len(json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), default=str).encode("utf-8"))

    @classmethod
    def _json_depth(cls, value: Any, depth: int = 0) -> int:
        if isinstance(value, Mapping):
            return max(
                [depth, *(cls._json_depth(item, depth + 1)
                           for item in value.values())])
        if isinstance(value, (list, tuple)):
            return max(
                [depth, *(cls._json_depth(item, depth + 1)
                           for item in value)])
        return depth

    def _load(self, project_id: str, object_type: str, object_id: str,
              domains: set[str] | None) -> dict:
        if object_type == "project":
            project = self.store.get_project(object_id)
            if not project:
                raise KeyError(f"no project {object_id}")
            if project["id"] != project_id:
                raise RecallAccessError(
                    f"project {object_id} is outside requested project {project_id}")
            return project
        if object_type == "goal":
            goal = self.goals.get(object_id)
            if not goal:
                raise KeyError(f"no goal {object_id}")
            self._require_owner(project_id, object_type, object_id,
                                goal.get("project_id"))
            return goal
        if object_type == "task":
            task = self.store.get(object_id)
            if not task:
                raise KeyError(f"no task {object_id}")
            self._require_owner(project_id, object_type, object_id,
                                task.get("project_id"))
            return task
        if object_type == "attempt":
            attempt = self.store.get_attempt(object_id)
            if not attempt:
                raise KeyError(f"no attempt {object_id}")
            task = self.store.get(attempt.get("work_item_id"))
            if not task:
                raise RecallError(
                    f"attempt {object_id} has dangling task {attempt.get('work_item_id')}")
            self._require_owner(project_id, object_type, object_id,
                                task.get("project_id"))
            return attempt
        if object_type == "session":
            session = self.store.get_session_reference(object_id)
            if not session:
                raise KeyError(f"no session reference {object_id}")
            self._require_owner(project_id, object_type, object_id,
                                session.get("project_id"))
            task_id = session.get("work_item_id")
            if task_id:
                task = self.store.get(task_id)
                if not task:
                    raise RecallError(
                        f"session {object_id} has dangling task {task_id}")
                self._require_owner(
                    project_id, "task", task_id, task.get("project_id"))
            attempt_id = session.get("attempt_id")
            if attempt_id:
                attempt = self.store.get_attempt(attempt_id)
                if not attempt:
                    raise RecallError(
                        f"session {object_id} has dangling attempt {attempt_id}")
                if task_id and attempt.get("work_item_id") != task_id:
                    raise RecallAccessError(
                        f"session {object_id} binds task {task_id} but attempt "
                        f"{attempt_id} belongs to {attempt.get('work_item_id')}")
            return session
        if object_type == "evidence":
            evidence_id = self._evidence_id(object_id)
            evidence = self.store.get_evidence(evidence_id)
            if not evidence:
                raise KeyError(f"no evidence {object_id}")
            self._validate_evidence_link(project_id, object_id, evidence)
            domain = self._evidence_domain(evidence)
            if not self._domain_allowed(domain, domains):
                raise RecallAccessError(
                    f"evidence {object_id} is in unauthorized security domain "
                    f"{domain!r}")
            return evidence
        entry = self.store.get_context_entry(object_id)
        if not entry:
            raise KeyError(f"no context entry {object_id}")
        self._require_owner(project_id, object_type, object_id,
                            entry.get("project_id"))
        domain = str(entry.get("security_domain") or "local")
        if not self._domain_allowed(domain, domains):
            raise RecallAccessError(
                f"context entry {object_id} is in unauthorized security domain "
                f"{domain!r}")
        return entry

    @staticmethod
    def _require_owner(project_id: str, object_type: str, object_id: str,
                       actual: Any) -> None:
        if actual != project_id:
            raise RecallAccessError(
                f"{object_type} {object_id} belongs to project {actual}, "
                f"not {project_id}")

    @staticmethod
    def _evidence_id(value: str) -> int:
        raw = value.removeprefix("evidence:")
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise RecallError("evidence object_id must be evidence:<integer>") from exc

    # ------------------------------------------------------------------ #
    # summaries and relations
    # ------------------------------------------------------------------ #
    def _summary_node(self, project: dict, object_type: str, obj: dict,
                      domains: set[str] | None,
                      budget: _ContentBudget) -> dict[str, Any]:
        local = self._domain_allowed("local", domains)
        object_id = self._id_for(object_type, obj)
        status = self._status_for(object_type, obj)
        if object_type == "project":
            name = obj.get("name") if local else None
            text = f"Project {name or object_id}; active={bool(obj.get('active'))}"
        elif object_type == "goal":
            text = (obj.get("goal_text") if local else None) or \
                f"Goal {object_id}; status={status}"
        elif object_type == "task":
            title = obj.get("title") if local else None
            text = title or (
                f"WorkItem {object_id}; state={obj.get('state')}; "
                f"verdict={obj.get('verdict')}")
        elif object_type == "attempt":
            reason = obj.get("reason") if local else None
            text = reason or f"Attempt {object_id}; state={status}"
        elif object_type == "session":
            session_id = obj.get("session_id") if local else None
            text = (f"SessionReference {object_id}"
                    + (f" ({session_id})" if session_id else ""))
        elif object_type == "evidence":
            text = f"Ledger evidence {object_id}; kind={obj.get('kind')}"
        else:
            text = f"{obj.get('kind')} knowledge: {obj.get('title')}"
        return {
            "object_type": object_type,
            "object_id": object_id,
            "status": status,
            "summary": budget.clip(text, per_field=400),
            "expandable": True,
        }

    @staticmethod
    def _id_for(object_type: str, obj: dict) -> str:
        if object_type == "project":
            return str(obj.get("id"))
        if object_type == "goal":
            return str(obj.get("goal_id"))
        if object_type == "task":
            return str(obj.get("task_id"))
        if object_type in ("attempt", "session", "context_entry"):
            return str(obj.get("id"))
        return f"evidence:{obj.get('id')}"

    @staticmethod
    def _status_for(object_type: str, obj: dict) -> str | None:
        if object_type == "project":
            return "active" if obj.get("active") else "disabled"
        if object_type == "goal":
            return obj.get("status")
        if object_type == "task":
            verdict = obj.get("verdict")
            return verdict if verdict and verdict != "pending" else obj.get("state")
        if object_type == "attempt":
            return obj.get("state") or "open"
        if object_type == "session":
            return "bound" if obj.get("attempt_id") and obj.get("work_item_id") \
                else "legacy_placeholder"
        if object_type == "evidence":
            return obj.get("kind")
        return obj.get("verification")

    @staticmethod
    def _ref(object_type: str, object_id: Any, relation: str, *,
             expandable: bool = True, security_domain: str | None = None) -> dict:
        out = {
            "object_type": object_type,
            "object_id": str(object_id),
            "relation": relation,
            "expandable": bool(expandable),
        }
        if security_domain:
            out["security_domain"] = security_domain
        return out

    @staticmethod
    def _dedupe_refs(refs: list[dict]) -> list[dict]:
        out, seen = [], set()
        for ref in refs:
            key = (ref.get("object_type"), ref.get("object_id"),
                   ref.get("relation"))
            if key in seen:
                continue
            seen.add(key)
            out.append(ref)
        return out

    def _references(self, project_id: str, object_type: str, obj: dict,
                    domains: set[str] | None) -> list[dict]:
        if object_type == "project":
            refs = [self._ref("goal", goal["goal_id"], "has_goal")
                    for goal in self._project_goals(project_id)]
            refs.extend(
                self._ref("task", task["task_id"], "has_task")
                for task in self._project_tasks(project_id))
            refs.extend(
                self._ref("context_entry", entry["id"], "has_context_entry",
                          security_domain=entry.get("security_domain"))
                for entry in self._visible_context_entries(project_id, domains))
            return refs
        if object_type == "goal":
            goal_ref = obj.get("goal_ref")
            tasks = [task for task in self._project_tasks(project_id)
                     if (task.get("metadata") or {}).get("ld_goal_ref") == goal_ref]
            tasks.sort(key=lambda item: item.get("created_at") or 0)
            return [self._ref("task", task["task_id"], "goal_task")
                    for task in tasks]
        if object_type == "task":
            task_id = obj["task_id"]
            attempts = self._attempts(task_id)
            refs = [self._ref("attempt", item["id"], "has_attempt")
                    for item in attempts]
            for attempt in attempts:
                session = self.store.session_reference_for_attempt(attempt["id"])
                if session:
                    self._load(project_id, "session", session["id"], domains)
                    refs.append(self._ref(
                        "session", session["id"], "has_session_reference"))
            for evidence in self._evidence_rows(task_id):
                self._validate_evidence_link(
                    project_id, f"evidence:{evidence['id']}", evidence)
                domain = self._evidence_domain(evidence)
                if not self._domain_allowed(domain, domains):
                    continue
                refs.append(self._ref(
                    "evidence", f"evidence:{evidence['id']}", "has_evidence",
                    security_domain=domain))
            for entry_id in obj.get("context_entry_refs") or []:
                entry = self._task_context_entry(
                    project_id, task_id, entry_id)
                if self._domain_allowed(
                        str(entry.get("security_domain") or "local"), domains):
                    refs.append(self._ref(
                        "context_entry", entry_id, "used_context_entry",
                        security_domain=entry.get("security_domain")))
            metadata = obj.get("metadata") or {}
            for key, kind, relation in (
                ("source_work_item_id", "task", "handoff_source_task"),
                ("source_attempt_id", "attempt", "handoff_source_attempt"),
                ("source_session_reference_id", "session", "handoff_source_session"),
            ):
                if metadata.get(key):
                    # A malformed historical metadata row must not turn into a
                    # cross-project ID oracle.  Validate every relation before
                    # returning even its spill pointer.
                    self._load(
                        project_id, kind, str(metadata[key]), domains)
                    refs.append(self._ref(kind, metadata[key], relation))
            if metadata.get("checkpoint_ref"):
                refs.append(self._external_ref(
                    metadata["checkpoint_ref"], "checkpoint",
                    "handoff_checkpoint"))
            return refs
        if object_type == "attempt":
            refs = [self._ref("task", obj["work_item_id"], "attempt_of")]
            session = self.store.session_reference_for_attempt(obj["id"])
            if session:
                self._load(project_id, "session", session["id"], domains)
                refs.append(self._ref("session", session["id"], "ran_in_session"))
            for evidence in self._evidence_rows(obj["work_item_id"]):
                if evidence.get("attempt_id") != obj["id"]:
                    continue
                self._validate_evidence_link(
                    project_id, f"evidence:{evidence['id']}", evidence)
                domain = self._evidence_domain(evidence)
                if not self._domain_allowed(domain, domains):
                    continue
                refs.append(self._ref(
                    "evidence", f"evidence:{evidence['id']}", "produced_evidence",
                    security_domain=domain))
            return refs
        if object_type == "session":
            refs = []
            if obj.get("work_item_id"):
                refs.append(self._ref("task", obj["work_item_id"], "session_for_task"))
            if obj.get("attempt_id"):
                refs.append(self._ref(
                    "attempt", obj["attempt_id"], "session_for_attempt"))
            return refs
        if object_type == "evidence":
            refs = [self._ref("task", obj["work_item_id"], "evidence_for_task")]
            if obj.get("attempt_id"):
                refs.append(self._ref(
                    "attempt", obj["attempt_id"], "evidence_for_attempt"))
            return refs
        refs = []
        source = str(obj.get("source") or "")
        if source.startswith("task:"):
            task_id = source.split(":", 2)[1]
            if task_id:
                self._load(project_id, "task", task_id, domains)
                refs.append(self._ref("task", task_id, "context_source_task"))
        for evidence_ref in obj.get("evidence_refs") or []:
            refs.append(self._external_ref(
                evidence_ref, "evidence_ref", "context_evidence_reference",
                security_domain=obj.get("security_domain")))
        return refs

    @staticmethod
    def _external_ref(value: Any, object_type: str, relation: str, *,
                      security_domain: str | None = None,
                      include_raw: bool = False) -> dict[str, Any]:
        raw = str(value)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        out = {
            "object_type": object_type,
            "object_id": f"external-ref:sha256:{digest}",
            "relation": relation,
            "expandable": False,
            "reference_kind": "external",
            "reference_summary": "external reference; raw value omitted",
        }
        if security_domain:
            out["security_domain"] = security_domain
        if include_raw:
            out["raw_reference"] = raw
        return out

    def _visible_context_entries(self, project_id: str,
                                 domains: set[str] | None) -> list[dict]:
        return [entry for entry in self._context_rows(project_id)
                if self._domain_allowed(
                    str(entry.get("security_domain") or "local"), domains)]

    def _project_tasks(self, project_id: str) -> list[dict]:
        """All project tasks; response limits are applied after relationships.

        ``work_items_for_project`` is an indexed SQL read.  ``+1`` distinguishes
        an exact maximum from overflow, which fails closed instead of silently
        severing an older Goal -> WorkItem relation or scanning without bound.
        """
        return self._bounded_rows(
            self.store.work_items_for_project(
                project_id, limit=MAX_RELATION_SCAN + 1),
            "project WorkItem")

    def _project_goals(self, project_id: str) -> list[dict]:
        return self._bounded_rows(
            self.goals.find_by_project(
                project_id, limit=MAX_RELATION_SCAN + 1),
            "project Goal")

    def _context_rows(self, project_id: str) -> list[dict]:
        return self._bounded_rows(
            self.store.context_entries(
                project_id, limit=MAX_RELATION_SCAN + 1),
            "project Context Entry")

    def _attempts(self, task_id: str) -> list[dict]:
        return self._bounded_rows(
            self.store.attempts(task_id, limit=MAX_RELATION_SCAN + 1),
            "task Attempt")

    def _evidence_rows(self, task_id: str) -> list[dict]:
        return self._bounded_rows(
            self.store.evidence(task_id, limit=MAX_RELATION_SCAN + 1),
            "task Evidence")

    @staticmethod
    def _bounded_rows(rows: list[dict], label: str) -> list[dict]:
        if len(rows) > MAX_RELATION_SCAN:
            raise RecallError(
                f"{label} scan exceeds maximum {MAX_RELATION_SCAN}; narrow the "
                "object scope before recalling")
        return rows

    def _task_context_entry(self, project_id: str, task_id: str,
                            entry_id: str) -> dict:
        entry = self.store.get_context_entry(entry_id)
        if not entry:
            raise RecallError(
                f"task {task_id} has dangling context entry {entry_id}")
        self._require_owner(
            project_id, "context_entry", entry_id, entry.get("project_id"))
        return entry

    def _validate_evidence_link(self, project_id: str, object_id: str,
                                evidence: Mapping[str, Any]) -> None:
        task_id = evidence.get("work_item_id")
        task = self.store.get(task_id)
        if not task:
            raise RecallError(
                f"evidence {object_id} has dangling task {task_id}")
        self._require_owner(
            project_id, "evidence", object_id, task.get("project_id"))
        attempt_id = evidence.get("attempt_id")
        if not attempt_id:
            return
        attempt = self.store.get_attempt(attempt_id)
        if not attempt:
            raise RecallError(
                f"evidence {object_id} has dangling attempt {attempt_id}")
        if attempt.get("work_item_id") != task_id:
            raise RecallAccessError(
                f"evidence {object_id} belongs to task {task_id} but attempt "
                f"{attempt_id} belongs to {attempt.get('work_item_id')}")

    @staticmethod
    def _evidence_domain(evidence: Mapping[str, Any]) -> str:
        payload = evidence.get("payload") or {}
        domain = payload.get("security_domain") if isinstance(payload, Mapping) else None
        return str(domain) if domain in SECURITY_DOMAINS else "local"

    # ------------------------------------------------------------------ #
    # level 2 projections
    # ------------------------------------------------------------------ #
    def _detail_payload(self, project_id: str, object_type: str, obj: dict,
                        domains: set[str] | None, budget: _ContentBudget,
                        limit: int) -> dict[str, Any]:
        local = self._domain_allowed("local", domains)
        if object_type == "project":
            progress = self.service.get_project_progress(
                project_id, allowed_security_domains=(domains if domains is not None
                                                       else None))
            all_entries = self._context_rows(project_id)
            visible = self._visible_context_entries(project_id, domains)
            out = {
                "project_id": project_id,
                "active": bool(obj.get("active")),
                "default_adapter": obj.get("default_adapter"),
                "context_version": self.store.context_version(project_id),
                "task_counts": progress.get("counts") or {},
                "goal_count": len(self._project_goals(project_id)),
                "context_visibility": {
                    "visible": len(visible),
                    "omitted": len(all_entries) - len(visible),
                    "allowed_security_domains": (
                        sorted(domains) if domains is not None else ["operator"]),
                },
            }
            if local:
                out["name"] = budget.clip(obj.get("name"), per_field=300)
            return out
        if object_type == "goal":
            decisions = []
            for row in self.goals.decisions(obj["goal_id"], limit=limit):
                decision = row.get("decision") or {}
                outcome = row.get("outcome") or {}
                item = {
                    "step": row.get("step"),
                    "state": row.get("state"),
                    "action": decision.get("action") or outcome.get("action"),
                    "task_id": outcome.get("task_id"),
                    "next_state": outcome.get("next_state"),
                    "evidence_refs": budget.value(
                        list(decision.get("evidence_refs") or [])[:limit],
                        per_string=500, max_items=limit),
                    "created_at": row.get("created_at"),
                }
                if local:
                    item["reason"] = budget.clip(
                        decision.get("reason") or outcome.get("reason"),
                        per_field=400)
                decisions.append(item)
            out = {
                "goal_id": obj.get("goal_id"),
                "goal_ref": obj.get("goal_ref"),
                "status": obj.get("status"),
                "current_task_id": obj.get("current_task_id"),
                "decision_count": obj.get("decision_count"),
                "model_decision_count": obj.get("model_decision_count"),
                "task_count": obj.get("task_count"),
                "max_decisions_total": obj.get("max_decisions_total"),
                "max_tasks_total": obj.get("max_tasks_total"),
                "decision_history": decisions,
                "decision_history_limited": int(obj.get("decision_count") or 0) > limit,
            }
            if local:
                out["goal_text"] = budget.clip(obj.get("goal_text"), per_field=2_000)
            else:
                out["goal_text_omitted"] = True
            return out
        if object_type == "task":
            status = self.service.get_task_status(obj["task_id"])
            metadata = obj.get("metadata") or {}
            out = {key: status.get(key) for key in (
                "task_id", "project_id", "state", "verdict", "queue_status",
                "attention_flags", "workspace_id", "context_version",
                "current_attempt_id", "ld_goal_ref",
                "created_at", "updated_at",
            )}
            out["execution_result"] = {
                "worker_state": status.get("state"),
                "verifier_verdict": status.get("verdict"),
                "worker_success_is_verification": False,
            }
            out["why_attention"] = self._why_attention(status, budget, local)
            out["attempt_count"] = len(self._attempts(obj["task_id"]))
            evidence_rows = self._evidence_rows(obj["task_id"])
            visible_evidence = [
                item for item in evidence_rows
                if self._domain_allowed(self._evidence_domain(item), domains)]
            out["evidence_count"] = len(visible_evidence)
            out["handoff"] = bool(metadata.get("handoff"))
            out["handoff_source"] = budget.value({
                key: metadata.get(key)
                for key in (
                    "source_work_item_id", "source_attempt_id",
                    "source_session_reference_id", "handoff_signature",
                )
                if metadata.get(key) is not None
            }, per_string=800, max_items=limit)
            if metadata.get("checkpoint_ref") is not None:
                out["checkpoint_reference"] = budget.value(
                    self._external_ref(
                        metadata["checkpoint_ref"], "checkpoint",
                        "handoff_checkpoint", include_raw=domains is None),
                    per_string=50_000, max_items=limit)
            context_entries = []
            for entry_id in obj.get("context_entry_refs") or []:
                entry = self._task_context_entry(
                    project_id, obj["task_id"], entry_id)
                domain = str(entry.get("security_domain") or "local")
                if not self._domain_allowed(domain, domains):
                    continue
                context_entries.append({
                    "id": entry["id"],
                    "kind": entry.get("kind"),
                    "title": budget.clip(entry.get("title"), per_field=500),
                    "verification": entry.get("verification"),
                    "version": entry.get("version"),
                    "security_domain": domain,
                })
                if len(context_entries) >= limit:
                    break
            out["context_entries"] = context_entries
            out["effective_prompt_omitted"] = True
            if local:
                out.update({
                    "title": budget.clip(status.get("title"), per_field=500),
                    "worker_job_id": budget.clip(
                        status.get("worker_job_id"), per_field=500),
                    "original_task": budget.clip(
                        obj.get("original_task"), per_field=2_000),
                    "assertions": budget.value(
                        (status.get("assertions") or [])[:limit],
                        per_string=500, max_items=limit),
                    "assertion_results": budget.value(
                        (status.get("assertion_results") or [])[:limit],
                        per_string=500, max_items=limit),
                    "warnings": budget.value(
                        (status.get("warnings") or [])[:limit],
                        per_string=500, max_items=limit),
                    "events": [self._event_projection(event, budget)
                               for event in self.store.events(
                                   obj["task_id"], limit=limit)],
                })
            else:
                out.update({"title_omitted": True, "task_text_omitted": True,
                            "event_payloads_omitted": True})
            return out
        if object_type == "attempt":
            out = {key: obj.get(key) for key in (
                "id", "work_item_id", "state", "created_at", "ended_at",
            )}
            if local:
                out["reason"] = budget.clip(obj.get("reason"), per_field=800)
                out["worker_job_id"] = budget.clip(
                    obj.get("worker_job_id"), per_field=500)
            else:
                out["reason_omitted"] = True
                out["worker_job_id_omitted"] = True
            session = self.store.session_reference_for_attempt(obj["id"])
            if session:
                self._load(project_id, "session", session["id"], domains)
            out["session_reference_id"] = session.get("id") if session else None
            evidence_rows = [
                item for item in self._evidence_rows(obj["work_item_id"])
                if item.get("attempt_id") == obj["id"]]
            visible_evidence = [
                item for item in evidence_rows
                if self._domain_allowed(self._evidence_domain(item), domains)]
            out["evidence_ids"] = [
                f"evidence:{item['id']}" for item in visible_evidence[:limit]]
            out["evidence_ids_limited"] = len(visible_evidence) > limit
            return out
        if object_type == "session":
            out = {key: obj.get(key) for key in (
                "id", "project_id", "workspace_id", "node_id", "adapter",
                "work_item_id", "attempt_id", "created_at", "updated_at",
            )}
            out["binding_status"] = self._status_for("session", obj)
            if local:
                out["worker_instance_id"] = budget.clip(
                    obj.get("worker_instance_id"), per_field=300)
                out["session_id"] = budget.clip(obj.get("session_id"), per_field=500)
            else:
                out["session_identity_omitted"] = True
            return out
        if object_type == "evidence":
            domain = self._evidence_domain(obj)
            return {
                "evidence_id": f"evidence:{obj.get('id')}",
                "work_item_id": obj.get("work_item_id"),
                "attempt_id": obj.get("attempt_id"),
                "kind": obj.get("kind"),
                "security_domain": domain,
                "payload": budget.value(
                    obj.get("payload") or {}, per_string=2_000,
                    max_items=limit),
                "created_at": obj.get("created_at"),
            }
        revisions = self.store.context_revisions(obj["id"])
        detail = {
            "entry_id": obj.get("id"),
            "project_id": obj.get("project_id"),
            "kind": obj.get("kind"),
            "title": budget.clip(obj.get("title"), per_field=500),
            "body": budget.clip(obj.get("body"), per_field=3_000),
            "tags": budget.value(
                (obj.get("tags") or [])[:limit], per_string=200,
                max_items=limit),
            "verification": obj.get("verification"),
            "security_domain": obj.get("security_domain"),
            "evidence_refs": budget.value(
                [self._external_ref(
                    item, "evidence_ref", "context_evidence_reference",
                    security_domain=obj.get("security_domain"),
                    include_raw=domains is None)
                 for item in (obj.get("evidence_refs") or [])[:limit]],
                per_string=50_000,
                max_items=limit),
            "version": obj.get("version"),
            "revision_count": len(revisions),
            "revisions": [{
                "version": item.get("version"),
                "changed_at": item.get("changed_at"),
                "changed_by": budget.clip(item.get("changed_by"), per_field=200),
                "note": budget.clip(item.get("note"), per_field=300),
            } for item in revisions[-limit:]],
            "created_at": obj.get("created_at"),
            "updated_at": obj.get("updated_at"),
        }
        source = str(obj.get("source") or "")
        if source.startswith("task:") or domains is None:
            detail["source"] = budget.clip(source, per_field=50_000)
        elif source:
            detail["source_reference"] = budget.value(
                self._external_ref(
                    source, "source_ref", "context_source",
                    security_domain=obj.get("security_domain")),
                per_string=500, max_items=limit)
            detail["source_omitted"] = True
        return detail

    @staticmethod
    def _event_projection(event: dict, budget: _ContentBudget) -> dict:
        return {
            "event_id": event.get("id"),
            "attempt_id": event.get("attempt_id"),
            "type": event.get("type"),
            "payload": budget.value(event.get("payload") or {}, per_string=800),
            "created_at": event.get("created_at"),
        }

    @staticmethod
    def _why_attention(status: dict, budget: _ContentBudget,
                       local: bool) -> dict:
        reasons = list(status.get("attention_flags") or [])
        verdict = status.get("verdict")
        if verdict in ("rejected", "inconclusive"):
            reasons.append(f"verdict:{verdict}")
        failed = [item for item in status.get("assertion_results") or []
                  if not item.get("passed", False)]
        out = {
            "reasons": list(dict.fromkeys(reasons)),
            "requires_human": bool(reasons),
            "failed_assertion_count": len(failed),
        }
        if local:
            out["failed_assertions"] = budget.value(failed, per_string=500)
            out["warnings"] = budget.value(
                status.get("warnings") or [], per_string=500)
            attempt = status.get("current_attempt") or {}
            out["current_attempt_reason"] = budget.clip(
                attempt.get("reason"), per_field=800)
        else:
            out["details_omitted"] = True
        return out


__all__ = [
    "OBJECT_TYPES", "RECALL_SCHEMA_VERSION", "ProjectRecall",
    "RecallAccessError", "RecallError",
]
