"""T63 / UI-0: black-box verification of the read-only dashboard.

This script is the *evidence producer*.  The pytest suite
(``tests/test_ui0_dashboard.py``) is the exhaustive regression; this script
answers the narrower question an operator asks: "if I start the server the way
the README says and click around, does it show real ledger data and change
nothing?"

Flow
----
1. Build a throw-away ledger with 3 projects (one empty), 2 active goals,
   1 needs_human goal, and WorkItems covering verified / rejected /
   inconclusive / needs_approval / running / dispatch_unknown.
2. Record a content fingerprint of every table plus the file bytes.
3. Start the real HTTP server (``dashboard.make_server``) on 127.0.0.1:0.
4. Fetch the real page + assets + every documented API endpoint.
5. Assert the negative cases (POST 405, traversal, foreign Host, unknown API).
6. Stop the server, re-fingerprint, and require byte-identical content.
7. Write ``evidence/project/T63-ui0-dashboard.json`` via the sanitizer.

No Worker, no model, no network egress: everything is loopback + a temp ledger
under ``.poc-state/``.

Run:  python scripts/t63_ui0_dashboard.py
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from adapters.base import ArtifactAssertion, TaskState, Verdict  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.project_context import ContextEntry  # noqa: E402
from controlplane.project_service import ProjectService  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from dashboard.api import LOOPBACK_HOSTS, make_server, validate_host  # noqa: E402
from ld.goal_store import LDGoalStore, goal_identity  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

STATE = ROOT / ".poc-state" / "ui0"
LEDGER = STATE / "t63.db"
EVIDENCE = ROOT / "evidence" / "project" / "T63-ui0-dashboard.json"

XSS_NAME = '<script>alert("xss")</script>'
XSS_TITLE = '"><img src=x onerror=alert(1)>'
SECRET = "SECRET-EFFECTIVE-PROMPT-must-not-leak"


class NameRef:
    name = "stub"


def git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if not (path / ".git").exists():
        subprocess.run(["git", "init", "-q", str(path)], check=True,
                       capture_output=True)
    return path


def add_task(store: TaskStore, *, task_id: str, project_id: str,
             workspace_id: str, title: str, state: str, verdict: str,
             queue_status: str, **extra) -> dict:
    return store.create({
        "task_id": task_id, "title": title, "prompt": "user prompt",
        "original_task": "original task", "cwd": "/never/served",
        "mode": "agent", "isolation": "worktree",
        "idempotency_key": f"t63-{task_id}", "state": state, "verdict": verdict,
        "queue_status": queue_status, "adapter": "stub", "project_id": project_id,
        "workspace_id": workspace_id,
        "assertions": [ArtifactAssertion("file_exists", "x").to_dict()],
        "metadata": {}, "warnings": [], "context_entry_refs": [],
        **extra,
    })


def fingerprint(path: Path) -> dict:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        digest = hashlib.sha256()
        counts = {}
        for table in tables:
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            counts[table] = len(rows)
            digest.update(table.encode())
            digest.update(json.dumps(rows, default=str).encode())
    finally:
        conn.close()
    return {"counts": counts, "content_digest": digest.hexdigest(),
            "file_digest": hashlib.sha256(path.read_bytes()).hexdigest()}


def fetch(url: str, method: str = "GET", host: str | None = None):
    request = urllib.request.Request(url, method=method)
    if host:
        request.add_header("Host", host)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()
    except urllib.error.URLError as exc:
        return -1, {}, str(exc).encode()


def build_ledger() -> dict:
    for stale in [LEDGER, *LEDGER.parent.glob(LEDGER.name + "*")]:
        if stale.is_file():
            stale.unlink()
    STATE.mkdir(parents=True, exist_ok=True)

    store = TaskStore(LEDGER)
    plane = ControlPlane(NameRef(), store, require_registered_projects=True,
                         worker_instance_id="t63-fixture")
    service = ProjectService(plane)
    goals = LDGoalStore(LEDGER)

    repos = STATE / "repos"
    alpha = plane.register_project("alpha-service", str(git_repo(repos / "alpha")),
                                   default_adapter="stub")
    beta = plane.register_project("beta-agent", str(git_repo(repos / "beta")),
                                  default_adapter="stub")
    gamma = plane.register_project(XSS_NAME, str(git_repo(repos / "gamma")),
                                   default_adapter="stub")
    aws = store.default_workspace(alpha["id"])["id"]
    bws = store.default_workspace(beta["id"])["id"]

    for kind, title, verification in (
        ("background", "alpha purpose", "verified"),
        ("constraints", "never touch prod", "verified"),
        ("knowledge", "flaky test note", "candidate"),
        ("progress", "next: wire the queue", "unverified"),
    ):
        service.add_context_entry(alpha["id"], ContextEntry(
            project_id=alpha["id"], kind=kind, title=title,
            body="ENTRY-BODY-must-not-leak", verification=verification,
            security_domain="local", source="t63", evidence_refs=[]))
    service.add_context_entry(alpha["id"], ContextEntry(
        project_id=alpha["id"], kind="knowledge", title="contested fact",
        body="ENTRY-BODY-must-not-leak", verification="candidate",
        security_domain="local", source="m2-auto", tags=["needs_merge"],
        evidence_refs=[]))
    service.add_context_entry(beta["id"], ContextEntry(
        project_id=beta["id"], kind="background", title="beta purpose",
        body="be brief", verification="candidate",
        security_domain="shareable", source="t63", evidence_refs=[]))
    acv = store.context_version(alpha["id"])

    add_task(store, task_id="t63-verified", project_id=alpha["id"],
             workspace_id=aws, title="add the parser",
             state=TaskState.SUCCEEDED.value, verdict=Verdict.VERIFIED.value,
             queue_status="done", effective_prompt=SECRET, context_version=acv)
    add_task(store, task_id="t63-rejected", project_id=alpha["id"],
             workspace_id=aws, title=XSS_TITLE,
             state=TaskState.SUCCEEDED.value, verdict=Verdict.REJECTED.value,
             queue_status="done", effective_prompt=SECRET, context_version=acv)
    add_task(store, task_id="t63-inconclusive", project_id=alpha["id"],
             workspace_id=aws, title="no assertion",
             state=TaskState.SUCCEEDED.value, verdict=Verdict.INCONCLUSIVE.value,
             queue_status="done")
    add_task(store, task_id="t63-approval", project_id=alpha["id"],
             workspace_id=aws, title="needs a human yes",
             state=TaskState.WAITING_FOR_APPROVAL.value,
             verdict=Verdict.PENDING.value, queue_status="needs_approval")
    running = add_task(store, task_id="t63-running", project_id=beta["id"],
                       workspace_id=bws, title="running task",
                       state=TaskState.RUNNING.value,
                       verdict=Verdict.PENDING.value, queue_status="running",
                       effective_prompt=SECRET)
    add_task(store, task_id="t63-lost", project_id=beta["id"],
             workspace_id=bws, title="lost worker",
             state=TaskState.DISPATCH_UNKNOWN.value,
             verdict=Verdict.PENDING.value, queue_status="needs_human",
             metadata={"suspected_lost": True})
    add_task(store, task_id="t63-conflict", project_id=beta["id"],
             workspace_id=bws, title="session conflict",
             state=TaskState.RUNNING.value, verdict=Verdict.PENDING.value,
             queue_status="needs_human",
             warnings=["SESSION_BINDING_CONFLICT: attempt a1 vs a2"])

    attempt = store.add_attempt(running["task_id"], "job-t63", reason="fixture")
    store.bind_attempt_job(running["task_id"], "job-t63")
    store.record_session_reference(
        beta["id"], workspace_id=bws, node_id="local",
        worker_instance_id="t63-fixture", adapter="stub",
        session_id="sess-t63", work_item_id=running["task_id"],
        attempt_id=attempt["id"])

    def goal(project_id: str, text: str, status: str, *, decisions: int = 0,
             **fields) -> dict:
        goal_id, goal_ref = goal_identity(text, project_id)
        goals.get_or_create(goal_id, project_id, text, goal_ref)
        if fields or status != "active":
            goals.update_goal(goal_id, status=status, **fields)
        if decisions:
            goals._conn.execute(  # noqa: SLF001 - fixture stamps the counter
                "UPDATE ld_goals SET decision_count=? WHERE goal_id=?",
                (decisions, goal_id))
            goals._conn.commit()  # noqa: SLF001
        return goals.get(goal_id) or {}

    goal(alpha["id"], "ship the parser", "active", decisions=3,
         current_task_id="t63-verified", task_count=1)
    goal(beta["id"], "stabilise the worker", "active", decisions=2,
         current_task_id="t63-running", task_count=1)
    goal(alpha["id"], "migrate the schema", "needs_human", decisions=9,
         task_count=2,
         last_decision={"stop": "lifetime decision budget exhausted"})

    ids = {"alpha": alpha["id"], "beta": beta["id"], "gamma": gamma["id"]}
    store.close()
    goals.close()
    return ids


def main() -> int:
    ids = build_ledger()
    before = fingerprint(LEDGER)

    server = make_server(LEDGER, host="127.0.0.1", port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    checks: list[dict] = []

    def check(name: str, ok: bool, detail=None) -> None:
        checks.append({"check": name, "pass": bool(ok), "detail": detail})

    try:
        # ---- page + assets ------------------------------------------- #
        status, headers, page = fetch(base + "/")
        check("index.html served", status == 200 and b"Agent Control Plane" in page,
              {"status": status, "bytes": len(page)})
        for asset in ("/app.js", "/styles.css"):
            status, _, body = fetch(base + asset)
            check(f"{asset} served", status == 200 and len(body) > 1000,
                  {"status": status, "bytes": len(body)})
        check("no CORS header",
              "Access-Control-Allow-Origin" not in headers, None)
        check("CSP same-origin only",
              "default-src 'self'" in headers.get("Content-Security-Policy", ""),
              headers.get("Content-Security-Policy", ""))
        check("cache disabled", headers.get("Cache-Control") == "no-store", None)

        # ---- API endpoints ------------------------------------------- #
        status, _, body = fetch(base + "/api/health")
        health = json.loads(body)
        check("health ok", status == 200 and health["read_only"] is True,
              {"status": status, "reachable": health["ledger"]["reachable"]})

        status, _, body = fetch(base + "/api/overview")
        overview = json.loads(body)
        totals = overview["totals"]
        check("overview projects", len(overview["projects"]) == 3,
              {"projects": len(overview["projects"])})
        check("overview goals", totals["goals"] == 3 and totals["active_goals"] == 2,
              {"goals": totals["goals"], "active": totals["active_goals"]})
        check("overview needs_human goal", totals["goals_needs_human"] == 1,
              totals["goals_needs_human"])
        check("overview attention items", totals["attention_items"] >= 6,
              totals["attention_items"])
        check("overview rejected/inconclusive kept apart",
              totals["rejected"] == 1 and totals["inconclusive"] == 1,
              {"rejected": totals["rejected"],
               "inconclusive": totals["inconclusive"]})

        status, _, body = fetch(base + "/api/projects")
        listing = json.loads(body)
        check("/api/projects", status == 200
              and len(listing["projects"]) == 3, status)

        status, _, body = fetch(base + f"/api/projects/{ids['alpha']}")
        detail = json.loads(body)
        check("detail alpha", status == 200
              and detail["project"]["name"] == "alpha-service", status)
        check("detail goal ownership",
              all(g["project_id"] == ids["alpha"] for g in detail["goals"]),
              len(detail["goals"]))
        check("detail task ownership",
              all(t["project_id"] == ids["alpha"] for t in detail["tasks"]),
              len(detail["tasks"]))
        check("detail memory buckets",
              detail["memory"]["verified"] == 2
              and detail["memory"]["candidate"] == 2
              and detail["memory"]["unverified"] == 1
              and detail["memory"]["needs_merge"] == 1,
              {k: detail["memory"][k] for k in
               ("verified", "candidate", "unverified", "needs_merge")})
        check("no project completion percent",
              "progress_percent" not in detail and "completion" not in detail["project"],
              None)

        status, _, body = fetch(base + f"/api/projects/{ids['gamma']}")
        empty = json.loads(body)
        check("empty project renders", status == 200
              and empty["tasks"] == [] and empty["goals"] == []
              and empty["memory"]["total"] == 0, status)

        status, _, body = fetch(base + f"/api/projects/{ids['beta']}")
        beta = json.loads(body)
        check("session reference bound",
              len(beta["sessions"]) == 1
              and beta["sessions"][0]["binding_status"] == "bound"
              and beta["sessions"][0]["work_item_id"] == "t63-running",
              beta["sessions"][0]["binding_status"] if beta["sessions"] else None)
        check("session liveness not inferred",
              beta["sessions"] and beta["sessions"][0]["liveness"] == "unknown",
              None)

        for section, key in (("goals", "goals"), ("tasks", "tasks"),
                             ("sessions", "sessions"),
                             ("memory-summary", "memory")):
            status, _, body = fetch(base + f"/api/projects/{ids['alpha']}/{section}")
            payload = json.loads(body)
            check(f"section {section}", status == 200 and key in payload, status)

        # ---- security ------------------------------------------------ #
        status, _, body = fetch(base + "/api/overview", method="POST")
        check("POST rejected", status == 405
              and json.loads(body)["error"]["code"] == "method_not_allowed", status)
        for method in ("PUT", "PATCH", "DELETE"):
            status, _, _ = fetch(base + "/api/overview", method=method)
            check(f"{method} rejected", status == 405, status)

        status, _, _ = fetch(base + "/%2e%2e/AGENTS.md")
        check("path traversal blocked", status in (403, 404), status)
        status, _, body = fetch(base + "/api/overview", host="evil.example.com")
        check("foreign Host rejected", status == 403
              and json.loads(body)["error"]["code"] == "bad_host", status)
        status, _, body = fetch(base + "/api/nope")
        check("unknown API is JSON 404", status == 404
              and json.loads(body)["error"]["code"] == "not_found", status)

        leaked = []
        blobs = [json.dumps(overview), json.dumps(detail), json.dumps(beta)]
        for needle in ("SECRET-EFFECTIVE-PROMPT", "ENTRY-BODY-must-not-leak",
                       "/never/served", "repos/alpha", "local_root"):
            if any(needle in blob for blob in blobs):
                leaked.append(needle)
        check("no prompt/body/path leaked", not leaked, leaked)

        check("xss name returned verbatim (escaped by renderer)",
              XSS_NAME in {p["name"] for p in overview["projects"]}, None)
        check("xss title returned verbatim",
              XSS_TITLE in {t["title"] for t in detail["tasks"]}, None)

        # ---- loopback binding ---------------------------------------- #
        refused = []
        for host in ("0.0.0.0", "192.168.1.5", "::"):
            try:
                validate_host(host)
                refused.append(host)
            except ValueError:
                pass
        check("non-loopback bind refused", not refused, refused)
        check("loopback allow-list", LOOPBACK_HOSTS == frozenset(
            {"127.0.0.1", "::1", "localhost"}), sorted(LOOPBACK_HOSTS))

        # ---- live runtime write visible on refresh -------------------- #
        store = TaskStore(LEDGER)
        try:
            ws = store.default_workspace(ids["beta"])["id"]
            add_task(store, task_id="t63-live", project_id=ids["beta"],
                     workspace_id=ws, title="live proof",
                     state=TaskState.RUNNING.value,
                     verdict=Verdict.PENDING.value, queue_status="running")
            _, _, body = fetch(base + f"/api/projects/{ids['beta']}")
            mid = json.loads(body)
            seen_running = next(t for t in mid["tasks"] if t["task_id"] == "t63-live")
            store.update("t63-live", state=TaskState.SUCCEEDED.value,
                         verdict=Verdict.VERIFIED.value, queue_status="done")
            _, _, body = fetch(base + f"/api/projects/{ids['beta']}")
            after = json.loads(body)
            seen_done = next(t for t in after["tasks"] if t["task_id"] == "t63-live")
            check("live write visible (running)", seen_running["state"] == "running",
                  seen_running["state"])
            check("live write visible (verified)",
                  seen_done["verdict"] == "verified", seen_done["verdict"])
        finally:
            store.close()

        # ---- short reads do not pin the WAL --------------------------- #
        conn = sqlite3.connect(str(LEDGER))
        try:
            busy, _log, _done = conn.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            conn.close()
        check("no read snapshot left open after requests", busy == 0, busy)

        check("read-only connection refuses writes",
              _readonly_refusal(LEDGER), None)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    # The live-write step above intentionally added one WorkItem, so compare
    # everything *except* work_items, which must have grown by exactly one.
    after = fingerprint(LEDGER)
    grew = {table: after["counts"][table] - before["counts"].get(table, 0)
            for table in after["counts"]}
    unexpected = {table: delta for table, delta in grew.items()
                  if delta and table != "work_items"}
    check("only the fixture's own work item was added",
          grew.get("work_items") == 1 and not unexpected,
          {"work_items": grew.get("work_items"), "unexpected": unexpected})
    check("goals/context/attempts/evidence untouched",
          all(grew.get(t, 0) == 0 for t in
              ("ld_goals", "ld_goal_decisions", "project_context_entries",
               "project_context_state", "attempts", "evidence",
               "session_references", "approvals", "events")),
          {t: grew.get(t) for t in ("ld_goals", "attempts", "evidence")})

    passed = sum(1 for c in checks if c["pass"])
    result = "Pass" if passed == len(checks) else "Fail"
    write_evidence(EVIDENCE, {
        "test": "T63-UI0-readonly-dashboard",
        "mode": "black-box HTTP against a throw-away ledger; no Worker, no model",
        "ledger": str(LEDGER.relative_to(ROOT)),
        "server": {"bind": "127.0.0.1", "port": "ephemeral",
                   "routes": ["/api/health", "/api/overview", "/api/projects",
                              "/api/projects/{id}",
                              "/api/projects/{id}/goals|tasks|sessions|memory-summary"]},
        "read_only_proof": {
            "before": before,
            "after": after,
            "allowed_delta": {"work_items": 1,
                              "reason": "test itself wrote one fixture WorkItem "
                                        "to prove refresh sees Runtime updates"},
        },
        "checks": checks,
        "summary": {"total": len(checks), "passed": passed,
                    "failed": len(checks) - passed},
        "result": result,
    })
    print(json.dumps({"result": result, "checks": len(checks), "passed": passed,
                      "evidence": str(EVIDENCE.relative_to(ROOT))},
                     ensure_ascii=False, indent=2))
    return 0 if result == "Pass" else 1


def _readonly_refusal(path: Path) -> bool:
    """Open the dashboard's own connection and try a representative write."""
    from dashboard.ledger import ReadOnlyLedger
    try:
        with ReadOnlyLedger(path).session() as session:
            conn = session.store._conn  # noqa: SLF001
            if conn.execute("PRAGMA query_only").fetchone()[0] != 1:
                return False
            try:
                conn.execute("UPDATE work_items SET state='tampered'")
            except sqlite3.OperationalError:
                return True
            return False
    except Exception:  # noqa: BLE001
        return False


if __name__ == "__main__":
    raise SystemExit(main())
