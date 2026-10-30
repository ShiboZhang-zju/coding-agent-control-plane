"""T40: handoff checkpoint safety (P1-1) and model security-domain default (P1-2).

C1  the accepted dirty state becomes a commit containing exactly the accepted
    files; caches are skipped; the source worktree, branch and main never move.
C2  every unprovable state blocks the handoff with no WorkItem, no ref, no
    branch/worktree change: file modified after acceptance, new file after
    acceptance, sensitive file in the accepted state, unverified source, dirty
    worktree without an acceptance snapshot, HEAD moved after acceptance.
C3  a submission refused before preparation (shell policy) writes no Git
    objects at all; an idempotency conflict creates no second WorkItem/ref.
C4  ld_cli with a real endpoint and no explicit domain grant never calls the
    endpoint for a 'local' goal; an explicit grant does.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import TaskState  # noqa: E402
from controlplane.handoff import HandoffIdempotencyConflict, HandoffNotReady  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.policies import poc_shell  # noqa: E402
from controlplane.project_service import ProjectService  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from controlplane.verify import worktree_fingerprint  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

FIXTURE = ROOT / "sandbox" / "m4-checkpoint-safety"
REPO = FIXTURE / "repo-a"
WORKTREE = FIXTURE / "repo-a-wt"
LEDGER = ROOT / ".poc-state" / "t40-checkpoint-safety.db"
CLI_LEDGER = ROOT / ".poc-state" / "t40-cli-domains.db"
SEQ = iter(range(1, 1000))


class AdapterRef:
    name = "stub"


def git(path: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(path), *args], check=False,
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"git {args} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def make_repo() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)
    REPO.mkdir(parents=True)
    git(REPO, "init", "-q")
    git(REPO, "config", "user.email", "t40@example.invalid")
    git(REPO, "config", "user.name", "T40")
    (REPO / "seed.txt").write_text("seed\n", encoding="utf-8")
    git(REPO, "add", "seed.txt")
    git(REPO, "commit", "-q", "-m", "seed")
    git(REPO, "branch", "-M", "main")


def new_worktree(name: str) -> Path:
    path = FIXTURE / f"wt-{name}"
    git(REPO, "worktree", "add", "-q", str(path), "-b", f"wt-{name}")
    return path


def source_in(plane: ControlPlane, store: TaskStore, project_id: str, wt: Path, *,
              verdict: str = "verified", accept: bool = True) -> str:
    """A finished source task whose acceptance pinned ``wt``'s current state."""
    n = next(SEQ)
    workspace = store.default_workspace(project_id)
    rec = plane.enqueue(f"t40 source {n}", str(REPO), title=f"t40-src-{n}",
                        project_id=project_id, workspace_id=workspace["id"])
    task_id = rec["task_id"]
    job = f"job-{n}"
    store.add_attempt(task_id, worker_job_id=job)
    store.observe_session_reference(
        work_item_id=task_id, worker_job_id=job, node_id="local",
        worker_instance_id="wi-t40", adapter="stub", session_id=f"sess-{n}")
    metadata = dict((store.get(task_id) or {}).get("metadata") or {})
    if accept:
        metadata["acceptance_snapshot"] = worktree_fingerprint(str(wt))
    store.update(task_id, state=TaskState.SUCCEEDED.value, verdict=verdict,
                 worktree=str(wt), head_commit=git(wt, "rev-parse", "HEAD"),
                 finished_at=1_700_000_000.0, metadata=metadata,
                 artifacts=[], assertion_results=[])
    return task_id


def snapshot(wt: Path) -> dict:
    return {"head": git(wt, "rev-parse", "HEAD"),
            "status": git(wt, "status", "--porcelain", "--untracked-files=all"),
            "main": git(REPO, "rev-parse", "main"),
            "refs": git(REPO, "for-each-ref", "refs/control-plane/")}


