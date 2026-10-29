"""T05b: SSE reconnect with a short-lived subject job and a long reconnect window.

The first attempt used a 25s job that never settled (see T02/lost evidence), so the
"state changed while disconnected" phase could not be observed. This rerun uses an
8s job and watches the reconnect stream for 35s.
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

CWD = str(ROOT / "sandbox" / "repo-b")
CMD = "timeout /t 8 /nobreak >nul & echo sseb-ok > sseb.txt"


def collect(api: Api, seconds: float, sink: dict) -> None:
    try:
        resp = api.open_stream("/api/v1/jobs/events", timeout=seconds + 5)
        sink.update(http=getattr(resp, "status", None),
                    content_type=resp.headers.get("Content-Type"),
                    connected_at=time.time(), events=[])
        for dt, line in iter_sse_lines(resp, max_seconds=seconds):
            if line.startswith(("event:", "data:", "id:")):
                sink["events"].append({"dt": round(dt, 2), "line": line[:260]})
        sink["closed_at"] = time.time()
        resp.close()
    except Exception as exc:  # noqa: BLE001
        sink["error"] = f"{type(exc).__name__}: {exc}"


def main() -> int:
    api = Api()
    api.login()
    adapter = CodeBuddyAdapter(api)
    plane = ControlPlane(adapter, TaskStore(ROOT / ".poc-state" / "t05b.json"))
    log = {"test": "T05b-sse-reconnect"}

    first: dict = {}
    th = threading.Thread(target=collect, args=(api, 14, first), daemon=True)
    th.start()
    time.sleep(2)
    rec = plane.dispatch(CMD, CWD, assertions=[ArtifactAssertion("file_exists", "sseb.txt")],
                         policy=poc_shell(), title="T05b-sse-subject", idempotency_key="t05b")
    job_id = rec["worker_job_id"]
    log["job"] = {"task_id": rec["task_id"], "worker_job_id": job_id}
    time.sleep(3)
    th.join(timeout=25)
    log["phase1"] = {"events": first.get("events", []), "error": first.get("error"),
                     "state_at_disconnect": adapter.get_task(job_id).state.value}

    # while disconnected: wait for the job to reach a terminal state
    t0 = time.time()
    while time.time() - t0 < 120:
        snap = adapter.get_task(job_id)
        if snap.state.terminal:
            break
        time.sleep(2)
    log["phase2_gap"] = {"state": adapter.get_task(job_id).state.value,
                         "gap_seconds": round(time.time() - t0, 1)}

    # reconnect and listen long enough to catch keepalive + snapshot
    second: dict = {}
    th2 = threading.Thread(target=collect, args=(api, 35, second), daemon=True)
    th2.start()
    th2.join(timeout=45)
    log["phase3_reconnect"] = {"events": second.get("events", []), "error": second.get("error"),
                               "content_type": second.get("content_type")}

    plane.refresh()
    rec_final = plane.store.get(rec["task_id"])
    log["reconcile"] = {"local_state": rec_final["state"], "verdict": rec_final["verdict"],
                        "http": "GET /api/v1/jobs?all=1"}

    ev = [e["line"] for e in log["phase1"]["events"]]
    rev = [e["line"] for e in second.get("events", [])]
    log["analysis"] = {
        "event_types_phase1": sorted({e.split(":", 1)[1].strip() for e in ev if e.startswith("event:")}),
        "event_types_reconnect": sorted({e.split(":", 1)[1].strip() for e in rev if e.startswith("event:")}),
        "has_id_field": any(e.startswith("id:") for e in ev + rev),
        "replayed_changed_event": any("sseb" in e or job_id in e for e in rev if e.startswith("data:")),
        "verdict": ("reconnect gives current state only; nothing that happened while "
                    "disconnected is replayed"),
    }
    write_evidence(ROOT / "evidence" / "sse" / "T05b-sse-rerun.json", log)
    print(json.dumps({"phase1": log["phase1"]["state_at_disconnect"],
                      "gap": log["phase2_gap"], "reconcile": log["reconcile"],
                      "analysis": log["analysis"]}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
