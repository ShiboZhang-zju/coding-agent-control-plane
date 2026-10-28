"""Print the current worker view of the last N jobs (compact, no secrets)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.codebuddy.client import Api  # noqa: E402


def main() -> int:
    api = Api()
    api.login()
    jobs = api.jobs()
    print(f"total jobs: {len(jobs)}")
    for j in jobs[:14]:
        print(f"{j['id']} {j.get('state'):>8} alive={j.get('alive')} settled={j.get('settled')} "
              f"pid={j.get('pid')} | {(j.get('intent') or '')[:52]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
