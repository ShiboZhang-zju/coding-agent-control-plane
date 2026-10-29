"""T12: duplicate dispatch + unsafe-policy guard.

  * the same idempotency key must start at most one worker job;
  * a policy that asks for an un-isolated shell command without an explicit
    opt-in must be refused (P0-1).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import ArtifactAssertion, DispatchPolicy  # noqa: E402
from adapters.codebuddy.adapter import CodeBuddyAdapter  # noqa: E402
from adapters.codebuddy.client import write_evidence  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.policies import poc_shell  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402

CWD = str(ROOT / "sandbox" / "repo-a")
CMD = "timeout /t 6 /nobreak >nul & echo idem > idem.txt"


def main() -> int:
    adapter = CodeBuddyAdapter()
    store = TaskStore(ROOT / ".poc-state" / "t12.json")
    store.clear()
    plane = ControlPlane(adapter, store, policy=poc_shell())
    log = {"test": "T12-idempotency-and-policy"}

    # ---- 1) unsafe policy is refused ----------------------------------
    refused = None
    try:
        plane.dispatch("echo hi", CWD,
                       policy=DispatchPolicy(mode="shell", isolation="none"))
    except ValueError as exc:
        refused = str(exc)
    log["unsafe_policy_refused"] = refused

    # ---- 2) same idempotency key twice --------------------------------
    before = len([j for j in adapter.api.jobs() if (j.get("intent") or "") == CMD])
    a = plane.dispatch_approved(CMD, CWD,
                                assertions=[ArtifactAssertion("file_exists", "idem.txt")],
                                title="T12-idem", idempotency_key="t12-same-key")
    b = plane.dispatch(CMD, CWD, assertions=[ArtifactAssertion("file_exists", "idem.txt")],
                       title="T12-idem", idempotency_key="t12-same-key")
    log["first"] = {"task_id": a["task_id"], "worker_job_id": a.get("worker_job_id"),
                    "deduplicated": a.get("deduplicated", False)}
    log["second"] = {"task_id": b["task_id"], "worker_job_id": b.get("worker_job_id"),
                     "deduplicated": b.get("deduplicated", False)}

    # count matching jobs on the worker side, after it had time to appear
    import time
    time.sleep(8)
    after = len([j for j in adapter.api.jobs() if (j.get("intent") or "") == CMD])
    log["worker_jobs_with_this_prompt"] = {"before": before, "after": after,
                                           "delta": after - before}
    log["pass"] = {
        "unsafe_policy_refused": refused is not None,
        "same_task_id": a["task_id"] == b["task_id"],
        "second_marked_deduplicated": bool(b.get("deduplicated")),
        "only_one_worker_job": (after - before) == 1,
    }
    log["result"] = "Pass" if all(log["pass"].values()) else "Fail"
    write_evidence(ROOT / "evidence" / "jobs" / "T12-idempotency.json", log)
    print(json.dumps(log, ensure_ascii=False, indent=1)[:1600])
    return 0


if __name__ == "__main__":
    sys.exit(main())
