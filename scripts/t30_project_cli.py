"""T30: end-to-end CLI walkthrough of the project-centric surface (M1).

This is the script the README Quick Start is copied from — if a documented
command stops working, this test fails.

Flow (all against a throw-away ledger, no worker is started):
    project add                 -> register a Git project
    project context-add x3      -> background, hard constraint, candidate knowledge
    project context             -> read back the stored knowledge
    project submit --preview    -> show the effective prompt without enqueueing
    project submit              -> enqueue the frozen prompt
    provenance                  -> original task + effective prompt + refs
    project brief / progress    -> deterministic LD-facing projections
    project context-update      -> CAS update; stale version must fail
    project submit --preview    -> a *new* build sees the new version
    provenance                  -> the already-queued task is untouched

The task is never dispatched, so no sandbox repository is modified.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sanitize_evidence import write_evidence  # noqa: E402

CLI = ROOT / "scripts" / "controlplane_cli.py"
REPO = ROOT / "sandbox" / "repo-a"
LEDGER = ROOT / ".poc-state" / "t30-cli.db"


def cli(*args: str, expect_ok: bool = True) -> dict:
    """Run one CLI command and return its parsed JSON (plus the exit code)."""
    proc = subprocess.run(
        [sys.executable, str(CLI), "--ledger", str(LEDGER), "--compact", *args],
        capture_output=True, text=True, timeout=120,
        cwd=str(ROOT), encoding="utf-8", errors="replace")
    out = (proc.stdout or "").strip()
    try:
        payload = json.loads(out) if out else {}
    except json.JSONDecodeError:
        payload = {"_raw": out, "_stderr": (proc.stderr or "").strip()}
    payload["_exit"] = proc.returncode
    if expect_ok and proc.returncode != 0:
        raise RuntimeError(f"command failed {args}: {payload}")
    return payload


def fresh() -> None:
    for stale in [LEDGER, *LEDGER.parent.glob(LEDGER.name + ".*")]:
        stale.unlink(missing_ok=True)


def main() -> int:
    fresh()
    log: dict = {"test": "T30-project-cli", "ledger": str(LEDGER.relative_to(ROOT))}

    project = cli("project", "add", "cli-demo", str(REPO),
                  "--mode", "agent", "--isolation", "worktree")
    pid = project["id"]
    log["project_add"] = {"id": pid, "root": project["root"],
                          "active": project["active"]}

    bg = cli("project", "context-add", pid, "--kind", "background",
             "--title", "purpose",
             "--body", "Sandbox repository used by the control-plane tests.",
             "--verification", "verified", "--source", "human:owner")
    constraint = cli("project", "context-add", pid, "--kind", "constraints",
                     "--title", "no network",
                     "--body", "Do not add network calls to this repository.",
                     "--verification", "verified", "--source", "human:owner",
                     "--tag", "safety")
    knowledge = cli("project", "context-add", pid, "--kind", "knowledge",
                    "--title", "test command",
                    "--body", "Run `python -m pytest` before claiming success.",
                    "--verification", "candidate", "--source", "agent:session-1")
    log["context_add"] = {
        "background": {"id": bg["id"], "version": bg["version"]},
        "constraint": {"id": constraint["id"], "version": constraint["version"]},
        "knowledge": {"id": knowledge["id"], "version": knowledge["version"]},
    }

    context = cli("project", "context", pid)
    log["context_read"] = {
        "version": context["version"],
        "entry_count": len(context["entries"]),
        "kinds": sorted(k for k, v in context["by_kind"].items() if v),
    }

    preview = cli("project", "submit", pid, "add a health endpoint", "--preview")
    log["submit_preview"] = {
        "context_version": preview["context_version"],
        "entry_refs": preview["context_entry_refs"],
        "blocked": preview["blocked"],
        "has_background": "Sandbox repository used by" in preview["effective_prompt"],
        "has_constraint": "Do not add network calls" in preview["effective_prompt"],
        "has_task": "add a health endpoint" in preview["effective_prompt"],
        "candidate_labelled": "[CANDIDATE]" in preview["effective_prompt"],
    }

    submitted = cli("project", "submit", pid, "add a health endpoint",
                    "--title", "cli health endpoint",
                    "--idempotency-key", "t30-cli-1",
                    "--tag", "safety")
    task_id = submitted["task_id"]
    log["submit"] = {
        "task_id": task_id,
        "queue_status": submitted["queue_status"],
        "context_version": submitted["context_version"],
        "workspace_id": submitted["workspace_id"],
        "has_effective_prompt": bool(submitted["effective_prompt"]),
    }

    # idempotent re-submit returns the same task and the same frozen prompt
    again = cli("project", "submit", pid, "add a health endpoint",
                "--idempotency-key", "t30-cli-1")
    log["resubmit_dedup"] = {"same_task": again["task_id"] == task_id,
                             "deduplicated": bool(again.get("deduplicated"))}

    prov = cli("provenance", task_id)
    log["provenance"] = {
        "original_task": prov["original_task"],
        "context_version": prov["context_version"],
        "ref_count": len(prov["context_entry_refs"]),
        "resolved_entries": len(prov["context_entries"]),
        "effective_equals_preview": prov["effective_prompt"] == preview["effective_prompt"],
    }

    brief = cli("project", "brief", pid)
    progress = cli("project", "progress", pid)
    log["brief"] = {"context_version": brief["context_version"],
                    "verified_count": brief["verified_count"],
                    "tasks": brief["tasks"],
                    "workspaces": len(brief["workspaces"]),
                    "workspace_executable": brief["workspaces"][0]["executable"]}
    log["progress"] = {"derived_from": progress["derived_from"],
                       "counts": progress["counts"],
                       "blocked_tasks": len(progress["blocked_tasks"])}

    # CAS: the current version wins, a stale version must be refused
    updated = cli("project", "context-update", pid, constraint["id"],
                  "--expected-version", str(constraint["version"]),
                  "--body", "Do not add network calls. Also: no new dependencies.",
                  "--note", "tightened")
    stale = cli("project", "context-update", pid, constraint["id"],
                "--expected-version", str(constraint["version"]),
                "--body", "stale write", expect_ok=False)
    log["cas"] = {"new_version": updated["version"],
                  "stale_exit": stale["_exit"],
                  "stale_error": str(stale.get("error", ""))[:100]}

    re_preview = cli("project", "submit", pid, "add a health endpoint", "--preview")
    log["rebuild_sees_new_version"] = {
        "context_version": re_preview["context_version"],
        "has_tightened_body": "no new dependencies" in re_preview["effective_prompt"],
        "version_advanced": re_preview["context_version"] > preview["context_version"],
    }

    # the already-queued task must still hold the prompt frozen at submit time
    prov_after = cli("provenance", task_id)
    log["queued_prompt_frozen"] = {
        "unchanged": prov_after["effective_prompt"] == prov["effective_prompt"],
        "has_tightened_body": "no new dependencies" in prov_after["effective_prompt"],
    }

    # a missing prerequisite is reported as a blocking gap (exit code 3)
    blocked = cli("project", "submit", pid, "deploy it",
                  "--prerequisite", "deploy-credentials", expect_ok=False)
    log["blocked_prerequisite"] = {
        "exit": blocked["_exit"],
        "gaps": blocked.get("gaps", []),
        "error": str(blocked.get("error", ""))[:100],
    }

    checks = {
        "project_registered": log["project_add"]["active"] is True,
        "context_stored": log["context_read"]["entry_count"] == 3,
        "preview_injects_context": (log["submit_preview"]["has_background"]
                                    and log["submit_preview"]["has_constraint"]
                                    and log["submit_preview"]["has_task"]),
        "candidate_label_visible": log["submit_preview"]["candidate_labelled"],
        "submitted_enqueued": log["submit"]["queue_status"] in ("ready", "needs_approval"),
        "resubmit_deduplicated": (log["resubmit_dedup"]["same_task"]
                                  and log["resubmit_dedup"]["deduplicated"]),
        "provenance_traceable": (log["provenance"]["original_task"]
                                 == "add a health endpoint"
                                 and log["provenance"]["ref_count"] >= 2
                                 and log["provenance"]["resolved_entries"]
                                 == log["provenance"]["ref_count"]),
        "preview_matches_recorded_prompt": log["provenance"]["effective_equals_preview"],
        "progress_derived_from_ledger": log["progress"]["derived_from"] == "ledger",
        "workspace_is_local": log["brief"]["workspace_executable"] is True,
        "cas_applied": log["cas"]["new_version"] > constraint["version"],
        "cas_refuses_stale": log["cas"]["stale_exit"] != 0,
        "new_build_sees_update": log["rebuild_sees_new_version"]["has_tightened_body"],
        "queued_prompt_frozen": (log["queued_prompt_frozen"]["unchanged"]
                                 and not log["queued_prompt_frozen"]["has_tightened_body"]),
        "blocked_prerequisite_reported": (log["blocked_prerequisite"]["exit"] == 3
                                          and bool(log["blocked_prerequisite"]["gaps"])),
    }
    log["checks"] = checks
    log["pass"] = checks
    log["result"] = "Pass" if all(checks.values()) else "Fail"
    write_evidence(ROOT / "evidence" / "project" / "T30-project-cli.json", log)
    print(json.dumps({k: log[k] for k in ("project_add", "submit_preview", "submit",
                                          "provenance", "cas", "blocked_prerequisite",
                                          "queued_prompt_frozen", "result")},
                     ensure_ascii=False, indent=1)[:4000])
    print("failed checks:", [k for k, v in checks.items() if not v] or "none")
    print("RESULT:", log["result"])
    return 0 if log["result"] == "Pass" else 1


if __name__ == "__main__":
    sys.exit(main())
