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

What this is NOT
----------------
This is an **approval policy**, not an operating-system sandbox.  It decides
which tool calls the agent may issue without a human answering a prompt; it does
not confine what an approved command can do.  Two consequences must not be
papered over:

* ``Bash(python:*)`` / ``PowerShell(python3:*)`` are in the allow list because an
  unattended coding task must be able to run its own tests.  Once granted, an
  arbitrary Python program runs with the Worker account's full rights: it can
  write outside the repository, read any file that account can read, and open
  network connections.  **No command allow-list can constrain arbitrary
  interpreted code**, and claiming otherwise would be false.
* ``Write``/``Edit``/``NotebookEdit`` are likewise broad: they may target any
  path the process can reach, not just the task's checkout.

The real boundary for a production Worker is therefore outside this process:
a dedicated low-privilege Windows account, directory ACLs limited to the
isolated checkout (plus a read-only toolchain), no interactive logon, no stored
credentials beyond the Worker's own model access, and an egress policy that
permits only the model endpoint.  See ``docs/adapter-contract.md`` §4.1.  Treat
this module as "what the agent may ask for unattended", never as "what damage it
can do".

Scope
-----
This is CodeBuddy vendor knowledge (file layout, rule syntax, which layer wins),
so it lives under ``adapters/codebuddy/`` and never leaks into ``adapters/base.py``
or ``controlplane/``.
"""
from __future__ import annotations

import json
import os
import tempfile
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

#: ``--worker-profile`` values that mean "do not write a profile".  Anything
#: else must name a profile this module actually ships: a typo used to start the
#: Worker with the default (intermittent) classifier while the operator believed
#: the least-privilege list was applied.
DISABLED_PROFILES: frozenset[str] = frozenset({"none"})


def known_profiles() -> tuple[str, ...]:
    """Every profile name ``--worker-profile`` accepts (besides the disabled one)."""
    return (PROFILE_NAME,)


def resolve_profile(name: str | None) -> str | None:
    """Normalize and validate a ``--worker-profile`` value.

    Returns ``None`` when profiling is disabled, the canonical name otherwise,
    and raises :class:`WorkerConfigError` for anything unknown.  Refusing an
    unknown name is the point: silently falling back to "no rules" would start a
    Worker whose permission behaviour is the intermittent default.
    """
    text = (name or "").strip()
    if not text:
        raise WorkerConfigError("worker profile name is empty")
    if text.lower() in DISABLED_PROFILES:
        return None
    if text not in known_profiles():
        raise WorkerConfigError(
            f"unknown worker profile {text!r}; known profiles: "
            f"{sorted(known_profiles())} (or 'none' to disable)")
    return text


def profile() -> dict:
    """The profile as plain data (safe to record in evidence)."""
    return {"name": PROFILE_NAME, "allow": list(ALLOW_RULES), "deny": list(DENY_RULES)}


def permission_block() -> dict:
    """The ``permissions`` object to merge into a settings file."""
    return {"allow": list(ALLOW_RULES), "deny": list(DENY_RULES)}


# --------------------------------------------------------------------------- #
# settings file handling
# --------------------------------------------------------------------------- #
class WorkerConfigError(RuntimeError):
    """The Worker's settings location cannot be used safely.

    Raised for every refusal in this module so the caller can stop the start
    sequence instead of silently writing a permission policy somewhere it was not
    asked to, or overwriting a file it failed to parse.
    """


def settings_path(config_dir: str | Path) -> Path:
    """``$CODEBUDDY_CONFIG_DIR/settings.json``."""
    return Path(config_dir) / "settings.json"


def operator_config_dirs() -> list[Path]:
    """Directories that belong to the human operator, never to a Worker.

    Two candidates, both resolved through ``os.path.realpath`` so a symlink or
    NTFS junction cannot make two different paths look distinct:

    * ``~/.codebuddy`` — the CLI's default home, where an interactive session
      keeps its settings and login;
    * ``$CODEBUDDY_CONFIG_DIR`` when this process has it set — whoever set it is
      treating that directory as their own configuration.
    """
    candidates = {Path.home() / ".codebuddy"}
    configured = (os.environ.get("CODEBUDDY_CONFIG_DIR") or "").strip()
    if configured:
        candidates.add(Path(configured))
    return [Path(os.path.realpath(path)) for path in candidates]


def validate_worker_config_dir(config_dir: str | Path) -> Path:
    """Return the resolved directory, or raise if it could be the operator's.

    An unattended Worker must never write ``permissions`` into a file an
    interactive session also reads: that would silently grant allow-listed
    commands to every human session on the machine, which is the opposite of a
    least-privilege profile.  Nesting in either direction is refused too —
    writing inside the operator tree lands in the live session's own files, and
    writing above it leaves a shadow settings file next to it.
    """
    if not str(config_dir).strip():
        raise WorkerConfigError("worker config dir is empty")
    raw = Path(config_dir)
    if not raw.is_absolute():
        raise WorkerConfigError(
            f"worker config dir must be an absolute path: {config_dir!r}")
    resolved = Path(os.path.realpath(raw))
    for operator in operator_config_dirs():
        if resolved == operator:
            raise WorkerConfigError(
                "worker config dir resolves to the operator's real CodeBuddy "
                f"settings directory ({operator}); refusing to write permissions "
                "there")
        for inner, outer, why in ((resolved, operator, "inside"),
                                  (operator, resolved, "a parent of")):
            try:
                inner.relative_to(outer)
            except ValueError:
                continue
            raise WorkerConfigError(
                f"worker config dir {resolved} is {why} the operator's CodeBuddy "
                f"directory {operator}; refusing")
    return resolved


def merge_into_settings(path: str | Path, *, extra_allow: Iterable[str] = (),
                        extra_deny: Iterable[str] = ()) -> dict:
    """Write the profile into ``path``, failing closed on anything unreadable.

    Rules of the road:

    * A file that exists but cannot be parsed is **never** rewritten.  Replacing
      it would destroy the gateway credential block and any operator settings,
      and it would silently swap "I could not read the policy" for "policy is
      now exactly what I wanted".
    * ``allow`` is **replaced** by the reviewed profile; it is not unioned with
      whatever was already in the file.  Inheriting a pre-existing ``Bash`` or
      ``Bash(*)`` would re-open exactly the blanket grant this profile exists to
      avoid.
    * ``deny`` is unioned, so a caller can only ever add restrictions.
    * The write is atomic (temp file in the same directory + ``os.replace``), so
      a crash cannot leave a truncated settings file that the next start then
      fails to parse.
    """
    target = Path(path)
    data: dict = {}
    if target.exists():
        try:
            loaded = json.loads(target.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise WorkerConfigError(
                f"cannot read {target}: {type(exc).__name__}: {exc}; refusing to "
                "rewrite it (that would drop the gateway credentials and replace "
                "an unknown policy)") from exc
        if not isinstance(loaded, dict):
            raise WorkerConfigError(
                f"{target} does not contain a JSON object; refusing to rewrite it")
        data = loaded

    # A pre-existing `allow` is not trusted: only rules re-derived below survive.
    existing_deny = [str(rule) for rule in (data.get("permissions") or {}).get("deny") or []]
    deny = list(dict.fromkeys([*existing_deny, *DENY_RULES,
                               *[str(r) for r in extra_deny]]))
    allow = list(dict.fromkeys([*ALLOW_RULES, *[str(r) for r in extra_allow]]))
    dropped_allow = sorted({str(rule) for rule in
                            (data.get("permissions") or {}).get("allow") or []}
                           - set(allow))
    # A rule in both lists would be resolved by the vendor's precedence (deny
    # wins) but is still ambiguous to a reader: keep it denied, drop the allow.
    allow = [rule for rule in allow if rule not in set(deny)]
    perms = dict(data.get("permissions") or {})
    perms["allow"] = allow
    perms["deny"] = deny
    data["permissions"] = perms

    payload = json.dumps(data, indent=2, ensure_ascii=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(prefix=".settings-", suffix=".tmp",
                                        dir=str(target.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return {"settings_path": str(target), "allow_count": len(allow),
            "deny_count": len(deny), "dropped_allow_rules": dropped_allow}


def read_permissions(path: str | Path) -> dict:
    """Current allow/deny lists, for post-start verification and evidence."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {"allow": [], "deny": []}
    perms = (data.get("permissions") if isinstance(data, dict) else None) or {}
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
