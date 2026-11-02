"""Generate the T62 stage 2 supporting record from the pair archives.

The main acceptance file answers "did the required facts hold". This one keeps
the *behavioural* detail - how many files each arm read, what it edited, how
long it ran - because that is what a reader needs to judge whether memory was
actually used.

Where the numbers come from, and what happens when they cannot
--------------------------------------------------------------
Behaviour is read from the Worker's own job transcript at the local service.
The service is usually NOT running when this record is regenerated, and pair 1's
sessions were never preserved at all. In both cases the transcript is recorded as
``unavailable`` with the reason; a tool-call count is never inferred, estimated
or copied from a different arm. A record whose transcripts cannot be read is
``INCOMPLETE``, not ``PASS``.

Pair 1 is read from its archive (``historical_record_only``): it contributes its
verdict, commit and duration because those survived, and nothing else.

Raw transcripts are not committed (absolute workspace paths); the sanitizer runs
over everything that is written here, so shell commands keep their meaning but
lose the machine prefix.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from sanitize_evidence import write_evidence  # noqa: E402
import t62_evidence_model as model  # noqa: E402

STATE = ROOT / ".poc-state" / "t62"
ARCHIVE_DIR = ROOT / "evidence" / "project" / "t62"
OUT = ROOT / "evidence" / "project" / "T62-stage2-pilot-transcripts.json"
BASE = "http://127.0.0.1:8787"

# Observations that were really read at run time (service up) are facts. The
# service is down now, so they cannot be re-read - they are carried forward
# verbatim and labelled, never re-derived and never dropped.
INHERITABLE = ("tool_calls", "reads", "edits", "shell_commands")


def prior_observations() -> dict[str, dict]:
    """Index the previous file's arms by task_id *and* by label.

    The pre-G1 file keyed its arms by label ("A", "B"), the new one keys by
    task_id; both must find the same real observation.
    """
    if not OUT.exists():
        return {}
    try:
        data = json.loads(OUT.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    index: dict[str, dict] = {}
    for key, arm in (data.get("arms") or {}).items():
        if not isinstance(arm, dict):
            continue
        index.setdefault(key, arm)
        if arm.get("task_id"):
            index.setdefault(arm["task_id"], arm)
    return index

PURPOSE = {
    "cp-50dc18149ac2": "session A: fix parsing.py boundary defects",
    "cp-570efa369576": "B's first attempt: committed outside its worktree, rejected",
    "cp-5a2bd464ca89": "session B: follow-up in metrics.py with reviewed memory",
    "cp-69d3691973ed": "pair1 with memory (archived)",
    "cp-133a8c27b707": "pair1 without memory (archived)",
}

# The two-session pilot's own tasks. They are not part of the three contrast
# pairs, but they ARE real, still in stage2.db, and they are what the memory
# chain claim rests on - so they belong in the behaviour record.
PILOT_LEDGER = STATE / "stage2.db"
PILOT_TASKS = {
    "A": ("cp-50dc18149ac2", "without_memory"),
    "B_rejected": ("cp-570efa369576", "with_memory"),
    "B": ("cp-5a2bd464ca89", "with_memory"),
}


def transcript_stats(job_prefix: str) -> dict:
    """Tool calls and file activity from the Worker's own transcript."""
    from adapters.codebuddy.client import Api

    api = Api(base=BASE)
    api.login()
    offset, updates = 0, []
    while True:
        response = api.job_transcript(job_prefix, offset=offset, limit=60)
        chunk = ((response.get("body") or {}).get("data") or {}).get("updates") or []
        if not chunk:
            break
        updates.extend(chunk)
        offset += len(chunk)
        if len(chunk) < 60:
            break

    reads, edits, commands = [], [], []
    for update in updates:
        if update.get("sessionUpdate") != "tool_call":
            continue
        raw = update.get("rawInput") or {}
        title = (update.get("title") or "").split(" ")[0]
        if title == "Read":
            reads.append(Path(str(raw.get("file_path", ""))).name)
        elif title in ("Edit", "Write", "MultiEdit"):
            edits.append(Path(str(raw.get("file_path", ""))).name)
        if raw.get("command"):
            commands.append(str(raw["command"])[:200])
    if not updates:
        return {"transcript": "unavailable",
                "transcript_reason": "service returned no updates for this session"}
    return {"transcript": "available",
            "tool_calls": len(reads) + len(edits) + len(commands),
            "reads": sorted(set(reads)),
            "edits": sorted(set(edits)),
            "shell_commands": commands}


def unavailable(reason: str) -> dict:
    return {"transcript": "unavailable", "transcript_reason": reason,
            "tool_calls": None, "reads": [], "edits": [], "shell_commands": []}