def attempt_handoff(service: ProjectService, source: str, **kwargs) -> dict:
    tasks_before = len(service.store.all())
    error = None
    try:
        service.submit_handoff(source, "continue on the accepted code",
                               checkpoint_mode="reachable", require_checkpoint=True,
                               idempotency_key=f"t40-{uuid.uuid4().hex[:6]}", **kwargs)
    except (HandoffNotReady, ValueError) as exc:
        error = exc
    return {"error": error, "new_tasks": len(service.store.all()) - tasks_before}


def blocked_case(service: ProjectService, source: str, wt: Path, needle: str) -> dict:
    before = snapshot(wt)
    attempt = attempt_handoff(service, source)
    preview = service.prepare_handoff(source, "continue on the accepted code",
                                      checkpoint_mode="reachable")
    after = snapshot(wt)
    message = str(attempt["error"] or "")
    return {
        "submit_refused": isinstance(attempt["error"], HandoffNotReady),
        "reason_reported": needle in message,
        "preview_reports_blocker": any(needle in b for b in preview.get("blockers", [])),
        "no_workitem": attempt["new_tasks"] == 0,
        "no_side_effects": before == after,
    }


def c1_accepted_state(service, plane, store, project_id) -> dict:
    wt = new_worktree("c1")
    (wt / "feature.py").write_text("x = 1\n", encoding="utf-8")
    (wt / "seed.txt").write_text("seed\nchanged\n", encoding="utf-8")
    (wt / "__pycache__").mkdir()
    (wt / "__pycache__" / "feature.cpython-311.pyc").write_bytes(b"\x00\x01")
    (wt / ".pytest_cache").mkdir()
    (wt / ".pytest_cache" / "README.md").write_text("cache\n", encoding="utf-8")
    source = source_in(plane, store, project_id, wt)
    # A test run after acceptance only adds caches: not a change of accepted code.
    (wt / "__pycache__" / "late.cpython-311.pyc").write_bytes(b"\x02")
    before = snapshot(wt)
    record = service.submit_handoff(
        source, "continue on the accepted code", checkpoint_mode="reachable",
        require_checkpoint=True, idempotency_key=f"t40-c1-{uuid.uuid4().hex[:6]}")
    after = snapshot(wt)
    capture = (record.get("handoff_context") or {}).get("checkpoint", {}).get("capture") or {}
    commit = capture.get("commit") or ""
    files = git(wt, "ls-tree", "-r", "--name-only", commit).splitlines() if commit else []
    ref = f"refs/control-plane/handoff/{record.get('task_id')}"
    return {
        "commit_contains_new_file": "feature.py" in files,
        "commit_contains_modification": "changed" in git(wt, "show", f"{commit}:seed.txt"),
        "caches_not_committed": not any(
            f.startswith(("__pycache__/", ".pytest_cache/")) for f in files),
        "caches_reported_as_skipped": len(capture.get("skipped_paths") or []) == 2,
        "parent_is_accepted_head": git(wt, "rev-parse", f"{commit}^") == before["head"],
        "source_worktree_untouched": (before["head"], before["status"])
        == (after["head"], after["status"]),
        "main_untouched": before["main"] == after["main"],
        "ref_only_after_success": ref not in before["refs"] and ref in after["refs"],
        "prompt_merges_checkpoint": f"git merge {commit}" in (record.get("effective_prompt") or ""),
    }


