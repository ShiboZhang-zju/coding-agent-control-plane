"""T74: cross-project portfolio report — isolated fixtures + production contrast.

Two phases, both read-only with respect to the production ledger:

1. **Isolated fixtures.**  A throwaway ledger with four projects covering the
   states the report must distinguish (rejected-but-not-blocked,
   inconclusive-and-blocked, completed goal, no tasks at all).  Runs the real
   ``LDChat`` and records what it answered.

2. **Production contrast.**  Opens the *real* production ledger read-only through
   the same code path, prints the report, and cross-checks every total against a
   direct SQL read.  No number is hard-coded: the check compares the report with
   a fresh query, so a change in production content changes both sides.

Nothing here writes to the production ledger; the isolated phase writes only to
its own throwaway database under ``.poc-state``.
"""
from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

from adapters.base import ArtifactAssertion                    # noqa: E402
from controlplane.plane import ControlPlane                     # noqa: E402
from controlplane.project_service import ProjectService          # noqa: E402
from controlplane.store import TaskStore                        # noqa: E402
from ld.chat import LDChat                                      # noqa: E402
from ld.chat_store import LDChatStore                           # noqa: E402
from ld.goal_store import LDGoalStore, goal_identity            # noqa: E402
from ld.tools import LDTools                                    # noqa: E402
from sanitize_evidence import write_evidence                    # noqa: E402

EVIDENCE = ROOT / "evidence" / "project" / "T74-ui4-portfolio.json"
PROD_LEDGER = Path(r"C:\ACP\acp-manager-state\ledger.db")


class _Adapter:
    name = "stub"


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True,
                   capture_output=True)
    return path


def _chat_for(ledger: Path) -> tuple[LDChat, list]:
    store = TaskStore(ledger)
    plane = ControlPlane(_Adapter(), store, require_registered_projects=True)
    tools = LDTools(ProjectService(plane))
    goals = LDGoalStore(ledger)
    chats = LDChatStore(ledger)
    chat = LDChat(tools, goals, chats, None,
                  model_authorized_security_domains=("local",))
    return chat, [chats, goals, store]


def _close(handles: list) -> None:
    for handle in handles:
        try:
            handle.close()
        except Exception:  # noqa: BLE001 - teardown only
            pass


def build_fixture() -> tuple[Path, dict]:
    """Four projects: the states the report must tell apart."""
    root = ROOT / ".poc-state" / "t74" / uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    ledger = root / "fixture.db"
    store = TaskStore(ledger)
    plane = ControlPlane(_Adapter(), store, require_registered_projects=True)
    alpha = plane.register_project("ACP-Alpha", str(_git_repo(root / "a")),
                                   default_adapter="stub")
    beta = plane.register_project("ACP-Beta", str(_git_repo(root / "b")),
                                  default_adapter="stub")
    gamma = plane.register_project("ACP-Gamma", str(_git_repo(root / "g")),
                                   default_adapter="stub")
    delta = plane.register_project("ACP-Delta", str(_git_repo(root / "d")),
                                   default_adapter="stub")

    rejected = plane.enqueue("alpha rejected", str(root / "a"),
                             assertions=[ArtifactAssertion("file_exists", "a.md")],
                             project_id=alpha["id"])["task_id"]
    blocked = plane.enqueue("beta blocked", str(root / "b"),
                            assertions=[ArtifactAssertion("file_exists", "b.md")],
                            project_id=beta["id"])["task_id"]
    beta_rejected = plane.enqueue("beta rejected", str(root / "b"),
                                  assertions=[ArtifactAssertion("file_exists",
                                                                "b2.md")],
                                  project_id=beta["id"])["task_id"]
    verified = plane.enqueue("gamma verified", str(root / "g"),
                             assertions=[ArtifactAssertion("file_exists", "g.md")],
                             project_id=gamma["id"])["task_id"]
    store.update(rejected, state="succeeded", verdict="rejected",
                 queue_status="complete")
    store.update(beta_rejected, state="succeeded", verdict="rejected",
                 queue_status="complete")
    store.update(blocked, state="unknown", verdict="inconclusive",
                 queue_status="needs_human")
    store.update(verified, state="succeeded", verdict="verified",
                 queue_status="complete")
    store.close()

    goals = LDGoalStore(ledger)
    pairs = [
        (alpha, "alpha exhausted goal", "needs_human",
         {"stop": "lifetime decision budget exhausted"}),
        (beta, "beta needs decision", "needs_human",
         {"step": 2, "action": "request_human", "next_state": "human_required"}),
        (gamma, "gamma finished", "completed", None),
    ]
    ids = {}
    for project, text, status, decision in pairs:
        goal_id, ref = goal_identity(text, project["id"])
        goals.get_or_create(goal_id, project["id"], text, ref)
        fields = {"status": status}
        if decision:
            fields["last_decision"] = decision
        goals.update_goal(goal_id, **fields)
        ids[project["name"]] = goal_id
    goals.close()
    return ledger, {"alpha": alpha, "beta": beta, "gamma": gamma,
                    "delta": delta, "tasks": {
                        "alpha_rejected": rejected, "beta_blocked": blocked,
                        "beta_rejected": beta_rejected,
                        "gamma_verified": verified}, "goals": ids}


