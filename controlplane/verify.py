"""Artifact-based acceptance with provenance.

The first PoC version was wrong in a way that mattered: it only asked "does the
file exist", so a task that wrote into *another* project could still be marked
`verified` (see evidence/jobs/T10-summary.json, B1). Acceptance now requires:

  1. a **baseline captured before dispatch** (git HEAD/status + hash of every
     declared target + fingerprint of every sibling repository);
  2. **ownership**: the asserted artifact must be new or changed relative to the
     baseline, or the asserted commit must be reachable in `baseline..HEAD`;
  3. **no unauthorised cross-project side effects**: if any sibling repository
     changed during the attempt, the verdict is `rejected` no matter what the
     assertions say.

Rationale stays the same as before: the worker's own terminal state is not an
acceptance signal (a shell job that exits 3 is still reported `done`).
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Iterable

from adapters.base import ArtifactAssertion, TaskState, Verdict


# --------------------------------------------------------------------------- #
# git / hashing helpers
# --------------------------------------------------------------------------- #
def _git(cwd: str, *args: str) -> tuple[int, str]:
    try:
        p = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=25)
        return p.returncode, (p.stdout or p.stderr).strip()
    except Exception as exc:  # noqa: BLE001
        return 1, f"{type(exc).__name__}: {exc}"


def _sha256(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def repo_fingerprint(path: str, ignore_prefixes: Iterable[str] = (".codebuddy/",)) -> dict:
    """(HEAD, status) of a git repo, or {'git': False} when it is not one.

    Worker bookkeeping (`.codebuddy/`, where worktrees live) is filtered out: an
    agent job creating a worktree in repo-b must not look like repo-a reached
    into repo-b (T11 first run produced exactly that false positive).
    """
    if not path or not Path(path).exists():
        return {"git": False, "exists": False}
    rc, head = _git(path, "rev-parse", "HEAD")
    if rc != 0:
        return {"git": False, "exists": True}
    _, status = _git(path, "status", "--porcelain")
    kept = []
    for line in status.splitlines():
        rel = line[3:].strip().replace("\\", "/")
        if any(rel.startswith(p) for p in ignore_prefixes):
            continue
        kept.append(line)
    return {"git": True, "exists": True, "head": head.strip(), "status": "\n".join(kept)}


def sibling_repos(cwd: str, exclude: Iterable[str] = ()) -> dict[str, dict]:
    """Every git repo next to `cwd` (same parent directory)."""
    parent = Path(cwd).resolve().parent
    skip = {str(Path(p).resolve()) for p in exclude}
    out: dict[str, dict] = {}
    try:
        children = [p for p in parent.iterdir() if p.is_dir()]
    except Exception:
        return out
    for child in children:
        if not (child / ".git").exists():
            continue
        if str(child) in skip or str(child.resolve()) == str(Path(cwd).resolve()):
            continue
        out[str(child)] = repo_fingerprint(str(child))
    return out


def capture_baseline(cwd: str, assertions: Iterable[ArtifactAssertion]) -> dict:
    """Snapshot everything acceptance later needs to prove provenance."""
    import time

    targets: dict[str, str | None] = {}
    for a in assertions:
        if a.kind in ("file_exists", "file_contains"):
            p = Path(cwd) / a.target
            targets[a.target] = _sha256(p) if p.is_file() else None
    return {
        "captured_at": time.time(),
        "cwd": cwd,
        "head": repo_fingerprint(cwd).get("head"),
        "status": repo_fingerprint(cwd).get("status", ""),
        "targets": targets,
        "siblings": sibling_repos(cwd),
    }


# --------------------------------------------------------------------------- #
# assertions
# --------------------------------------------------------------------------- #
def check_one(cwd: str, a: ArtifactAssertion, baseline: dict | None = None) -> dict:
    baseline = baseline or {}
    target = Path(cwd) / a.target
    res = {"kind": a.kind, "target": a.target, "value": a.value,
           "passed": False, "owned": None, "observed": ""}

    if a.kind == "file_exists":
        exists = target.exists()
        prev = (baseline.get("targets") or {}).get(a.target, "NOT_IN_BASELINE")
        if not exists:
            res["observed"] = "missing"
        elif prev == "NOT_IN_BASELINE":
            res.update(passed=True, owned=True, observed="exists (new since baseline)")
        else:
            now = _sha256(target) if target.is_file() else ("dir" if prev is None else prev)
            changed = now != prev
            res.update(passed=changed, owned=changed,
                       observed="changed since baseline" if changed
                       else "pre-existing and unchanged (not produced by this attempt)")
    elif a.kind == "path_absent":
        res["passed"] = not target.exists()
        res["owned"] = not res["passed"]
        res["observed"] = "missing (expected)" if res["passed"] else "present (unexpected)"
    elif a.kind == "file_contains":
        if not target.is_file():
            res["observed"] = "file missing"
        else:
            text = target.read_text(encoding="utf-8", errors="replace")
            has_text = (a.value or "") in text
            prev = (baseline.get("targets") or {}).get(a.target, "NOT_IN_BASELINE")
            now_hash = _sha256(target)
            owned = prev == "NOT_IN_BASELINE" or prev != now_hash
            res["passed"] = has_text and owned
            res["owned"] = owned
            res["observed"] = (f"contains value={has_text}, changed_since_baseline={owned}")
    elif a.kind == "git_commit":
        base_head = baseline.get("head")
        rc, ahead = _git(cwd, "log", "--oneline", f"{base_head}..HEAD") if base_head else (1, "")
        rc2, log_all = _git(cwd, "log", "--oneline", "-15")
        found_ahead = rc == 0 and (a.value or "") in ahead
        res["passed"] = found_ahead
        res["owned"] = found_ahead
        res["observed"] = (f"commits since baseline: {ahead.splitlines()[:3]}" if ahead
                          else f"no new commit (head log: {log_all.splitlines()[:2]})")
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


def check_all(cwd: str, assertions: Iterable[ArtifactAssertion],
              baseline: dict | None = None) -> list[dict]:
    return [check_one(cwd, a, baseline) for a in assertions]


# --------------------------------------------------------------------------- #
# side effects
# --------------------------------------------------------------------------- #
def side_effects(cwd: str, baseline: dict | None,
                 allowed_paths: Iterable[str] = (),
                 allowed_prefixes: Iterable[str] = ()) -> list[dict]:
    """Changes outside this project that the attempt was not allowed to make."""
    if not baseline:
        return []
    allowed = {str(Path(cwd) / p) for p in allowed_paths}
    prefixes = tuple(allowed_prefixes)
    violations: list[dict] = []
    for repo, before in (baseline.get("siblings") or {}).items():
        after = repo_fingerprint(repo)
        if before == after:
            continue
        violations.append({"type": "cross_project_change", "repo": repo,
                           "before": before, "after": after})
    here_before = {"head": baseline.get("head"), "status": baseline.get("status", "")}
    here_after = repo_fingerprint(cwd)
    if (here_before["head"] != here_after.get("head")
            or here_before["status"] != here_after.get("status", "")):
        status_now = set(here_after.get("status", "").splitlines())
        status_before = set(here_before["status"].splitlines())
        extra = [ln for ln in (status_now - status_before) if ln.strip()]
        if allowed:
            extra = [ln for ln in extra
                     if str(Path(cwd) / ln[3:].strip()) not in allowed]
        if prefixes:
            extra = [ln for ln in extra
                     if not any(ln[3:].strip().replace("\\", "/").startswith(p) for p in prefixes)]
        if extra:
            violations.append({"type": "undeclared_local_change", "cwd": cwd,
                               "entries": extra[:20]})
    return violations


# --------------------------------------------------------------------------- #
# verdict
# --------------------------------------------------------------------------- #
def verdict_for(state: TaskState, state_known: bool, results: list[dict],
                violations: list[dict] | None = None) -> Verdict:
    """Acceptance rule shared by every adapter."""
    if violations:
        # an attempt that touched another project can never be accepted,
        # even if every declared artifact assertion passed
        return Verdict.REJECTED
    if not state_known:
        return Verdict.INCONCLUSIVE
    if state in (TaskState.FAILED, TaskState.CANCELLED, TaskState.LOST):
        return Verdict.REJECTED
    if state is not TaskState.SUCCEEDED:
        return Verdict.PENDING
    if not results:
        return Verdict.INCONCLUSIVE      # "file exists" is not "task passed"
    return Verdict.VERIFIED if all(r["passed"] for r in results) else Verdict.REJECTED
