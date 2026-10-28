"""Probe the restarted service: is the job list still queryable, and how slow?"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.codebuddy.client import Api  # noqa: E402


def timed(api: Api, path: str, timeout: float) -> dict:
    t0 = time.time()
    try:
        status, payload, _ = api.raw("GET", path, timeout=timeout)
        return {"path": path, "status": status, "seconds": round(time.time() - t0, 1),
                "bytes": len(payload)}
    except Exception as exc:  # noqa: BLE001
        return {"path": path, "error": f"{type(exc).__name__}: {exc}",
                "seconds": round(time.time() - t0, 1)}


def main() -> int:
    api = Api()
    api.login()
    out = {"test": "T08b-post-restart-probe", "probes": []}
    for path, timeout in (("/api/v1/health", 20), ("/api/v1/jobs?all=1", 180),
                          ("/api/v1/jobs/43dc7110", 60)):
        row = timed(api, path, timeout)
        out["probes"].append(row)
        print(json.dumps(row))
    try:
        jobs = api.jobs()
        out["job_count"] = len(jobs)
        out["states"] = {j["id"]: j.get("state") for j in jobs}
        print("jobs:", out["job_count"])
    except Exception as exc:  # noqa: BLE001
        out["jobs_error"] = f"{type(exc).__name__}: {exc}"
        print("jobs error:", out["jobs_error"])
    p = ROOT / "evidence" / "recovery" / "T08b-post-restart-probe.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
