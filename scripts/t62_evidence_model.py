"""T62 stage 2: one stable, validated archive per contrast pair.

Why this module exists
----------------------
Before G1, the contrast record re-derived pair 1 from an acceptance file that no
longer contains it, and the supporting record re-queried pair 1's task ids in a
ledger whose `-wal` had been deleted. Both produced a *pass-looking* output over
data that was no longer there (`all([]) is True`).

Here a pair is an explicit artefact with an explicit evidence level. Nothing is
inferred from an empty query, a missing session, or a missing transcript:

  evidence levels (never upgraded, only declared):
    live_ledger_verified  the pair's own ledger row still carries the verdict,
                          session and attempt, and can be re-read today
    archived_verdict      the verdict survives only in the per-pair archive that
                          was written while the ledger still held it
    historical_record_only
                          the ledger is gone; only the contemporaneous record
                          written before the incident survives. This is the
                          weakest level and is never re-labelled.

Everything in this file is deterministic: the same inputs give byte-identical
archives, and re-running never appends a pair that already exists.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ARCHIVE_SCHEMA = "t62.pair_archive/1"

PAIR_IDS = ("pair1", "pair2", "pair3")

TERMINAL_VERDICTS = ("verified", "rejected")
TERMINAL_STATES = ("succeeded", "failed")

EVIDENCE_LEVELS = (
    "live_ledger_verified",
    "archived_verdict",
    "historical_record_only",
)

ROLE_WITH_MEMORY = "with_memory"
ROLE_WITHOUT_MEMORY = "without_memory"

COMPLETE = "COMPLETE"
INCOMPLETE = "INCOMPLETE"
BLOCKED = "BLOCKED"


def pair_filename(pair_id: str) -> str:
    """pair1 keeps an explicit `archived` suffix; it is not a live measurement."""
    return "pair1-archived.json" if pair_id == "pair1" else f"{pair_id}.json"


def pair_path(directory: Path, pair_id: str) -> Path:
    return Path(directory) / pair_filename(pair_id)


# --------------------------------------------------------------------------- #
# normalisation
# --------------------------------------------------------------------------- #
def _refs(row: dict) -> list[str]:
    raw = row.get("context_entry_refs")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    return [str(item) for item in (raw or [])]


def _as_float(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _arm(row: dict) -> dict:
    refs = _refs(row)
    version = row.get("context_version")
    version = int(version) if version not in (None, "") else (1 if refs else 0)
    role = ROLE_WITH_MEMORY if (version > 0 or refs) else ROLE_WITHOUT_MEMORY
    return {
        "role": role,
        "task_id": (row.get("task_id") or row.get("id") or "") or "",
        "arm_label": row.get("arm") or "",
        "verdict": row.get("verdict") or "",
        "state": row.get("state") or "",
        "context_version": version,
        "context_entry_refs": refs,
        "head_commit": row.get("head_commit") or "",
        "duration_s": _as_float(row.get("duration_s")),
        "session_id": row.get("session_id") or None,
        "attempt_id": row.get("attempt_id") or None,
        "assertions_failed": list(row.get("assertions_failed") or []),
        "added_lines": row.get("added_lines"),
        "files_changed": list(row.get("files_changed") or []),
        "tests_tail": ((row.get("tests_tail") or [""]) or [""])[0]
        if isinstance(row.get("tests_tail"), list) else (row.get("tests_tail") or ""),
    }


def normalize_pair(pair_id: str, rows: list[dict], *, source: dict,
                   evidence_level: str, ledger_recoverable: bool,
                   notes: list[str] | None = None) -> dict:
    """Turn raw per-arm rows into a validated archive record.

    A record is `complete` only when it really holds two arms, one per role,
    with distinct task ids and a terminal verdict. Anything else is recorded in
    `problems` so callers can fail closed instead of guessing.
    """
    problems: list[str] = []
    if evidence_level not in EVIDENCE_LEVELS:
        problems.append(f"unknown evidence level: {evidence_level}")

    arms = [_arm(row) for row in rows]
    if len(arms) != 2:
        problems.append(f"arm count is {len(arms)}, expected exactly 2")

    seen_roles: dict[str, int] = {}
    for arm in arms:
        seen_roles[arm["role"]] = seen_roles.get(arm["role"], 0) + 1
        if not arm["task_id"]:
            problems.append("an arm has no task_id")
        if arm["verdict"] not in TERMINAL_VERDICTS:
            problems.append(
                f"{arm['task_id'] or '<unknown>'}: verdict {arm['verdict']!r} "
                f"is not terminal {TERMINAL_VERDICTS}")
        if arm["state"] and arm["state"] not in TERMINAL_STATES:
            problems.append(
                f"{arm['task_id'] or '<unknown>'}: state {arm['state']!r} is not "
                f"terminal {TERMINAL_STATES}")
    if seen_roles.get(ROLE_WITH_MEMORY) != 1:
        problems.append(
            f"expected exactly one {ROLE_WITH_MEMORY} arm, got "
            f"{seen_roles.get(ROLE_WITH_MEMORY, 0)}")
    if seen_roles.get(ROLE_WITHOUT_MEMORY) != 1:
        problems.append(
            f"expected exactly one {ROLE_WITHOUT_MEMORY} arm, got "
            f"{seen_roles.get(ROLE_WITHOUT_MEMORY, 0)}")

    ids = [arm["task_id"] for arm in arms if arm["task_id"]]
    if len(ids) != len(set(ids)):
        problems.append(f"task ids are not distinct inside the pair: {ids}")

    verdicts = {}
    for arm in arms:
        verdicts[arm["role"]] = arm["verdict"]

    return {
        "schema": ARCHIVE_SCHEMA,
        "pair_id": pair_id,
        "evidence_level": evidence_level,
        # Two different questions, deliberately not one flag:
        #   ledger_recoverable  - can the ORIGINAL ledger still substantiate this?
        #   archive_readable    - can this archive file be read today?
        # pair2's ledger was reset, so its ledger is not recoverable even though
        # its archive reads fine. Conflating them would let a weaker pair look
        # stronger than it is.
        "ledger_recoverable": bool(ledger_recoverable),
        "archive_readable": True,
        "source": source,
        "notes": list(notes or []),
        "complete": not problems,
        "problems": problems,
        "verdicts": verdicts,
        "flipped": (len(set(verdicts.values())) > 1
                    if len(verdicts) == 2 else False),
        "arms": arms,
    }


# --------------------------------------------------------------------------- #
# reading the per-pair sources
# --------------------------------------------------------------------------- #
def read_summary(state_dir: Path, pair_id: str) -> list[dict] | None:
    """The pair's per-arm rows. A dict wrapper (`{"arms": [...], "ledger_path": ..}`)
    is accepted so run metadata can live in the same file."""
    path = Path(state_dir) / "pairs" / pair_id / "summary.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("arms")
    return data if isinstance(data, list) else None


def recorded_ledger(state_dir: Path, pair_id: str) -> Path | None:
    """The ledger a pair says it ran on, if the run recorded one.

    Two accepted shapes so the metadata can live either beside the summary or
    inside it:
      pairs/<pair>/ledger.json  -> {"ledger_path": "..."}
      summary.json              -> {"ledger_path": "...", "arms": [...]}
    A path that does not exist is reported as None, never silently followed.
    """
    pair_dir = Path(state_dir) / "pairs" / pair_id
    for path in (pair_dir / "ledger.json", pair_dir / "summary.json"):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            raw = data.get("ledger_path")
            if raw:
                candidate = Path(raw)
                if not candidate.is_absolute():
                    candidate = (Path.cwd() / candidate)
                return candidate if candidate.exists() else None
    return None


def _relative(path: Path) -> str:
    """Repo-relative where possible; never leaks an absolute machine path."""
    try:
        return Path(path).resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return Path(path).name


# Ledger filenames the pilot is known to have used. This is a *fallback*, not the
# source of truth: `setup()` now allocates `stage2c-runN.db`, so a hard-coded list
# would make a brand-new, perfectly valid experiment look unrecoverable.
HISTORICAL_LEDGERS = ("stage2c.db", "stage2c-pair2.db", "stage2c-pair2-final.db")
# Every ledger `setup()` ever created, one absolute path per line, in order.
LEDGER_LOG = "contrast-ledgers.txt"


def candidate_ledgers(state_dir: Path, hint: Path | None = None) -> list[Path]:
    """Every ledger worth reading, most specific first, deduplicated.

    Order: an explicit hint, then the run log (newest last, so reversed), then
    any `stage2c*.db` present, then the historical names. Globbing is what makes
    `stage2c-runN.db` findable at all; the old fixed tuple did not.
    """
    state = Path(state_dir)
    seen: list[Path] = []

    def add(path: Path | None) -> None:
        if path is None:
            return
        path = Path(path)
        if path.exists() and path.suffix == ".db" and path not in seen:
            seen.append(path)

    add(hint)

    log = state / LEDGER_LOG
    if log.exists():
        try:
            lines = [ln.strip() for ln in
                     log.read_text(encoding="utf-8").splitlines() if ln.strip()]
        except OSError:
            lines = []
        for line in reversed(lines):
            add(Path(line))
    for path in sorted(state.glob("stage2c*.db"), reverse=True):
        add(path)
    for name in HISTORICAL_LEDGERS:
        add(state / name)
    add(state / "stage2.db")
    return seen


def _session_map(state_dir: Path, hint: Path | None = None) -> dict[str, dict]:
    """Best-effort session/attempt lookup from the surviving ledgers.

    Read-only. A missing entry stays missing - it is never fabricated, and an
    open ledger that has already been reset for the next pair simply returns
    nothing for tasks it no longer contains.
    """
    import sqlite3

    found: dict[str, dict] = {}
    for path in candidate_ledgers(state_dir, hint):
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        except sqlite3.Error:
            continue
        try:
            rows = conn.execute(
                "select work_item_id, session_id, attempt_id "
                "from session_references").fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            conn.close()
        for work_item_id, session_id, attempt_id in rows:
            found.setdefault(work_item_id, {
                "session_id": session_id, "attempt_id": attempt_id})
    return found


def _still_in_a_ledger(state_dir: Path,
                       expected: dict[str, str],
                       hint: Path | None = None) -> bool:
    """Can ONE surviving ledger still substantiate every task with its verdict?

    `expected` maps task_id -> the verdict the archive recorded. A row that still
    exists but whose verdict has reverted to `pending` proves nothing: the point
    of `ledger_recoverable` is that the ledger still supports the archived
    verdict, not that some row with the same id is lying around. All tasks must
    be substantiated by the *same* ledger - two halves of a pair held in two
    different files is not one recoverable measurement.
    """
    if not expected:
        return False
    for path in candidate_ledgers(state_dir, hint):
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        except sqlite3.Error:
            continue
        try:
            matched = 0
            for task_id, wanted in expected.items():
                row = conn.execute(
                    "select verdict from work_items where id=?", (task_id,)
                ).fetchone()
                if row and (row[0] or "").strip() == wanted:
                    matched += 1
            if matched == len(expected):
                return True
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    return False


def build_archived_pair(state_dir: Path, pair_id: str) -> dict | None:
    """pair2 / pair3: from the per-pair archive written when they finished.

    `ledger_recoverable` is derived, not asserted: it is true only if the pair's
    own task rows (with their verdicts) still exist in a surviving ledger. What
    it does NOT mean is "the whole run is reconstructible": pair2's rows and
    verdicts survive, but its `session_references` table is empty, so the
    Session/Attempt link is gone and `validate_archives()` reports the set as
    INCOMPLETE. The two facts use two fields (`ledger_recoverable` vs the
    per-arm `session_id`/`attempt_id`) precisely so neither hides the other.
    """
    rows = read_summary(state_dir, pair_id)
    if rows is None:
        return None
    hint = recorded_ledger(state_dir, pair_id)
    sessions = _session_map(state_dir, hint)
    enriched = []
    for row in rows:
        item = dict(row)
        extra = sessions.get(row.get("task_id") or "", {})
        item.setdefault("session_id", extra.get("session_id"))
        item.setdefault("attempt_id", extra.get("attempt_id"))
        enriched.append(item)

    # Compare against the verdict the archive recorded, not merely the row id.
    expected_verdicts = {str(row.get("task_id") or ""): str(row.get("verdict") or "")
                         for row in rows if row.get("task_id")}
    recoverable = _still_in_a_ledger(state_dir, expected_verdicts, hint)
    source = {
        "kind": "per-pair archive written at run time",
        "summary": f".poc-state/t62/pairs/{pair_id}/summary.json",
        "session_lookup": "read-only SessionReference lookup in the "
                          "surviving contrast ledgers",
        "derived_from_rerun": False,
        "verdicts_still_in_ledger": recoverable,
    }
    if hint is not None:
        # The ledger this pair actually ran on, when the run wrote it down. A
        # fixed filename list cannot work now that every round allocates a new
        # file (`stage2c-runN.db`).
        source["ledger_path"] = _relative(hint)
        source["ledger_from"] = "pair run metadata"
    else:
        source["ledger_from"] = ("not recorded for this pair; discovered by "
                                 "scanning .poc-state/t62 for stage2c*.db")
    record = normalize_pair(
        pair_id, enriched,
        source=source,
        evidence_level="archived_verdict",
        ledger_recoverable=recoverable,
    )
    if not recoverable:
        record["notes"].append(
            "No single surviving ledger still holds both task rows WITH the "
            "verdicts recorded here, so the original ledger cannot substantiate "
            "this pair today; only this "
            "archive can.")
    missing = [arm["task_id"] for arm in record["arms"]
               if not arm["session_id"] or not arm["attempt_id"]]
    if missing:
        record["notes"].append(
            "SessionReference/Attempt for "
            f"{', '.join(missing)} is not retained any more; the verdict is "
            "still the one the Verifier wrote at run time, but the transcript "
            "is unavailable and no tool-call count may be derived for it.")
    return record


def build_historical_pair1(legacy_record: dict) -> dict | None:
    """pair1: seeded once from the record written before its ledger was lost.

    `T62-stage2-memory-contrast.json` still holds pair 1's task ids, verdicts,
    head commits and durations. That file is the surviving contemporaneous
    record, so it seeds the archive; afterwards the archive is authoritative and
    this function is not called again.
    """
    for detail in legacy_record.get("pair_detail") or []:
        # Two shapes are accepted because the summary file was itself rewritten
        # in the G1 format: {"pair": "pair1 (...)"} and {"pair_id": "pair1"}.
        named = str(detail.get("pair") or detail.get("pair_id") or "")
        if not named.startswith("pair1"):
            continue
        rows = [dict(arm) for arm in detail.get("arms") or []]
        for row in rows:
            row.setdefault("state", "")
            # An archive-shaped row has the same facts under the same names.
            row.setdefault("task_id", row.get("id") or "")
        record = normalize_pair(
            "pair1", rows,
            source={
                "kind": "contemporaneous record written before the incident",
                "file": "evidence/project/T62-stage2-memory-contrast.json",
                "reexecuted": False,
                "session_lookup": "not preserved in the surviving record",
            },
            evidence_level="historical_record_only",
            ledger_recoverable=False,
            notes=[
                "The pair's own ledger lost its -wal file during cleanup and "
                "silently reverted to the queued state, so it can no longer "
                "substantiate this pair.",
                "Only task_id, verdict, head_commit and duration survived; "
                "session, attempt, assertions and transcript did not.",
                "This pair is cited, not re-measured, and its evidence level is "
                "deliberately the weakest of the three.",
            ],
        )
        record["ledger_state_seen_today"] = "queued / pending (damaged, no rows)"
        return record
    return None


# --------------------------------------------------------------------------- #
# archive set
# --------------------------------------------------------------------------- #
def load_archive(directory: Path, pair_id: str) -> dict | None:
    path = pair_path(directory, pair_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def collect_archives(state_dir: Path, archive_dir: Path,
                     legacy_record: dict | None = None) -> tuple[dict, list[str]]:
    """Load every pair archive, seeding the ones that do not exist yet.

    Order matters only for seeding: pair1 is seeded from the pre-incident record
    and then never rebuilt, so a later re-run cannot change it (and cannot
    upgrade it either).
    """
    archive_dir = Path(archive_dir)
    pairs: dict[str, dict] = {}
    seeded: list[str] = []

    for pair_id in PAIR_IDS:
        existing = load_archive(archive_dir, pair_id)
        if existing is not None:
            pairs[pair_id] = existing
            continue
        if pair_id == "pair1":
            record = build_historical_pair1(legacy_record or {})
        else:
            record = build_archived_pair(state_dir, pair_id)
        if record is not None:
            pairs[pair_id] = record
            seeded.append(pair_id)
    return pairs, seeded


def audit_archive(pair_id: str, record: dict, *,
                  expected_level: str | None = None) -> list[str]:
    """Re-check a *loaded* archive from scratch. Returns structural problems.

    A stored `"complete": true, "problems": []` is a claim made by whoever wrote
    the file, not a fact about it: any edit to `arms` would otherwise go
    unnoticed because nothing recomputes it. Everything below is derived from
    `arms` alone, so hand-editing an archive cannot silently pass.
    """
    problems: list[str] = []

    if not isinstance(record, dict):
        return [f"{pair_id}: archive is not an object"]

    if record.get("schema") != ARCHIVE_SCHEMA:
        problems.append(f"{pair_id}: unknown archive schema "
                        f"{record.get('schema')!r}")
    if record.get("pair_id") != pair_id:
        problems.append(f"{pair_id}: record says pair_id "
                        f"{record.get('pair_id')!r}, but it is stored/filed as "
                        f"{pair_id!r}")

    level = record.get("evidence_level")
    if level not in EVIDENCE_LEVELS:
        problems.append(f"{pair_id}: unknown evidence level {level!r}")
    if expected_level is not None and level != expected_level:
        problems.append(f"{pair_id}: evidence level is {level!r}, expected "
                        f"{expected_level!r}")

    arms = record.get("arms")
    if not isinstance(arms, list):
        problems.append(f"{pair_id}: `arms` is {type(arms).__name__}, not a list")
        arms = []

    # Arm count, then roles, then ids, then verdicts - recomputed, never trusted.
    if len(arms) != 2:
        problems.append(f"{pair_id}: arm count is {len(arms)}, expected exactly 2")

    roles: dict[str, int] = {}
    ids: list[str] = []
    for arm in arms:
        if not isinstance(arm, dict):
            problems.append(f"{pair_id}: an arm is {type(arm).__name__}, not an object")
            continue
        role = arm.get("role")
        roles[role] = roles.get(role, 0) + 1
        task_id = arm.get("task_id") or ""
        if not task_id:
            problems.append(f"{pair_id}: an arm has no task_id")
        else:
            ids.append(task_id)
        verdict = arm.get("verdict")
        if verdict not in TERMINAL_VERDICTS:
            problems.append(f"{pair_id}: {task_id or '<unknown>'}: verdict "
                            f"{verdict!r} is not terminal {TERMINAL_VERDICTS}")
        state = arm.get("state")
        if state and state not in TERMINAL_STATES:
            problems.append(f"{pair_id}: {task_id or '<unknown>'}: state "
                            f"{state!r} is not terminal {TERMINAL_STATES}")

    if roles.get(ROLE_WITH_MEMORY) != 1:
        problems.append(f"{pair_id}: expected exactly one {ROLE_WITH_MEMORY} arm, "
                        f"got {roles.get(ROLE_WITH_MEMORY, 0)}")
    if roles.get(ROLE_WITHOUT_MEMORY) != 1:
        problems.append(f"{pair_id}: expected exactly one {ROLE_WITHOUT_MEMORY} "
                        f"arm, got {roles.get(ROLE_WITHOUT_MEMORY, 0)}")
    unknown_roles = [r for r in roles if r not in
                     (ROLE_WITH_MEMORY, ROLE_WITHOUT_MEMORY)]
    if unknown_roles:
        problems.append(f"{pair_id}: unknown arm role(s) {unknown_roles}")

    if len(ids) != len(set(ids)):
        problems.append(f"{pair_id}: task ids are not distinct inside the pair: {ids}")

    # Recompute the stored summary and compare. A mismatch means the file was
    # edited after it was written.
    recomputed_verdicts = {arm.get("role"): arm.get("verdict")
                           for arm in arms if isinstance(arm, dict)}
    stored_verdicts = record.get("verdicts")
    if stored_verdicts != recomputed_verdicts:
        problems.append(f"{pair_id}: stored verdicts {stored_verdicts!r} do not "
                        f"match the arms {recomputed_verdicts!r}")
    recomputed_flipped = (len(set(recomputed_verdicts.values())) > 1
                          if len(recomputed_verdicts) == 2 else False)
    if bool(record.get("flipped")) != recomputed_flipped:
        problems.append(f"{pair_id}: stored flipped={record.get('flipped')!r} but "
                        f"the arms give flipped={recomputed_flipped}")

    if record.get("complete") is not True:
        problems.append(f"{pair_id}: archive is marked complete="
                        f"{record.get('complete')!r}")
    stored_problems = record.get("problems")
    if not isinstance(stored_problems, list):
        problems.append(f"{pair_id}: `problems` is "
                        f"{type(stored_problems).__name__}, not a list")
    elif stored_problems:
        problems.extend(f"{pair_id}: {p}" for p in stored_problems)

    # `ledger_recoverable` describes the ORIGINAL ledger, not the archive.
    # Getting this backwards is how pair1 would quietly gain credibility.
    recoverable = record.get("ledger_recoverable")
    if not isinstance(recoverable, bool):
        problems.append(f"{pair_id}: ledger_recoverable is "
                        f"{recoverable!r}, not a bool")
    elif level == "historical_record_only" and recoverable:
        problems.append(f"{pair_id}: historical pair must not claim a recoverable "
                        "ledger")
    else:
        # For an archived pair the flag must agree with what the source recorded
        # when the archive was built. A ledger whose verdict reverted to
        # 'pending' can no longer substantiate the archived verdict, so an
        # archive claiming `true` there would describe a stronger state than
        # exists.
        source_state = (record.get("source") or {}).get("verdicts_still_in_ledger")
        if isinstance(source_state, bool) and source_state != recoverable:
            problems.append(
                f"{pair_id}: ledger_recoverable={recoverable} contradicts the "
                f"source's verdicts_still_in_ledger={source_state}")

    if record.get("archive_readable") is not True:
        problems.append(f"{pair_id}: this record was loaded, so "
                        f"archive_readable must be true, got "
                        f"{record.get('archive_readable')!r}")

    return problems


def validate_archives(pairs: dict) -> dict:
    """Fail closed over the archive set. No pair, no pass.

    Every record is re-audited here rather than trusting what it says about
    itself, so a corrupted or hand-edited archive is BLOCKED, not read.
    """
    problems: list[str] = []
    incomplete: list[str] = []

    missing = [pair_id for pair_id in PAIR_IDS if pair_id not in pairs]
    if missing:
        problems.append(f"missing pair archive(s): {', '.join(missing)}")

    extra = [pid for pid in pairs if pid not in PAIR_IDS]
    if extra:
        problems.append(
            f"unexpected pair archive(s) {', '.join(extra)}: the experiment has "
            "exactly three pairs and re-running must not accumulate more")

    # pair1 is the only pair allowed to be historical, and it must be.
    expected_levels = {"pair1": "historical_record_only"}

    seen: dict[str, str] = {}
    for pair_id, record in pairs.items():
        problems.extend(audit_archive(
            pair_id, record, expected_level=expected_levels.get(pair_id)))
        if not isinstance(record, dict):
            continue
        for arm in record.get("arms") or []:
            if not isinstance(arm, dict):
                continue
            task_id = arm.get("task_id") or ""
            if not task_id:
                continue
            if task_id in seen:
                problems.append(
                    f"{pair_id}: task {task_id} already counted in {seen[task_id]}")
            else:
                seen[task_id] = pair_id
        if record.get("evidence_level") == "historical_record_only":
            # Allowed to lack session/attempt - that is exactly why its level is
            # lower. It must never be read as a re-verified measurement.
            continue
        lacking = [arm.get("task_id") for arm in record.get("arms") or []
                   if isinstance(arm, dict)
                   and (not arm.get("session_id") or not arm.get("attempt_id"))]
        if lacking:
            incomplete.append(
                f"{pair_id}: no SessionReference/Attempt retained for "
                f"{', '.join(filter(None, lacking))}")

    if problems:
        result = BLOCKED
    elif incomplete:
        result = INCOMPLETE
    elif len(pairs) != len(PAIR_IDS):
        result = BLOCKED
    else:
        result = COMPLETE
    return {"result": result, "problems": problems,
            "incomplete": incomplete, "task_ids": sorted(seen)}


def tally(pairs: dict) -> dict:
    counts = {ROLE_WITH_MEMORY: {"verified": 0, "rejected": 0},
              ROLE_WITHOUT_MEMORY: {"verified": 0, "rejected": 0}}
    for pair_id in PAIR_IDS:
        record = pairs.get(pair_id)
        if not record:
            continue
        for arm in record.get("arms") or []:
            verdict = arm.get("verdict")
            role = arm.get("role")
            if role in counts and verdict in counts[role]:
                counts[role][verdict] += 1
    return counts


def pair_summary(pairs: dict) -> list[dict]:
    out = []
    for pair_id in PAIR_IDS:
        record = pairs.get(pair_id)
        if not record:
            continue
        out.append({
            "pair": pair_id,
            "evidence_level": record.get("evidence_level"),
            "ledger_recoverable": bool(record.get("ledger_recoverable")),
            "with_memory": record.get("verdicts", {}).get(ROLE_WITH_MEMORY),
            "without_memory": record.get("verdicts", {}).get(ROLE_WITHOUT_MEMORY),
            "flipped": bool(record.get("flipped")),
            "complete": bool(record.get("complete")),
        })
    return out


def write_archives(archive_dir: Path, pairs: dict, writer) -> list[str]:
    """Write only the archives that do not exist yet (idempotent by pair id)."""
    archive_dir = Path(archive_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for pair_id in PAIR_IDS:
        record = pairs.get(pair_id)
        if record is None:
            continue
        path = pair_path(archive_dir, pair_id)
        if path.exists():
            continue
        writer(path, record)
        written.append(pair_id)
    return written


def read_pilot_row(ledger: Path, task_id: str) -> dict:
    """Read one pilot task (read-only). An absent row returns {} - never a guess."""
    import sqlite3

    ledger = Path(ledger)
    if not ledger.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True, timeout=15)
    except sqlite3.Error:
        return {}
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("select * from work_items where id=?", (task_id,)).fetchone()
        if row is None:
            return {}
        data = dict(row)
        ref = conn.execute(
            "select session_id, attempt_id from session_references "
            "where work_item_id=?", (task_id,)).fetchone()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    started, finished = data.get("started_at"), data.get("finished_at")
    return {
        "verdict": data.get("verdict"),
        "state": data.get("state"),
        "context_version": data.get("context_version"),
        "head_commit": data.get("head_commit"),
        "duration_s": (round(finished - started, 1)
                       if started and finished else None),
        "prompt_chars": len(data.get("effective_prompt") or ""),
        "session_id": (ref["session_id"] if ref else None),
        "attempt_id": (ref["attempt_id"] if ref else None),
    }


def arm_by_task(pairs: dict) -> dict[str, tuple[str, dict]]:
    index = {}
    for pair_id, record in pairs.items():
        for arm in record.get("arms") or []:
            if arm.get("task_id"):
                index[arm["task_id"]] = (pair_id, arm)
    return index
