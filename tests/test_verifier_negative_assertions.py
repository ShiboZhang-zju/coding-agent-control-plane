"""The acceptance rule must not accept an attempt for doing nothing.

Found by the T66 real end-to-end run: the real LD selected only ``path_absent``
assertions for a read-only objective, the real Worker correctly made no change,
every assertion "passed", and ``verdict_for`` returned ``verified``.  A goal that
asked for analysis was then reported as completed by the whole stack, dashboard
included.

These tests pin the rule at the only place that decides it, so no adapter, no
model choice and no future assertion kind can reintroduce it silently.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from adapters.base import ArtifactAssertion, TaskState, Verdict  # noqa: E402
from controlplane.verify import (  # noqa: E402
    capture_baseline, check_all, side_effects, verdict_for)


def _verify(cwd: Path, assertions: list[ArtifactAssertion],
            state: TaskState = TaskState.SUCCEEDED) -> tuple[Verdict, list[dict]]:
    baseline = capture_baseline(str(cwd), assertions)
    results = check_all(str(cwd), assertions, baseline)
    violations = side_effects(str(cwd), baseline)
    return verdict_for(state, True, results, violations), results


def test_negative_only_assertions_cannot_verify_a_no_op(tmp_path: Path):
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    verdict, results = _verify(
        tmp_path, [ArtifactAssertion("path_absent", "report.md")])
    assert all(result["passed"] for result in results)
    assert [result["owned"] for result in results] == [False]
    # Nothing was produced, so nothing was accepted.
    assert verdict is Verdict.INCONCLUSIVE


def test_git_clean_alone_cannot_verify_a_no_op(tmp_path: Path):
    import subprocess

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True,
                       capture_output=True, text=True, timeout=60)

    git("init", "-q")
    git("config", "user.email", "verifier@example.invalid")
    git("config", "user.name", "Verifier")
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "seed")

    verdict, results = _verify(
        tmp_path, [ArtifactAssertion("git_clean", "HEAD")])
    assert all(result["passed"] for result in results)
    assert verdict is Verdict.INCONCLUSIVE


def test_passed_but_unowned_results_are_inconclusive_not_verified(tmp_path: Path):
    """The exact case the T66 fix exists for, asserted on a *non-empty* result set.

    The earlier real-run regression (T66 S3) produced ``assertion_results: []``,
    which the pre-existing "no assertions" branch already turned into
    ``inconclusive``.  So that run never exercised the new rule.  This test feeds
    the rule the shape it actually guards: results that are present, all
    ``passed``, and none ``owned``.
    """
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    results = [{"kind": "path_absent", "target": "report.md", "value": None,
                "passed": True, "owned": False, "observed": "missing (expected)"}]
    assert results, "the guard must be proven on a non-empty result set"
    assert all(result["passed"] for result in results)
    assert not any(result["owned"] for result in results)
    assert verdict_for(TaskState.SUCCEEDED, True, results, []) is Verdict.INCONCLUSIVE


def test_git_branch_alone_cannot_verify_a_no_op(tmp_path: Path):
    """``git_branch`` is a state assertion, not production evidence.

    ``HEAD`` sits on the requested branch whether or not the attempt did
    anything, so a branch-only objective must not be accepted either.  This pins
    the documented behaviour: ``git_branch`` never sets ``owned``.
    """
    import subprocess

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True,
                       capture_output=True, text=True, timeout=60)

    git("init", "-q")
    git("config", "user.email", "verifier@example.invalid")
    git("config", "user.name", "Verifier")
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "seed")
    branch = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "--abbrev-ref", "HEAD"],
        check=True, capture_output=True, text=True, timeout=60).stdout.strip()

    verdict, results = _verify(
        tmp_path, [ArtifactAssertion("git_branch", branch)])
    assert [result["passed"] for result in results] == [True]
    assert [result["owned"] for result in results] == [None]
    assert verdict is Verdict.INCONCLUSIVE


def test_state_assertions_cannot_upgrade_an_unowned_set(tmp_path: Path):
    """Mixing "still clean" into a negative-only set does not manufacture evidence."""
    import subprocess

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True,
                       capture_output=True, text=True, timeout=60)

    git("init", "-q")
    git("config", "user.email", "verifier@example.invalid")
    git("config", "user.name", "Verifier")
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "seed")

    verdict, results = _verify(tmp_path, [
        ArtifactAssertion("path_absent", "report.md"),
        ArtifactAssertion("git_clean", "HEAD"),
    ])
    assert all(result["passed"] for result in results)
    assert not any(result["owned"] for result in results)
    assert verdict is Verdict.INCONCLUSIVE


def test_positive_assertion_still_verifies(tmp_path: Path):
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    assertions = [ArtifactAssertion("file_exists", "made.txt")]
    baseline = capture_baseline(str(tmp_path), assertions)
    (tmp_path / "made.txt").write_text("done\n", encoding="utf-8")
    results = check_all(str(tmp_path), assertions, baseline)
    assert verdict_for(TaskState.SUCCEEDED, True, results, []) is Verdict.VERIFIED


def test_mixed_negative_and_positive_verifies(tmp_path: Path):
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    assertions = [ArtifactAssertion("file_exists", "made.txt"),
                  ArtifactAssertion("path_absent", "unwanted.md")]
    baseline = capture_baseline(str(tmp_path), assertions)
    (tmp_path / "made.txt").write_text("done\n", encoding="utf-8")
    results = check_all(str(tmp_path), assertions, baseline)
    assert verdict_for(TaskState.SUCCEEDED, True, results, []) is Verdict.VERIFIED


def test_failed_negative_assertion_is_rejected(tmp_path: Path):
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    (tmp_path / "unwanted.md").write_text("oops\n", encoding="utf-8")
    verdict, results = _verify(
        tmp_path, [ArtifactAssertion("path_absent", "unwanted.md")])
    assert [result["passed"] for result in results] == [False]
    assert verdict is Verdict.REJECTED


def test_no_assertions_is_still_inconclusive(tmp_path: Path):
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    verdict, _ = _verify(tmp_path, [])
    assert verdict is Verdict.INCONCLUSIVE


def test_control_plane_does_not_record_verified_for_a_negative_only_task(tmp_path: Path):
    """End-to-end through ``ControlPlane``, not a hand-built result list.

    This is the integration shape the reviewer asked for: a real WorkItem whose
    declared assertions are all negative, observed as SUCCEEDED by the adapter,
    reconciled by the real plane.  The persisted verdict must be
    ``inconclusive`` — never ``verified``.
    """
    import subprocess

    from adapters.base import TaskSnapshot
    from controlplane.plane import ControlPlane
    from controlplane.store import TaskStore

    class _StubAdapter:
        name = "stub"

        def collect_artifacts(self, task_id):
            return []

        def reconcile(self):
            return {"source": "stub", "total": 0, "new": [], "changed": [],
                    "lost": [], "jobs": []}

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True,
                       capture_output=True, text=True, timeout=60)

    git("init", "-q")
    git("config", "user.email", "verifier@example.invalid")
    git("config", "user.name", "Verifier")
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "seed")

    store = TaskStore(tmp_path / "ledger.db")
    try:
        plane = ControlPlane(_StubAdapter(), store)
        assertions = [ArtifactAssertion("path_absent", "report.md")]
        task = plane.enqueue(
            "analyze only, do not write anything", str(tmp_path),
            assertions=assertions)
        task_id = task["task_id"]
        # The attempt did nothing; the adapter reports success.
        store.update(task_id, **plane._snapshot_patch(
            task_id,
            TaskSnapshot(task_id=task_id, worker_job_id="stub-" + task_id,
                         state=TaskState.SUCCEEDED, cwd=str(tmp_path),
                         detail="read-only, nothing produced")))
        record = store.get(task_id)
        assert [r["passed"] for r in record["assertion_results"]] == [True]
        assert [bool(r["owned"]) for r in record["assertion_results"]] == [False]
        assert record["verdict"] == Verdict.INCONCLUSIVE.value
    finally:
        store.close()


@pytest.mark.parametrize("state", [TaskState.FAILED, TaskState.CANCELLED,
                                   TaskState.LOST])
def test_bad_states_stay_rejected_even_with_positive_assertions(
        tmp_path: Path, state: TaskState):
    (tmp_path / "made.txt").write_text("done\n", encoding="utf-8")
    assertions = [ArtifactAssertion("file_exists", "made.txt")]
    baseline = capture_baseline(str(tmp_path), assertions)
    (tmp_path / "made.txt").write_text("done\n", encoding="utf-8")
    results = check_all(str(tmp_path), assertions, baseline)
    assert verdict_for(state, True, results, []) is Verdict.REJECTED
