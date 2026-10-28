"""Durable control-plane task store.

This is the piece that makes *workflow* recovery possible when the worker's own
execution cannot be recovered: the plane keeps its own record of what was asked,
what the worker id was, and what was verified.

Stored under `.poc-state/tasks.json` (git-ignored) so a test-process restart can
reload it.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(__file__).resolve().parent.parent / ".poc-state" / "tasks.json"


class TaskStore:
    def __init__(self, path: Path | str = DEFAULT_PATH):
        self.path = Path(path)
        self.records: dict[str, dict] = {}
        self.load()

    # -- persistence -----------------------------------------------------
    def load(self) -> None:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.records = data.get("records", {})
            except Exception:
                self.records = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "saved_at": time.time(), "records": self.records}
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    # -- records ---------------------------------------------------------
    def new_task_id(self) -> str:
        return "cp-" + uuid.uuid4().hex[:12]

    def create(self, record: dict) -> dict:
        task_id = record["task_id"]
        record.setdefault("created_at", time.time())
        record["updated_at"] = time.time()
        self.records[task_id] = record
        self.save()
        return record

    def update(self, task_id: str, **patch: Any) -> dict:
        rec = self.records.setdefault(task_id, {"task_id": task_id})
        rec.update(patch)
        rec["updated_at"] = time.time()
        self.save()
        return rec

    def get(self, task_id: str) -> dict | None:
        return self.records.get(task_id)

    def by_job(self, worker_job_id: str) -> dict | None:
        for rec in self.records.values():
            if rec.get("worker_job_id") == worker_job_id:
                return rec
        return None

    def by_idempotency_key(self, key: str) -> dict | None:
        if not key:
            return None
        for rec in self.records.values():
            if rec.get("idempotency_key") == key:
                return rec
        return None

    def all(self) -> list[dict]:
        return list(self.records.values())
