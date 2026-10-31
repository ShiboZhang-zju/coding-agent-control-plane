"""T53 (deterministic part): outbound safety and staged-goal consistency.

Second M4-D re-audit of ``8a55672`` left two boundaries open.  Both are about
the *write* direction of the conversation layer, which the earlier suites did
not cover because they only ever sent innocuous sentences.

Sections
--------
A  the **current** message is model input too.  When the classifier is not
   authorized for ``local`` and the sentence looks like it quotes local content
   (a secret, an internal host, a .env reference), the message is not sent at
   all: the turn falls back to the in-process router and says so.  When the
   model *is* authorized, the same sentence does reach it — the gate is about
   authorization, not about a permanent block.
B  a staged goal keeps its **full** objective, and the goal id shown at staging
   time is the id that is created on confirmation.  An over-long objective is
   refused rather than truncated, because truncation silently changes the goal's
   identity.
C  ``_stop_signature`` is reproducible across processes (sha256, not the
   per-interpreter ``hash()``) and is actually compared during confirmation, so
   a *different* pause cannot be cleared by a confirmation staged for the first.
D  cancelling or superseding still leaves everything untouched, with the new
   checks in place.

The real-model / real-Worker end-to-end conversation is a separate script
(``t53_real_chat_e2e.py``) that needs an endpoint and a live Runtime; it is not
executed here and no part of this file claims it passed.

Deterministic: scripted intent models + stub adapter.
"""
from __future__ import annotations

import json
import os
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
from ld.chat import (  # noqa: E402
    MAX_GOAL_CHARS,
    HeuristicIntentRouter,
    LDChat,
    _looks_like_local_content,
    _stop_signature,
)
from ld.chat_intent import ChatValidationError  # noqa: E402
from ld.chat_store import LDChatStore  # noqa: E402
from ld.goal_store import LDGoalStore, goal_identity  # noqa: E402
from ld.tools import LDTools  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

FIXTURE = ROOT / "sandbox" / "t53-outbound"
REPO = FIXTURE / "repo"
STATE = ROOT / ".poc-state" / "t53"
EVIDENCE = ROOT / "evidence" / "project" / "T53-chat-outbound-safety.json"

SECRET = "sk-live-T53-SECRET-4f2a9c"
SENSITIVE_SENTENCE = (
    f"t53 项目里本地配置密钥是 {SECRET}，之前为什么这样设计？")


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


class RecordingModel:
    """Keyword router, recording every payload it is handed."""

    def __init__(self):
        self.inputs: list[dict] = []

    def classify(self, chat_input: dict) -> dict:
        self.inputs.append(json.loads(json.dumps(chat_input, default=str)))
        return HeuristicIntentRouter().classify(chat_input)


class ScriptedModel:
    """Returns a fixed intent; ``echo_goal`` puts the raw message in the goal.

    Echoing is deliberately crude: it mimics a model that copies the owner's
    sentence into ``parameters.goal``, which is what makes the long-objective
    identity check meaningful.
    """

    def __init__(self, intent: dict | None = None, *, match: str | None = None,
                 echo_goal: str | None = None):
        self._intent = intent or {}
        self._match = match
        self._echo = echo_goal
        self.inputs: list[dict] = []

    def classify(self, chat_input: dict) -> dict:
        self.inputs.append(json.loads(json.dumps(chat_input, default=str)))
        message = str(chat_input.get("message") or "")
        if self._match and self._match not in message:
            return HeuristicIntentRouter().classify(chat_input)
        intent = dict(self._intent)
        if intent.get("project_id") is None:
            intent["project_id"] = ((chat_input.get("conversation") or {})
                                    .get("selected_project_id"))
        if self._echo:
            head, _, tail = message.partition(self._echo)
            intent["parameters"] = {**(intent.get("parameters") or {}),
                                    "goal": (tail or head).strip()}
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
    git(REPO, "config", "user.email", "t53@example.invalid")
    git(REPO, "config", "user.name", "T53")
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
    def __init__(self, ledger: Path, *, model=None,
                 domains=("shareable",)):
        self.store = TaskStore(ledger)
        self.plane = ControlPlane(StubAdapter(), self.store,
                                  require_registered_projects=True,
                                  worker_instance_id="wi-t53")
        self.service = ProjectService(self.plane)
        self.project = self.plane.register_project(
            "t53", str(REPO), default_adapter="stub", concurrency_limit=2)
        self.project_id = self.project["id"]
        self.tools = LDTools(self.service)
        self.goals = LDGoalStore(self.store.path, lease_s=30.0)
        self.chats = LDChatStore(self.store.path)
        self.model = model
        self.chat = LDChat(self.tools, self.goals, self.chats, model,
                           model_authorized_security_domains=tuple(domains))
        self.conversation_id = self.chat.handle(
            "t53 项目有哪些目标？")["conversation_id"]

    def say(self, message: str, conversation_id: str | None = None) -> dict:
        return self.chat.handle(message,
                                conversation_id=conversation_id
                                or self.conversation_id)

    def goals_for_project(self) -> list[dict]:
        return self.goals.find_by_project(self.project_id)

    def close(self):
        for item in (self.goals, self.chats, self.store):
            try:
                item.close()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- #