def collect_arm(pair_id: str, arm: dict) -> dict:
    record = {
        "pair": pair_id,
        "task_id": arm.get("task_id"),
        "purpose": PURPOSE.get(arm.get("task_id"), ""),
        "role": arm.get("role"),
        "verdict": arm.get("verdict"),
        "state": arm.get("state"),
        "context_version": arm.get("context_version"),
        "head_commit": arm.get("head_commit"),
        "duration_s": arm.get("duration_s"),
        "evidence_level": None,
        "evidence_level_of_pair": None,
    }
    return record


def main() -> int:
    pairs, _ = model.collect_archives(STATE, ARCHIVE_DIR)
    validation = model.validate_archives(pairs)

    if validation["result"] == model.BLOCKED:
        write_evidence(OUT, {
            "test": "T62 stage 2 supporting record: worker behaviour per arm",
            "result": model.BLOCKED,
            "blocked_reason": validation["problems"],
            "note": ("Behaviour is only reported for pairs whose verdict archive "
                     "exists and validates. Nothing is inferred."),
        })
        print(json.dumps({"result": model.BLOCKED,
                          "problems": validation["problems"]}, ensure_ascii=False))
        return 2

    prior = prior_observations()
    arms: dict[str, dict] = {}
    transcript_gaps: list[str] = []
    inherited: list[str] = []
    service_error = None

    def carry_forward(entry: dict, *, source: str) -> bool:
        """Reuse a real run-time observation instead of overwriting it with null.

        The pre-G1 file has no `transcript` key at all - it simply had numbers,
        because they were read while the service was up. A non-null `tool_calls`
        together with `reads`/`edits`/`shell_commands` is that same observation.
        Lookup is by task id first, then by the pilot label the old file used.
        """
        old = (prior.get(entry.get("task_id") or "")
               or prior.get(entry.get("label") or ""))
        if not old:
            return False
        legacy_observation = (old.get("transcript") in (None, "available")
                              and old.get("tool_calls") is not None)
        if not legacy_observation:
            return False
        for key in INHERITABLE:
            if key not in old:
                return False
        for key in INHERITABLE:
            entry[key] = old[key]
        entry["transcript"] = "available"
        entry["transcript_source"] = source
        entry.pop("transcript_reason", None)
        inherited.append(entry.get("task_id") or "")
        return True

    RUN_TIME = ("read from the Worker job API when the service was up; carried "
                "forward verbatim because the service is not running now")
    CONTEMPORANEOUS = ("read from the contemporaneous supporting record written "
                       "at run time; the sessions no longer exist, so this is "
                       "cited, not re-measured")

    for pair_id in model.PAIR_IDS:
        record = pairs.get(pair_id) or {}
        level = record.get("evidence_level")
        for arm in record.get("arms") or []:
            entry = collect_arm(pair_id, arm)
            entry["evidence_level_of_pair"] = level
            if level == "historical_record_only":
                entry["evidence_level"] = "historical_record_only"
                entry.update(unavailable(
                    "this pair's sessions were never preserved: its ledger lost "
                    "its -wal file and only the contemporaneous record survives"))
                if not carry_forward(entry, source=CONTEMPORANEOUS):
                    transcript_gaps.append(arm.get("task_id") or "")
            elif not arm.get("session_id"):
                entry["evidence_level"] = "session_reference_not_retained"
                entry.update(unavailable(
                    "no SessionReference survives for this task; its ledger was "
                    "reset for a later pair"))
                transcript_gaps.append(arm.get("task_id") or "")
            else:
                try:
                    entry.update(transcript_stats(arm["session_id"][:8]))
                except Exception as exc:  # noqa: BLE001 - a dead service must not fake data
                    service_error = f"{type(exc).__name__}: {exc}"
                    entry["evidence_level"] = "transcript_unavailable"
                    entry.update(unavailable(
                        "the local CodeBuddy service was unreachable, so the "
                        "transcript could not be read"))
                    if not carry_forward(entry, source=RUN_TIME):
                        transcript_gaps.append(arm.get("task_id") or "")
                else:
                    entry["evidence_level"] = (
                        "live_transcript" if entry.get("transcript") == "available"
                        else "transcript_empty")
                    if entry.get("transcript") != "available" and not carry_forward(
                            entry, source=RUN_TIME):
                        transcript_gaps.append(arm.get("task_id") or "")
            arms[entry["task_id"] or f"{pair_id}-unknown"] = entry

    # The two-session pilot tasks: real rows in stage2.db, so their identity,
    # verdict and duration are re-derived rather than cited.
    for label, (task_id, role) in PILOT_TASKS.items():
        found = model.read_pilot_row(PILOT_LEDGER, task_id)
        entry = {
            "pair": "pilot",
            "label": label,
            "task_id": task_id,
            "purpose": PURPOSE.get(task_id, ""),
            "role": role,
            "verdict": found.get("verdict"),
            "state": found.get("state"),
            "context_version": found.get("context_version"),
            "head_commit": found.get("head_commit"),
            "duration_s": found.get("duration_s"),
            "prompt_chars": found.get("prompt_chars"),
            "evidence_level": "live_ledger_read",
            "evidence_level_of_pair": "live_ledger_verified",
        }
        if not found:
            entry["evidence_level"] = "ledger_row_missing"
            entry.update(unavailable(f"{task_id} is not in stage2.db"))
            transcript_gaps.append(task_id)
        elif not found.get("session_id"):
            entry["evidence_level"] = "session_reference_not_retained"
            entry.update(unavailable("no SessionReference for this task"))
            if not carry_forward(entry, source=RUN_TIME):
                transcript_gaps.append(task_id)
        else:
            try:
                entry.update(transcript_stats(found["session_id"][:8]))
            except Exception as exc:  # noqa: BLE001
                service_error = f"{type(exc).__name__}: {exc}"
                entry["evidence_level"] = "transcript_unavailable"
                entry.update(unavailable(
                    "the local CodeBuddy service was unreachable, so the "
                    "transcript could not be read"))
                if not carry_forward(entry, source=RUN_TIME):
                    transcript_gaps.append(task_id)
            else:
                if entry.get("transcript") != "available" and not carry_forward(
                        entry, source=RUN_TIME):
                    transcript_gaps.append(task_id)
        arms[task_id] = entry

    readable = [tid for tid, arm in arms.items()
                if arm.get("transcript") == "available"]

    per_pair = {}
    for pair_id in model.PAIR_IDS:
        pair_arms = [arm for arm in arms.values() if arm["pair"] == pair_id]
        with_mem = next((a for a in pair_arms
                         if a.get("role") == model.ROLE_WITH_MEMORY), {})
        without = next((a for a in pair_arms
                        if a.get("role") == model.ROLE_WITHOUT_MEMORY), {})
        pair_arms_reads_known = all(a.get("transcript") == "available"
                                    for a in pair_arms)
        per_pair[pair_id] = {
            "with_memory_verdict": with_mem.get("verdict"),
            "without_memory_verdict": without.get("verdict"),
            "with_memory_duration_s": with_mem.get("duration_s"),
            "without_memory_duration_s": without.get("duration_s"),
            "with_memory_reads": (len(with_mem.get("reads") or [])
                                  if with_mem.get("transcript") == "available"
                                  else None),
            "without_memory_reads": (len(without.get("reads") or [])
                                     if without.get("transcript") == "available"
                                     else None),
            "behaviour_observable": pair_arms_reads_known,
        }

    behaviour_complete = bool(arms) and not transcript_gaps
    result = model.COMPLETE if behaviour_complete and \
        validation["result"] == model.COMPLETE else model.INCOMPLETE

    record = {
        "test": "T62 stage 2 supporting record: worker behaviour per arm",
        "result": result,
        "validation": {
            "archive_validation": validation["result"],
            "arms_recorded": len(arms),
            "arms_with_readable_transcript": len(readable),
            "arms_without_transcript": sorted(set(filter(None, transcript_gaps))),
            "arms_carried_forward_from_run_time": sorted(set(inherited)),
            "service_error": service_error,
            "service_reachable": service_error is None,
        },
        "derived_from": [
            "evidence/project/t62/pair*.json (per-pair verdict archives)",
            "Worker job transcripts at the local CodeBuddy service, when it is up",
        ],
        "note": ("Generated by scripts/t62_stage2_supporting_record.py. Raw "
                 "transcripts are not committed; only counts, file names and "
                 "shell commands are kept. No value here is estimated: an "
                 "unreadable transcript is reported as unavailable."),
        "why_not_complete": (None if result == model.COMPLETE else
                             "one or more arms have no readable transcript, so "
                             "behaviour cannot be compared for those pairs"),
        "pairs": per_pair,
        "arms": arms,
        "behaviour_reading": {
            "comparable_pairs": sorted(pair_id for pair_id, row
                                       in per_pair.items()
                                       if row["behaviour_observable"]),
            "interpretation": (
                "Behaviour (files read, tool calls, wall time) is only reported "
                "where both arms' transcripts were readable. Where they were not, "
                "this file says so instead of quoting a number."),
            "is_statistical_claim": False,
            "supports_any_benefit_claim": False,
        },
        "usage_availability": {
            "worker_reports_token_usage": False,
            "detail": ("Every token/cost field the CodeBuddy job API returns was "
                       "null for the sessions that could be read, so token usage "
                       "is recorded as unavailable rather than estimated."),
        },
    }
    write_evidence(OUT, record)
    print(json.dumps({
        "result": result,
        "arms": len(arms),
        "readable": len(readable),
        "missing": sorted(set(filter(None, transcript_gaps))),
    }, ensure_ascii=False))
    return 0 if result == model.COMPLETE else 3


if __name__ == "__main__":
    raise SystemExit(main())