def c2_unprovable_states(service, plane, store, project_id) -> dict:
    out: dict = {}

    wt = new_worktree("modified")
    (wt / "feature.py").write_text("x = 1\n", encoding="utf-8")
    source = source_in(plane, store, project_id, wt)
    (wt / "feature.py").write_text("x = 2  # edited after acceptance\n", encoding="utf-8")
    out["modified_after_acceptance"] = blocked_case(
        service, source, wt, "changed after acceptance")

    wt = new_worktree("added")
    (wt / "feature.py").write_text("x = 1\n", encoding="utf-8")
    source = source_in(plane, store, project_id, wt)
    (wt / "notes.txt").write_text("unreviewed\n", encoding="utf-8")
    out["new_file_after_acceptance"] = blocked_case(
        service, source, wt, "changed after acceptance")

    wt = new_worktree("sensitive")
    (wt / "feature.py").write_text("x = 1\n", encoding="utf-8")
    (wt / ".env").write_text("API_TOKEN=do-not-hand-over\n", encoding="utf-8")
    source = source_in(plane, store, project_id, wt)
    out["sensitive_file"] = blocked_case(service, source, wt, "sensitive-looking")

    wt = new_worktree("rejected")
    (wt / "feature.py").write_text("x = 1\n", encoding="utf-8")
    source = source_in(plane, store, project_id, wt, verdict="rejected")
    out["unverified_source"] = blocked_case(service, source, wt, "only verified work")

    wt = new_worktree("nosnap")
    (wt / "feature.py").write_text("x = 1\n", encoding="utf-8")
    source = source_in(plane, store, project_id, wt, accept=False)
    out["dirty_without_snapshot"] = blocked_case(service, source, wt, "never verified")

    wt = new_worktree("moved")
    source = source_in(plane, store, project_id, wt)
    (wt / "late.txt").write_text("late\n", encoding="utf-8")
    git(wt, "add", "late.txt")
    git(wt, "-c", "user.name=T40", "-c", "user.email=t40@example.invalid",
        "commit", "-q", "-m", "after acceptance")
    out["head_moved_with_snapshot"] = blocked_case(
        service, source, wt, "changed after acceptance")

    wt = new_worktree("moved-nosnap")
    source = source_in(plane, store, project_id, wt, accept=False)
    (wt / "late.txt").write_text("late\n", encoding="utf-8")
    git(wt, "add", "late.txt")
    git(wt, "-c", "user.name=T40", "-c", "user.email=t40@example.invalid",
        "commit", "-q", "-m", "after acceptance")
    out["head_moved_without_snapshot"] = blocked_case(
        service, source, wt, "HEAD moved after acceptance")
    return out


def c3_refused_requests(service, plane, store, project_id) -> dict:
    wt = new_worktree("refused")
    (wt / "feature.py").write_text("x = 1\n", encoding="utf-8")
    source = source_in(plane, store, project_id, wt)
    objects_before = git(REPO, "count-objects", "-v")
    before = snapshot(wt)
    shell = attempt_handoff(service, source, policy=poc_shell())
    out = {
        "shell_policy_refused": isinstance(shell["error"], ValueError),
        "shell_refusal_wrote_no_git_objects": git(REPO, "count-objects", "-v") == objects_before,
        "shell_refusal_no_workitem": shell["new_tasks"] == 0,
        "shell_refusal_no_side_effects": snapshot(wt) == before,
    }
    key = f"t40-c3-{uuid.uuid4().hex[:6]}"
    first = service.submit_handoff(source, "continue", checkpoint_mode="reachable",
                                   idempotency_key=key)
    refs_after_first = snapshot(wt)["refs"]
    conflict = False
    try:
        service.submit_handoff(source, "continue", checkpoint_mode="reachable",
                               idempotency_key=key,
                               assertions=[{"kind": "file_exists", "target": "other.py"}])
    except HandoffIdempotencyConflict:
        conflict = True
    out.update({
        "idempotency_conflict_raised": conflict,
        "single_workitem_for_key": sum(
            1 for t in store.all() if t.get("idempotency_key") == key) == 1,
        "no_second_ref": snapshot(wt)["refs"] == refs_after_first,
        "first_submit_ok": bool(first.get("task_id")),
    })
    return out


