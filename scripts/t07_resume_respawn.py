"""T07: resume / respawn against a *healthy* completed job (T06 hit a lock timeout).

Also probes the archived-session resume path (`GET /api/v1/jobs/resumable` +
`POST /api/v1/jobs/resume`), which is the only documented conversation-recovery
mechanism.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.codebuddy.adapter import CodeBuddyAdapter  # noqa: E402
from adapters.codebuddy.client import Api, write_evidence  # noqa: E402

CWD = str(ROOT / "sandbox" / "repo-b")
CMD = "timeout /t 6 /nobreak >nul & echo t07 > t07.txt"


def main() -> int:
    api = Api()
    api.login()
    adapter = CodeBuddyAdapter(api)
    log = {"test": "T07-resume-respawn"}

    r = api.dispatch(prompt=CMD, cwd=CWD, bash=True)
    log["dispatch_http"] = r["status"]
    job_id = (r["body"].get("data") or {}).get("id")
    if not job_id:
        hits = [j for j in api.jobs() if (j.get("intent") or "") == CMD]
        job_id = hits[-1]["id"] if hits else None
    log["worker_job_id"] = job_id

    t0 = time.time()
    while time.time() - t0 < 90:
        job = api.job(job_id)
        if job.get("settled"):
            break
        time.sleep(2)
    job = api.job(job_id)
    log["after_completion"] = {"state": job.get("state"), "settled": job.get("settled"),
                               "session_id": job.get("sessionId")}

    # 1) respawn a completed job
    rr = api.respawn_job(job_id)
    log["respawn"] = {"http": rr["status"], "body": rr["body"]}
    if rr["status"] == 200:
        time.sleep(5)
        j2 = api.job(job_id)
        log["respawn"]["state_after_5s"] = j2.get("state")
        log["respawn"]["alive_after_5s"] = j2.get("alive")
        log["respawn"]["pid_after_5s"] = j2.get("pid")

    # 2) archived-session resume
    rs = api.resumable(CWD)
    log["resumable"] = {"http": rs["status"], "body": rs["body"]}
    sid = job.get("sessionId")
    rs2 = api.resume_session(sid, cwd=CWD)
    log["resume"] = {"session_id": sid, "http": rs2["status"], "body": rs2["body"]}
    if rs2["status"] == 200:
        new_job = (rs2["body"].get("data") or {}).get("id")
        log["resume"]["new_job_id"] = new_job
        if new_job:
            time.sleep(3)
            log["resume"]["new_job"] = {k: api.job(new_job).get(k)
                                        for k in ("id", "state", "sessionId", "alive", "settled")}

    # 3) what survives: session -> job mapping
    js = api.job_session(job_id)
    log["job_session"] = {"http": js["status"], "body": js["body"]}

    log["conclusion"] = {
        "conversation_recovery": "see resume/resumable",
        "execution_recovery": "no endpoint restarts an interrupted command; respawn only "
                              "re-opens the conversation",
    }
    write_evidence(ROOT / "evidence" / "recovery" / "T07-resume-respawn.json", log)
    print(json.dumps(log, ensure_ascii=False, indent=1)[:2200])
    return 0


if __name__ == "__main__":
    sys.exit(main())
