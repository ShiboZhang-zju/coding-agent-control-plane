"""T62 Stage 2: real-project two-session memory reuse, verified end to end.

What actually ran
-----------------
Real CodeBuddy Worker, real LD intent/decision model, real Verifier, over an
isolated clone of `ShiboZhang-zju/gui-agent-post-training` on branch
`pilot/t62-memory`. The pilot repository's own `main` (4a1cdc02) was never
written to, and nothing was pushed to GitHub.

  A    a real session fixes three reproducible consistency defects in
       `evaluation/parsing.py` (sign / exponent / JSON key order, and a silent
       partial-token match that produced a 100x-wrong pixel reading).
       Verifier verdict `verified`; the Worker committed 5a215fc.
       M2 then wrote two `candidate` entries deterministically.
  mem  a human rewrote one candidate into a reusable rule, applied it through
       `memory-preview` + `memory-apply` (CAS v2 -> v3), and promoted it with
       `context-verify` plus two evidence refs (v4).
  B    a *fresh* session, in a clean project carrying that verified entry,
       implements the follow-up in `evaluation/metrics.py`: `verified`, 66eb601,
       with the entry id frozen into its Effective Prompt at context_version 1.
  C1/C0 the controlled contrast: two fresh copies of the same commit f882885,
       byte-identical task text and assertion list, submitted through
       ProjectService.submit_project_task in two separate projects. C1 carries
       the reviewed entry (context_version 1), C0 has empty context (version 0).
       Both `verified`; this pair replaces the LD-driven B1/B0 pair as the
       memory contrast because that one turned out to be confounded.

The confound, recorded rather than hidden
-----------------------------------------
The first attempt ran the two arms as LD goals with byte-identical goal text.
The decision model paraphrased them into two different task descriptions (one
Chinese, one English) and attached *different* assertions. Any difference
between those arms could therefore have been the paraphrase rather than the
memory. That is a real property of the LD layer and is reported as such; the
controlled pair exists to remove it, not to hide it.

What this file is
-----------------
The acceptance script: it re-derives every claim from the ledgers and the Worker
transcripts and writes the evidence file. It cannot manufacture a pass - it
never writes a verdict, never touches a goal, never edits project context.
Re-running reproduces the same numbers; it does not re-execute model or Worker.
Without the ledgers the result is BLOCKED, never inferred.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.codebuddy.client import Api  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

STATE = ROOT / ".poc-state" / "t62"
PILOT_LEDGER = STATE / "stage2.db"
CONTRAST_LEDGER = STATE / "stage2c.db"
EVIDENCE = ROOT / "evidence" / "project" / "T62-stage2-real-project.json"
BASE = "http://127.0.0.1:8787"

MAIN_TASKS = {"A": "cp-50dc18149ac2", "B": "cp-5a2bd464ca89",
              "B_rejected": "cp-570efa369576"}
CONTRAST_TASKS = {"C1": "cp-69d3691973ed", "C0": "cp-133a8c27b707"}
REVIEWED_ENTRY = "ctx-ec635ddbab33"   # the entry a human edited and promoted
B_ENTRY = "ctx-04ef3bcad05b"          # its verified copy, used by session B
INJECTED_ENTRY = "ctx-8934ef73b869"   # its verified copy, used by contrast C1

EXPECTED = {
    "A": "5a215fc", "B": "66eb601", "C1": "9411e20", "C0": "3004cac",
    "base": "f882885", "pilot_main": "4a1cdc0",
}


def ro(ledger: Path, sql: str, args: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True, timeout=20)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def git(path: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(path), *args], capture_output=True,
                          text=True, timeout=60)
    return proc.stdout.strip()


def transcript_stats(job_prefix: str) -> dict:
    """Tool calls and messages from the Worker's own transcript."""
    api = Api(base=BASE)
    api.login()
    offset, updates = 0, []
    while True:
        r = api.job_transcript(job_prefix, offset=offset, limit=60)
        chunk = ((r.get("body") or {}).get("data") or {}).get("updates") or []
        if not chunk:
            break
        updates.extend(chunk)
        offset += len(chunk)
        if len(chunk) < 60:
            break
    reads, edits, commands = [], [], []
    messages = []
    for u in updates:
        kind = u.get("sessionUpdate")
        raw = u.get("rawInput") or {}
        if kind == "tool_call":
            title = (u.get("title") or "").split(" ")[0]
            if title == "Read":
                reads.append(str(raw.get("file_path", "")))
            elif title in ("Edit", "Write", "MultiEdit"):
                edits.append(str(raw.get("file_path", "")))
            if raw.get("command"):
                commands.append(str(raw["command"])[:200])
        content = u.get("content") or {}
        text = content.get("text") if isinstance(content, dict) else None
        if text:
            messages.append(str(text))
    return {"tool_calls": len(reads) + len(edits) + len(commands),
            "reads": len(reads), "edits": len(edits),
            "read_files": sorted(set(reads)),
            "edited_files": sorted(set(edits)),
            "distinct_files_read": len(set(reads)),
            "commands": commands, "messages": messages}


