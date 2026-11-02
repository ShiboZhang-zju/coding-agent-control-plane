"""HTTP surface for the UI-0 dashboard: GET/HEAD only, loopback only.

Design constraints (all tested, see ``tests/test_ui0_dashboard.py``):

* **Read-only by construction.** There is no handler that mutates anything, and
  the only database handle the process can obtain is a ``mode=ro`` connection
  with ``PRAGMA query_only=ON``.  Even if a route were added by mistake, the
  connection refuses the write.
* **No cross-origin use.** No ``Access-Control-Allow-Origin`` is ever emitted.
  A browser on another origin therefore cannot read responses, and the
  ``Host`` header is validated against an allow-list so a DNS-rebinding page
  cannot reach the server through a name it happens to resolve.
* **No local detail leaks.** Error bodies are stable codes with a short,
  path-free message; exception text (which can embed absolute paths) is
  logged to the console, never sent to the browser.
* **Static files cannot escape ``static/``.** Every request path is resolved
  and re-checked against the static root, and only a fixed extension/type map
  is served.

The server binds ``127.0.0.1`` by default. Binding any other address is
refused outright rather than merely discouraged.
"""
from __future__ import annotations

import json
import posixpath
import sys
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .ledger import (
    DashboardError,
    LedgerBusy,
    LedgerMissing,
    LedgerUnreadable,
    ProjectNotFound,
    ReadOnlyLedger,
    SchemaIncompatible,
)
from .projections import overview, project_detail

#: Loopback names/addresses the server may bind.  ``0.0.0.0`` is not here on
#: purpose: this dashboard has no authentication and must not be on a LAN.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

#: Loopback spellings accepted in the ``Host`` header (port optional).
_ALLOWED_HOST_NAMES = frozenset({"127.0.0.1", "localhost", "[::1]"})

STATIC_ROOT = Path(__file__).resolve().parent / "static"

#: Explicit content types.  ``mimetypes`` is used as a fallback but only after
#: the suffix has passed this map, so an unexpected file type is a 404, not a
#: sniffed ``application/octet-stream`` download.
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}

#: Hard cap on request body reads; no route accepts a body, so anything large
#: is closed instead of buffered.
_MAX_BODY = 64 * 1024


