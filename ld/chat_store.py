"""Durable conversation transcript for the conversational LD (M4-D).

Why a separate store
--------------------
Three different things must never be confused:

* **Conversation memory** (this module) — "what were we just talking about".
  Disposable, per-conversation, answers no question about project facts.
* **Project memory** (``controlplane.project_context``) — "what the project has
  verified about itself".  Never written from a chat message.
* **Ledger** (``controlplane.store``) — "what actually ran and how it was
  judged".  The only source of task truth; a chat turn can only *read* it.

So this module stores only text, the validated intent that produced an answer,
and the *snapshot of facts* that answer was built from.  It contains no task
state, no verdict and no knowledge entry, which is what makes it safe for the
LD to re-read an old conversation without ever mistaking it for current
progress.

Tables live in the same SQLite ledger file as everything else (no new
infrastructure), but nothing here is authoritative for anything.
"""
from __future__ import annotations

import contextlib
import json
import re
import sqlite3
import threading
import time
import uuid
from typing import Any

#: Roles a stored message may have.  ``ld`` is the assistant side; ``system``
#: is reserved for deterministic notices (e.g. a refused action).
ROLES = ("user", "ld", "system")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ld_conversations (
    conversation_id TEXT PRIMARY KEY,
    project_id TEXT,
    title TEXT NOT NULL DEFAULT '',
    pending_action TEXT,
    message_count INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ld_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    intent TEXT,
    facts TEXT,
    goal_ids TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    UNIQUE(conversation_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_ld_messages_conversation
    ON ld_messages(conversation_id, seq);
"""

_MIGRATIONS = {"ld_conversations": {"pending_action": "TEXT",
                                    "message_count": "INTEGER NOT NULL DEFAULT 0"}}

#: Deterministic confirmation vocabulary.  A confirmation is *never* inferred
#: from a model output or from free-form agreement: the whole normalized
#: message must equal one of these tokens.
CONFIRM_TOKENS = frozenset({
    "confirm", "confirmed", "yes", "y", "ok", "approve", "do it",
    "确认", "确定", "是", "同意", "批准", "执行",
})
DENY_TOKENS = frozenset({
    "cancel", "no", "n", "deny", "stop", "abort",
    "取消", "否", "不", "拒绝", "算了",
})

_PUNCT_RE = re.compile(r"^[\s\"'`.,;:!?。，；：！？、]+|[\s\"'`.,;:!?。，；：！？、]+$")


def normalize_confirmation(text: str) -> str | None:
    """Return ``"confirm"`` / ``"deny"`` for an exact-token message, else None.

    Anything that is not *only* a confirmation token is a new request, so
    "yes, and also deploy to prod" is not an approval.
    """
    cleaned = _PUNCT_RE.sub("", (text or "").strip().lower())
    if cleaned in CONFIRM_TOKENS:
        return "confirm"
    if cleaned in DENY_TOKENS:
        return "deny"
    return None


class ChatStoreError(RuntimeError):
    """Conversation persistence is inconsistent with the requested operation."""


class LDChatStore:
    """Conversations + messages in the shared ledger file."""

    def __init__(self, ledger_path):
        self.path = str(ledger_path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(ledger_path), timeout=15,
                                     check_same_thread=False,
                                     isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=15000")
        self._conn.executescript(_SCHEMA)
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _migrate(self) -> None:
        for table, wanted in _MIGRATIONS.items():
            have = {row["name"] for row in
                    self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for column, sql_type in wanted.items():
                if column not in have:
                    self._conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")

    @contextlib.contextmanager
    def _txn(self):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    # ------------------------------------------------------------------ #
    # conversations
    # ------------------------------------------------------------------ #
    def create_conversation(self, *, project_id: str | None = None,
                            title: str = "") -> dict:
        conversation_id = "conv-" + uuid.uuid4().hex[:12]
        now = time.time()
        with self._txn() as conn:
            conn.execute(
                "INSERT INTO ld_conversations (conversation_id, project_id, title,"
                " pending_action, message_count, created_at, updated_at)"
                " VALUES (?,?,?,NULL,0,?,?)",
                (conversation_id, project_id, (title or "")[:200], now, now))
        return self.get_conversation(conversation_id) or {}

    def get_conversation(self, conversation_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM ld_conversations WHERE conversation_id=?",
                (conversation_id,)).fetchone()
        return self._row_to_conversation(row) if row else None

    def ensure_conversation(self, conversation_id: str | None, *,
                            project_id: str | None = None) -> dict:
        """Load an existing conversation, or start one."""
        if conversation_id:
            existing = self.get_conversation(conversation_id)
            if existing is None:
                raise KeyError(f"no conversation {conversation_id}")
            return existing
        return self.create_conversation(project_id=project_id)

    def list_conversations(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM ld_conversations ORDER BY updated_at DESC LIMIT ?",
                (int(limit),)).fetchall()
        return [self._row_to_conversation(row) for row in rows]

    def set_project(self, conversation_id: str, project_id: str | None) -> dict:
        with self._txn() as conn:
            conn.execute(
                "UPDATE ld_conversations SET project_id=?, updated_at=? "
                "WHERE conversation_id=?", (project_id, time.time(), conversation_id))
        return self.get_conversation(conversation_id) or {}

    def set_pending_action(self, conversation_id: str,
                           pending: dict | None) -> dict:
        encoded = None if pending is None else json.dumps(
            pending, ensure_ascii=False, default=str)
        with self._txn() as conn:
            conn.execute(
                "UPDATE ld_conversations SET pending_action=?, updated_at=? "
                "WHERE conversation_id=?", (encoded, time.time(), conversation_id))
        return self.get_conversation(conversation_id) or {}

    # ------------------------------------------------------------------ #
    # messages
    # ------------------------------------------------------------------ #
    def append_message(self, conversation_id: str, *, role: str, content: str,
                       intent: dict | None = None, facts: dict | None = None,
                       goal_ids: list[str] | None = None) -> dict:
        if role not in ROLES:
            raise ChatStoreError(f"unknown message role {role!r}")
        if not isinstance(content, str) or not content.strip():
            raise ChatStoreError("message content must be a non-empty string")
        if self.get_conversation(conversation_id) is None:
            raise KeyError(f"no conversation {conversation_id}")
        now = time.time()
        with self._txn() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS last FROM ld_messages "
                "WHERE conversation_id=?", (conversation_id,)).fetchone()
            seq = int(row["last"]) + 1
            conn.execute(
                "INSERT INTO ld_messages (conversation_id, seq, role, content, intent,"
                " facts, goal_ids, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (conversation_id, seq, role, content,
                 None if intent is None else json.dumps(
                     intent, ensure_ascii=False, default=str),
                 None if facts is None else json.dumps(
                     facts, ensure_ascii=False, default=str),
                 json.dumps(list(goal_ids or []), ensure_ascii=False), now))
            conn.execute(
                "UPDATE ld_conversations SET message_count=message_count+1,"
                " updated_at=? WHERE conversation_id=?", (now, conversation_id))
        return self.get_message(conversation_id, seq) or {}

    def get_message(self, conversation_id: str, seq: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM ld_messages WHERE conversation_id=? AND seq=?",
                (conversation_id, int(seq))).fetchone()
        return self._row_to_message(row) if row else None

    def messages(self, conversation_id: str, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM ld_messages WHERE conversation_id=? "
                "ORDER BY seq ASC LIMIT ?", (conversation_id, int(limit))).fetchall()
        return [self._row_to_message(row) for row in rows]

    def recent_turns(self, conversation_id: str, limit: int = 6) -> list[dict]:
        """The last ``limit`` messages, oldest first (bounded model context)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM ld_messages WHERE conversation_id=? "
                "ORDER BY seq DESC LIMIT ?",
                (conversation_id, int(limit))).fetchall()
        return [self._row_to_message(row) for row in reversed(rows)]

    # ------------------------------------------------------------------ #
    @staticmethod
    def _load(raw: Any, default: Any = None) -> Any:
        if raw is None or raw == "":
            return default
        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return default

    @classmethod
    def _row_to_conversation(cls, row: sqlite3.Row) -> dict:
        item = dict(row)
        item["pending_action"] = cls._load(item.get("pending_action"))
        return item

    @classmethod
    def _row_to_message(cls, row: sqlite3.Row) -> dict:
        item = dict(row)
        item["intent"] = cls._load(item.get("intent"))
        item["facts"] = cls._load(item.get("facts"))
        item["goal_ids"] = cls._load(item.get("goal_ids"), [])
        return item


__all__ = ["CONFIRM_TOKENS", "DENY_TOKENS", "ChatStoreError", "LDChatStore",
           "ROLES", "normalize_confirmation"]
