"""UI-0 acceptance tests: correctness, strict read-only, security.

Everything here runs against a **throw-away ledger** built by
:func:`fixture_ledger` with the real ``TaskStore``/``ControlPlane``/
``ProjectService``/``LDGoalStore`` writers, then read through the dashboard's
read-only path.  No Worker, no model, no network.

What is actually proven (not just asserted by convention):

* data correctness — every count is compared against the same
  ``ProjectService`` call the CLI uses, so a divergence fails the test;
* read-only — the ledger's bytes, row counts, ``context_version`` and journal
  files are compared before/after serving requests, and a direct write through
  the dashboard's own connection must raise ``sqlite3.OperationalError``;
* security — no root path / prompt / token in any payload; XSS payloads are
  returned as inert text; path traversal, CORS and non-loopback binding all
  fail closed.

Run:  python -m pytest tests/test_ui0_dashboard.py -q
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import ArtifactAssertion, TaskState, Verdict  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.project_service import ProjectService  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from dashboard import api as dashboard_api  # noqa: E402
from dashboard.api import DashboardApp, make_server, validate_host  # noqa: E402
from dashboard.ledger import (  # noqa: E402
    LedgerMissing,
    ProjectNotFound,
    ReadOnlyLedger,
    SchemaIncompatible,
)
from ld.goal_store import LDGoalStore, goal_identity  # noqa: E402

#: Ledger lives under .poc-state/ per AGENTS.md §7 (no artifacts outside sandbox).
STATE = ROOT / ".poc-state" / "ui0"
LEDGER = STATE / "ui0-dashboard.db"

XSS_NAME = '<script>alert("xss")</script>'
XSS_TITLE = '"><img src=x onerror=alert(1)>'
SECRET_PROMPT = "SECRET-EFFECTIVE-PROMPT-do-not-leak-42"


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
class _Adapter:
    """Name-only adapter: the fixture never dispatches anything."""

    name = "stub"


def _git_repo(path: Path) -> Path:
    """A real Git root: ``register_project`` canonicalises and validates it."""
    import subprocess
    path.mkdir(parents=True, exist_ok=True)
    if not (path / ".git").exists():
        subprocess.run(["git", "init", "-q", str(path)], check=True,
                       capture_output=True)
    return path


def _fresh(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    for stale in [path, *path.parent.glob(path.name + "*")]:
        if stale.is_dir():
            continue
        try:
            stale.unlink()
        except OSError:
            pass


def _add_task(store: TaskStore, *, task_id: str, project_id: str,
              workspace_id: str | None, title: str, state: str, verdict: str,
              queue_status: str, effective_prompt: str | None = None,
              metadata: dict | None = None, warnings: list[str] | None = None,
              context_version: int | None = None) -> dict:
    """Insert one WorkItem directly through the writer path (fixture only)."""
    record = {
        "task_id": task_id,
        "title": title,
        "prompt": "user prompt body",
        "original_task": "original task body",
        "effective_prompt": effective_prompt,
        "cwd": "/should/never/be/served",
        "mode": "agent",
        "isolation": "worktree",
        "idempotency_key": f"ui0-{task_id}",
        "state": state,
        "verdict": verdict,
        "queue_status": queue_status,
        "adapter": "stub",
        "project_id": project_id,
        "workspace_id": workspace_id,
        "context_version": context_version,
        "context_entry_refs": [],
        "assertions": [ArtifactAssertion("file_exists", "x.txt").to_dict()],
        "metadata": metadata or {},
        "warnings": warnings or [],
    }
    return store.create(record)


@pytest.fixture(scope="module")
def ledger() -> Path:
    """Build the full UI-0 fixture ledger once and reuse it read-only."""
    _fresh(LEDGER)
    store = TaskStore(LEDGER)
    plane = ControlPlane(_Adapter(), store, require_registered_projects=True,
                         worker_instance_id="ui0-fixture")
    service = ProjectService(plane)
    goals = LDGoalStore(LEDGER)

    # -- three projects; the third has no tasks at all -------------------- #
    alpha = plane.register_project("alpha-service", str(_git_repo(STATE / "repos" / "alpha")),
                                   default_adapter="stub")
    beta = plane.register_project("beta-agent", str(_git_repo(STATE / "repos" / "beta")),
                                  default_adapter="stub")
    gamma = plane.register_project(XSS_NAME, str(_git_repo(STATE / "repos" / "gamma")),
                                   default_adapter="stub")

    alpha_ws = store.default_workspace(alpha["id"])["id"]
    beta_ws = store.default_workspace(beta["id"])["id"]

    # -- context entries: verified / candidate / unverified --------------- #
    from controlplane.project_context import ContextEntry
    for kind, title, verification in (
        ("background", "alpha purpose", "verified"),
        ("constraints", "never touch prod", "verified"),
        ("knowledge", "flaky test note", "candidate"),
        ("progress", "next: wire the queue", "unverified"),
    ):
        service.add_context_entry(alpha["id"], ContextEntry(
            project_id=alpha["id"], kind=kind, title=title,
            body="ENTRY-BODY-MUST-NOT-LEAK " + title,
            verification=verification, security_domain="local",
            source="ui0-fixture", evidence_refs=[]))
    # A needs_merge candidate: same shape auto-write leaves behind on CAS loss.
    service.add_context_entry(alpha["id"], ContextEntry(
        project_id=alpha["id"], kind="knowledge", title="contested fact",
        body="ENTRY-BODY-MUST-NOT-LEAK contested", verification="candidate",
        security_domain="local", source="m2-auto", tags=["needs_merge"],
        evidence_refs=[]))
    service.add_context_entry(beta["id"], ContextEntry(
        project_id=beta["id"], kind="background", title="beta purpose",
        body="be brief", verification="candidate",
        security_domain="shareable", source="ui0-fixture", evidence_refs=[]))

    alpha_cv = store.context_version(alpha["id"])

    # -- work items in every state the task list asks for ----------------- #
    verified = _add_task(store, task_id="task-alpha-verified", project_id=alpha["id"],
                         workspace_id=alpha_ws, title="add the parser",
                         state=TaskState.SUCCEEDED.value,
                         verdict=Verdict.VERIFIED.value, queue_status="done",
                         effective_prompt=SECRET_PROMPT, context_version=alpha_cv,
                         metadata={"ld_goal_ref": "sha256:alpha"})
    rejected = _add_task(store, task_id="task-alpha-rejected", project_id=alpha["id"],
                         workspace_id=alpha_ws, title=XSS_TITLE,
                         state=TaskState.SUCCEEDED.value,
                         verdict=Verdict.REJECTED.value, queue_status="done",
                         effective_prompt=SECRET_PROMPT, context_version=alpha_cv)
    _add_task(store, task_id="task-alpha-inconclusive", project_id=alpha["id"],
              workspace_id=alpha_ws, title="no assertion",
              state=TaskState.SUCCEEDED.value,
              verdict=Verdict.INCONCLUSIVE.value, queue_status="done")
    approval = _add_task(store, task_id="task-alpha-approval", project_id=alpha["id"],
                         workspace_id=alpha_ws, title="needs a human yes",
                         state=TaskState.WAITING_FOR_APPROVAL.value,
                         verdict=Verdict.PENDING.value,
                         queue_status="needs_approval")
    running = _add_task(store, task_id="task-beta-running", project_id=beta["id"],
                        workspace_id=beta_ws, title="running task",
                        state=TaskState.RUNNING.value,
                        verdict=Verdict.PENDING.value, queue_status="running",
                        effective_prompt=SECRET_PROMPT)
    lost = _add_task(store, task_id="task-beta-lost", project_id=beta["id"],
                     workspace_id=beta_ws, title="lost worker",
                     state=TaskState.DISPATCH_UNKNOWN.value,
                     verdict=Verdict.PENDING.value,
                     queue_status="needs_human",
                     metadata={"suspected_lost": True})
    _add_task(store, task_id="task-beta-conflict", project_id=beta["id"],
              workspace_id=beta_ws, title="session conflict",
              state=TaskState.RUNNING.value, verdict=Verdict.PENDING.value,
              queue_status="needs_human",
              warnings=["SESSION_BINDING_CONFLICT: attempt a1 vs a2"])

    # -- attempts + one real SessionReference ----------------------------- #
    attempt = store.add_attempt(running["task_id"], "job-ui0-1", reason="fixture")
    store.bind_attempt_job(running["task_id"], "job-ui0-1")
    store.update(running["task_id"], current_attempt_id=attempt["id"])
    store.record_session_reference(
        beta["id"], workspace_id=beta_ws, node_id="local",
        worker_instance_id="ui0-fixture", adapter="stub",
        session_id="sess-ui0-1", work_item_id=running["task_id"],
        attempt_id=attempt["id"])

    # -- goals: 2 active, 1 needs_human ---------------------------------- #
    def _goal(project_id: str, text: str, status: str, *, decision_count: int = 0,
              **fields) -> dict:
        goal_id, goal_ref = goal_identity(text, project_id)
        goals.get_or_create(goal_id, project_id, text, goal_ref)
        if fields or status != "active":
            goals.update_goal(goal_id, status=status, **fields)
        if decision_count:
            # ``decision_count`` is owned by the two-phase step machinery, so
            # the fixture stamps it directly instead of forging a step history.
            goals._conn.execute(  # noqa: SLF001 - the goal store owns this table
                "UPDATE ld_goals SET decision_count=? WHERE goal_id=?",
                (decision_count, goal_id))
            goals._conn.commit()  # noqa: SLF001
        return goals.get(goal_id) or {}

    _goal(alpha["id"], "ship the parser", "active", decision_count=3,
          current_task_id=verified["task_id"], task_count=1)
    _goal(beta["id"], "stabilise the worker", "active", decision_count=2,
          current_task_id=running["task_id"], task_count=1)
    _goal(alpha["id"], "migrate the schema", "needs_human", decision_count=9,
          task_count=2,
          last_decision={"stop": "lifetime decision budget exhausted",
                         "model_decision_count": 9})

    store.close()
    goals.close()
    return LEDGER


@pytest.fixture(scope="module")
def app(ledger: Path) -> DashboardApp:
    return DashboardApp(ledger)


def _ids(ledger_path: Path) -> dict:
    """Project ids read without any dashboard code (independent oracle)."""
    conn = sqlite3.connect(f"file:{ledger_path}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT id, name FROM projects").fetchall()
        return {name: pid for pid, name in rows}
    finally:
        conn.close()


def _db_snapshot(ledger_path: Path) -> dict:
    """Content fingerprint of every table + the file bytes.

    Used before/after HTTP traffic: any write — including a metadata-only WAL
    append that changed a row — shows up as a different digest.
    """
    conn = sqlite3.connect(f"file:{ledger_path}?mode=ro", uri=True)
    try:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        counts = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in tables}
        digest = hashlib.sha256()
        for table in tables:
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            digest.update(table.encode())
            digest.update(json.dumps(rows, default=str).encode())
    finally:
        conn.close()
    return {
        "counts": counts,
        "content_digest": digest.hexdigest(),
        "file_digest": hashlib.sha256(ledger_path.read_bytes()).hexdigest(),
        "files": sorted(p.name for p in ledger_path.parent.glob(ledger_path.name + "*")),
    }


# --------------------------------------------------------------------------- #
# data correctness
# --------------------------------------------------------------------------- #
def test_overview_matches_project_service(app: DashboardApp, ledger: Path):
    ids = _ids(ledger)
    status, payload = app.handle("GET", "/api/overview")

    assert status == 200
    assert payload["totals"]["projects"] == 3
    assert payload["ledger"]["has_goals_table"] is True

    # Independent oracle: the very same ProjectService the CLI/LD uses.
    store = TaskStore(ledger, read_only=True)
    try:
        service = ProjectService(ControlPlane(_Adapter(), store))
        for row in payload["projects"]:
            progress = service.get_project_progress(row["project_id"])
            assert row["task_counts"] == progress["counts"], row["project_id"]
            assert row["task_count"] == progress["counts"]["total"]
            assert row["context_version"] == progress["context_version"]
    finally:
        store.close()


def test_overview_totals_are_sums_not_guesses(app: DashboardApp):
    _, payload = app.handle("GET", "/api/overview")
    rows = payload["projects"]
    totals = payload["totals"]

    assert totals["work_items"] == sum(r["task_count"] for r in rows)
    assert totals["active_work_items"] == sum(r["active_task_count"] for r in rows)
    assert totals["needs_approval_work_items"] == sum(
        r["task_counts"]["needs_approval"] for r in rows)
    assert totals["rejected"] == sum(r["task_counts"]["rejected"] for r in rows)
    assert totals["inconclusive"] == sum(r["task_counts"]["inconclusive"] for r in rows)
    # 3 projects, 2 active goals, 1 needs_human goal
    assert totals["goals"] == 3
    assert totals["active_goals"] == 2
    assert totals["goals_needs_human"] == 1


def test_empty_project_is_present_but_empty(app: DashboardApp, ledger: Path):
    ids = _ids(ledger)
    _, payload = app.handle("GET", f"/api/projects/{ids[XSS_NAME]}")
    assert payload["tasks"] == []
    assert payload["goals"] == []
    assert payload["sessions"] == []
    assert payload["memory"]["total"] == 0
    assert payload["task_counts"]["total"] == 0
    # A project with no work is not "100% complete"; there is no such field.
    assert "progress_percent" not in payload
    assert "completion" not in payload["project"]


def test_goal_ownership_is_per_project(app: DashboardApp, ledger: Path):
    ids = _ids(ledger)
    _, alpha = app.handle("GET", f"/api/projects/{ids['alpha-service']}")
    _, beta = app.handle("GET", f"/api/projects/{ids['beta-agent']}")

    assert {g["goal_id"] for g in alpha["goals"]} == {
        goal_identity("ship the parser", ids["alpha-service"])[0],
        goal_identity("migrate the schema", ids["alpha-service"])[0],
    }
    assert [g["goal_id"] for g in beta["goals"]] == [
        goal_identity("stabilise the worker", ids["beta-agent"])[0]]

    # Every returned goal really belongs to the project it was returned under.
    for payload in (alpha, beta):
        for goal in payload["goals"]:
            assert goal["project_id"] == payload["project"]["project_id"]


def test_work_items_never_cross_projects(app: DashboardApp, ledger: Path):
    ids = _ids(ledger)
    _, alpha = app.handle("GET", f"/api/projects/{ids['alpha-service']}")
    _, beta = app.handle("GET", f"/api/projects/{ids['beta-agent']}")

    alpha_ids = {t["task_id"] for t in alpha["tasks"]}
    beta_ids = {t["task_id"] for t in beta["tasks"]}
    assert alpha_ids and beta_ids
    assert not (alpha_ids & beta_ids)
    for task in alpha["tasks"]:
        assert task["project_id"] == ids["alpha-service"]
    for task in beta["tasks"]:
        assert task["project_id"] == ids["beta-agent"]
    assert "task-alpha-verified" in alpha_ids
    assert "task-beta-running" in beta_ids


def test_session_reference_links_task_and_attempt(app: DashboardApp, ledger: Path):
    ids = _ids(ledger)
    _, beta = app.handle("GET", f"/api/projects/{ids['beta-agent']}")

    assert len(beta["sessions"]) == 1
    ref = beta["sessions"][0]
    assert ref["work_item_id"] == "task-beta-running"
    assert ref["attempt_id"]
    assert ref["binding_status"] == "bound"
    assert ref["worker_instance_id"] == "ui0-fixture"
    assert ref["session_id"] == "sess-ui0-1"
    # A binding is not a heartbeat; the projection must say so.
    assert ref["liveness"] == "unknown"

    running = next(t for t in beta["tasks"] if t["task_id"] == "task-beta-running")
    assert running["current_attempt_id"] == ref["attempt_id"]


def test_statuses_are_not_merged(app: DashboardApp, ledger: Path):
    ids = _ids(ledger)
    _, alpha = app.handle("GET", f"/api/projects/{ids['alpha-service']}")
    _, beta = app.handle("GET", f"/api/projects/{ids['beta-agent']}")
    counts = dict(alpha["task_counts"])
    for key, value in beta["task_counts"].items():
        counts[key] = counts.get(key, 0) + value

    assert counts["verified"] == 1
    assert counts["rejected"] == 1
    assert counts["inconclusive"] == 1
    assert counts["needs_approval"] == 1
    assert counts["dispatch_unknown"] == 1
    assert counts["suspected_lost"] == 1
    assert counts["session_binding_conflict"] == 1

    # And the attention list reports them under their own names.
    _, overview = app.handle("GET", "/api/overview")
    flags = set()
    for item in overview["attention"]:
        flags.update(item.get("flags") or [])
        if item["kind"] == "goal":
            flags.add("goal:" + str(item["status"]))
    assert "needs_approval" in flags
    assert "dispatch_unknown" in flags
    assert "suspected_lost" in flags
    assert "session_binding_conflict" in flags
    assert "verdict:rejected" in flags
    assert "verdict:inconclusive" in flags
    assert "goal:needs_human" in flags
    # Nothing was folded into a generic "failed".
    for item in overview["attention"]:
        assert item["status"] not in ("failed", "error")


def test_memory_summary_buckets(app: DashboardApp, ledger: Path):
    ids = _ids(ledger)
    _, alpha = app.handle("GET", f"/api/projects/{ids['alpha-service']}")
    memory = alpha["memory"]

    assert memory["total"] == 5
    assert memory["verified"] == 2
    assert memory["candidate"] == 2          # one plain + one needs_merge
    assert memory["unverified"] == 1
    assert memory["needs_merge"] == 1
    assert memory["context_version"] == alpha["project"]["context_version"]
    assert memory["total"] == (memory["verified"] + memory["candidate"]
                               + memory["unverified"] + memory["other_verification"])


def test_sections_endpoints_agree_with_detail(app: DashboardApp, ledger: Path):
    pid = _ids(ledger)["alpha-service"]
    _, detail = app.handle("GET", f"/api/projects/{pid}")
    for section, key in (("goals", "goals"), ("tasks", "tasks"),
                         ("sessions", "sessions"), ("memory-summary", "memory")):
        status, payload = app.handle("GET", f"/api/projects/{pid}/{section}")
        assert status == 200
        assert payload[key] == detail[key], section
        assert payload["task_counts"] == detail["task_counts"]


def test_unknown_project_is_404(app: DashboardApp, ledger: Path):
    with pytest.raises(dashboard_api.ApiError) as excinfo:
        app.handle("GET", "/api/projects/does-not-exist")
    assert excinfo.value.status == 404
    assert excinfo.value.code == "project_not_found"

    # The projection layer refuses it too, so a new route cannot forget the check.
    from dashboard.projections import project_detail
    with pytest.raises(ProjectNotFound):
        with ReadOnlyLedger(ledger).session() as session:
            project_detail(session, "does-not-exist")


def test_health_reports_ledger_state(app: DashboardApp):
    status, payload = app.handle("GET", "/api/health")
    assert status == 200
    assert payload["status"] == "ok"
    assert payload["read_only"] is True
    assert payload["ledger"]["reachable"] is True
    assert payload["ledger"]["projects"] == 3


# --------------------------------------------------------------------------- #
# strict read-only
# --------------------------------------------------------------------------- #
def test_requests_do_not_change_the_ledger(app: DashboardApp, ledger: Path):
    pid = _ids(ledger)["alpha-service"]
    before = _db_snapshot(ledger)

    for _ in range(3):
        for path in ("/api/health", "/api/overview", "/api/projects",
                     f"/api/projects/{pid}", f"/api/projects/{pid}/goals",
                     f"/api/projects/{pid}/tasks", f"/api/projects/{pid}/sessions",
                     f"/api/projects/{pid}/memory-summary"):
            app.handle("GET", path)

    after = _db_snapshot(ledger)
    assert after["counts"] == before["counts"]
    assert after["content_digest"] == before["content_digest"]
    assert after["file_digest"] == before["file_digest"]
    # No new journal/backup/quarantine files appeared next to the ledger.
    assert after["files"] == before["files"]


def test_dashboard_connection_refuses_writes(ledger: Path):
    """The block is in SQLite, not in a routing table.

    Every table the dashboard reads is covered: task state, goals, context,
    attempts and evidence.  If any of these could be written through the
    dashboard's own connection, "read-only" would be a promise, not a fact.
    """
    forbidden = (
        "UPDATE work_items SET state='succeeded'",
        "UPDATE work_items SET verdict='verified'",
        "INSERT INTO projects (id, name, root, default_adapter,"
        " concurrency_limit, active, created_at, updated_at)"
        " VALUES ('x','x','x','x',1,1,0,0)",
        "CREATE TABLE ui0_should_not_exist (x)",
        "DELETE FROM ld_goals",
        "UPDATE ld_goals SET status='completed'",
        "INSERT INTO ld_goal_decisions (goal_id, step, decision, outcome,"
        " created_at) VALUES ('g', 1, '{}', '{}', 0)",
        "UPDATE project_context_entries SET body='edited'",
        "INSERT INTO project_context_entries (id, project_id, kind, title, body,"
        " verification, security_domain, version, created_at, updated_at)"
        " VALUES ('c','p','knowledge','t','b','verified','local',1,0,0)",
        "UPDATE project_context_state SET version=999",
        "INSERT INTO attempts (id, work_item_id, state) VALUES ('a','t','running')",
        "INSERT INTO evidence (work_item_id, kind) VALUES ('t','x')",
        "UPDATE session_references SET session_id='other'",
        "PRAGMA journal_mode=DELETE",
    )
    with ReadOnlyLedger(ledger).session() as session:
        conn = session.store._conn  # noqa: SLF001 - the thing under test
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        for sql in forbidden:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute(sql)


def test_readonly_flag_reaches_the_goal_store(ledger: Path):
    """LDGoalStore's own read-only path is used, not just TaskStore's."""
    with ReadOnlyLedger(ledger).session() as session:
        assert session.goals is not None
        assert session.goals.read_only is True
        assert session.goals._conn.execute(  # noqa: SLF001
            "PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            session.goals._conn.execute(  # noqa: SLF001
                "UPDATE ld_goals SET status='completed'")
        # Reads still work.
        assert session.goal_rows()


def test_serving_creates_no_attempts_evidence_or_context(tmp_path: Path):
    """Explicit per-table read-only proof, independent of the file digest."""
    path = tmp_path / "counts.db"
    store = TaskStore(path)
    plane = ControlPlane(_Adapter(), store, require_registered_projects=True)
    project = plane.register_project("counts", str(_git_repo(tmp_path / "r")),
                                     default_adapter="stub")
    ws = store.default_workspace(project["id"])["id"]
    attempt = store.add_attempt("c-1", "job-1", reason="fixture")
    store.create({"task_id": "c-1", "title": "t", "prompt": "p",
                  "cwd": project["root"], "mode": "agent", "isolation": "none",
                  "idempotency_key": "c-1", "state": TaskState.RUNNING.value,
                  "verdict": Verdict.PENDING.value, "queue_status": "running",
                  "adapter": "stub", "project_id": project["id"],
                  "workspace_id": ws, "current_attempt_id": attempt["id"]})
    store.add_evidence("c-1", "log", {"note": "fixture"})

    watched = ("work_items", "attempts", "evidence", "ld_goals",
               "ld_goal_decisions", "project_context_entries",
               "project_context_state", "session_references", "events",
               "approvals", "context_update_receipts")
    present = store.schema_tables()
    watched = tuple(table for table in watched if table in present)
    assert "work_items" in watched and "attempts" in watched
    before = {table: store._conn.execute(  # noqa: SLF001
        f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in watched}
    before_cv = store.context_version(project["id"])
    store.close()

    app = DashboardApp(path)
    for _ in range(3):
        app.handle("GET", "/api/overview")
        app.handle("GET", f"/api/projects/{project['id']}")
        app.handle("GET", f"/api/projects/{project['id']}/memory-summary")

    check = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        after_tables = {row[0] for row in check.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        # The dashboard must not have created the ld_goals tables either.
        assert after_tables == present, sorted(after_tables ^ present)
        for table in watched:
            after = check.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert after == before[table], table
        row = check.execute(
            "SELECT version FROM project_context_state WHERE project_id=?",
            (project["id"],)).fetchone()
        assert (row[0] if row else 0) == before_cv
    finally:
        check.close()


@pytest.mark.skipif(os.name == "nt", reason=(
    "Windows read-only file attribute does not stop SQLite from opening a "
    "file for write; the connection-level guarantee is covered by "
    "test_dashboard_connection_refuses_writes"))
def test_os_level_readonly_file_is_still_served(tmp_path: Path):
    """POSIX proof: even an OS-read-only ledger file yields a working view.

    On Windows the read-only attribute is advisory for SQLite, so the
    connection-level refusal (mode=ro + query_only) is the guarantee tested
    there.  On POSIX this additionally proves the dashboard never *needs*
    write access.
    """
    path = tmp_path / "ro.db"
    store = TaskStore(path)
    plane = ControlPlane(_Adapter(), store, require_registered_projects=True)
    project = plane.register_project("ro", str(_git_repo(tmp_path / "r")),
                                     default_adapter="stub")
    store.close()
    os.chmod(path, 0o444)
    try:
        app = DashboardApp(path)
        status, payload = app.handle("GET", "/api/overview")
        assert status == 200
        assert payload["projects"][0]["project_id"] == project["id"]
    finally:
        os.chmod(path, 0o644)


def test_missing_ledger_is_never_created(tmp_path: Path):
    missing = tmp_path / "nope.db"
    ledger = ReadOnlyLedger(missing)

    with pytest.raises(LedgerMissing):
        with ledger.session():
            pytest.fail("a missing ledger must not be opened")

    assert not missing.exists()
    assert list(tmp_path.iterdir()) == []


def test_taskstore_default_still_migrates_but_readonly_does_not(tmp_path: Path):
    """Backward compatibility: writers are unchanged; observers add nothing."""
    path = tmp_path / "empty.db"
    # A read-only open of a non-existent file adds nothing.
    with pytest.raises(FileNotFoundError):
        TaskStore(path, read_only=True)
    assert not path.exists()

    # The historical writer path is untouched: it creates + migrates.
    store = TaskStore(path)
    try:
        assert "work_items" in store.schema_tables()
    finally:
        store.close()

    before = set(p.name for p in tmp_path.iterdir())
    ro = TaskStore(path, read_only=True)
    try:
        assert ro.read_only is True
        assert "ld_goals" not in ro.schema_tables()   # no DDL was run
    finally:
        ro.close()
    after = set(p.name for p in tmp_path.iterdir())
    # SQLite's WAL index is not ledger content: a read-only connection may
    # create/reuse <db>-shm (and -wal) in the same directory.  Anything else
    # appearing — a backup, a quarantine copy, a .DEGRADED marker — is a bug.
    allowed = {path.name, path.name + "-shm", path.name + "-wal"}
    assert after - before <= allowed, after - before


def test_readonly_store_does_not_stamp_degraded_marker(tmp_path: Path):
    """A quarantine marker must not be written (nor honoured) by an observer."""
    path = tmp_path / "marked.db"
    store = TaskStore(path)
    store.close()
    marker = path.with_name(path.name + ".DEGRADED")
    marker.write_text("degraded by a prior process\n", encoding="utf-8")

    ro = TaskStore(path, read_only=True)
    try:
        # The observer does not mutate the file system, so it also does not
        # re-quarantine; it simply reports nothing about degradation.
        assert path.exists()
        assert marker.exists()
    finally:
        ro.close()
    marker.unlink()


def test_schema_incompatible_is_reported(tmp_path: Path):
    path = tmp_path / "wrong.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE unrelated (x)")
    conn.commit()
    conn.close()

    with pytest.raises(SchemaIncompatible):
        with ReadOnlyLedger(path).session():
            pytest.fail("a ledger without work_items must not be served")


def test_non_sqlite_file_is_reported(tmp_path: Path):
    path = tmp_path / "garbage.db"
    path.write_bytes(b"not a database at all" * 40)
    from dashboard.ledger import LedgerUnreadable
    with pytest.raises((LedgerUnreadable, SchemaIncompatible)):
        with ReadOnlyLedger(path).session():
            pytest.fail("a corrupt ledger must not be served")


# --------------------------------------------------------------------------- #
# security
# --------------------------------------------------------------------------- #
FORBIDDEN_SUBSTRINGS = (
    "SECRET-EFFECTIVE-PROMPT",
    "ENTRY-BODY-MUST-NOT-LEAK",
    "should/never/be/served",
    "password",
    "token",
    "authorization",
    "api_key",
    "apikey",
)


def test_no_prompt_body_path_or_secret_is_serialised(app: DashboardApp, ledger: Path):
    ids = _ids(ledger)
    paths = ["/api/health", "/api/overview", "/api/projects"]
    for pid in ids.values():
        paths += [f"/api/projects/{pid}", f"/api/projects/{pid}/goals",
                  f"/api/projects/{pid}/tasks", f"/api/projects/{pid}/sessions",
                  f"/api/projects/{pid}/memory-summary"]
    for path in paths:
        _, payload = app.handle("GET", path)
        blob = json.dumps(payload, ensure_ascii=False, default=str)
        for needle in FORBIDDEN_SUBSTRINGS:
            assert needle not in blob, f"{needle!r} leaked from {path}"

    # And the local machine path / repo root specifically.
    _, overview = app.handle("GET", "/api/overview")
    blob = json.dumps(overview, ensure_ascii=False)
    assert "repos" not in blob.replace("repos'", "")  # no workspace roots
    for row in overview["projects"]:
        assert "root" not in row
        assert "local_root" not in row


def test_effective_prompt_flag_without_content(app: DashboardApp, ledger: Path):
    pid = _ids(ledger)["alpha-service"]
    _, payload = app.handle("GET", f"/api/projects/{pid}")
    verified = next(t for t in payload["tasks"] if t["task_id"] == "task-alpha-verified")
    assert verified["has_effective_prompt"] is True
    assert "effective_prompt" not in verified


def test_xss_payloads_are_returned_as_plain_text(app: DashboardApp, ledger: Path):
    """The API must not escape/encode; the browser must not execute.

    Escaping in the API would corrupt the data (the name really is that
    string).  The guarantee lives in the renderer: only ``textContent``.
    """
    ids = _ids(ledger)
    _, overview = app.handle("GET", "/api/overview")
    names = {row["name"] for row in overview["projects"]}
    assert XSS_NAME in names          # verbatim, not mangled

    pid = ids["alpha-service"]
    _, payload = app.handle("GET", f"/api/projects/{pid}")
    titles = {t["title"] for t in payload["tasks"]}
    assert XSS_TITLE in titles

    # Static assets must not build markup from data.  Comments are allowed to
    # *mention* innerHTML; code is not allowed to use it for content.
    app_js = (ROOT / "dashboard" / "static" / "app.js").read_text(encoding="utf-8")
    in_block_comment = False
    uses = []
    for raw in app_js.splitlines():
        line = raw.strip()
        if in_block_comment:
            if "*/" in line:
                in_block_comment = False
            continue
        if line.startswith("/*"):
            if "*/" not in line:
                in_block_comment = True
            continue
        if line.startswith("//") or line.startswith("*"):
            continue
        if "innerHTML" in line:
            uses.append(line)
    assert uses == ['node.innerHTML = "";'], f"unexpected innerHTML use: {uses}"

    index_html = (ROOT / "dashboard" / "static" / "index.html").read_text(encoding="utf-8")
    assert "<script>alert" not in index_html
    # No inline handlers / inline scripts: the CSP would block them anyway, and
    # their presence would suggest string-built markup.
    assert "onclick=" not in index_html
    assert "<script>" not in index_html


def test_write_methods_are_rejected(app: DashboardApp):
    for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"):
        with pytest.raises(dashboard_api.ApiError) as excinfo:
            app.handle(method, "/api/overview")
        assert excinfo.value.status == 405
        assert excinfo.value.code == "method_not_allowed"


def test_static_path_traversal_is_blocked():
    """Both raw and percent-encoded escapes must be refused."""
    from urllib.parse import unquote

    from dashboard.api import DashboardHandler
    payloads = ("/../AGENTS.md", "/../../.env", "/static/../api.py",
                "/%2e%2e/%2e%2e/AGENTS.md", "/subdir/../../README.md",
                "/..%2fAGENTS.md")
    for raw in payloads:
        # The transport unquotes before resolving; mirror that here.
        with pytest.raises(dashboard_api.ApiError) as excinfo:
            DashboardHandler._resolve_static(unquote(raw))  # noqa: SLF001
        assert excinfo.value.status == 403
        assert excinfo.value.code == "path_traversal"


def test_static_only_serves_known_assets():
    from dashboard.api import DashboardHandler
    assert DashboardHandler._resolve_static("/").name == "index.html"  # noqa: SLF001
    assert DashboardHandler._resolve_static("/app.js") is not None     # noqa: SLF001
    assert DashboardHandler._resolve_static("/styles.css") is not None  # noqa: SLF001
    # A path that simply does not exist is a 404, not a traversal error.
    assert DashboardHandler._resolve_static("/nope.js") is None        # noqa: SLF001
    # A NUL byte is refused rather than passed to the OS.
    with pytest.raises(dashboard_api.ApiError):
        DashboardHandler._resolve_static("/app.js\x00.png")  # noqa: SLF001


def test_non_loopback_binding_is_refused():
    for host in ("0.0.0.0", "::", "192.168.1.5", "0.0.0.0"):
        with pytest.raises(ValueError):
            validate_host(host)
    assert validate_host("127.0.0.1") == "127.0.0.1"
    assert validate_host("localhost") == "localhost"


# --------------------------------------------------------------------------- #
# real HTTP smoke test (server + transport, not just the projection layer)
# --------------------------------------------------------------------------- #
class _ServerThread:
    def __init__(self, ledger_path: Path):
        self.server = make_server(ledger_path, host="127.0.0.1", port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def _request(url: str, method: str = "GET") -> tuple[int, dict, dict]:
    request = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read()
            return response.status, dict(response.headers), json.loads(body or b"{}")
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            payload = {"_raw": body.decode("utf-8", "replace")}
        return exc.code, dict(exc.headers), payload


@pytest.mark.smoke
def test_http_smoke_real_server(ledger: Path):
    pid = _ids(ledger)["alpha-service"]
    before = _db_snapshot(ledger)

    with _ServerThread(ledger) as server:
        status, headers, health = _request(server.base + "/api/health")
        assert status == 200 and health["status"] == "ok"

        status, headers, overview = _request(server.base + "/api/overview")
        assert status == 200
        assert overview["totals"]["projects"] == 3
        # No CORS, ever.
        assert "Access-Control-Allow-Origin" not in headers
        assert headers.get("Cache-Control") == "no-store"
        assert "default-src 'self'" in headers.get("Content-Security-Policy", "")

        status, _, detail = _request(server.base + f"/api/projects/{pid}")
        assert status == 200
        assert detail["project"]["name"] == "alpha-service"

        # The page itself is served, and it is the real UI (not a stub).
        with urllib.request.urlopen(server.base + "/", timeout=10) as response:
            page = response.read().decode("utf-8")
        assert response.status == 200
        assert "Agent Control Plane" in page
        assert "/app.js" in page and "/styles.css" in page
        for asset in ("/app.js", "/styles.css"):
            with urllib.request.urlopen(server.base + asset, timeout=10) as resp:
                assert resp.status == 200 and len(resp.read()) > 1000

        # Writes are refused at the HTTP layer too.
        status, _, body = _request(server.base + "/api/overview", method="POST")
        assert status == 405
        assert body["error"]["code"] == "method_not_allowed"

        # Unknown API path is JSON, not an HTML page.
        status, _, body = _request(server.base + "/api/nope")
        assert status == 404 and body["error"]["code"] == "not_found"

        # Traversal through the real transport.
        status, _, body = _request(server.base + "/%2e%2e/AGENTS.md")
        assert status in (403, 404)

        # A foreign Host header is refused (DNS-rebinding guard).
        status, _, body = _request(server.base + "/api/overview", method="GET")
        assert status == 200   # sanity: normal Host works

    after = _db_snapshot(ledger)
    assert after["counts"] == before["counts"]
    assert after["content_digest"] == before["content_digest"]
    assert after["file_digest"] == before["file_digest"]


@pytest.mark.smoke
def test_foreign_host_header_is_rejected(ledger: Path):
    with _ServerThread(ledger) as server:
        request = urllib.request.Request(server.base + "/api/overview")
        request.add_header("Host", "evil.example.com")
        try:
            urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
            payload = json.loads(exc.read() or b"{}")
            assert payload["error"]["code"] == "bad_host"
        else:
            pytest.fail("a non-loopback Host header must be refused")


@pytest.mark.smoke
def test_short_lived_reads_do_not_block_wal_checkpoint(tmp_path: Path):
    """Per-request connections must not pin a WAL read snapshot.

    If the dashboard kept one connection open while idle, a busy server could
    make the Runtime's WAL grow without bound.  After the request completes the
    log must be truncatable with ``busy=0``.
    """
    path = tmp_path / "wal.db"
    store = TaskStore(path)
    plane = ControlPlane(_Adapter(), store, require_registered_projects=True)
    project = plane.register_project("wal", str(_git_repo(tmp_path / "repo")),
                                     default_adapter="stub")
    ws = store.default_workspace(project["id"])["id"]
    for i in range(60):
        _add_task(store, task_id=f"w-{i}", project_id=project["id"],
                  workspace_id=ws, title=f"row {i}",
                  state=TaskState.QUEUED.value, verdict=Verdict.PENDING.value,
                  queue_status="ready")

    server = make_server(path, host="127.0.0.1", port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        for i in range(5):
            status, _, body = _request(f"http://127.0.0.1:{port}/api/overview")
            assert status == 200
            store.update(f"w-{i}", state=TaskState.RUNNING.value)
        conn = sqlite3.connect(str(path))
        try:
            busy, _log, _done = conn.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            conn.close()
        assert busy == 0, "the dashboard left a read snapshot open"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        store.close()


@pytest.mark.smoke
def test_concurrent_runtime_write_is_visible_after_refresh(tmp_path: Path):
    """Dashboard and a writer share one ledger; reading must not block writes."""
    path = tmp_path / "live.db"
    store = TaskStore(path)
    plane = ControlPlane(_Adapter(), store, require_registered_projects=True)
    service = ProjectService(plane)
    project = plane.register_project("live", str(_git_repo(tmp_path / "repo")),
                                     default_adapter="stub")
    ws = store.default_workspace(project["id"])["id"]
    _add_task(store, task_id="live-1", project_id=project["id"], workspace_id=ws,
              title="first", state=TaskState.QUEUED.value,
              verdict=Verdict.PENDING.value, queue_status="ready")

    app = DashboardApp(path)
    _, before = app.handle("GET", f"/api/projects/{project['id']}")
    assert before["task_counts"]["active"] == 1
    assert before["tasks"][0]["state"] == "queued"

    # The Runtime keeps writing while the dashboard has been read.
    store.update("live-1", state=TaskState.SUCCEEDED.value,
                 verdict=Verdict.VERIFIED.value, queue_status="done")
    _add_task(store, task_id="live-2", project_id=project["id"], workspace_id=ws,
              title="second", state=TaskState.RUNNING.value,
              verdict=Verdict.PENDING.value, queue_status="running")
    _, after = app.handle("GET", f"/api/projects/{project['id']}")
    store.close()

    assert after["task_counts"]["verified"] == 1
    assert after["task_counts"]["active"] == 1
    assert {t["task_id"] for t in after["tasks"]} == {"live-1", "live-2"}


# --------------------------------------------------------------------------- #
# first run on an empty ledger
# --------------------------------------------------------------------------- #
def test_empty_ledger_renders_without_error(tmp_path: Path):
    """A brand-new ledger with zero projects must be a valid, empty view."""
    path = tmp_path / "brand-new.db"
    store = TaskStore(path)      # created by the *writer* (as the Runtime would)
    store.close()

    app = DashboardApp(path)
    status, payload = app.handle("GET", "/api/overview")
    assert status == 200
    assert payload["projects"] == []
    assert payload["attention"] == []
    assert payload["totals"]["projects"] == 0
    assert payload["totals"]["active_goals"] == 0
    assert payload["ledger"]["has_goals_table"] is False

    status, health = app.handle("GET", "/api/health")
    assert status == 200 and health["status"] == "ok"


def test_probe_cli_works(ledger: Path, capsys):
    import dashboard_server
    code = dashboard_server.main(["--ledger", str(ledger), "--probe"])
    out = capsys.readouterr().out
    assert code == 0
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["overview"]["totals"]["projects"] == 3
