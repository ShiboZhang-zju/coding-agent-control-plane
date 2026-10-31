"""T51: intent safety — a question must never become a goal (M4-D Safety Closure).

The M4-D audit of ``2d478ae`` found that ``HeuristicIntentRouter`` tested its
``_SUBMIT`` verbs *before* any question/status branch, so a turn such as
"这个项目的任务完成了吗？" — containing 完成 — was classified ``submit_goal``
and, with a project already selected, immediately created a Goal that the
Runtime would then advance.  The same gap existed for the model path: a model
``submit_goal`` was applied with no deterministic second opinion.

Sections
--------
A  interrogatives never submit: question forms of every submit verb, explicit
   references/quotes of a verb, and negated requests all stay read-only, and the
   ledger gains no goal.
B  a model ``submit_goal`` that the deterministic router does not corroborate is
   staged, never applied; a corroborated one still submits immediately.
C  an unregistered or foreign project never yields a Goal (no implicit create).
D  repeating a goal never changes its lifetime budget, and a chat intent that
   carries budget parameters is a policy violation (fail-closed).
E  the router still does the right thing on genuine imperatives, so the safety
   fix is not "refuse everything".

Deterministic: scripted intent models + stub adapter.
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
from controlplane.project_service import ProjectService  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from ld.chat import HeuristicIntentRouter, LDChat  # noqa: E402
from ld.chat_intent import (  # noqa: E402
    ChatPolicyError,
    ChatValidationError,
    parse_intent,
)
from ld.chat_store import LDChatStore  # noqa: E402
from ld.coordinator import LDCoordinator  # noqa: E402
from ld.goal_store import LDGoalStore, goal_identity  # noqa: E402
from ld.tools import LDTools  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

FIXTURE = ROOT / "sandbox" / "t51-intent-safety"
REPO = FIXTURE / "repo"
STATE = ROOT / ".poc-state" / "t51"
EVIDENCE = ROOT / "evidence" / "project" / "T51-intent-safety.json"

#: Every one of these contains a submit verb but is a question about state.
QUESTIONS = (
    "这个项目的任务完成了吗？",
    "t51 项目实现了哪些功能？",
    "t51 现在的进展怎么样？",
    "t51 是不是已经完成了？",
    "t51 还有哪些任务没有实现？",
    "t51 项目现在什么状态？",
    "我刚才说的“实现”是什么意思？",
    "谁负责推进 t51 的下一步？",
    "t51 为什么还没完成？",
)
#: Negated, prohibited, deferred or conditional instructions.  Second re-audit
#: P0-2: the previous proximity window (6 chars after a negation) let
#: "先不要在未审批通过前继续推进 t51 项目" through, because 继续推进 sat further
#: away from 不要.  Widening the window only moves the counterexample, so the
#: router now refuses *any* directive that carries a negation/condition, and
#: asks instead of submitting.  Everything below must therefore stay read-only.
NEGATIONS = (
    "先不要推进 t51 项目",
    "别实现新功能，t51 现在什么状态？",
    "先不要在未审批通过前继续推进 t51 项目。",
    "暂缓推进 t51 项目",
    "请勿继续推进 t51 项目",
    "禁止推进 t51 项目",
    "严禁实现新功能",
    "不得推进 t51",
    "搁置 t51 的推进计划",
    "如果测试还没过就继续推进 t51 项目",
    "在未审批通过前继续推进 t51",
    "待审批通过后再推进 t51 项目",
)


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


class FrozenModel:
    """Returns a fixed intent for matching messages, else the keyword router.

    Scoping matters: a model that answered *every* turn with ``submit_goal``
    would also answer the harness's own setup question, which would make the
    section prove nothing about the sentence under test.
    """

    def __init__(self, intent: dict | None = None, *, match: str | None = None):
        self._intent = intent or {}
        self._match = match
        self.inputs: list[dict] = []

    def classify(self, chat_input: dict) -> dict:
        self.inputs.append(chat_input)
        message = str(chat_input.get("message") or "")
        if self._match and self._match not in message:
            return HeuristicIntentRouter().classify(chat_input)
        intent = dict(self._intent)
        # A real classifier sees the conversation's selected project; mimic that
        # so the section tests routing rather than a missing-field defect.
        if intent.get("project_id") is None:
            intent["project_id"] = ((chat_input.get("conversation") or {})
                                    .get("selected_project_id"))
        return intent


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
    git(REPO, "config", "user.email", "t51@example.invalid")
    git(REPO, "config", "user.name", "T51")
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
    def __init__(self, ledger: Path, *, intent_model=None,
                 domains=("local", "shareable")):
        self.store = TaskStore(ledger)
        self.plane = ControlPlane(StubAdapter(), self.store,
                                  require_registered_projects=True,
                                  worker_instance_id="wi-t51")
        self.service = ProjectService(self.plane)
        self.project = self.plane.register_project(
            "t51", str(REPO), default_adapter="stub", concurrency_limit=2)
        self.project_id = self.project["id"]
        self.tools = LDTools(self.service)
        self.goals = LDGoalStore(self.store.path, lease_s=30.0)
        self.chats = LDChatStore(self.store.path)
        self.model = intent_model
        self.coordinator = LDCoordinator(
            self.tools, None, model_authorized_security_domains=tuple(domains))
        self.chat = LDChat(self.tools, self.goals, self.chats, intent_model,
                           model_authorized_security_domains=tuple(domains))
        self.conversation_id = self.chat.handle(
            "t51 项目有哪些目标？")["conversation_id"]

    def say(self, message: str) -> dict:
        return self.chat.handle(message, conversation_id=self.conversation_id)

    def goals_for_project(self, project_id: str | None = None) -> list[dict]:
        return self.goals.find_by_project(project_id or self.project_id)

    def close(self):
        for item in (self.goals, self.chats, self.store):
            try:
                item.close()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- #
# A. interrogatives and negations never submit
# --------------------------------------------------------------------------- #
def a_questions_never_submit() -> dict:
    h = Harness(fresh_ledger("a"))
    try:
        router = HeuristicIntentRouter()
        projects = [{"project_id": h.project_id, "name": "t51"}]
        base = {"projects": projects, "conversation": {
            "selected_project_id": h.project_id, "selected_goal_id": None}}
        results = []
        for text in (*QUESTIONS, *NEGATIONS):
            intent = router.classify({**base, "message": text})["intent"]
            results.append((text, intent))

        # And through the real turn path: no Goal may appear.
        goals_before = len(h.goals_for_project())
        for text in QUESTIONS:
            h.say(text)
        goals_after_questions = len(h.goals_for_project())
        for text in NEGATIONS:
            h.say(text)
        goals_after_negations = len(h.goals_for_project())
        return {
            "router_never_submits_on_questions":
                all(intent != "submit_goal" for _t, intent in results),
            "router_never_submits_on_negations":
                all(router.classify({**base, "message": text})["intent"]
                    != "submit_goal" for text in NEGATIONS),
            "questions_answered_as_reads":
                all(intent in ("project_status", "goal_status", "explain_blockers",
                               "task_status", "knowledge_query", "list_projects",
                               "clarify")
                    for _t, intent in results),
            "no_goal_created_by_questions": goals_after_questions == goals_before,
            "no_goal_created_by_negations":
                goals_after_negations == goals_before,
            # A negated/conditional *instruction* is not a status question
            # either: answering "here is the project status" would misread it,
            # so the router must ask which was meant.
            "negations_are_asked_about_not_answered_sideways":
                all(router.classify({**base, "message": text})["intent"]
                    in ("clarify", "goal_status", "project_status")
                    for text in NEGATIONS),
            "negations_containing_a_submit_verb_are_clarified":
                all(router.classify({**base, "message": text})["intent"]
                    == "clarify"
                    for text in NEGATIONS
                    if any(v in text.lower()
                           for v in HeuristicIntentRouter._SUBMIT)
                    and not router.is_question(text)),
            "routed_intents": {text: intent for text, intent in results},
        }
    finally:
        h.close()


# --------------------------------------------------------------------------- #
# B. a model submit_goal needs deterministic corroboration
# --------------------------------------------------------------------------- #
def b_model_submit_needs_corroboration() -> dict:
    question_intent = {
        "intent": "submit_goal", "project_id": None, "task_id": None,
        "goal_id": None, "reason": "looks like a request",
        "parameters": {"goal": "把 t51 完成"},
    }
    h = Harness(fresh_ledger("b"),
                intent_model=FrozenModel(question_intent, match="完成了吗"))
    try:
        # The model claims submit_goal for a question: it must be staged only.
        staged = h.say("这个项目的任务完成了吗？")
        goals_after_stage = h.goals_for_project()
        pending = staged.get("pending_action") or {}
        confirmed = h.say("确认")
        goals_after_confirm = h.goals_for_project()
        return {
            "model_submit_on_question_is_staged":
                staged.get("requires_confirmation") is True
                and pending.get("kind") == "submit_goal",
            "no_goal_before_confirmation": len(goals_after_stage) == 0,
            "confirmation_creates_exactly_one_goal": len(goals_after_confirm) == 1,
            "confirmed_goal_matches_objective":
                (goals_after_confirm[0].get("goal_text") or "").strip()
                == "把 t51 完成",
            "answer_discloses_staging":
                "confirm" in (staged.get("answer") or "").lower(),
            "intent_source_is_model": staged.get("intent_source") == "model",
            "confirmation_source":
                confirmed.get("intent_source"),
        }
    finally:
        h.close()


def b2_corroborated_submit_is_immediate() -> dict:
    intent = {
        "intent": "submit_goal", "project_id": None, "task_id": None,
        "goal_id": None, "reason": "owner asked to advance",
        "parameters": {"goal": "实现 t51 的演示功能"},
    }
    h = Harness(fresh_ledger("b2"),
                intent_model=FrozenModel(intent, match="继续推进"))
    try:
        turn = h.say("继续推进 t51 项目：实现 t51 的演示功能")
        goals = h.goals_for_project()
        return {
            "corroborated_submit_is_immediate":
                turn.get("requires_confirmation") is False and len(goals) == 1,
            "goal_text_is_the_objective":
                (goals[0].get("goal_text") or "").strip() == "实现 t51 的演示功能",
            "model_origin_still_labelled": turn.get("intent_source") == "model",
        }
    finally:
        h.close()


# --------------------------------------------------------------------------- #
# C. unknown / foreign project never yields a goal
# --------------------------------------------------------------------------- #
def c_unregistered_project_refused() -> dict:
    intent = {
        "intent": "submit_goal", "project_id": "prj-does-not-exist",
        "task_id": None, "goal_id": None,
        "reason": "invented a project", "parameters": {"goal": "做点什么"},
    }
    h = Harness(fresh_ledger("c"),
                intent_model=FrozenModel(intent, match="不存在的项目"))
    try:
        refused = None
        try:
            h.say("继续推进不存在的项目：做点什么")
        except ChatPolicyError as exc:
            refused = str(exc)
        all_goals = h.goals.all_goals()

        other_root = FIXTURE / "repo-other"
        if not other_root.exists():
            other_root.mkdir(parents=True)
            git(other_root, "init", "-q")
            git(other_root, "config", "user.email", "t51@example.invalid")
            git(other_root, "config", "user.name", "T51")
            (other_root / "seed.txt").write_text("seed\n", encoding="utf-8")
            git(other_root, "add", "seed.txt")
            git(other_root, "commit", "-q", "-m", "seed")
            git(other_root, "branch", "-M", "main")
        other = h.plane.register_project("t51-other", str(other_root),
                                         default_adapter="stub",
                                         concurrency_limit=2)
        # A goal that belongs to a *different* registered project must not be
        # resumable through a conversation bound to this one.
        foreign_goal_id, foreign_ref = goal_identity("别的项目目标", other["id"])
        h.goals.get_or_create(foreign_goal_id, other["id"], "别的项目目标",
                              foreign_ref)
        h.goals.stop_goal_if(foreign_goal_id, "needs_human",
                             {"stop": "foreign goal paused"})
        # The dangerous case is a classifier naming another project's goal while
        # the conversation is bound to this project: it must be refused, not
        # silently resumed.
        cross = {"intent": "resume_goal", "project_id": h.project_id,
                 "task_id": None, "goal_id": foreign_goal_id,
                 "reason": "cross-project resume", "parameters": {}}
        h.chat = LDChat(h.tools, h.goals, h.chats,
                        FrozenModel(cross, match="恢复"),
                        model_authorized_security_domains=("local", "shareable"))
        h.conversation_id = h.chat.handle("t51 项目有哪些目标？")["conversation_id"]
        refused_foreign = None
        try:
            h.chat.handle("恢复 t51 那个停下来的目标",
                          conversation_id=h.conversation_id)
        except ChatPolicyError as exc:
            refused_foreign = str(exc)
        foreign_after = h.goals.get(foreign_goal_id) or {}
        return {
            "unknown_project_is_policy_error": refused is not None,
            "unknown_project_creates_no_goal": len(all_goals) == 0,
            "foreign_goal_resume_refused": refused_foreign is not None,
            "foreign_goal_still_parked":
                foreign_after.get("status") == "needs_human",
            "error_message_names_the_project":
                refused is not None and "prj-does-not-exist" in refused,
        }
    finally:
        h.close()


# --------------------------------------------------------------------------- #
# D. budgets are never touched from the chat path
# --------------------------------------------------------------------------- #
def d_chat_never_changes_budget() -> dict:
    h = Harness(fresh_ledger("d"))
    try:
        first = h.say("继续推进 t51 项目：实现预算测试")
        goal_id = first["facts"]["goal_id"]
        h.goals.ensure_lifetime_budget(goal_id, max_decisions=4, max_tasks=2)
        before = h.goals.get(goal_id) or {}

        # Repeating the same objective must reuse the row and keep the budget.
        again = h.say("继续推进 t51 项目：实现预算测试")
        after = h.goals.get(goal_id) or {}

        # A chat intent carrying budget parameters is a schema-level violation.
        budget_intent = {
            "intent": "submit_goal", "project_id": h.project_id,
            "task_id": None, "goal_id": None, "reason": "trying to enlarge",
            "parameters": {"goal": "实现预算测试", "max_decisions": 50},
        }
        h.chat = LDChat(
            h.tools, h.goals, h.chats,
            FrozenModel(budget_intent, match="继续推进"),
            model_authorized_security_domains=("local", "shareable"))
        h.conversation_id = h.chat.handle("t51 项目有哪些目标？")["conversation_id"]
        policy_error = None
        try:
            h.chat.handle("继续推进 t51 项目：实现预算测试",
                          conversation_id=h.conversation_id)
        except ChatPolicyError as exc:
            policy_error = str(exc)
        after_attempt = h.goals.get(goal_id) or {}
        return {
            "repeat_reuses_goal": again["facts"]["reused"] is True,
            "repeat_keeps_budget":
                after.get("max_decisions_total") == before.get("max_decisions_total")
                and after.get("max_tasks_total") == before.get("max_tasks_total"),
            "budget_parameters_are_policy_error": policy_error is not None,
            "budget_unchanged_after_attempt":
                after_attempt.get("max_decisions_total")
                == before.get("max_decisions_total")
                and after_attempt.get("max_tasks_total")
                == before.get("max_tasks_total"),
            "goal_count_unchanged": len(h.goals_for_project()) == 1,
            "budget_parameters_rejected_by_schema": _budget_param_rejected(),
        }
    finally:
        h.close()


def _budget_param_rejected() -> bool:
    raw = {"intent": "submit_goal", "project_id": "prj-1", "task_id": None,
           "goal_id": None, "reason": "x",
           "parameters": {"goal": "g", "max_tasks": 50}}
    try:
        parse_intent(raw)
    except ChatPolicyError:
        return True
    except ChatValidationError:
        return False
    return False


# --------------------------------------------------------------------------- #
# E. genuine imperatives still work
# --------------------------------------------------------------------------- #
def e_imperatives_still_submit() -> dict:
    h = Harness(fresh_ledger("e"))
    try:
        router = HeuristicIntentRouter()
        base = {"projects": [{"project_id": h.project_id, "name": "t51"}],
                "conversation": {"selected_project_id": h.project_id,
                                 "selected_goal_id": None}}
        cases = {
            "继续推进 t51 项目：把包测试补齐": "submit_goal",
            "开始实现 t51 的导出功能": "submit_goal",
            "t51 有哪些阻塞？": "explain_blockers",
            # "进展如何" is a project-level question; "目标进度" is goal-level.
            "t51 项目进展如何": "project_status",
            "t51 的目标进度如何？": "goal_status",
        }
        routed = {text: router.classify({**base, "message": text})["intent"]
                  for text in cases}
        turn = h.say("继续推进 t51 项目：把包测试补齐")
        return {
            "imperatives_still_submit": routed["继续推进 t51 项目：把包测试补齐"]
            == "submit_goal",
            "english_imperative_submits":
                router.classify({**base, "message": "start implementing the export"})
                ["intent"] == "submit_goal",
            "blockers_still_read":
                routed["t51 有哪些阻塞？"] == "explain_blockers",
            "progress_question_is_project_status":
                routed["t51 项目进展如何"] == "project_status",
            "goal_progress_question_is_goal_status":
                routed["t51 的目标进度如何？"] == "goal_status",
            "turn_created_the_goal": turn["facts"].get("goal_id") is not None,
            "routed": routed,
        }
    finally:
        h.close()


def main() -> int:
    make_repo()
    checks: dict[str, object] = {}
    for name, fn in (("A_questions_never_submit", a_questions_never_submit),
                     ("B_model_needs_corroboration",
                      b_model_submit_needs_corroboration),
                     ("B2_corroborated_submit",
                      b2_corroborated_submit_is_immediate),
                     ("C_unregistered_project", c_unregistered_project_refused),
                     ("D_budget_immutable", d_chat_never_changes_budget),
                     ("E_imperatives", e_imperatives_still_submit)):
        try:
            checks[name] = fn()
        except Exception as exc:  # noqa: BLE001 - a section failure is evidence
            checks[name] = {"section_error": f"{type(exc).__name__}: {exc}"}

    failed: list[str] = []
    for section, payload in checks.items():
        if "section_error" in payload:
            failed.append(f"{section}.section_error")
            continue
        for key, value in payload.items():
            if isinstance(value, bool) and not value:
                failed.append(f"{section}.{key}")

    evidence = {
        "test": "T51 intent safety (M4-D Safety Closure)",
        "fixture": "<REPO>/sandbox/t51-intent-safety",
        "note": ("Deterministic: scripted intent model + stub adapter. Covers the "
                 "M4-D audit findings P0-1 (question misrouted to submit_goal) and "
                 "P0-3 (chat path changing a goal's lifetime budget), plus "
                 "unregistered-project refusal."),
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
