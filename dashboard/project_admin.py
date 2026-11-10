"""Operator-driven project registration for the authenticated console.

Deliberately separate from :mod:`dashboard.chat`: registering a project is a
Human Owner management operation, not a conversational write intent.  It must
never go through the LD, the model, the GoalRunner or any Worker.  This module
only validates a local path and calls the *existing*
``ControlPlane.register_project``, which owns the Git-root boundary.

The HTTP layer is responsible for authentication, same-origin and Host
checks; this module is responsible for the filesystem boundary:

* absolute, existing, resolved local path (no UNC / network shares, no ``..``
  escape, no symlink resolving outside the resolved root);
* exactly a Git working-tree top level (enforced by ControlPlane, not
  reimplemented here);
* no nesting with an already registered project root — a nested root would
  silently change ``resolve_project_for_cwd``'s longest-prefix match and thus
  re-attribute existing work items.

``default_adapter``, policy, concurrency and active state are fixed here and
are NOT request fields: the browser may choose *which repository*, never how
work in it will be dispatched.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from controlplane.plane import ControlPlane
from controlplane.policies import AGENT_WORKTREE
from controlplane.store import TaskStore

from .ledger import NameOnlyAdapter

MAX_ROOT_CHARS = 4096
MAX_NAME_CHARS = 120

#: Names are labels, not paths: control characters and both separators are
#: refused so a name can never be mistaken for a root in logs or projections.
_NAME_RE = re.compile(r"^[^\x00-\x1f/\\]{1,%d}$" % MAX_NAME_CHARS)


class ProjectRegistrationError(ValueError):
    """A stable, path-free rejection the HTTP layer may classify as 400."""


def _clean_name(name: Any) -> str:
    if not isinstance(name, str):
        raise ProjectRegistrationError("project name must be a string")
    clean = name.strip()
    if not clean:
        raise ProjectRegistrationError("project name must not be empty")
    if len(clean) > MAX_NAME_CHARS:
        raise ProjectRegistrationError("project name is too long")
    if not _NAME_RE.match(clean):
        raise ProjectRegistrationError(
            "project name must not contain path separators or control characters")
    return clean


def _clean_root(root: Any) -> Path:
    if not isinstance(root, str):
        raise ProjectRegistrationError("repository path must be a string")
    raw = root.strip()
    if not raw:
        raise ProjectRegistrationError("repository path must not be empty")
    if len(raw) > MAX_ROOT_CHARS:
        raise ProjectRegistrationError("repository path is too long")
    if "\x00" in raw:
        raise ProjectRegistrationError("repository path contains a null byte")

    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ProjectRegistrationError("repository path must be absolute")
    # A UNC anchor is absolute on Windows but is a network share, not a local
    # repository this control plane owns.  Remote execution is not supported.
    if path.anchor.startswith("\\\\"):
        raise ProjectRegistrationError("network paths are not supported")
    # ``..`` in the *supplied* path is rejected even though resolve() would
    # collapse it: an explicit traversal is never an operator typo we should
    # silently reinterpret.
    if ".." in path.parts:
        raise ProjectRegistrationError("repository path must not contain '..'")

    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProjectRegistrationError(
            "repository path does not exist or cannot be resolved") from exc
    if not resolved.is_dir():
        raise ProjectRegistrationError("repository path is not a directory")
    return resolved


def _reject_nested(store: TaskStore, candidate: Path) -> None:
    for project in store.all_projects():
        try:
            other = Path(str(project.get("root") or "")).resolve()
        except (OSError, RuntimeError):
            continue
        if other == candidate:
            # Same root: an idempotent re-registration, handled by the store.
            continue
        if candidate in other.parents or other in candidate.parents:
            raise ProjectRegistrationError(
                "repository path overlaps an already registered project root "
                "(nested roots would change existing task attribution)")


def register_project(ledger_path: Path | str, *, root: Any,
                     name: Any) -> dict[str, Any]:
    """Register one local Git root and return a narrow, operator-facing view."""
    clean_name = _clean_name(name)
    candidate = _clean_root(root)

    store = TaskStore(ledger_path)
    try:
        same_name = [row for row in store.all_projects()
                     if row.get("name") == clean_name]
        if same_name:
            registered_root = Path(str(same_name[0].get("root") or "")).resolve()
            if registered_root != candidate:
                raise ProjectRegistrationError(
                    "project name is already registered for another root")
            # Truly idempotent: re-registering the same name+root writes
            # nothing at all (not even an updated_at bump).
            row = same_name[0]
            return {
                "project_id": row.get("id"),
                "name": row.get("name"),
                "root": row.get("root"),
                "active": bool(row.get("active")),
                "created": False,
                "dispatch_policy": "unchanged (existing registration reused)",
                "next_step": "registration_reused",
            }
        _reject_nested(store, candidate)
        plane = ControlPlane(NameOnlyAdapter("dashboard-admin"), store)
        row = plane.register_project(
            clean_name, str(candidate),
            default_adapter="codebuddy",
            policy=AGENT_WORKTREE,
            concurrency_limit=1,
            active=True)
    except ProjectRegistrationError:
        raise
    except ValueError as exc:
        # ControlPlane rejects non-Git roots, subdirectories of a Git work tree
        # and name/root conflicts.  Keep the message short and path-free.
        raise ProjectRegistrationError(
            "repository path is not a Git top-level directory, or is already "
            "registered under another name") from exc
    finally:
        store.close()

    return {
        "project_id": row.get("id"),
        "name": row.get("name"),
        "root": row.get("root"),
        "active": bool(row.get("active")),
        "created": True,
        "dispatch_policy": "agent/worktree/acceptEdits (fixed; not caller-set)",
        "next_step": "project_registered_no_work_dispatched",
    }


__all__ = ["ProjectRegistrationError", "register_project"]
