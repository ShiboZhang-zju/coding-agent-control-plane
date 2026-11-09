"""T72 / UI-4 pre-deployment routing-guard evidence.

Runs the targeted suite (``tests/test_ui4_routing_guards.py``) and records which
guard each test proves, so the evidence says *what* was verified rather than
only "pytest exited 0".  It never upgrades a skip or a failure to PASS.

Exit codes: 0 PASS, 1 FAIL.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from sanitize_evidence import write_evidence  # noqa: E402

EVIDENCE = ROOT / "evidence" / "project" / "T72-ui4-routing-guards.json"
SUITE = "tests/test_ui4_routing_guards.py"

#: Test name -> the guard it is evidence for.  Keep in sync with the test file;
#: a test that exists but is not listed here still counts, it just has no label.
GUARDS = {
    "test_model_project_id_is_not_authority_when_routing_is_none":
        "model project_id cannot authorise a write (route=none)",
    "test_model_project_id_is_not_authority_when_routing_is_ambiguous":
        "model project_id cannot authorise a write (route=ambiguous)",
    "test_model_cannot_move_a_write_to_another_project":
        "model cannot retarget a write away from the named project",
    "test_model_cannot_read_a_different_project_than_the_sentence":
        "model cannot read a project the sentence does not point at",
    "test_agreement_between_model_and_router_still_writes":
        "agreement between model and router still creates exactly one Goal",
    "test_project_name_plus_another_projects_task_id_is_ambiguous":
        "project name + foreign task id is ambiguous, not ordered",
    "test_two_task_ids_from_two_projects_are_refused":
        "two task ids from two projects are refused",
    "test_two_goal_ids_from_two_projects_are_refused":
        "two goal ids from two projects are refused",
    "test_two_task_ids_from_one_project_still_resolve":
        "several ids from ONE project still resolve",
    "test_reference_does_not_skip_a_recent_ambiguous_turn":
        "a recent ambiguous turn is not skipped by 「这个项目」",
    "test_confirming_with_another_project_writes_nothing":
        "confirmation addressed to another project writes nothing",
    "test_confirmation_shows_project_name_id_goal_text_and_goal_id":
        "confirmation shows name + id + goal text + goal id",
    "test_bound_conversation_confirmation_behaviour_is_unchanged":
        "project-bound conversation confirmation is unchanged",
}


def run_suite() -> dict:
    env = dict(os.environ)
    for name in list(env):
        if name.startswith("CODEBUDDY_SAFE_DELETE"):
            env.pop(name, None)
    env["NODE_OPTIONS"] = ""
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT), str(ROOT / "scripts")])
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", SUITE, "-v", "--no-header"],
        cwd=ROOT, env=env, text=True, capture_output=True, timeout=1800)
    output = (completed.stdout or "") + (completed.stderr or "")
    outcome: dict[str, str] = {}
    for line in output.splitlines():
        match = re.match(r"^(\S+?::test_\w+)\s+(PASSED|FAILED|SKIPPED|ERROR)",
                         line.strip())
        if match:
            outcome[match.group(1).split("::", 1)[1]] = match.group(2)
    return {"exit_code": completed.returncode, "outcomes": outcome,
            "tail": output.strip()[-3000:]}


def main() -> int:
    result = run_suite()
    outcomes = result["outcomes"]
    missing = sorted(set(GUARDS) - set(outcomes))
    not_passed = sorted(name for name, state in outcomes.items()
                        if state != "PASSED")
    checks = {
        name: (outcomes.get(name) == "PASSED") for name in sorted(GUARDS)
    }
    passed = all(checks.values()) and not not_passed and not missing
    payload = {
        "task": "T72-ui4-routing-guards",
        "schema_version": "ui-4-routing-guards.v1",
        "result": "PASS" if passed else "FAIL",
        "suite": SUITE,
        "guards": {GUARDS[name]: checks[name] for name in sorted(GUARDS)},
        "outcomes": outcomes,
        "missing_guards": missing,
        "not_passed": not_passed,
        "environment": {
            "ledger": "isolated throwaway ledger per test under .poc-state/ui4-guards",
            "chat": "real LDChat + LDGoalStore + TaskStore behind a real "
                    "loopback HTTP server",
            "model_paths_covered": ["no endpoint (in-process router)",
                                    "fake intent_model with model_domains=local"],
            "worker": "none: chat never dispatches a WorkItem",
            "production_manager_touched": False,
        },
        "negative_assertions": (
            "every rejection re-reads SQLite: per-project Goal counts, the "
            "needs_human status of both seeded goals, the pending_action row and "
            "the WorkItem count (the harness seeds exactly one per project, so a "
            "chat-side dispatch would change the number)"),
        "pytest_tail": result["tail"],
    }
    write_evidence(EVIDENCE, payload)
    print(json.dumps({"result": payload["result"],
                      "not_passed": not_passed, "missing": missing,
                      "evidence": str(EVIDENCE.relative_to(ROOT))},
                     ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