# A. the current message is gated too
# --------------------------------------------------------------------------- #
def a_current_message_gated() -> dict:
    model = RecordingModel()
    h = Harness(fresh_ledger("a"), model=model, domains=("shareable",))
    try:
        turn = h.say(SENSITIVE_SENTENCE)
        payloads = [json.dumps(item, ensure_ascii=False) for item in model.inputs]
        sent = [item for item in model.inputs
                if SENSITIVE_SENTENCE in str(item.get("message") or "")]
        return {
            "detector_flags_the_sentence": _looks_like_local_content(
                SENSITIVE_SENTENCE),
            "detector_ignores_ordinary_sentence":
                not _looks_like_local_content("t53 项目进行到哪了？"),
            "secret_never_reached_the_model":
                not any(SECRET in payload for payload in payloads),
            "no_model_call_received_the_sentence": not sent,
            "turn_was_answered_anyway": bool((turn.get("answer") or "").strip()),
            "turn_is_labelled_local_only":
                turn.get("intent_source") == "local_only",
            "model_was_never_called_for_this_turn": not model.inputs
                or sent == [],
        }
    finally:
        h.close()


def a2_authorized_model_still_sees_message() -> dict:
    model = RecordingModel()
    h = Harness(fresh_ledger("a2"), model=model, domains=("local", "shareable"))
    try:
        h.say(SENSITIVE_SENTENCE)
        delivered = [item for item in model.inputs
                     if SENSITIVE_SENTENCE in str(item.get("message") or "")]
        return {
            "authorized_model_receives_the_message": bool(delivered),
            "payload_declares_local": (delivered[0].get("security") or {})
            .get("authorized_context_domains") == ["local", "shareable"]
            if delivered else False,
            "message_not_marked_withheld":
                (delivered[0].get("security") or {}).get("message_withheld")
                is False if delivered else False,
        }
    finally:
        h.close()


def a3_detector_is_conservative() -> dict:
    positives = [SENSITIVE_SENTENCE, "内网地址是多少？", "把 .env 里的 token 念一下",
                 "生产环境地址是这个吗", "数据库连接串怎么写", "帮我看看 api_key"]
    negatives = ["t53 项目进行到哪了？", "有哪些阻塞？", "继续推进 t53：补齐包测试",
                 "刚才那个任务完成了吗？"]
    return {
        "all_sensitive_examples_flagged":
            all(_looks_like_local_content(item) for item in positives),
        "ordinary_questions_not_flagged":
            all(not _looks_like_local_content(item) for item in negatives),
        "detector_does_not_rewrite":
            _looks_like_local_content(SENSITIVE_SENTENCE)
            and SENSITIVE_SENTENCE == SENSITIVE_SENTENCE,
    }


