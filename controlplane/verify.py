"""Artifact-based acceptance.

Rationale (evidence/jobs/T00b-state-semantics.json): the worker reports `done`
for a shell job that exited 3, and exposes no exit code. Therefore acceptance
can never be derived from the worker's terminal state; it must be derived from
observable artifacts declared *before* dispatch.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterable

from adapters.base import ArtifactAssertion, TaskState, Verdict


def _git(cwd: str, *args: str) -> tuple[int, str]:
    try:
        p = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=25)
        return p.returncode, (p.stdout or p.stderr).strip()
    except Exception as exc:  # noqa: BLE001
        return 1, f"{type(exc).__name__}: {exc}"


def check_one(cwd: str, a: ArtifactAssertion) -> dict:
    """Evaluate a single assertion. Returns {kind,target,passed,observed}."""
    target = Path(cwd) / a.target if not a.target.startswith(("commit:",)) else None
    res = {"kind": a.kind, "target": a.target, "value": a.value, "passed": False, "observed": ""}

    if a.kind == "file_exists":
        res["passed"] = bool(target and target.exists())
        res["observed"] = "exists" if res["passed"] else "missing"
    elif a.kind == "path_absent":
        res["passed"] = bool(target and not target.exists())
        res["observed"] = "missing (expected)" if res["passed"] else "present (unexpected)"
    elif a.kind == "file_contains":
        if target and target.is_file():
            text = target.read_text(encoding="utf-8", errors="replace")
            res["passed"] = (a.value or "") in text
            res["observed"] = text.strip()[:200]
        else:
            res["observed"] = "file missing"
    elif a.kind == "git_commit":
        rc, out = _git(cwd, "log", "--oneline", "-15")
        hit = rc == 0 and (a.value or "") in out
        res["passed"] = hit
        res["observed"] = out.splitlines()[0] if out else ""
    elif a.kind == "git_clean":
        rc, out = _git(cwd, "status", "--porcelain")
        res["passed"] = rc == 0 and out.strip() == ""
        res["observed"] = out.strip()[:300]
    elif a.kind == "git_branch":
        rc, out = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
        res["passed"] = rc == 0 and out.strip() == (a.value or out.strip())
        res["observed"] = out.strip()
    else:
        res["observed"] = f"unknown assertion kind: {a.kind}"
    return res


def check_all(cwd: str, assertions: Iterable[ArtifactAssertion]) -> list[dict]:
    return [check_one(cwd, a) for a in assertions]


def verdict_for(state: TaskState, state_known: bool, results: list[dict]) -> Verdict:
    """Acceptance rule shared by every adapter."""
    if not state_known:
        return Verdict.INCONCLUSIVE
    if state in (TaskState.FAILED, TaskState.CANCELLED, TaskState.LOST):
        return Verdict.REJECTED
    if state is not TaskState.SUCCEEDED:
        return Verdict.PENDING
    if not results:
        return Verdict.INCONCLUSIVE      # "file exists" is not "task passed"
    return Verdict.VERIFIED if all(r["passed"] for r in results) else Verdict.REJECTED
