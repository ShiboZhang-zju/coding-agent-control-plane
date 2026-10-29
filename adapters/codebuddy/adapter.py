"""CodeBuddy implementation of the vendor-neutral WorkerAdapter contract.

Everything CodeBuddy-specific lives here: REST paths, job state strings, the
per-start password banner, the on-disk `jobs/<id>/state.json` layout and the
"dispatch returns 500 but still creates the job" workaround.

Mapping notes backed by evidence (see evidence/jobs/T00*.json):

  * `state` is the only terminal signal: working | blocked | done | failed | stopped
  * `status` (busy|idle|waiting|stopped) and `tempo` (active|idle|blocked) are
    derived display hints, not lifecycle authority
  * a shell job that exits non-zero is reported as `done` (T00b) -> the worker
    terminal state can never be used as an acceptance signal
  * POST /api/v1/jobs answers HTTP 500 (safe-delete shim) *after* the job was
    created, so dispatch always reconciles through GET /api/v1/jobs
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable

from ..base import (
    ArtifactAssertion,
    Capability,
    CapabilityLevel,
    DispatchRequest,
    DispatchResult,
    TaskEvent,
    TaskSnapshot,
    TaskState,
    UnsupportedOperation,
    Verdict,
)
from .client import Api, iter_sse_lines, project_root
__import__("sys").path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))  # repo root on path
import winproc  # noqa: E402  (hidden console children)

_TERMINAL_STATES = {"done", "failed", "stopped"}

_STATE_MAP = {
    "working": TaskState.RUNNING,
    "blocked": TaskState.WAITING_FOR_APPROVAL,
    "done": TaskState.SUCCEEDED,
    "failed": TaskState.FAILED,
    "stopped": TaskState.CANCELLED,
}


def _pid_alive(pid: int) -> bool:
    """True when the OS still owns this PID (Windows tasklist)."""
    try:
        out = winproc.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True, timeout=15).stdout
        return str(pid) in out
    except Exception:  # noqa: BLE001
        return True     # unknown -> do not declare lost


def _git(cwd: str, *args: str) -> tuple[int, str]:
    try:
        p = winproc.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=25)
        return p.returncode, (p.stdout or p.stderr).strip()
    except Exception as exc:  # noqa: BLE001
        return 1, f"{type(exc).__name__}: {exc}"


class CodeBuddyAdapter:
    """Thin, honest adapter over the local CodeBuddy REST API."""

    name = "codebuddy"

    def __init__(self, api: Api | None = None, autologin: bool = True):
        self.api = api or Api()
        if autologin and not self.api.token:
            self.api.login()
        self._jobs: dict[str, dict] = {}        # worker_job_id -> last raw job
        self._meta: dict[str, dict] = {}        # worker_job_id -> dispatch metadata

    # ------------------------------------------------------------------ #
    # capabilities
    # ------------------------------------------------------------------ #
    def capabilities(self) -> dict[str, Capability]:
        E = "evidence/"
        caps = [
            Capability("task_dispatch", CapabilityLevel.NATIVE, f"{E}jobs/T01-concurrency.json",
                       "POST /api/v1/jobs; HTTP 500 bug worked around by list-reconcile"),
            Capability("background_run", CapabilityLevel.NATIVE, f"{E}jobs/T00-api-probe.json"),
            Capability("concurrent_tasks", CapabilityLevel.NATIVE, f"{E}jobs/T01-concurrency.json"),
            Capability("worktree_isolation", CapabilityLevel.CONFIG, f"{E}isolation/T03-isolation.json",
                       "bgIsolation=worktree on dispatch"),
            Capability("status_query", CapabilityLevel.NATIVE, f"{E}jobs/T01-concurrency.json"),
            Capability("global_task_query", CapabilityLevel.NATIVE, f"{E}jobs/T01-concurrency.json",
                       "GET /api/v1/jobs?all=1"),
            Capability("sse", CapabilityLevel.NATIVE, f"{E}sse/T05-sse-reconnect.json",
                       "GET /api/v1/jobs/events, event types snapshot|added|changed|keepalive"),
            Capability("sse_reconnect", CapabilityLevel.NATIVE, f"{E}sse/T05-sse-reconnect.json"),
            Capability("event_replay", CapabilityLevel.UNSUPPORTED, f"{E}sse/T05-sse-reconnect.json",
                       "no SSE id:/Last-Event-ID support"),
            Capability("state_reconcile", CapabilityLevel.PLANE, f"{E}sse/T05-sse-reconnect.json",
                       "adapter.reconcile() re-queries /api/v1/jobs?all=1"),
            Capability("cancel", CapabilityLevel.NATIVE, f"{E}recovery/T06-cancel-stop.json",
                       "POST /api/v1/jobs/{id}/stop"),
            Capability("stop", CapabilityLevel.NATIVE, f"{E}recovery/T06-cancel-stop.json"),
            Capability("resume", CapabilityLevel.ADAPTER, f"{E}recovery/T07-resume-respawn.json",
                       "POST /api/v1/jobs/resume for archived sessions only"),
            Capability("respawn", CapabilityLevel.NATIVE, f"{E}recovery/T07-resume-respawn.json",
                       "POST /api/v1/jobs/{id}/respawn"),
            Capability("conversation_recovery", CapabilityLevel.NATIVE,
                       f"{E}recovery/T07-resume-respawn.json"),
            Capability("execution_recovery", CapabilityLevel.UNSUPPORTED,
                       f"{E}recovery/T08-service-restart.json"),
            Capability("workflow_recovery", CapabilityLevel.PLANE,
                       f"{E}recovery/T08-service-restart.json",
                       "plane-side persistent task store + reconcile"),
            Capability("approval_request", CapabilityLevel.NATIVE, f"{E}permissions/T09-*.json",
                       "state=blocked + waitingFor"),
            Capability("human_denial", CapabilityLevel.UNVERIFIED, f"{E}permissions/",
                       "no API to deliver a deny decision; requires Web UI click"),
            Capability("artifact_collection", CapabilityLevel.ADAPTER,
                       f"{E}isolation/T03-isolation.json", "git + declared paths"),
            Capability("failure_detection", CapabilityLevel.PLANE, f"{E}jobs/T00b-state-semantics.json",
                       "worker reports done even on non-zero exit"),
            Capability("auto_acceptance", CapabilityLevel.PLANE, f"{E}jobs/T04-acceptance.json"),
            Capability("master_agent_summary", CapabilityLevel.PLANE, f"{E}jobs/T10-summary.json"),
            Capability("adapter_onboarding_cost", CapabilityLevel.ADAPTER, "REPORT.md",
                       "~1 adapter file + state map per worker"),
        ]
        return {c.name: c for c in caps}

    # ------------------------------------------------------------------ #
    # dispatch
    # ------------------------------------------------------------------ #
    def dispatch(self, request: DispatchRequest) -> DispatchResult:
        warnings: list[str] = []
        if not request.idempotency_key:
            warnings.append("NO_IDEMPOTENCY_KEY: worker has no idempotency key field; "
                            "the control plane must dedupe before calling dispatch()")
        # NOTE: no pre-dispatch list snapshot. `GET /api/v1/jobs?all=1` needs ~30s on a
        # large store (T08b), so recovery matches on (cwd, prompt, startedAt) instead.

        body: dict[str, Any] = {"prompt": request.prompt, "cwd": request.cwd}
        if request.mode == "shell":
            body["bash"] = True
        else:
            body["permissionMode"] = request.metadata.get("permissionMode", "acceptEdits")
            body["bgIsolation"] = "worktree" if request.isolation == "worktree" else "none"
            if request.metadata.get("model"):
                body["model"] = request.metadata["model"]
        if request.title:
            body["name"] = request.title

        # Exactly ONE POST per dispatch() call. The worker can answer 500 *after*
        # creating the job, so a second POST could start a duplicate; when the
        # outcome cannot be proven the result is reported as `uncertain` and the
        # plane decides (state=dispatch_unknown -> resolve_dispatch).
        attempt_started = time.time()
        resp = self.api.dispatch(**body)
        data = resp["body"].get("data") if isinstance(resp["body"], dict) else None
        job_id = None
        if isinstance(data, dict):
            job_id = data.get("id") or data.get("jobId")

        raw: dict[str, Any] = {"dispatch_http": resp["status"], "dispatch_body": resp["body"],
                               "posted_at": attempt_started}
        if resp["status"] != 200:
            warnings.append(
                f"DISPATCH_HTTP_{resp['status']}: response body is not authoritative "
                "(the job may still have been created)")

        if not job_id:
            job_id = self._recover_job_id(request, since=attempt_started)
            if job_id:
                warnings.append("DISPATCH_RECOVERED_BY_LIST: job existed despite the error response")

        raw["recovered_by"] = "list-jobs scan" if job_id else None

        if not job_id:
            # Proven-absent is NOT claimed here: the recovery window may simply
            # have been too short. The plane must call resolve_dispatch().
            warnings.append("DISPATCH_UNCERTAIN: no job observed after one POST; "
                            "not retried (would risk a duplicate)")
            return DispatchResult(task_id=request.task_id, worker_job_id=None, accepted=False,
                                  uncertain=True, warnings=warnings, raw=raw)

        job = self.api.job(job_id)
        self._jobs[job_id] = job
        self._meta[job_id] = {"request": request, "raw": raw,
                              "started_at": time.time()}
        return DispatchResult(task_id=request.task_id, worker_job_id=job_id,
                              accepted=True, warnings=warnings, raw=raw)

    def find_job(self, prompt: str, cwd: str, since: float) -> str | None:
        """Look for a job matching a dispatch signature (read-only, no side effect)."""
        request = DispatchRequest(prompt=prompt, cwd=cwd, task_id="lookup")
        return self._recover_job_id(request, since=since)

    def _recover_job_id(self, request: DispatchRequest, since: float) -> str | None:
        """The API can 500 after creating the job; find it by cwd+prompt+startedAt."""
        deadline = time.time() + 25
        while time.time() < deadline:
            for j in self.api.jobs():
                started = (j.get("startedAt") or 0) / 1000.0
                if started + 2 < since:
                    continue
                if j.get("cwd") != request.cwd:
                    continue
                if (j.get("intent") or j.get("exec") or "").strip() == request.prompt.strip():
                    return j["id"]
            time.sleep(0.5)
        return None

    # ------------------------------------------------------------------ #
    # queries
    # ------------------------------------------------------------------ #
    def _to_snapshot(self, job: dict, task_id: str | None = None) -> TaskSnapshot:
        job_id = job.get("id")
        state = _STATE_MAP.get(job.get("state", ""), TaskState.UNKNOWN)
        alive = job.get("alive")
        settled = job.get("settled")
        # T01 showed `alive` flickers false for a healthy running job, so a single
        # sample is not enough: only treat it as lost once the record stops moving
        # *and* the OS process is really gone.
        stale_s = 0.0
        updated = job.get("updatedAt")
        if updated:
            stale_s = time.time() - (updated / 1000.0)
        pid = job.get("pid")
        pid_dead = (pid is None) or (not _pid_alive(int(pid)))
        lost_candidate = (state is TaskState.RUNNING and alive is False
                          and not settled and pid_dead and stale_s > 90)
        if lost_candidate:
            state = TaskState.LOST
        meta = self._meta.get(job_id, {})
        req: DispatchRequest | None = meta.get("request")
        started = job.get("startedAt")
        finished = job.get("firstTerminalAt")
        snap = TaskSnapshot(
            task_id=task_id or (req.task_id if req else job_id),
            worker_job_id=job_id,
            state=state,
            title=job.get("name") or job.get("intent") or "",
            cwd=job.get("cwd") or "",
            pid=job.get("pid"),
            session_id=job.get("sessionId"),
            worktree=job.get("worktree") or job.get("worktreePath"),
            branch=job.get("branch"),
            started_at=(started / 1000.0) if started else None,
            finished_at=(finished / 1000.0) if finished else None,
            detail=job.get("detail") or "",
            metadata={"codebuddy": {
                "state": job.get("state"), "status": job.get("status"), "tempo": job.get("tempo"),
                "alive": alive, "settled": settled, "waitingFor": job.get("waitingFor"),
                "needs": job.get("needs"), "block": job.get("block"),
                "kind": job.get("kind"), "exec": job.get("exec"),
                "logPath": job.get("logPath"), "webUrl": job.get("webUrl"),
                "initialSessionId": job.get("initialSessionId"),
                "stale_s": round(stale_s, 1), "lost_candidate": lost_candidate,
            }},
        )
        if req:
            snap.artifacts = [a.target for a in req.assertions]
        return snap

    def get_task(self, task_id_or_job: str) -> TaskSnapshot:
        job = self.api.job(task_id_or_job)
        if not job:
            raise KeyError(f"job not found: {task_id_or_job}")
        self._jobs[job["id"]] = job
        return self._to_snapshot(job, task_id=self._task_id_for(job["id"]))

    def _task_id_for(self, job_id: str) -> str:
        meta = self._meta.get(job_id, {})
        req = meta.get("request")
        return req.task_id if req else job_id

    def list_tasks(self, include_terminal: bool = True) -> list[TaskSnapshot]:
        jobs = self.api.jobs()
        out = []
        for j in jobs:
            self._jobs[j["id"]] = j
            snap = self._to_snapshot(j, task_id=self._task_id_for(j["id"]))
            if not include_terminal and snap.state.terminal:
                continue
            out.append(snap)
        return out

    # ------------------------------------------------------------------ #
    # events
    # ------------------------------------------------------------------ #
    def stream_events(self, task_id: str | None = None, max_seconds: float | None = None
                      ) -> Iterable[TaskEvent]:
        """Normalize the job-list SSE stream into TaskEvents.

        The stream carries no `id:` field, so `event_id`/`cursor` stay None: a
        reconnect cannot resume, it can only re-observe current state.
        """
        resp = self.api.open_stream("/api/v1/jobs/events", timeout=(max_seconds or 3600) + 5)
        event_name, buf = "message", []
        try:
            for _, line in iter_sse_lines(resp, max_seconds=max_seconds):
                if line.startswith("event:"):
                    event_name = line.split(":", 1)[1].strip()
                    continue
                if line.startswith("id:"):
                    yield TaskEvent(type="unsupported-replay-marker", task_id=task_id or "",
                                    event_id=line.split(":", 1)[1].strip())
                    continue
                if line.startswith("data:"):
                    buf.append(line.split(":", 1)[1].strip())
                    continue
                if line == "":
                    payload = "\n".join(buf)
                    buf = []
                    if not payload:
                        continue
                    try:
                        parsed = json.loads(payload)
                    except Exception:
                        parsed = {"_raw": payload[:500]}
                    yield from self._events_for(event_name, parsed, task_id)
                # ignore comment/keepalive frames without data
            # flush a trailing frame
            if buf:
                try:
                    parsed = json.loads("\n".join(buf))
                except Exception:
                    parsed = {"_raw": "\n".join(buf)[:500]}
                yield from self._events_for(event_name, parsed, task_id)
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def _events_for(self, event_name: str, parsed: Any, task_id: str | None) -> Iterable[TaskEvent]:
        now = time.time()
        if event_name == "keepalive":
            yield TaskEvent(type="keepalive", task_id=task_id or "", ts=now)
            return
        jobs = parsed if isinstance(parsed, list) else [parsed]
        for j in jobs:
            if not isinstance(j, dict) or not j.get("id"):
                continue
            jid = j["id"]
            tid = self._task_id_for(jid)
            if task_id and task_id not in (tid, jid):
                continue
            st = _STATE_MAP.get(j.get("state", ""), TaskState.UNKNOWN)
            yield TaskEvent(
                type={"snapshot": "snapshot", "added": "dispatched",
                      "changed": "state_changed"}.get(event_name, event_name),
                task_id=tid, worker_job_id=jid, session_id=j.get("sessionId"),
                state=st, ts=now,
                payload={"worker_state": j.get("state"), "detail": j.get("detail"),
                         "status": j.get("status"), "tempo": j.get("tempo")},
            )

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def cancel(self, task_id: str) -> TaskSnapshot:
        job_id = self._job_id(task_id)
        r = self.api.stop_job(job_id)
        self.api.calls.append(r)
        deadline = time.time() + 30
        while time.time() < deadline:
            job = self.api.job(job_id)
            if job.get("state") in _TERMINAL_STATES or job.get("settled"):
                self._jobs[job_id] = job
                return self._to_snapshot(job, task_id=task_id)
            time.sleep(1.0)
        return self.get_task(job_id)

    def resume(self, task_id: str) -> TaskSnapshot:
        job_id = self._job_id(task_id)
        job = self.api.job(job_id)
        session_id = job.get("sessionId")
        if not session_id:
            raise UnsupportedOperation("no sessionId on this job")
        r = self.api.resume_session(session_id, cwd=job.get("cwd"))
        if r["status"] != 200:
            raise UnsupportedOperation(f"resume rejected: HTTP {r['status']} {r['body']}")
        new_job_id = (r["body"].get("data") or {}).get("id") or (r["body"].get("data") or {}).get("jobId")
        if new_job_id:
            return self.get_task(new_job_id)
        return self.get_task(job_id)

    def respawn(self, task_id: str) -> TaskSnapshot:
        job_id = self._job_id(task_id)
        r = self.api.respawn_job(job_id)
        if r["status"] != 200:
            raise UnsupportedOperation(f"respawn rejected: HTTP {r['status']} {r['body']}")
        time.sleep(2)
        return self.get_task(job_id)

    def request_approval(self, task_id: str, decision: str, note: str = "") -> dict:
        raise UnsupportedOperation(
            "CodeBuddy 2.169.0 exposes no REST endpoint that delivers a permission decision "
            "for a job: POST /api/v1/jobs/{id}/reply only injects a user *message*, and "
            "/api/v1/permission-bridge/request targets teammate->leader bridging. "
            "Approval must be answered in the Web UI (human click)."
        )

    # ------------------------------------------------------------------ #
    # artifacts / reconcile
    # ------------------------------------------------------------------ #
    def collect_artifacts(self, task_id: str) -> list[str]:
        job_id = self._job_id(task_id)
        job = self._jobs.get(job_id) or self.api.job(job_id)
        cwd = job.get("cwd") or ""
        found: list[str] = []
        if cwd and os.path.isdir(cwd):
            rc, out = _git(cwd, "status", "--porcelain")
            if rc == 0:
                for line in out.splitlines():
                    path = line[3:].strip()
                    if path:
                        found.append(os.path.join(cwd, path))
            rc, out = _git(cwd, "log", "--oneline", "-10")
            if rc == 0:
                found.extend([f"commit:{ln}" for ln in out.splitlines()])
        log_path = self._log_path(job_id)
        if log_path and os.path.exists(log_path):
            found.append(log_path)
        return found

    def _log_path(self, job_id: str) -> str | None:
        state_file = Path.home() / ".codebuddy" / "jobs" / job_id / "state.json"
        if not state_file.exists():
            return None
        try:
            return json.loads(state_file.read_text(encoding="utf-8")).get("logPath")
        except Exception:
            return None

    def worker_exit_signal(self, task_id: str) -> dict:
        """Best-effort failure signal that the REST API does not expose.

        Returns {"exit_code": None, "stderr_tail": str} — CodeBuddy never persists
        the shell exit code (T00b), so exit_code is always None and callers must
        rely on artifact assertions instead.
        """
        job_id = self._job_id(task_id)
        log_path = self._log_path(job_id)
        tail = ""
        if log_path and os.path.exists(log_path):
            try:
                text = Path(log_path).read_text(encoding="utf-8", errors="replace")
                tail = "\n".join(text.splitlines()[-20:])
            except Exception:
                tail = ""
        return {"exit_code": None, "stderr_tail": tail,
                "log_path": log_path, "note": "exit code not exposed by CodeBuddy 2.169.0"}

    def reconcile(self) -> dict:
        """Re-query every job and repair local view. Never invents history."""
        before = {jid: j.get("state") for jid, j in self._jobs.items()}
        jobs = self.api.jobs()
        seen = set()
        changed, new, lost = [], [], []
        for j in jobs:
            seen.add(j["id"])
            prev = before.get(j["id"])
            if prev is None:
                new.append(j["id"])
            elif prev != j.get("state"):
                changed.append({"job": j["id"], "from": prev, "to": j.get("state")})
            self._jobs[j["id"]] = j
        for jid in before:
            if jid not in seen:
                lost.append(jid)
                j = self._jobs.get(jid, {})
                if not j:
                    continue
                j["state"] = "lost"
                self._jobs[jid] = j
        return {"source": "GET /api/v1/jobs?all=1", "total": len(jobs),
                "new": new, "changed": changed, "lost": lost,
                "jobs": jobs, "at": time.time()}

    # ------------------------------------------------------------------ #
    def _job_id(self, task_id: str) -> str:
        if task_id in self._jobs:
            return task_id
        for jid, job in self._jobs.items():
            if (job.get("name") or job.get("intent") or "").startswith(task_id):
                return jid
        return task_id
