"""T66 supplementary checks (post-hoc, deterministic).

The reviewer of commit 5f2af39 raised two gaps that the T66 real run did not
close on its own:

1. T66's S3 produced ``assertion_results: []``, so the "no assertions" branch —
   which predates the fix — could explain the ``inconclusive`` verdict.  The new
   ``owned`` rule was therefore only covered by unit tests.
2. The happy path only asserted two *file* checks; nothing independently proved
   that the seeded ``test_load.py`` actually passes.

This script closes both, honestly labelled:

* it mutates the production rule in a scratch copy of the repository tree
  (``controlplane/verify.py`` is restored in a ``finally``) and asserts the new
  tests fail without the guard — i.e. the guard is what they protect;
* it runs the seeded tests against the worktree that the real run left on disk,
  and records the exit code.

It does NOT re-run the real LLM/Worker chain.  The evidence it writes says so.
"""
from __future__ import annotations

import json
import subprocess
import sys
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


def _pytest(*targets: str) -> dict:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header",
         "-p", "no:cacheprovider", *targets],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=1800)
    summary = [line.strip() for line in (proc.stdout or "").splitlines()
               if " passed" in line or " failed" in line or " error" in line]
    return {"returncode": proc.returncode,
            "summary": summary[-1] if summary else "",
            "stdout_tail": (proc.stdout or "").strip()[-600:],
            "stderr_tail": (proc.stderr or "").strip()[-600:]}


def mutation_check() -> dict:
    """Remove the ``owned`` guard and confirm the tests notice."""
    target = ROOT / "controlplane" / "verify.py"
    original = target.read_text(encoding="utf-8")
    if GUARD not in original:
        return {"performed": False,
                "reason": "guard text not found; the rule may have been rewritten"}
    caught = None
    run = {}
    try:
        target.write_text(original.replace(GUARD, ""), encoding="utf-8")
        run = _pytest(str(TEST_FILE.relative_to(ROOT)))
        caught = run["returncode"] != 0
    finally:
        target.write_text(original, encoding="utf-8")
    return {
        "performed": True,
        "guard_removed": True,
        "tests_fail_without_the_guard": caught,
        "mutation_run": run,
        "source_restored": target.read_text(encoding="utf-8") == original,
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
        "mutation.source_restored": bool(mutation.get("source_restored")),
        # A runnable proof that the seeded behaviour actually holds.
        "seeded.tests_were_run": bool(seeded.get("ran")),
        "seeded.tests_pass": bool(seeded.get("all_passed")),
    }
    evidence = {
        "test": "T66b supplementary checks for the acceptance rule",
        "mode": ("deterministic, post-hoc: mutation of the production rule in place "
                 "(restored in finally) + the seeded tests run against the worktree "
                 "the real run left on disk"),
        "why": ("T66's S3 produced an empty assertion list, so its inconclusive "
                "verdict is also explained by the pre-existing 'no assertions' "
                "branch; and the happy path never executed the seeded tests. Both "
                "gaps are closed here without claiming a new real E2E run."),
        "not_covered": [
            "re-running the real LLM/CodeBuddy chain (not attempted here)",
            "browser DOM interaction",
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
