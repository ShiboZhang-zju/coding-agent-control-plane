"""G1 regression tests for T62 evidence governance.

These are deterministic unit checks over the archive model and the SQLite
cleanup guard - no Worker, no model, no real ledger. Every case that used to
produce a pass-shaped output now has to produce BLOCKED / INCOMPLETE.

Run:  python -m pytest tests/test_t62_g1_evidence.py -q
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import t62_evidence_model as model  # noqa: E402
from t62_sqlite_cleanup import (  # noqa: E402
    LedgerInUse,
    fresh_ledger_path,
    remove_scratch_ledger,
    wal_size,
)

EVID = ROOT / "evidence" / "project"


def row(task_id: str, *, verdict: str, with_memory: bool, state: str = "succeeded",
        session: str | None = None, attempt: str | None = None) -> dict:
    return {
        "task_id": task_id,
        "verdict": verdict,
        "state": state,
        "context_version": 1 if with_memory else 0,
        "context_entry_refs": json.dumps([f"ctx-{task_id}"] if with_memory else []),
        "head_commit": "a" * 40,
        "duration_s": 100.0,
        "session_id": session,
        "attempt_id": attempt,
    }


def pair_rows(prefix: str, *, with_verdict: str = "verified",
              without_verdict: str = "verified",
              sessions: bool = True) -> list[dict]:
    return [
        row(f"{prefix}-mem", verdict=with_verdict, with_memory=True,
            session=f"{prefix}-s1" if sessions else None,
            attempt=f"{prefix}-a1" if sessions else None),
        row(f"{prefix}-ctl", verdict=without_verdict, with_memory=False,
            session=f"{prefix}-s0" if sessions else None,
            attempt=f"{prefix}-a0" if sessions else None),
    ]


# --------------------------------------------------------------------------- #
# archive validation: fail closed
# --------------------------------------------------------------------------- #
def test_empty_archive_set_is_blocked():
    result = model.validate_archives({})
    assert result["result"] == model.BLOCKED
    assert any("missing pair archive" in problem for problem in result["problems"])


def test_missing_one_arm_is_blocked():
    record = model.normalize_pair(
        "pair2", pair_rows("p2")[:1],
        source={"kind": "test"}, evidence_level="archived_verdict",
        ledger_recoverable=True)
    assert record["complete"] is False
    assert any("arm count is 1" in problem for problem in record["problems"])
    assert any("without_memory" in problem for problem in record["problems"])


def test_two_arms_of_same_role_is_blocked():
    rows = [row("t1", verdict="verified", with_memory=True),
            row("t2", verdict="verified", with_memory=True)]
    record = model.normalize_pair("pair2", rows, source={"kind": "test"},
                                  evidence_level="archived_verdict",
                                  ledger_recoverable=True)
    assert record["complete"] is False


def test_missing_session_makes_set_incomplete_not_complete():
    pairs = {}
    for index, pair_id in enumerate(model.PAIR_IDS):
        pairs[pair_id] = model.normalize_pair(
            pair_id, pair_rows(f"t{index}", sessions=(pair_id != "pair2")),
            source={"kind": "test"},
            evidence_level=("historical_record_only" if pair_id == "pair1"
                            else "archived_verdict"),
            ledger_recoverable=(pair_id != "pair1"))
    result = model.validate_archives(pairs)
    assert result["result"] == model.INCOMPLETE
    assert any("pair2" in item for item in result["incomplete"])


def test_duplicate_task_id_across_pairs_is_blocked():
    pairs = {}
    for index, pair_id in enumerate(model.PAIR_IDS):
        rows = pair_rows(f"t{index}")
        if pair_id == "pair3":
            rows[0]["task_id"] = "t0-mem"       # reuse pair1's task id
        pairs[pair_id] = model.normalize_pair(
            pair_id, rows, source={"kind": "test"},
            evidence_level=("historical_record_only" if pair_id == "pair1"
                            else "archived_verdict"),
            ledger_recoverable=(pair_id != "pair1"))
    result = model.validate_archives(pairs)
    assert result["result"] == model.BLOCKED
    assert any("already counted" in problem for problem in result["problems"])


def test_fourth_pair_is_blocked_not_accumulated():
    pairs = {}
    for index, pair_id in enumerate(model.PAIR_IDS):
        pairs[pair_id] = model.normalize_pair(
            pair_id, pair_rows(f"t{index}"), source={"kind": "test"},
            evidence_level=("historical_record_only" if pair_id == "pair1"
                            else "archived_verdict"),
            ledger_recoverable=(pair_id != "pair1"))
    pairs["pair4"] = model.normalize_pair(
        "pair4", pair_rows("t9"), source={"kind": "test"},
        evidence_level="archived_verdict", ledger_recoverable=True)
    result = model.validate_archives(pairs)
    assert result["result"] == model.BLOCKED
    assert any("pair4" in problem for problem in result["problems"])


def test_non_terminal_verdict_is_not_complete():
    rows = pair_rows("p2")
    rows[0]["verdict"] = "pending"
    record = model.normalize_pair("pair2", rows, source={"kind": "test"},
                                  evidence_level="archived_verdict",
                                  ledger_recoverable=True)
    assert record["complete"] is False
    assert any("not terminal" in problem for problem in record["problems"])


def test_historical_pair_may_lack_session_but_must_not_claim_recoverable_ledger():
    record = model.normalize_pair(
        "pair1", pair_rows("p1", sessions=False), source={"kind": "test"},
        evidence_level="historical_record_only", ledger_recoverable=False)
    assert record["complete"] is True
    record["ledger_recoverable"] = True
    result = model.validate_archives({"pair1": record})
    assert result["result"] == model.BLOCKED
    assert any("recoverable ledger" in problem for problem in result["problems"])


# --------------------------------------------------------------------------- #
# the real, committed archives
# --------------------------------------------------------------------------- #
def test_committed_archives_hold_the_real_three_pairs():
    pairs = {pair_id: model.load_archive(EVID / "t62", pair_id)
             for pair_id in model.PAIR_IDS}
    assert all(pairs.values()), "pair archives are missing"
    validation = model.validate_archives(pairs)
    assert validation["result"] in (model.COMPLETE, model.INCOMPLETE)
    assert len(validation["task_ids"]) == 6

    verdicts = model.pair_summary(pairs)
    by_pair = {row["pair"]: row for row in verdicts}
    assert by_pair["pair1"]["with_memory"] == "verified"
    assert by_pair["pair1"]["without_memory"] == "verified"
    assert by_pair["pair1"]["evidence_level"] == "historical_record_only"
    assert by_pair["pair1"]["ledger_recoverable"] is False
    assert by_pair["pair2"]["with_memory"] == "rejected"
    assert by_pair["pair2"]["without_memory"] == "verified"
    assert by_pair["pair3"]["with_memory"] == "verified"
    assert by_pair["pair3"]["without_memory"] == "verified"

    counts = model.tally(pairs)
    assert counts["with_memory"] == {"verified": 2, "rejected": 1}
    assert counts["without_memory"] == {"verified": 3, "rejected": 0}


def _tamper(pairs: dict, target: str, **changes) -> dict:
    """Copy the archive set and apply an edit, as a person editing the JSON would."""
    clone = json.loads(json.dumps(pairs))
    clone[target].update(changes)
    return clone


def test_archive_with_arms_removed_is_blocked_on_read():
    """A stored 'complete: true' must not survive losing its arms.

    This is the exact shape the reviewer described: the file still reads, still
    claims completeness, but the arms are gone.
    """
    pairs = {pair_id: model.load_archive(EVID / "t62", pair_id)
             for pair_id in model.PAIR_IDS}
    assert model.validate_archives(pairs)["result"] in (model.COMPLETE,
                                                        model.INCOMPLETE)
    tampered = _tamper(pairs, "pair2", arms=[], problems=[], complete=True)
    result = model.validate_archives(tampered)
    assert result["result"] == model.BLOCKED
    assert any("arm count" in p for p in result["problems"])


def test_archive_with_one_arm_is_blocked_on_read():
    pairs = {pair_id: model.load_archive(EVID / "t62", pair_id)
             for pair_id in model.PAIR_IDS}
    one_arm = [pairs["pair2"]["arms"][0]]
    tampered = _tamper(pairs, "pair2", arms=one_arm)
    assert model.validate_archives(tampered)["result"] == model.BLOCKED


def test_archive_with_stale_verdicts_is_blocked_on_read():
    pairs = {pair_id: model.load_archive(EVID / "t62", pair_id)
             for pair_id in model.PAIR_IDS}
    # Flip the stored verdicts without touching the arms.
    tampered = _tamper(pairs, "pair2", verdicts={
        "with_memory": "verified", "without_memory": "verified"}, flipped=False)
    result = model.validate_archives(tampered)
    assert result["result"] == model.BLOCKED
    assert any("do not match the arms" in p for p in result["problems"])


def test_archive_with_mismatched_flipped_is_blocked():
    pairs = {pair_id: model.load_archive(EVID / "t62", pair_id)
             for pair_id in model.PAIR_IDS}
    tampered = _tamper(pairs, "pair2", flipped=False)  # arms still disagree
    result = model.validate_archives(tampered)
    assert result["result"] == model.BLOCKED
    assert any("flipped" in p for p in result["problems"])


def test_archive_with_wrong_pair_id_is_blocked():
    pairs = {pair_id: model.load_archive(EVID / "t62", pair_id)
             for pair_id in model.PAIR_IDS}
    tampered = _tamper(pairs, "pair2", pair_id="pair9")
    result = model.validate_archives(tampered)
    assert result["result"] == model.BLOCKED
    assert any("stored/filed as" in p for p in result["problems"])


def test_historical_archive_cannot_claim_a_recoverable_ledger():
    pairs = {pair_id: model.load_archive(EVID / "t62", pair_id)
             for pair_id in model.PAIR_IDS}
    tampered = _tamper(pairs, "pair1", ledger_recoverable=True)
    result = model.validate_archives(tampered)
    assert result["result"] == model.BLOCKED
    assert any("historical pair must not claim" in p for p in result["problems"])


def test_pair1_cannot_be_relabelled_as_a_stronger_level():
    pairs = {pair_id: model.load_archive(EVID / "t62", pair_id)
             for pair_id in model.PAIR_IDS}
    tampered = _tamper(pairs, "pair1", evidence_level="archived_verdict",
                       ledger_recoverable=True)
    result = model.validate_archives(tampered)
    assert result["result"] == model.BLOCKED
    assert any("expected 'historical_record_only'" in p
               for p in result["problems"])


def test_duplicate_task_id_inside_a_pair_is_blocked_on_read():
    pairs = {pair_id: model.load_archive(EVID / "t62", pair_id)
             for pair_id in model.PAIR_IDS}
    arms = json.loads(json.dumps(pairs["pair3"]["arms"]))
    arms[1]["task_id"] = arms[0]["task_id"]
    result = model.validate_archives(_tamper(pairs, "pair3", arms=arms))
    assert result["result"] == model.BLOCKED
    assert any("not distinct" in p for p in result["problems"])


def test_pair1_archive_is_not_upgraded_by_later_evidence_levels():
    record = model.load_archive(EVID / "t62", "pair1")
    assert record["evidence_level"] == "historical_record_only"
    assert record["ledger_recoverable"] is False
    assert record["source"]["reexecuted"] is False
    assert all(arm["session_id"] is None for arm in record["arms"])


def test_pair1_filenames_are_explicitly_archived():
    assert model.pair_filename("pair1") == "pair1-archived.json"
    assert model.pair_filename("pair2") == "pair2.json"


def test_archive_regeneration_is_idempotent(tmp_path):
    target = tmp_path / "t62"
    source_state = tmp_path / "state"
    (source_state / "pairs" / "pair2").mkdir(parents=True)
    (source_state / "pairs" / "pair3").mkdir(parents=True)
    (source_state / "pairs" / "pair2" / "summary.json").write_text(
        json.dumps(pair_rows("p2", with_verdict="rejected")), encoding="utf-8")
    (source_state / "pairs" / "pair3" / "summary.json").write_text(
        json.dumps(pair_rows("p3")), encoding="utf-8")
    legacy = json.loads(
        (EVID / "T62-stage2-memory-contrast.json").read_text(encoding="utf-8"))

    first, seeded_first = model.collect_archives(source_state, target, legacy)
    written_first = model.write_archives(
        target, first, lambda path, data: path.write_text(
            json.dumps(data, indent=2), encoding="utf-8"))
    assert sorted(written_first) == ["pair1", "pair2", "pair3"]

    second, seeded_second = model.collect_archives(source_state, target, legacy)
    written_second = model.write_archives(
        target, second, lambda path, data: path.write_text(
            json.dumps(data, indent=2), encoding="utf-8"))
    assert seeded_second == [] and written_second == []
    assert len(second) == 3
    assert model.tally(second) == model.tally(first)
    assert list((target).glob("*.json")).__len__() == 3


def test_two_runs_produce_identical_archive_bytes(tmp_path):
    source_state = tmp_path / "state"
    (source_state / "pairs" / "pair2").mkdir(parents=True)
    (source_state / "pairs" / "pair3").mkdir(parents=True)
    (source_state / "pairs" / "pair2" / "summary.json").write_text(
        json.dumps(pair_rows("p2")), encoding="utf-8")
    (source_state / "pairs" / "pair3" / "summary.json").write_text(
        json.dumps(pair_rows("p3")), encoding="utf-8")
    legacy = json.loads(
        (EVID / "T62-stage2-memory-contrast.json").read_text(encoding="utf-8"))

    blobs = []
    for _ in range(2):
        target = tmp_path / f"run{len(blobs)}"
        pairs, _ = model.collect_archives(source_state, target, legacy)
        model.write_archives(
            target, pairs, lambda path, data: path.write_text(
                json.dumps(data, indent=2, sort_keys=True), encoding="utf-8"))
        blobs.append({
            pair_id: model.pair_path(target, pair_id).read_bytes()
            for pair_id in model.PAIR_IDS})
    assert blobs[0] == blobs[1]


def test_contrast_record_separates_three_layers():
    record = json.loads(
        (EVID / "T62-stage2-memory-contrast.json").read_text(encoding="utf-8"))
    layers = record["three_layer_reading"]
    assert layers["layer1_memory_chain_established"]["result"] == "ESTABLISHED"
    assert layers["layer2_memory_benefit_not_proven"]["result"] == "NOT_PROVEN"
    assert layers["layer3_verifier_maybe_mismeasuring"]["pair"] == "pair2"
    # The experimental conclusion is fixed; archive completeness is separate.
    assert record["result"] == "INCONCLUSIVE"
    assert record["archive_completeness"] in ("COMPLETE", "INCOMPLETE")
    assert len(record["pairs"]) == 3
    assert record["tally"]["with_memory"] == {"verified": 2, "rejected": 1}
    assert record["tally"]["without_memory"] == {"verified": 3, "rejected": 0}


def test_old_opposite_directions_claim_is_gone():
    record = json.loads(
        (EVID / "T62-stage2-memory-contrast.json").read_text(encoding="utf-8"))
    text = json.dumps(record, ensure_ascii=False)
    assert "opposite directions" not in text
    direction = record["direction_of_disagreement"]
    assert direction["pairs_where_memory_arm_passed_and_control_failed"] == []
    assert direction["pairs_where_control_passed_and_memory_arm_failed"] == ["pair2"]


def test_conclusion_is_never_upgraded_by_evidence_level():
    record = json.loads(
        (EVID / "T62-stage2-memory-contrast.json").read_text(encoding="utf-8"))
    # An incomplete archive must still read INCONCLUSIVE, never PASS.
    assert record["result"] == "INCONCLUSIVE"
    assert record["archive_completeness"] in ("COMPLETE", "INCOMPLETE")
    note = record["result_cannot_be_upgraded_by_evidence_level"]
    assert "neither may be used to turn it into PASS" in note


def test_contrast_does_not_claim_benefit_or_harm():
    record = json.loads(
        (EVID / "T62-stage2-memory-contrast.json").read_text(encoding="utf-8"))
    forbidden = " ".join(record["not_claimed"])
    assert "increased the failure rate" in forbidden
    assert record["variance_observation"]["is_statistical_claim"] is False


# --------------------------------------------------------------------------- #
# supporting record: no invented transcripts
# --------------------------------------------------------------------------- #
def test_supporting_record_does_not_claim_completeness_when_a_transcript_is_missing():
    record = json.loads(
        (EVID / "T62-stage2-pilot-transcripts.json").read_text(encoding="utf-8"))
    gaps = record["validation"]["arms_without_transcript"]
    if gaps:
        assert record["result"] == "INCOMPLETE"
        assert record["why_not_complete"]
    else:
        assert record["result"] == "COMPLETE"


def test_pilot_tasks_are_read_from_the_ledger_not_invented():
    record = json.loads(
        (EVID / "T62-stage2-pilot-transcripts.json").read_text(encoding="utf-8"))
    for task_id in ("cp-50dc18149ac2", "cp-570efa369576", "cp-5a2bd464ca89"):
        arm = record["arms"][task_id]
        assert arm["pair"] == "pilot"
        assert arm["verdict"] in ("verified", "rejected")


def test_run_time_observations_survive_regeneration():
    record = json.loads(
        (EVID / "T62-stage2-pilot-transcripts.json").read_text(encoding="utf-8"))
    expected = {"cp-50dc18149ac2": 7, "cp-570efa369576": 7, "cp-5a2bd464ca89": 8}
    for task_id, calls in expected.items():
        arm = record["arms"][task_id]
        assert arm["tool_calls"] == calls, task_id
        assert arm["transcript_source"], task_id


def test_status_refuses_empty_and_single_arm_ledgers(tmp_path):
    """`all([]) is True` used to make an empty ledger look finished."""
    import sqlite3

    sys.path.insert(0, str(ROOT / "scripts"))
    ledger = tmp_path / "empty.db"
    conn = sqlite3.connect(str(ledger))
    conn.execute("create table work_items (id text, project_id text, state text, "
                 "verdict text, context_version integer, context_entry_refs text)")
    conn.commit()
    conn.close()

    import t62_stage2_contrast as contrast
    original = contrast.active_ledger
    contrast.active_ledger = lambda: ledger
    try:
        assert contrast.status() != 0
        conn = sqlite3.connect(str(ledger))
        conn.execute("insert into work_items values "
                     "('cp-1','p','succeeded','verified',0,'[]')")
        conn.commit()
        conn.close()
        assert contrast.status() != 0, "one arm must not be READY"
    finally:
        contrast.active_ledger = original


def test_supporting_record_never_infers_tool_calls():
    record = json.loads(
        (EVID / "T62-stage2-pilot-transcripts.json").read_text(encoding="utf-8"))
    for task_id, arm in record["arms"].items():
        if arm.get("transcript") != "available":
            assert arm.get("tool_calls") is None, task_id
            assert arm.get("transcript_reason"), task_id
    assert record["result"] in ("COMPLETE", "INCOMPLETE")
    assert record["behaviour_reading"]["supports_any_benefit_claim"] is False


def test_supporting_record_marks_pair1_as_historical():
    record = json.loads(
        (EVID / "T62-stage2-pilot-transcripts.json").read_text(encoding="utf-8"))
    pair1_arms = [arm for arm in record["arms"].values()
                  if arm.get("pair") == "pair1"]
    assert len(pair1_arms) == 2
    assert all(arm["evidence_level"] == "historical_record_only"
               for arm in pair1_arms)


# --------------------------------------------------------------------------- #
# acceptance script: no hardcoded pass
# --------------------------------------------------------------------------- #
RECHECK = EVID / "T62-stage2-real-project-recheck.json"
HISTORICAL = EVID / "T62-stage2-real-project.json"


def test_real_project_evidence_has_no_hardcoded_push_check():
    record = json.loads(RECHECK.read_text(encoding="utf-8"))
    assert "no_push_to_github" not in record["checks"]
    declared = record["human_declarations_not_automated"]
    assert declared and all(d["verified_automatically"] is False for d in declared)
    assert record["automated_checks_counted"] == len(record["checks"])
    assert record["result"] in ("PASS", "INCOMPLETE", "FAIL")


def test_recheck_does_not_overwrite_the_run_time_record():
    historical = json.loads(HISTORICAL.read_text(encoding="utf-8"))
    recheck = json.loads(RECHECK.read_text(encoding="utf-8"))
    # The run-time file keeps what only it could observe.
    assert historical["checks"]["transcripts_readable"] is True
    assert historical["observed"]["sessions"]["A"]["tool_calls"] == 7
    assert historical["observed"]["sessions"]["B"]["tool_calls"] == 8
    # The re-check cites it instead of replacing it.
    assert recheck["recheck_of"] == HISTORICAL.name
    assert recheck["historical_record"]["transcripts_readable_at_run_time"] is True
    assert recheck["result"] != "PASS" or "transcripts_readable" not in recheck["checks"]


def test_acceptance_scripts_do_not_unlink_a_live_ledger():
    for name in ("t62_stage2_contrast.py", "t62_stage2_real_project.py",
                 "t62_stage2_contrast_record.py",
                 "t62_stage2_supporting_record.py"):
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "LEDGER.unlink()" not in text, name
        assert ".unlink()" not in text.replace("leftover", ""), (
            f"{name} still calls unlink() directly; deletion must go through "
            "t62_sqlite_cleanup.remove_scratch_ledger()")
        # No script may remove a sidecar itself; mentioning "-wal" in prose is
        # fine, calling unlink on it is not.
        for sidecar in ('"-wal"', "'-wal'", "-wal\""):
            assert f"{sidecar}.unlink" not in text.replace(" ", ""), name
        assert "shutil.rmtree" not in text, name


# --------------------------------------------------------------------------- #
# SQLite cleanup guard
# --------------------------------------------------------------------------- #
def _ledger(path: Path, rows: int, verdict: str = "verified",
            state: str = "succeeded") -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("create table work_items (id text primary key, state text, "
                 "verdict text)")
    for index in range(rows):
        conn.execute("insert into work_items values (?,?,?)",
                     (f"cp-{index}", state, verdict))
    conn.commit()
    conn.close()


def test_scratch_ledger_without_verdicts_can_be_removed(tmp_path):
    """Only never-started rows count as scratch: queued, no verdict."""
    ledger = tmp_path / "scratch.db"
    _ledger(ledger, 2, verdict="", state="queued")
    report = remove_scratch_ledger(ledger)
    assert report.deleted is True
    assert not ledger.exists()
    assert wal_size(ledger) == 0


def test_pending_verdict_in_terminal_state_is_not_scratch(tmp_path):
    """`succeeded` + pending verdict means work happened but was never judged."""
    ledger = tmp_path / "odd.db"
    _ledger(ledger, 2, verdict="pending", state="succeeded")
    with pytest.raises(LedgerInUse):
        remove_scratch_ledger(ledger)
    assert ledger.exists()


def test_ledger_with_verdicts_is_refused_on_the_default_path(tmp_path):
    """The old bug: the guard was only enforced when a caller opted in.

    `remove_scratch_ledger(ledger)` is exactly what setup() calls, so that - not
    the `allow_pending=False` variant - is what must be tested.
    """
    ledger = tmp_path / "evidence.db"
    _ledger(ledger, 2)
    with pytest.raises(LedgerInUse):
        remove_scratch_ledger(ledger)
    assert ledger.exists()
    conn = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True)
    try:
        assert conn.execute("select count(*) from work_items").fetchone()[0] == 2
    finally:
        conn.close()


def test_single_verdict_is_enough_to_refuse(tmp_path):
    ledger = tmp_path / "one-verdict.db"
    _ledger(ledger, 2, verdict="pending")
    conn = sqlite3.connect(str(ledger))
    conn.execute("update work_items set verdict='verified' where id='cp-0'")
    conn.commit()
    conn.close()
    with pytest.raises(LedgerInUse):
        remove_scratch_ledger(ledger)
    assert ledger.exists()


def test_inconclusive_verdict_is_also_evidence(tmp_path):
    ledger = tmp_path / "inconclusive.db"
    _ledger(ledger, 1, verdict="inconclusive")
    with pytest.raises(LedgerInUse):
        remove_scratch_ledger(ledger)
    assert ledger.exists()


def test_running_task_blocks_removal(tmp_path):
    ledger = tmp_path / "running.db"
    _ledger(ledger, 1, verdict="pending", state="running")
    with pytest.raises(LedgerInUse):
        remove_scratch_ledger(ledger)
    assert ledger.exists()


def test_dispatch_unknown_blocks_removal(tmp_path):
    ledger = tmp_path / "unknown.db"
    _ledger(ledger, 1, verdict="pending", state="dispatch_unknown")
    with pytest.raises(LedgerInUse):
        remove_scratch_ledger(ledger)
    assert ledger.exists()


def test_terminal_state_without_verdict_blocks_removal(tmp_path):
    """A succeeded row whose verdict was never written is still real work."""
    ledger = tmp_path / "no-verdict.db"
    _ledger(ledger, 1, verdict="", state="succeeded")
    with pytest.raises(LedgerInUse):
        remove_scratch_ledger(ledger)
    assert ledger.exists()


def test_unrecognised_state_blocks_removal(tmp_path):
    ledger = tmp_path / "weird.db"
    _ledger(ledger, 1, verdict="pending", state="teleported")
    with pytest.raises(LedgerInUse):
        remove_scratch_ledger(ledger)
    assert ledger.exists()


def test_wal_is_never_deleted_by_hand(tmp_path):
    ledger = tmp_path / "live.db"
    _ledger(ledger, 1, verdict="pending")
    wal = Path(f"{ledger}-wal")
    # Simulate a live connection holding the WAL open.
    holder = sqlite3.connect(str(ledger))
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("insert into work_items values ('cp-x','queued','pending')")
    holder.commit()
    try:
        with pytest.raises(LedgerInUse):
            remove_scratch_ledger(ledger)
    finally:
        holder.close()
    # Nothing was deleted: the ledger, its rows and the WAL all survive.
    assert ledger.exists()
    conn = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True)
    try:
        assert conn.execute("select count(*) from work_items").fetchone()[0] >= 1
    finally:
        conn.close()
    assert not wal.exists() or wal.stat().st_size >= 0


def test_refusal_never_deletes_wal_or_shm(tmp_path):
    """A refused call must not touch the sidecars at all.

    Closing the holder connection is itself what lets SQLite fold a WAL away, so
    the assertion is scoped to the refused call: at the moment it raises, the
    ledger and its data are untouched, and this module never calls unlink on a
    sidecar (checked statically too).
    """
    ledger = tmp_path / "keep.db"
    _ledger(ledger, 1)
    holder = sqlite3.connect(str(ledger))
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("insert into work_items values ('cp-y','succeeded','pending')")
    holder.commit()
    try:
        with pytest.raises(LedgerInUse):
            remove_scratch_ledger(ledger)
        # Still inside the refusal: nothing removed, rows intact.
        assert ledger.exists()
        rows = holder.execute("select count(*) from work_items").fetchone()[0]
        assert rows >= 1
    finally:
        holder.close()
    # And the module contains no sidecar deletion at all.
    source = (ROOT / "scripts" / "t62_sqlite_cleanup.py").read_text(encoding="utf-8")
    assert "-wal\").unlink" not in source
    assert "sidecars(" in source and ".unlink()" in source.split("sidecars(")[0]


def test_fresh_ledger_path_avoids_the_existing_file(tmp_path):
    ledger = tmp_path / "stage2c.db"
    _ledger(ledger, 1)
    candidate = fresh_ledger_path(ledger)
    assert candidate != ledger and not candidate.exists()
    assert candidate.name.startswith("stage2c-run")


def test_unknown_schema_is_refused(tmp_path):
    ledger = tmp_path / "mystery.db"
    conn = sqlite3.connect(str(ledger))
    conn.execute("create table unrelated (x integer)")
    conn.commit()
    conn.close()
    with pytest.raises(LedgerInUse):
        remove_scratch_ledger(ledger)
    assert ledger.exists()


# --------------------------------------------------------------------------- #
# sanitisation
# --------------------------------------------------------------------------- #
def test_absolute_paths_are_sanitised():
    from sanitize_evidence import sanitize_text

    text = json.dumps({
        "cmd": "cd F:\\coding-agent-control-plane\\.poc-state\\t62\\work-c1 && pytest",
        "home": "C:\\Users\\somebody\\AppData\\Local\\Temp\\x.json",
        "token": "token=abcdef123456",
    })
    cleaned, hits = sanitize_text(text)
    assert hits > 0
    assert "coding-agent-control-plane" not in cleaned
    assert "somebody" not in cleaned
    assert "abcdef123456" not in cleaned


def test_structured_keys_are_redacted_in_any_spelling():
    """JSON *fields*, not substrings: camelCase used to bypass the whole filter."""
    from sanitize_evidence import redact_sensitive

    payload = {
        "token": "s1",
        "accessToken": "s2",
        "clientSecret": "s3",
        "Access-Token": "s4",
        "apiKey": "s5",
        "github_token": "s6",
        "nested": {"refreshToken": "s7", "ok": "keep-me"},
        "list": [{"idToken": "s8"}, {"note": "keep-me-too"}],
    }
    cleaned = redact_sensitive(payload)
    dumped = json.dumps(cleaned)
    for secret in ("s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8"):
        assert f'"{secret}"' not in dumped, f"{secret} survived structured redaction"
    # Structure and harmless values are preserved.
    assert cleaned["nested"]["ok"] == "keep-me"
    assert cleaned["list"][1]["note"] == "keep-me-too"


def test_token_metrics_are_not_redacted_as_secrets():
    """`token_count` is a measurement; redacting it would delete real evidence."""
    from sanitize_evidence import is_sensitive_key

    assert is_sensitive_key("token") is True
    assert is_sensitive_key("accessToken") is True
    assert is_sensitive_key("token_count") is False
    assert is_sensitive_key("max_tokens") is False


def test_write_evidence_redacts_before_the_bytes_hit_disk(tmp_path):
    """The end-to-end path: a serialized evidence file must carry no secret."""
    from sanitize_evidence import write_evidence

    target = tmp_path / "e.json"
    write_evidence(target, {
        "accessToken": "super-secret",
        "worker": {"clientSecret": "another-secret"},
        "cmd": "cd F:\\coding-agent-control-plane\\.poc-state && pytest",
    })
    text = target.read_text(encoding="utf-8")
    assert "super-secret" not in text
    assert "another-secret" not in text
    assert "coding-agent-control-plane" not in text
    json.loads(text)  # still valid JSON


def test_committed_t62_evidence_has_no_machine_path():
    for name in ("T62-stage2-pilot-transcripts.json",
                 "T62-stage2-memory-contrast.json",
                 "T62-stage2-real-project-recheck.json",
                 "t62/pair1-archived.json", "t62/pair2.json", "t62/pair3.json"):
        text = (EVID / name).read_text(encoding="utf-8")
        assert "coding-agent-control-plane" not in text, name
        assert "/f/coding-agent-control-plane" not in text, name
        assert str(Path.home()) not in text, name
        for marker in ("Bearer ", "api_key=", "password="):
            assert marker not in text, f"{name} leaks {marker}"


def test_run_time_record_keeps_its_raw_paths_out_of_the_new_files():
    """The old file may predate the git-bash pattern; the new ones may not."""
    for name in ("T62-stage2-pilot-transcripts.json",
                 "T62-stage2-memory-contrast.json",
                 "T62-stage2-real-project-recheck.json"):
        text = (EVID / name).read_text(encoding="utf-8")
        assert "/f/coding-agent-control-plane" not in text, name
        assert "F:\\\\coding-agent-control-plane" not in text, name


def test_shell_commands_in_supporting_record_are_relative_or_placeholder():
    record = json.loads(
        (EVID / "T62-stage2-pilot-transcripts.json").read_text(encoding="utf-8"))
    for arm in record["arms"].values():
        for command in arm.get("shell_commands") or []:
            assert "F:\\coding-agent-control-plane" not in command
            assert "C:\\Users\\" not in command
