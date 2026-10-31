"""T52: conversation security — history projection and confirmation binding.

The M4-D audit of ``2d478ae`` found two cross-turn problems that a happy-path
test cannot see:

* **P0-2 (indirect leak).**  ``_classify()`` forwarded the last six transcript
  messages verbatim, including the LD's own answers.  A ``knowledge_query``
  renders project-knowledge bodies into that answer, so the next turn shipped
  ``local``-domain content to a classifier endpoint authorized only for
  ``shareable`` — a leak the per-field projection never covered.
* **P1 (stale confirmation).**  A staged ``resume`` was bound to the goal id
  only.  If the goal was resumed elsewhere, paused again for a different
  reason, and the old conversation still held the staged action, that stale
  confirmation could clear the *new* pause.

Sections
--------
A  the classifier input never carries LD answer prose, never carries a
   ``local`` knowledge body, and still carries the routing hints it needs.
B  a confirmation is bound to the pause it was staged from: after an unrelated
   resume the stale confirmation is refused and the goal stays parked.
C  concurrent confirmations apply at most once (atomic consume).
D  a refused or superseded confirmation leaves the goal untouched.
E  the projection degrades correctly across security-domain authorization: a
   ``shareable``-only model sees less, not more.

Deterministic: scripted models + stub adapter.  Real-endpoint behaviour is not
claimed here.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import DispatchResult, TaskState, TaskSnapshot  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.project_context import ContextEntry  # noqa: E402
from controlplane.project_service import ProjectService  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from ld.chat import HeuristicIntentRouter, LDChat, _safe_history  # noqa: E402
from ld.chat_store import LDChatStore  # noqa: E402
from ld.coordinator import LDCoordinator  # noqa: E402
from ld.goal_store import LDGoalStore, goal_identity  # noqa: E402
from ld.tools import LDTools  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

FIXTURE = ROOT / "sandbox" / "t52-chat-security"
REPO = FIXTURE / "repo"
STATE = ROOT / ".poc-state" / "t52"
EVIDENCE = ROOT / "evidence" / "project" / "T52-conversation-security.json"

GOAL_TEXT = "实现 T52 安全对话演示"
SECRET = "T52-LOCAL-ONLY-CONFIG-SECRET-9f1c"
SECRET_TITLE = "T52 本地部署密钥约定"


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
    """Keyword router, but every classifier input is recorded for inspection."""

    def __init__(self):
        self.inputs: list[dict] = []

    def classify(self, chat_input: dict) -> dict:
        self.inputs.append(json.loads(json.dumps(chat_input, default=str)))
        return HeuristicIntentRouter().classify(chat_input)


class ScriptedModel:
    def __init__(self, intents: list[dict]):
        self._queue = list(intents)
        self.inputs: list[dict] = []

    def classify(self, chat_input: dict) -> dict:
        self.inputs.append(chat_input)
        if not self._queue:
            return HeuristicIntentRouter().classify(chat_input)
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
    git(REPO, "config", "user.email", "t52@example.invalid")
    git(REPO, "config", "user.name", "T52")
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
                 domains=("local", "shareable")):
        self.store = TaskStore(ledger)
        self.plane = ControlPlane(StubAdapter(), self.store,
                                  require_registered_projects=True,
                                  worker_instance_id="wi-t52")
        self.service = ProjectService(self.plane)
        self.project = self.plane.register_project(
            "t52", str(REPO), default_adapter="stub", concurrency_limit=2)
        self.project_id = self.project["id"]
        self.tools = LDTools(self.service)
        self.goals = LDGoalStore(self.store.path, lease_s=30.0)
        self.chats = LDChatStore(self.store.path)
        self.model = model
        self.coordinator = LDCoordinator(
            self.tools, None,
            model_authorized_security_domains=tuple(domains))
        self.chat = LDChat(self.tools, self.goals, self.chats, model,
                           model_authorized_security_domains=tuple(domains))
        self.conversation_id = self.chat.handle(
            "t52 项目有哪些目标？")["conversation_id"]

    def say(self, message: str, conversation_id: str | None = None) -> dict:
        return self.chat.handle(message,
                                conversation_id=conversation_id
                                or self.conversation_id)

    def add_local_knowledge(self) -> dict:
        return self.service.add_context_entry(self.project_id, ContextEntry(
            project_id=self.project_id, kind="knowledge",
            title=SECRET_TITLE, body=SECRET, tags=["deploy", "secret"],
            verification="verified", security_domain="local", source="human",
            evidence_refs=["evidence/project/T52-conversation-security.json"]))

    def staged_goal(self, status: str = "needs_human") -> tuple[str, str]:
        goal_id, goal_ref = goal_identity(GOAL_TEXT, self.project_id)
        self.goals.get_or_create(goal_id, self.project_id, GOAL_TEXT, goal_ref)
        if status == "needs_human":
            self.goals.stop_goal_if(goal_id, "needs_human",
                                    {"stop": "needs an owner decision"})
        return goal_id, goal_ref

    def close(self):
        for item in (self.goals, self.chats, self.store):
            try:
                item.close()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- #
# A. history projection
# --------------------------------------------------------------------------- #
def a_history_projection() -> dict:
    model = RecordingModel()
    h = Harness(fresh_ledger("a"), model=model, domains=("shareable",))
    try:
        h.add_local_knowledge()
        # A knowledge query renders the local body into the LD's answer.
        read_turn = h.say(f"t52 的知识里 {SECRET_TITLE} 说了什么？")
        answer_leaked = SECRET in (read_turn.get("answer") or "")
        # The next turn is what a naive implementation would leak.
        h.say("那接下来做什么？")
        second_input = model.inputs[-1] if model.inputs else {}
        payload = json.dumps(second_input, ensure_ascii=False)
        history = ((second_input.get("conversation") or {})
                   .get("recent_turns") or [])

        # The projection itself, exercised directly for the local branch.
        local_history = _safe_history(
            [{"role": "ld", "content": f"body: {SECRET}", "intent": {
                "intent": "knowledge_query"}, "goal_ids": []}],
            domains=("local", "shareable"))
        return {
            "knowledge_answer_really_contains_local_body": answer_leaked,
            "secret_absent_from_next_model_input": SECRET not in payload,
            "secret_title_absent_from_history":
                SECRET_TITLE not in json.dumps(history, ensure_ascii=False),
            "ld_answer_prose_not_forwarded":
                all(item.get("content_omitted") is True
                    for item in history if item.get("role") != "user"),
            "ld_answer_keeps_intent_label":
                any(item.get("intent") == "knowledge_query" for item in history),
            "history_is_bounded":
                len(history) <= 6,
            "user_turn_still_present":
                any(item.get("role") == "user" for item in history),
            "project_names_excluded_without_local":
                all("name" not in row
                    for row in second_input.get("projects") or []),
            "declared_domains_match":
                (second_input.get("security") or {}).get(
                    "authorized_context_domains") == ["shareable"],
            "local_domain_also_omits_prose":
                all(item.get("content_omitted") is True
                    for item in local_history if item.get("role") != "user"),
            "note_is_explicit":
                all(item.get("note") for item in history
                    if item.get("role") != "user"),
        }
    finally:
        h.close()


# --------------------------------------------------------------------------- #
# B. a confirmation is bound to its own pause
# --------------------------------------------------------------------------- #
def b_stale_confirmation_refused() -> dict:
    h = Harness(fresh_ledger("b"))
    try:
        goal_id, _ref = h.staged_goal("needs_human")
        staged = h.say("恢复 t52 那个停下来的目标")
        staged_count = (staged.get("facts") or {}).get("decision_count")
        pending = staged.get("pending_action") or {}

        # Another terminal resumes the goal, then it pauses again for a
        # *different* reason.  The old conversation still holds a staged action.
        h.goals.resume(goal_id, note="resumed from another terminal")
        h.goals.stop_goal_if(goal_id, "needs_human",
                             {"stop": "a completely different blocker"})
        current = h.goals.get(goal_id) or {}

        confirmed = h.say("确认")
        after = h.goals.get(goal_id) or {}
        return {
            "staged_bound_to_decision_count": isinstance(staged_count, int),
            "pending_carries_the_version":
                pending.get("decision_count") == staged_count,
            "goal_moved_on_as_expected":
                int(current.get("decision_count") or 0) != int(staged_count or 0),
            "stale_confirmation_refused":
                confirmed.get("intent_source") == "human_confirmation_stale",
            "goal_still_parked_after_stale_confirm":
                after.get("status") == "needs_human",
            "refusal_explains_why":
                "changed since I staged" in (confirmed.get("answer") or ""),
            "facts_report_both_versions":
                (confirmed.get("facts") or {}).get("staged_decision_count")
                == staged_count
                and (confirmed.get("facts") or {}).get("current_decision_count")
                == current.get("decision_count"),
        }
    finally:
        h.close()


# --------------------------------------------------------------------------- #
# C. concurrent confirmations apply at most once
# --------------------------------------------------------------------------- #
def c_concurrent_confirm_once() -> dict:
    h = Harness(fresh_ledger("c"))
    try:
        goal_id, _ref = h.staged_goal("needs_human")
        staged = h.say("恢复 t52 那个停下来的目标")
        conversation_id = staged["conversation_id"]
        decisions_before = int((h.goals.get(goal_id) or {})
                               .get("decision_count") or 0)

        results: list[dict] = []
        errors: list[str] = []
        barrier = threading.Barrier(4)

        def confirm() -> None:
            barrier.wait()
            try:
                results.append(h.chat.handle(
                    "确认", conversation_id=conversation_id))
            except Exception as exc:  # noqa: BLE001 - recorded as evidence
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=confirm) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        applied = [r for r in results
                   if r.get("intent_source") == "human_confirmation"
                   and (r.get("facts") or {}).get("resumed") is True]
        stale = [r for r in results
                 if r.get("intent_source") == "human_confirmation_stale"]
        decisions_after = int((h.goals.get(goal_id) or {})
                              .get("decision_count") or 0)
        return {
            "four_concurrent_confirmations_returned": len(results) == 4,
            "no_thread_raised": not errors,
            "exactly_one_applied": len(applied) == 1,
            "others_reported_stale": len(stale) == 3,
            "decision_count_advanced_once":
                decisions_after == decisions_before + 1,
            "goal_is_active": (h.goals.get(goal_id) or {}).get("status")
            == "active",
        }
    finally:
        h.close()


# --------------------------------------------------------------------------- #
# D. refused / superseded confirmations change nothing
# --------------------------------------------------------------------------- #
def d_refusals_are_inert() -> dict:
    h = Harness(fresh_ledger("d"))
    try:
        goal_id, _ref = h.staged_goal("needs_human")
        before = h.goals.get(goal_id) or {}

        staged = h.say("恢复 t52 那个停下来的目标")
        conversation_id = staged["conversation_id"]
        superseded = h.chat.handle("t52 现在有几个任务？",
                                   conversation_id=conversation_id)
        after_supersede = h.goals.get(goal_id) or {}
        stray = h.chat.handle("确认", conversation_id=conversation_id)
        after_stray = h.goals.get(goal_id) or {}

        staged_again = h.chat.handle("恢复 t52 那个停下来的目标",
                                     conversation_id=conversation_id)
        denied = h.chat.handle("取消", conversation_id=conversation_id)
        after_deny = h.goals.get(goal_id) or {}

        return {
            "staged": staged.get("requires_confirmation") is True,
            "superseding_message_reports_it":
                superseded.get("superseded_pending_action") is not None,
            "goal_untouched_by_supersede":
                after_supersede.get("status") == before.get("status")
                and after_supersede.get("decision_count")
                == before.get("decision_count"),
            "confirm_after_supersede_does_nothing":
                after_stray.get("status") == before.get("status")
                and after_stray.get("decision_count")
                == before.get("decision_count"),
            "confirm_without_stage_is_not_applied":
                stray.get("intent_source") != "human_confirmation",
            "restage_works": staged_again.get("requires_confirmation") is True,
            "cancel_keeps_goal_parked":
                after_deny.get("status") == "needs_human",
            "cancel_does_not_advance_history":
                after_deny.get("decision_count") == before.get("decision_count"),
            "cancel_reported": (denied.get("facts") or {}).get("cancelled") is True,
        }
    finally:
        h.close()


# --------------------------------------------------------------------------- #
# E. the projection degrades monotonically with authorization
# --------------------------------------------------------------------------- #
def e_projection_is_monotonic() -> dict:
    messages = [
        {"role": "user", "content": f"看下 {SECRET_TITLE}", "intent": None,
         "goal_ids": []},
        {"role": "ld", "content": f"正文：{SECRET}", "goal_ids": ["goal-1"],
         "intent": {"intent": "knowledge_query"}},
    ]
    both = _safe_history(messages, domains=("local", "shareable"))
    shareable = _safe_history(messages, domains=("shareable",))
    empty = _safe_history(messages, domains=())

    def payload_chars(items) -> int:
        """Characters of *content* that would reach the endpoint.

        Byte size is not the right measure — an explanatory ``note`` is longer
        than the omitted text — what matters is how much real content travels.
        """
        return sum(len(item.get("content") or "") for item in items)

    return {
        "authorization_is_monotonic":
            payload_chars(empty) <= payload_chars(shareable)
            <= payload_chars(both),
        "unauthorized_projection_carries_no_content":
            payload_chars(shareable) == 0 and payload_chars(empty) == 0,
        "secret_never_in_any_projection":
            all(SECRET not in json.dumps(items, ensure_ascii=False)
                for items in (both, shareable, empty)),
        "shareable_only_withholds_user_text":
            all(item.get("content_omitted") for item in shareable
                if item.get("role") == "user"),
        "local_authorization_keeps_user_text":
            not any(item.get("content_omitted") for item in both
                    if item.get("role") == "user"),
        "local_authorized_user_text_is_real":
            any(item.get("content") == f"看下 {SECRET_TITLE}" for item in both
                if item.get("role") == "user"),
        "goal_ids_survive_projection":
            all(item.get("goal_ids") == ["goal-1"] for items in
                (both, shareable, empty) for item in items
                if item.get("role") == "ld"),
    }


def main() -> int:
    make_repo()
    checks: dict[str, object] = {}
    for name, fn in (("A_history_projection", a_history_projection),
                     ("B_stale_confirmation", b_stale_confirmation_refused),
                     ("C_concurrent_confirm", c_concurrent_confirm_once),
                     ("D_refusals_inert", d_refusals_are_inert),
                     ("E_projection_monotonic", e_projection_is_monotonic)):
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
        "test": "T52 conversation security (M4-D Safety Closure)",
        "fixture": "<REPO>/sandbox/t52-chat-security",
        "note": ("Deterministic: scripted/heuristic classifiers + stub adapter. "
                 "Covers M4-D audit P0-2 (cross-turn leak through the LD's own "
                 "answers) and P1 (stale/concurrent confirmation). Real endpoints "
                 "are not exercised; no claim is made about them."),
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
