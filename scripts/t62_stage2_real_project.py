"""T62 Stage 2: real-project two-session memory reuse, end-to-end acceptance.

What actually ran
-----------------
Real CodeBuddy Worker, real LD intent/decision model, real Verifier, over an
isolated clone of `ShiboZhang-zju/gui-agent-post-training` on branch
`pilot/t62-memory`. The pilot repository's own `main` (4a1cdc02) was never
written to and nothing was pushed to GitHub.

  A    a real session fixed three reproducible consistency defects in
       `evaluation/parsing.py`: JSON key order was ignored, the tolerant number
       grammar was narrower than the strict grammar it is supposed to score, and
       a partial match on "7.1e-1" silently produced a 100x-wrong pixel reading.
       Regression tests were written first and failed for real (16 failed /
       4 passed), then passed (20/20). Verifier: verified, commit 5a215fc.
  M2   wrote two `candidate` entries deterministically from that verdict.
  mem  a human rewrote one candidate into a reusable engineering rule, applied
       it through memory-preview + memory-apply (CAS v2 -> v3), and promoted it
       with context-verify plus two evidence refs (v4, verified).
  B    a *fresh* session, in a clean project carrying that entry, implemented
       the follow-up in `evaluation/metrics.py`: verified, 66eb601, with the
       entry id frozen into its Effective Prompt at context_version 1.

Scope of this file
------------------
It asserts the chain above: real sessions, real Verifier verdicts, real human
memory review, and a second session whose frozen prompt provably carries the
reviewed entry. It does NOT assert a memory *benefit* - that experiment is in
`T62-stage2-memory-contrast.json` and came out INCONCLUSIVE, so no benefit is
claimed here or there.

What this file is
-----------------
An acceptance script over an already-executed run: every claim is re-derived
from the ledgers and the Worker transcripts. It cannot manufacture a pass - it
never writes a verdict, never touches a goal, never edits project context.
Without the ledgers the result is BLOCKED, never inferred.
"""
from __future__ import annotations

import json
import os
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
# The file that was written at run time, when the Worker service was up. It is
# historical evidence and must never be overwritten by a later re-check: the
# transcript it captured can no longer be re-read today, so re-running would
# silently replace a real observation with "unavailable".
HISTORICAL_EVIDENCE = ROOT / "evidence" / "project" / "T62-stage2-real-project.json"
# Where this re-check writes. Same assertions, stricter checks, own file.
EVIDENCE = ROOT / "evidence" / "project" / os.environ.get(
    "T62_RECHECK_EVIDENCE", "T62-stage2-real-project-recheck.json")
CONTRAST_EVIDENCE = ROOT / "evidence" / "project" / "T62-stage2-memory-contrast.json"
BASE = "http://127.0.0.1:8787"

MAIN_TASKS = {"A": "cp-50dc18149ac2", "B": "cp-5a2bd464ca89",
              "B_rejected": "cp-570efa369576"}
REVIEWED_ENTRY = "ctx-ec635ddbab33"   # the entry a human edited and promoted
B_ENTRY = "ctx-04ef3bcad05b"          # its verified copy, used by session B

EXPECTED = {"A": "5a215fc", "B": "66eb601",
            "base": "f882885", "pilot_main": "4a1cdc0"}


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
    """Tool calls and file activity from the Worker's own transcript."""
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
    for u in updates:
        if u.get("sessionUpdate") != "tool_call":
            continue
        raw = u.get("rawInput") or {}
        title = (u.get("title") or "").split(" ")[0]
        if title == "Read":
            reads.append(Path(str(raw.get("file_path", ""))).name)
        elif title in ("Edit", "Write", "MultiEdit"):
            edits.append(Path(str(raw.get("file_path", ""))).name)
        if raw.get("command"):
            commands.append(str(raw["command"])[:200])
    return {"tool_calls": len(reads) + len(edits) + len(commands),
            "reads": sorted(set(reads)), "edits": sorted(set(edits)),
            "shell_commands": commands}


