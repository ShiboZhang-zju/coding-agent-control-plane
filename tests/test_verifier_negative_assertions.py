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
