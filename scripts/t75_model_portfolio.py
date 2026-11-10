"""T75: model-console cross-project read acceptance (independent Ledger + real model).

Two distinct things are measured and never conflated:

1. **The console path.** A real dashboard server, a real deployed intent model,
   and an independent test Ledger.  The six cross-project questions must answer
   ``portfolio_status`` with numbers read from that Ledger.  The deterministic
   pre-route means these turns make ZERO model calls; ``intent_source`` stays
   honest (``heuristic_router``), never ``model``.

2. **The prompt contract.** The same real model is asked to classify the same six
   sentences directly.  Its raw output is validated with ``parse_intent`` — this
   is what proves the ``_CHAT_SYSTEM_PROMPT`` fix rather than merely proving the
   fast path works.

Negative cases, injected bad model output and a SQL snapshot of Goal / WorkItem /
approval / resume state are recorded too.  No production service is touched, no
endpoint, model id or key is written to evidence.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

from adapters.base import ArtifactAssertion                    # noqa: E402
from controlplane.plane import ControlPlane                     # noqa: E402
from controlplane.project_service import ProjectService        # noqa: E402
from controlplane.store import TaskStore                        # noqa: E402
from dashboard.api import make_server                           # noqa: E402
from dashboard.chat import ChatBridge                           # noqa: E402
from ld.chat_intent import PORTFOLIO_FOCUS, chat_intent_schema, parse_intent  # noqa: E402
from ld.chat_store import LDChatStore                           # noqa: E402
from ld.goal_store import LDGoalStore, goal_identity            # noqa: E402
from ld.model import OpenAIChatIntentModel                      # noqa: E402
from ld.tools import LDTools                                    # noqa: E402
from sanitize_evidence import write_evidence                    # noqa: E402

EVIDENCE = ROOT / "evidence" / "project" / "T75-model-portfolio.json"
STATE = ROOT / ".poc-state" / "t75"
MODEL_ID = "DeepSeek-V4.1-Flash-1017-W8A8-P800-1028test"
MODELS_JSON = Path(os.environ["USERPROFILE"]) / ".codebuddy" / "models.json"
TOKEN = "t75-model-portfolio-operator-token-32"

SIX_QUESTIONS = (
    "汇总所有项目目前的进度",
    "所有项目有哪些未完成目标？",
    "两个项目各自有哪些需要我处理的？",
    "现在所有项目加起来有哪些未完成目标？",
    "给出各项目的优先级建议",
    "汇总所有项目的阻塞、异常验收和待人工 Goal",
)
SNAPSHOT_TABLES = (
    "projects", "workspaces", "work_items", "attempts", "ld_goals",
    "ld_goal_decisions", "approvals", "ld_goal_runner",
)


class _Adapter:
    name = "stub"


class CountingModel:
    """Real model + an exact call counter (never a stub)."""

    def __init__(self, inner):
        self.inner = inner
        self.calls = 0

    def classify(self, payload):
        self.calls += 1
        return self.inner.classify(payload)

    def describe(self):
        return self.inner.describe()


class _FakeModel:
    """Injected raw output, used only for the fail-closed cases."""

    def __init__(self, response=None, raises=None):
        self.response = response
        self.raises = raises
        self.calls = 0

    def classify(self, _payload):
        self.calls += 1
        if self.raises:
            raise self.raises
        return self.response


def _raw(intent="portfolio_status", *, parameters=None, project_id=None):
    return {"intent": intent, "project_id": project_id, "task_id": None,
            "goal_id": None, "reason": "injected classification",
            "parameters": {} if parameters is None else dict(parameters)}


def real_model_config() -> dict:
    cfg = json.loads(MODELS_JSON.read_text(encoding="utf-8-sig"))
    for item in cfg.get("models") or []:
        if item.get("id") == MODEL_ID:
            return {"base_url": item["url"], "model": item["id"],
                    "api_key": item.get("apiKey") or None}
    raise RuntimeError("configured deployment model not found in models.json")


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True,
                   capture_output=True)
    return path


def build_ledger() -> tuple[Path, dict]:
    root = STATE / uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    ledger = root / "t75.db"
    store = TaskStore(ledger)
    plane = ControlPlane(_Adapter(), store, require_registered_projects=True)
    alpha = plane.register_project("ACP-Alpha", str(_git_repo(root / "alpha")),
                                   default_adapter="stub")
    beta = plane.register_project("ACP-Beta", str(_git_repo(root / "beta")),
                                  default_adapter="stub")
    rejected = plane.enqueue(
        "alpha rejected", str(root / "alpha"),
        assertions=[ArtifactAssertion("file_exists", "a.md")],
        project_id=alpha["id"])["task_id"]
    blocked = plane.enqueue(
        "beta inconclusive blocked", str(root / "beta"),
        assertions=[ArtifactAssertion("file_exists", "b.md")],
        project_id=beta["id"])["task_id"]
    verified = plane.enqueue(
        "beta verified", str(root / "beta"),
        assertions=[ArtifactAssertion("file_exists", "v.md")],
        project_id=beta["id"])["task_id"]
    store.update(rejected, state="succeeded", verdict="rejected",
                 queue_status="complete")
    store.update(blocked, state="unknown", verdict="inconclusive",
                 queue_status="needs_human")
    store.update(verified, state="succeeded", verdict="verified",
                 queue_status="complete")
    store.close()

    goals = LDGoalStore(ledger)
    text = "alpha paused goal"
    goal_id, ref = goal_identity(text, alpha["id"])
    goals.get_or_create(goal_id, alpha["id"], text, ref)
    goals.update_goal(goal_id, status="needs_human",
                      last_decision={"stop": "human review required"})
    goals.close()
    chats = LDChatStore(ledger)
    chats.close()
    return ledger, {"projects": (alpha, beta), "work_items": 3,
                    "verified": 1, "rejected": 1, "inconclusive": 1,
                    "blocked": 1, "needs_human_goals": 1}


def snapshot(ledger: Path) -> dict:
    con = sqlite3.connect("file:" + str(ledger).replace("\\", "/")
                          + "?mode=ro", uri=True)
    con.execute("PRAGMA query_only=1")
    try:
        out = {}
        for table in SNAPSHOT_TABLES:
            try:
                cur = con.execute("SELECT * FROM %s" % table)
            except sqlite3.Error as exc:
                out[table] = "ERR:%s" % exc
                continue
            rows = [tuple(str(v) for v in r) for r in cur.fetchall()]
            blob = json.dumps(rows, ensure_ascii=False, sort_keys=True)
            out[table] = {"rows": len(rows), "sha256": hashlib.sha256(
                blob.encode("utf-8")).hexdigest()}
        return out
    finally:
        con.close()


class Server:
    def __init__(self, ledger: Path, model):
        self.bridge = ChatBridge(ledger, intent_model=model,
                                 model_domains="local,shareable")
        self.server = make_server(ledger, host="127.0.0.1", port=0,
                                  enable_chat=True, chat_token=TOKEN,
                                  chat_bridge=self.bridge)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.02},
                                       daemon=True)
        self.thread.start()

    def say(self, message, *, conversation_id=None, project_id=None):
        body = json.dumps({"conversation_id": conversation_id,
                           "project_id": project_id, "message": message},
                          ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            "http://127.0.0.1:%d/api/chat/messages" % self.port,
            data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Accept": "application/json",
                     "Origin": "http://127.0.0.1:%d" % self.port,
                     "Authorization": "Bearer " + TOKEN})
        try:
            with urllib.request.urlopen(req, timeout=240) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8", "replace"))

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def model_input_for(question: str, projects) -> dict:
    """The same classification payload shape LDChat._classify builds."""
    return {
        "schema_version": "m4-d.v1",
        "message": question,
        "conversation": {
            "conversation_id": "t75-probe", "selected_project_id": None,
            "selected_goal_id": None,
            "project_routing": {"source": "none", "project_id": None,
                                "candidates": []},
            "conversation_is_global": True, "recent_turns": [],
        },
        "projects": [{"project_id": p["id"], "active": True}
                     for p in projects],
        "goals": [], "known_task_ids": [],
        "security": {"authorized_context_domains": ["local", "shareable"],
                     "project_names_included": True,
                     "message_withheld": False,
                     "message_withheld_reason": None},
        "rules": [
            "Return exactly one structured intent; never return code or commands.",
            "You cannot approve, deny, write project knowledge, or dispatch work.",
            "Use clarify when the request is ambiguous or names no project.",
        ],
        "intent_schema": chat_intent_schema(),
    }


def main() -> int:
    checks: dict[str, bool] = {}
    detail: dict = {}
    ledger, truth = build_ledger()
    before = snapshot(ledger)
    cfg = real_model_config()
    real = OpenAIChatIntentModel(
        base_url=cfg["base_url"], model=cfg["model"],
        api_key=cfg["api_key"], timeout_s=180)
    counted = CountingModel(real)
    console = Server(ledger, counted)

    try:
        # -- 1. six questions through the real console --------------------- #
        six = []
        conversation_id = None
        for question in SIX_QUESTIONS:
            calls_before = counted.calls
            status, body = console.say(question, conversation_id=conversation_id)
            conversation_id = body.get("conversation_id") or conversation_id
            portfolio = body.get("portfolio") or {}
            totals = portfolio.get("totals") or {}
            six.append({
                "question": question, "http_status": status,
                "intent": (body.get("intent") or {}).get("intent"),
                "intent_source": body.get("intent_source"),
                "model_calls": counted.calls - calls_before,
                "goal_ids": body.get("goal_ids"),
                "requires_confirmation": body.get("requires_confirmation"),
                "totals": {k: totals.get(k) for k in
                           ("work_items", "verified", "rejected",
                            "inconclusive", "blocked",
                            "abnormal_verdict", "needs_human_goals")},
                "projects_in_answer": [p["id"] in (body.get("answer") or "")
                                       for p in truth["projects"]],
                "answer_head": (body.get("answer") or "")[:160],
            })
        detail["six_questions"] = six
        checks["six_http_200"] = all(r["http_status"] == 200 for r in six)
        checks["six_all_portfolio_status"] = all(
            r["intent"] == "portfolio_status" for r in six)
        checks["six_intent_source_is_honest"] = all(
            r["intent_source"] == "heuristic_router" for r in six)
        checks["six_no_model_calls"] = all(
            r["model_calls"] == 0 for r in six)
        checks["six_numbers_match_ledger"] = all(
            r["totals"]["work_items"] == truth["work_items"]
            and r["totals"]["verified"] == truth["verified"]
            and r["totals"]["rejected"] == truth["rejected"]
            and r["totals"]["inconclusive"] == truth["inconclusive"]
            and r["totals"]["blocked"] == truth["blocked"]
            and r["totals"]["needs_human_goals"] == truth["needs_human_goals"]
            for r in six)
        checks["six_report_every_project"] = all(
            all(r["projects_in_answer"]) for r in six)
        checks["six_create_nothing"] = all(
            not r["goal_ids"] and not r["requires_confirmation"] for r in six)

        # -- 2. real model's raw classification of the same six ----------- #
        probe = []
        for question in SIX_QUESTIONS:
            entry: dict = {"question": question}
            try:
                raw = real.classify(model_input_for(question, truth["projects"]))
                params = raw.get("parameters") if isinstance(raw, dict) else None
                focus = (params or {}).get("focus")
                entry.update({"raw_intent": raw.get("intent")
                              if isinstance(raw, dict) else None,
                              "focus_present": isinstance(params, dict)
                              and "focus" in params,
                              "focus": focus})
                parsed = parse_intent(raw)
                entry.update({"parse_ok": True,
                              "parsed_intent": parsed.intent.value,
                              "focus_in_closed_set": focus in PORTFOLIO_FOCUS
                              if focus is not None else None})
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                entry.update({"parse_ok": False,
                              "error": "%s: %s" % (type(exc).__name__,
                                                   str(exc)[:200])})
            probe.append(entry)
        detail["model_direct_probe"] = probe
        detail["model_describe"] = real.describe()
        detail["model_direct_calls"] = len(SIX_QUESTIONS)
        checks["model_never_emits_empty_string_focus"] = all(
            item.get("focus") != "" for item in probe)
        checks["model_direct_output_all_parses"] = all(
            item.get("parse_ok") is True for item in probe)
        checks["model_direct_all_portfolio"] = all(
            item.get("parsed_intent") == "portfolio_status" for item in probe)
        checks["model_direct_focus_is_closed_set"] = all(
            item.get("focus_in_closed_set") in (True, None) for item in probe)

        # -- 3. negative cases through the real model --------------------- #
        negative = []
        # ``goal_ids`` on a status read are *references to existing rows*, not
        # creations, so the only sound "created nothing" check is a SQL
        # snapshot around the turns (see the earlier T71 lesson).
        before_negative = snapshot(ledger)
        for question, expect in (
                ("现在有哪些项目？", "list_projects"),
                ("汇总 ACP-Alpha 目前的进度", "not_portfolio"),
                ("汇总所有项目目前的进度，并提交一个新目标", "not_portfolio")):
            calls_before = counted.calls
            status, body = console.say(question)
            negative.append({
                "question": question, "http_status": status,
                "intent": (body.get("intent") or {}).get("intent"),
                "intent_source": body.get("intent_source"),
                "model_calls": counted.calls - calls_before,
                "portfolio_present": bool(body.get("portfolio")),
                "goal_ids": body.get("goal_ids"),
                "expect": expect})
        detail["negative"] = negative
        detail["negative_snapshot_unchanged"] = (
            before_negative == snapshot(ledger))
        checks["registry_question_is_list_projects"] = (
            negative[0]["intent"] == "list_projects"
            and negative[0]["intent_source"] == "model")
        checks["negative_questions_use_model"] = all(
            row["model_calls"] == 1 for row in negative)
        checks["single_project_not_widened"] = (
            negative[1]["intent"] != "portfolio_status"
            and not negative[1]["portfolio_present"])
        checks["mixed_write_not_widened"] = (
            negative[2]["intent"] != "portfolio_status"
            and not negative[2]["portfolio_present"])
        checks["negative_create_nothing"] = detail[
            "negative_snapshot_unchanged"]
    finally:
        console.close()

    # -- 4. injected model output must still fail closed ------------------ #
    injections = []
    for name, model in (
            ("focus_empty", _FakeModel(_raw(parameters={"focus": ""}))),
            ("focus_illegal", _FakeModel(_raw(parameters={"focus": "everything"}))),
            ("model_unavailable", _FakeModel(raises=RuntimeError("down")))):
        bridge = ChatBridge(ledger, intent_model=model,
                            model_domains="local")
        try:
            # A single-project status question: never eligible for the fast path.
            before_turn = snapshot(ledger)
            try:
                turn = bridge.handle(message="ACP-Alpha 当前状态？",
                                     conversation_id=None, project_id=None)
                outcome = {"intent": (turn.get("intent") or {}).get("intent"),
                           "intent_source": turn.get("intent_source"),
                           "goal_ids": turn.get("goal_ids")}
                error = None
            except Exception as exc:  # noqa: BLE001 - fail-closed is the point
                outcome = {"intent": None, "intent_source": None,
                           "goal_ids": []}
                error = "%s: %s" % (type(exc).__name__, str(exc)[:200])
            injections.append({
                "case": name, "model_calls": model.calls,
                "ledger_unchanged": snapshot(ledger) == before_turn,
                "error": error, **outcome})
        finally:
            pass
    detail["injections"] = injections
    checks["injection_empty_focus_not_executed"] = (
        injections[0]["intent"] != "portfolio_status"
        and injections[0]["intent_source"] == "invalid_model_intent")
    checks["injection_illegal_focus_rejected"] = (
        injections[1]["intent"] != "portfolio_status"
        and injections[1]["error"] is not None)
    checks["injection_unavailable_not_executed"] = (
        injections[2]["intent_source"] == "model_unavailable")
    checks["injections_do_not_write"] = all(
        row["ledger_unchanged"] and not row["goal_ids"] for row in injections)

    # -- 5. existing write gates ------------------------------------------ #
    staged_model = _FakeModel()
    bridge = ChatBridge(ledger, intent_model=staged_model,
                        model_domains="local")
    # Give the injected submit_goal a legal project id from this Ledger.
    alpha = truth["projects"][0]["id"]
    beta = truth["projects"][1]["id"]
    staged_model.response = _raw("submit_goal", project_id=alpha,
                                 parameters={"goal": "实现 greet.py"})
    staged = bridge.handle(message="继续推进 ACP-Alpha：实现 greet.py",
                           conversation_id=None, project_id=None)
    cid = staged.get("conversation_id")
    mismatch = bridge.handle(message="confirm", conversation_id=cid,
                             project_id=beta)
    confirmed = bridge.handle(message="confirm", conversation_id=cid,
                              project_id=None)
    detail["write_gates"] = {
        "staged_requires_confirmation": staged.get("requires_confirmation"),
        "staged_goal_ids": staged.get("goal_ids"),
        "mismatch_intent_source": mismatch.get("intent_source"),
        "mismatch_goal_ids": mismatch.get("goal_ids"),
        "confirmed_intent_source": confirmed.get("intent_source"),
        "confirmed_goal_ids": confirmed.get("goal_ids"),
    }
    checks["global_submit_goal_still_staged"] = (
        staged.get("requires_confirmation") is True
        and not staged.get("goal_ids"))
    checks["cross_project_confirmation_rejected"] = (
        mismatch.get("intent_source") == "human_confirmation_project_mismatch"
        and not mismatch.get("goal_ids"))
    checks["confirmation_still_creates_one_goal"] = (
        confirmed.get("intent_source") == "human_confirmation"
        and len(confirmed.get("goal_ids") or []) == 1)

    after = snapshot(ledger)
    detail["ledger_snapshot_before"] = before
    detail["ledger_snapshot_after"] = after
    detail["ledger_truth"] = {k: v for k, v in truth.items()
                              if k != "projects"}
    # The confirmation intentionally creates one Goal; compare everything else.
    changed = [t for t in before if before[t] != after[t]]
    detail["changed_tables"] = changed
    checks["only_goal_table_changed_by_confirmation"] = (
        changed in (["ld_goals"], []))
    checks["workitems_approvals_resume_unchanged"] = all(
        before[t] == after[t] for t in
        ("work_items", "approvals", "attempts", "ld_goal_decisions"))

    failed = sorted(k for k, v in checks.items() if not v)
    payload = {
        "task": "T75-model-portfolio",
        "schema_version": "ui4-model-portfolio.v1",
        "result": "PASS" if not failed else "FAIL",
        "checks": checks, "failed": failed, "detail": detail,
        "environment": {
            "ledger": "independent throwaway Ledger under .poc-state/t75",
            "model": "the deployed loopback intent model (config read locally)",
            "model_call_shape": real.describe(),
            "console": "real dashboard HTTP chat surface on a free loopback port",
            "production": "not contacted, not modified",
        },
    }
    write_evidence(EVIDENCE, payload)
    print(json.dumps({
        "result": payload["result"], "checks": len(checks), "failed": failed,
        "six_questions": [{k: r[k] for k in
                           ("question", "intent", "intent_source",
                            "model_calls", "totals")} for r in six],
        "model_direct_probe": probe,
        "model_direct_calls": len(SIX_QUESTIONS),
        "negative": negative, "injections": injections,
        "evidence": str(EVIDENCE.relative_to(ROOT)),
    }, ensure_ascii=False, indent=2))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