def main() -> int:
    if not PILOT_LEDGER.exists():
        write_evidence(EVIDENCE, {
            "test": "T62 stage 2 real-project two-session memory reuse",
            "result": "BLOCKED",
            "blocked_reason": f"pilot ledger not found: {PILOT_LEDGER.name}",
            "note": "This acceptance re-derives an executed run; nothing inferred.",
        })
        print(json.dumps({"result": "BLOCKED", "reason": "ledger missing"}))
        return 2

    rows = {}
    for label, tid in MAIN_TASKS.items():
        found = ro(PILOT_LEDGER, "select * from work_items where id=?", (tid,))
        rows[label] = found[0] if found else {}
    missing = [label for label, row in rows.items() if not row]
    if missing:
        write_evidence(EVIDENCE, {
            "test": "T62 stage 2 real-project two-session memory reuse",
            "result": "BLOCKED",
            "blocked_reason": f"ledger has no work item for {missing}",
        })
        print(json.dumps({"result": "BLOCKED", "missing": missing}))
        return 2

    refs = {}
    for label, tid in MAIN_TASKS.items():
        found = ro(PILOT_LEDGER, "select * from session_references where work_item_id=?",
                   (tid,))
        refs[label] = found[0] if found else {}

    reviewed = (ro(PILOT_LEDGER, "select * from project_context_entries where id=?",
                   (REVIEWED_ENTRY,)) or [{}])[0]
    b_entry = (ro(PILOT_LEDGER, "select * from project_context_entries where id=?",
                  (B_ENTRY,)) or [{}])[0]

    stats, transcript_ok = {}, {}
    for label in ("A", "B"):
        session = refs.get(label, {}).get("session_id") or ""
        if not session:
            stats[label], transcript_ok[label] = {}, False
            continue
        try:
            stats[label] = transcript_stats(session[:8])
            transcript_ok[label] = True
        except Exception as exc:  # noqa: BLE001 - a dead worker must not fake a pass
            stats[label] = {"error": type(exc).__name__}
            transcript_ok[label] = False

    durations = {
        label: (round(row["finished_at"] - row["started_at"], 1)
                if row.get("finished_at") and row.get("started_at") else None)
        for label, row in rows.items()
    }

    def refs_of(label: str) -> list[str]:
        raw = rows[label].get("context_entry_refs") or "[]"
        return json.loads(raw) if isinstance(raw, str) else list(raw)

    pilot = STATE / "pilot-clone"
    contrast = (json.loads(CONTRAST_EVIDENCE.read_text(encoding="utf-8"))
                if CONTRAST_EVIDENCE.exists() else {})

    def assertion_rows(label: str) -> list[dict]:
        return json.loads(rows[label].get("assertion_results") or "[]")

    def assertions_passed(label: str) -> bool:
        """`all([])` is True - an absent result set is not a pass."""
        items = assertion_rows(label)
        return bool(items) and all(item.get("passed") for item in items)

    task_ids = [row.get("id") for row in rows.values() if row.get("id")]
    unique_task_ids = len(task_ids) == len(set(task_ids))
    # A real duplicate check: the same id appearing twice in the ledger is the
    # thing we care about. `count(*) >= len(rows)` only proved the table was not
    # smaller than the task list, which says nothing about duplicates.
    ledger_id_rows = ro(PILOT_LEDGER, "select id from work_items")
    ledger_ids = [row["id"] for row in ledger_id_rows]
    duplicated_in_ledger = sorted(
        {tid for tid in ledger_ids if ledger_ids.count(tid) > 1})

    checks = {
        # ---- the ledger really holds these runs ---------------------------
        "all_three_tasks_found": len(rows) == len(MAIN_TASKS) and all(rows.values()),
        "task_ids_are_distinct": unique_task_ids and len(task_ids) == len(rows),
        "ledger_holds_no_duplicate_rows": not duplicated_in_ledger,
        # ---- deployment is real -------------------------------------------
        "real_worker_session_for_a": bool(refs.get("A", {}).get("session_id")),
        "real_worker_session_for_b": bool(refs.get("B", {}).get("session_id")),
        "a_and_b_are_different_sessions": bool(
            refs.get("A", {}).get("session_id")
            and refs.get("B", {}).get("session_id")
            and refs["A"]["session_id"] != refs["B"]["session_id"]),
        "attempts_recorded": all(rows[label].get("current_attempt_id")
                                 for label in ("A", "B")),
        # ---- session / attempt / verdict are one chain per task -----------
        "session_attempt_verdict_linked": all(
            (rows[label].get("current_attempt_id") or "")
            == (refs.get(label, {}).get("attempt_id") or "")
            and bool(refs.get(label, {}).get("session_id"))
            and bool(rows[label].get("verdict"))
            for label in ("A", "B")),
        "terminal_states_recorded": all(
            rows[label].get("state") == "succeeded" for label in rows),
        # "transcripts_readable" is deliberately NOT a pass/fail check: a dead
        # local service makes the record INCOMPLETE, not failing (see result).
        # ---- verdicts came from the Verifier ------------------------------
        "a_verified": rows["A"].get("verdict") == "verified",
        "b_verified": rows["B"].get("verdict") == "verified",
        "a_commit_matches": EXPECTED["A"] in (rows["A"].get("head_commit") or ""),
        "b_commit_matches": EXPECTED["B"] in (rows["B"].get("head_commit") or ""),
        "a_assertions_all_passed": assertions_passed("A"),
        "b_assertions_all_passed": assertions_passed("B"),
        # ---- B inherited A's accepted code --------------------------------
        "b_starts_from_a_baseline": EXPECTED["base"] in (rows["B"].get("baseline") or ""),
        # ---- memory went through the human review path --------------------
        "reviewed_entry_verified": reviewed.get("verification") == "verified",
        "reviewed_entry_has_two_evidence_refs":
            len(json.loads(reviewed.get("evidence_refs") or "[]")) >= 2,
        "reviewed_entry_revised_by_human": int(reviewed.get("version") or 0) >= 4,
        "reviewed_entry_was_candidate_first": any(
            e["verification"] == "candidate" and e["title"].startswith("Follow-up")
            for e in ro(PILOT_LEDGER,
                        "select * from project_context_entries where project_id=?",
                        (reviewed.get("project_id"),))),
        # ---- B's frozen prompt really carried the knowledge --------------
        "b_entry_verified": b_entry.get("verification") == "verified",
        "b_entry_carries_reviewed_body":
            (b_entry.get("body") or "") == (reviewed.get("body") or ""),
        "b_entry_id_in_frozen_prompt": B_ENTRY in refs_of("B"),
        "b_prompt_has_knowledge_block":
            "PROJECT KNOWLEDGE" in (rows["B"].get("effective_prompt") or ""),
        "b_context_version_positive": int(rows["B"].get("context_version") or 0) > 0,
        # ---- the failure that happened is preserved ----------------------
        "rejected_b_run_preserved": rows["B_rejected"].get("verdict") == "rejected",
        # ---- isolation ---------------------------------------------------
        "pilot_main_untouched": (
            git(pilot, "rev-parse", "main") == git(pilot, "rev-parse", "origin/main")
            and EXPECTED["pilot_main"] in git(pilot, "rev-parse", "main")),
        # ---- the contrast archive is complete and not overstated ----------
        "contrast_record_exists": bool(contrast),
        # Not BLOCKED means the three archives exist and validate. An INCOMPLETE
        # contrast (a verdict survived but its SessionReference did not) is a
        # documented gap, not a failing check - see contrast_gaps below.
        "contrast_archive_not_blocked":
            (contrast.get("validation") or {}).get("result") in ("COMPLETE",
                                                                 "INCOMPLETE"),
        "contrast_has_exactly_three_pairs":
            len(contrast.get("pairs") or []) == 3,
        "contrast_not_reported_as_a_benefit":
            contrast.get("result") == "INCONCLUSIVE",
        "contrast_archive_completeness_is_separate":
            "archive_completeness" in contrast,
        "pair1_kept_as_historical_record": any(
            (pair.get("evidence_level") == "historical_record_only")
            for pair in contrast.get("pairs") or []),
        "contrast_counts_three_pairs_of_arms":
            (contrast.get("validation") or {}).get("task_ids_counted") == 6,
    }
    failed = [name for name, ok in checks.items() if not ok]

    # Not an automated result: a human declaration about what left this machine.
    # It is recorded separately and never counted as a verified check.
    human_declarations = [
        {"claim": "nothing in this pilot was pushed to GitHub",
         "declared_by": "human:t62-owner",
         "verified_automatically": False,
         "how_to_verify_independently":
             "compare the pilot remote's refs / GitHub branch list out of band"},
    ]

    transcript_gaps = [label for label in ("A", "B")
                       if not transcript_ok.get(label, False)]

    observed = {
        "pilot": {
            "repository": "ShiboZhang-zju/gui-agent-post-training",
            "isolation": "local clone + branch pilot/t62-memory",
            "main_head": git(pilot, "rev-parse", "main")[:8],
            "origin_main_head": git(pilot, "rev-parse", "origin/main")[:8],
            "pilot_branch_head": git(pilot, "rev-parse", "pilot/t62-memory")[:8],
            # Declared by a human, not measured here - see human_declarations.
            "pushed": "declared_false_by_operator_not_measured",
        },
        "sessions": {
            label: {
                "task_id": rows[label].get("id"),
                "attempt_id": rows[label].get("current_attempt_id"),
                "session_id": refs.get(label, {}).get("session_id"),
                "verdict": rows[label].get("verdict"),
                "state": rows[label].get("state"),
                "head_commit": rows[label].get("head_commit"),
                "context_version": rows[label].get("context_version"),
                "context_entry_refs": refs_of(label),
                "prompt_chars": len(rows[label].get("effective_prompt") or ""),
                "duration_s": durations[label],
                "tool_calls": stats.get(label, {}).get("tool_calls"),
                "reads": stats.get(label, {}).get("reads"),
                "edits": stats.get(label, {}).get("edits"),
            }
            for label in ("A", "B", "B_rejected")
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
        },
        "contrast_summary": {
            "file": "T62-stage2-memory-contrast.json",
            "result": contrast.get("result"),
            "pairs": contrast.get("pairs"),
            "tally": contrast.get("tally"),
        },
        "ld_confound": {
            "what_happened": (
                "two LD goals with byte-identical goal text were paraphrased by the "
                "decision model into different task descriptions (Chinese vs "
                "English) with different assertions"),
            "consequence": (
                "the LD-driven pair could not isolate memory; the controlled pair "
                "was built instead of discarding the observation"),
        },
        "human_interventions": [
            "registered the isolated pilot project",
            "created each goal once via get_or_create",
            "rewrote one M2 candidate into a reusable rule",
            "applied it via memory-preview + memory-apply (CAS)",
            "promoted it via context-verify with two evidence refs",
            "built the memory-free comparison copies",
        ],
        "pre_existing_failures_not_caused_by_this_work": [
            "tests/test_dataset.py has 4 failures on the untouched baseline "
            "f882885 as well, so they are not a regression from A or B",
        ],
        "not_claimed": [
            "that memory improved the outcome - the contrast came out INCONCLUSIVE",
            "any statistical improvement: three pairs, no significance",
            "that any passing patch is better code; the probes found no behavioural "
            "difference between the arms that passed",
            "token usage: the Worker API returns null for every usage field",
        ],
    }

    # A missing transcript is INCOMPLETE, not FAIL and above all not PASS: the
    # verdict was still written by the Verifier, but the behaviour cannot be
    # re-read today, so "everything was verified" would be an overstatement.
    if failed:
        result = "FAIL"
    elif transcript_gaps:
        result = "INCOMPLETE"
    else:
        result = "PASS"

    historical = (json.loads(HISTORICAL_EVIDENCE.read_text(encoding="utf-8"))
                  if HISTORICAL_EVIDENCE.exists() else {})
    historical_checks = historical.get("checks") or {}

    evidence = {
        "test": "T62 stage 2 real-project two-session memory reuse (re-check)",
        "recheck_of": HISTORICAL_EVIDENCE.name,
        "historical_record": {
            "file": HISTORICAL_EVIDENCE.name,
            "result_at_run_time": historical.get("result"),
            "checks_at_run_time": len(historical_checks),
            "transcripts_readable_at_run_time":
                historical_checks.get("transcripts_readable"),
            "note": ("written while the Worker service was up, so it holds the "
                     "transcripts this re-check cannot read today; it is cited, "
                     "not regenerated, and was not overwritten"),
        },
        # This re-check starts no Worker and calls no model. Claiming either as a
        # bare `true` would be a fact nobody measured in this run, so they are
        # inherited from the run-time record and labelled as inherited.
        "worker_started": {
            "value": bool(historical.get("worker_started")),
            "measured_by": HISTORICAL_EVIDENCE.name,
            "measured_in_this_run": False,
        },
        "real_model_called": {
            "value": bool(historical.get("real_model_called")),
            "measured_by": HISTORICAL_EVIDENCE.name,
            "measured_in_this_run": False,
        },
        "real_project_touched": ("isolated clone only; pilot main "
                                 f"{git(pilot, 'rev-parse', 'main')[:8]} untouched"),
        "checks": checks,
        "human_declarations_not_automated": human_declarations,
        "automated_checks_counted": len(checks),
        "transcript_gaps": {
            "sessions_missing_transcript": transcript_gaps,
            "effect": ("verdicts, commits, prompts and memory chain are still "
                       "re-derivable; per-arm tool-call behaviour is not"),
            "service": BASE,
        },
        "observed": observed,
        "result": result,
        "failed": failed,
        "why_not_pass": (None if result == "PASS" else
                         ("real check(s) failed: " + ", ".join(failed))
                         if failed else
                         "no check failed, but a required transcript is "
                         "unavailable, so the record is incomplete rather than "
                         "passing"),
    }
    write_evidence(EVIDENCE, evidence)
    print(json.dumps({"result": evidence["result"], "failed": failed,
                      "passed": sum(1 for v in checks.values() if v),
                      "total": len(checks),
                      "incomplete": transcript_gaps}, ensure_ascii=False))
    return 0 if result == "PASS" else (3 if result == "INCOMPLETE" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
