"""T76: isolated closed-loop acceptance for console project registration.

Runs entirely against a throwaway Ledger and temporary Git repositories on a
free loopback port.  It never opens the production Ledger for writing and never
calls the production Dashboard / Worker / Runtime processes.

Closed loop, in order:

1. register a valid Git root through the real authenticated HTTP route;
2. repeat the same registration and prove it writes nothing (idempotency);
3. reload the page and re-open read-only sessions (refresh persistence);
4. stop the HTTP server, start a *new* server over the same Ledger, and prove
   both the rows and the new route survive (restart persistence);
5. ask the real LD (deterministic router, no model) about the new project and
   prove it is listed, aggregated and routable by name;
6. publish one low-risk Goal through the real console route and prove no
   WorkItem is created by the Dashboard itself;
7. wake the real LDGoalRunner to forward the Goal, then prove the resulting
   WorkItem is bound to the NEW project's default workspace, that an actual
   dispatch lands in that repository, and that re-pointing the same item at the
   OTHER project's workspace is refused before any adapter call;
8. compare the production Ledger's business-table hashes before and after.

Evidence: ``evidence/project/T76-isolated-registration.json``.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

from adapters.base import DispatchResult  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.project_service import ProjectService  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from dashboard.api import make_server  # noqa: E402
from dashboard.chat import ChatBridge  # noqa: E402
from ld.coordinator import LDCoordinator  # noqa: E402
from ld.goal_runner import LDGoalRunner  # noqa: E402
from ld.goal_store import LDGoalStore  # noqa: E402
from ld.tools import LDTools  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

PROD_LEDGER = Path(r"C:\ACP\acp-manager-state\ledger.db")
PROD_REPO = Path(r"C:\ACP\acp-manager")
EVIDENCE = ROOT / "evidence" / "project" / "T76-isolated-registration.json"
WORK = ROOT / ".poc-state" / "t76-isolated"

TOKEN = "t76-isolated-operator-token-32ch"
NEW_NAME = "T76-Isolated"
EXISTING_NAME = "T76-Existing"
OBJECTIVE = "创建 hello.txt 文件"
ROUTE = "/api/chat/project-register"

BUSINESS_TABLES = (
    "projects", "workspaces", "work_items", "attempts", "ld_goals",
    "ld_goal_decisions", "approvals", "evidence", "session_references",
    "project_context_entries", "project_context_revisions",
    "context_update_receipts",
)
INFO_TABLES = ("events", "ld_goal_runner", "ld_conversations", "ld_messages")


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def git_repo(path: Path) -> Path:
    """A real, isolated Git working tree with one initial commit."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.name=T76",
                    "-c", "user.email=t76@localhost", "commit", "-q",
                    "--allow-empty", "-m", "isolated init"],
                   check=True, capture_output=True)
    return path.resolve()


def connect_ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect("file:" + path.as_posix() + "?mode=ro", uri=True)
    con.execute("PRAGMA query_only=1")
    con.row_factory = sqlite3.Row
    return con


def table_hash(path: Path, table: str) -> dict:
    con = connect_ro(path)
    try:
        rows = [tuple(str(v) for v in r)
                for r in con.execute("SELECT * FROM %s ORDER BY 1" % table)]
    except sqlite3.Error as exc:
        rows = ["ERR:%s" % exc]
    finally:
        con.close()
    blob = json.dumps(rows, ensure_ascii=False, sort_keys=True)
    return {"rows": len(rows),
            "sha256": hashlib.sha256(blob.encode("utf-8")).hexdigest()}


def ledger_snapshot(path: Path) -> dict:
    return {t: table_hash(path, t) for t in BUSINESS_TABLES + INFO_TABLES}


def jsonable(value):
    """SQLite rows must survive json.dumps inside write_evidence()."""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def rows(path: Path, sql: str, params=()) -> list[dict]:
    con = connect_ro(path)
    try:
        return [jsonable(dict(r)) for r in con.execute(sql, params)]
    finally:
        con.close()


