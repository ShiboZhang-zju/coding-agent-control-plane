"""T06 + T07: cancel/stop semantics, then conversation resume and respawn.

Kept in one script because both end up asking the same questions: what survives
a stop, and what does the worker actually offer for recovery?
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import ArtifactAssertion, UnsupportedOperation  # noqa: E402
from adapters.codebuddy.adapter import CodeBuddyAdapter  # noqa: E402
from adapters.codebuddy.client import write_evidence  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402

CWD = str(ROOT / "sandbox" / "repo-a")
CMD = "timeout /t 90 /nobreak >nul & echo should-not-exist > cancel_target.txt"


def main() -> int:
    adapter = CodeBuddyAdapter()
    plane = ControlPlane(adapter, TaskStore(ROOT / ".poc-state" / "t06.json"))
    log = {"test": "T06-cancel-stop", "cancel": {}, "resume": {}, "respawn": {}}

    rec = plane.dispatch(CMD, CWD, assertions=[ArtifactAssertion("file_exists", "cancel_target.txt")],
                         mode="shell", title="T06-cancel-subject", idempotency_key="t06")
    job_id = rec["worker_job_id"]
    log["cancel"]["job"] = {"task_id": rec["task_id"], "worker_job_id": job_id}
    time.sleep(8)

    before = adapter.get_task(job_id)
    log["cancel"]["state_before"] = {"state": before.state.value, "pid": before.pid}
    t0 = time.time()
    snap = plane.cancel(rec["task_id"])
    log["cancel"].update({
        "stop_request_http": adapter.api.calls[-1]["status"] if adapter.api.calls else None,
        "state_after": snap["state"], "verdict_after": snap["verdict"],
        "elapsed_s": round(time.time() - t0, 1),
        "worker_state": snap.get("worker_metadata", {}).get("state"),
        "error": (rec.get("metadata") or {}).get("dispatch_raw", {}).get("dispatch_http"),
    })
    log["cancel"]["artifact_absent"] = not (Path(CWD) / "cancel_target.txt").exists()
    log["cancel"]["note"] = ("a stopped job must never be accepted; the artifact assertion is "
                             "what proves the side effect did not happen")

    # ---- conversation resume (archived sessions only) -------------------
    try:
        log["resumable"] = {"http": adapter.api.resumable(CWD)["status"],
                            "body": adapter.api.resumable(CWD)["body"]}
    except Exception as exc:  # noqa: BLE001
        log["resumable"] = {"error": f"{type(exc).__name__}: {exc}"}

    session_id = before.session_id
    try:
        r = adapter.api.resume_session(session_id, cwd=CWD)
        log["resume"] = {"session_id": session_id, "http": r["status"], "body": r["body"]}
        plane.refresh()
    except UnsupportedOperation as exc:
        log["resume"] = {"session_id": session_id, "unsupported": str(exc)}

    # ---- respawn --------------------------------------------------------
    try:
        snap2 = plane.respawn(rec["task_id"])
        log["respawn"] = {"state": snap2["state"], "session_id": snap2.get("session_id"),
                          "http": adapter.api.calls[-1]["status"] if adapter.api.calls else None}
        time.sleep(6)
        snap3 = adapter.get_task(job_id)
        log["respawn"]["state_after_6s"] = snap3.state.value
        log["respawn"]["worker_meta"] = snap3.metadata["codebuddy"]
    except UnsupportedOperation as exc:
        log["respawn"] = {"unsupported": str(exc)}

    write_evidence(ROOT / "evidence" / "recovery" / "T06-cancel-stop.json", log)
    print(json.dumps(log, ensure_ascii=False, indent=1)[:2500])
    return 0


if __name__ == "__main__":
    sys.exit(main())
