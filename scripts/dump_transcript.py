"""Dump a job transcript (sanitized) to evidence, for post-mortem of agent jobs."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.codebuddy.client import Api, write_evidence  # noqa: E402


def main() -> int:
    job_id = sys.argv[1]
    name = sys.argv[2] if len(sys.argv) > 2 else job_id
    api = Api()
    api.login()
    out = {"job_id": job_id, "pages": []}
    offset = 0
    while True:
        r = api.job_transcript(job_id, offset=offset, limit=50)
        data = r["body"].get("data", {}) if isinstance(r["body"], dict) else {}
        ups = data.get("updates") or []
        out["pages"].append({"http": r["status"], "offset": offset, "count": len(ups),
                             "updates": ups})
        pg = data.get("pagination") or {}
        if not pg.get("hasMore"):
            break
        offset = pg.get("nextOffset") or 0
        if len(out["pages"]) > 10:
            break
    write_evidence(ROOT / "evidence" / "permissions" / f"T09-transcript-{name}.json", out)
    total = sum(p["count"] for p in out["pages"])
    print(f"pages={len(out['pages'])} updates={total}")
    for p in out["pages"][:2]:
        for u in p["updates"][:6]:
            print(json.dumps(u, ensure_ascii=False)[:300])
    return 0


if __name__ == "__main__":
    sys.exit(main())
