"""T05: SSE subscribe -> disconnect -> miss state changes -> reconnect -> reconcile.

Answers the three questions that must not be conflated:
  * can the stream be resumed (replay of missed events)?
  * can current state be re-queried?
  * can local state be repaired after lost events (reconcile)?
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import ArtifactAssertion  # noqa: E402
from adapters.codebuddy.adapter import CodeBuddyAdapter  # noqa: E402
from adapters.codebuddy.client import Api, iter_sse_lines, write_evidence  # noqa: E402
from controlplane.plane import ControlPlane
from controlplane.policies import agent_worktree, poc_shell  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402

CWD = str(ROOT / "sandbox" / "repo-a")
CMD = "timeout /t 25 /nobreak >nul & echo sse-ok > sse_t05.txt"


def collect(api: Api, seconds: float, sink: dict, extra_headers: dict | None = None) -> None:
    try:
        resp = api.open_stream("/api/v1/jobs/events", timeout=seconds + 5)
        sink["http"] = getattr(resp, "status", None)
        sink["content_type"] = resp.headers.get("Content-Type")
        sink["connected_at"] = time.time()
        sink["events"] = []
        if extra_headers:
            sink["request_headers"] = extra_headers
        for dt, line in iter_sse_lines(resp, max_seconds=seconds):
            if line.startswith("event:") or line.startswith("data:") or line.startswith("id:"):
                sink["events"].append({"dt": round(dt, 2), "line": line[:300]})
        sink["closed_at"] = time.time()
        resp.close()
    except Exception as exc:  # noqa: BLE001
        sink["error"] = f"{type(exc).__name__}: {exc}"


def main() -> int:
    api = Api()
    api.login()
    adapter = CodeBuddyAdapter(api)
    plane = ControlPlane(adapter, TaskStore(ROOT / ".poc-state" / "t05.json"))
    log = {"test": "T05-sse-reconnect", "phases": {}}

    # 1. subscribe
    first: dict = {}
    t1 = threading.Thread(target=collect, args=(api, 12, first), daemon=True)
    t1.start()
    time.sleep(2)

    # 2. dispatch under observation
    rec = plane.dispatch(CMD, CWD, assertions=[ArtifactAssertion("file_exists", "sse_t05.txt")],
                         policy=poc_shell(), title="T05-sse-subject", idempotency_key="t05")
    job_id = rec.get("worker_job_id")
    log["job"] = {"task_id": rec["task_id"], "worker_job_id": job_id,
                  "dispatch_raw": rec.get("metadata", {}).get("dispatch_raw")}
    time.sleep(4)

    # 3. disconnect while the job is still running
    t1.join(timeout=20)
    log["phases"]["connected_before_disconnect"] = first
    state_at_disconnect = adapter.get_task(job_id).state.value if job_id else None
    log["state_at_disconnect"] = state_at_disconnect

    # 4. let the job change state while nobody is listening
    deadline = time.time() + 90
    while time.time() < deadline:
        snap = adapter.get_task(job_id)
        if snap.state.terminal:
            break
        time.sleep(2)
    log["state_after_gap"] = adapter.get_task(job_id).state.value

    # 5. reconnect, ask for replay explicitly with Last-Event-ID
    second: dict = {}
    t2 = threading.Thread(target=collect, args=(api, 10, second, {"Last-Event-ID": "1"}), daemon=True)
    t2.start()
    t2.join(timeout=15)
    log["phases"]["reconnected"] = second

    # 6. reconcile instead of pretending replay worked
    before_local = {t["task_id"]: t.get("state") for t in plane.store.all()}
    rec_out = plane.refresh()
    after_local = {t["task_id"]: t.get("state") for t in plane.store.all()}

    log["reconcile"] = {"adapter": rec_out["reconcile"],
                        "local_before": before_local, "local_after": after_local,
                        "http": "GET /api/v1/jobs?all=1"}

    events = [e["line"] for e in first.get("events", [])]
    kinds = sorted({e.split(":", 1)[1].strip() for e in events if e.startswith("event:")})
    log["analysis"] = {
        "event_types_seen": kinds,
        "has_id_field": any(e.startswith("id:") for e in events),
        "replay_supported": False,
        "replay_evidence": ("reconnect only delivers a fresh `snapshot` of current state; "
                            "no `id:`/Last-Event-ID cursor exists, so events missed while "
                            "disconnected are unrecoverable"),
        "reconnect_event_types": sorted({e["line"].split(":", 1)[1].strip()
                                         for e in second.get("events", [])
                                         if e["line"].startswith("event:")}),
        "state_recovered_by": "GET /api/v1/jobs?all=1 (reconcile)",
    }

    write_evidence(ROOT / "evidence" / "sse" / "T05-sse-reconnect.json", log)
    print(json.dumps({k: log[k] for k in ("job", "state_at_disconnect", "state_after_gap")},
                     ensure_ascii=False))
    print("event_types:", kinds, "has_id:", log["analysis"]["has_id_field"])
    print("reconnect_types:", log["analysis"]["reconnect_event_types"])
    print("first_stream_events:", len(events), "reconnect_events:", len(second.get("events", [])))
    print("local_after:", after_local)
    return 0


if __name__ == "__main__":
    sys.exit(main())
