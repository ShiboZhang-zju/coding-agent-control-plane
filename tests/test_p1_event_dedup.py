"""P1: repeated observation must not spam the Ledger.

The field report: one WorkItem (``cp-d10689e7567f``) accumulated ~75 identical
``project_context_updated`` events and repeated the same
``SIDE_EFFECT:undeclared_local_change`` warning on every refresh.

Both are consequences of the *same* mistake — treating "I observed the terminal
task again" as "something new happened":

* ``_sync_project_context`` applied the idempotent M2 update (which correctly
  replayed its receipts) and then appended a success event unconditionally;
* ``_snapshot_patch`` appended every side-effect violation it recomputed,
  including ones already in ``warnings``.

The fixes keep both properties that matter:

1. a genuinely new outcome (new attempt, another violation) is still recorded;
2. a crash between "receipts committed" and "event written" still recovers,
   because the event is keyed by the outcome signature, not by a process-local
   flag.

Verifier rejection semantics are untouched; that is asserted here as well so a
future "reduce noise" change cannot quietly weaken acceptance.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from adapters.base import (  # noqa: E402
    ArtifactAssertion,
    DispatchPolicy,
    TaskSnapshot,
    TaskState,
)
from controlplane.context_update import ProjectContextUpdateLoop  # noqa: E402
from controlplane.plane import (  # noqa: E402
    ControlPlane,
    merge_warnings,
    side_effect_warnings,
)
from controlplane.store import TaskStore  # noqa: E402
from controlplane.verify import capture_baseline, verdict_for  # noqa: E402


def _git(*args: str, cwd: Path) -> None:
    import subprocess
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   capture_output=True, text=True, timeout=60)


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=path)
    _git("config", "user.email", "p1@example.invalid", cwd=path)
    _git("config", "user.name", "P1", cwd=path)
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git("add", "-A", cwd=path)
    _git("commit", "-q", "-m", "seed", cwd=path)
    return path


class _Adapter:
    """Minimal adapter: one job whose snapshot the test controls."""

    name = "p1-stub"

    def __init__(self) -> None:
        self.state = TaskState.RUNNING
        self.cwd: str | None = None

    def collect_artifacts(self, _job_id):
        return []

    def get_task(self, _job_id, timeout_s=None):
        return TaskSnapshot(task_id="p1-job", worker_job_id="p1-job",
                            state=self.state, cwd=self.cwd, detail="p1")

    def reconcile(self):
        return {"source": "p1-stub", "total": 1, "new": [], "changed": [],
                "lost": [], "jobs": [{"id": "p1-job", "state": self.state.value}]}


def _build(tmp_path: Path, *, task_cwd: Path) -> tuple[TaskStore, ControlPlane, dict]:
    store = TaskStore(tmp_path / "ledger.db")
    adapter = _Adapter()
    adapter.cwd = str(task_cwd)
    updates = ProjectContextUpdateLoop(store, repo_root=tmp_path, owner="p1")
    plane = ControlPlane(adapter, store, policy=DispatchPolicy(), context_updates=updates)
    project = plane.register_project("p1-project", str(_repo(tmp_path / "proj")),
                                     default_adapter="p1-stub")
    return store, plane, project


def _terminal(plane: ControlPlane, store: TaskStore, project: dict) -> str:
    assertions = [ArtifactAssertion("file_exists", "made.txt")]
    task = plane.enqueue(
        "produce the artifact", str(project["root"]),
        assertions=assertions, project_id=project["id"])
    # A real dispatch captures this baseline; the test dispatches nothing (the
    # adapter is scripted), so it is captured here explicitly.  Without it
    # ``side_effects()`` has nothing to compare against.
    store.update(task["task_id"],
                 baseline=capture_baseline(str(project["root"]), assertions),
                 worker_job_id="p1-job",
                 current_attempt_id=str((store.attempts(task["task_id"]) or
                                         [{"id": "1"}])[0]["id"]))
    Path(project["root"], "made.txt").write_text("done\n", encoding="utf-8")
    plane.adapter.state = TaskState.SUCCEEDED
    plane.refresh_known_jobs()
    return task["task_id"]


def _events(store: TaskStore, task_id: str, type_: str) -> list[dict]:
    return [event for event in store.events(task_id, limit=1000)
            if event.get("type") == type_]


# --------------------------------------------------------------------------- #
# project_context_updated
# --------------------------------------------------------------------------- #
def test_repeated_refresh_records_one_context_update_event(tmp_path: Path):
    store, plane, project = _build(tmp_path, task_cwd=tmp_path / "proj")
    try:
        task_id = _terminal(plane, store, project)
        record = store.get(task_id)
        assert record["verdict"] == "verified"

        # Eight further sweeps see the same terminal task.
        for _ in range(8):
            plane.refresh_known_jobs()
            plane.update_project_context(task_id)

        events = _events(store, task_id, "project_context_updated")
        assert len(events) == 1, [e["payload"].get("status") for e in events]
        payload = events[0]["payload"]
        assert payload["status"] == "applied"
        assert payload["signature"].startswith("sha256:")
        assert {update["status"] for update in payload["updates"]} == {"applied"}
    finally:
        store.close()


def test_a_genuinely_new_outcome_is_still_recorded(tmp_path: Path):
    store, plane, project = _build(tmp_path, task_cwd=tmp_path / "proj")
    try:
        task_id = _terminal(plane, store, project)
        assert len(_events(store, task_id, "project_context_updated")) == 1

        # A new attempt with a different terminal verdict is new information:
        # its receipts and therefore its signature differ.
        store.add_attempt(task_id, "p1-job-2", reason="retry after failure")
        attempt_id = store.attempts(task_id)[-1]["id"]
        store.update(task_id, current_attempt_id=str(attempt_id),
                     state=TaskState.FAILED.value, verdict="rejected")
        plane.update_project_context(task_id)
        plane.update_project_context(task_id)

        events = _events(store, task_id, "project_context_updated")
        assert len(events) == 2
        assert events[0]["payload"]["signature"] != events[1]["payload"]["signature"]
        assert events[1]["payload"]["status"] in ("applied", "needs_merge")
    finally:
        store.close()


def test_crash_between_receipt_and_event_still_recovers_exactly_once(
        tmp_path: Path):
    """The dedup must be a ledger fact, not an in-memory "already logged" flag."""
    import sqlite3

    store, plane, project = _build(tmp_path, task_cwd=tmp_path / "proj")
    try:
        task_id = _terminal(plane, store, project)
        # Simulate: receipts committed, process died before the event was written.
        conn = sqlite3.connect(store.path)
        conn.execute("DELETE FROM events WHERE type='project_context_updated'")
        conn.commit()
        conn.close()
        assert _events(store, task_id, "project_context_updated") == []

        plane.update_project_context(task_id)          # recovery sweep
        plane.update_project_context(task_id)          # and the next one
        assert len(_events(store, task_id, "project_context_updated")) == 1
        # Receipts are untouched by the recovery (still exactly two, applied).
        receipts = store.context_update_receipts(task_id)
        assert len(receipts) == 2
        assert {row["status"] for row in receipts} == {"applied"}
    finally:
        store.close()


def test_busy_claim_records_no_success_event(tmp_path: Path):
    """Nothing was committed, so there is no outcome to record."""
    store, plane, project = _build(tmp_path, task_cwd=tmp_path / "proj")
    try:
        task_id = _terminal(plane, store, project)
        # Drop the receipts and hand the claim to another live owner.
        import sqlite3
        conn = sqlite3.connect(store.path)
        conn.execute("DELETE FROM context_update_receipts WHERE work_item_id=?", (task_id,))
        conn.execute("DELETE FROM events WHERE type='project_context_updated'")
        conn.commit()
        conn.close()
        attempt_id = store.get(task_id)["current_attempt_id"]
        from controlplane.context_update import context_update_key
        for kind, title in (
                ("progress", f"Follow-up for task {task_id}"),
                ("knowledge",
                 f"Execution evidence for task {task_id} attempt {attempt_id}")):
            store.claim_context_update(
                context_update_key(task_id, str(attempt_id), kind, title),
                project_id=project["id"], work_item_id=task_id,
                attempt_id=str(attempt_id), entry_kind=kind, entry_title=title,
                owner="another-runtime", lease_s=600)

        result = plane.update_project_context(task_id)
        assert result["status"] == "busy"
        assert _events(store, task_id, "project_context_updated") == []
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# SIDE_EFFECT warnings
# --------------------------------------------------------------------------- #
def test_side_effect_warnings_are_deduplicated_but_not_merged():
    violation = {"type": "undeclared_local_change", "cwd": "C:\\repo",
                 "entries": ["?? a.txt", "?? b.txt"]}
    first = side_effect_warnings([violation])
    second = side_effect_warnings([violation])
    assert first == second and len(first) == 1
    assert first[0].startswith("SIDE_EFFECT:undeclared_local_change:C:\\repo#")

    # A different set of undeclared files is a different finding, not the same.
    other = side_effect_warnings([{**violation, "entries": ["?? c.txt"]}])
    assert other != first

    cross = side_effect_warnings([{"type": "cross_project_change",
                                   "repo": "C:\\other"}])
    assert cross == ["SIDE_EFFECT:cross_project_change:C:\\other"]


def test_merge_warnings_only_appends_new_lines():
    merged = merge_warnings(["A: x", "B: y"], ["B: y", "C: z", "C: z"])
    assert merged == ["A: x", "B: y", "C: z"]
    assert merge_warnings(None, ["A"]) == ["A"]
    assert merge_warnings(["A"], None) == ["A"]


def _observe(plane: ControlPlane, store: TaskStore, task_id: str) -> dict:
    """One more observation of the same still-running WorkItem.

    ``refresh_known_jobs`` deliberately stops polling a terminal task, so the
    pathological case has to be reproduced directly on the observation path
    (``_save_snapshot`` -> ``_snapshot_patch``), which is what the runtime and
    the recovery paths call.
    """
    snapshot = TaskSnapshot(task_id=task_id, worker_job_id="p1-job",
                            state=TaskState.SUCCEEDED,
                            cwd=store.get(task_id)["cwd"], detail="p1")
    return plane._save_snapshot(task_id, snapshot)


def test_repeated_observation_does_not_accumulate_side_effect_warnings(
        tmp_path: Path):
    """The exact field symptom, at the plane level."""
    store, plane, project = _build(tmp_path, task_cwd=tmp_path / "proj")
    try:
        task_id = _terminal(plane, store, project)
        # An undeclared change the attempt was never allowed to make.
        (Path(project["root"]) / "stray.txt").write_text("oops\n", encoding="utf-8")
        for _ in range(10):
            record = _observe(plane, store, task_id)

        side_effect = [w for w in record["warnings"] if w.startswith("SIDE_EFFECT:")]
        assert len(side_effect) == len(set(side_effect)) == 1, side_effect
        # ... and the violation still cost the task its verification.
        assert record["verdict"] == "rejected"
        assert len(store.get(task_id)["warnings"]) == len(record["warnings"])
    finally:
        store.close()


def test_a_second_distinct_violation_is_still_recorded(tmp_path: Path):
    store, plane, project = _build(tmp_path, task_cwd=tmp_path / "proj")
    try:
        task_id = _terminal(plane, store, project)
        root = Path(project["root"])
        (root / "stray.txt").write_text("oops\n", encoding="utf-8")
        _observe(plane, store, task_id)
        (root / "second-stray.txt").write_text("again\n", encoding="utf-8")
        _observe(plane, store, task_id)
        record = _observe(plane, store, task_id)

        warnings = [w for w in record["warnings"] if w.startswith("SIDE_EFFECT:")]
        assert len(warnings) == 2, warnings
        assert len(set(warnings)) == 2
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# Verifier semantics must not change
# --------------------------------------------------------------------------- #
def test_verifier_still_rejects_side_effects_and_unowned_assertions():
    from controlplane.verify import Verdict

    assert verdict_for(
        TaskState.SUCCEEDED, True,
        [{"kind": "file_exists", "target": "x", "passed": True, "owned": True}],
        [{"type": "undeclared_local_change", "cwd": "C:\\repo"}]) is Verdict.REJECTED
    assert verdict_for(
        TaskState.SUCCEEDED, True,
        [{"kind": "path_absent", "target": "x", "passed": True, "owned": False}],
        []) is Verdict.INCONCLUSIVE
    assert verdict_for(
        TaskState.SUCCEEDED, True,
        [{"kind": "file_exists", "target": "x", "passed": True, "owned": True}],
        []) is Verdict.VERIFIED
