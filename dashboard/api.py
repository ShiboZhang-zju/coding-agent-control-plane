"""HTTP surface for UI-0 and the explicitly enabled UI-1 chat.

Design constraints (tested by ``tests/test_ui0_dashboard.py`` and
``tests/test_ui1_web_chat.py``):

* **Read-only by default.** Without ``--enable-chat`` every write method is
  rejected and all dashboard projections use ``mode=ro`` plus
  ``PRAGMA query_only=ON``.  UI-1 adds exactly three authenticated POST routes
  (``/api/chat/messages``, ``/api/chat/publish`` and ``/api/chat/goal-resume``);
  all delegate to the existing ``LDChat`` service graph through request-scoped
  writable connections.  None of them can submit a WorkItem, approve work,
  change a budget or apply a state change without an explicit confirm turn.
* **A stopped goal is readable, not acted upon.** ``GET
  /api/projects/{id}/goals/{goal_id}/handling`` is a read-only projection:
  stop reason, linked WorkItems, Verifier results and evidence row ids.
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

import hmac
import json
import posixpath
import sqlite3
import sys
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from ld.chat_intent import ChatPolicyError, ChatValidationError

from .chat import ChatBridge, ChatBridgeError, ConversationProjectMismatch
from .ledger import (
    DashboardError,
    LedgerBusy,
    LedgerMissing,
    LedgerUnreadable,
    ProjectNotFound,
    ReadOnlyLedger,
    SchemaIncompatible,
)
from .projections import goal_handling, overview, project_detail

#: Loopback names/addresses the server may bind.  ``0.0.0.0`` is not here on
#: purpose: neither the read-only views nor operator chat belong on a LAN.
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

#: Hard caps on request body reads.  GET/HEAD never accept a body; the sole
#: chat POST accepts only a bounded JSON document.
_MAX_BODY = 64 * 1024
_MAX_CHAT_BODY = 64 * 1024


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

    def __init__(self, ledger_path: Path | str, *,
                 chat: ChatBridge | None = None):
        self.ledger = ReadOnlyLedger(ledger_path)
        self.chat = chat

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

        if path == "/api/chat/conversations":
            self._require_chat()
            query = parse_qs(parsed.query, keep_blank_values=True)
            unknown = set(query) - {"project_id", "limit"}
            if unknown or any(len(values) != 1 for values in query.values()):
                raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_query",
                               "Conversation query parameters are invalid.")
            project_id = (query.get("project_id") or [None])[0] or None
            try:
                limit = int((query.get("limit") or ["50"])[0])
                rows = self.chat.list_conversations(
                    project_id=project_id, limit=limit)
            except (KeyError, ValueError):
                raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_project",
                               "The requested project is not registered.") from None
            return HTTPStatus.OK, {
                "conversations": rows,
                "generated_at": _now(),
                "capabilities": self.chat.capabilities,
            }

        if path == "/api/chat/projects":
            # Authenticated: this is the publication-target list, and it carries
            # the repository root so the operator can see *where* a Goal would be
            # created.  It is not part of the read-only cockpit projections.
            self._require_chat()
            return HTTPStatus.OK, {"projects": self.chat.project_targets(),
                                   "generated_at": _now()}

        chat_prefix = "/api/chat/conversations/"
        if path.startswith(chat_prefix):
            self._require_chat()
            conversation_id = path[len(chat_prefix):].strip("/")
            if not conversation_id or "/" in conversation_id:
                raise ApiError(HTTPStatus.NOT_FOUND, "not_found",
                               "Unknown API path.")
            try:
                result = self.chat.get_conversation(conversation_id)
            except ValueError:
                raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_conversation",
                               "The conversation id is invalid.") from None
            if result is None:
                raise ApiError(HTTPStatus.NOT_FOUND, "conversation_not_found",
                               "No conversation with that id exists.")
            result["capabilities"] = self.chat.capabilities
            return HTTPStatus.OK, result

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
            if len(parts) == 4 and parts[1] == "goals" and parts[3] == "handling":
                return HTTPStatus.OK, self._read(
                    lambda session: self._goal_handling(
                        session, project_id, parts[2]))
            raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "Unknown API path.")

        if path.startswith("/api/"):
            # An unknown /api/* path is a client bug, not a missing asset:
            # answering with HTML/404 would hide it behind the static handler.
            raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "Unknown API path.")

        return 0, b""   # sentinel: not an API path (static handler decides)

    def handle_chat_message(self, payload: Any) -> dict[str, Any]:
        """Validate the chat write schema and call the real LDChat.

        Three fields, all still required to be *present*: the browser may not
        omit ``project_id`` and hope something is inferred.  It may send
        ``null``, which means "global LD": the LD resolves each turn's project
        from the sentence and asks when it cannot.  Sending a value keeps the
        historical UI-1 meaning.

        The wire contract stays a closed set of three keys, so a caller still
        cannot smuggle an ``intent``, a ``goal_id``, a budget or a command.
        """
        self._require_chat()
        if not isinstance(payload, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_body",
                           "The request body must be one JSON object.")
        required = {"conversation_id", "project_id", "message"}
        if set(payload) != required:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_fields",
                           "The chat request fields are invalid.")
        conversation_id = payload.get("conversation_id")
        project_id = payload.get("project_id")
        message = payload.get("message")
        if conversation_id is not None and not isinstance(conversation_id, str):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_conversation",
                           "conversation_id must be a string or null.")
        if project_id is not None and (not isinstance(project_id, str)
                                       or not project_id.strip()):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_project",
                           "project_id must be a registered project id or null "
                           "for the global LD.")
        if not isinstance(message, str) or not message.strip():
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_message",
                           "message must be a non-empty string.")
        try:
            result = self.chat.handle(
                message=message, conversation_id=conversation_id,
                project_id=project_id)
        except ConversationProjectMismatch:
            raise ApiError(HTTPStatus.CONFLICT, "conversation_project_mismatch",
                           "This conversation belongs to another project.") from None
        except KeyError:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_reference",
                           "The project or conversation does not exist.") from None
        except (ChatPolicyError, ChatValidationError, ValueError):
            raise ApiError(HTTPStatus.BAD_REQUEST, "chat_request_rejected",
                           "LDChat rejected this request under its safety policy.") from None
        except (ChatBridgeError, sqlite3.Error):
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "chat_unavailable",
                           "The chat service cannot access the ledger right now.") from None
        return {**result, "capabilities": self.chat.capabilities}

    def handle_publish_request(self, payload: Any) -> dict[str, Any]:
        """Validate the console publication schema and call ``LDChat``.

        Two fields only, and neither names an intent, a Goal id, a budget, an
        assertion or a command: the browser cannot choose *how* the work is
        dispatched, only *what* it wants done.  Framing the goal and forwarding
        it to a Worker remain the LD's job.
        """
        self._require_chat()
        if not isinstance(payload, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_body",
                           "The request body must be one JSON object.")
        required = {"project_id", "objective"}
        if set(payload) != required:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_fields",
                           "The publish request fields are invalid.")
        project_id = payload.get("project_id")
        objective = payload.get("objective")
        if not isinstance(project_id, str) or not project_id.strip():
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_project",
                           "project_id must name a registered project.")
        if not isinstance(objective, str) or not objective.strip():
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_objective",
                           "objective must be a non-empty string.")
        try:
            return self.chat.publish_task(project_id=project_id,
                                          objective=objective)
        except KeyError:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_reference",
                           "The project does not exist.") from None
        except (ChatPolicyError, ChatValidationError, ValueError):
            raise ApiError(HTTPStatus.BAD_REQUEST, "publish_request_rejected",
                           "LDChat rejected this request under its safety policy.") from None
        except (ChatBridgeError, sqlite3.Error):
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "chat_unavailable",
                           "The chat service cannot access the ledger right now.") from None

    def handle_goal_resume_request(self, payload: Any) -> dict[str, Any]:
        """Stage a resume of one named goal; the human still has to confirm it.

        Two fields only: which project and which goal.  There is deliberately no
        field for a budget, a task, an assertion or a "force" flag — those are not
        things this console may decide, and the resume itself is applied by the
        existing chat confirmation path, not here.
        """
        self._require_chat()
        if not isinstance(payload, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_body",
                           "The request body must be one JSON object.")
        required = {"project_id", "goal_id"}
        if set(payload) != required:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_fields",
                           "The goal resume request fields are invalid.")
        project_id = payload.get("project_id")
        goal_id = payload.get("goal_id")
        if not isinstance(project_id, str) or not project_id.strip():
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_project",
                           "project_id must name a registered project.")
        if not isinstance(goal_id, str) or not goal_id.strip():
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_goal",
                           "goal_id must name a goal of that project.")
        try:
            return self.chat.stage_goal_resume(project_id=project_id,
                                               goal_id=goal_id)
        except KeyError:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_reference",
                           "The project does not exist.") from None
        except (ChatPolicyError, ChatValidationError, ValueError):
            raise ApiError(HTTPStatus.BAD_REQUEST, "resume_request_rejected",
                           "LDChat rejected this resume under its safety policy."
                           ) from None
        except (ChatBridgeError, sqlite3.Error):
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "chat_unavailable",
                           "The chat service cannot access the ledger right now."
                           ) from None

    def _require_chat(self) -> None:
        if self.chat is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "chat_disabled",
                           "Interactive chat is not enabled on this server.")

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

    @staticmethod
    def _goal_handling(session, project_id: str, goal_id: str) -> dict[str, Any]:
        """Read one stopped goal's handling view (read-only projection)."""
        detail = project_detail(session, project_id)
        goal_row = next((row for row in session.goal_rows_for(project_id)
                         if row.get("goal_id") == goal_id), None)
        if goal_row is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "goal_not_found",
                           "No goal with that id exists in this project.")
        return {
            "generated_at": detail["generated_at"],
            **goal_handling(session, project_id, goal_id, detail["tasks"],
                            project_name=detail["project"]["name"],
                            goal=goal_row),
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
            "read_only": self.chat is None,
            "ledger_views_read_only": True,
            "chat_enabled": self.chat is not None,
            "server_time": _now(),
            "ledger": {"path_configured": True},
        }
        if self.chat is not None:
            body["chat"] = {
                "enabled": True,
                "authentication": "bearer_token",
                # The console can publish an objective, but "published" only
                # means "a Goal row exists".  Forwarding to a Worker, and every
                # verdict, still comes from the LD/Runner/Verifier, so this flag
                # must never be read as "work will start".
                "task_publish_enabled": True,
                "task_publish_creates": "goal_only_no_work_item",
                **self.chat.capabilities,
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

    server_version = "ProjectDashboard/1.0"
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

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch_write()

    def _reject_write(self) -> None:
        self._send_error(ApiError(HTTPStatus.METHOD_NOT_ALLOWED,
                                  "method_not_allowed",
                                  "That write method is not available. UI-1 "
                                  "only permits its authenticated chat POST."))

    do_PUT = do_PATCH = do_DELETE = _reject_write  # noqa: N815
    do_OPTIONS = do_TRACE = _reject_write  # noqa: N815

    # -- request pipeline ------------------------------------------------ #
    def _dispatch(self, method: str) -> None:
        try:
            self._check_host()
            length = int(self.headers.get("Content-Length") or 0)
            if length > _MAX_BODY:
                raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                               "body_too_large", "Request body is not accepted.")
            if unquote(urlparse(self.path).path).startswith("/api/chat/"):
                self._authorize_chat()
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

    #: The only two write routes UI-1 has, and the validator each one uses.  A
    #: path absent from this table can never be written to, whatever the caller
    #: sends, so adding a route is a deliberate edit here rather than a routing
    #: side effect.
    _WRITE_ROUTES = {
        "/api/chat/messages": "handle_chat_message",
        "/api/chat/publish": "handle_publish_request",
        "/api/chat/goal-resume": "handle_goal_resume_request",
    }

    def _dispatch_write(self) -> None:
        try:
            self._check_host()
            path = unquote(urlparse(self.path).path or "/")
            handler_name = self._WRITE_ROUTES.get(path)
            if self.app.chat is None or handler_name is None:
                raise ApiError(
                    HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed",
                    "This server accepts no write at that path.")
            self._authorize_chat()
            self._check_same_origin()
            payload = self._read_json_body()
            result = getattr(self.app, handler_name)(payload)
            if handler_name == "handle_chat_message":
                self._audit_chat(result, payload.get("project_id"))
            elif handler_name == "handle_publish_request":
                self._audit_publish(result, payload.get("project_id"))
            else:
                self._audit_goal_resume(result, payload.get("project_id"))
            self._send_json(HTTPStatus.OK, result, head_only=False)
        except ApiError as exc:
            self._send_error(exc)
        except Exception:  # noqa: BLE001 - never leak details to the browser
            traceback.print_exc(file=sys.stderr)
            self._send_error(ApiError(
                HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error",
                "The dashboard failed to handle this request."))

    def _read_json_body(self) -> Any:
        """One bounded JSON document, or a stable error.  Shared by both routes."""
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0]
        if content_type.strip().lower() != "application/json":
            raise ApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                           "invalid_content_type",
                           "Chat requests must use application/json.")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ApiError(HTTPStatus.LENGTH_REQUIRED, "length_required",
                           "Chat requests require Content-Length.")
        try:
            length = int(raw_length)
        except ValueError:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_length",
                           "Content-Length is invalid.") from None
        if length < 1:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_body",
                           "The request body is empty.")
        if length > _MAX_CHAT_BODY:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                           "body_too_large", "The chat request is too large.")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_json",
                           "The request body is not valid UTF-8 JSON.") from None

    def _authorize_chat(self) -> None:
        token = getattr(self.server, "chat_token", None)
        if not token:
            raise ApiError(HTTPStatus.METHOD_NOT_ALLOWED, "chat_disabled",
                           "Interactive chat is not enabled on this server.")
        header = self.headers.get("Authorization") or ""
        prefix = "Bearer "
        supplied = header[len(prefix):] if header.startswith(prefix) else ""
        if not supplied or not hmac.compare_digest(supplied, token):
            raise ApiError(HTTPStatus.UNAUTHORIZED, "unauthorized",
                           "A valid local operator token is required.")

    def _check_same_origin(self) -> None:
        origin = (self.headers.get("Origin") or "").strip().lower().rstrip("/")
        host = (self.headers.get("Host") or "").strip().lower()
        if not origin or origin != "http://" + host:
            raise ApiError(HTTPStatus.FORBIDDEN, "bad_origin",
                           "Chat writes require this dashboard's exact Origin.")

    @staticmethod
    def _audit_chat(result: dict[str, Any], project_id: str | None) -> None:
        intent = result.get("intent") or {}
        routing = result.get("project_routing") or {}
        # The *effective* project of the turn, not the request field: in a
        # global conversation the field is null and the LD resolved the project,
        # so auditing only the field would lose which project was discussed.
        # ``routing_source`` says on what basis it was resolved.
        # No message text, answer text, token, endpoint, key or arbitrary facts.
        event = {
            "event": "chat_turn",
            "project_id": routing.get("project_id") or None,
            "requested_project_id": project_id,
            "routing_source": routing.get("source"),
            "routing_candidate_count": len(routing.get("candidates") or []),
            "conversation_id": result.get("conversation_id"),
            "intent": intent.get("intent"),
            "intent_source": result.get("intent_source"),
            "goal_count": len(result.get("goal_ids") or []),
            "requires_confirmation": bool(result.get("requires_confirmation")),
        }
        sys.stderr.write("[dashboard.audit] " + json.dumps(
            event, ensure_ascii=False, separators=(",", ":")) + "\n")

    @staticmethod
    def _audit_publish(result: dict[str, Any], project_id: str | None) -> None:
        """Publication audit line: routing facts only, never the objective text.

        The goal objective is the owner's own sentence and lives in the ledger;
        copying it into a log would create a second copy outside the ledger's
        access rules for no diagnostic benefit.
        """
        event = {
            "event": "task_published",
            "project_id": project_id,
            "goal_id": result.get("goal_id"),
            "goal_created": bool(result.get("created")),
            "goal_reused": bool(result.get("reused")),
            "goal_status": result.get("status"),
            "next_step": result.get("next_step"),
            "goal_chars": result.get("goal_chars"),
        }
        sys.stderr.write("[dashboard.audit] " + json.dumps(
            event, ensure_ascii=False, separators=(",", ":")) + "\n")

    @staticmethod
    def _audit_goal_resume(result: dict[str, Any], project_id: str | None) -> None:
        """Staging audit line.  ``staged`` means "waiting for confirm", not "resumed"."""
        pending = result.get("pending_action") or {}
        event = {
            "event": "goal_resume_staged",
            "project_id": project_id,
            "goal_id": pending.get("goal_id") or (result.get("goal_ids") or [None])[0],
            "conversation_id": result.get("conversation_id"),
            "requires_confirmation": bool(result.get("requires_confirmation")),
            "resumed": False,
        }
        sys.stderr.write("[dashboard.audit] " + json.dumps(
            event, ensure_ascii=False, separators=(",", ":")) + "\n")

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
                 allowed_hosts: frozenset[str] = _ALLOWED_HOST_NAMES,
                 chat_token: str | None = None):
        self.app = app
        self.chat_token = chat_token
        handler = type("BoundDashboardHandler", (DashboardHandler,),
                       {"app": app, "allowed_hosts": allowed_hosts})
        super().__init__(address, handler)


