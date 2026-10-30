"""T33: deterministic M3 Session Registry / Handoff boundaries (no Worker).

Covers the locally verifiable entries of the M3 checklist in KNOWN_ISSUES.md:

M3-03  a recorded checkpoint stays usable when the source worktree directory is
       gone, as long as it is available and integrated in the target workspace.
M3-04  a pre-M3 ledger (session_references without node_id, duplicate attempt
       bindings, NULL placeholders) migrates without data loss; the strict write
       path still refuses ambiguous attempts.
M3-05  two independent SQLite connections racing to bind the same Attempt:
       exactly one wins, the other gets an explicit SessionBindingConflict.
M3-06  a conflicting Snapshot in _save_snapshot is escalated (event +
       needs_human) and the first binding stays immutable; the known
       WorkItem/registry divergence is recorded, not hidden.
M3-07  a late Snapshot of an older Attempt conflicts explicitly; the event's
       attempt association (current attempt) is recorded as the known window.
M3-08  target HEAD drift after prepare is caught by the submit-time re-check
       (HandoffNotReady / HandoffStale); drift after a successful submit does
       not mutate the already frozen WorkItem.
M3-09  untracked or uncommitted files in the source worktree block the
       checkpoint (documented conservative guard).
M3-10  reusing a handoff idempotency key after context drift raises an explicit
       HandoffIdempotencyConflict instead of creating a second task.
M3-11  CLI project submit routes queued work to the project adapter, and the
       Runtime claim path picks it up.
M3-12  the Session Registry projection labels placeholders and dangling rows
       explicitly (legacy_placeholder / dangling_* / legacy_unscoped_node).

No Worker is started. Fixtures live under sandbox/m3-handoff and the ledger is
a throw-away file under .poc-state.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import TaskSnapshot, TaskState  # noqa: E402
from controlplane.handoff import (HandoffIdempotencyConflict,  # noqa: E402
                                  HandoffNotReady, HandoffStale)
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.project_context import ContextEntry  # noqa: E402
from controlplane.project_service import ProjectService  # noqa: E402
from controlplane.store import SessionBindingConflict, TaskStore  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

FIXTURE = ROOT / "sandbox" / "m3-handoff"
REPO_A = FIXTURE / "repo-a"
REPO_B = FIXTURE / "repo-b"
LEDGER = ROOT / ".poc-state" / "t33-m3-handoff.db"
LEGACY_LEDGER = ROOT / ".poc-state" / "t33-m3-legacy.db"
DUP_LEDGER = ROOT / ".poc-state" / "t33-m3-duplicates.db"
CLI_LEDGER = ROOT / ".poc-state" / "t33-m3-cli.db"
CLI = ROOT / "scripts" / "controlplane_cli.py"
JOB_SEQ = iter(range(1, 1000))


class AdapterRef:
    """Name-only Adapter: dispatch is never called in this test."""

    name = "stub"


def git(path: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(path), *args], check=False,
                          capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"git {args} failed in {path}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def make_repo(path: Path) -> None:
    if (path / ".git").exists():
        return
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True)
    git(path, "init", "-q")
    git(path, "config", "user.email", "t33@example.invalid")
    git(path, "config", "user.name", "T33")
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    git(path, "add", "seed.txt")
    git(path, "commit", "-q", "-m", "seed")
    git(path, "branch", "-M", "main")


def make_source(plane: ControlPlane, store: TaskStore, project_id: str,
                repo: Path, *, head_commit: str, worktree: str | None,
                session: str, state: str = None,
                verdict: str = "verified", job: str | None = None) -> str:
    """Enqueue one project task and mark it as a terminal, session-bound item."""
    n = next(JOB_SEQ)
    workspace = store.default_workspace(project_id)
    rec = plane.enqueue(f"m3 source task {n}", str(repo),
                        title=f"m3-source-{n}", project_id=project_id,
                        workspace_id=workspace["id"])
    task_id = rec["task_id"]
    job = job or f"job-{n}"
    store.add_attempt(task_id, worker_job_id=job)
    store.observe_session_reference(
        work_item_id=task_id, worker_job_id=job, node_id="local",
        worker_instance_id="wi-t33", adapter="stub", session_id=session)
    store.update(
        task_id, state=state or TaskState.SUCCEEDED.value, verdict=verdict,
        head_commit=head_commit, worktree=worktree,
        finished_at=1.0, artifacts=[], assertion_results=[])
    return task_id


def fresh() -> None:
    for stale in [LEDGER, LEGACY_LEDGER, DUP_LEDGER, CLI_LEDGER]:
        for f in [stale, *stale.parent.glob(stale.name + ".*")]:
            f.unlink(missing_ok=True)
    make_repo(REPO_A)
    make_repo(REPO_B)
    git(REPO_A, "checkout", "-q", "-f", "main")
    git(REPO_A, "reset", "-q", "--hard", "main")
    git(REPO_A, "clean", "-qfd")
    git(REPO_B, "checkout", "-q", "-f", "main")
    git(REPO_B, "reset", "-q", "--hard", "main")
    git(REPO_B, "clean", "-qfd")
    for branch in ("drift-08", "drift-08-late"):
        subprocess.run(["git", "-C", str(REPO_B), "branch", "-D", branch],
                       capture_output=True, timeout=30)


def conflict_events(store: TaskStore, task_id: str) -> list[dict]:
    return [e for e in store.events(task_id, limit=100)
            if e.get("type") == "session_binding_conflict"]


def cli(*args: str) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, str(CLI), "--ledger", str(CLI_LEDGER), "--compact", *args],
        capture_output=True, text=True, timeout=120, cwd=str(ROOT),
        encoding="utf-8", errors="replace")
    out = (proc.stdout or "").strip()
    try:
        payload = json.loads(out) if out else {}
    except json.JSONDecodeError:
        payload = {"_raw": out, "_stderr": (proc.stderr or "").strip()}
    payload["_exit"] = proc.returncode
    return proc.returncode, payload


def m03_checkpoint_without_source_worktree(service: ProjectService,
                                           store: TaskStore,
                                           project_a: str) -> dict:
    seed = git(REPO_A, "rev-parse", "HEAD")
    gone = REPO_A / "wt-gone"
    task = make_source(store=store, plane=service.plane, project_id=project_a,
                       repo=REPO_A, head_commit=seed,
                       worktree=str(gone), session="sess-03")
    prepared = service.prepare_handoff(task, "continue after handoff")
    ready_without_worktree = prepared["ready"]
    checkpoint = prepared["checkpoint"]

    # Contrast: a live worktree whose HEAD moved past the recorded checkpoint.
    ahead = make_source(store=store, plane=service.plane, project_id=project_a,
                        repo=REPO_A, head_commit=seed,
                        worktree=str(REPO_A), session="sess-03b")
    git(REPO_A, "commit", "-q", "--allow-empty", "-m", "ahead")
    blocked = service.prepare_handoff(ahead, "continue after handoff")
    git(REPO_A, "reset", "-q", "--hard", seed)

    return {
        "ready_without_source_worktree": ready_without_worktree,
        "checkpoint_available": checkpoint["available"],
        "checkpoint_integrated": checkpoint["integrated"],
        "source_worktree_missing": not Path(str(gone)).is_dir(),
        "live_worktree_drift_blocked": (
            not blocked["ready"]
            and any("HEAD no longer matches" in b for b in blocked["blockers"])),
    }


def m04_legacy_ledger_migration() -> dict:
    # Path A: a pre-M3 database whose session_references table lacks node_id.
    conn = sqlite3.connect(str(LEGACY_LEDGER))
    conn.execute(
        "CREATE TABLE session_references (id TEXT PRIMARY KEY, project_id TEXT"
        " NOT NULL, workspace_id TEXT, worker_instance_id TEXT, adapter TEXT,"
        " session_id TEXT, work_item_id TEXT, attempt_id TEXT, created_at REAL,"
        " updated_at REAL)")
    conn.execute(
        "INSERT INTO session_references (id, project_id, workspace_id,"
        " worker_instance_id, adapter, session_id, work_item_id, attempt_id,"
        " created_at, updated_at) VALUES ('sr-legacy-1','prj-legacy',NULL,NULL,"
        "NULL,NULL,NULL,NULL,0,0)")
    conn.commit()
    conn.close()

    store = TaskStore(LEGACY_LEDGER)
    columns = {row["name"] for row in
               store._conn.execute("PRAGMA table_info(session_references)").fetchall()}
    projection = store.session_references("prj-legacy")
    store.close()
    legacy_opened = "node_id" in columns
    legacy_placeholder = (len(projection) == 1
                          and projection[0]["binding_status"] == "legacy_placeholder")

    # Path B: duplicate historical attempt bindings on a fully populated ledger.
    seed = git(REPO_A, "rev-parse", "HEAD")
    dup_store = TaskStore(DUP_LEDGER)
    plane = ControlPlane(AdapterRef(), dup_store, require_registered_projects=True)
    project = plane.register_project("t33-dup", str(REPO_A), default_adapter="stub")
    workspace = dup_store.default_workspace(project["id"])
    rec = plane.enqueue("m3-04 duplicate binding source", str(REPO_A),
                        title="m3-04", project_id=project["id"],
                        workspace_id=workspace["id"])
    task_id = rec["task_id"]
    dup_store.add_attempt(task_id, worker_job_id="job-dup-1")
    ref = dup_store.observe_session_reference(
        work_item_id=task_id, worker_job_id="job-dup-1", node_id="local",
        worker_instance_id="wi-dup", adapter="stub", session_id="sess-dup-1")
    attempt_id = ref["attempt_id"]
    dup_store.close()

    raw = sqlite3.connect(str(DUP_LEDGER))
    raw.execute("DROP INDEX IF EXISTS ux_session_ref_attempt")
    raw.execute("DROP TRIGGER IF EXISTS trg_session_ref_attempt_insert")
    raw.execute(
        "INSERT INTO session_references (id, project_id, workspace_id, node_id,"
        " worker_instance_id, adapter, session_id, work_item_id, attempt_id,"
        " created_at, updated_at) VALUES ('sr-dup-2', ?, ?, 'local', 'wi-dup',"
        " 'stub', 'sess-dup-2', ?, ?, 1, 1)",
        (project["id"], ref["workspace_id"], task_id, attempt_id))
    raw.commit()
    raw.close()

    reopened = TaskStore(DUP_LEDGER)
    rows = reopened._conn.execute(
        "SELECT session_id FROM session_references WHERE attempt_id=?"
        " ORDER BY created_at ASC", (attempt_id,)).fetchall()
    bound_sessions = [row["session_id"] for row in rows]
    strict_conflict = False
    try:
        reopened.observe_session_reference(
            work_item_id=task_id, worker_job_id="job-dup-1", node_id="local",
            worker_instance_id="wi-dup", adapter="stub", session_id="sess-dup-3")
    except SessionBindingConflict:
        strict_conflict = True
    index_kept_out = reopened._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
        " AND name='ux_session_ref_attempt'").fetchone()
    trigger_active = False
    try:
        reopened._conn.execute(
            "INSERT INTO session_references (id, project_id, workspace_id,"
            " node_id, worker_instance_id, adapter, session_id, work_item_id,"
            " attempt_id, created_at, updated_at) VALUES ('sr-dup-4', ?, ?,"
            " 'local', 'wi-dup', 'stub', 'sess-dup-4', ?, ?, 2, 2)",
            (project["id"], ref["workspace_id"], task_id, attempt_id))
        reopened._conn.commit()
    except sqlite3.IntegrityError:
        trigger_active = True
        reopened._conn.rollback()
    reopened.close()

    return {
        "legacy_ledger_opens": legacy_opened,
        "node_id_column_added": legacy_opened,
        "legacy_placeholder_labelled": legacy_placeholder,
        "duplicate_rows_preserved": set(bound_sessions) == {"sess-dup-1", "sess-dup-2"},
        "strict_write_path_refuses_ambiguity": strict_conflict,
        "unique_index_not_created_on_conflict": index_kept_out is None,
        "insert_trigger_rearmed_after_reopen": trigger_active,
    }


def m05_concurrent_binding(store: TaskStore, plane: ControlPlane,
                           project_a: str) -> dict:
    seed = git(REPO_A, "rev-parse", "HEAD")
    task = make_source(store=store, plane=plane, project_id=project_a,
                       repo=REPO_A, head_commit=seed,
                       worktree=str(REPO_A / "wt-unused-05"), session="sess-05-tomb",
                       verdict="inconclusive")
    # Drop the pre-made binding so two writers race on a fresh attempt.
    fresh_task = make_source(store=store, plane=plane, project_id=project_a,
                             repo=REPO_A, head_commit=seed,
                             worktree=str(REPO_A / "wt-unused-05b"),
                             session="sess-05-seed")
    # Observe via a second attempt to leave one attempt unbound for the race.
    workspace = store.default_workspace(project_a)
    rec = plane.enqueue("m3-05 race source", str(REPO_A), title="m3-05",
                        project_id=project_a, workspace_id=workspace["id"])
    race_task = rec["task_id"]
    store.add_attempt(race_task, worker_job_id="job-race")
    _ = task, fresh_task  # keep fixtures referenced

    store_one = TaskStore(LEDGER)
    store_two = TaskStore(LEDGER)
    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, str | None, str | None]] = []

    def bind(st: TaskStore, session: str, tag: str) -> None:
        barrier.wait()
        try:
            st.observe_session_reference(
                work_item_id=race_task, worker_job_id="job-race", node_id="local",
                worker_instance_id="wi-race", adapter="stub", session_id=session)
            outcomes.append((tag, session, None))
        except SessionBindingConflict as exc:
            outcomes.append((tag, session, f"conflict: {exc}"))
        except Exception as exc:  # noqa: BLE001 - recorded verbatim
            outcomes.append((tag, session, f"{type(exc).__name__}: {exc}"))

    threads = [threading.Thread(target=bind, args=(store_one, "sess-A", "one")),
               threading.Thread(target=bind, args=(store_two, "sess-B", "two"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    winners = [o for o in outcomes if o[2] is None]
    conflicts = [o for o in outcomes if o[2] and o[2].startswith("conflict:")]
    final = store.session_reference_for_attempt(
        store.attempts(race_task)[-1]["id"])
    store_one.close()
    store_two.close()
    return {
        "exactly_one_winner": len(winners) == 1,
        "loser_is_explicit_conflict": len(conflicts) == 1,
        "final_binding_matches_winner": (
            final is not None and final["session_id"] == winners[0][1]),
        "outcomes": [{"side": o[0], "session": o[1], "result": o[2]}
                     for o in outcomes],
    }


def m06_snapshot_conflict_consistency(store: TaskStore, plane: ControlPlane,
                                      project_a: str) -> dict:
    seed = git(REPO_A, "rev-parse", "HEAD")
    task = make_source(store=store, plane=plane, project_id=project_a,
                       repo=REPO_A, head_commit=seed,
                       worktree=str(REPO_A / "wt-06"), session="sess-D1")
    attempt_id = store.get(task)["current_attempt_id"]
    first = store.session_reference_for_attempt(attempt_id)

    snap = TaskSnapshot(task_id=task, worker_job_id="job-d1",
                        state=TaskState.SUCCEEDED, session_id="sess-D2")
    saved = plane._save_snapshot(task, snap, patch={
        "state": TaskState.SUCCEEDED.value, "session_id": "sess-D2"})

    events = conflict_events(store, task)
    registry = store.session_reference_for_attempt(attempt_id)
    return {
        "conflict_event_recorded": len(events) == 1,
        "work_item_needs_human": saved.get("queue_status") == "needs_human",
        "work_item_warns": any("SESSION_BINDING_CONFLICT" in w
                               for w in saved.get("warnings") or []),
        "first_binding_immutable": (
            registry is not None and registry["session_id"] == "sess-D1"),
        # Known atomicity window (M3-06): the WorkItem's session_id is the
        # conflicting snapshot's, the immutable registry keeps the first one.
        "work_item_session_id": saved.get("session_id"),
        "registry_session_id": (registry or {}).get("session_id"),
        "registry_first_created_before": (
            first is not None and registry is not None
            and first["id"] == registry["id"]),
    }


def m07_late_snapshot_attempt_association(store: TaskStore, plane: ControlPlane,
                                          project_a: str) -> dict:
    seed = git(REPO_A, "rev-parse", "HEAD")
    task = make_source(store=store, plane=plane, project_id=project_a,
                       repo=REPO_A, head_commit=seed,
                       worktree=str(REPO_A / "wt-07"), session="sess-E1",
                       job="job-e1")
    store.add_attempt(task, worker_job_id="job-e2")
    current_attempt = store.get(task)["current_attempt_id"]
    old_attempt = [a for a in store.attempts(task)
                   if a["worker_job_id"] == "job-e1"][0]["id"]

    snap = TaskSnapshot(task_id=task, worker_job_id="job-e1",
                        state=TaskState.SUCCEEDED, session_id="sess-E2")
    saved = plane._save_snapshot(task, snap, patch={
        "state": TaskState.SUCCEEDED.value, "session_id": "sess-E2"})

    events = conflict_events(store, task)
    event_attempt_ids = [e.get("attempt_id") for e in events]
    old_binding = store.session_reference_for_attempt(old_attempt)
    return {
        "conflict_event_recorded": len(events) == 1,
        "work_item_needs_human": saved.get("queue_status") == "needs_human",
        "old_attempt_binding_unchanged": (
            old_binding is not None and old_binding["session_id"] == "sess-E1"),
        # Known association window (M3-07): the event is filed under the
        # current attempt, not the late snapshot's own older attempt.
        "event_attempt_is_current": event_attempt_ids == [current_attempt],
        "late_snapshot_attempt": old_attempt,
        "event_attempt_ids": event_attempt_ids,
    }


def m08_head_drift_windows(service: ProjectService, store: TaskStore,
                           plane: ControlPlane, project_b: str) -> dict:
    seed = git(REPO_B, "rev-parse", "HEAD")
    task = make_source(store=store, plane=plane, project_id=project_b,
                       repo=REPO_B, head_commit=seed,
                       worktree=str(REPO_B / "wt-gone-08"), session="sess-08")
    prepared = service.prepare_handoff(task, "continue goal after drift")
    ready_before = prepared["ready"]

    # (a) drift before submit, no expected signature: re-check must block.
    git(REPO_B, "checkout", "-q", "--orphan", "drift-08")
    git(REPO_B, "commit", "-q", "-m", "orphan")
    not_ready = False
    not_ready_blockers: list[str] = []
    try:
        service.submit_handoff(task, "continue goal after drift")
    except HandoffNotReady as exc:
        not_ready = True
        not_ready_blockers = list(exc.blockers)
    git(REPO_B, "checkout", "-q", "-f", "main")

    # (b) drift that keeps the checkpoint integrated, but changes the signature.
    prepared2 = service.prepare_handoff(task, "continue goal after drift")
    signature2 = prepared2["handoff_signature"]
    git(REPO_B, "commit", "-q", "--allow-empty", "-m", "forward")
    stale = False
    try:
        service.submit_handoff(task, "continue goal after drift",
                               expected_handoff_signature=signature2)
    except HandoffStale:
        stale = True

    # (c) submit succeeds; later drift must not mutate the frozen WorkItem.
    record = service.submit_handoff(task, "continue goal after drift")
    frozen_prompt = record["effective_prompt"]
    frozen_signature = (record.get("metadata") or {}).get("handoff_signature")
    frozen_context = (record.get("metadata") or {}).get("handoff_context") or {}
    git(REPO_B, "checkout", "-q", "--orphan", "drift-08-late")
    git(REPO_B, "commit", "-q", "-m", "orphan-late")
    after = store.get(record["task_id"])
    git(REPO_B, "checkout", "-q", "-f", "main")

    return {
        "ready_before_drift": ready_before,
        "submit_recheck_blocks_unintegrated_head": (
            not_ready
            and any("not contained in target workspace HEAD" in b
                    for b in not_ready_blockers)),
        "expected_signature_detects_drift": stale,
        "frozen_prompt_unchanged_after_late_drift": (
            after.get("effective_prompt") == frozen_prompt),
        "frozen_signature_unchanged_after_late_drift": (
            (after.get("metadata") or {}).get("handoff_signature")
            == frozen_signature),
        "frozen_context_unchanged_after_late_drift": (
            (after.get("metadata") or {}).get("handoff_context")
            == frozen_context),
    }


def m09_untracked_worktree_blocks(service: ProjectService, plane: ControlPlane,
                                  store: TaskStore, project_a: str) -> dict:
    seed = git(REPO_A, "rev-parse", "HEAD")
    task = make_source(store=store, plane=plane, project_id=project_a,
                       repo=REPO_A, head_commit=seed,
                       worktree=str(REPO_A), session="sess-09")
    (REPO_A / "untracked-run-artifact.txt").write_text("junk\n", encoding="utf-8")
    untracked = service.prepare_handoff(task, "continue after handoff")
    git(REPO_A, "clean", "-qfd")

    (REPO_A / "seed.txt").write_text("seed\nmutated\n", encoding="utf-8")
    dirty = service.prepare_handoff(task, "continue after handoff")
    git(REPO_A, "reset", "-q", "--hard", seed)

    return {
        "untracked_blocks": (
            not untracked["ready"]
            and any("uncommitted changes" in b for b in untracked["blockers"])),
        "modified_tracked_blocks": (
            not dirty["ready"]
            and any("uncommitted changes" in b for b in dirty["blockers"])),
    }


def m10_idempotency_key_after_context_drift(service: ProjectService,
                                            store: TaskStore, plane: ControlPlane,
                                            project_b: str) -> dict:
    seed = git(REPO_B, "rev-parse", "HEAD")
    task = make_source(store=store, plane=plane, project_id=project_b,
                       repo=REPO_B, head_commit=seed,
                       worktree=str(REPO_B / "wt-gone-10"), session="sess-10")
    record = service.submit_handoff(task, "continue goal",
                                    idempotency_key="handoff-key-10")
    first_task_id = record["task_id"]
    version_before = (record.get("metadata") or {}).get("handoff_context", {}).get(
        "context_version")

    service.add_context_entry(project_b, ContextEntry(
        project_id=project_b, kind="background", title="post-submit fact",
        body="new fact", verification="unverified"))
    conflict = False
    second_task_id = None
    try:
        again = service.submit_handoff(task, "continue goal",
                                       idempotency_key="handoff-key-10")
        second_task_id = again["task_id"]
    except HandoffIdempotencyConflict:
        conflict = True

    return {
        "first_submit_enqueued": bool(first_task_id),
        "context_version_before": version_before,
        "same_key_after_drift_conflicts": conflict and second_task_id is None,
    }


def m11_cli_adapter_routing() -> dict:
    code, _ = cli("project", "add", "m3-cli", str(REPO_A), "--adapter", "stub")
    added = code == 0
    code, submitted = cli("project", "submit", "m3-cli",
                          "verify runtime claim routing",
                          "--idempotency-key", "cli-m3-11")
    submit_ok = code == 0 and submitted.get("_exit") == 0

    store = TaskStore(CLI_LEDGER)
    rec = store.by_idempotency_key("cli-m3-11") or {}
    routed = rec.get("adapter") == "stub"
    claimable = False
    task_id = rec.get("task_id")
    if rec.get("queue_status") == "needs_approval":
        approvals = [a for a in store.approvals(task_id)
                     if a.get("decision") == "pending"]
        if approvals:
            store.decide_approval(approvals[0]["id"], "approved")
            store.release_approved_queue(rec.get("task_digest"))
    claimed = store.claim_next_ready("stub", owner="runtime-t33",
                                     concurrency_limit=4)
    claimable = bool(claimed) and claimed.get("task_id") == task_id
    ledger_only_claim = store.claim_next_ready("ledger-only", owner="runtime-x",
                                               concurrency_limit=4)
    store.close()
    return {
        "project_added": added,
        "submit_ok": submit_ok,
        "work_item_routed_to_project_adapter": routed,
        "runtime_claim_picks_it_up": claimable,
        "ledger_only_cannot_claim": ledger_only_claim is None,
        "queue_status": rec.get("queue_status"),
    }


def m12_dangling_projection(store: TaskStore, plane: ControlPlane,
                            project_a: str) -> dict:
    seed = git(REPO_A, "rev-parse", "HEAD")
    task = make_source(store=store, plane=plane, project_id=project_a,
                       repo=REPO_A, head_commit=seed,
                       worktree=str(REPO_A / "wt-12"), session="sess-12")
    workspace_id = store.get(task)["workspace_id"]
    unbound_attempt = store.add_attempt(task, worker_job_id="job-12-extra")["id"]
    now = 2.0
    rows = [
        # NULL session/attempt historical placeholder.
        ("sr-t33-1", project_a, workspace_id, "local", "wi-x", "stub",
         None, task, None),
        # Workspace no longer exists.
        ("sr-t33-2", project_a, "ws-gone", "local", "wi-x", "stub",
         "sess-gone-ws", task, "att-gone-1"),
        # Work item no longer exists.
        ("sr-t33-3", project_a, workspace_id, "local", "wi-x", "stub",
         "sess-gone-task", "task-gone", "att-gone-2"),
        # Pre-M3 row without node scope but otherwise intact.
        ("sr-t33-4", project_a, workspace_id, None, "wi-x", "stub",
         "sess-noscope", task, unbound_attempt),
    ]
    conn = store._conn
    for row in rows:
        conn.execute(
            "INSERT INTO session_references (id, project_id, workspace_id,"
            " node_id, worker_instance_id, adapter, session_id, work_item_id,"
            " attempt_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (*row, now, now))
    conn.commit()

    statuses = {ref["id"]: ref["binding_status"]
                for ref in store.session_references(project_a, limit=50)}
    return {
        "placeholder_labelled": statuses.get("sr-t33-1") == "legacy_placeholder",
        "dangling_workspace_labelled": (
            statuses.get("sr-t33-2") == "dangling_or_mismatched_workspace"),
        "dangling_work_item_labelled": (
            statuses.get("sr-t33-3") == "dangling_or_mismatched_work_item"),
        "unscoped_node_labelled": statuses.get("sr-t33-4") == "legacy_unscoped_node",
        "live_binding_still_bound": any(
            v == "bound" and k not in {"sr-t33-2", "sr-t33-3"}
            for k, v in statuses.items()),
    }


def main() -> int:
    fresh()
    store = TaskStore(LEDGER)
    try:
        plane = ControlPlane(AdapterRef(), store, require_registered_projects=True,
                             worker_instance_id="wi-t33")
        service = ProjectService(plane)
        project_a = plane.register_project(
            "t33-m3-a", str(REPO_A), default_adapter="stub")["id"]
        project_b = plane.register_project(
            "t33-m3-b", str(REPO_B), default_adapter="stub")["id"]

        r03 = m03_checkpoint_without_source_worktree(service, store, project_a)
        r04 = m04_legacy_ledger_migration()
        r05 = m05_concurrent_binding(store, plane, project_a)
        r06 = m06_snapshot_conflict_consistency(store, plane, project_a)
        r07 = m07_late_snapshot_attempt_association(store, plane, project_a)
        r08 = m08_head_drift_windows(service, store, plane, project_b)
        r09 = m09_untracked_worktree_blocks(service, plane, store, project_a)
        r10 = m10_idempotency_key_after_context_drift(service, store, plane,
                                                      project_b)
        r11 = m11_cli_adapter_routing()
        r12 = m12_dangling_projection(store, plane, project_a)

        sections = {"M3-03": r03, "M3-04": r04, "M3-05": r05, "M3-06": r06,
                    "M3-07": r07, "M3-08": r08, "M3-09": r09, "M3-10": r10,
                    "M3-11": r11, "M3-12": r12}
        checks = {f"{name}.{key}": bool(value)
                  for name, result in sections.items()
                  for key, value in result.items()
                  if isinstance(value, bool)}
        log = {
            "test": "T33-m3-handoff-deterministic",
            "worker_started": False,
            "covered": list(sections),
            "not_covered": [
                "M3-01 (requires a real CodeBuddy Worker)",
                "M3-02 (requires a real two-session end-to-end acceptance)",
            ],
            "sections": sections,
            "checks": checks,
            "result": "Pass" if all(checks.values()) else "Fail",
        }
        write_evidence(
            ROOT / "evidence" / "project" / "T33-m3-handoff-deterministic.json", log)
        print(json.dumps(log, ensure_ascii=False, indent=2, default=str))
        return 0 if log["result"] == "Pass" else 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