def norm(path) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def port_pid(port: int):
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-NetTCPConnection -State Listen -LocalPort %d -ErrorAction "
         "SilentlyContinue | Select-Object -First 1).OwningProcess" % port],
        capture_output=True, text=True)
    text = out.stdout.strip()
    return int(text) if text.isdigit() else None


def prod_head() -> str:
    out = subprocess.run(["git", "-C", str(PROD_REPO), "rev-parse", "HEAD"],
                         capture_output=True, text=True)
    return out.stdout.strip()


def prod_state() -> dict:
    prod_adapters = [r["adapter"] for r in rows(
        PROD_LEDGER, "SELECT DISTINCT adapter FROM work_items")]
    prod_project_adapters = [r["default_adapter"] for r in rows(
        PROD_LEDGER, "SELECT DISTINCT default_adapter FROM projects")]
    return {
        "business_hashes": {t: table_hash(PROD_LEDGER, t)
                            for t in BUSINESS_TABLES},
        "info_hashes": {t: table_hash(PROD_LEDGER, t) for t in INFO_TABLES},
        "manager_head": prod_head(),
        "worker_pid": port_pid(8789),
        "dashboard_pid": port_pid(8766),
        "ledger_bytes": PROD_LEDGER.stat().st_size,
        "workitem_adapters": sorted(str(a) for a in prod_adapters),
        "project_default_adapters": sorted(
            str(a) for a in prod_project_adapters),
    }


#: ``project-register`` fixes ``default_adapter="codebuddy"`` server-side.
#: The LD submission service rejects a Goal whose project adapter differs from
#: the service adapter ("submission service adapter ... does not match project
#: adapter ..."), so an isolated harness MUST use the same name or the Goal is
#: parked in needs_human instead of producing a WorkItem.  Production's
#: run_controlplane.py uses CodeBuddyAdapter, whose name is also "codebuddy".
ISOLATED_ADAPTER = "codebuddy"


class NameOnlyAdapter:
    """No Worker methods: used only while the Goal is forwarded to a WorkItem."""
    name = ISOLATED_ADAPTER


class RecordingAdapter:
    """Records the exact cwd the control plane hands to a Worker, without
    starting any real Worker process."""

    name = ISOLATED_ADAPTER

    def __init__(self):
        self.requests: list = []

    def dispatch(self, request):
        self.requests.append({"task_id": request.task_id,
                              "cwd": request.cwd,
                              "mode": request.mode,
                              "isolation": request.isolation,
                              "permission_mode": (
                                  request.metadata or {}).get("permissionMode")})
        return DispatchResult(task_id=request.task_id,
                              worker_job_id="t76-recording-job",
                              accepted=True, raw={"isolated": True})


class SubmitOnceModel:
    """Scripted LD decision (the repository's T50 seam): submit one task."""

    def __init__(self, task: str):
        self.task = task
        self.calls = 0

    def decide(self, decision_input):
        self.calls += 1
        state = decision_input.get("goal_state") or {}
        if not state.get("all_goal_tasks"):
            return {"action": "submit_task",
                    "project_id": decision_input.get("requested_project_id"),
                    "reason": "forward the low-risk published objective",
                    "evidence_refs": [],
                    "parameters": {"task": self.task,
                                   "idempotency_key": "t76-low-risk",
                                   "assertions": []}}
        return {"action": "wait",
                "project_id": decision_input.get("requested_project_id"),
                "reason": "wait for the forwarded task", "evidence_refs": [],
                "parameters": {"task_id": (state.get("current_task") or {})
                               .get("task_id")}}


