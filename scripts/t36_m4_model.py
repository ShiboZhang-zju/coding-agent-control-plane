"""T36: real-model boundary and model-input security (no external calls).

A local OpenAI-compatible fake endpoint exercises the real ``OpenAIChatModel``
transport, error taxonomy, fail-closed coordinator behaviour and the M4B-01
model-view provenance filter deterministically.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.project_context import ContextEntry  # noqa: E402
from controlplane.project_service import ProjectService  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from ld.coordinator import LDCoordinator  # noqa: E402
from ld.model import (ModelCallError, ModelResponseError,  # noqa: E402
                      ModelTimeoutError, OpenAIChatModel)
from ld.tools import LDTools  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

FIXTURE = ROOT / "sandbox" / "m4-model"
REPO = FIXTURE / "repo-a"
LEDGER = ROOT / ".poc-state" / "t36-m4-model.db"


class AdapterRef:
    name = "stub"


class FakeEndpoint(BaseHTTPRequestHandler):
    """Scripted OpenAI-compatible endpoint; responses come from a queue."""

    responses: list[dict] = []
    requests_seen: list[dict] = []
    delay_s: float = 0.0

    def do_POST(self):  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        FakeEndpoint.requests_seen.append({
            "path": self.path,
            "has_auth": bool(self.headers.get("Authorization")),
            "model": body.get("model"),
            "user_content_len": len(
                (body.get("messages") or [{}])[-1].get("content") or ""),
        })
        if FakeEndpoint.delay_s:
            time.sleep(FakeEndpoint.delay_s)
        if not FakeEndpoint.responses:
            self._send(500, {"error": "no scripted response"})
            return
        scripted = FakeEndpoint.responses.pop(0)
        status = scripted.get("status", 200)
        if status == 200:
            self._send(200, {"choices": [{"message": {
                "role": "assistant", "content": scripted["content"]}}]})
        else:
            self._send(status, {"error": scripted.get("error", "boom")})

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def git(path: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(path), *args], check=False,
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"git {args} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def make_repo() -> None:
    if not (REPO / ".git").exists():
        shutil.rmtree(FIXTURE, ignore_errors=True)
        REPO.mkdir(parents=True)
        git(REPO, "init", "-q")
        git(REPO, "config", "user.email", "t36@example.invalid")
        git(REPO, "config", "user.name", "T36")
        (REPO / "seed.txt").write_text("seed\n", encoding="utf-8")
        git(REPO, "add", "seed.txt")
        git(REPO, "commit", "-q", "-m", "seed")
        git(REPO, "branch", "-M", "main")


def model_at(server: ThreadingHTTPServer, **kwargs) -> OpenAIChatModel:
    port = server.server_address[1]
    return OpenAIChatModel(base_url=f"http://127.0.0.1:{port}/v1",
                           model="fake-model", **kwargs)


def model_layer(server: ThreadingHTTPServer) -> dict:
    out: dict = {}

    FakeEndpoint.responses = [{"content": '{"action": "wait", "project_id": null,'
                                          ' "reason": "r", "evidence_refs": [],'
                                          ' "parameters": {"task_id": "cp-1"}}'}]
    out["bare_json_parses"] = model_at(server).decide(
        {"goal": "g"}).get("action") == "wait"

    FakeEndpoint.responses = [{"content": "```json\n{\"action\": \"finish\"}\n```"}]
    out["fenced_json_stripped"] = model_at(server).decide(
        {"goal": "g"}).get("action") == "finish"

    FakeEndpoint.responses = [{"status": 500, "error": "endpoint exploded"}]
    try:
        model_at(server).decide({"goal": "g"})
        out["http_error_typed"] = False
    except ModelCallError as exc:
        out["http_error_typed"] = "HTTP 500" in str(exc)

    FakeEndpoint.responses = [{"content": "thinking... not json"}]
    try:
        model_at(server).decide({"goal": "g"})
        out["bad_payload_typed"] = False
    except ModelResponseError:
        out["bad_payload_typed"] = True

    FakeEndpoint.responses = [{"content": "[1, 2, 3]"}]
    try:
        model_at(server).decide({"goal": "g"})
        out["non_object_rejected"] = False
    except ModelResponseError:
        out["non_object_rejected"] = True

    FakeEndpoint.responses = [{}]
    FakeEndpoint.delay_s = 1.0
    try:
        model_at(server, timeout_s=0.3).decide({"goal": "g"})
        out["timeout_typed"] = False
    except ModelTimeoutError:
        out["timeout_typed"] = True
    finally:
        FakeEndpoint.delay_s = 0.0

    described = model_at(server, api_key="sk-secret").describe()
    blob = json.dumps(described) + str(described)
    out["describe_hides_secret"] = "sk-secret" not in blob

    saved = {key: os.environ.pop(key, None) for key in
             ("LD_MODEL_BASE_URL", "LD_MODEL_NAME", "LD_MODEL_API_KEY")}
    try:
        out["from_env_absent_is_none"] = OpenAIChatModel.from_env() is None
        os.environ["LD_MODEL_BASE_URL"] = "http://127.0.0.1:9/v1"
        os.environ["LD_MODEL_NAME"] = "env-model"
        out["from_env_configures"] = bool(OpenAIChatModel.from_env())
    finally:
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value
    return out


def coordinator_layer(server: ThreadingHTTPServer, tools: LDTools,
                      project_id: str, task_id: str) -> dict:
    out: dict = {}
    domains = ("local", "shareable")

    FakeEndpoint.responses = [{"content": json.dumps({
        "action": "rm -rf /", "project_id": project_id, "reason": "bad",
        "evidence_refs": [], "parameters": {}})}]
    result = LDCoordinator(tools, model_at(server),
                           model_authorized_security_domains=domains
                           ).run_once("goal", project_id=project_id)
    out["illegal_action_fails_closed"] = (
        result["next_state"] == "invalid_model_decision" and not result["executed"])

    FakeEndpoint.responses = [{"status": 503, "error": "unavailable"}]
    result = LDCoordinator(tools, model_at(server),
                           model_authorized_security_domains=domains
                           ).run_once("goal", project_id=project_id)
    out["transport_error_fails_closed"] = (
        result["next_state"] == "invalid_model_decision" and not result["executed"])

    seen_before = len(FakeEndpoint.requests_seen)
    result = LDCoordinator(tools, model_at(server),
                           model_authorized_security_domains=("shareable",)
                           ).run_once("goal", project_id=project_id)
    out["unauthorized_domain_skips_model"] = (
        result["next_state"] == "human_required"
        and len(FakeEndpoint.requests_seen) == seen_before)

    provenance = tools.get_task_provenance(
        task_id, allowed_security_domains=("shareable",))
    blob = json.dumps(provenance, ensure_ascii=False, default=str)
    out["model_view_has_no_prompt"] = (
        "effective_prompt_omitted" in provenance
        and "PROMPT-SECRET" not in blob and "TASK-ORIGINAL-TEXT" not in blob)
    out["model_view_keeps_facts"] = (
        provenance.get("task_id") == task_id
        and provenance.get("verdict") is not None
        and provenance.get("state") is not None
        and provenance.get("task_digest") is not None)
    operator_view = tools.get_task_provenance(task_id)
    out["operator_view_keeps_prompt"] = (
        "PROMPT-SECRET" in json.dumps(operator_view, ensure_ascii=False))

    FakeEndpoint.responses = [{"content": json.dumps({
        "action": "inspect", "project_id": project_id, "reason": "check task",
        "evidence_refs": [], "parameters": {
            "tool": "get_task_provenance", "task_id": task_id}})}]
    result = LDCoordinator(tools, model_at(server),
                           model_authorized_security_domains=("local", "shareable")
                           ).run_once("goal", project_id=project_id)
    result_blob = json.dumps(result.get("result") or {}, ensure_ascii=False)
    # Authorized for local, the model may see local entry titles — but never
    # the frozen Effective Prompt or the original task text.
    out["inspect_returns_filtered_view"] = (
        result["next_state"] == "inspection_complete"
        and "PROMPT-SECRET" not in result_blob
        and "TASK-ORIGINAL-TEXT" not in result_blob
        and "effective_prompt_omitted" in result_blob)
    return out


def main() -> int:
    make_repo()
    for stale in [LEDGER, *LEDGER.parent.glob(LEDGER.name + ".*")]:
        stale.unlink(missing_ok=True)

    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeEndpoint)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    store = None
    try:
        r_model = model_layer(server)

        store = TaskStore(LEDGER)
        plane = ControlPlane(AdapterRef(), store, require_registered_projects=True,
                             worker_instance_id="wi-t36")
        service = ProjectService(plane)
        project = plane.register_project("t36-model", str(REPO),
                                         default_adapter="stub")
        service.add_context_entry(project["id"], ContextEntry(
            project_id=project["id"], kind="background",
            title="SHAREABLE-TITLE", body="PROMPT-SECRET shareable body",
            security_domain="shareable"))
        service.add_context_entry(project["id"], ContextEntry(
            project_id=project["id"], kind="knowledge",
            title="LOCAL-ONLY-TITLE", body="LOCAL-SECRET local body",
            security_domain="local"))
        record = service.submit_project_task(
            project["id"], "TASK-ORIGINAL-TEXT",
            idempotency_key="t36-task-1", title="T36 model input")
        task_id = record["task_id"]
        tools = LDTools(service)
        r_coord = coordinator_layer(server, tools, project["id"], task_id)

        sections = {"model": r_model, "coordinator": r_coord}
        checks = {f"{name}.{key}": bool(value)
                  for name, result in sections.items()
                  for key, value in result.items()}
        log = {
            "test": "T36-m4-model-boundary",
            "worker_started": False,
            "sections": sections,
            "checks": checks,
            "result": "Pass" if all(checks.values()) else "Fail",
        }
        write_evidence(ROOT / "evidence" / "project" / "T36-m4-model-boundary.json",
                       log)
        print(json.dumps(log, ensure_ascii=False, indent=2, default=str))
        return 0 if log["result"] == "Pass" else 1
    finally:
        server.shutdown()
        if store:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
