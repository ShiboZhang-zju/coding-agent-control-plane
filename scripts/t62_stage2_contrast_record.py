"""Record the T62 stage 2 memory contrast honestly, including its variance.

Three pairs were run. They did NOT agree, and that is the finding:

  pair 1 (first attempt)  with_memory verified, without_memory verified
  pair 2                  with_memory rejected, without_memory verified
  pair 3                  with_memory verified, without_memory verified

The disagreement is one-sided: only pair 2 differs, and it differs against the
memory arm. There is no pair where the memory arm passed and the control failed.

Each pair is a separate, stable archive under ``evidence/project/t62/``:

  pair1-archived.json   seeded once from the record written BEFORE pair 1's
                        ledger lost its -wal file. Evidence level
                        ``historical_record_only``: cited, never re-measured.
  pair2.json            per-pair archive written when the pair finished.
  pair3.json            same.

Re-running this script never re-seeds pair 1, never appends a fourth pair, and
never upgrades an archive's evidence level. If a pair is missing or malformed
the result is BLOCKED, and if a verdict survives but its Session/Attempt does
not the result is INCOMPLETE - never PASS.

The conclusion this supports is deliberately narrow and three-layered:

  1. the memory *chain* is established (candidate -> human CAS edit ->
     verified promotion -> injected into a new Session's frozen prompt);
  2. a memory *benefit* is NOT established (2 verified / 1 rejected with
     memory vs 3 verified / 0 rejected without);
  3. the Verifier may itself be mismeasuring: pair 2's memory arm passed all 54
     tests and was rejected only by a `file_contains: normalized_1000`
     assertion, so `rejected` there is not proof the feature was not built.
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
OUT = ROOT / "evidence" / "project" / "T62-stage2-memory-contrast.json"
LEGACY_RECORD = OUT        # the pre-G1 file still holds pair 1's original rows

LAYERS = {
    "layer1_memory_chain_established": {
        "result": "ESTABLISHED",
        "proven_by": "evidence/project/T62-stage2-real-project.json",
        "what_is_proven": [
            "a candidate entry is produced deterministically from a verdict",
            "a human edit is applied through memory-preview + memory-apply (CAS)",
            "the entry is promoted to verified through context-verify with evidence",
            "a fresh Session receives the entry in its frozen Effective Prompt",
            "the Effective Prompt records the entry id and context version",
        ],
        "what_is_not_proven_here": (
            "that carrying the entry changes the Worker's outcome"),
    },
    "layer2_memory_benefit_not_proven": {
        "result": "NOT_PROVEN",
        "with_memory": {"verified": 2, "rejected": 1},
        "without_memory": {"verified": 3, "rejected": 0},
        "reading": (
            "The control arm did at least as well as the memory arm. With three "
            "pairs and n=1 per pair this shows neither a benefit nor a "
            "degradation; it only shows the sample cannot separate them."),
        "must_not_be_quoted_as": [
            "memory improves the success rate",
            "memory degrades the success rate",
        ],
    },
    "layer3_verifier_maybe_mismeasuring": {
        "result": "SUSPECTED_MEASUREMENT_BIAS",
        "pair": "pair2",
        "what_happened": (
            "the with-memory arm ran the full test suite successfully (54 passed) "
            "and was rejected solely on file_contains: normalized_1000; the "
            "without-memory arm passed every assertion"),
        "consequence": (
            "a rejected verdict on this task may reflect an over-strict text "
            "assertion rather than a missing feature, so rejected must not be "
            "read as 'the implementation failed'"),
        "supports": "re-run with behavioural assertions before trusting a "
                    "rejected verdict as a failure",
    },
}


def load_legacy() -> dict:
    if not LEGACY_RECORD.exists():
        return {}
    return json.loads(LEGACY_RECORD.read_text(encoding="utf-8"))


def build_pairs() -> tuple[dict, list[str], dict]:
    legacy = load_legacy()
    pairs, seeded = model.collect_archives(STATE, ARCHIVE_DIR, legacy)
    # Persist any archive that had to be seeded, so the next run reads the file
    # rather than re-deriving it. Existing archives are never rewritten.
    written = model.write_archives(ARCHIVE_DIR, pairs, write_evidence)
    return pairs, sorted(set(seeded) | set(written)), legacy


def main() -> int:
    pairs, seeded, legacy = build_pairs()
    validation = model.validate_archives(pairs)

    if validation["result"] == model.BLOCKED:
        write_evidence(OUT, {
            "test": "T62 stage 2 memory contrast (with vs without reviewed knowledge)",
            "result": model.BLOCKED,
            "blocked_reason": validation["problems"],
            "pairs_present": sorted(pairs),
            "archive_dir": "evidence/project/t62/",
            "note": ("No conclusion is derived while the archive set is "
                     "incomplete or inconsistent. This is deliberate: an empty "
                     "or partial set must never read as a pass."),
        })
        print(json.dumps({"result": model.BLOCKED,
                          "problems": validation["problems"]},
                         ensure_ascii=False, indent=1))
        return 2

    counts = model.tally(pairs)
    summary = model.pair_summary(pairs)
    detail = [pairs[pair_id] for pair_id in model.PAIR_IDS if pair_id in pairs]

    with_memory = counts[model.ROLE_WITH_MEMORY]
    without_memory = counts[model.ROLE_WITHOUT_MEMORY]

    flipped_pairs = [row["pair"] for row in summary if row["flipped"]]
    memory_wins = [row["pair"] for row in summary
                   if row["with_memory"] == "verified"
                   and row["without_memory"] == "rejected"]
    control_wins = [row["pair"] for row in summary
                    if row["without_memory"] == "verified"
                    and row["with_memory"] == "rejected"]

    layers = json.loads(json.dumps(LAYERS))
    layers["layer2_memory_benefit_not_proven"]["with_memory"] = with_memory
    layers["layer2_memory_benefit_not_proven"]["without_memory"] = without_memory

    direction = {
        "pairs_where_memory_arm_passed_and_control_failed": memory_wins,
        "pairs_where_control_passed_and_memory_arm_failed": control_wins,
        "pairs_that_disagreed": flipped_pairs,
        "reading": (
            "The disagreement is one-sided: no pair shows the memory arm passing "
            "while the control arm failed, and pair2 shows the control arm passing "
            "while the memory arm was rejected. The earlier claim that the arms "
            "diverged in both directions does not match the data and has been "
            "withdrawn."
            if not memory_wins else
            "At least one pair shows memory passing where the control failed."),
    }

    record = {
        "test": "T62 stage 2 memory contrast (with vs without reviewed knowledge)",
        # Two separate things, deliberately not merged:
        #   result             = what the experiment concluded from 3 real pairs
        #   archive_completeness = whether all supporting evidence is still readable
        # A missing SessionReference does not change the experimental conclusion,
        # and the conclusion must not be used to hide an incomplete archive.
        "result": "INCONCLUSIVE",
        "archive_completeness": validation["result"],
        "validation": {
            "result": validation["result"],
            "problems": validation["problems"],
            "incomplete": validation["incomplete"],
            "pairs_expected": list(model.PAIR_IDS),
            "pairs_present": sorted(pairs),
            "task_ids_counted": len(validation["task_ids"]),
            "duplicate_task_ids": False,
        },
        "result_cannot_be_upgraded_by_evidence_level": (
            "pair1 is cited from a pre-incident record and pair2 lacks a "
            "SessionReference; neither raises nor lowers the INCONCLUSIVE "
            "conclusion, and neither may be used to turn it into PASS or FAIL."),
        "why_inconclusive": (
            "The knowledge provably reaches the memory arm's frozen prompt, but "
            "the two arms' outcomes cannot be separated at n=3: the control arm "
            "passed all three pairs, the memory arm passed two. That is not "
            "evidence of benefit, and it is not evidence of harm either."),
        "three_layer_reading": layers,
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
        "archive_dir": "evidence/project/t62/",
        "archive_files": {pair_id: model.pair_filename(pair_id)
                          for pair_id in model.PAIR_IDS},
        "archives_seeded_this_run": seeded,
        "pairs": summary,
        "tally": counts,
        "direction_of_disagreement": direction,
        "pair_detail": detail,
        "variance_observation": {
            "with_memory_verified": with_memory["verified"],
            "with_memory_rejected": with_memory["rejected"],
            "without_memory_verified": without_memory["verified"],
            "without_memory_rejected": without_memory["rejected"],
            "reading": (
                "Both arms pass on some pairs and one arm fails on one pair; the "
                "difference is within single-sample Worker variance. The one "
                "disagreement runs against the memory arm, so no benefit may be "
                "claimed from this experiment."),
            "is_statistical_claim": False,
        },
        "evidence_levels": {
            pair_id: {
                "evidence_level": pairs[pair_id].get("evidence_level"),
                "ledger_recoverable": pairs[pair_id].get("ledger_recoverable"),
                "source": pairs[pair_id].get("source"),
                "notes": pairs[pair_id].get("notes"),
            }
            for pair_id in model.PAIR_IDS if pair_id in pairs
        },
        "incident_affecting_this_record": {
            "what": ("the first contrast ledger's -wal file was deleted during "
                     "cleanup, silently reverting that ledger to its queued state"),
            "effect": ("the ledger can no longer substantiate pair 1; the pair is "
                       "cited from the record written before the incident and is "
                       "labelled historical_record_only"),
            "lesson": ("never delete a live SQLite -wal by hand; see "
                       "scripts/t62_sqlite_cleanup.py for the guarded path"),
            "remediation": ("the pair runner no longer deletes wal files; pair 1 "
                            "keeps its lowest evidence level permanently"),
        },
        "not_claimed": [
            "that memory reduced exploration, time, or failure rate",
            "that memory increased the failure rate",
            "any statistical conclusion: 3 pairs, n=1 per pair, no significance",
            "that pair 1 was re-verified - its ledger is gone",
            "that a rejected verdict proves the feature was not implemented",
            "that C1's or C0's patch is better code",
        ],
    }

    record["why_not_pass"] = (
        "No pair-based claim is supportable: the control arm did as well or "
        "better, and one rejection is attributable to a text assertion rather "
        "than to missing functionality. The result stays INCONCLUSIVE.")

    write_evidence(OUT, record)
    print(json.dumps({
        "result": record["result"],
        "validation": validation["result"],
        "pairs": summary,
        "tally": counts,
        "seeded": seeded,
    }, ensure_ascii=False, indent=1))
    return 0 if validation["result"] == model.COMPLETE else 3


if __name__ == "__main__":
    raise SystemExit(main())