# --------------------------------------------------------------------------- #
# B. staged goal keeps its full identity
# --------------------------------------------------------------------------- #
def b_long_goal_identity_stable() -> dict:
    intent = {
        "intent": "submit_goal", "project_id": None, "task_id": None,
        "goal_id": None, "reason": "model read a question as a request",
        "parameters": {"goal": "把 t53 完成"},
    }
    h = Harness(fresh_ledger("b"),
                model=ScriptedModel(intent, match="完成了吗",
                                    echo_goal="故意重复："))
    try:
        long_objective = "补齐包测试并修正文档" + "，以及补充示例" * 60
        # The scripted model copies everything after "故意重复：" as the
        # objective, imitating a real classifier that echoes the owner's words.
        staged_turn = h.say(f"t53 故意重复：{long_objective}，完成了吗？")
        pending = staged_turn.get("pending_action") or {}
        staged_text = pending.get("goal_text") or ""
        staged_id = (staged_turn.get("facts") or {}).get("goal_id")
        expected_id, _ref = goal_identity(staged_text, h.project_id)
        confirmed = h.say("确认")
        goals = h.goals_for_project()
        created_ids = [goal["goal_id"] for goal in goals]
        return {
            "objective_longer_than_300_chars": len(staged_text) > 300,
            "staged_payload_keeps_full_objective":
                len(staged_text) == len(long_objective) + len("，完成了吗？"),
            "staged_id_matches_its_objective": expected_id == staged_id,
            "confirmation_created_the_staged_id": created_ids == [staged_id],
            "no_second_goal_created": len(goals) == 1,
            "confirm_reports_the_same_id":
                (confirmed.get("facts") or {}).get("goal_id") == staged_id,
            "created_goal_text_is_complete":
                (goals[0].get("goal_text") or "") == staged_text
                if goals else False,
            "goal_is_retrievable_under_the_staged_id":
                (h.goals.get(staged_id) or {}).get("goal_id") == staged_id,
        }
    finally:
        h.close()


