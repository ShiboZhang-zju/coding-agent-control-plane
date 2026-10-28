"""Re-evaluate T03b assertions against the isolated worktree.

The first pass verified against the main checkout and therefore rejected a task
that actually succeeded inside its worktree. This recheck proves the fix without
burning another model run.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.codebuddy.adapter import CodeBuddyAdapter  # noqa: E402
from adapters.codebuddy.client import write_evidence  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402


def main() -> int:
    store = TaskStore(ROOT / ".poc-state" / "t03b.json")
    plane = ControlPlane(CodeBuddyAdapter(), store)
    before = {t["task_id"]: (t.get("verdict"), t.get("assertion_results")) for t in store.all()}
    plane.refresh()
    after = {t["task_id"]: {"verdict": t.get("verdict"),
                            "assertions": t.get("assertion_results"),
                            "verify_root": t.get("verify_root"),
                            "worktree": t.get("worktree")} for t in store.all()}

    prev_path = ROOT / "evidence" / "isolation" / "T03b-worktree-agent.json"
    prev = json.loads(prev_path.read_text(encoding="utf-8")) if prev_path.exists() else {}
    prev["recheck"] = {
        "why": "assertions were first evaluated against cwd; an isolated run writes into its worktree",
        "before": {k: {"verdict": v[0], "assertions": v[1]} for k, v in before.items()},
        "after": after,
    }
    prev["verdict"] = after.get(list(after)[0], {}).get("verdict") if after else None
    write_evidence(prev_path, prev)
    print(json.dumps(after, ensure_ascii=False, indent=1)[:1500])
    return 0


if __name__ == "__main__":
    sys.exit(main())