def production_truth(ledger: Path) -> dict:
    """Direct SQL truth from the production ledger, for cross-checking."""
    conn = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=1")
    try:
        projects = []
        for row in conn.execute(
                "SELECT id, name, active FROM projects ORDER BY id"):
            pid, name, active = row
            items = conn.execute(
                "SELECT state, verdict, queue_status FROM work_items "
                "WHERE project_id=?", (pid,)).fetchall()
            goals = conn.execute(
                "SELECT status FROM ld_goals WHERE project_id=?", (pid,)).fetchall()
            projects.append({
                "project_id": pid, "name": name, "active": bool(active),
                "total": len(items),
                "verified": sum(1 for _, v, _ in items if v == "verified"),
                "rejected": sum(1 for _, v, _ in items if v == "rejected"),
                "inconclusive": sum(1 for _, v, _ in items if v == "inconclusive"),
                "blocked": sum(
                    1 for state, _, queue in items
                    if state in ("unknown", "dispatch_unknown")
                    or queue == "needs_approval"),
                "needs_human_goals": sum(1 for s, in goals if s == "needs_human"),
                "goal_status": {status: sum(1 for s, in goals if s == status)
                                for status, in goals} if goals else {},
            })
        return {"projects": projects}
    finally:
        conn.close()


def main() -> int:
    checks: dict[str, bool] = {}
    detail: dict = {}

    # -- phase 1: isolated fixtures -------------------------------------- #
    ledger, fixture = build_fixture()
    chat, handles = _chat_for(ledger)
    try:
        turn = chat.handle("汇总所有项目目前的进度")
        answer = turn["answer"]
        facts = turn.get("facts") or {}
        rows = {row["project_id"]: row for row in facts.get("projects") or []}
        alpha_row = rows[fixture["alpha"]["id"]]
        beta_row = rows[fixture["beta"]["id"]]
        gamma_row = rows[fixture["gamma"]["id"]]
        delta_row = rows[fixture["delta"]["id"]]

        checks["fixture_intent_is_portfolio"] = (
            turn["intent"]["intent"] == "portfolio_status")
        checks["fixture_no_goal_created"] = turn["goal_ids"] == []
        # A rejected-but-not-blocked task must be counted as abnormal *and* not
        # as blocked.  (One check, not two: an earlier separate
        # "blocked == 0" boolean was true only because 0 is falsy, which tested
        # nothing.)
        checks["fixture_rejected_counted_separately"] = (
            alpha_row["abnormal_verdict"] == 1 and alpha_row["blocked"] == 0)
        checks["fixture_needs_human_counted"] = (
            alpha_row["needs_human_goals"] == 1)
        checks["fixture_overlap_not_double_counted"] = (
            beta_row["blocked"] == 1 and beta_row["abnormal_verdict"] == 1)
        checks["fixture_completed_goal_not_unfinished"] = (
            gamma_row["needs_human_goals"] == 0
            and gamma_row["goal_status_counts"].get("completed") == 1)
        checks["fixture_empty_project_reported"] = (
            delta_row["counts"].get("total") == 0
            and delta_row["blocked"] == 0)
        for project in (fixture["alpha"], fixture["beta"], fixture["gamma"],
                        fixture["delta"]):
            checks[f"fixture_mentions_{project['name']}"] = (
                project["id"] in answer)

        # The unknown-state task must be declared undecidable, not guessed.
        checks["fixture_declares_unknown"] = any(
            fixture["tasks"]["beta_blocked"] in line
            for line in facts.get("unknowns") or [])
        # Fact / judgement separation is in the text.
        checks["fixture_separates_fact_from_judgement"] = (
            "一、事实" in answer and "二、判断" in answer and "规则：" in answer)
        detail["fixture_answer"] = answer

        # Read-only proof: business tables unchanged by the report.
        store = TaskStore(ledger, read_only=True)
        try:
            before = sorted((t["task_id"], t.get("state"), t.get("verdict"))
                            for t in store.all())
        finally:
            store.close()
        for question in ("所有项目有哪些未完成目标？", "现在有什么需要我处理",
                         "给出各项目的优先级建议"):
            chat.handle(question)
        store = TaskStore(ledger, read_only=True)
        try:
            after = sorted((t["task_id"], t.get("state"), t.get("verdict"))
                           for t in store.all())
        finally:
            store.close()
        checks["fixture_read_only"] = before == after
        detail["fixture_rows"] = {key: rows[key] for key in rows}
    finally:
        _close(handles)

    # -- phase 2: production contrast ------------------------------------ #
    prod_ok = PROD_LEDGER.is_file()
    checks["production_ledger_present"] = prod_ok
    if prod_ok:
        truth = production_truth(PROD_LEDGER)
        chat, handles = _chat_for(PROD_LEDGER)
        try:
            turn = chat.handle("汇总所有项目目前的进度")
            answer = turn["answer"]
            facts = turn.get("facts") or {}
            reported = {row["project_id"]: row
                        for row in facts.get("projects") or []}
            totals = facts.get("totals") or {}
            mismatches = []
            for expected in truth["projects"]:
                got = reported.get(expected["project_id"])
                if got is None:
                    mismatches.append(f"{expected['name']}: missing")
                    continue
                for key in ("total", "verified", "rejected", "inconclusive",
                            "needs_human_goals"):
                    report_key = ("work_items_total" if key == "total" else key)
                    if key == "total":
                        report_value = got["counts"].get("total")
                    elif key in ("verified", "rejected", "inconclusive"):
                        report_value = got["counts"].get(key)
                    else:
                        report_value = got["needs_human_goals"]
                    if report_value != expected[key]:
                        mismatches.append(
                            f"{expected['name']}.{key}: report={report_value} "
                            f"ledger={expected[key]}")
                    del report_key
            checks["production_numbers_match_ledger"] = not mismatches
            checks["production_intent_is_portfolio"] = (
                turn["intent"]["intent"] == "portfolio_status")
            checks["production_no_goal_created"] = turn["goal_ids"] == []
            checks["production_answer_not_clarify"] = (
                "未完成目标需要按项目查询" not in answer)
            detail["production_mismatches"] = mismatches
            detail["production_truth"] = truth
            detail["production_reported"] = reported
            detail["production_totals"] = totals
            detail["production_answer"] = answer
            detail["production_priorities"] = facts.get("priorities")
            detail["production_unknowns"] = facts.get("unknowns")
        finally:
            _close(handles)

    failed = [name for name, ok in checks.items() if not ok]
    payload = {
        "task": "T74-ui4-portfolio",
        "schema_version": "ui-4-portfolio.v1",
        "result": "PASS" if not failed else "FAIL",
        "checks": checks,
        "failed": failed,
        "detail": detail,
        "environment": {
            "fixture": "isolated throwaway ledger under .poc-state/t74",
            "production": "opened mode=ro + query_only; never written",
            "model": "none (in-process deterministic router)",
            "worker": "none: a portfolio report dispatches nothing",
        },
        "note": ("production numbers are compared with a fresh SQL read at run "
                 "time; no figure from this report is hard-coded in the code or "
                 "the checks"),
    }
    write_evidence(EVIDENCE, payload)
    print(json.dumps({"result": payload["result"], "failed": failed,
                      "checks": len(checks),
                      "evidence": str(EVIDENCE.relative_to(ROOT))},
                     ensure_ascii=False))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