class Server:
    """A real Dashboard HTTP server over the isolated Ledger."""

    def __init__(self, ledger: Path):
        self.ledger = ledger
        bridge = ChatBridge(ledger, model_base_url="", model_name="")
        self.server = make_server(ledger, host="127.0.0.1", port=0,
                                  enable_chat=True, chat_token=TOKEN,
                                  chat_bridge=bridge)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return "http://127.0.0.1:%d" % self.port

    def request(self, path: str, *, payload=None, token: str | None = TOKEN,
                origin: str | None = "same"):
        data = None if payload is None else json.dumps(
            payload, ensure_ascii=False).encode("utf-8")
        headers = {"Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        if origin == "same":
            headers["Origin"] = self.base
        elif origin is not None:
            headers["Origin"] = origin
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.base + path, data=data, headers=headers,
            method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def close(self):
        if getattr(self, "_closed", False):
            return
        self._closed = True
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


# --------------------------------------------------------------------------- #
# acceptance
# --------------------------------------------------------------------------- #
def main() -> int:
    checks: dict[str, bool] = {}
    detail: dict = {}
    prod_before = prod_state()

    root = WORK / uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    ledger = root / "isolated.db"
    existing_repo = git_repo(root / "repos" / "existing")
    new_repo = git_repo(root / "repos" / "isolated")
    detail["environment"] = {
        "isolated_ledger": str(ledger),
        "temporary_repos": [str(existing_repo), str(new_repo)],
        "production_ledger": str(PROD_LEDGER),
        "no_model": True,
        "no_real_worker": True,
    }
    checks["isolated_ledger_is_not_production"] = (
        norm(ledger) != norm(PROD_LEDGER)
        and not norm(ledger).startswith(norm(PROD_LEDGER.parent)))

    store = TaskStore(ledger)
    plane = ControlPlane(NameOnlyAdapter(), store,
                         require_registered_projects=True)
    existing = plane.register_project(EXISTING_NAME, str(existing_repo),
                                      default_adapter=ISOLATED_ADAPTER)
    existing_ws = store.get_default_workspace(existing["id"]) \
        if hasattr(store, "get_default_workspace") else None
    if existing_ws is None:
        existing_ws = rows(ledger, "SELECT * FROM workspaces WHERE project_id=?",
                           (existing["id"],))[0]
    store.close()
    detail["existing_project"] = {"project_id": existing["id"],
                                  "workspace_id": existing_ws["id"]}

    server = Server(ledger)
    new_id = None
    try:
        # -- 1. valid registration --------------------------------------- #
        before_rows = rows(ledger, "SELECT COUNT(*) AS n FROM projects")[0]["n"]
        status, body = server.request(
            ROUTE, payload={"root": str(new_repo), "name": NEW_NAME})
        project = (body.get("project") or {})
        new_id = project.get("project_id")
        detail["registration"] = {"http_status": status, "response": body}
        proj_rows = rows(ledger, "SELECT * FROM projects WHERE id=?", (new_id,))
        ws_rows = rows(ledger, "SELECT * FROM workspaces WHERE project_id=?",
                       (new_id,))
        detail["registration_sql"] = {"project": proj_rows,
                                      "workspace": ws_rows}
        checks["valid_registration_http_200"] = status == 200
        checks["isolated_adapter_matches_registered_project"] = (
            bool(proj_rows)
            and proj_rows[0]["default_adapter"] == ISOLATED_ADAPTER
            and NameOnlyAdapter.name == ISOLATED_ADAPTER
            and RecordingAdapter.name == ISOLATED_ADAPTER)
        checks["registration_created_project_and_workspace"] = (
            project.get("created") is True
            and len(proj_rows) == 1 and len(ws_rows) == 1
            and rows(ledger, "SELECT COUNT(*) AS n FROM projects")[0]["n"]
            == before_rows + 1)
        if proj_rows and ws_rows:
            policy = json.loads(proj_rows[0]["policy"] or "{}")
            ws = ws_rows[0]
            checks["registration_uses_fixed_server_side_policy"] = (
                proj_rows[0]["default_adapter"] == "codebuddy"
                and proj_rows[0]["concurrency_limit"] == 1
                and proj_rows[0]["active"] == 1
                and policy.get("mode") == "agent"
                and policy.get("isolation") == "worktree"
                and policy.get("permission_mode") == "acceptEdits")
            checks["workspace_bound_to_registered_root"] = (
                norm(ws["local_root"]) == norm(new_repo)
                and ws["project_id"] == new_id
                and ws["node_id"] == "local"
                and ws["is_default"] == 1 and ws["active"] == 1
                and norm(proj_rows[0]["root"]) == norm(new_repo))
        else:
            checks["registration_uses_fixed_server_side_policy"] = False
            checks["workspace_bound_to_registered_root"] = False

        # -- 2. idempotent re-registration -------------------------------- #
        snap = ledger_snapshot(ledger)
        status2, body2 = server.request(
            ROUTE, payload={"root": str(new_repo), "name": NEW_NAME})
        detail["reregistration"] = {"http_status": status2,
                                    "response": body2}
        checks["reregistration_is_idempotent"] = (
            status2 == 200
            and (body2.get("project") or {}).get("created") is False
            and (body2.get("project") or {}).get("project_id") == new_id
            and ledger_snapshot(ledger) == snap)

        # -- 3. refresh persistence --------------------------------------- #
        status3, targets = server.request("/api/chat/projects")
        status4, overview = server.request("/api/overview")
        fresh_store = TaskStore(ledger, read_only=True)
        fresh_names = [p["name"] for p in fresh_store.all_projects()]
        fresh_store.close()
        detail["refresh"] = {
            "chat_projects_status": status3,
            "overview_status": status4,
            "fresh_readonly_session_projects": fresh_names,
            "overview_project_ids": [p["project_id"]
                                     for p in overview.get("projects", [])],
        }
        checks["refresh_persists_in_new_sessions"] = (
            status3 == 200 and status4 == 200
            and NEW_NAME in fresh_names
            and new_id in detail["refresh"]["overview_project_ids"]
            and any(p["project_id"] == new_id
                    for p in targets.get("projects", [])))

        # Browser reload: the left rail is fed by the read-only projection.
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                try:
                    engine = p.chromium.launch(channel="msedge", headless=True)
                except Exception:
                    engine = p.chromium.launch(headless=True)
                try:
                    page = engine.new_page(
                        viewport={"width": 1400, "height": 900})
                    page.goto(server.base + "/", wait_until="load",
                              timeout=30000)
                    page.wait_for_selector(".nav-item", timeout=15000)
                    first = page.locator(".nav-item").all_inner_texts()
                    page.reload(wait_until="load")
                    page.wait_for_selector(".nav-item", timeout=15000)
                    second = page.locator(".nav-item").all_inner_texts()
                    detail["refresh"]["browser_nav_before"] = first
                    detail["refresh"]["browser_nav_after"] = second
                    checks["browser_reload_keeps_project"] = (
                        any(NEW_NAME in t for t in first)
                        and any(NEW_NAME in t for t in second))
                finally:
                    engine.close()
        except Exception as exc:  # noqa: BLE001 - report, do not hide
            detail["refresh"]["browser_error"] = "%s: %s" % (
                type(exc).__name__, exc)
            checks["browser_reload_keeps_project"] = False

        # -- 4. restart persistence --------------------------------------- #
        server.close()
        server = Server(ledger)
        status5, overview2 = server.request("/api/overview")
        status6, targets2 = server.request("/api/chat/projects")
        status7, body7 = server.request(
            ROUTE, payload={"root": str(new_repo), "name": NEW_NAME})
        detail["restart"] = {
            "overview_status": status5,
            "chat_projects_status": status6,
            "reregister_status": status7,
            "reregister_created": (body7.get("project") or {}).get("created"),
            "project_ids_after_restart": [
                p["project_id"] for p in overview2.get("projects", [])],
        }
        checks["restart_persists_rows_and_route"] = (
            status5 == 200 and status6 == 200 and status7 == 200
            and new_id in detail["restart"]["project_ids_after_restart"]
            and any(p["project_id"] == new_id
                    for p in targets2.get("projects", []))
            and (body7.get("project") or {}).get("created") is False)

        # -- 5. LD recognises the new project ------------------------------ #
        ld_turns = []
        for question in ("现在有哪些项目？",
                         "汇总所有项目目前的进度",
                         NEW_NAME + " 当前状态？"):
            code, turn = server.request(
                "/api/chat/messages",
                payload={"conversation_id": None, "project_id": None,
                         "message": question})
            ld_turns.append({
                "question": question, "http_status": code,
                "intent": (turn.get("intent") or {}).get("intent"),
                "intent_source": turn.get("intent_source"),
                "project_routing": turn.get("project_routing"),
                "portfolio_project_ids": [
                    p.get("project_id")
                    for p in (turn.get("portfolio") or {}).get("projects", [])],
                "answer": turn.get("answer") or "",
                "answer_head": (turn.get("answer") or "")[:200],
                "goal_ids": turn.get("goal_ids"),
            })
        detail["ld_recognition"] = ld_turns
        list_turn, portfolio_turn, named_turn = ld_turns
        checks["ld_lists_the_new_project"] = (
            list_turn["http_status"] == 200
            and list_turn["intent"] == "list_projects"
            and NEW_NAME in list_turn["answer"]
            and new_id in list_turn["answer"])
        checks["ld_aggregates_the_new_project"] = (
            portfolio_turn["http_status"] == 200
            and portfolio_turn["intent"] == "portfolio_status"
            and new_id in portfolio_turn["portfolio_project_ids"]
            and existing["id"] in portfolio_turn["portfolio_project_ids"])
        checks["ld_routes_by_new_project_name"] = (
            named_turn["http_status"] == 200
            and (named_turn["project_routing"] or {}).get("source") == "named"
            and (named_turn["project_routing"] or {}).get("project_id") == new_id
            and named_turn["intent"] != "portfolio_status")

        # -- 6. low-risk Goal ---------------------------------------------- #
        workitems_before = rows(
            ledger, "SELECT COUNT(*) AS n FROM work_items")[0]["n"]
        status8, published = server.request(
            "/api/chat/publish",
            payload={"project_id": new_id, "objective": OBJECTIVE})
        goal_id = published.get("goal_id")
        goal_rows = rows(ledger, "SELECT * FROM ld_goals WHERE goal_id=?",
                         (goal_id,))
        workitems_after = rows(
            ledger, "SELECT COUNT(*) AS n FROM work_items")[0]["n"]
        approvals = rows(ledger, "SELECT * FROM approvals")
        detail["low_risk_goal"] = {
            "http_status": status8, "published": published,
            "goal_rows": goal_rows,
            "workitems_before": workitems_before,
            "workitems_after": workitems_after,
            "approvals": approvals,
        }
        checks["low_risk_goal_created_for_new_project"] = (
            status8 == 200 and bool(goal_id) and len(goal_rows) == 1
            and goal_rows[0]["project_id"] == new_id
            and goal_rows[0]["status"] == "active"
            and not approvals)
        checks["dashboard_does_not_dispatch_workitems"] = (
            workitems_after == workitems_before)

        # -- 7. Goal -> WorkItem -> correct workspace ---------------------- #
        runner_store = TaskStore(ledger)
        runner_plane = ControlPlane(NameOnlyAdapter(), runner_store,
                                    require_registered_projects=True)
        runner_service = ProjectService(runner_plane)
        goals = LDGoalStore(ledger)
        coordinator = LDCoordinator(
            LDTools(runner_service, goal_store=goals),
            SubmitOnceModel(OBJECTIVE),
            model_authorized_security_domains=("local", "shareable"))
        runner = LDGoalRunner(
            coordinator, goals, max_decisions=2, max_tasks=1,
            base_backoff_s=0.1, max_backoff_s=0.2,
            lifetime_max_decisions=4, lifetime_max_tasks=2)
        sweep = jsonable(runner.advance_due())
        runner.close()
        goals.close()
        runner_store.close()
        detail["runner_sweep"] = sweep

        tasks = rows(ledger, "SELECT * FROM work_items")
        detail["runner_diagnostics"] = {
            "goal_rows_after_sweep": rows(
                ledger, "SELECT * FROM ld_goals"),
            "workitem_count": len(tasks),
            "events_tail": rows(
                ledger, "SELECT * FROM events ORDER BY rowid DESC LIMIT 12"),
        }
        detail["workitems"] = [
            {k: t.get(k) for k in
             ("id", "project_id", "workspace_id", "cwd", "queue_status",
              "state", "original_task", "policy", "adapter")}
            for t in tasks]
        checks["runner_created_exactly_one_workitem"] = len(tasks) == 1
        task = tasks[0] if tasks else {}
        new_ws_id = detail["registration_sql"]["workspace"][0]["id"] \
            if detail["registration_sql"]["workspace"] else None
        cwd_ok = bool(task) and norm(task["cwd"]) == norm(new_repo)
        checks["workitem_bound_to_new_project_workspace"] = (
            bool(task)
            and task["project_id"] == new_id
            and task["workspace_id"] == new_ws_id
            and cwd_ok
            and not Path(str(task["cwd"])).resolve().is_relative_to(
                existing_repo))
        task_policy = json.loads(task.get("policy") or "{}") if task else {}
        checks["workitem_is_low_risk_no_approval"] = (
            bool(task) and task["queue_status"] == "ready"
            and task_policy.get("mode") == "agent"
            and task_policy.get("isolation") == "worktree"
            and not approvals)

        if not task:
            detail["dispatch"] = {"skipped": "runner produced no WorkItem"}
            detail["cross_workspace_negative"] = {
                "skipped": "runner produced no WorkItem"}
            checks["dispatch_lands_in_the_new_repository"] = False
            checks["cross_workspace_dispatch_refused"] = False
        else:
            # Real dispatch path with a recording adapter: no Worker is
            # started, but every workspace/cwd check in ControlPlane runs.
            adapter = RecordingAdapter()
            dispatch_store = TaskStore(ledger)
            dispatch_plane = ControlPlane(adapter, dispatch_store,
                                          require_registered_projects=True)
            dispatch_plane.dispatch_next(owner="t76-acceptance",
                                         concurrency_limit=1)
            dispatch_store.close()
            detail["dispatch"] = {"adapter_calls": adapter.requests}
            checks["dispatch_lands_in_the_new_repository"] = (
                len(adapter.requests) == 1
                and norm(adapter.requests[0]["cwd"]) == norm(new_repo)
                and adapter.requests[0]["task_id"] == task.get("id"))

            # Negative control: point the same WorkItem at the OTHER project's
            # workspace.  The control plane must refuse before any adapter call.
            con = sqlite3.connect(str(ledger))
            try:
                con.execute(
                    "UPDATE work_items SET workspace_id=?, queue_status='ready', "
                    "dispatch_claim_owner=NULL, dispatch_claimed_at=NULL "
                    "WHERE id=?", (existing_ws["id"], task["id"]))
                con.commit()
            finally:
                con.close()
            wrong_adapter = RecordingAdapter()
            neg_store = TaskStore(ledger)
            neg_plane = ControlPlane(wrong_adapter, neg_store,
                                     require_registered_projects=True)
            neg_plane.dispatch_next(owner="t76-negative",
                                    concurrency_limit=1)
            neg_store.close()
            neg_events = rows(ledger, "SELECT * FROM events ORDER BY rowid")
            neg_payload = json.dumps(neg_events, ensure_ascii=False,
                                     default=str)
            after_neg = rows(ledger, "SELECT * FROM work_items WHERE id=?",
                             (task["id"],))
            detail["cross_workspace_negative"] = {
                "injected_workspace_id": existing_ws["id"],
                "adapter_calls": wrong_adapter.requests,
                "queue_status_after": after_neg[0]["queue_status"]
                if after_neg else None,
                "refusal_event_present":
                    "belongs to another project" in neg_payload,
            }
            checks["cross_workspace_dispatch_refused"] = (
                not wrong_adapter.requests
                and after_neg and after_neg[0]["queue_status"] == "needs_human"
                and "belongs to another project" in neg_payload)
    finally:
        server.close()

    # -- 8. production untouched ------------------------------------------ #
    prod_after = prod_state()
    prod_changed = [t for t in BUSINESS_TABLES
                    if prod_before["business_hashes"][t]
                    != prod_after["business_hashes"][t]]
    prod_info_changed = [t for t in INFO_TABLES
                         if prod_before["info_hashes"][t]
                         != prod_after["info_hashes"][t]]
    detail["production"] = {
        "before": prod_before, "after": prod_after,
        "changed_business_tables": prod_changed,
        "changed_info_tables": prod_info_changed,
    }
    checks["production_business_data_unchanged"] = not prod_changed
    checks["production_services_unchanged"] = (
        prod_before["dashboard_pid"] == prod_after["dashboard_pid"]
        and prod_before["worker_pid"] == prod_after["worker_pid"]
        and prod_before["manager_head"] == prod_after["manager_head"])
    # Prerequisite for ever opening real registration: project-register fixes
    # default_adapter="codebuddy", and the LD submission service refuses a Goal
    # whose project adapter differs from its own.  Production's runtime uses
    # CodeBuddyAdapter; confirm the name really is the same before advising.
    prod_adapters = set(prod_after["workitem_adapters"])
    prod_project_adapters = set(prod_after["project_default_adapters"])
    detail["adapter_prerequisite"] = {
        "registration_fixes_default_adapter": ISOLATED_ADAPTER,
        "production_workitem_adapters": sorted(prod_adapters),
        "production_project_default_adapters": sorted(prod_project_adapters),
        "isolated_finding": (
            "A submission service whose adapter name differs from the "
            "registered project's default_adapter parks the Goal in "
            "needs_human with ld_action_failed instead of creating a WorkItem"),
    }
    checks["production_adapter_matches_registration_policy"] = (
        prod_adapters == {ISOLATED_ADAPTER}
        and prod_project_adapters == {ISOLATED_ADAPTER})

    failed = sorted(k for k, v in checks.items() if not v)
    payload = {
        "task": "T76-isolated-registration-closed-loop",
        "schema_version": "t76.registration.v1",
        "result": "PASS" if not failed else "FAIL",
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "checks": checks, "failed": failed, "detail": detail,
        "conclusion": ("isolated closed loop passed; production registration "
                       "is still not exercised"
                       if not failed else
                       "isolated closed loop FAILED; do not open production "
                       "registration"),
    }
    write_evidence(EVIDENCE, payload)
    print(json.dumps({
        "result": payload["result"], "checks": len(checks), "failed": failed,
        "new_project_id": new_id,
        "registration": detail.get("registration", {}).get("http_status"),
        "reregistration": detail.get("reregistration", {}).get("http_status"),
        "ld_recognition": [
            {"q": t["question"], "intent": t["intent"],
             "source": t["intent_source"]}
            for t in detail.get("ld_recognition", [])],
        "goal": detail.get("low_risk_goal", {}).get("published", {}).get(
            "goal_id"),
        "workitems": len(detail.get("workitems", [])),
        "dispatch": detail.get("dispatch", {}).get("adapter_calls"),
        "cross_workspace_negative": detail.get("cross_workspace_negative"),
        "production_changed_business_tables": prod_changed,
        "evidence": str(EVIDENCE.relative_to(ROOT)),
    }, ensure_ascii=False, indent=2))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
