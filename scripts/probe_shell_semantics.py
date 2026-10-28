"""T00d: what does `bash: true` actually execute on this platform?

T01 revealed that `;`-separated commands are not interpreted as a script:
`echo A1-start; sleep 6; echo a1-ok > a1.txt; git ...` wrote the whole text into
a1.txt and never ran git. This probe pins down the real execution semantics.
"""
from __future__ import annotations

import time
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.codebuddy.client import Api, write_evidence  # noqa: E402

CWD = str(ROOT / "sandbox" / "repo-a")
CASES = [
    ("semicolon", "timeout /t 3 /nobreak >nul ; echo semi > semi.txt"),
    ("amp", "timeout /t 3 /nobreak >nul & echo amp1 > amp.txt & echo amp2 >> amp.txt"),
    ("ampamp", "timeout /t 3 /nobreak >nul && echo ampamp > ampamp.txt"),
    ("execcode", "cmd /c exit 3"),
]


def main() -> int:
    api = Api()
    api.login()
    out = {"test": "T00d-shell-semantics", "cwd": CWD, "cases": []}
    for label, cmd in CASES:
        t0 = time.time()
        r = api.dispatch(prompt=cmd, cwd=CWD, bash=True)
        job = None
        deadline = time.time() + 25
        while time.time() < deadline:
            time.sleep(1.0)
            hits = [j for j in api.jobs() if (j.get("intent") or "") == cmd]
            if hits:
                job = hits[-1]
                if job.get("settled"):
                    break
        row = {
            "case": label, "cmd": cmd, "dispatch_http": r["status"],
            "job_id": (job or {}).get("id"), "state": (job or {}).get("state"),
            "detail": (job or {}).get("detail"),
            "observed_duration_s": round(((job or {}).get("firstTerminalAt") or 0) / 1000 - t0, 1),
        }
        out["cases"].append(row)
        print(label, row["job_id"], row["state"], row["observed_duration_s"])
    write_evidence(ROOT / "evidence" / "jobs" / "T00d-shell-semantics.json", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
