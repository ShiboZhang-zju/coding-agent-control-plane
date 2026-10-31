"""T50: conversational LD (M4-D) — one owner conversation over the real core.

The M4-D claim under test is deliberately narrow: a human can *talk* to the LD
about projects, goals and knowledge, submit a bounded goal, and be told the
truth about what happened — without the chat layer becoming a second agent, a
second source of truth, or a second approval path.

Sections
--------
A  read intents are rendered from Ledger / Goal / Context rows: project list,
   project status, blockers (including a real rejected WorkItem), goal status,
   task status, knowledge query.
B  ``submit_goal`` creates one bounded goal (a repeat reuses the same row), the
   existing ``LDGoalRunner`` advances it, and the follow-up conversation reports
   the *ledger* verdict — not a chat summary.
C  ``resume_goal`` is staged and never applied without an explicit human
   confirmation token; an unrelated next message supersedes it; ``cancel``
   leaves the goal parked; ``confirm`` resumes it without enlarging the budget.
D  no second truth and no new capability: a chat turn adds no WorkItem, no
   project-context entry and no revision; deleting the conversation does not
   affect progress; re-opening a conversation re-reads the ledger (old chat
   text never becomes current state).
E  the intent schema is a real gate: unknown intents/fields/parameters are
   policy violations (never retried), missing fields are retryable shape
   defects, and the model-free keyword router still answers the four core
   questions.

Deterministic: scripted decision model + a scripted intent classifier + stub
adapter.  Real-endpoint and real-Worker behaviour of the underlying LD is
already covered by T38b/T43/T48; this test verifies the *conversation layer*.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import DispatchResult, TaskState, TaskSnapshot  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.project_context import ContextEntry  # noqa: E402
from controlplane.project_service import ProjectService  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from ld.chat import HeuristicIntentRouter, LDChat  # noqa: E402
from ld.chat_intent import (  # noqa: E402
    ChatIntentKind,
    ChatPolicyError,
    ChatValidationError,
    parse_intent,
)
from ld.chat_store import LDChatStore, normalize_confirmation  # noqa: E402
from ld.coordinator import LDCoordinator  # noqa: E402
from ld.goal_runner import LDGoalRunner  # noqa: E402
from ld.goal_store import LDGoalStore, goal_identity  # noqa: E402
from ld.tools import LDTools  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

FIXTURE = ROOT / "sandbox" / "t50-chat"
REPO = FIXTURE / "repo"
STATE = ROOT / ".poc-state" / "t50"
EVIDENCE = ROOT / "evidence" / "project" / "T50-conversational-ld.json"

GOAL_TEXT = "实现聊天入口的演示功能并让预置测试通过"
SECRET = "T50-LOCAL-ONLY-SECRET"


class StubAdapter:
    name = "stub"

    def dispatch(self, request):  # noqa: ANN001
        return DispatchResult(task_id=request.task_id,
                              worker_job_id=f"job-{request.task_id}",
                              accepted=True)

    def get_task(self, task_id, **_kwargs):  # noqa: ANN001
        return TaskSnapshot(task_id=task_id, worker_job_id=f"job-{task_id}",
                            state=TaskState.RUNNING)

    def pid_alive(self, _pid):  # noqa: ANN001
        return None

    def collect_artifacts(self, _task_id):  # noqa: ANN001
        return []

    def reconcile(self):
        return {"jobs": []}

    def capabilities(self):
        return {}


class ScriptedDecisionModel:
    """Canonical bounded flow: inspect once, submit, wait, finish."""

    def __init__(self):
        self.calls: list[dict] = []

    def decide(self, decision_input: dict) -> dict:
        state = decision_input.get("goal_state") or {}
        project = decision_input.get("requested_project_id")
        self.calls.append({"decision_count": state.get("decision_count")})
        verified = [t["task_id"] for t in state.get("completed_tasks", [])
                    if t.get("verdict") == "verified"]
        if verified:
            return {"action": "finish", "project_id": project,
                    "reason": "the goal task is verified",
                    "evidence_refs": [f"task:{tid}" for tid in verified],
                    "parameters": {"verified_task_ids": verified,
                                   "completion_conditions": []}}
        current = state.get("current_task") or {}
        if current.get("task_id") and current.get("verdict") != "verified":
            return {"action": "wait", "project_id": project,
                    "reason": "wait for the running task",
                    "evidence_refs": [f"task:{current['task_id']}"],
                    "parameters": {"task_id": current["task_id"]}}
        if not state.get("all_goal_tasks"):
            return {"action": "submit_task", "project_id": project,
                    "reason": "implement the goal",
                    "evidence_refs": [],
                    "parameters": {
                        "task": "实现 demo 功能并让预置测试通过",
                        "idempotency_key": "t50-demo",
                        "assertions": [{"kind": "file_exists",
                                        "target": "demo.txt"},
                                       {"kind": "file_contains",
                                        "target": "demo.txt",
                                        "value": "demo"}]}}
        return {"action": "wait", "project_id": project,
                "reason": "task settled; continue", "evidence_refs": [],
                "parameters": {"task_id": state["current_task"]["task_id"]}}


class ScriptedIntentModel:
    """Returns a queued intent per call; records what it was shown."""

    def __init__(self, intents: list[dict]):
        self._queue = list(intents)
        self.inputs: list[dict] = []

    def classify(self, chat_input: dict) -> dict:
        self.inputs.append(chat_input)
        if not self._queue:
            raise RuntimeError("no scripted intent left")
        return self._queue.pop(0)


def git(path: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(path), *args], check=False,
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"git {args} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def make_repo() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)
    REPO.mkdir(parents=True)
    git(REPO, "init", "-q")
    git(REPO, "config", "user.email", "t50@example.invalid")
    git(REPO, "config", "user.name", "T50")
    (REPO / "seed.txt").write_text("seed\n", encoding="utf-8")
    git(REPO, "add", "seed.txt")
    git(REPO, "commit", "-q", "-m", "seed")
    git(REPO, "branch", "-M", "main")


def fresh_ledger(name: str) -> Path:
    STATE.mkdir(parents=True, exist_ok=True)
    ledger = STATE / f"{name}.db"
    for stale in [ledger, *ledger.parent.glob(ledger.name + "*")]:
        stale.unlink(missing_ok=True)
    return ledger


class Harness:
    def __init__(self, ledger: Path, *, intent_model=None):
        self.store = TaskStore(ledger)
        self.plane = ControlPlane(StubAdapter(), self.store,
                                  require_registered_projects=True,
                                  worker_instance_id="wi-t50")
        self.service = ProjectService(self.plane)
        self.project = self.plane.register_project(
            "t50", str(REPO), default_adapter="stub", concurrency_limit=4)
        self.tools = LDTools(self.service)
        self.goals = LDGoalStore(self.store.path, lease_s=30.0)
        self.chats = LDChatStore(self.store.path)
        self.decision_model = ScriptedDecisionModel()
        self.coordinator = LDCoordinator(
            self.tools, self.decision_model,
            model_authorized_security_domains=("local", "shareable"))
        self.runner = LDGoalRunner(
            self.coordinator, self.goals, max_decisions=2, max_tasks=1,
            base_backoff_s=0.2, max_backoff_s=1.0,
            lifetime_max_decisions=8, lifetime_max_tasks=2)
        self.chat = LDChat(self.tools, self.goals, self.chats, intent_model,
                           model_authorized_security_domains=("local", "shareable"))
        self.project_id = self.project["id"]

    def close(self):
        for item in (self.runner, self.goals, self.chats, self.store):
            try:
                item.close()
            except Exception:  # noqa: BLE001
                pass

    # -- helpers -------------------------------------------------------- #
    def add_context(self, kind: str, title: str, body: str, *,
                    verification: str = "verified", tags=()) -> dict:
        return self.service.add_context_entry(self.project_id, ContextEntry(
            project_id=self.project_id, kind=kind, title=title, body=body,
            tags=list(tags), verification=verification,
            security_domain="local", source="human",
            evidence_refs=["evidence/project/T50-conversational-ld.json"]))

    def complete_task(self, task_id: str, verdict: str = "verified") -> None:
        self.store.update(task_id, state=TaskState.SUCCEEDED.value,
                          verdict=verdict, queue_status="complete",
                          finished_at=1_700_000_000.0)

    def project_tasks(self) -> list[dict]:
        return self.store.work_items_for_project(self.project_id)


def count_rows(store: TaskStore, sql: str, args=()) -> int:
    row = store._conn.execute(sql, args).fetchone()  # noqa: SLF001 - test probe
    return int(row[0]) if row else 0


# --------------------------------------------------------------------------- #
# A. read intents
# --------------------------------------------------------------------------- #
def a_read_intents() -> dict:
    h = Harness(fresh_ledger("a"))
    try:
        h.add_context("background", "项目背景", "这是一个演示项目，用于验证对话入口。",
                      tags=["demo"])
        h.add_context("knowledge", "配置读取方式",
                      "配置必须经过 load_config() 读取，不要直接解析原始文件。",
                      tags=["config"])
        h.add_context("constraints", "禁止直连数据库",
                      "任何代码不得绕过服务层直连 SQLite。", tags=["db"])
        rejected = h.plane.enqueue("do a thing that fails", str(REPO),
                                   assertions=[], project_id=h.project_id,
                                   workspace_id=None)
        h.store.update(rejected["task_id"], state=TaskState.SUCCEEDED.value,
                       verdict="rejected", queue_status="complete",
                       warnings=["ASSERTION_FAILED:file_exists:missing.txt"])

        # A real owner keeps one conversation open across questions.
        first = h.chat.handle("现在有哪些项目？")
        conversation_id = first["conversation_id"]
        listed = first["facts"]["projects"]

        def say(text: str) -> dict:
            return h.chat.handle(text, conversation_id=conversation_id)

        status_turn = say("t50 项目进行到哪了？")
        blocked_turn = say("t50 有哪些阻塞？需要我决定什么？")
        knowledge_turn = say(
            "我们之前为什么用 load_config 读取配置？把这条知识找出来。")
        task_turn = say(f"任务 {rejected['task_id']} 怎么样？")
        return {
            "list_projects_answered": len(listed) == 1
            and listed[0]["project_id"] == h.project_id,
            "list_projects_uses_ledger": listed[0]["counts"]["total"] == 1,
            "status_reports_rejected": status_turn["facts"]["counts"]["rejected"] == 1,
            "status_mentions_attention":
                "Needs attention" in status_turn["answer"],
            "blockers_name_rejected_task":
                rejected["task_id"] in blocked_turn["answer"],
            "blockers_flags_present":
                any(item["task_id"] == rejected["task_id"]
                    for item in blocked_turn["facts"]["blocked"]),
            "knowledge_returns_config_entry":
                any("配置" in entry["title"]
                    for entry in knowledge_turn["facts"]["entries"]),
            "knowledge_marks_verification":
                "verified" in knowledge_turn["answer"],
            "task_status_verdict_from_ledger":
                task_turn["facts"]["task"]["verdict"] == "rejected",
            "task_status_warns_state_is_not_truth":
                "not a success claim" in task_turn["answer"].lower(),
            "read_turns_created_no_work_item":
                len(h.project_tasks()) == 1,
            "intent_source_is_heuristic":
                status_turn["intent_source"] == "heuristic_router",
            "conversation_continues": all(
                turn["conversation_id"] == conversation_id
                for turn in (status_turn, blocked_turn, knowledge_turn, task_turn)),
        }
    finally:
        h.close()


# --------------------------------------------------------------------------- #
# B. submit a goal from the conversation, then report the real verdict
# --------------------------------------------------------------------------- #
def b_submit_goal_and_report() -> dict:
    h = Harness(fresh_ledger("b"))
    try:
        first = h.chat.handle(f"继续推进 t50 项目：{GOAL_TEXT}")
        # The chat layer derives the goal identity from the *objective* it extracted
        # from the sentence; the test must use exactly that identity, never invent
        # one, or it would be checking a second source of truth.
        goal_id = first["facts"]["goal_id"]
        goal_ref = goal_identity(GOAL_TEXT.strip(), h.project_id)[1]
        goal_after_chat = h.goals.get(goal_id) or {}
        # A second mention must reuse the same goal row, not mint a second one.
        second = h.chat.handle(f"继续推进 t50 项目：{GOAL_TEXT}")
        goals = h.goals.find_by_project(h.project_id)

        # The existing runner does the advancing; the chat layer never does.
        sweeps = 0
        while sweeps < 8 and (h.goals.get(goal_id) or {}).get("status") not in (
                "completed", "needs_human", "failed"):
            sweeps += 1
            h.runner.advance_due()
            tasks = [t for t in h.project_tasks()
                     if (t.get("metadata") or {}).get("ld_goal_ref") == goal_ref]
            for task in tasks:
                if task.get("verdict") in (None, "pending"):
                    h.complete_task(task["task_id"], verdict="verified")
        goal_final = h.goals.get(goal_id) or {}
        report = h.chat.handle("刚才的任务做得怎么样？t50 的目标状态呢？")
        return {
            "submit_intent_detected":
                first["intent"]["intent"] == "submit_goal",
            "goal_row_created": goal_after_chat.get("status") in ("active", "waiting"),
            "goal_budget_bounded":
                goal_after_chat.get("max_decisions_total") is not None
                or (h.goals.get(goal_id) or {}).get("max_decisions_total") is not None,
            "second_mention_reused_goal": second["facts"]["reused"] is True,
            "single_goal_row": len(goals) == 1,
            "goal_completed_by_runner": goal_final.get("status") == "completed",
            "chat_reports_ledger_verdict":
                any(goal["status"] == "completed"
                    for goal in report["facts"]["goals"]),
            "chat_reports_verified_count":
                any(goal["verified_tasks"] >= 1 for goal in report["facts"]["goals"]),
            "chat_did_not_submit_task_itself":
                report["intent"]["intent"] in (
                    "goal_status", "project_status", "task_status"),
            "sweeps_used": sweeps,
            "model_decisions": len(h.decision_model.calls),
        }
    finally:
        h.close()


# --------------------------------------------------------------------------- #
# C. resume gating
# --------------------------------------------------------------------------- #
def c_resume_requires_confirmation() -> dict:
    h = Harness(fresh_ledger("c"))
    try:
        goal_id, goal_ref = goal_identity(GOAL_TEXT, h.project_id)
        h.goals.get_or_create(goal_id, h.project_id, GOAL_TEXT, goal_ref)
        # Seed the conversation's project binding, exactly like a previous turn
        # that mentioned the project would have done.
        h.chat.handle(f"t50 项目现在有什么目标？")
        h.goals.ensure_lifetime_budget(goal_id, max_decisions=6, max_tasks=2)
        h.goals.stop_goal_if(goal_id, "needs_human",
                             {"stop": "the owner has to choose between options"})
        before = h.goals.get(goal_id) or {}
        # A staged action belongs to *one* conversation (like any real chat):
        # every turn below must continue the same conversation id.
        conversation_id = (h.chat.handle("t50 项目现在有什么目标？")
                           ["conversation_id"])

        def say(text: str) -> dict:
            return h.chat.handle(text, conversation_id=conversation_id)

        staged = say("恢复 t50 那个停下来的目标")
        after_stage = h.goals.get(goal_id) or {}
        # An unrelated sentence must supersede, never silently approve.
        unrelated = say("先不管它，t50 现在有几个任务？")
        after_unrelated = h.goals.get(goal_id) or {}

        staged_again = say("恢复 t50 那个停下来的目标")
        denied = say("取消")
        after_deny = h.goals.get(goal_id) or {}

        staged_third = say("恢复 t50 那个停下来的目标")
        confirmed = say("确认")
        after_confirm = h.goals.get(goal_id) or {}

        # A confirmation token alone, with nothing staged, is not an approval.
        stray = say("confirm")
        after_stray = h.goals.get(goal_id) or {}
        return {
            "staged_not_applied": after_stage.get("status") == "needs_human",
            "stage_requires_confirmation": staged["requires_confirmation"] is True,
            "unrelated_message_supersedes":
                after_unrelated.get("status") == "needs_human"
                and unrelated.get("superseded_pending_action") is not None,
            "cancel_keeps_goal_parked": after_deny.get("status") == "needs_human",
            "cancel_reported": denied["facts"].get("cancelled") is True,
            "confirm_resumes": after_confirm.get("status") == "active",
            "confirm_is_a_history_step":
                int(after_confirm.get("decision_count") or 0)
                == int(before.get("decision_count") or 0) + 1,
            "resume_did_not_enlarge_budget":
                (after_confirm.get("max_decisions_total")
                 == before.get("max_decisions_total")
                 and after_confirm.get("max_tasks_total")
                 == before.get("max_tasks_total")),
            "resume_counted_as_human":
                int(after_confirm.get("resume_count") or 0)
                == int(before.get("resume_count") or 0) + 1,
            "stray_confirm_changes_nothing":
                after_stray.get("status") == after_confirm.get("status")
                and stray["intent"]["intent"] != "resume_goal",
            "three_stages": all(item["requires_confirmation"] for item in
                                (staged, staged_again, staged_third)),
            "confirmation_tokens_exact": (
                normalize_confirmation("confirm") == "confirm"
                and normalize_confirmation("确认。") == "confirm"
                and normalize_confirmation("yes, and also deploy") is None),
        }
    finally:
        h.close()


# --------------------------------------------------------------------------- #
# D. no second truth
# --------------------------------------------------------------------------- #
def d_no_second_truth() -> dict:
    h = Harness(fresh_ledger("d"))
    try:
        h.add_context("knowledge", "既有知识", "这是一条已确认的知识。", tags=["demo"])
        tasks_before = len(h.project_tasks())
        version_before = h.store.context_version(h.project_id)
        entries_before = len(h.service.context.list_entries(h.project_id))
        revisions_before = count_rows(
            h.store, "SELECT COUNT(*) FROM project_context_revisions")

        turn = h.chat.handle("t50 项目进行到哪了？")
        conversation_id = turn["conversation_id"]
        h.chat.handle("t50 有哪些阻塞？")
        knowledge = h.chat.handle("t50 的既有知识是什么？")

        tasks_after = len(h.project_tasks())
        version_after = h.store.context_version(h.project_id)
        entries_after = len(h.service.context.list_entries(h.project_id))
        revisions_after = count_rows(
            h.store, "SELECT COUNT(*) FROM project_context_revisions")

        # A conversation is disposable: dropping it must not change progress.
        progress_before = h.service.get_project_progress(h.project_id)["counts"]
        h.chats._conn.execute(  # noqa: SLF001 - test probe
            "DELETE FROM ld_messages WHERE conversation_id=?", (conversation_id,))
        h.chats._conn.execute(  # noqa: SLF001 - test probe
            "DELETE FROM ld_conversations WHERE conversation_id=?", (conversation_id,))
        h.chats._conn.commit()  # noqa: SLF001
        progress_after = h.service.get_project_progress(h.project_id)["counts"]
        reopened = h.chat.handle("t50 项目进行到哪了？")
        return {
            "chat_added_no_work_item": tasks_after == tasks_before,
            "chat_added_no_context_entry": (entries_after == entries_before
                                            and version_after == version_before),
            "chat_added_no_revision": revisions_after == revisions_before,
            "chat_asked_no_model_to_write":
                knowledge["intent"]["intent"] == "knowledge_query",
            "deleting_conversation_keeps_progress":
                progress_before == progress_after,
            "reopened_reads_ledger":
                reopened["facts"]["project_id"] == h.project_id
                and reopened["conversation_id"] != conversation_id,
            "conversation_separate_from_project_memory": (
                count_rows(h.store,
                           "SELECT COUNT(*) FROM project_context_entries") == 1),
            "model_calls_zero": len(h.decision_model.calls) == 0,
        }
    finally:
        h.close()


# --------------------------------------------------------------------------- #
# E. intent schema gate + model-fed routing
# --------------------------------------------------------------------------- #
def e_intent_gate() -> dict:
    h = Harness(fresh_ledger("e"))
    try:
        base = {"intent": "project_status", "project_id": h.project_id,
                "task_id": None, "goal_id": None, "reason": "asked",
                "parameters": {}}
        results: dict[str, object] = {}
        for name, patch, expected in (
                ("unknown_intent", {"intent": "dispatch_worker"}, ChatPolicyError),
                ("unknown_field", {"sql": "DROP TABLE"}, ChatPolicyError),
                ("forbidden_param", {"parameters": {"approve": True}}, ChatPolicyError),
                ("missing_project", {"project_id": None}, ChatValidationError),
                ("bad_limit", {"intent": "knowledge_query",
                               "parameters": {"limit": 999}}, ChatValidationError),
                ("ok", {}, None)):
            raw = {**base, **patch}
            try:
                intent = parse_intent(raw)
                results[name] = intent.intent.value
            except Exception as exc:  # noqa: BLE001 - expected classification
                results[name] = (type(exc).__name__,
                                 isinstance(exc, expected) if expected else False)

        # A model-fed turn: the classifier's output is validated like any other.
        model = ScriptedIntentModel([
            {"intent": "list_projects", "project_id": None, "task_id": None,
             "goal_id": None, "reason": "owner asked", "parameters": {}},
            {"intent": "approve", "project_id": h.project_id, "task_id": None,
             "goal_id": None, "reason": "trying to approve", "parameters": {}},
        ])
        h.chat = LDChat(h.tools, h.goals, h.chats, model,
                        model_authorized_security_domains=("local", "shareable"))
        model_turn = h.chat.handle("现在有哪些项目？")
        policy_error = None
        try:
            h.chat.handle("把那个审批通过了吧")
        except ChatPolicyError as exc:
            policy_error = str(exc)
        last_input = model.inputs[0] if model.inputs else {}
        statuses_before = len(h.project_tasks())

        router = HeuristicIntentRouter()
        router_input = {"message": "t50 有哪些阻塞？",
                        "projects": [{"project_id": h.project_id, "name": "t50"}],
                        "selected_project_id": h.project_id,
                        "known_task_ids": [], "selected_goal_id": None}
        routed = router.classify(router_input)
        return {
            "unknown_intent_is_policy_error":
                results["unknown_intent"][0] == "ChatPolicyError",
            "unknown_field_is_policy_error":
                results["unknown_field"][0] == "ChatPolicyError",
            "forbidden_param_is_policy_error":
                results["forbidden_param"][0] == "ChatPolicyError",
            "missing_project_is_validation_error":
                results["missing_project"][0] == "ChatValidationError",
            "bad_limit_is_validation_error":
                results["bad_limit"][0] == "ChatValidationError",
            "valid_intent_accepted": results["ok"] == "project_status",
            "model_turn_labelled_model": model_turn["intent_source"] == "model",
            # The *schema* legitimately names the forbidden fields; what must not
            # happen is a frozen prompt / task text actually being sent.
            "model_input_has_no_prompt_content":
                "effective_prompt" not in last_input
                and "# TASK" not in json.dumps(last_input.get("projects"),
                                               ensure_ascii=False)
                and not any("original_task" in json.dumps(turn, ensure_ascii=False)
                            for turn in last_input.get("conversation", {})
                            .get("recent_turns", [])),
            "model_input_declares_rules":
                bool(last_input.get("rules") and last_input.get("intent_schema")),
            "forbidden_intent_refused": policy_error is not None,
            "refused_turn_created_nothing":
                len(h.project_tasks()) == statuses_before,
            "heuristic_router_blockers":
                routed["intent"] == "explain_blockers",
        }
    finally:
        h.close()


def main() -> int:
    make_repo()
    checks: dict[str, object] = {}
    for name, fn in (("A_read_intents", a_read_intents),
                     ("B_submit_goal", b_submit_goal_and_report),
                     ("C_resume_gating", c_resume_requires_confirmation),
                     ("D_no_second_truth", d_no_second_truth),
                     ("E_intent_gate", e_intent_gate)):
        try:
            checks[name] = fn()
        except Exception as exc:  # noqa: BLE001 - a section failure is evidence
            checks[name] = {"section_error": f"{type(exc).__name__}: {exc}"}

    failed: list[str] = []
    for section, payload in checks.items():
        if "section_error" in payload:
            # A section that raised proves nothing: it must never read as a pass.
            failed.append(f"{section}.section_error")
            continue
        for key, value in payload.items():
            if isinstance(value, bool) and not value:
                failed.append(f"{section}.{key}")
    evidence = {
        "test": "T50 conversational LD (M4-D)",
        "fixture": "<REPO>/sandbox/t50-chat",
        "note": ("Deterministic: scripted intent classifier + scripted decision "
                 "model + stub adapter. Real endpoint / real Worker behaviour of "
                 "the LD itself is covered by T38b/T43/T48."),
        "checks": checks,
        "result": "PASS" if not failed else "FAIL",
        "failed": failed,
    }
    write_evidence(EVIDENCE, evidence)
    print(json.dumps({"result": evidence["result"], "failed": failed},
                     ensure_ascii=False))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
