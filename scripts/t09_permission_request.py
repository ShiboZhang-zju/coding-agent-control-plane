"""T09 (part 1): park a job in the approval state — no decision is made here.

A human must click "拒绝" in the CodeBuddy Web UI. This script only proves the
request exists and records what the worker exposes about it. Part 2
(`t09b_permission_verify.py`) records the outcome and checks for side effects.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.codebuddy.adapter import CodeBuddyAdapter  # noqa: E402
from adapters.codebuddy.client import write_evidence  # noqa: E402
from controlplane.plane import ControlPlane
from controlplane.policies import agent_worktree, poc_shell  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402

CWD = str(ROOT / "sandbox" / "repo-b")
TARGET = Path(CWD) / "approval_probe.txt"
PROMPT = ("请使用 Bash 工具执行这条命令，然后立刻结束任务，不要做其他改动：\n"
          "cmd /c echo approval-probe > approval_probe.txt")


def main() -> int:
    adapter = CodeBuddyAdapter()
    plane = ControlPlane(adapter, TaskStore(ROOT / ".poc-state" / "t09.json"))
    log = {"test": "T09-permission-request", "target": str(TARGET)}

    idem = f"t09-{int(time.time())}"
    rec = plane.dispatch(PROMPT, CWD, policy=agent_worktree(),
                         title="T09-approval-subject", idempotency_key=idem,
                         metadata={"permissionMode": "default"})
    job_id = rec["worker_job_id"]
    log["job"] = {"task_id": rec["task_id"], "worker_job_id": job_id,
                  "dispatch_http": (rec.get("metadata") or {}).get("dispatch_raw", {}).get("dispatch_http")}

    deadline = time.time() + 240
    reached = None
    samples = []
    while time.time() < deadline:
        snap = adapter.get_task(job_id)
        meta = snap.metadata["codebuddy"]
        samples.append({"t": round(time.time() - (rec.get("created_at") or time.time()), 1),
                        "state": snap.state.value, "worker_state": meta["state"],
                        "status": meta["status"], "tempo": meta["tempo"],
                        "waitingFor": meta["waitingFor"], "needs": meta["needs"],
                        "block": meta["block"], "webUrl": meta["webUrl"]})
        if snap.state.value == "waiting_for_approval":
            reached = time.time()
            break
        if snap.state.terminal:
            break
        time.sleep(3)

    log["samples"] = samples
    log["reached_waiting_for_approval"] = reached is not None
    log["worker_job"] = {k: adapter.api.job(job_id).get(k) for k in
                         ("id", "state", "status", "tempo", "alive", "waitingFor",
                          "needs", "block", "webUrl", "sessionId")}
    log["ui_hint"] = "Web UI: http://127.0.0.1:8787/ (password is in the serve banner, not in git)"
    log["next_step"] = ("human must click 拒绝 for this job; then run "
                        "python scripts/t09b_permission_verify.py")
    plane.refresh()
    log["verdict_while_waiting"] = plane.store.get(rec["task_id"])["verdict"]
    write_evidence(ROOT / "evidence" / "permissions" / "T09-permission-request.json", log)

    print(json.dumps({"job": log["job"],
                      "reached_waiting_for_approval": log["reached_waiting_for_approval"],
                      "verdict_while_waiting": log["verdict_while_waiting"],
                      "worker_job": log["worker_job"]}, ensure_ascii=False, indent=1)[:2000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