class ApiError(Exception):
    """An error with an HTTP status and a machine-readable code."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _classify(exc: DashboardError) -> ApiError:
    """Map a dashboard/ledger failure to a documented API error.

    The message is always ours, never ``str(exc)``: SQLite errors contain the
    absolute ledger path, and an operator console knows the path already.
    """
    if isinstance(exc, LedgerMissing):
        return ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "ledger_missing",
                        "The configured ledger file does not exist. "
                        "The dashboard never creates one.")
    if isinstance(exc, SchemaIncompatible):
        return ApiError(HTTPStatus.CONFLICT, "schema_incompatible",
                        "The ledger exists but is missing tables this dashboard "
                        "needs. It was probably written by a different build.")
    if isinstance(exc, LedgerBusy):
        return ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "ledger_busy",
                        "The ledger is locked by a writer right now; retry shortly.")
    if isinstance(exc, LedgerUnreadable):
        return ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "ledger_unreadable",
                        "The ledger could not be read. It may be corrupt, "
                        "permission-denied, or not a SQLite database.")
    if isinstance(exc, ProjectNotFound):
        return ApiError(HTTPStatus.NOT_FOUND, "project_not_found",
                        "No project with that id exists in this ledger.")
    return ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error",
                    "The dashboard failed to build this view.")


class DashboardApp:
    """Route table + projections.  Transport-independent and unit testable."""

    def __init__(self, ledger_path: Path | str):
        self.ledger = ReadOnlyLedger(ledger_path)

    # -- routes ---------------------------------------------------------- #
    def handle(self, method: str, raw_path: str) -> tuple[int, dict[str, Any] | bytes]:
        """Return ``(status, payload)``; ``payload`` is JSON-able or raw bytes.

        Raising :class:`ApiError` is the only failure mode the transport sees.
        """
        parsed = urlparse(raw_path)
        path = unquote(parsed.path or "/")

        if method not in ("GET", "HEAD"):
            raise ApiError(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed",
                           "UI-0 is strictly read-only; only GET and HEAD are served.")

        if path == "/api/health":
            return HTTPStatus.OK, self._health()
        if path == "/api/overview":
            return HTTPStatus.OK, self._read(overview)
        if path == "/api/projects":
            data = self._read(lambda session: {"projects": overview(session)["projects"],
                                               "generated_at": _now()})
            return HTTPStatus.OK, data

        prefix = "/api/projects/"
        if path.startswith(prefix):
            rest = path[len(prefix):].strip("/")
            parts = [part for part in rest.split("/") if part]
            if not parts:
                raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "Unknown API path.")
            project_id = parts[0]
            if len(parts) == 1:
                return HTTPStatus.OK, self._read(
                    lambda session: project_detail(session, project_id))
            if len(parts) == 2:
                section = parts[1]
                return HTTPStatus.OK, self._read(
                    lambda session: self._section(session, project_id, section))
            raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "Unknown API path.")

        if path.startswith("/api/"):
            # An unknown /api/* path is a client bug, not a missing asset:
            # answering with HTML/404 would hide it behind the static handler.
            raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "Unknown API path.")

        return 0, b""   # sentinel: not an API path (static handler decides)

    # -- sections -------------------------------------------------------- #
    @staticmethod
    def _section(session, project_id: str, section: str) -> dict[str, Any]:
        detail = project_detail(session, project_id)
        mapping = {
            "goals": "goals",
            "tasks": "tasks",
            "sessions": "sessions",
            "memory-summary": "memory",
        }
        if section not in mapping:
            raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "Unknown API path.")
        key = mapping[section]
        return {
            "project_id": project_id,
            "generated_at": detail["generated_at"],
            key: detail[key],
            # Counts travel with the section so a filtered view never has to
            # guess the denominator from a truncated list.
            "task_counts": detail["task_counts"],
            "goal_status_counts": detail["goal_status_counts"],
        }

    # -- helpers --------------------------------------------------------- #
    def _read(self, projection) -> dict[str, Any]:
        try:
            with self.ledger.session() as session:
                return projection(session)
        except DashboardError as exc:
            raise _classify(exc) from None

    def _health(self) -> dict[str, Any]:
        """Liveness + ledger reachability, without failing the request.

        ``status`` is ``ok`` only when the ledger answered.  A missing or
        unreadable ledger is reported *in* the body with HTTP 200 so the UI can
        show "API reachable, ledger broken" instead of only "connection lost".
        """
        body: dict[str, Any] = {
            "status": "ok",
            "read_only": True,
            "server_time": _now(),
            "ledger": {"path_configured": True},
        }
        try:
            with self.ledger.session() as session:
                body["ledger"].update({
                    "reachable": True,
                    "has_goals_table": session.has_goals_table(),
                    "projects": len(session.store.all_projects()),
                })
        except DashboardError as exc:
            api_error = _classify(exc)
            body["status"] = "degraded"
            body["ledger"].update({"reachable": False,
                                   "error_code": api_error.code,
                                   "error": api_error.message})
        return body


def _now() -> float:
    return time.time()


# --------------------------------------------------------------------------- #
# HTTP transport
# --------------------------------------------------------------------------- #
class DashboardHandler(BaseHTTPRequestHandler):
    """Serves the API above plus the three static assets."""

    server_version = "UI0Dashboard/1.0"
    #: ``dashboard`` is injected by :func:`make_server`.
    app: DashboardApp
    allowed_hosts: frozenset[str] = _ALLOWED_HOST_NAMES

    # -- logging --------------------------------------------------------- #
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
        sys.stderr.write("[dashboard] %s - %s\n"
                         % (self.address_string(), fmt % args))

    # -- verb gate ------------------------------------------------------- #
    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("HEAD")

    def _reject_write(self) -> None:
        self._send_error(ApiError(HTTPStatus.METHOD_NOT_ALLOWED,
                                  "method_not_allowed",
                                  "UI-0 is strictly read-only; only GET and HEAD "
                                  "are served."))

    do_POST = do_PUT = do_PATCH = do_DELETE = _reject_write  # noqa: N815
    do_OPTIONS = do_TRACE = _reject_write  # noqa: N815

    # -- request pipeline ------------------------------------------------ #
    def _dispatch(self, method: str) -> None:
        try:
            self._check_host()
            length = int(self.headers.get("Content-Length") or 0)
            if length > _MAX_BODY:
                raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                               "body_too_large", "Request body is not accepted.")
            status, payload = self.app.handle(method, self.path)
            if isinstance(payload, bytes) and status == 0:
                self._serve_static(method, self.path)
                return
            self._send_json(status, payload, head_only=(method == "HEAD"))
        except ApiError as exc:
            self._send_error(exc, head_only=(method == "HEAD"))
        except Exception:  # noqa: BLE001 - never leak a stack trace to a browser
            traceback.print_exc(file=sys.stderr)
            self._send_error(ApiError(HTTPStatus.INTERNAL_SERVER_ERROR,
                                      "internal_error",
                                      "The dashboard failed to handle this request."),
                             head_only=(method == "HEAD"))

    def _check_host(self) -> None:
        """Reject requests whose ``Host`` is not a loopback spelling.

        Why it matters: binding to 127.0.0.1 does not stop a page on
        ``evil.example`` from resolving a hostname to 127.0.0.1 and issuing
        same-origin requests (DNS rebinding).  Checking ``Host`` — combined
        with never sending CORS headers — closes that.
        """
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_host",
                           "Missing Host header.")
        name = host.rsplit(":", 1)[0] if not host.startswith("[") else \
            host.split("]")[0] + "]"
        if name not in self.allowed_hosts:
            raise ApiError(HTTPStatus.FORBIDDEN, "bad_host",
                           "This dashboard only accepts loopback Host headers.")

    # -- responses ------------------------------------------------------- #
    def _common_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # No CDN, no third-party anything: the page may only load its own
        # same-origin assets.  This is also the XSS backstop.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.send_header("X-Frame-Options", "DENY")
        # Deliberately absent: Access-Control-Allow-Origin.  Cross-origin
        # reads are impossible, by omission.

    def _send_json(self, status: int, payload: Any, *, head_only: bool) -> None:
        body = json.dumps(payload, ensure_ascii=False,
                          default=str).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._common_headers()
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _send_error(self, exc: ApiError, *, head_only: bool = False) -> None:
        body = json.dumps({"error": {"code": exc.code, "message": exc.message}},
                          ensure_ascii=False).encode("utf-8")
        self.send_response(int(exc.status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._common_headers()
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    # -- static ---------------------------------------------------------- #
    def _serve_static(self, method: str, raw_path: str) -> None:
        try:
            target = self._resolve_static(unquote(urlparse(raw_path).path or "/"))
        except ApiError as exc:
            self._send_error(exc, head_only=(method == "HEAD"))
            return
        if target is None:
            self._send_error(ApiError(HTTPStatus.NOT_FOUND, "not_found",
                                      "Not found."), head_only=(method == "HEAD"))
            return
        try:
            data = target.read_bytes()
        except OSError:
            self._send_error(ApiError(HTTPStatus.INTERNAL_SERVER_ERROR,
                                      "internal_error", "Cannot read asset."),
                             head_only=(method == "HEAD"))
            return
        content_type = _CONTENT_TYPES[target.suffix.lower()]
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self._common_headers()
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(data)

    @staticmethod
    def _resolve_static(path: str) -> Path | None:
        """Map a URL path onto ``static/`` without ever escaping it.

        Traversal defence is two independent steps: the path is normalised as a
        POSIX path (``..`` collapses there), and the resolved absolute target
        must still be inside the resolved static root.  Symlinks are resolved,
        so a link planted in ``static/`` cannot point outside either.
        """
        if "\x00" in path:
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_path", "Invalid path.")
        relative = "index.html" if path in ("", "/") else path.lstrip("/")
        # Reject raw '..' segments before normalisation: they only ever appear
        # in an attempt to escape, and normalising first would hide that.
        if any(part == ".." for part in relative.split("/")):
            raise ApiError(HTTPStatus.FORBIDDEN, "path_traversal",
                           "Path traversal is not allowed.")
        normalised = posixpath.normpath(relative)
        if normalised.startswith("../") or normalised == "..":
            raise ApiError(HTTPStatus.FORBIDDEN, "path_traversal",
                           "Path traversal is not allowed.")
        candidate = (STATIC_ROOT / normalised)
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        root = STATIC_ROOT.resolve()
        if root != resolved and root not in resolved.parents:
            raise ApiError(HTTPStatus.FORBIDDEN, "path_traversal",
                           "Path traversal is not allowed.")
        if not resolved.is_file():
            return None
        if resolved.suffix.lower() not in _CONTENT_TYPES:
            return None
        return resolved


class DashboardServer(ThreadingHTTPServer):
    """Threaded server; ``daemon_threads`` so shutdown is immediate."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], app: DashboardApp,
                 allowed_hosts: frozenset[str] = _ALLOWED_HOST_NAMES):
        self.app = app
        handler = type("BoundDashboardHandler", (DashboardHandler,),
                       {"app": app, "allowed_hosts": allowed_hosts})
        super().__init__(address, handler)


def validate_host(host: str) -> str:
    """Refuse anything that is not loopback.  Raises ``ValueError``."""
    if host not in LOOPBACK_HOSTS:
        raise ValueError(
            f"refusing to bind {host!r}: the UI-0 dashboard is read-only and has "
            f"no authentication, so it may only listen on {sorted(LOOPBACK_HOSTS)}")
    return host


def make_server(ledger_path: Path | str, *, host: str = "127.0.0.1",
                port: int = 8765) -> DashboardServer:
    validate_host(host)
    app = DashboardApp(ledger_path)
    return DashboardServer((host, int(port)), app)


__all__ = ["ApiError", "DashboardApp", "DashboardHandler", "DashboardServer",
           "LOOPBACK_HOSTS", "make_server", "validate_host"]
