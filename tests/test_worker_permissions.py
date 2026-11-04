"""Worker permission profile + permission-block evidence (deterministic layer).

The real CodeBuddy run is ``scripts/t68_worker_permissions.py``; these tests pin
the parts that must never regress silently:

* the profile is an allow-list, not ``bypassPermissions``, and it keeps the
  destructive commands denied;
* merging preserves the server's ``gateway`` block (a rewrite would break auth);
* a refused tool call is turned into an explicit ``PERMISSION_DENIED`` warning
  and a non-success state, instead of an ambiguous ``succeeded`` with no file;
* the Ledger keeps that warning and the project projection flags it, so the
  operator sees the refusal rather than a "missing artifact" mystery;
* the effective prompt tells the agent *not* to build its own worktree.

Why the assertions look like this: the failure being fixed (goal-b6ca68a03f0d /
cp-934341b3f942 / job 89303e1f) was reported as ``state=done`` with zero
delivered files. Every check below is written so that same shape fails here.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import (  # noqa: E402
    ArtifactAssertion, DispatchRequest, TaskState, Verdict)
from adapters.codebuddy import permissions  # noqa: E402
from adapters.codebuddy.adapter import CodeBuddyAdapter  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.policies import AGENT_WORKTREE  # noqa: E402
from controlplane.project_service import ProjectService, _task_attention_flags  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402


# --------------------------------------------------------------------------- #
# profile shape
# --------------------------------------------------------------------------- #
def test_profile_is_allow_list_not_bypass():
    data = permissions.profile()
    assert data["allow"], "an empty allow list would deny everything"
    assert data["deny"], "high-risk commands must stay explicitly denied"
    joined = " ".join(data["allow"] + data["deny"])
    assert "bypassPermissions" not in joined
    # The blanket tool grants are exactly what this P0 forbids.
    assert "Bash" not in data["allow"]
    assert "PowerShell" not in data["allow"]


def test_profile_covers_the_commands_a_task_needs():
    allow = set(permissions.ALLOW_RULES)
    for rule in ("Bash(git rev-parse:*)", "Bash(git status:*)", "Bash(git commit:*)",
                 "Bash(pytest:*)", "Bash(python:*)"):
        assert rule in allow, f"unattended task cannot run {rule}"
    # PowerShell is a distinct tool name in this build; assuming Bash's rule
    # spelling carries over is exactly the mistake this list guards against.
    assert "PowerShell(git rev-parse:*)" in allow
    assert "PowerShell(git commit:*)" in allow


def test_deny_list_blocks_high_risk_commands():
    deny = set(permissions.DENY_RULES)
    for rule in ("Bash(git push:*)", "Bash(rm:*)", "Bash(git reset --hard:*)",
                 "PowerShell(Remove-Item:*)", "PowerShell(git push:*)",
                 "Bash(curl:*)", "Bash(pip install:*)"):
        assert rule in deny, f"high-risk command not denied: {rule}"


def test_every_power_shell_rule_has_a_bash_twin():
    """The shell tools are separate; a rule in one namespace proves nothing about the other."""
    allow_bash = {r for r in permissions.ALLOW_RULES if r.startswith("Bash(")}
    allow_ps = {r for r in permissions.ALLOW_RULES if r.startswith("PowerShell(")}
    assert {r.replace("Bash(", "PowerShell(") for r in allow_bash} == allow_ps
    deny_bash = {r for r in permissions.DENY_RULES if r.startswith("Bash(")}
    deny_ps = {r for r in permissions.DENY_RULES if r.startswith("PowerShell(")}
    # PowerShell has extra entries (Invoke-WebRequest ...); Bash must be a subset
    # of the mapped set so nothing is denied on one shell only by accident.
    assert {r.replace("Bash(", "PowerShell(") for r in deny_bash} <= deny_ps


def test_a_command_cannot_be_both_allowed_and_denied():
    assert not (set(permissions.ALLOW_RULES) & set(permissions.DENY_RULES))


# --------------------------------------------------------------------------- #
# settings merge
# --------------------------------------------------------------------------- #
def test_merge_preserves_gateway_credentials(tmp_path: Path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "gateway": {"auth": "password", "password": "s3cret-value"},
        "enabledPlugins": {"pdf@official": True},
    }), encoding="utf-8")

    summary = permissions.merge_into_settings(settings)
    data = json.loads(settings.read_text(encoding="utf-8"))

    # Rewriting this file wholesale would break server login (T34: --home broke
    # auth exactly like this), so the credential block must survive untouched.
    assert data["gateway"]["password"] == "s3cret-value"
    assert data["enabledPlugins"] == {"pdf@official": True}
    assert set(permissions.ALLOW_RULES) <= set(data["permissions"]["allow"])
    assert set(permissions.DENY_RULES) <= set(data["permissions"]["deny"])
    assert summary["allow_count"] == len(data["permissions"]["allow"])
    assert "s3cret-value" not in json.dumps(summary)


def test_merge_never_widens_deny_and_drops_conflicting_allow(tmp_path: Path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "permissions": {"allow": ["Bash(rm:*)", "Read"], "deny": ["Bash(git push:*)"]},
    }), encoding="utf-8")

    permissions.merge_into_settings(settings)
    data = json.loads(settings.read_text(encoding="utf-8"))
    allow, deny = data["permissions"]["allow"], data["permissions"]["deny"]

    # A pre-existing `Bash(rm:*)` allow must not survive next to our deny rule.
    assert "Bash(rm:*)" not in allow
    assert "Bash(rm:*)" in deny
    assert len(allow) == len(set(allow)) and len(deny) == len(set(deny))


def test_read_permissions_tolerates_a_missing_or_broken_file(tmp_path: Path):
    missing = tmp_path / "nope.json"
    assert permissions.read_permissions(missing) == {"allow": [], "deny": []}
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert permissions.read_permissions(broken) == {"allow": [], "deny": []}


# --------------------------------------------------------------------------- #
# denial detection
# --------------------------------------------------------------------------- #
REAL_DENIAL_TEXTS = [
    # Captured verbatim from job 89303e1f (the incident) and from the probes.
    "The Bash tool was denied (permission prompts unavailable in non-interactive "
    "mode), so I couldn't run `git rev-parse HEAD`.",
    "Bash tool access was denied (permission prompts aren't available in this "
    "non-interactive mode), so I couldn't run either command.",
    "Tool access was denied in this non-interactive session.",
    "Permission to run PowerShell was denied, so the command did not execute.",
    "The push was denied by a permission rule — the command did not run.",
]


@pytest.mark.parametrize("text", REAL_DENIAL_TEXTS)
def test_real_denial_texts_are_detected(text: str):
    denial = permissions.detect_permission_denial(text)
    assert denial is not None, text
    assert denial["marker"] in text.lower()
    assert denial["excerpt"]


@pytest.mark.parametrize("text", [
    "",
    None,
    "Everything ran fine; README.md updated and committed.",
    "I checked the permissions of the file with `ls -l`.",
    "The task is done.",
])
def test_non_denial_texts_are_not_flagged(text):
    assert permissions.detect_permission_denial(text) is None


@pytest.mark.parametrize("text", [
    "Authentication required. Please use /login command to sign in to your account",
    "Error: invalid api key",
    "Unauthorized: please log in first",
])
def test_auth_blockers_are_detected(text: str):
    assert permissions.detect_auth_block(text) is not None


@pytest.mark.parametrize("text", ["", None, "Signed in as someone; task done."])
def test_non_auth_texts_are_not_flagged(text):
    assert permissions.detect_auth_block(text) is None


# --------------------------------------------------------------------------- #
# adapter: a refused job is not a success
# --------------------------------------------------------------------------- #
INCIDENT_JOB = {
    "id": "89303e1f",
    "state": "done",
    "status": "idle",
    "tempo": "idle",
    "alive": True,
    "settled": True,
    "cwd": "C:/workspace",
    "detail": "result: 执行被阻断，无法继续。",
    "output": {"result": (
        "执行被阻断，无法继续。本次会话中 Bash 与 PowerShell 工具的调用被系统以"
        "\"permission prompts are not available in non-interactive mode\"拒绝。")},
    "startedAt": 1793762360000,
    "firstTerminalAt": 1793762434962,
}


def test_refused_job_is_not_reported_as_succeeded():
    adapter = CodeBuddyAdapter(autologin=False)
    snap = adapter._to_snapshot(INCIDENT_JOB, task_id="cp-934341b3f942")

    # The vendor says done; the work demonstrably did not happen.
    assert snap.state is TaskState.FAILED
    assert snap.state is not TaskState.SUCCEEDED
    assert any(w.startswith("PERMISSION_DENIED:") for w in snap.warnings)
    # The vendor's own words still travel, so the operator can read the cause.
    assert snap.metadata["codebuddy"]["state"] == "done"


def test_a_clean_job_keeps_its_success_state():
    adapter = CodeBuddyAdapter(autologin=False)
    job = dict(INCIDENT_JOB, output={"result": "60d1c0f"}, detail="result: 60d1c0f")
    snap = adapter._to_snapshot(job, task_id="cp-clean")
    assert snap.state is TaskState.SUCCEEDED
    assert not any(w.startswith("PERMISSION_DENIED:") for w in snap.warnings)
    assert not any(w.startswith("WORKER_NOT_AUTHENTICATED:") for w in snap.warnings)


def test_an_unauthenticated_job_is_not_reported_as_succeeded():
    """`serve_ctl --home` rewrites HOME, which orphans the CLI login (T34/M4-01).

    The job then ends ``state=done`` with a one-line output and an empty
    transcript.  That is the same false-success shape as a permission refusal and
    must be reported just as explicitly.
    """
    adapter = CodeBuddyAdapter(autologin=False)
    job = dict(INCIDENT_JOB,
               output={"result": "Authentication required. Please use /login "
                                  "command to sign in to your account"},
               detail="result: Authentication required. Please use /login command "
                      "to sign in to your account")
    snap = adapter._to_snapshot(job, task_id="cp-auth")
    assert snap.state is TaskState.FAILED
    assert any(w.startswith("WORKER_NOT_AUTHENTICATED:") for w in snap.warnings)
    assert not any(w.startswith("PERMISSION_DENIED:") for w in snap.warnings)


class _RefusedAdapter(CodeBuddyAdapter):
    """Reports a refused terminal job without touching HTTP (no server needed)."""

    name = "stub-refused"

    def __init__(self):  # noqa: D107 - deliberately skips Api/login
        self._jobs, self._meta, self._worktree_heads = {}, {}, {}

    def dispatch(self, request: DispatchRequest):
        from adapters.base import DispatchResult
        self._meta["j1"] = {"request": request, "raw": {}, "started_at": 0.0}
        return DispatchResult(task_id=request.task_id, worker_job_id="j1",
                              accepted=True)

    def get_task(self, task_id, timeout_s: float = 30.0):
        return self._to_snapshot(dict(INCIDENT_JOB, id="j1"), task_id=task_id)

    def collect_artifacts(self, task_id):
        return []


def test_ledger_records_permission_block_and_verifier_still_rejects(tmp_path: Path):
    """End to end through the plane: warning persisted, verdict rejected, flag raised."""
    store = TaskStore(str(tmp_path / "ledger.db"))
    plane = ControlPlane(_RefusedAdapter(), store=store, policy=AGENT_WORKTREE)
    task = plane.enqueue(
        "produce t67-artifacts/summary.md", str(tmp_path),
        assertions=[ArtifactAssertion("file_exists",
                                      "t67-artifacts/summary.md")],
        policy=AGENT_WORKTREE)
    task_id = task["task_id"]

    plane.dispatch_next("test-owner")
    plane.refresh_known_jobs()

    record = store.get(task_id)
    assert record["state"] == TaskState.FAILED.value
    warnings = [w for w in record["warnings"] if w.startswith("PERMISSION_DENIED:")]
    assert warnings, f"permission refusal not recorded: {record['warnings']}"
    # The verifier rule is untouched: no file, no verification.
    assert record["verdict"] == Verdict.REJECTED.value

    # The operator-facing reason is derivable from the ledger alone.
    flags = _task_attention_flags(record)
    assert "permission_blocked" in flags
    store.close()


def test_warning_is_recorded_once_per_observation(tmp_path: Path):
    store = TaskStore(str(tmp_path / "ledger.db"))
    plane = ControlPlane(_RefusedAdapter(), store=store, policy=AGENT_WORKTREE)
    task = plane.enqueue("x", str(tmp_path), policy=AGENT_WORKTREE,
                         assertions=[ArtifactAssertion("file_exists", "a.txt")])
    plane.dispatch_next("o")
    for _ in range(3):
        plane.refresh_known_jobs()
    warnings = [w for w in store.get(task["task_id"])["warnings"]
                if w.startswith("PERMISSION_DENIED:")]
    assert len(warnings) == 1, f"repeated observation duplicated the warning: {warnings}"
    store.close()


# --------------------------------------------------------------------------- #
# execution contract in the effective prompt
# --------------------------------------------------------------------------- #
def _git_repo(path: Path) -> Path:
    """A registered project must be a real Git root, so make one."""
    import subprocess

    subprocess.run(["git", "init", "-q", str(path)], check=True,
                   capture_output=True, text=True, timeout=60)
    for key, value in (("user.email", "verifier@example.invalid"),
                       ("user.name", "Verifier")):
        subprocess.run(["git", "-C", str(path), "config", key, value],
                       check=True, capture_output=True, text=True, timeout=60)
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True,
                   capture_output=True, text=True, timeout=60)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "seed"],
                   check=True, capture_output=True, text=True, timeout=60)
    return path


def test_effective_prompt_forbids_a_second_worktree(tmp_path: Path):
    _git_repo(tmp_path)
    store = TaskStore(str(tmp_path / "ledger.db"))
    plane = ControlPlane(_RefusedAdapter(), store=store, policy=AGENT_WORKTREE)
    project = plane.register_project("p", str(tmp_path))
    service = ProjectService(plane)

    built = service.build_task_context(project["id"], "do the thing")
    prompt = built.effective_prompt

    assert "Do NOT create an additional git worktree" in prompt
    assert "already the isolated checkout" in prompt
    # And it must tell the agent what to do with a refusal, not how to bypass it.
    assert "no approval prompt can be answered" in prompt
    assert "bypass" not in prompt.lower()
    store.close()


def test_prompt_is_still_deterministic_after_the_contract_was_added(tmp_path: Path):
    _git_repo(tmp_path)
    store = TaskStore(str(tmp_path / "ledger.db"))
    plane = ControlPlane(_RefusedAdapter(), store=store, policy=AGENT_WORKTREE)
    project = plane.register_project("p", str(tmp_path))
    service = ProjectService(plane)
    first = service.build_task_context(project["id"], "task")
    second = service.build_task_context(project["id"], "task")
    assert first.effective_prompt == second.effective_prompt
    assert first.context_version == second.context_version
    store.close()
