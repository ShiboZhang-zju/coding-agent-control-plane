"""Least-privilege, non-interactive permission profile for CodeBuddy worker jobs.

Why this module exists
----------------------
A CodeBuddy agent job dispatched over ``POST /api/v1/jobs`` runs unattended: there
is nobody to answer an approval prompt, so every tool call that is not already
allowed is answered "deny".  Measured on CodeBuddy 2.179.3 (see
``evidence/permissions/T68-*.json``):

* the **job-level** ``permissionMode`` (``acceptEdits``) only auto-approves file
  edits; ``Bash`` / ``PowerShell`` still fall through to the prompt path;
* ``--allowedTools`` / ``--disallowedTools`` passed to ``codebuddy --serve`` have
  **no effect** on HTTP-dispatched jobs (they are per-process CLI options);
* ``permissions.allow`` in the serve process' own settings
  (``$CODEBUDDY_CONFIG_DIR/settings.json``) **is** the effective layer: a job
  started afterwards inherits it, and ``permissions.deny`` wins over ``allow``;
* with no rule at all the default classifier intermittently lets a read-only git
  command through and denies the next one — that non-determinism is worse than a
  hard deny, because a multi-step task can be half-executed.

So the control plane ships an explicit allow/deny list instead of a mode.  This
is deliberately *not* ``bypassPermissions``/``-y``: the list names commands, and
everything not named stays denied.

Scope
-----
This is CodeBuddy vendor knowledge (file layout, rule syntax, which layer wins),
so it lives under ``adapters/codebuddy/`` and never leaks into ``adapters/base.py``
or ``controlplane/``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

# --------------------------------------------------------------------------- #
# The profile
# --------------------------------------------------------------------------- #
#: Read-only inspection + the narrow set of write operations an unattended
#: coding task needs: stage and commit its own work, run its own tests.
ALLOW_RULES: tuple[str, ...] = (
    # file tools: the job-level acceptEdits already covers edits, but naming them
    # keeps the profile honest if a caller dispatches with permissionMode=default.
    "Read", "Glob", "Grep", "Write", "Edit", "NotebookEdit", "TodoWrite",
    # git: inspect the worktree it was given, then commit inside it.
    "Bash(git status:*)",
    "Bash(git rev-parse:*)",
    "Bash(git log:*)",
    "Bash(git diff:*)",
    "Bash(git show:*)",
    "Bash(git branch:*)",
    "Bash(git switch:*)",
    "Bash(git checkout:*)",
    "Bash(git add:*)",
    "Bash(git commit:*)",
    "Bash(git stash:*)",
    # tests: the interpreter and the two common runners, nothing else.
    "Bash(python:*)",
    "Bash(python3:*)",
    "Bash(pytest:*)",
    # PowerShell keeps its own rule namespace (verified separately: the shell
    # rule syntax is shared, but the tool name is not -- see module docstring).
    "PowerShell(git status:*)",
    "PowerShell(git rev-parse:*)",
    "PowerShell(git log:*)",
    "PowerShell(git diff:*)",
    "PowerShell(git show:*)",
    "PowerShell(git branch:*)",
    "PowerShell(git switch:*)",
    "PowerShell(git checkout:*)",
    "PowerShell(git add:*)",
    "PowerShell(git commit:*)",
    "PowerShell(git stash:*)",
    "PowerShell(python:*)",
    "PowerShell(python3:*)",
    "PowerShell(pytest:*)",
)

#: Anything that reaches outside the disposable workspace, rewrites history, or
#: installs/uninstalls software stays denied even if a broader allow rule is
#: added later.  ``deny`` outranks ``allow`` in this CodeBuddy build.
DENY_RULES: tuple[str, ...] = (
    "Bash(git push:*)",
    "Bash(git reset --hard:*)",
    "Bash(git clean:*)",
    "Bash(git rebase:*)",
    "Bash(rm:*)",
    "Bash(rmdir:*)",
    "Bash(del:*)",
    "Bash(curl:*)",
    "Bash(wget:*)",
    "Bash(ssh:*)",
    "Bash(scp:*)",
    "Bash(npm publish:*)",
    "Bash(pip install:*)",
    "Bash(pip uninstall:*)",
    "PowerShell(git push:*)",
    "PowerShell(git reset --hard:*)",
    "PowerShell(git clean:*)",
    "PowerShell(git rebase:*)",
    "PowerShell(Remove-Item:*)",
    "PowerShell(rm:*)",
    "PowerShell(rmdir:*)",
    "PowerShell(del:*)",
    "PowerShell(curl:*)",
    "PowerShell(wget:*)",
    "PowerShell(ssh:*)",
    "PowerShell(scp:*)",
    "PowerShell(npm publish:*)",
    "PowerShell(pip install:*)",
    "PowerShell(pip uninstall:*)",
    "PowerShell(Invoke-WebRequest:*)",
    "PowerShell(Invoke-RestMethod:*)",
    "PowerShell(Start-Process:*)",
    "PowerShell(Set-ExecutionPolicy:*)",
    "PowerShell(Stop-Process:*)",
)

PROFILE_NAME = "acp-worker-minimal"


def profile() -> dict:
    """The profile as plain data (safe to record in evidence)."""
    return {"name": PROFILE_NAME, "allow": list(ALLOW_RULES), "deny": list(DENY_RULES)}


def permission_block() -> dict:
    """The ``permissions`` object to merge into a settings file."""
    return {"allow": list(ALLOW_RULES), "deny": list(DENY_RULES)}


# --------------------------------------------------------------------------- #
# settings file handling
# --------------------------------------------------------------------------- #
def settings_path(config_dir: str | Path) -> Path:
    """``$CODEBUDDY_CONFIG_DIR/settings.json``."""
    return Path(config_dir) / "settings.json"


def merge_into_settings(path: str | Path, *, extra_allow: Iterable[str] = (),
                        extra_deny: Iterable[str] = ()) -> dict:
    """Merge the profile into an existing settings file without losing other keys.

    The gateway block (server password) and plugin/agent settings must survive:
    rewriting the file wholesale would break authentication and re-enable hooks.
    ``deny`` is kept a superset so a caller can only ever add restrictions.
    """
    target = Path(path)
    data: dict = {}
    if target.exists():
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
            data = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            data = {}
    perms = dict(data.get("permissions") or {})
    allow = list(dict.fromkeys([*(perms.get("allow") or []), *ALLOW_RULES,
                                *[str(r) for r in extra_allow]]))
    deny = list(dict.fromkeys([*(perms.get("deny") or []), *DENY_RULES,
                               *[str(r) for r in extra_deny]]))
    # A rule in both lists would be resolved by the vendor's precedence (deny
    # wins) but is still ambiguous to a reader: keep it denied, drop the allow.
    allow = [rule for rule in allow if rule not in set(deny)]
    perms["allow"] = allow
    perms["deny"] = deny
    data["permissions"] = perms
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"settings_path": str(target), "allow_count": len(allow), "deny_count": len(deny)}


def read_permissions(path: str | Path) -> dict:
    """Current allow/deny lists, for post-start verification and evidence."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"allow": [], "deny": []}
    perms = data.get("permissions") or {}
    return {"allow": list(perms.get("allow") or []), "deny": list(perms.get("deny") or [])}


