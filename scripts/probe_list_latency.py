"""Measure /api/v1/jobs latency variants, to decide how the plane should poll."""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.codebuddy.client import Api, write_evidence  # noqa: E402

CWD = str(ROOT / "sandbox" / "repo-a")
VARIANTS = [
    ("all=1", "/api/v1/jobs?all=1"),
    ("all=0", "/api/v1/jobs?all=0"),
    ("cwd", "/api/v1/jobs?all=1&cwd=" + urllib.parse.quote(CWD)),
    ("prefs", "/api/v1/jobs/prefs"),
    ("resumable", "/api/v1/jobs/resumable"),
]


def main() -> int:
    api = Api()
    api.login()
    out = {"test": "T00e-list-latency", "cwd": CWD, "runs": []}
    for label, path in VARIANTS:
        for rep in (1, 2):
            t0 = time.time()
            try:
                status, payload, _ = api.raw("GET", path, timeout=300)
                row = {"variant": label, "rep": rep, "status": status,
                       "seconds": round(time.time() - t0, 2), "bytes": len(payload)}
            except Exception as exc:  # noqa: BLE001
                row = {"variant": label, "rep": rep, "error": f"{type(exc).__name__}: {exc}",
                       "seconds": round(time.time() - t0, 2)}
            out["runs"].append(row)
            print(json.dumps(row))
    write_evidence(ROOT / "evidence" / "jobs" / "T00e-list-latency.json", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
