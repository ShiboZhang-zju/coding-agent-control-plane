"""Final pre-deployment fixes: serve_ctl ordering, prompt/policy agreement, LD flag.

Three confirmed problems, one file each, no new design:

1. ``serve_ctl start`` validated the Worker config directory *after* creating it,
   so an illegal (operator-owned) path still left an empty directory behind, and
   a misspelled ``--worker-profile`` started a Worker with the default
   (intermittent) classifier while the operator believed the profile was applied.
2. The execution contract in the Effective Prompt always claimed "your working
   directory is already the isolated checkout", even for ``isolation="none"``
   tasks — including tasks whose policy came from the *project*, not the caller.
3. ``permission_blocked`` was a Ledger/UI flag but not a LD human-attention
   flag, so the LD loop kept retrying a task that can only be refused again.

Every test here is written so the pre-fix behaviour fails it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from adapters.base import DispatchPolicy  # noqa: E402
from adapters.codebuddy import permissions  # noqa: E402
from controlplane import context_builder  # noqa: E402


# --------------------------------------------------------------------------- #
# 1. serve_ctl: validate first, create second, refuse with a structured result
# --------------------------------------------------------------------------- #
@pytest.fixture()
def serve_ctl():
    import serve_ctl as module  # noqa: WPS433 - deliberately imported per test

    return module


@pytest.fixture()
def operator_home(tmp_path: Path, monkeypatch):
    """A fake operator home whose ``.codebuddy`` must never be written to."""
    home = tmp_path / "home"
    (home / ".codebuddy").mkdir(parents=True)
    monkeypatch.setattr(permissions.Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("CODEBUDDY_CONFIG_DIR", raising=False)
    return home


def test_illegal_config_dir_is_refused_without_creating_anything(
        serve_ctl, operator_home: Path, tmp_path: Path, capsys, monkeypatch):
    """The pre-fix code ran mkdir() first, leaving a directory behind on refusal."""
    illegal = operator_home / ".codebuddy" / "worker"
    assert not illegal.exists()

    code = serve_ctl.cmd_start(port=0, password="p", config_dir=str(illegal),
                               worker_profile=permissions.PROFILE_NAME)

    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code != 0, "an illegal config dir must not exit 0"
    assert out["result"] == "refused"
    assert "reason" in out and out["reason"]
    # The decisive assertion: nothing was created on the way to the refusal.
    assert not illegal.exists()


def test_the_operator_settings_directory_itself_is_refused(
        serve_ctl, operator_home: Path, capsys):
    operator = operator_home / ".codebuddy"
    code = serve_ctl.cmd_start(port=0, password="p", config_dir=str(operator),
                               worker_profile=permissions.PROFILE_NAME)
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code != 0 and out["result"] == "refused"
    # Not even the settings file may be created/rewritten.
    assert not (operator / "settings.json").exists()


def test_a_home_derived_config_dir_is_also_validated(
        serve_ctl, operator_home: Path, tmp_path: Path, capsys):
    """``--home`` derives ``$HOME/.codebuddy``; that path must be checked too.

    Pointing --home at the operator home makes the derived directory *be* the
    operator directory, which is exactly what must not be profiled.
    """
    code = serve_ctl.cmd_start(port=0, password="p", home=str(operator_home),
                               worker_profile=permissions.PROFILE_NAME)
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code != 0 and out["result"] == "refused"
    assert not (operator_home / ".codebuddy" / "settings.json").exists()


def test_unknown_worker_profile_is_refused_before_writing(
        serve_ctl, operator_home: Path, tmp_path: Path, capsys):
    """A typo used to start the Worker with the default classifier, silently."""
    isolated = tmp_path / "state" / "codebuddy-config"

    code = serve_ctl.cmd_start(port=0, password="p", config_dir=str(isolated),
                               worker_profile="acp-worker-minmal")

    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code != 0, "an unknown profile name must not start a Worker"
    assert out["result"] == "refused"
    assert "unknown worker profile" in out["reason"]
    # Refused before anything happened: no settings file, no directory.
    assert not (isolated / "settings.json").exists()
    assert not isolated.exists()


def test_resolve_profile_accepts_only_the_shipped_names():
    assert permissions.resolve_profile(permissions.PROFILE_NAME) == permissions.PROFILE_NAME
    assert permissions.resolve_profile("none") is None
    with pytest.raises(permissions.WorkerConfigError):
        permissions.resolve_profile("acp-worker-minimal-ish")
    with pytest.raises(permissions.WorkerConfigError):
        permissions.resolve_profile("")


def test_plan_config_dir_does_not_create_the_directory(
        serve_ctl, tmp_path: Path, operator_home: Path):
    """Planning is pure: it returns a path, it never performs the mkdir."""
    target = tmp_path / "state" / "cfg"
    planned = serve_ctl._plan_config_dir(None, str(target), None)
    assert planned == target.resolve()
    assert not target.exists()


def test_profile_without_an_isolated_dir_is_still_refused(serve_ctl, capsys):
    code = serve_ctl.cmd_start(port=0, password="p",
                               worker_profile=permissions.PROFILE_NAME)
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code != 0 and out["result"] == "refused"


# --------------------------------------------------------------------------- #
# 2. the contract must match the policy that will really be dispatched
# --------------------------------------------------------------------------- #
ISOLATED_CLAIM = "already the isolated checkout"
UNISOLATED_CLAIM = "Do NOT assume isolation"


def test_contract_claims_isolation_only_for_worktree():
    isolated = "\n".join(context_builder.workspace_contract("worktree"))
    assert ISOLATED_CLAIM in isolated
    assert UNISOLATED_CLAIM not in isolated


@pytest.mark.parametrize("isolation", ["none", "", None, "unexpected"])
def test_contract_does_not_claim_isolation_for_anything_else(isolation):
    text = "\n".join(context_builder.workspace_contract(isolation))
    assert ISOLATED_CLAIM not in text, (
        "claiming a disposable worktree while running in the real checkout is a "
        f"false statement for isolation={isolation!r}")
    assert UNISOLATED_CLAIM in text


def test_contract_keeps_the_unchanged_paragraphs():
    for isolation in ("worktree", "none"):
        text = "\n".join(context_builder.workspace_contract(isolation))
        # The parts that are true regardless of isolation must not be lost.
        assert "no approval prompt can be answered" in text
        assert "Run one simple command per shell call" in text
        assert "Never push" in text


def _git_repo(path: Path) -> Path:
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


class _IdleAdapter:
    """Never dispatches; these tests only build prompts."""

    name = "stub-idle"

    def describe(self):
        return {"name": self.name}

    def dispatch(self, request):  # pragma: no cover - never reached
        raise AssertionError("no dispatch expected")

    def get_task(self, task_id, timeout_s: float = 30.0):  # pragma: no cover
        raise AssertionError("no dispatch expected")

    def cancel(self, task_id):  # pragma: no cover
        raise AssertionError("no dispatch expected")

    def collect_artifacts(self, task_id):
        return []


def _service_with_policy(tmp_path: Path, policy: DispatchPolicy | None):
    from controlplane.plane import ControlPlane
    from controlplane.project_service import ProjectService
    from controlplane.store import TaskStore

    _git_repo(tmp_path)
    store = TaskStore(str(tmp_path / "ledger.db"))
    plane = ControlPlane(_IdleAdapter(), store=store,
                         policy=DispatchPolicy(mode="agent", isolation="worktree"))
    project = plane.register_project("p", str(tmp_path), policy=policy)
    return ProjectService(plane), project, store


def test_explicit_none_policy_gets_the_unisolated_contract(tmp_path: Path):
    service, project, store = _service_with_policy(tmp_path, None)
    built = service.build_task_context(
        project["id"], "do it", isolation="none")
    assert ISOLATED_CLAIM not in built.effective_prompt
    assert UNISOLATED_CLAIM in built.effective_prompt
    store.close()


def test_preview_defaults_to_the_effective_isolation_of_the_project(tmp_path: Path):
    """A preview must show what a submission would freeze, not "worktree" by default."""
    service, project, store = _service_with_policy(tmp_path, None)
    worktree_preview = service.build_task_context(project["id"], "task")
    assert ISOLATED_CLAIM in worktree_preview.effective_prompt
    store.close()

    none_service, none_project, none_store = _service_with_policy(
        tmp_path / "none-project",
        DispatchPolicy(mode="agent", isolation="none", permission_mode="acceptEdits"))
    none_preview = none_service.build_task_context(none_project["id"], "task")
    assert ISOLATED_CLAIM not in none_preview.effective_prompt
    assert UNISOLATED_CLAIM in none_preview.effective_prompt
    none_store.close()


def test_project_default_policy_overriding_the_caller_is_reflected(tmp_path: Path):
    """``enqueue`` prefers the project policy; the prompt must say the same thing.

    This is the case a caller-side fix would miss: the caller passes no policy,
    the project supplies ``isolation=none``, and the frozen prompt has to admit
    that the task runs in the real checkout.
    """
    service, project, store = _service_with_policy(
        tmp_path,
        DispatchPolicy(mode="agent", isolation="none", permission_mode="acceptEdits"))
    built = service.build_task_context(project["id"], "task")
    assert ISOLATED_CLAIM not in built.effective_prompt
    assert UNISOLATED_CLAIM in built.effective_prompt
    store.close()


def test_effective_prompt_stays_frozen_and_deterministic(tmp_path: Path):
    """Same policy + same context version => byte-identical prompt, twice."""
    service, project, store = _service_with_policy(
        tmp_path,
        DispatchPolicy(mode="agent", isolation="none", permission_mode="acceptEdits"))
    first = service.build_task_context(project["id"], "task")
    second = service.build_task_context(project["id"], "task")
    assert first.effective_prompt == second.effective_prompt
    assert first.context_version == second.context_version
    store.close()


def test_submitted_task_freezes_the_matching_contract(tmp_path: Path):
    """The persisted prompt is the one built from the effective policy."""
    from controlplane.plane import ControlPlane
    from controlplane.project_service import ProjectService
    from controlplane.store import TaskStore

    _git_repo(tmp_path)
    store = TaskStore(str(tmp_path / "ledger.db"))
    plane = ControlPlane(_IdleAdapter(), store=store,
                         policy=DispatchPolicy(mode="agent", isolation="worktree"))
    project = plane.register_project(
        "p", str(tmp_path),
        policy=DispatchPolicy(mode="agent", isolation="none",
                              permission_mode="acceptEdits"))
    service = ProjectService(plane)

    record = service.submit_project_task(project["id"], "task")
    stored = store.get(record["task_id"])
    assert ISOLATED_CLAIM not in stored["effective_prompt"]
    assert stored["effective_prompt"] == stored["prompt"]
    assert stored["task_digest"]
    store.close()


# --------------------------------------------------------------------------- #
# 3. permission_blocked must reach the LD human-attention gate
# --------------------------------------------------------------------------- #
def test_permission_blocked_is_a_ld_human_attention_flag():
    """Without this, LD keeps resubmitting a task that will be refused again."""
    from ld.coordinator import _HUMAN_ATTENTION_FLAGS

    assert "permission_blocked" in _HUMAN_ATTENTION_FLAGS
    # The other two P0 flags must stay reachable as well.
    assert "worker_not_authenticated" in _HUMAN_ATTENTION_FLAGS
    assert "session_binding_conflict" in _HUMAN_ATTENTION_FLAGS


def test_permission_blocked_stops_ld_and_keeps_the_verdict_rejected(tmp_path: Path):
    """End to end: the flag reaches a blocker, and acceptance is unchanged."""
    from adapters.base import ArtifactAssertion, TaskSnapshot, TaskState, Verdict
    from controlplane.plane import ControlPlane
    from controlplane.project_service import ProjectService, _task_attention_flags
    from controlplane.store import TaskStore

    class _RefusingAdapter(_IdleAdapter):
        name = "stub-refusing"

    repo = _git_repo(tmp_path / "repo")
    store = TaskStore(str(tmp_path / "ledger.db"))
    plane = ControlPlane(_RefusingAdapter(), store=store,
                         policy=DispatchPolicy(mode="agent", isolation="worktree"))
    project = plane.register_project("p", str(repo))
    service = ProjectService(plane)
    record = service.submit_project_task(
        project["id"], "produce a.txt",
        assertions=[ArtifactAssertion("file_exists", "a.txt")])
    # A real dispatch would call the Worker; the observation path is the same one
    # runtime recovery uses, and it is what records the refusal.
    plane._save_snapshot(record["task_id"], TaskSnapshot(
        task_id=record["task_id"], worker_job_id="j1", state=TaskState.FAILED,
        cwd=str(repo), detail="denied",
        warnings=["PERMISSION_DENIED: refused in an unattended session"]))

    task = store.get(record["task_id"])
    assert "permission_blocked" in _task_attention_flags(task)
    # Handing the task over must not make it acceptable.
    assert task["verdict"] == Verdict.REJECTED.value

    progress = service.get_project_progress(project["id"])
    blocked = progress.get("blocked_tasks") or []
    # A refused task settles as `failed`, so a state-only blocker filter dropped
    # it entirely and LD never saw why the task stopped.
    assert [t["task_id"] for t in blocked] == [record["task_id"]], blocked
    assert "permission_blocked" in blocked[0]["attention_flags"]

    from ld.coordinator import LDCoordinator

    attention = LDCoordinator._project_attention_blockers(progress)
    assert any(str(item).startswith("permission_blocked") for item in attention), attention
    store.close()
