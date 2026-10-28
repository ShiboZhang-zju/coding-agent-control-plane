"""Smoke check: does dispatch now return 200 and create a visible job?"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.codebuddy.client import Api  # noqa: E402

CWD = str(ROOT / "sandbox" / "repo-a")


def main() -> int:
    api = Api()
    api.login()
    for i in range(2):
        cmd = f"timeout /t 3 /nobreak >nul & echo smoke{i} > smoke{i}.txt"
        r = api.dispatch(prompt=cmd, cwd=CWD, bash=True)
        print("dispatch", i, "http", r["status"], str(r["body"])[:160])
        time.sleep(1.5)
        hits = [j for j in api.jobs() if (j.get("intent") or "") == cmd]
        print("  visible_job:", hits[-1]["id"] if hits else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
