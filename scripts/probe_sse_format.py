"""T00c: capture the raw wire format of the two candidate event streams.

  GET /api/v1/jobs/events        (job list change notifications)
  GET /api/v1/jobs/{id}/stream   (per-job transcript tail)
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.codebuddy.client import Api, iter_sse_lines, write_evidence  # noqa: E402

CWD = str(ROOT / "sandbox" / "repo-a")


def read_stream(api: Api, path: str, seconds: float, sink: list) -> None:
    try:
        resp = api.open_stream(path, timeout=seconds + 5)
        sink.append({"path": path, "http": getattr(resp, "status", None),
                     "content_type": resp.headers.get("Content-Type"),
                     "opened_at": time.time(), "lines": []})
        for dt, line in iter_sse_lines(resp, max_seconds=seconds):
            sink[-1]["lines"].append({"dt": round(dt, 3), "line": line})
        resp.close()
    except Exception as exc:  # noqa: BLE001 - probe must record failures verbatim
        sink.append({"path": path, "error": f"{type(exc).__name__}: {exc}"})


def main() -> int:
    api = Api()
    api.login()
    out = {"test": "T00c-sse-format", "streams": []}
    sink: list = []

    t = threading.Thread(target=read_stream, args=(api, "/api/v1/jobs/events", 25, sink), daemon=True)
    t.start()
    time.sleep(2.0)

    disp = api.dispatch(prompt="sleep 6; echo sse-probe > sse.txt", cwd=CWD, bash=True)
    out["dispatch"] = disp
    time.sleep(1.0)
    jobs = api.jobs()
    job_id = None
    for j in jobs:
        if j.get("intent") == "sleep 6; echo sse-probe > sse.txt":
            job_id = j["id"]
    out["job_id"] = job_id

    if job_id:
        t2 = threading.Thread(target=read_stream, args=(api, f"/api/v1/jobs/{job_id}/stream", 12, sink), daemon=True)
        t2.start()

    t.join(timeout=30)
    time.sleep(13)
    out["streams"] = sink
    write_evidence(ROOT / "evidence" / "sse" / "T00c-sse-format.json", out)
    for s in out["streams"]:
        print("=" * 60)
        print(s.get("path"), "http", s.get("http"), "ct", s.get("content_type"), "err", s.get("error"))
        for row in s.get("lines", [])[:12]:
            print(f"  +{row['dt']:>6}s  {row['line'][:240]}")
        print("  total lines:", len(s.get("lines", [])))
    print("job_id", job_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
