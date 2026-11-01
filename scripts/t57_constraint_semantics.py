"""T57: verified/candidate/unverified constraint prompt semantics.

Deterministic ContextBuilder test; no model or Worker is started.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.project_service import ProjectService  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

FIXTURE = ROOT / "sandbox" / "t57-constraint-semantics"
LEDGER = ROOT / ".poc-state" / "t57-constraint-semantics.db"
EVIDENCE = ROOT / "evidence" / "project" / "T57-constraint-semantics.json"


class AdapterRef:
    name = "stub"


def make_repo(path: Path, name: str) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email",
         "t57@example.invalid"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "T57"],
        check=True)
    (path / "seed.txt").write_text(f"{name}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "seed.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", "seed"],
        check=True)


def add(service: ProjectService, project_id: str, *, title: str,
        body: str, verification: str, tags: list[str]) -> dict:
    return service.add_context_entry(project_id, {
        "kind": "constraints", "title": title, "body": body,
        "verification": verification, "security_domain": "local",
        "source": "human:t57", "evidence_refs": (
            ["evidence/project/T57-constraint-semantics.json"]
            if verification == "verified" else []),
        "tags": tags,
    })


def main() -> int:
    shutil.rmtree(FIXTURE, ignore_errors=True)
    repo1 = FIXTURE / "verified"
    repo2 = FIXTURE / "candidate-only"
    make_repo(repo1, "verified")
    make_repo(repo2, "candidate-only")
    for stale in [LEDGER, *LEDGER.parent.glob(LEDGER.name + ".*")]:
        stale.unlink(missing_ok=True)
    store = TaskStore(LEDGER)
    plane = ControlPlane(AdapterRef(), store, require_registered_projects=True)
    service = ProjectService(plane)
    project = plane.register_project(
        "t57-constraints", str(repo1), default_adapter="stub")
    verified_body = "VERIFIED-FULL-" + ("must-remain-complete-" * 80)
    verified = add(
        service, project["id"], title="Verified deployment boundary",
        body=verified_body, verification="verified", tags=["verified-boundary"])
    candidate = add(
        service, project["id"], title="Candidate deployment note",
        body="CANDIDATE-NOT-A-RULE", verification="candidate",
        tags=["candidate-only"])
    unverified = add(
        service, project["id"], title="Unverified deployment note",
        body="UNVERIFIED-NOT-A-RULE", verification="unverified",
        tags=["unverified-only"])
    built = service.build_task_context(
        project["id"], "Prepare deployment", budget_chars=200,
        require_verified_constraints=True)
    prompt = built.effective_prompt
    hard_start = prompt.find("# VERIFIED HARD CONSTRAINTS")
    notes_start = prompt.find("# UNVERIFIED CONSTRAINTS / NOTES")
    hard_text = prompt[hard_start:notes_start] if (
        hard_start >= 0 and notes_start > hard_start) else prompt
    all_refs = [entry_id for ids in built.sections.values() for entry_id in ids]

    project2 = plane.register_project(
        "t57-candidate-only", str(repo2), default_adapter="stub")
    add(service, project2["id"], title="Only candidate constraint",
        body="not verified", verification="candidate", tags=["candidate-only"])
    candidate_only = service.build_task_context(
        project2["id"], "Prepare deployment",
        require_verified_constraints=True,
        prerequisites=["candidate-only"])

    checks = {
        "verified_header_is_explicit": hard_start >= 0,
        "unverified_header_is_separate": notes_start > hard_start >= 0,
        "verified_constraint_is_complete_despite_budget": verified_body in prompt,
        "candidate_not_in_hard_section": "CANDIDATE-NOT-A-RULE" not in hard_text,
        "unverified_not_in_hard_section": "UNVERIFIED-NOT-A-RULE" not in hard_text,
        "candidate_is_marked_untrusted": (
            "Candidate deployment note [CANDIDATE]" in prompt
            and "not mandatory" in prompt),
        "unverified_is_marked_untrusted": (
            "Unverified deployment note [UNVERIFIED]" in prompt),
        "constraints_are_not_duplicated": (
            len(all_refs) == len(set(all_refs))
            and all_refs.count(verified["id"]) == 1
            and all_refs.count(candidate["id"]) == 1
            and all_refs.count(unverified["id"]) == 1),
        "verified_constraint_satisfies_requirement": not built.blocked,
        "candidate_constraint_does_not_satisfy_requirement": (
            candidate_only.blocked
            and any("no verified constraint" in gap
                    for gap in candidate_only.gaps)),
        "candidate_tag_does_not_satisfy_prerequisite": (
            any("candidate-only" in gap for gap in candidate_only.gaps)),
    }
    failed = [name for name, passed in checks.items() if not passed]
    evidence = {
        "test": "T57 constraint verification semantics",
        "worker_started": False,
        "real_model_called": False,
        "fixture": "<REPO>/sandbox/t57-constraint-semantics",
        "checks": checks,
        "result": "PASS" if not failed else "FAIL",
        "failed": failed,
    }
    write_evidence(EVIDENCE, evidence)
    store.close()
    print(json.dumps(
        {"result": evidence["result"], "failed": failed},
        ensure_ascii=False))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
