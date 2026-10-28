"""Why do jobs stay at `working`? Compare the dispatch response with later reads.

Prints the raw worker record every 2s and reports whether the expected artifact
appears, i.e. whether the child process actually ran.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.codebuddy.client import Api, write_evidence  # noqa: E402

CWD = str(ROOT / "sandbox" / "repo-a")
CMD = "timeout /t 5 /nobreak >nul & echo settle-ok > settle_probe.txt"


def main() -> int:
    api = Api()
    api.login()
    out = {"test": "T00f-settle-probe", "cmd": CMD, "samples": []}
    r = api.dispatch(prompt=CMD, cwd=CWD, bash=True)
    out["dispatch"] = {"http": r["status"], "body": r["body"]}
    job_id = (r["body"].get("data") or {}).get("id")
    out["job_id"] = job_id

    target = Path(CWD) / "settle_probe.txt"
    for i in range(20):
        j = api.job(job_id) if job_id else {}
        out["samples"].append({"t": i * 2, "state": j.get("state"), "alive": j.get("alive"),
                               "settled": j.get("settled"), "pid": j.get("pid"),
                               "detail": j.get("detail"), "artifact": target.exists()})
        time.sleep(2)
        if j.get("settled"):
            break

    out["final_artifact"] = target.exists()
    out["artifact_content"] = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
    write_evidence(ROOT / "evidence" / "jobs" / "T00f-settle-probe.json", out)
    print(json.dumps({"job_id": job_id, "dispatch_http": out["dispatch"]["http"],
                      "samples": out["samples"], "final_artifact": out["final_artifact"]},
                     ensure_ascii=False, indent=1)[:2200])
    return 0


if __name__ == "__main__":
    sys.exit(main())