class FakeEndpoint(BaseHTTPRequestHandler):
    hits = 0

    def do_POST(self):  # noqa: N802 - http.server API
        FakeEndpoint.hits += 1
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        decision = {"action": "request_human", "project_id": None,
                    "reason": "fake endpoint", "evidence_refs": [],
                    "parameters": {"question": "ok?"}}
        body = json.dumps({"choices": [{"message": {
            "role": "assistant", "content": json.dumps(decision)}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence
        return


def c4_cli_domain_default() -> dict:
    for stale in [CLI_LEDGER, *CLI_LEDGER.parent.glob(CLI_LEDGER.name + ".*")]:
        stale.unlink(missing_ok=True)
    store = TaskStore(CLI_LEDGER)
    ControlPlane(AdapterRef(), store).register_project(
        "t40-cli", str(REPO), default_adapter="stub")
    store.close()
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeEndpoint)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = {k: v for k, v in os.environ.items() if not k.startswith("LD_MODEL_")}
    base = [sys.executable, str(ROOT / "scripts" / "ld_cli.py"), "--ledger",
            str(CLI_LEDGER), "--compact", "run-once", "推进当前项目",
            "--project", "t40-cli", "--model-base-url",
            f"http://127.0.0.1:{server.server_address[1]}/v1", "--model-name", "fake"]

    def run(extra=(), extra_env=None):
        proc = subprocess.run([*base, *extra], capture_output=True, text=True,
                              timeout=60, cwd=str(ROOT), encoding="utf-8",
                              env={**env, **(extra_env or {})})
        return json.loads((proc.stdout or "{}").strip() or "{}")

    try:
        FakeEndpoint.hits = 0
        default = run()
        hits_default = FakeEndpoint.hits
        explicit = run(("--model-domains", "local,shareable"))
        hits_explicit = FakeEndpoint.hits - hits_default
        before_env = FakeEndpoint.hits
        via_env = run(extra_env={"LD_MODEL_DOMAINS": "local,shareable"})
        hits_env = FakeEndpoint.hits - before_env
    finally:
        server.shutdown()
    return {
        "default_grants_no_domain": "security_domain_unauthorized:local" in (
            default.get("blocking_reasons") or []),
        "default_never_calls_endpoint": hits_default == 0,
        "explicit_flag_grant_calls_endpoint": hits_explicit >= 1
        and explicit.get("decision_source") == "model",
        "env_grant_calls_endpoint": hits_env >= 1 and via_env.get("decision_source") == "model",
    }


def main() -> int:
    make_repo()
    for stale in [LEDGER, *LEDGER.parent.glob(LEDGER.name + ".*")]:
        stale.unlink(missing_ok=True)
    store = TaskStore(LEDGER)
    try:
        plane = ControlPlane(AdapterRef(), store, require_registered_projects=True,
                             worker_instance_id="wi-t40")
        service = ProjectService(plane)
        project_id = plane.register_project("t40", str(REPO), default_adapter="stub")["id"]
        sections = {
            "C1_accepted_state": c1_accepted_state(service, plane, store, project_id),
            "C2_unprovable_states": c2_unprovable_states(service, plane, store, project_id),
            "C3_refused_requests": c3_refused_requests(service, plane, store, project_id),
        }
    finally:
        store.close()
    sections["C4_cli_domain_default"] = c4_cli_domain_default()

    checks: dict[str, bool] = {}

    def collect(node, prefix):
        for key, value in node.items():
            name = f"{prefix}.{key}"
            if isinstance(value, bool):
                checks[name] = value
            elif isinstance(value, dict):
                collect(value, name)

    for name, section in sections.items():
        collect(section, name)
    log = {"test": "T40-m4-checkpoint-safety", "worker_started": False,
           "sections": sections, "checks": checks,
           "result": "Pass" if checks and all(checks.values()) else "Fail"}
    write_evidence(ROOT / "evidence" / "project" / "T40-m4-checkpoint-safety.json", log)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps(log, ensure_ascii=False, indent=2, default=str))
    return 0 if log["result"] == "Pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
