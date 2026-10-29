"""T27: Project Context Store — isolation, versioning, provenance, security domain.

Cases
-----
A  isolation      Project A's knowledge never reaches project B's prompt; the same
                  (kind, title) may exist in both projects without conflict; a
                  cross-project read is a hard error, not an empty result.
B  versioning     Two writers holding the same expected_version: exactly one wins.
                  The loser gets ContextVersionConflict, never a silent overwrite.
                  Versions survive a process restart (new TaskStore on the same db).
C  provenance     Every entry keeps source / evidence refs / timestamps, and each
                  successful update leaves a revision row with the previous state.
D  security       An unverified constraint is NOT a hard constraint; a verified one
                  is. `local` entries are excluded when a caller asks for shareable
                  context only. Nothing is synced anywhere (no network code path).

All work happens in a temporary ledger under `.poc-state/`; no sandbox repo is
modified.
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import DispatchPolicy, DispatchResult, TaskSnapshot  # noqa: E402
from controlplane.context_builder import ContextBuilder  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.project_context import (  # noqa: E402
    ContextError,
    ContextVersionConflict,
    ProjectContextStore,
    is_hard_constraint,
)
from controlplane.store import TaskStore  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

REPO_A = str(ROOT / "sandbox" / "repo-a")
REPO_B = str(ROOT / "sandbox" / "repo-b")
LOW = DispatchPolicy(mode="agent", isolation="worktree")


class StubAdapter:
    """Records dispatches; never touches a real worker."""

    name = "stub"

    def __init__(self):
        self.calls = 0
        self.prompts: list[str] = []

    def dispatch(self, request) -> DispatchResult:
        self.calls += 1
        self.prompts.append(request.prompt)
        return DispatchResult(task_id=request.task_id,
                              worker_job_id=f"job-{self.calls}",
                              accepted=True, raw={"dispatch_http": 200})

    def get_task(self, job_id: str, timeout_s: float = 20.0) -> TaskSnapshot:
        return TaskSnapshot(task_id=job_id, worker_job_id=job_id,
                            state="running", cwd="", detail="stub")

    def collect_artifacts(self, _task_id):
        return []

    def reconcile(self) -> dict:
        return {"source": "stub", "total": 0, "new": [], "changed": [], "lost": [],
                "jobs": []}

    def pid_alive(self, pid):
        return None


def fresh_store(name: str) -> TaskStore:
    db = ROOT / ".poc-state" / f"t27-{name}.db"
    for stale in [db, *db.parent.glob(f"t27-{name}.db.*")]:
        stale.unlink(missing_ok=True)
    return TaskStore(db)


def register(plane: ControlPlane, name: str, root: str) -> dict:
    return plane.register_project(name, root, default_adapter="stub",
                                  policy=LOW, concurrency_limit=2)


# --------------------------------------------------------------------------- #
# A: isolation
# --------------------------------------------------------------------------- #
def case_a_isolation() -> dict:
    store = fresh_store("a")
    plane = ControlPlane(StubAdapter(), store, policy=LOW)
    pa = register(plane, "t27-alpha", REPO_A)
    pb = register(plane, "t27-beta", REPO_B)
    ctx = ProjectContextStore(store)

    ctx.add_context_entry(pa["id"], {
        "kind": "constraints", "title": "no force push",
        "body": "ALPHA-ONLY: never force push to main.",
        "verification": "verified", "security_domain": "local",
        "source": "human", "tags": ["git"]})
    ctx.add_context_entry(pb["id"], {
        "kind": "constraints", "title": "no force push",
        "body": "BETA-ONLY: never force push to main either.",
        "verification": "verified", "security_domain": "local",
        "source": "human", "tags": ["git"]})

    builder = ContextBuilder(store)
    built_a = builder.build(pa["id"], "add a feature to alpha")
    built_b = builder.build(pb["id"], "add a feature to beta")

    # cross-project read must fail loudly
    entry_a = ctx.list_entries(pa["id"])[0]
    try:
        ctx.get_entry(pb["id"], entry_a["id"])
        cross_read = "ALLOWED (bad)"
    except ContextError:
        cross_read = "refused"

    # same title in two projects is fine; same title twice in one project is not
    try:
        ctx.add_context_entry(pa["id"], {
            "kind": "constraints", "title": "no force push",
            "body": "duplicate", "verification": "candidate"})
        duplicate = "ALLOWED (bad)"
    except ContextError:
        duplicate = "refused"

    store.close()
    return {
        "alpha_prompt_has_alpha_only": "ALPHA-ONLY" in built_a.effective_prompt,
        "alpha_prompt_has_beta_only": "BETA-ONLY" in built_a.effective_prompt,
        "beta_prompt_has_beta_only": "BETA-ONLY" in built_b.effective_prompt,
        "beta_prompt_has_alpha_only": "ALPHA-ONLY" in built_b.effective_prompt,
        "cross_project_read": cross_read,
        "duplicate_title_same_project": duplicate,
        "same_title_two_projects": "allowed",
        "pass": ("ALPHA-ONLY" in built_a.effective_prompt
                 and "BETA-ONLY" not in built_a.effective_prompt
                 and "BETA-ONLY" in built_b.effective_prompt
                 and "ALPHA-ONLY" not in built_b.effective_prompt
                 and cross_read == "refused"
                 and duplicate == "refused"),
    }


# --------------------------------------------------------------------------- #
# B: versioning / CAS / restart
# --------------------------------------------------------------------------- #
def case_b_versioning() -> dict:
    db = ROOT / ".poc-state" / "t27-b.db"
    for stale in [db, *db.parent.glob("t27-b.db.*")]:
        stale.unlink(missing_ok=True)
    store = TaskStore(db)
    plane = ControlPlane(StubAdapter(), store, policy=LOW)
    project = register(plane, "t27-version", REPO_A)
    ctx = ProjectContextStore(store)

    entry = ctx.add_context_entry(project["id"], {
        "kind": "knowledge", "title": "build command",
        "body": "run `make build`", "verification": "candidate",
        "source": "agent:session-1"})
    base_version = entry["version"]

    results: list[dict] = []
    barrier = threading.Barrier(2)

    def writer(body: str) -> None:
        barrier.wait(timeout=10)
        try:
            updated = ctx.update_context_entry(
                project["id"], entry["id"], base_version, {"body": body},
                changed_by="thread")
            results.append({"ok": True, "body": updated["body"],
                            "version": updated["version"]})
        except ContextVersionConflict as exc:
            results.append({"ok": False, "expected": exc.expected,
                            "actual": exc.actual})

    threads = [threading.Thread(target=writer, args=(f"body-{i}",)) for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [r for r in results if r["ok"]]
    losers = [r for r in results if not r["ok"]]
    final = ctx.get_entry(project["id"], entry["id"])
    revisions = ctx.revisions(project["id"], entry["id"])
    version_before_restart = final["version"]
    store.close()

    # restart: a brand-new process over the same file must see the same version
    reopened = TaskStore(db)
    ctx2 = ProjectContextStore(reopened)
    after_restart = ctx2.get_entry(project["id"], entry["id"])
    project_version_after_restart = reopened.context_version(project["id"])
    reopened.close()

    return {
        "concurrent_writers": len(results),
        "winners": len(winners),
        "losers": len(losers),
        "loser_reports_conflict": all("actual" in r for r in losers),
        "final_body": final["body"],
        "final_version": final["version"],
        "revision_rows": len(revisions),
        "revision_holds_previous_body": any(
            r["snapshot"].get("body") == "run `make build`" for r in revisions),
        "version_survives_restart": after_restart["version"] == version_before_restart,
        "project_version_after_restart": project_version_after_restart,
        "pass": (len(winners) == 1 and len(losers) == 1
                 and all("actual" in r for r in losers)
                 and final["version"] > base_version
                 and len(revisions) == 1
                 and any(r["snapshot"].get("body") == "run `make build`"
                         for r in revisions)
                 and after_restart["version"] == version_before_restart
                 and project_version_after_restart > 0),
    }


# --------------------------------------------------------------------------- #
# C: provenance
# --------------------------------------------------------------------------- #
def case_c_provenance() -> dict:
    store = fresh_store("c")
    plane = ControlPlane(StubAdapter(), store, policy=LOW)
    project = register(plane, "t27-prov", REPO_A)
    ctx = ProjectContextStore(store)

    entry = ctx.add_context_entry(project["id"], {
        "kind": "knowledge", "title": "flaky test",
        "body": "test_x fails on cold cache", "verification": "candidate",
        "source": "agent:session-42",
        "evidence_refs": ["evidence/jobs/T27-c.json"]})
    verified = ctx.verify_entry(
        project["id"], entry["id"], entry["version"],
        evidence_refs=["evidence/jobs/T27-c.json"], changed_by="human:owner")

    try:
        ctx.verify_entry(project["id"], entry["id"], verified["version"],
                         evidence_refs=[], changed_by="agent:guess")
        no_evidence = "ALLOWED (bad)"
    except ContextError:
        no_evidence = "refused"

    store.close()
    return {
        "source_recorded": entry["source"],
        "evidence_refs": entry["evidence_refs"],
        "created_at_set": bool(entry["created_at"]),
        "verified_state": verified["verification"],
        "verify_without_evidence": no_evidence,
        "pass": (entry["source"] == "agent:session-42"
                 and entry["evidence_refs"] == ["evidence/jobs/T27-c.json"]
                 and bool(entry["created_at"])
                 and verified["verification"] == "verified"
                 and no_evidence == "refused"),
    }


# --------------------------------------------------------------------------- #
# D: security domain / verification gating
# --------------------------------------------------------------------------- #
def case_d_security() -> dict:
    store = fresh_store("d")
    plane = ControlPlane(StubAdapter(), store, policy=LOW)
    project = register(plane, "t27-sec", REPO_A)
    ctx = ProjectContextStore(store)

    candidate = ctx.add_context_entry(project["id"], {
        "kind": "constraints", "title": "maybe no network",
        "body": "agent suspects network access is forbidden",
        "verification": "candidate", "security_domain": "local",
        "source": "agent:session-7"})
    verified = ctx.add_context_entry(project["id"], {
        "kind": "constraints", "title": "no secrets in repo",
        "body": "never commit credentials",
        "verification": "verified", "security_domain": "local",
        "source": "human:owner"})
    shareable = ctx.add_context_entry(project["id"], {
        "kind": "background", "title": "public summary",
        "body": "this project is a demo",
        "verification": "verified", "security_domain": "shareable",
        "source": "human:owner"})

    builder = ContextBuilder(store)
    built = builder.build(project["id"], "implement the feature")
    local_only = ctx.get_project_context(project["id"], include_shareable=False)
    local_ids = {e["id"] for e in local_only["entries"]}

    store.close()
    return {
        "candidate_is_hard_constraint": is_hard_constraint(candidate),
        "verified_is_hard_constraint": is_hard_constraint(verified),
        "candidate_labelled_in_prompt": "[CANDIDATE]" in built.effective_prompt,
        "verified_not_labelled": "no secrets in repo: never commit credentials"
                                 in built.effective_prompt,
        "shareable_excluded_from_local_only": shareable["id"] not in local_ids,
        "local_entries_present_in_local_only": {candidate["id"], verified["id"]} <= local_ids,
        "pass": (is_hard_constraint(candidate) is False
                 and is_hard_constraint(verified) is True
                 and "[CANDIDATE]" in built.effective_prompt
                 and "no secrets in repo: never commit credentials"
                     in built.effective_prompt
                 and shareable["id"] not in local_ids
                 and {candidate["id"], verified["id"]} <= local_ids),
    }


def main() -> int:
    log = {
        "test": "T27-project-context",
        "A_isolation": case_a_isolation(),
        "B_versioning": case_b_versioning(),
        "C_provenance": case_c_provenance(),
        "D_security_domain": case_d_security(),
    }
    keys = ("A_isolation", "B_versioning", "C_provenance", "D_security_domain")
    log["pass"] = {k: log[k]["pass"] for k in keys}
    log["result"] = "Pass" if all(log["pass"].values()) else "Fail"
    write_evidence(ROOT / "evidence" / "project" / "T27-project-context.json", log)
    print(json.dumps(log, ensure_ascii=False, indent=1)[:4000])
    print("RESULT:", log["result"])
    return 0 if log["result"] == "Pass" else 1


if __name__ == "__main__":
    sys.exit(main())
