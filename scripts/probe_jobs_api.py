"""T00: probe the Jobs API contract (dispatch -> poll -> observe) before designing the adapter.

Answers: what does POST /api/v1/jobs return, what fields does GET /api/v1/jobs/{id}
expose, what state strings are used, and how fast does state change.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from adapters.codebuddy.client import Api, write_evidence  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CWD = str(ROOT / "sandbox" / "repo-a")


def main() -> int:
    api = Api()
    log = {"test": "T00-api-probe", "steps": []}
    log["steps"].append({"label": "auth_status", **api.auth_status()})
    log["steps"].append({"label": "login", **api.login()})

    disp = api.dispatch(prompt="echo probe > probe.txt", cwd=CWD, bash=True,
                        name="poc-probe-bash")
    log["steps"].append({"label": "dispatch", **disp})
    job = disp["body"].get("data", disp["body"])
    job_id = job.get("id") or job.get("jobId") or job.get("sessionId")
    log["job_id"] = job_id

    for i in range(12):
        time.sleep(2)
        g = api.get_job(job_id)
        snap = g["body"].get("data", g["body"])
        log["steps"].append({"label": f"poll-{i}", "status": g["status"], "data": snap})
        state = (snap or {}).get("state") or (snap or {}).get("status")
        print(i, g["status"], state, json.dumps(snap, ensure_ascii=False)[:200])
        if state in ("completed", "failed", "cancelled", "stopped", "error"):
            break

    log["steps"].append({"label": "list", **api.list_jobs()})
    write_evidence(ROOT / "evidence" / "jobs" / "T00-api-probe.json", log)
    print("job_id", job_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