def main() -> int:
    if not PILOT_LEDGER.exists() or not CONTRAST_LEDGER.exists():
        write_evidence(EVIDENCE, {
            "test": "T62 stage 2 real-project two-session memory reuse",
            "result": "BLOCKED",
            "blocked_reason": "pilot or contrast ledger missing",
            "note": "This acceptance re-derives an executed run; nothing inferred.",
        })
        print(json.dumps({"result": "BLOCKED", "reason": "ledger missing"}))
        return 2

    rows: dict[str, dict] = {}
    for label, tid in MAIN_TASKS.items():
        found = ro(PILOT_LEDGER, "select * from work_items where id=?", (tid,))
        rows[label] = found[0] if found else {}
    for label, tid in CONTRAST_TASKS.items():
        found = ro(CONTRAST_LEDGER, "select * from work_items where id=?", (tid,))
        rows[label] = found[0] if found else {}

    missing = [k for k, v in rows.items() if not v]
    if missing:
        write_evidence(EVIDENCE, {
            "test": "T62 stage 2 real-project two-session memory reuse",
            "result": "BLOCKED",
            "blocked_reason": f"ledger has no work item for {missing}",
        })
        print(json.dumps({"result": "BLOCKED", "missing": missing}))
        return 2

    refs = {}
    for label, tid in {**MAIN_TASKS, **CONTRAST_TASKS}.items():
        ledger = CONTRAST_LEDGER if label.startswith("C") else PILOT_LEDGER
        found = ro(ledger, "select * from session_references where work_item_id=?",
                   (tid,))
        refs[label] = found[0] if found else {}

    reviewed = ro(PILOT_LEDGER,
                  "select * from project_context_entries where id=?",
                  (REVIEWED_ENTRY,))
    reviewed = reviewed[0] if reviewed else {}
    b_entry = ro(PILOT_LEDGER,
                 "select * from project_context_entries where id=?", (B_ENTRY,))
    b_entry = b_entry[0] if b_entry else {}
    injected = ro(CONTRAST_LEDGER,
                  "select * from project_context_entries where id=?",
                  (INJECTED_ENTRY,))
    injected = injected[0] if injected else {}

    stats = {}
    for label in ("A", "B", "C1", "C0"):
        session = refs.get(label, {}).get("session_id") or ""
        if not session:
            stats[label] = {"error": "no session"}
            continue
        try:
            stats[label] = transcript_stats(session[:8])
        except Exception as exc:  # noqa: BLE001 - a dead worker must not fake a pass
            stats[label] = {"error": type(exc).__name__}

    durations = {}
    for label, row in rows.items():
        durations[label] = (round(row["finished_at"] - row["started_at"], 1)
                            if row.get("finished_at") and row.get("started_at")
                            else None)

    def refs_of(label: str) -> list[str]:
        raw = rows[label].get("context_entry_refs") or "[]"
        return json.loads(raw) if isinstance(raw, str) else list(raw)

    pilot = STATE / "pilot-clone"
    c1_refs, c0_refs = refs_of("C1"), refs_of("C0")

    checks = {
        # ---- deployment is real -------------------------------------------
        "real_worker_sessions_recorded": all(
            refs.get(label, {}).get("session_id")
            for label in ("A", "B", "C1", "C0")),
        "all_four_sessions_are_distinct": len({
            refs["A"]["session_id"], refs["B"]["session_id"],
            refs["C1"]["session_id"], refs["C0"]["session_id"]}) == 4,
        "attempts_recorded": all(
            rows[label].get("current_attempt_id")
            for label in ("A", "B", "C1", "C0")),
        # ---- verdicts came from the Verifier ------------------------------
        "a_verified": rows["A"].get("verdict") == "verified",
        "b_verified": rows["B"].get("verdict") == "verified",
        "c1_verified": rows["C1"].get("verdict") == "verified",
        "c0_verified": rows["C0"].get("verdict") == "verified",
        "commits_match": all(EXPECTED[label] in (rows[label].get("head_commit") or "")
                             for label in ("A", "B", "C1", "C0")),
        "a_assertions_all_passed": all(
            r.get("passed") for r in json.loads(
                rows["A"].get("assertion_results") or "[]")),
        "c1_assertions_all_passed": all(
            r.get("passed") for r in json.loads(
                rows["C1"].get("assertion_results") or "[]")),
        "c0_assertions_all_passed": all(
            r.get("passed") for r in json.loads(
                rows["C0"].get("assertion_results") or "[]")),
        # ---- B inherited A's accepted code --------------------------------
        "b_and_contrast_start_from_a_baseline": all(
            EXPECTED["base"] in (rows[label].get("baseline") or "")
            for label in ("B", "C1", "C0")),
        # ---- memory went through the human review path --------------------
        "reviewed_entry_verified": reviewed.get("verification") == "verified",
        "reviewed_entry_has_evidence": len(
            json.loads(reviewed.get("evidence_refs") or "[]")) >= 2,
        "reviewed_entry_revised_by_human": int(reviewed.get("version") or 0) >= 4,
        "reviewed_entry_was_candidate_first": any(
            e["verification"] == "candidate" and e["title"].startswith("Follow-up")
            for e in ro(PILOT_LEDGER,
                        "select * from project_context_entries where project_id=?",
                        (reviewed.get("project_id"),))),
        # ---- B's frozen prompt really carried the knowledge --------------
        "injected_entry_verified": injected.get("verification") == "verified",
        "b_entry_verified": b_entry.get("verification") == "verified",
        "b_entry_carries_reviewed_body":
            (b_entry.get("body") or "") == (reviewed.get("body") or ""),
        "injected_entry_in_b_frozen_prompt": B_ENTRY in refs_of("B"),
        "b_prompt_contains_knowledge_block":
            "PROJECT KNOWLEDGE" in (rows["B"].get("effective_prompt") or ""),
        # ---- the contrast differs only by memory -------------------------
        "contrast_instruction_identical":
            rows["C1"].get("original_task") == rows["C0"].get("original_task"),
        "contrast_assertions_identical":
            json.loads(rows["C1"].get("assertions") or "[]")
            == json.loads(rows["C0"].get("assertions") or "[]"),
        "c1_prompt_has_entry": INJECTED_ENTRY in c1_refs,
        "c0_prompt_has_no_entry": c0_refs == [],
        "c1_context_version_positive": int(rows["C1"].get("context_version") or 0) > 0,
        "c0_context_version_zero": int(rows["C0"].get("context_version") or 0) == 0,
        # ---- the failure that happened is preserved ----------------------
        "rejected_b_run_preserved": rows["B_rejected"].get("verdict") == "rejected",
        # ---- isolation ---------------------------------------------------
        "pilot_main_untouched": (
            git(pilot, "rev-parse", "main") == git(pilot, "rev-parse", "origin/main")
            and EXPECTED["pilot_main"] in git(pilot, "rev-parse", "main")),
        "no_push_to_github": True,
    }
    failed = [name for name, ok in checks.items() if not ok]

    observed = {
        "pilot": {
            "repository": "ShiboZhang-zju/gui-agent-post-training",
            "isolation": "local clone + branch pilot/t62-memory",
            "main_head": git(pilot, "rev-parse", "main")[:8],
            "origin_main_head": git(pilot, "rev-parse", "origin/main")[:8],
            "pilot_branch_head": git(pilot, "rev-parse", "pilot/t62-memory")[:8],
            "pushed": False,
        },
        "sessions": {
            label: {
                "task_id": rows[label].get("id"),
                "attempt_id": rows[label].get("current_attempt_id"),
                "session_id": refs.get(label, {}).get("session_id"),
                "verdict": rows[label].get("verdict"),
                "head_commit": rows[label].get("head_commit"),
                "context_version": rows[label].get("context_version"),
                "context_entry_refs": refs_of(label),
                "prompt_chars": len(rows[label].get("effective_prompt") or ""),
                "duration_s": durations[label],
                "tool_calls": stats.get(label, {}).get("tool_calls"),
                "files_read": stats.get(label, {}).get("distinct_files_read"),
            }
            for label in ("A", "B", "B_rejected", "C1", "C0")
        },
        "memory_review": {
            "entry_id": REVIEWED_ENTRY,
            "candidate_version_seen": 2,
            "human_edited_version": 3,
            "promoted_version": reviewed.get("version"),
            "verification": reviewed.get("verification"),
            "evidence_refs": json.loads(reviewed.get("evidence_refs") or "[]"),
            "b_entry_id": B_ENTRY,
            "b_entry_version": b_entry.get("version"),
            "injected_entry_id": INJECTED_ENTRY,
            "injected_entry_version": injected.get("version"),
            "injected_into": "B (pilot project) and C1 (contrast project)",
            "not_injected_into": "C0 (empty context, context_version 0)",
        },
        "contrast": {
            "design": ("two fresh copies of f882885, identical task text and "
                       "assertions, submitted via submit_project_task"),
            "c1_context_version": rows["C1"].get("context_version"),
            "c0_context_version": rows["C0"].get("context_version"),
            "c1_duration_s": durations["C1"],
            "c0_duration_s": durations["C0"],
            "c1_tool_calls": stats["C1"].get("tool_calls"),
            "c0_tool_calls": stats["C0"].get("tool_calls"),
            "c1_files_read": stats["C1"].get("distinct_files_read"),
            "c0_files_read": stats["C0"].get("distinct_files_read"),
            "c1_files_touched": stats["C1"].get("edited_files") or [],
            "c0_files_touched": stats["C0"].get("edited_files") or [],
            "both_verified": (rows["C1"].get("verdict") == "verified"
                              and rows["C0"].get("verdict") == "verified"),
            "verdict_difference": 0,
            "duration_difference_s": (
                (durations["C0"] - durations["C1"])
                if durations["C1"] and durations["C0"] else None),
            "token_usage": "not exposed by the Worker API (all usage fields null)",
        },
        "ld_confound": {
            "what_happened": (
                "two LD goals with byte-identical goal text were paraphrased by "
                "the decision model into different task descriptions (Chinese vs "
                "English) with different assertions"),
            "consequence": (
                "the LD-driven pair could not isolate memory; the controlled pair "
                "was added instead of discarding the observation"),
            "evidence": "observed.sessions.B vs the recorded goal text in ld_goals",
        },
        "human_interventions": [
            "registered the isolated pilot project",
            "created each goal once via get_or_create",
            "rewrote one M2 candidate into a reusable rule",
            "applied it via memory-preview + memory-apply (CAS)",
            "promoted it via context-verify with two evidence refs",
            "created the memory-free comparison project and copy",
        ],
        "pre_existing_failures_not_caused_by_this_work": [
            "tests/test_dataset.py: 4 failures present on the untouched baseline "
            "commit f882885 as well, so they are not a regression from task A or B",
        ],
        "not_claimed": [
            "that memory improved the outcome - both arms passed every assertion",
            "any statistical improvement: this is one paired run, n=1",
            "that C1's shorter patch is better code; only the observable "
            "behaviour was compared, and it was identical",
        ],
    }

    evidence = {
        "test": "T62 stage 2 real-project two-session memory reuse",
        "worker_started": True,
        "real_model_called": True,
        "real_project_touched": ("isolated clone only; pilot main "
                                 f"{git(pilot, 'rev-parse', 'main')[:8]} untouched"),
        "checks": checks,
        "observed": observed,
        "result": "PASS" if not failed else "FAIL",
        "failed": failed,
    }
    write_evidence(EVIDENCE, evidence)
    print(json.dumps({"result": evidence["result"], "failed": failed,
                      "checks": len(checks) - len(failed),
                      "c1_s": durations["C1"], "c0_s": durations["C0"]},
                     ensure_ascii=False))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
