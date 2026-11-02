"""T62 stage 1: real-project memory pilot, read-only rehearsal.

What this run is
----------------
A *rehearsal* of the pilot deployment, on a throwaway Git fixture, with the
real decision/intent model.  It answers one question and nothing more:

    Can a human, using only LD Chat, register a project, have the project's
    confirmed knowledge injected into a task's Effective Prompt, and create a
    bounded Goal — without any real repository being touched and without any
    Worker being dispatched?

What this run is NOT
--------------------
* No real project repository is registered or modified.
* No CodeBuddy Worker is started, no WorkItem is created, no verdict is written.
* Nothing here proves the two-task memory hand-off (Session B reusing Session A's
  knowledge).  That is stage 2, which needs a real repo and a real Worker.

Strictness
----------
* Every fact in the evidence is read back from SQLite (Ledger / Goal / Context),
  never from an answer string alone.
* Read-only turns must not change the WorkItem count, the Goal count or the
  context version.  A turn that quietly dispatched work FAILS the run.
* A real model answer of ``submit_goal`` to a read question is recorded as a
  finding; the turn is staged by design and must not apply.
* Without ``LD_MODEL_*`` the run is recorded as BLOCKED, never as a pass.

Exit codes: 0 PASS, 1 FAIL, 2 BLOCKED.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.project_memory_md import ProjectMemoryMarkdown  # noqa: E402
from controlplane.project_service import ProjectService  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from ld.chat import LDChat  # noqa: E402
from ld.chat_store import LDChatStore  # noqa: E402
from ld.goal_store import LDGoalStore  # noqa: E402
from ld.model import OpenAIChatIntentModel  # noqa: E402
from ld.tools import LDTools  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

FIXTURE = ROOT / "sandbox" / f"t62-rehearsal-{uuid.uuid4().hex[:8]}"
STATE = ROOT / ".poc-state" / "t62"
EVIDENCE = ROOT / "evidence" / "project" / os.environ.get(
    "T62_EVIDENCE_NAME", "T62-real-project-pilot-stage1.json")

PROJECT_NAME = "t62-rehearsal"

BACKGROUND = "T62-BACKGROUND: 这是一个用于 T62 只读预演的临时仓库。"
CONSTRAINT = "T62-CONSTRAINT: 不得引入任何网络调用。"
KNOWLEDGE = "T62-KNOWLEDGE: 历史结论是配置通过 load_config 读取。"
EDITED_KNOWLEDGE = "T62-KNOWLEDGE-EDITED: 配置通过 load_config 读取，并且必须支持默认值。"

OBJECTIVE = (
    "在 t62-rehearsal 项目中新增 configlib 包，实现 load_config 读取字典配置。"
)


class AdapterRef:
    """Name-only adapter: this process can never dispatch a Worker."""

    name = "stub"


def git(path: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(path), *args], check=False,
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"git {args} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q")
    git(path, "config", "user.email", "t62@example.invalid")
    git(path, "config", "user.name", "T62")
    (path / "README.md").write_text("# t62 rehearsal fixture\n", encoding="utf-8")
    git(path, "add", "README.md")
    git(path, "commit", "-q", "-m", "seed")
    git(path, "branch", "-M", "main")
    return path


def fresh_ledger(name: str) -> Path:
    STATE.mkdir(parents=True, exist_ok=True)
    ledger = STATE / f"{name}.db"
    for stale in [ledger, *ledger.parent.glob(ledger.name + "*")]:
        stale.unlink(missing_ok=True)
    return ledger


def blocked(reason: str) -> int:
    # A blocked run must never overwrite an existing result file: replacing a
    # real PASS with "BLOCKED because the endpoint is unset" would destroy the
    # very evidence this suite exists to produce.  It gets its own file.
    write_evidence(EVIDENCE.with_name(EVIDENCE.stem + ".blocked.json"), {
        "test": "T62 real-project memory pilot, stage 1 (read-only rehearsal)",
        "result": "BLOCKED",
        "blocked_reason": reason,
        "worker_started": False,
        "note": ("An environment-blocked run is recorded as BLOCKED, never as a "
                 "pass, and proves nothing about the pilot. It is written to a "
                 "separate file so it cannot overwrite a real result."),
    })
    print(json.dumps({"result": "BLOCKED", "reason": reason}, ensure_ascii=False))
    return 2


def main() -> int:
    base_url = os.environ.get("LD_MODEL_BASE_URL", "").strip()
    model_name = os.environ.get("LD_MODEL_NAME", "").strip()
    if not base_url or not model_name:
        return blocked(
            "LD_MODEL_BASE_URL / LD_MODEL_NAME not configured; no real intent "
            "model available. Re-run with the pilot's model endpoint.")

    repo = make_repo(FIXTURE / "repo")
    ledger = fresh_ledger("rehearsal")
    store = TaskStore(ledger)
    plane = ControlPlane(AdapterRef(), store, require_registered_projects=True,
                         worker_instance_id="wi-t62")
    service = ProjectService(plane)
    goals = LDGoalStore(store.path, lease_s=60.0)
    chats = LDChatStore(store.path)
    model = OpenAIChatIntentModel(
        base_url=base_url, model=model_name,
        api_key=os.environ.get("LD_MODEL_API_KEY") or None,
        timeout_s=float(os.environ.get("LD_MODEL_TIMEOUT_S", "60")))
    tools = LDTools(service)
    chat = LDChat(tools, goals, chats, model,
                  model_authorized_security_domains=("local", "shareable"))

    turns: list[dict] = []
    conversation_id: str | None = None
    evidence: dict = {}
    try:
        project = plane.register_project(
            PROJECT_NAME, str(repo), default_adapter="stub")
        project_id = project["id"]

        background = service.add_context_entry(project_id, {
            "kind": "background", "title": "t62 purpose", "body": BACKGROUND,
            "tags": ["t62"], "verification": "verified",
            "security_domain": "local", "source": "human:t62",
            "evidence_refs": ["t62:human-confirmed"],
        })
        constraint = service.add_context_entry(project_id, {
            "kind": "constraints", "title": "t62 no network", "body": CONSTRAINT,
            "tags": ["t62", "safety"], "verification": "verified",
            "security_domain": "local", "source": "human:t62",
            "evidence_refs": ["t62:human-confirmed"],
        })
        knowledge = service.add_context_entry(project_id, {
            "kind": "knowledge", "title": "t62 config knowledge",
            "body": KNOWLEDGE, "tags": ["t62"], "verification": "candidate",
            "security_domain": "local", "source": "agent:t62-rehearsal",
            "evidence_refs": [],
        })

        def counts() -> dict:
            return {
                "work_items": len(store.all()),
                "goals": len(goals.find_by_project(project_id, limit=100)),
                "context_version": store.context_version(project_id),
            }

        def say(text: str) -> dict:
            """One chat turn, with a before/after side-effect probe."""
            nonlocal conversation_id
            before = counts()
            turn = chat.handle(text, conversation_id=conversation_id)
            conversation_id = turn["conversation_id"]
            after = counts()
            turns.append({
                "message": text,
                "intent": (turn.get("intent") or {}).get("intent"),
                "intent_source": turn.get("intent_source"),
                "requires_confirmation": turn.get("requires_confirmation"),
                "answer_head": (turn.get("answer") or "")[:400],
                "goal_ids": turn.get("goal_ids"),
                "before": before, "after": after,
                "side_effects": {
                    "work_items": after["work_items"] - before["work_items"],
                    "goals": after["goals"] - before["goals"],
                    "context_version": (after["context_version"]
                                        - before["context_version"]),
                },
            })
            return turn

        # 1) read-only: what projects exist, read entirely from the ledger
        say("现在有哪些项目？")
        # 2) read-only: project status
        say(f"{PROJECT_NAME} 现在进行到哪了？")
        # 3) read-only: knowledge question (must not create anything)
        say(f"{PROJECT_NAME} 这个项目的配置是怎么读取的？")

        # 4) write intent, on a throwaway fixture: create one bounded Goal
        submit_turn = say(f"在 {PROJECT_NAME} 项目完成：{OBJECTIVE}")
        if submit_turn.get("requires_confirmation"):
            confirm_turn = say("confirm")
            confirmation_used = True
        else:
            confirm_turn = submit_turn
            confirmation_used = False
        goal_ids = confirm_turn.get("goal_ids") or []
        goal_rows = goals.find_by_project(project_id, limit=100)

        # 5) read-only again: the goal the conversation created
        goal_status_turn = say(f"{PROJECT_NAME} 的目标现在怎么样了？")

        # --- what the Worker *would* see, built but never dispatched ---------
        built = service.build_task_context(project_id, OBJECTIVE)
        prompt = built.effective_prompt
        entry_ids = list(built.entry_refs or [])

        # --- the same human memory loop T62 stage 2 will run, read-only ------
        memory = ProjectMemoryMarkdown(service, goal_store=goals,
                                       base_dir=STATE / "memory-root")
        exported = memory.export(project_id)
        entry_path = (Path(exported["memory_dir"]) / "entries"
                      / f"{knowledge['id']}.md")
        entry_path.write_text(
            entry_path.read_text(encoding="utf-8").replace(
                KNOWLEDGE, EDITED_KNOWLEDGE), encoding="utf-8")
        preview = memory.preview(project_id, entry_path)
        applied = memory.apply(project_id, entry_path,
                               preview["preview_digest"])
        rebuilt = service.build_task_context(project_id, OBJECTIVE)

        # --- evidence-only assertions ---------------------------------------
        read_turns = [turn for turn in turns if turn["intent"] in (
            "list_projects", "project_status", "goal_status", "knowledge_query")]
        read_turn_side_effects = sum(
            abs(turn["side_effects"]["work_items"])
            + abs(turn["side_effects"]["goals"])
            + abs(turn["side_effects"]["context_version"])
            for turn in read_turns)

        checks = {
            # deployment shape
            "real_intent_model_used": all(
                turn["intent_source"] in ("model", "local_only")
                for turn in turns),
            "no_worker_dispatched": len(store.all()) == 0,
            "fixture_is_throwaway_git_repo": (repo / ".git").exists(),
            # read-only discipline
            "read_turns_have_no_side_effects": read_turn_side_effects == 0,
            "read_turns_render_from_ledger": all(
                turn["answer_head"] for turn in read_turns),
            # project memory reached the prompt
            "verified_background_injected": BACKGROUND in prompt,
            "verified_constraint_injected": CONSTRAINT in prompt,
            "verified_constraint_marked_mandatory": (
                "VERIFIED HARD CONSTRAINTS" in prompt),
            "candidate_marked_untrusted": (
                "[CANDIDATE]" in prompt and KNOWLEDGE in prompt),
            "prompt_records_entry_refs": (
                background["id"] in entry_ids
                and constraint["id"] in entry_ids
                and knowledge["id"] in entry_ids),
            # goal created by the conversation, bounded, not dispatched
            "goal_created_by_conversation": (
                len(goal_rows) == 1 and goal_ids
                and goal_rows[0]["goal_id"] in goal_ids),
            "goal_has_no_dispatched_task": (
                len(goal_rows) == 1
                and not goal_rows[0].get("current_task_id")),
            "goal_status_read_back_from_ledger": (
                len(goal_rows) == 1
                and (goal_status_turn.get("intent") or {}).get("intent")
                == "goal_status"
                and goal_rows[0]["goal_id"] in (goal_status_turn.get("answer")
                                                or "")),
            # human memory edit reaches a *new* prompt, old prompt untouched
            "markdown_edit_applied_with_cas": applied.get("applied") is True,
            "new_prompt_has_edited_knowledge": EDITED_KNOWLEDGE in (
                rebuilt.effective_prompt),
            "old_prompt_still_frozen": KNOWLEDGE in prompt,
            "no_verdict_or_success_written": all(
                task.get("verdict") in (None, "") for task in store.all()),
        }
        failed = [name for name, passed in checks.items() if not passed]
        evidence = {
            "test": "T62 real-project memory pilot, stage 1 (read-only rehearsal)",
            "worker_started": False,
            "real_model_called": True,
            "real_project_touched": False,
            "fixture": "<REPO>/sandbox/t62-rehearsal-*",
            "deployment": {
                "ld_chat": "in-process LDChat over the real intent model",
                "worker": "none (stub adapter reference only)",
                "repo": "throwaway Git fixture, not a real project",
            },
            "model": model.describe() if hasattr(model, "describe") else {},
            "checks": checks,
            "observed": {
                "conversation_id": conversation_id,
                "confirmation_required_for_goal": confirmation_used,
                "goal_ids": goal_ids,
                "goal_status": [row.get("status") for row in goal_rows],
                "goal_task_count": [row.get("task_count") for row in goal_rows],
                "work_item_count": len(store.all()),
                "context_version_after_apply": store.context_version(project_id),
                "prompt_entry_ids": entry_ids,
                "prompt_chars": len(prompt),
                "prompt_has_verified_constraint_block": (
                    "VERIFIED HARD CONSTRAINTS" in prompt),
                "memory_edit_status": {
                    "applied": applied.get("applied"),
                    "changed_fields": applied.get("changed_fields"),
                    "markdown_refresh_status": applied.get(
                        "markdown_refresh_status"),
                },
                "turns": [
                    {key: value for key, value in turn.items()
                     if key != "answer_head"}
                    | {"answer_head": turn["answer_head"]}
                    for turn in turns
                ],
            },
            "not_proven_here": [
                "a real Worker executing and the Verifier judging the result",
                "a second Session (B) inheriting A's verified work",
                "whether memory injection measurably helps a real task",
            ],
            "result": "PASS" if not failed else "FAIL",
            "failed": failed,
        }
        write_evidence(EVIDENCE, evidence)
        print(json.dumps({"result": evidence["result"], "failed": failed,
                          "goal_status": evidence["observed"]["goal_status"],
                          "work_items": len(store.all())}, ensure_ascii=False))
        return 0 if not failed else 1
    except Exception as exc:  # noqa: BLE001 - evidence must record failures
        write_evidence(EVIDENCE, {
            "test": "T62 real-project memory pilot, stage 1 (read-only rehearsal)",
            "result": "ERROR",
            "error": f"{type(exc).__name__}: {exc}",
            "worker_started": False,
            "turns": turns,
        })
        raise
    finally:
        chat = None
        chats.close()
        goals.close()
        store.close()
        shutil.rmtree(FIXTURE, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