def validate_host(host: str) -> str:
    """Refuse anything that is not loopback.  Raises ``ValueError``."""
    if host not in LOOPBACK_HOSTS:
        raise ValueError(
            f"refusing to bind {host!r}: the dashboard is local-only and may "
            f"listen only on {sorted(LOOPBACK_HOSTS)}")
    return host


def make_server(ledger_path: Path | str, *, host: str = "127.0.0.1",
                port: int = 8765, enable_chat: bool = False,
                chat_token: str | None = None,
                chat_bridge: ChatBridge | None = None,
                model_base_url: str | None = None,
                model_name: str | None = None,
                model_api_key: str | None = None,
                model_timeout_s: float | None = None,
                model_domains: str | None = None) -> DashboardServer:
    validate_host(host)
    if enable_chat:
        if not isinstance(chat_token, str) or len(chat_token) < 20:
            raise ValueError(
                "--enable-chat requires a local operator token of at least "
                "20 characters")
        bridge = chat_bridge or ChatBridge(
            ledger_path, model_base_url=model_base_url, model_name=model_name,
            model_api_key=model_api_key, model_timeout_s=model_timeout_s,
            model_domains=model_domains)
        bridge.prepare()
    else:
        bridge = None
        chat_token = None
    app = DashboardApp(ledger_path, chat=bridge)
    return DashboardServer((host, int(port)), app, chat_token=chat_token)


__all__ = ["ApiError", "DashboardApp", "DashboardHandler", "DashboardServer",
           "LOOPBACK_HOSTS", "make_server", "validate_host"]
