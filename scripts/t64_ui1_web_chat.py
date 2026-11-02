"""T64 / UI-1: reproducible Web LD chat regression evidence.

The browser walk-through is performed separately because it needs a live GUI.
This producer runs the deterministic HTTP/security suites, checks the static
client syntax, and records whether the environment can support the separate
real LD -> CodeBuddy acceptance.  It never turns a missing endpoint into Pass.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from sanitize_evidence import write_evidence  # noqa: E402


EVIDENCE = ROOT / "evidence" / "project" / "T64-ui1-web-ld-chat.json"


def run(argv: list[str]) -> dict:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT), str(ROOT / "scripts"), existing]).rstrip(os.pathsep)
    completed = subprocess.run(
        argv, cwd=ROOT, env=env, text=True, capture_output=True, timeout=180)
    return {
        "argv": argv,
        "exit_code": completed.returncode,
        "stdout": completed.stdout.strip()[-4_000:],
        "stderr": completed.stderr.strip()[-2_000:],
    }


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def browser_probe(path: Path | None) -> dict:
    """Read only durable facts left by the live browser walk-through."""
    if path is None:
        return {"status": "NOT_RUN"}
    if not path.is_file():
        return {"status": "ERROR", "reason": "browser ledger is missing"}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conversations = conn.execute(
            "SELECT conversation_id, project_id, message_count "
            "FROM ld_conversations ORDER BY updated_at DESC").fetchall()
        messages = conn.execute(
            "SELECT role, intent FROM ld_messages ORDER BY id").fetchall()
        goals = conn.execute(
            "SELECT goal_id, status, resume_count, max_decisions_total, "
            "max_tasks_total FROM ld_goals ORDER BY created_at").fetchall()
    finally:
        conn.close()
    intents = []
    for row in messages:
        try:
            intent = json.loads(row["intent"] or "{}")
        except json.JSONDecodeError:
            intent = {}
        if intent.get("intent"):
            intents.append(intent["intent"])
    resumed = [dict(row) for row in goals if int(row["resume_count"] or 0) == 1]
    checks = {
        "conversation_persisted": bool(conversations),
        "history_has_user_and_ld": (
            {row["role"] for row in messages} >= {"user", "ld"}),
        "submit_goal_observed": "submit_goal" in intents,
        "resume_goal_observed": "resume_goal" in intents,
        "confirmed_resume_once": len(resumed) == 1,
        "resume_budget_preserved": bool(resumed) and
            resumed[0]["max_decisions_total"] == 7 and
            resumed[0]["max_tasks_total"] == 2,
    }
    return {
        "status": "Pass" if all(checks.values()) else "Fail",
        "checks": checks,
        "conversation_count": len(conversations),
        "message_count": len(messages),
        "roles": sorted({row["role"] for row in messages}),
        "intent_labels": intents,
        "goals": [dict(row) for row in goals],
        "note": ("Visual checks and input-retention/reload checks were performed "
                 "through browser automation; this probe records the durable "
                 "Ledger/chat effects without transcript content."),
    }


def main() -> int:
    python = sys.executable
    tests = run([
        python, "-m", "pytest",
        "tests/test_ui1_web_chat.py", "tests/test_ui0_dashboard.py", "-q",
    ])
    node_check = run(["node", "--check", "dashboard/static/app.js"])
    regression_refs = {}
    for name in (
        "T50-conversational-ld.json",
        "T51-intent-safety.json",
        "T52-conversation-security.json",
        "T53-chat-outbound-safety.json",
        "T63-ui0-dashboard.json",
    ):
        path = ROOT / "evidence" / "project" / name
        regression_refs[name] = {
            "exists": path.is_file(),
            "sha256": digest(path) if path.is_file() else None,
        }

    model_ready = bool(
        os.environ.get("LD_MODEL_BASE_URL") and os.environ.get("LD_MODEL_NAME"))
    real_e2e = "NOT_RUN" if model_ready else "BLOCKED_ENVIRONMENT"
    browser_ledger = os.environ.get("UI1_BROWSER_LEDGER")
    browser = browser_probe(Path(browser_ledger) if browser_ledger else None)
    passed = (tests["exit_code"] == 0
              and node_check["exit_code"] == 0
              and all(item["exists"] for item in regression_refs.values()))
    evidence = {
        "test": "T64 UI-1 Web LD Interaction",
        "mode": ("real loopback HTTP + real LDChat/SQLite; no Worker and no "
                 "decision model in deterministic tests"),
        "commands": {
            "pytest": tests,
            "javascript_syntax": node_check,
        },
        "regression_evidence_refs": regression_refs,
        "coverage": [
            "conversation create/continue/history/project isolation",
            "real Goal row creation and deterministic idempotency",
            "model fallback and invalid-model-intent fail-closed",
            "resume confirm/cancel/stale/concurrent and budget immutability",
            "UI-0 default 405, auth, Origin, schema, size and XSS boundaries",
            "UI-0 projection regression",
        ],
        "real_ld_codebuddy_e2e": real_e2e,
        "browser_walkthrough": browser,
        "real_ld_environment": {
            "base_url_configured": bool(os.environ.get("LD_MODEL_BASE_URL")),
            "model_name_configured": bool(os.environ.get("LD_MODEL_NAME")),
            "domains_configured": bool(os.environ.get("LD_MODEL_DOMAINS")),
            "note": ("No endpoint URL, model id, API key or credential value is "
                     "written to evidence."),
        },
        "result": "Pass" if passed else "Fail",
    }
    write_evidence(EVIDENCE, evidence)
    print(json.dumps({
        "result": evidence["result"],
        "pytest_exit": tests["exit_code"],
        "javascript_exit": node_check["exit_code"],
        "real_ld_codebuddy_e2e": real_e2e,
        "evidence": str(EVIDENCE.relative_to(ROOT)),
    }, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
