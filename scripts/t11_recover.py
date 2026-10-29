"""Rebuild the T11 evidence for a run whose final API call timed out.

Source of truth, in priority order:
  1. worker job state files (~/.codebuddy/jobs/<id>/state.json) - state + timestamps
  2. the git worktree the worker created (artifact + branch + commits)
  3. the main checkout status, with `.codebuddy/` bookkeeping filtered out

This is deliberately API-independent: the 30s job-store stall can make a
parent-test refresh() time out even though all four agents did their work.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import winproc  # noqa: E402

JOBS = {
    "agent-a1": "b3ef26ac",
    "agent-a2": "8dc5d154",
    "agent-b1": "36726069",
    "agent-b2": "87c8807d",
}


def state_file(job_id: str) -> dict:
    p = Path.home() / ".codebuddy" / "jobs" / job_id / "state.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def git(cwd: Path, *args: str) -> str:
    if not cwd.exists():
        return ""
    p = winproc.run(["git", "-C", str(cwd), *args], timeout=30)
    return (p.stdout or p.stderr).strip()


def find_worktree(job_id: str) -> Path | None:
    for base in (ROOT / "sandbox").glob("repo-*/"):
        wt = base / ".codebuddy" / "worktrees" / f"bg-{job_id}"
        if wt.exists():
            return wt
    return None


def main() -> int:
    rows = []
    for name, job_id in JOBS.items():
        st = state_file(job_id)
        wt = find_worktree(job_id)
        artifact = None
        branch = ""
        files: list[str] = []
        if wt:
            for cand in wt.glob(f"{name}.txt"):
                artifact = {"path": str(cand), "content": cand.read_text(
                    encoding="utf-8", errors="replace").strip()}
            branch = git(wt, "rev-parse", "--abbrev-ref", "HEAD")
            files = [ln[3:].strip() for ln in git(wt, "status", "--porcelain").splitlines() if ln.strip()]
        rows.append({
            "name": name, "worker_job_id": job_id,
            "worker_state": st.get("state"),
            "started_at": st.get("createdAt"), "finished_at": st.get("firstTerminalAt"),
            "pid": st.get("pid"),
            "worktree": str(wt) if wt else None,
            "branch": branch,
            "artifact": artifact,
            "uncommitted_in_worktree": files,
            "detail": (st.get("detail") or "")[:120],
        })

    def epoch(ts: str | None) -> float | None:
        if not ts:
            return None
        return time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))

    spans = [(r["name"], epoch(r["started_at"]), epoch(r["finished_at"])) for r in rows]
    events = []
    for n, s, e in spans:
        if s:
            events += [(s, 1, n), (e or time.time(), -1, n)]
    events.sort()
    cur = peak = 0
    for _ts, delta, _n in events:
        cur += delta
        peak = max(peak, cur)

    mains = {}
    for base in sorted((ROOT / "sandbox").glob("repo-*/")):
        if ".codebuddy" in str(base):
            continue
        status = "\n".join(ln for ln in git(base, "status", "--porcelain").splitlines()
                           if not ln[3:].strip().replace("\\", "/").startswith(".codebuddy/"))
        if not status and not git(base, "log", "--oneline", "-1"):
            continue
        mains[base.name] = {"status": status, "head": git(base, "rev-parse", "--short", "HEAD")}

    evidence = {
        "test": "T11-four-agents-parallel",
        "recovered_from": "worker state files + git worktrees (UI/API refresh timed out)",
        "all_succeeded": all(r["worker_state"] == "done" for r in rows),
        "all_have_artifact": all(r["artifact"] for r in rows),
        "distinct_worktrees": len({r["worktree"] for r in rows if r["worktree"]}),
        "peak_concurrent_by_timestamps": peak,
        "tasks": rows,
        "main_checkouts": mains,
        "pass": (all(r["worker_state"] == "done" for r in rows)
                 and all(r["artifact"] for r in rows)
                 and len({r["worktree"] for r in rows if r["worktree"]}) == 4
                 and peak == 4),
    }
    (ROOT / "evidence" / "isolation").mkdir(parents=True, exist_ok=True)
    (ROOT / "evidence" / "isolation" / "T11-4way-success.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"peak={peak} distinct_worktrees={evidence['distinct_worktrees']} "
          f"all_done={evidence['all_succeeded']} all_artifact={evidence['all_have_artifact']} "
          f"pass={evidence['pass']}")
    for r in rows:
        print(f"  {r['name']}: {r['worker_state']} {r['started_at']}..{r['finished_at']} "
              f"branch={r['branch']} artifact={'yes' if r['artifact'] else 'NO'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