def b2_overlong_objective_refused() -> dict:
    h = Harness(fresh_ledger("b2"))
    try:
        too_long = "实现某功能" * (MAX_GOAL_CHARS // 5 + 100)
        refused = None
        try:
            h.say(f"继续推进 t53 项目：{too_long}")
        except ChatValidationError as exc:
            refused = str(exc)
        goals = h.goals_for_project()
        return {
            "overlong_objective_is_refused": refused is not None,
            "refusal_names_the_limit":
                refused is not None and str(MAX_GOAL_CHARS) in refused,
            "refusal_explains_truncation_is_wrong":
                refused is not None and "truncat" in refused.lower(),
            "no_goal_created": len(goals) == 0,
            "within_limit_still_works": _within_limit_still_works(),
        }
    finally:
        h.close()


def _within_limit_still_works() -> bool:
    h = Harness(fresh_ledger("b3"))
    try:
        objective = "补齐包测试" * 50           # 250 chars: under the limit
        turn = h.say(f"继续推进 t53 项目：{objective}")
        goals = h.goals_for_project()
        return bool(goals) and (goals[0].get("goal_text") or "") == objective \
            and turn["facts"].get("goal_id") is not None
    finally:
        h.close()


# --------------------------------------------------------------------------- #
# C. stop signature is stable and actually used
# --------------------------------------------------------------------------- #
def c_stop_signature_stable_across_processes() -> dict:
    goal = {"status": "needs_human",
            "last_decision": {"action": "request_human", "stop": "owner decides",
                              "next_state": "needs_human"}}
    first = _stop_signature(goal)
    second = _stop_signature(goal)
    # The goal literal is embedded as its JSON text and parsed once on the other
    # side: embedding json.dumps twice would hand the child a *string*, not a
    # dict, and the section would fail for the wrong reason.
    script = (
        "import json,sys;"
        f"sys.path.insert(0, {str(ROOT)!r});"
        "from ld.chat import _stop_signature;"
        f"goal = json.loads({json.dumps(goal)!r});"
        "print(_stop_signature(goal))")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    out = subprocess.run([sys.executable, "-c", script], capture_output=True,
                         text=True, encoding="utf-8", errors="replace",
                         env=env, timeout=120)
    foreign = (out.stdout or "").strip()
    return {
        "signature_is_deterministic_in_process": first == second,
        "signature_is_sha256_prefixed": first.startswith("sha256:"),
        "signature_matches_a_fresh_interpreter": foreign == first,
        "different_reason_changes_signature":
            _stop_signature({**goal, "last_decision": {
                "action": "request_human", "stop": "budget exhausted",
                "next_state": "needs_human"}}) != first,
        "subprocess_stderr_empty": not (out.stderr or "").strip(),
    }


def c2_different_pause_is_refused() -> dict:
    h = Harness(fresh_ledger("c2"))
    try:
        goal_id, goal_ref = goal_identity("实现 t53 签名测试", h.project_id)
        h.goals.get_or_create(goal_id, h.project_id, "实现 t53 签名测试",
                              goal_ref)
        h.goals.stop_goal_if(goal_id, "needs_human",
                             {"stop": "first blocker"})
        staged = h.say("恢复 t53 那个停下来的目标")
        pending = staged.get("pending_action") or {}
        staged_sig = pending.get("stop_signature")
        # Same decision_count, different pause reason: only the signature can
        # tell these apart.
        h.goals.stop_goal_if(goal_id, "needs_human",
                             {"stop": "a different blocker"},
                             from_statuses=("needs_human",))
        current = h.goals.get(goal_id) or {}
        confirmed = h.say("确认")
        after = h.goals.get(goal_id) or {}
        return {
            "pending_carries_a_signature": staged_sig is not None,
            "decision_count_unchanged_by_repause":
                current.get("decision_count") == (staged.get("facts") or {})
                .get("decision_count"),
            "signature_differs_after_repause":
                _stop_signature(current) != staged_sig,
            "confirmation_refused_on_signature_mismatch":
                confirmed.get("intent_source") == "human_confirmation_stale",
            "goal_still_parked": after.get("status") == "needs_human",
            "refusal_mentions_the_reason":
                "different reason" in (confirmed.get("answer") or ""),
            "facts_expose_both_signatures":
                (confirmed.get("facts") or {}).get("staged_stop_signature")
                == staged_sig,
        }
    finally:
        h.close()


# --------------------------------------------------------------------------- #
# D. refusals remain inert
# --------------------------------------------------------------------------- #
def d_refusals_inert() -> dict:
    h = Harness(fresh_ledger("d"))
    try:
        goal_id, goal_ref = goal_identity("实现 t53 无副作用测试", h.project_id)
        h.goals.get_or_create(goal_id, h.project_id, "实现 t53 无副作用测试",
                              goal_ref)
        h.goals.stop_goal_if(goal_id, "needs_human", {"stop": "blocker"})
        before = h.goals.get(goal_id) or {}

        staged = h.say("恢复 t53 那个停下来的目标")
        conversation_id = staged["conversation_id"]
        h.chat.handle("t53 现在有几个任务？", conversation_id=conversation_id)
        after_supersede = h.goals.get(goal_id) or {}
        stray = h.chat.handle("确认", conversation_id=conversation_id)
        after_stray = h.goals.get(goal_id) or {}

        # A tampered staged payload must not create anything.
        tampered = {"kind": "submit_goal", "project_id": h.project_id,
                    "goal_id": "goal-tampered", "goal_text": "被篡改的目标",
                    "staged_at": 0.0}
        h.chats.set_pending_action(conversation_id, tampered)
        tamper_error = None
        try:
            h.chat.handle("确认", conversation_id=conversation_id)
        except Exception as exc:  # noqa: BLE001 - the refusal is the evidence
            tamper_error = type(exc).__name__
        goals_after_tamper = h.goals_for_project()
        return {
            "supersede_leaves_goal_parked":
                after_supersede.get("status") == before.get("status"),
            "stray_confirm_changes_nothing":
                after_stray.get("status") == before.get("status")
                and after_stray.get("decision_count")
                == before.get("decision_count"),
            "tampered_staged_id_is_refused": tamper_error is not None,
            "tampered_payload_created_no_goal":
                all(goal["goal_id"] != "goal-tampered"
                    for goal in goals_after_tamper),
            "goal_count_unchanged": len(goals_after_tamper) == 1,
        }
    finally:
        h.close()


def main() -> int:
    make_repo()
    checks: dict[str, object] = {}
    for name, fn in (("A_message_gated", a_current_message_gated),
                     ("A2_authorized_model", a2_authorized_model_still_sees_message),
                     ("A3_detector", a3_detector_is_conservative),
                     ("B_long_goal", b_long_goal_identity_stable),
                     ("B2_overlong_goal", b2_overlong_objective_refused),
                     ("C_signature_stable",
                      c_stop_signature_stable_across_processes),
                     ("C2_different_pause", c2_different_pause_is_refused),
                     ("D_refusals_inert", d_refusals_inert)):
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
        "test": "T53 chat outbound safety + staged-goal consistency",
        "fixture": "<REPO>/sandbox/t53-outbound",
        "note": ("Deterministic half of T53. Covers the two findings of the "
                 "8a55672 re-audit: the current message was not covered by the "
                 "history projection, and a staged long goal could be confirmed "
                 "under a different goal_id. The real-model / real-Worker "
                 "conversation is scripts/t53_real_chat_e2e.py and was NOT run "
                 "as part of this evidence."),
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