# --------------------------------------------------------------------------- #
# denial evidence
# --------------------------------------------------------------------------- #
#: Literal phrases this build uses when a tool is refused.
#: Matched case-insensitively; kept narrow so a model that merely *talks* about
#: permissions does not trip it.
DENIAL_MARKERS: tuple[str, ...] = (
    "permission prompts aren't available",
    "permission prompts are not available",
    "approval prompts are unavailable",
    "tool access was denied",
    "was denied (permission",
    "permission denied for",
    "denied by a permission rule",
    "blocked by permission settings",
    "denied by permission",
    "permission to run",
)

#: ``"<tool> ... was denied"`` / ``"access denied"`` without the fixed phrasing
#: above.  Both real incidents reported the refusal in the model's own words, so
#: a literal list alone missed them; these two shapes cover that without
#: matching a sentence like "the permissions of the file".
DENIAL_REGEXES: tuple[tuple[str, str], ...] = (
    (r"\b(?:bash|powershell|tool|tool call|command|access)\b[^.\n]{0,80}?"
     r"\b(?:was|were|is|are|has been|have been)\s+denied\b", "denied"),
    (r"\bdenied\b[^.\n]{0,40}?\b(?:permission|approval|prompt)\b", "denied"),
)


def detect_permission_denial(text: str | None) -> dict | None:
    """Return ``{marker, excerpt}`` when ``text`` contains a permission refusal.

    Used by the adapter to turn an ambiguous ``state=done`` into an explicit
    ``PERMISSION_DENIED`` warning: a job that was blocked at tool level produced
    no artifacts and must never be reported as a plain success.
    """
    if not text:
        return None
    import re as _re

    lowered = text.lower()
    for marker in DENIAL_MARKERS:
        idx = lowered.find(marker)
        if idx >= 0:
            return _excerpt(text, idx, marker)
    for pattern, label in DENIAL_REGEXES:
        match = _re.search(pattern, lowered)
        if match:
            return _excerpt(text, match.start(), label)
    return None


def _excerpt(text: str, idx: int, marker: str) -> dict:
    start = max(0, idx - 120)
    return {"marker": marker, "excerpt": text[start:idx + 200].strip()}


# --------------------------------------------------------------------------- #
# a second silent blocker: the job never had a login
# --------------------------------------------------------------------------- #
#: ``serve_ctl.py --home`` rewrites USERPROFILE/HOME, and the CLI stores its
#: login credential under HOME.  A job started against such a server ends with
#: this sentence while still reporting ``state=done`` and an empty transcript —
#: the same "succeeded but did nothing" shape as a permission refusal.
AUTH_MARKERS: tuple[str, ...] = (
    "authentication required",
    "please use /login",
    "please run /login",
    "not logged in",
    "invalid api key",
    "unauthorized: please log in",
)


def detect_auth_block(text: str | None) -> dict | None:
    """Return ``{marker, excerpt}`` when the job ended because it was not signed in."""
    if not text:
        return None
    lowered = text.lower()
    for marker in AUTH_MARKERS:
        idx = lowered.find(marker)
        if idx >= 0:
            return _excerpt(text, idx, marker)
    return None
