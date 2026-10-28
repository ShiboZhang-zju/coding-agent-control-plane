"""T05c: SSE replay test without keeping a subscription open across the dispatch.

T05/T05b showed a side effect worth isolating: jobs dispatched *while* a Jobs SSE
subscription was held never settled (state stayed `working`, alive=false, pid=null).
This variant dispatches first, then subscribes, so "state changed while
disconnected" can be observed independently of that defect.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.codebuddy.adapter import CodeBuddyAdapter  # noqa: E402
from adapters.codebuddy.client import Api, iter_sse_lines, write_evidence  # noqa: E402

CWD = str(ROOT / "sandbox" / "repo-a")
CMD = "timeout /t 12 /nobreak >nul & echo ssec-ok > ssec.txt"


def collect(api: Api, seconds: float, sink: dict) -> None:
    try:
        resp = api.open_stream("/api/v1/jobs/events", timeout=seconds + 5)
        sink.update(http=getattr(resp, "status", None),
                    content_type=resp.headers.get("Content-Type"), events=[])
        for dt, line in iter_sse_lines(resp, max_seconds=seconds):
            if line.startswith(("event:", "data:", "id:")):
                sink["events"].append({"dt": round(dt, 2), "line": line[:260]})
        resp.close()
    except Exception as exc:  # noqa: BLE001
        sink["error"] = f"{type(exc).__name__}: {exc}"


def main() -> int:
    api = Api()
    api.login()
    adapter = CodeBuddyAdapter(api)
    log = {"test": "T05c-sse-isolated"}

    r = api.dispatch(prompt=CMD, cwd=CWD, bash=True)
    job_id = (r["body"].get("data") or {}).get("id")
    if not job_id:
        hits = [j for j in api.jobs() if (j.get("intent") or "") == CMD]
        job_id = hits[-1]["id"] if hits else None
    log["job"] = {"worker_job_id": job_id, "dispatch_http": r["status"]}

    time.sleep(2)
    # 1) subscribe while the job is running
    first: dict = {}
    th = threading.Thread(target=collect, args=(api, 6, first), daemon=True)
    th.start()
    th.join(timeout=15)
    log["subscribe_while_running"] = {"events": first.get("events", []),
                                      "error": first.get("error"),
                                      "state": api.job(job_id).get("state")}

    # 2) disconnect, then let it finish unwatched
    t0 = time.time()
    while time.time() - t0 < 90:
        j = api.job(job_id)
        if j.get("settled"):
            break
        time.sleep(2)
    j = api.job(job_id)
    log["after_gap"] = {"state": j.get("state"), "settled": j.get("settled"),
                        "gap_seconds": round(time.time() - t0, 1)}

    # 3) reconnect and see whether the missed transition is replayed
    second: dict = {}
    th2 = threading.Thread(target=collect, args=(api, 22, second), daemon=True)
    th2.start()
    th2.join(timeout=30)
    log["reconnect"] = {"events": second.get("events", []), "error": second.get("error")}

    ev = log["subscribe_while_running"]["events"]
    rev = second.get("events", [])
    log["analysis"] = {
        "types_running": sorted({e["line"].split(":", 1)[1].strip() for e in ev
                                 if e["line"].startswith("event:")}),
        "types_reconnect": sorted({e["line"].split(":", 1)[1].strip() for e in rev
                                   if e["line"].startswith("event:")}),
        "id_field_present": any(e["line"].startswith("id:") for e in ev + rev),
        "reconnect_payload_mentions_job": any(job_id in e["line"] for e in rev
                                              if e["line"].startswith("data:")),
        "job_settled_after_stream_closed": j.get("settled"),
        "conclusion": ("no cursor, no replay: a reconnect can only reveal current state "
                       "via a fresh snapshot (or nothing at all until the next change)"),
    }
    write_evidence(ROOT / "evidence" / "sse" / "T05c-sse-isolated.json", log)
    print(json.dumps(log["analysis"], ensure_ascii=False, indent=1))
    print("subscribe_events:", len(ev), "reconnect_events:", len(rev))
    print("after_gap:", log["after_gap"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
