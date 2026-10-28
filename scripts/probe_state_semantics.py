"""T00b: does a non-zero shell exit map to a terminal `failed` state?

Central PoC question for "失败任务绝不能被标记为验收通过".
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


def watch(api: Api, job_id: str, seconds: int = 40) -> list[dict]:
    seen, t0 = [], time.time()
    while time.time() - t0 < seconds:
        g = api.get_job(job_id)
        d = g["body"].get("data", g["body"]) or {}
        row = {"t": round(time.time() - t0, 2), "http": g["status"],
               "state": d.get("state"), "status": d.get("status"), "tempo": d.get("tempo"),
               "alive": d.get("alive"), "settled": d.get("settled"),
               "detail": d.get("detail"), "waitingFor": d.get("waitingFor")}
        if not seen or {k: seen[-1][k] for k in ("state", "status", "tempo", "alive")} != \
                {k: row[k] for k in ("state", "status", "tempo", "alive")}:
            seen.append(row)
        if d.get("settled") or d.get("state") in ("done", "failed", "stopped"):
            if d.get("settled"):
                break
        time.sleep(1.0)
    return seen


def dispatch_and_watch(api: Api, label: str, prompt: str, bash: bool = True) -> dict:
    r = api.dispatch(prompt=prompt, cwd=CWD, bash=bash, name=label)
    out = {"label": label, "dispatch_http": r["status"], "dispatch_body": r["body"]}
    data = r["body"].get("data", {}) if isinstance(r["body"], dict) else {}
    job_id = data.get("id")
    out["job_id"] = job_id
    if not job_id:
        # dispatch may 500 after creating the job (see REPORT: safe-delete shim) -> reconcile by name
        lst = api.list_jobs()["body"]["data"]["jobs"]
        cand = [j for j in lst if j.get("name") == label]
        if cand:
            job_id = sorted(cand, key=lambda j: j.get("startedAt", 0))[-1]["id"]
            out["job_id"] = job_id
            out["recovered_by"] = "list-jobs name match"
    if job_id:
        out["timeline"] = watch(api, job_id)
        out["final"] = (api.get_job(job_id)["body"].get("data") or {})
    return out


def main() -> int:
    api = Api()
    api.login()
    log = {"test": "T00b-state-semantics", "runs": []}
    log["runs"].append(dispatch_and_watch(api, "poc-ok", "echo ok > ok.txt", bash=True))
    log["runs"].append(dispatch_and_watch(api, "poc-fail", "echo boom >&2; exit 3", bash=True))
    write_evidence(ROOT / "evidence" / "jobs" / "T00b-state-semantics.json", log)
    for r in log["runs"]:
        print(r["label"], "job", r["job_id"], "dispatch_http", r["dispatch_http"],
              "final_state", (r.get("final") or {}).get("state"),
              "detail", str((r.get("final") or {}).get("detail"))[:80])
    return 0


if __name__ == "__main__":
    sys.exit(main())
