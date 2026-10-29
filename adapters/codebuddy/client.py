"""Thin HTTP/SSE transport for the local CodeBuddy server (adapter-internal).

Nothing here leaks into the vendor-neutral contract; `adapter.py` is the only
caller. Security posture: the bearer token and the server password never touch
disk, and every request carries the mandatory `X-CodeBuddy-Request: 1` header
(see OPENAPI_REQUIREMENTS.md).
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable

ANSI = re.compile(r"\x1b\[[0-9;]*m")
DEFAULT_BASE = os.environ.get("POC_BASE_URL", "http://127.0.0.1:8787")


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def server_password(log_path: Path | None = None) -> str:
    """Read the per-start random server password from the (git-ignored) banner log."""
    env = os.environ.get("POC_SERVE_PASSWORD")
    if env:
        return env
    # Two launch modes write two different banners (default desktop vs hidden
    # desktop); only one server runs at a time, so trust the most recently
    # modified banner that actually contains a password line.
    env_dir = project_root() / "evidence" / "environment"
    candidates = [log_path] if log_path else [env_dir / "serve-stdout.log",
                                             env_dir / "hidden-serve-stdout.log"]
    banners: list[tuple[float, str]] = []
    for path in candidates:
        if not path or not path.exists():
            continue
        txt = ANSI.sub("", path.read_text(encoding="utf-8", errors="replace"))
        found = re.findall(r"^.*Password\s+(\S+)\s*$", txt, flags=re.M)
        if found:
            banners.append((path.stat().st_mtime, found[-1]))
    if not banners:
        raise RuntimeError("server password unknown: no env POC_SERVE_PASSWORD and no banner log")
    return max(banners)[1]


class Api:
    """Blocking JSON client. Every call returns a recorded request/response pair."""

    def __init__(self, base: str = DEFAULT_BASE, password: str | None = None):
        self.base = base.rstrip("/")
        self._password = password
        self.token: str | None = None
        self.calls: list[dict] = []

    # -- low level -------------------------------------------------------
    def raw(self, method: str, path: str, body: Any = None, timeout: float = 30.0,
            extra_headers: dict[str, str] | None = None) -> tuple[int, bytes, dict[str, str]]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"X-CodeBuddy-Request": "1"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        headers.update(extra_headers or {})
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers or {})

    def call(self, method: str, path: str, body: Any = None, timeout: float = 30.0) -> dict:
        t0 = time.time()
        status, payload, headers = self.raw(method, path, body, timeout)
        elapsed = (time.time() - t0) * 1000
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except Exception:
            parsed = {"_raw": payload.decode("utf-8", errors="replace")[:2000]}
        rec = {"method": method, "path": path, "status": status,
               "requested_at": t0, "elapsed_ms": round(elapsed, 1),
               "content_type": headers.get("Content-Type", ""), "body": parsed}
        self.calls.append(rec)
        return rec

    # -- auth ------------------------------------------------------------
    def login(self) -> dict:
        self._password = self._password or server_password()
        r = self.call("POST", "/api/v1/auth/login", {"password": self._password})
        tok = r["body"].get("token") if isinstance(r["body"], dict) else None
        if r["status"] != 200 or not tok:
            raise RuntimeError(f"login failed: HTTP {r['status']} {r['body']}")
        self.token = tok
        return r

    def auth_status(self) -> dict:
        return self.call("GET", "/api/v1/auth/status")

    # -- jobs ------------------------------------------------------------
    def list_jobs(self, all_: bool = True, cwd: str | None = None) -> dict:
        # T08b: the global list needs ~30s on this store (cold index over the job
        # directory), so it must never use the default short timeout.
        q = f"/api/v1/jobs?all={1 if all_ else 0}"
        if cwd:
            q += "&cwd=" + urllib.parse.quote(cwd)
        return self.call("GET", q, timeout=180)

    def jobs(self, all_: bool = True) -> list[dict]:
        r = self.list_jobs(all_=all_)
        data = r["body"].get("data", {}) if isinstance(r["body"], dict) else {}
        return data.get("jobs", []) or []

    def dispatch(self, **body) -> dict:
        return self.call("POST", "/api/v1/jobs", body, timeout=60)

    def get_job(self, job_id: str, timeout: float = 30.0) -> dict:
        return self.call("GET", f"/api/v1/jobs/{job_id}", timeout=timeout)

    def job(self, job_id: str, timeout: float = 30.0) -> dict:
        r = self.get_job(job_id, timeout=timeout)
        data = r["body"].get("data", {}) if isinstance(r["body"], dict) else {}
        return data.get("job", data) or {}

    def stop_job(self, job_id: str) -> dict:
        return self.call("POST", f"/api/v1/jobs/{job_id}/stop", {}, timeout=60)

    def respawn_job(self, job_id: str) -> dict:
        return self.call("POST", f"/api/v1/jobs/{job_id}/respawn", {}, timeout=60)

    def delete_job(self, job_id: str) -> dict:
        return self.call("DELETE", f"/api/v1/jobs/{job_id}")

    def job_session(self, job_id: str) -> dict:
        return self.call("GET", f"/api/v1/jobs/{job_id}/session")

    def job_transcript(self, job_id: str, offset: int = 0, limit: int = 25) -> dict:
        return self.call("GET", f"/api/v1/jobs/{job_id}/transcript?offset={offset}&limit={limit}")

    def job_reply(self, job_id: str, text: str, bash: bool = False) -> dict:
        return self.call("POST", f"/api/v1/jobs/{job_id}/reply", {"text": text, "bash": bash})

    def resumable(self, cwd: str | None = None) -> dict:
        q = "/api/v1/jobs/resumable"
        if cwd:
            q += "?cwd=" + urllib.parse.quote(cwd)
        return self.call("GET", q)

    def resume_session(self, session_id: str, cwd: str | None = None) -> dict:
        q = "/api/v1/jobs/resume"
        if cwd:
            q += "?cwd=" + urllib.parse.quote(cwd)
        return self.call("POST", q, {"sessionId": session_id}, timeout=60)

    # -- meta ------------------------------------------------------------
    def info(self) -> dict:
        return self.call("GET", "/api/v1/info")

    def health(self) -> dict:
        return self.call("GET", "/api/v1/health")

    def openapi(self) -> dict:
        return self.call("GET", "/api/openapi.json")

    # -- SSE -------------------------------------------------------------
    def open_stream(self, path: str, timeout: float = 3600.0):
        headers = {"X-CodeBuddy-Request": "1", "Accept": "text/event-stream"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.base + path, method="GET", headers=headers)
        return urllib.request.urlopen(req, timeout=timeout)


def iter_sse_lines(resp, max_seconds: float | None = None) -> Iterable[tuple[float, str]]:
    """Yield (timestamp, line) from an open SSE response, tolerating NDJSON or SSE framing."""
    import socket

    t0 = time.time()
    while True:
        if max_seconds is not None and time.time() - t0 > max_seconds:
            return
        try:
            raw = resp.readline()
        except (socket.timeout, TimeoutError):
            return
        except Exception:
            return
        if not raw:
            return
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        yield time.time() - t0, line


def redact(obj: Any, keys: Iterable[str] = ("password", "token", "authorization", "cookie")) -> Any:
    lowered = {k.lower() for k in keys}
    if isinstance(obj, dict):
        return {k: ("<redacted>" if str(k).lower() in lowered else redact(v, keys))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v, keys) for v in obj]
    return obj


def write_evidence(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(redact(payload), ensure_ascii=False, indent=2), encoding="utf-8")
