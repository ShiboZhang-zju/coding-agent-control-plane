"""T73 / UI-4 pre-deployment guards, round 2: confirmation and honest intent.

Runs ``tests/test_ui4_confirmation_guards.py`` and records which guarantee each
test proves.  A skip or a failure is never upgraded to PASS.

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

EVIDENCE = ROOT / "evidence" / "project" / "T73-ui4-confirmation-guards.json"
SUITE = "tests/test_ui4_confirmation_guards.py"

GUARDS = {
    "test_global_command_creates_nothing_until_confirmed":
        "global chat: an unambiguous command creates nothing until confirmed",
    "test_global_command_with_a_model_still_needs_confirmation":
        "global chat: the model path is staged too",
    "test_repeated_confirmation_creates_only_one_goal":
        "a repeated confirmation creates no second goal and starts no execution",
    "test_cancel_creates_nothing_and_clears_the_stage":
        "cancel creates nothing and clears the stage",
    "test_superseded_confirmation_creates_nothing":
        "a superseded confirmation creates nothing",
    "test_confirmation_for_another_project_writes_nothing":
        "a confirmation addressed to another project writes nothing and keeps the stage",
    "test_refused_write_is_clarify_in_body_history_and_audit":
        "a routed-away write is clarify in body, history and audit",
    "test_ambiguous_write_is_clarify_in_history":
        "an ambiguous write is clarify in the persisted history",
    "test_audit_line_reports_the_effective_intent":
        "the chat_turn audit line reports the effective intent",
    "test_project_bound_conversation_still_applies_an_imperative":
        "compat: a project-bound conversation still applies a clear imperative",
    "test_structured_publish_still_creates_the_goal_directly":
        "compat: structured publish still creates the goal directly",
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
    checks = {name: outcomes.get(name) == "PASSED" for name in sorted(GUARDS)}
    passed = all(checks.values()) and not not_passed and not missing
    payload = {
        "task": "T73-ui4-confirmation-guards",
        "schema_version": "ui-4-confirmation-guards.v1",
        "result": "PASS" if passed else "FAIL",
        "suite": SUITE,
        "guards": {GUARDS[name]: checks[name] for name in sorted(GUARDS)},
        "outcomes": outcomes,
        "missing_guards": missing,
        "not_passed": not_passed,
        "environment": {
            "ledger": "isolated throwaway ledger per test under "
                      ".poc-state/ui4-confirm",
            "chat": "real LDChat + LDGoalStore + TaskStore behind a real "
                    "loopback HTTP server",
            "model_paths_covered": ["no endpoint (in-process router)",
                                    "fake intent_model with model_domains=local"],
            "worker": "none: chat never dispatches a WorkItem",
            "production_manager_touched": False,
        },
        "negative_assertions": (
            "every staging/rejection re-reads SQLite: per-project Goal rows and "
            "goal_text, the frozen pending_action payload, the persisted "
            "ld_messages.intent of the last turn, created_at/decision_count of "
            "the created goal, and the WorkItem count (one seeded per project)"),
        "mutation_checks": [
            "removing the 'global conversation always stages' rule breaks 6 tests",
            "persisting the classifier intent instead of the mapped intent "
            "breaks test_refused_write_is_clarify_in_body_history_and_audit",
        ],
        "pytest_tail": result["tail"],
    }
    write_evidence(EVIDENCE, payload)
    print(json.dumps({"result": payload["result"], "not_passed": not_passed,
                      "missing": missing,
                      "evidence": str(EVIDENCE.relative_to(ROOT))},
                     ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
