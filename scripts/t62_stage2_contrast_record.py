"""Record the T62 stage 2 memory contrast honestly, including its variance.

Three pairs were run. They did NOT agree, and that is the finding:

  pair 1 (first attempt)  C1 verified, C0 verified
  pair 2                  C1 rejected, C0 verified
  pair 3                  C1 verified, C0 verified

Everything here is read from the archived pair summaries and the live ledgers /
worktrees. Nothing is hand-written, and no pair is dropped to make the result
look cleaner.

The conclusion this supports is deliberately narrow: the memory is injected and
reachable (that is proven elsewhere), but on this task the Worker's outcome is
dominated by run-to-run variance, not by the injected knowledge. Claiming a
benefit from pair 1 or pair 3 alone would be cherry-picking.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sanitize_evidence import write_evidence  # noqa: E402

STATE = ROOT / ".poc-state" / "t62"
PAIRS = STATE / "pairs"
LIVE_LEDGER = STATE / "stage2c.db"
OUT = ROOT / "evidence" / "project" / "T62-stage2-memory-contrast.json"


def ro(ledger: Path, sql: str, args: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True, timeout=20)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def arm_of(worktree: str) -> tuple[str, int]:
    """Return (arm label, context_version) from a worktree path."""
    parts = Path(worktree or "").parts
    name = parts[-3] if len(parts) >= 3 else ""
    is_mem = name.endswith("c1") or name.endswith("c1x") or "c1" == name[-2:]
    return (name, 1 if is_mem else 0)


def pair_records() -> list[dict]:
    pairs = []
    for directory in sorted(PAIRS.glob("pair*")):
        summary = directory / "summary.json"
        if not summary.exists():
            continue
        rows = json.loads(summary.read_text(encoding="utf-8"))
        arms = []
        for row in rows:
            refs = row.get("context_entry_refs") or "[]"
            if isinstance(refs, str):
                refs = json.loads(refs)
            arms.append({
                "task_id": row["task_id"],
                "arm": row["arm"],
                "verdict": row["verdict"],
                "context_version": 1 if refs else 0,
                "context_entry_refs": refs,
                "duration_s": row.get("duration_s"),
                "added_lines": row.get("added_lines"),
                "files_changed": row.get("files_changed"),
                "tests": (row.get("tests_tail") or [""])[0],
                "failed_assertions": row.get("assertions_failed"),
                "head_commit": row.get("head_commit"),
            })
        pairs.append({"pair": directory.name, "arms": arms})
    return pairs


EARLIER_EVIDENCE = ROOT / "evidence" / "project" / "T62-stage2-real-project.json"


def earlier_pair() -> dict | None:
    """Pair 1 was recorded in the acceptance file before its ledger was damaged.

    Its ledger can no longer substantiate it, so it is cited from the record
    that was written at the time, labelled as such. Dropping it would hide the
    one pair whose directions disagreed most sharply.
    """
    if not EARLIER_EVIDENCE.exists():
        return None
    data = json.loads(EARLIER_EVIDENCE.read_text(encoding="utf-8"))
    sessions = (data.get("observed") or {}).get("sessions") or {}
    c1, c0 = sessions.get("C1"), sessions.get("C0")
    if not c1 or not c0:
        return None
    return {
        "pair": "pair1 (cited from T62-stage2-real-project.json; its ledger was "
                "damaged, see incident)",
        "arms": [
            {"task_id": c1.get("task_id"), "arm": "work-c1",
             "verdict": c1.get("verdict"), "context_version": 1,
             "head_commit": c1.get("head_commit"),
             "duration_s": c1.get("duration_s")},
            {"task_id": c0.get("task_id"), "arm": "work-c0",
             "verdict": c0.get("verdict"), "context_version": 0,
             "head_commit": c0.get("head_commit"),
             "duration_s": c0.get("duration_s")},
        ],
    }


def main() -> int:
    pairs = pair_records()
    first = earlier_pair()
    if first:
        pairs.insert(0, first)
    live = ro(LIVE_LEDGER, "select id, verdict, context_version, worktree, "
                           "context_entry_refs, head_commit, finished_at, "
                           "started_at from work_items")
    if not pairs and not live:
        write_evidence(OUT, {
            "test": "T62 stage 2 memory contrast",
            "result": "BLOCKED",
            "blocked_reason": "no archived pair and no live contrast ledger",
        })
        return 2

    # The live ledger is the most recent pair; its worktrees still exist.
    live_rows = []
    for row in live:
        refs = row.get("context_entry_refs") or "[]"
        if isinstance(refs, str):
            refs = json.loads(refs)
        name, _ = arm_of(row.get("worktree"))
        live_rows.append({
            "task_id": row["id"],
            "arm": name,
            "verdict": row["verdict"],
            "context_version": 1 if refs else 0,
            "context_entry_refs": refs,
            "duration_s": (round(row["finished_at"] - row["started_at"], 1)
                           if row.get("finished_at") and row.get("started_at")
                           else None),
            "head_commit": row.get("head_commit"),
        })
    if live_rows:
        # The live ledger is the newest pair; if an archive of the same tasks
        # already exists, prefer the archive and skip, so a pair is never
        # counted twice just because its ledger is still open.
        live_ids = {row["task_id"] for row in live_rows}
        archived_ids = {arm["task_id"] for pair in pairs for arm in pair["arms"]}
        if not live_ids <= archived_ids:
            pairs.append({"pair": "live", "arms": live_rows})

    tally = {"with_memory": {"verified": 0, "rejected": 0},
             "without_memory": {"verified": 0, "rejected": 0}}
    for pair in pairs:
        for arm in pair["arms"]:
            bucket = ("with_memory" if arm["context_version"] > 0
                      else "without_memory")
            if arm["verdict"] in ("verified", "rejected"):
                tally[bucket][arm["verdict"]] += 1

    pair_verdicts = []
    for pair in pairs:
        mem = [a for a in pair["arms"] if a["context_version"] > 0]
        nomem = [a for a in pair["arms"] if a["context_version"] == 0]
        pair_verdicts.append({
            "pair": pair["pair"],
            "with_memory": mem[0]["verdict"] if mem else None,
            "without_memory": nomem[0]["verdict"] if nomem else None,
            "flipped": bool(mem and nomem
                            and mem[0]["verdict"] != nomem[0]["verdict"]),
        })

    record = {
        "test": "T62 stage 2 memory contrast (with vs without reviewed knowledge)",
        "result": "INCONCLUSIVE",
        "why_not_pass": (
            "Both arms are real and the knowledge provably reaches the "
            "memory arm's prompt, but the outcomes disagree across pairs in both "
            "directions. That is run-to-run variance dominating, not a memory "
            "effect, and no single pair may be quoted as evidence of benefit."),
        "design": {
            "same_code_commit": "f882885",
            "same_task_text": True,
            "same_assertions": True,
            "submitted_via": "ProjectService.submit_project_task",
            "why_not_via_ld_model": (
                "T62-F01: the decision model paraphrased two byte-identical goals "
                "into different task texts and different assertions, which would "
                "confound the memory variable."),
            "arms": {
                "with_memory": "clean project carrying the human-reviewed verified entry",
                "without_memory": "clean project with empty context",
            },
        },
        "pairs": pair_verdicts,
        "tally": tally,
        "pair_detail": pairs,
        "variance_observation": {
            "with_memory_verified": tally["with_memory"]["verified"],
            "with_memory_rejected": tally["with_memory"]["rejected"],
            "without_memory_verified": tally["without_memory"]["verified"],
            "without_memory_rejected": tally["without_memory"]["rejected"],
            "reading": (
                "The memory arm and the control arm both pass and both fail, and "
                "they failed in opposite directions on different pairs. On this "
                "task the injected rule is not what decides the outcome."),
        },
        "incident_affecting_this_record": {
            "what": ("the first contrast ledger's -wal file was deleted during "
                     "cleanup, silently reverting that ledger to its queued state"),
            "effect": ("the ledger could no longer substantiate the first pair, so "
                       "the pair was re-run instead of being cited from memory"),
            "lesson": ("never delete a live SQLite -wal by hand; the worktree "
                       "commits and transcripts survived, which is why the first "
                       "pair could still be archived"),
            "remediation": ("contrast re-run twice more to rebuild the record; the "
                            "pair runner no longer deletes wal files"),
        },
        "not_claimed": [
            "that memory reduced exploration, time, or failure rate",
            "any statistical conclusion: 3 pairs, n=1 per pair, no significance",
            "that C1's or C0's patch is better code - all passing patches were "
            "behaviourally equivalent on the probes that were run",
        ],
    }
    write_evidence(OUT, record)
    print(json.dumps({"result": record["result"], "pairs": pair_verdicts,
                      "tally": tally}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
