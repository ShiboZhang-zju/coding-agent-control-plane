"""Project registration from the authenticated console surface.

The browser may choose *which local Git repository* becomes a project, but
never how work in it is dispatched.  This suite pins the filesystem boundary,
the closed field set, the auth/origin gates and the "no work dispatched"
guarantee (SQL snapshot before/after).
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from dashboard.api import make_server  # noqa: E402
from dashboard.chat import ChatBridge  # noqa: E402

STATE = ROOT / ".poc-state" / "ui5-register"
TOKEN = "ui5-project-register-token-32ch"
ROUTE = "/api/chat/project-register"
BUSINESS_TABLES = ("projects", "workspaces", "work_items", "attempts",
                   "ld_goals", "ld_goal_decisions", "approvals")


class _Adapter:
    name = "stub"


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True,
                   capture_output=True)
    return path


class Harness:
    def __init__(self, *, enable_chat: bool = True):
        root = STATE / uuid.uuid4().hex
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.ledger = root / "ui5.db"
        self.existing_repo = _git_repo(root / "existing")
        store = TaskStore(self.ledger)
        plane = ControlPlane(_Adapter(), store, require_registered_projects=True)
        self.existing = plane.register_project(
            "ui5-existing", str(self.existing_repo), default_adapter="stub")
        store.close()

        bridge = ChatBridge(self.ledger) if enable_chat else None
        self.server = make_server(
            self.ledger, host="127.0.0.1", port=0,
            enable_chat=enable_chat,
            chat_token=TOKEN if enable_chat else None,
            chat_bridge=bridge)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.02},
                                       daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, path: str, *, payload=None, token: str | None = TOKEN,
                origin: str | None = "same") -> tuple[int, dict]:
        data = None if payload is None else json.dumps(
            payload, ensure_ascii=False).encode("utf-8")
        headers = {"Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        if origin == "same":
            headers["Origin"] = self.base
        elif origin is not None:
            headers["Origin"] = origin
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base + path, data=data, headers=headers,
            method="POST" if payload is not None else "GET")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def snapshot(self) -> dict:
        con = sqlite3.connect("file:" + str(self.ledger).replace("\\", "/")
                              + "?mode=ro", uri=True)
        con.execute("PRAGMA query_only=1")
        try:
            return {table: con.execute(
                "SELECT * FROM %s ORDER BY 1" % table).fetchall()
                for table in BUSINESS_TABLES}
        finally:
            con.close()


@pytest.fixture()
def harness():
    item = Harness()
    try:
        yield item
    finally:
        item.close()


def _register(harness: Harness, repo: Path, name: str = "ui5-new", **kwargs):
    return harness.request(ROUTE, payload={"root": str(repo), "name": name},
                           **kwargs)


# --------------------------------------------------------------------------- #
# 1. happy path + ledger truth
# --------------------------------------------------------------------------- #
def test_register_a_git_root(harness: Harness):
    repo = _git_repo(harness.root / "new-project")
    before = harness.snapshot()
    status, body = _register(harness, repo)
    assert status == 200, body
    project = body["project"]
    assert project["name"] == "ui5-new"
    assert project["created"] is True
    assert project["active"] is True
    import os
    assert os.path.normcase(str(Path(project["root"]))) == \
           os.path.normcase(str(repo.resolve()))

    con = sqlite3.connect("file:" + str(harness.ledger).replace("\\", "/")
                          + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT * FROM projects WHERE id=?",
                          (project["project_id"],)).fetchone()
        assert row is not None
        # Fixed server-side defaults: the browser never chooses dispatch policy.
        assert row["default_adapter"] == "codebuddy"
        assert row["concurrency_limit"] == 1
        policy = json.loads(row["policy"])
        assert policy["mode"] == "agent"
        assert policy["isolation"] == "worktree"
        assert policy["permission_mode"] == "acceptEdits"
        workspaces = con.execute(
            "SELECT * FROM workspaces WHERE project_id=?",
            (project["project_id"],)).fetchall()
        assert len(workspaces) == 1
        assert workspaces[0]["is_default"] == 1
        assert workspaces[0]["node_id"] == "local"
    finally:
        con.close()

    after = harness.snapshot()
    assert len(after["projects"]) == len(before["projects"]) + 1
    assert len(after["workspaces"]) == len(before["workspaces"]) + 1
    for table in ("work_items", "attempts", "ld_goals",
                  "ld_goal_decisions", "approvals"):
        assert after[table] == before[table], table


def test_registered_project_appears_in_targets_and_overview(harness: Harness):
    repo = _git_repo(harness.root / "visible")
    status, body = _register(harness, repo, "ui5-visible")
    assert status == 200, body
    pid = body["project"]["project_id"]

    status, targets = harness.request("/api/chat/projects")
    assert status == 200
    assert any(row["project_id"] == pid for row in targets["projects"])

    status, overview = harness.request("/api/overview")
    assert status == 200
    assert any(row["project_id"] == pid for row in overview["projects"])
    # Roots are local-domain data: never in the unauthenticated cockpit view.
    assert str(repo) not in json.dumps(overview, ensure_ascii=False)


def test_registration_is_idempotent(harness: Harness):
    repo = _git_repo(harness.root / "twice")
    first_status, first = _register(harness, repo, "ui5-twice")
    assert first_status == 200 and first["project"]["created"] is True
    before = harness.snapshot()
    second_status, second = _register(harness, repo, "ui5-twice")
    assert second_status == 200, second
    assert second["project"]["created"] is False
    assert second["project"]["project_id"] == first["project"]["project_id"]
    assert harness.snapshot() == before


# --------------------------------------------------------------------------- #
# 2. auth / origin / field boundary
# --------------------------------------------------------------------------- #
def test_registration_requires_token(harness: Harness):
    repo = _git_repo(harness.root / "auth")
    assert _register(harness, repo, token=None)[0] == 401
    assert _register(harness, repo, token="wrong-token")[0] == 401
    assert _register(harness, repo, origin="http://evil.example")[0] == 403
    assert _register(harness, repo, origin=None)[0] == 403


@pytest.mark.parametrize("payload", [
    {"root": "D:\\repos\\x"},
    {"name": "x"},
    {"root": "D:\\repos\\x", "name": "x", "policy": {"mode": "shell"}},
    {"root": "D:\\repos\\x", "name": "x", "adapter": "evil"},
    {"root": "D:\\repos\\x", "name": "x", "goal": "do work"},
    {},
])
def test_field_set_is_closed(harness: Harness, payload):
    status, body = harness.request(ROUTE, payload=payload)
    assert status == 400, body
    assert body["error"]["code"] == "invalid_fields"


# --------------------------------------------------------------------------- #
# 3. filesystem boundary
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("root", [
    "relative/path",
    "D:\\repos\\..\\Windows",
    "\\\\server\\share\\repo",
    "/etc",
    "D:\\repos\\x\x00y",
    "x" * 5000,
])
def test_bad_paths_are_rejected(harness: Harness, root: str):
    before = harness.snapshot()
    status, body = harness.request(
        ROUTE, payload={"root": root, "name": "ui5-bad"})
    assert status == 400, body
    assert body["error"]["code"] == "project_registration_rejected"
    assert harness.snapshot() == before, "a rejected path must write nothing"


def test_non_git_directory_is_rejected(harness: Harness):
    plain = harness.root / "plain"
    plain.mkdir()
    status, body = _register(harness, plain, "ui5-plain")
    assert status == 400, body
    assert body["error"]["code"] == "project_registration_rejected"


def test_git_subdirectory_is_rejected(harness: Harness):
    sub = harness.existing_repo / "src"
    sub.mkdir()
    status, body = _register(harness, sub, "ui5-sub")
    assert status == 400, body
    assert body["error"]["code"] == "project_registration_rejected"


def test_nested_root_is_rejected(harness: Harness):
    nested = _git_repo(harness.existing_repo / "nested")
    status, body = _register(harness, nested, "ui5-nested")
    assert status == 400, body
    assert body["error"]["code"] == "project_registration_rejected"


def test_name_is_a_label_not_a_path(harness: Harness):
    repo = _git_repo(harness.root / "name-check")
    for bad in ("../escape", "a\\b", "", "x" * 200, "line\nbreak"):
        status, body = _register(harness, repo, bad)
        assert status == 400, (bad, body)


# --------------------------------------------------------------------------- #
# 4. no work dispatched, disabled chat
# --------------------------------------------------------------------------- #
def test_registration_never_dispatches_or_creates_goals(harness: Harness):
    repo = _git_repo(harness.root / "no-work")
    before = harness.snapshot()
    status, body = _register(harness, repo)
    assert status == 200, body
    after = harness.snapshot()
    for table in ("work_items", "attempts", "ld_goals",
                  "ld_goal_decisions", "approvals"):
        assert after[table] == before[table], table
    assert "未创建 Goal" in body["note"]


def test_disabled_chat_rejects_registration():
    item = Harness(enable_chat=False)
    try:
        status, body = item.request(
            ROUTE, payload={"root": str(item.root), "name": "x"})
        assert status in (400, 405), body
    finally:
        item.close()


def test_console_form_registers_a_project():
    """The actual browser flow: token -> expand form -> paste path -> submit."""
    from playwright.sync_api import sync_playwright

    item = Harness()
    try:
        repo = _git_repo(item.root / "from-console")
        with sync_playwright() as playwright:
            try:
                engine = playwright.chromium.launch(channel="msedge",
                                                    headless=True)
            except Exception:  # noqa: BLE001 - fall back to bundled browser
                engine = playwright.chromium.launch(headless=True)
            try:
                page = engine.new_page(viewport={"width": 1400, "height": 1000})
                errors: list[str] = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                page.goto(item.base + "/", wait_until="load", timeout=30000)
                page.wait_for_selector("#chatToken", timeout=15000)
                # Registration must not be offered before authentication.
                assert page.is_hidden("#projectRegister")
                page.fill("#chatToken", TOKEN)
                page.click("#chatConnect")
                page.wait_for_selector("#projectRegister:not([hidden])",
                                       timeout=15000)
                page.click("#projectRegister > summary")
                page.fill("#registerRoot", str(repo))
                # The console derives a suggested name, so only the path is
                # actually required from the operator.
                assert page.input_value("#registerName") == "from-console"
                before = item.snapshot()
                page.click("#registerBtn")
                page.wait_for_selector("#registerResult:not([hidden])",
                                       timeout=15000)
                text = page.text_content("#registerResult") or ""
                assert "from-console" in text, text
                assert "未创建 Goal" in text, text
                assert errors == []
            finally:
                engine.close()

        after = item.snapshot()
        assert len(after["projects"]) == len(before["projects"]) + 1
        assert any(row[1] == "from-console" for row in after["projects"])
        for table in ("work_items", "attempts", "ld_goals",
                      "ld_goal_decisions", "approvals"):
            assert after[table] == before[table], table
    finally:
        item.close()
