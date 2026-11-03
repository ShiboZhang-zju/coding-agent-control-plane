"""T66 supplementary checks (post-hoc, deterministic).

The reviewer of commit 5f2af39 raised two gaps that the T66 real run did not
close on its own:

1. T66's S3 produced ``assertion_results: []``, so the "no assertions" branch —
   which predates the fix — could explain the ``inconclusive`` verdict.  The new
   ``owned`` rule was therefore only covered by unit tests.
2. The happy path only asserted two *file* checks; nothing independently proved
   that the seeded ``test_load.py`` actually passes.

This script closes both, honestly labelled:

* it mutates the production rule inside a throw-away ``git worktree`` of HEAD
  (never in this working tree) and asserts the new tests fail without the guard
  — i.e. the guard is what they protect.  The production file is only ever
  *read* here; its SHA-256 is recorded before and after as a proof;
* it runs the seeded tests against the worktree that the real run left on disk,
  and records the exit code.

It does NOT re-run the real LLM/Worker chain.  The evidence it writes says so.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from sanitize_evidence import write_evidence  # noqa: E402

EVIDENCE = ROOT / "evidence" / "project" / "T66b-supplementary-checks.json"
GUARD = ("    if not any(r.get(\"owned\") for r in results):\n"
         "        # every assertion was a negative one: nothing proves the attempt\n"
         "        # produced anything, so this is not acceptance.\n"
         "        return Verdict.INCONCLUSIVE\n")
TEST_FILE = ROOT / "tests" / "test_verifier_negative_assertions.py"


def _pytest(cwd: Path, *targets: str) -> dict:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header",
         "-p", "no:cacheprovider", *targets],
        cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=1800)
    summary = [line.strip() for line in (proc.stdout or "").splitlines()
               if " passed" in line or " failed" in line or " error" in line]
    return {"returncode": proc.returncode,
            "summary": summary[-1] if summary else "",
            "stdout_tail": (proc.stdout or "").strip()[-600:],
            "stderr_tail": (proc.stderr or "").strip()[-600:]}


def _sha256_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _git(*args: str) -> tuple[int, str]:
    proc = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=300)
    return proc.returncode, (proc.stdout or proc.stderr or "").strip()


def mutation_check() -> dict:
    """Remove the ``owned`` guard in a scratch worktree and see the tests notice.

    The mutation happens in a throw-away ``git worktree`` of ``HEAD``, never in
    this working tree: a killed process must not be able to leave production
    source mutated, and a concurrent Runtime/test run must not observe a
    half-mutated rule.  The production file is only read, and its bytes are
    fingerprinted before and after as evidence that nothing touched it.
    """
    source = ROOT / "controlplane" / "verify.py"
    digest_before = _sha256_file(source)
    if digest_before is None:
        return {"performed": False, "reason": f"cannot read {source}"}
    try:
        original = source.read_text(encoding="utf-8")
    except OSError as exc:
        return {"performed": False, "reason": f"cannot decode: {exc}"}
    if GUARD not in original:
        return {"performed": False,
                "reason": "guard text not found; the rule may have been rewritten"}

    scratch = Path(tempfile.mkdtemp(prefix="t66b-mutation-"))
    worktree = scratch / "repo"
    created = False
    caught = None
    run: dict = {}
    cleanup: list[str] = []
    try:
        rc, out = _git("worktree", "add", "--detach", str(worktree), "HEAD")
        if rc != 0:
            return {"performed": False,
                    "reason": f"git worktree add failed: {out[-300:]}",
                    "production_source_sha256": digest_before}
        created = True
        target = worktree / "controlplane" / "verify.py"
        mutated = target.read_text(encoding="utf-8")
        if GUARD not in mutated:
            return {"performed": False,
                    "reason": ("HEAD's verify.py does not contain the guard; the "
                               "working tree is ahead of HEAD"),
                    "production_source_sha256": digest_before}
        target.write_text(mutated.replace(GUARD, ""), encoding="utf-8")
        # Same file, same test, but rooted in the scratch worktree.
        run = _pytest(worktree, "tests/test_verifier_negative_assertions.py")
        run["stdout_tail"] = run["stdout_tail"].replace(str(worktree), "<scratch>")
        run["stderr_tail"] = run["stderr_tail"].replace(str(worktree), "<scratch>")
        caught = run["returncode"] != 0
    finally:
        if created:
            rc, out = _git("worktree", "remove", "--force", str(worktree))
            if rc != 0:
                cleanup.append(f"worktree remove failed: {out[-200:]}")
        rc, out = _git("worktree", "prune")
        if rc != 0:
            cleanup.append(f"worktree prune failed: {out[-200:]}")
        shutil.rmtree(scratch, ignore_errors=True)

    digest_after = _sha256_file(source)
    return {
        "performed": True,
        "guard_removed": True,
        "isolation": ("mutation applied to a detached git worktree of HEAD; the "
                      "production working tree is read-only for this check"),
        "tests_fail_without_the_guard": caught,
        "mutation_run": run,
        "production_source_sha256_before": digest_before,
        "production_source_sha256_after": digest_after,
        "production_source_untouched": digest_before == digest_after,
        "cleanup_errors": cleanup,
    }


def find_seeded_worktrees() -> list[Path]:
    """Worktrees the real T66 run left behind, newest first."""
    found = []
    for repo in sorted((ROOT / "sandbox").glob("t66-real-e2e-*"), reverse=True):
        for worktree in repo.glob("happy/.codebuddy/worktrees/*"):
            if (worktree / "test_load.py").is_file():
                found.append(worktree)
    return found


def seeded_tests() -> dict:
    candidates = find_seeded_worktrees()
    if not candidates:
        return {"ran": False,
                "reason": "no sandbox worktree with test_load.py remains on disk",
                "note": ("The in-script check now runs during a real T66 execution; "
                         "this post-hoc probe cannot reproduce it after cleanup.")}
    results = []
    for worktree in candidates[:3]:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "test_load.py", "-q",
             "--no-header", "-p", "no:cacheprovider"],
            cwd=str(worktree), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=600)
        results.append({
            "worktree": str(worktree.relative_to(ROOT)).replace("\\", "/"),
            "returncode": proc.returncode,
            "stdout_tail": (proc.stdout or "").strip()[-300:],
        })
    return {"ran": True, "results": results,
            "all_passed": all(item["returncode"] == 0 for item in results)}


def main() -> int:
    mutation = mutation_check()
    seeded = seeded_tests()

    checks = {
        # The guard is load-bearing: without it, the tests fail.
        "mutation.guard_removal_is_detected": bool(
            mutation.get("tests_fail_without_the_guard")),
        # ... and the mutation never reaches this working tree.
        "mutation.production_source_untouched": bool(
            mutation.get("production_source_untouched")),
        "mutation.scratch_worktree_cleaned_up": bool(
            mutation.get("performed") and not mutation.get("cleanup_errors")),
        # A runnable proof that the seeded behaviour actually holds.
        "seeded.tests_were_run": bool(seeded.get("ran")),
        "seeded.tests_pass": bool(seeded.get("all_passed")),
    }
    evidence = {
        "test": "T66b supplementary checks for the acceptance rule",
        "mode": ("deterministic, post-hoc: mutation of the acceptance rule inside a "
                 "throw-away git worktree of HEAD (the production working tree is "
                 "read-only; its SHA-256 is recorded before/after) + the seeded tests "
                 "run against the worktree the real run left on disk"),
        "why": ("T66's S3 produced an empty assertion list, so its inconclusive "
                "verdict is also explained by the pre-existing 'no assertions' "
                "branch; and the happy path never executed the seeded tests. Both "
                "gaps are closed here without claiming a new real E2E run."),
        "not_covered": [
            "re-running the real LLM/CodeBuddy chain (not attempted here)",
            "browser DOM interaction",
            "uncommitted test edits: the scratch worktree is checked out from HEAD, "
            "so the mutation run always exercises the committed test file",
        ],
        "checks": checks,
        "mutation": mutation,
        "seeded_tests": seeded,
        "result": "Pass" if all(checks.values()) else "Fail",
    }
    write_evidence(EVIDENCE, evidence)
    failed = [name for name, value in checks.items() if not value]
    print(json.dumps({"result": evidence["result"], "failed": failed,
                      "evidence": str(EVIDENCE.relative_to(ROOT))},
                     ensure_ascii=False, indent=2))
    return 0 if evidence["result"] == "Pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
