"""Run the controlled memory contrast for T62 stage 2 (C1 vs C0).

Both arms are byte-identical except for project memory:
  * a fresh copy of the same commit,
  * the same task string and assertion list,
  * submitted through ProjectService.submit_project_task (the entry point the LD
    tool itself calls), not through the decision model - because the model
    paraphrased two identical goals into different task texts (T62-F01),
    which would make memory an unattributable variable.

Usage:
    python scripts/t62_stage2_contrast.py setup    # build copies, register, enqueue
    python scripts/t62_stage2_contrast.py run      # start the resident service
    python scripts/t62_stage2_contrast.py status   # poll until both are terminal
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(ROOT / "scripts"))
# `remove_scratch_ledger` is intentionally NOT imported here any more: setup()
# always allocates a new ledger, because "is this scratch?" is the wrong
# question - queue_status='dispatching' coexists with state='queued'. The guard
# still exists in t62_sqlite_cleanup as the single sanctioned deletion path.
from t62_sqlite_cleanup import (  # noqa: E402
    fresh_ledger_path,
    wal_size,
)

STATE = ROOT / ".poc-state" / "t62"
LEDGER = STATE / "stage2c.db"
PILOT_LEDGER = STATE / "stage2.db"
ACTIVE_LEDGER_FILE = STATE / "contrast-active-ledger.txt"


class NoActiveLedger(RuntimeError):
    """There is no current ledger to run against or report on."""


def active_ledger() -> Path:
    """The ledger this round is actually using. Fails closed.

    Every round now allocates a new file, so falling back to the historical
    `stage2c.db` would silently point `run()` at a finished experiment - and
    `status()` would then report on old data as if it were this round's. A
    missing pointer, an unreadable pointer, or a pointer to a file that no longer
    exists is an error, not a default.
    """
    if not ACTIVE_LEDGER_FILE.exists():
        raise NoActiveLedger(
            "no active-ledger pointer: run `setup` for this round before `run` "
            "or `status`. There is deliberately no fallback to a historical "
            f"ledger ({LEDGER.name}).")
    raw = ACTIVE_LEDGER_FILE.read_text(encoding="utf-8").strip()
    if not raw:
        raise NoActiveLedger(f"{ACTIVE_LEDGER_FILE.name} is empty; re-run `setup`")
    candidate = Path(raw)
    if not candidate.exists():
        raise NoActiveLedger(
            f"active-ledger pointer targets {candidate.name}, which does not "
            "exist; re-run `setup` rather than falling back to an old ledger")
    return candidate


SRC = STATE / "pilot-work"
BASE = "http://127.0.0.1:8787"
COMMIT = "f882885"
SOURCE_PROJECT = "prj-efe109f94e"
SOURCE_ENTRY = "ctx-ec635ddbab33"

ARM_MEM = os.environ.get("T62_ARM_MEM", "c1")
ARM_NOMEM = os.environ.get("T62_ARM_NOMEM", "c0")


def arm_tags() -> tuple[str, str]:
    return ARM_MEM, ARM_NOMEM


TASK = (
    "Read tests/test_metrics_space_ambiguity.py and change "
    "src/gui_post_training/evaluation/metrics.py so that every test in that file "
    "passes and tests/test_metrics.py and tests/test_parsing.py do not regress. "
    "When a numeric pair is valid under both the pixel and the normalized_1000 "
    "reading, both the per-sample record and the aggregate result must make it "
    "visible that the decision rests on a convention rather than on the data. "
    "Commit all changes with git: if you are on main or master, create and switch "
    "to a new feature branch first. Never push, and never commit to or modify "
    "main or master."
)

ASSERTIONS = [
    {"kind": "file_exists",
     "target": "src/gui_post_training/evaluation/metrics.py"},
    {"kind": "file_contains",
     "target": "src/gui_post_training/evaluation/metrics.py",
     "value": "normalized_1000"},
    {"kind": "file_contains",
     "target": "src/gui_post_training/evaluation/metrics.py",
     "value": "pixel"},
    {"kind": "file_contains",
     "target": "src/gui_post_training/evaluation/metrics.py",
     "value": "ambigu"},
]


def git(*args, cwd=None, check=True):
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, timeout=180)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {args}: {proc.stderr[-400:]}")
    return proc.stdout.strip()


def build_copy(tag: str) -> Path:
    origin = STATE / f"origin-{tag}.git"
    work = STATE / f"work-{tag}"
    if work.exists():
        raise RuntimeError(f"{work} already exists; remove it first")
    subprocess.run(["git", "init", "-q", "--bare", "-b", "pilot/t62-memory",
                    str(origin)], check=True)
    git("remote", "add", "src", str(SRC), cwd=origin, check=False)
    git("fetch", "-q", "src", "pilot/t62-memory:refs/heads/pilot/t62-memory",
        cwd=origin)
    git("update-ref", "refs/heads/pilot/t62-memory", COMMIT, cwd=origin)
    git("symbolic-ref", "HEAD", "refs/heads/pilot/t62-memory", cwd=origin)
    git("clone", "-q", str(origin), str(work))
    git("config", "user.email", "t62@example.invalid", cwd=work)
    git("config", "user.name", "T62 Pilot", cwd=work)
    perms = json.dumps({"permissions": {"allow": ["Bash", "PowerShell"]}}, indent=1)
    for rel in (".claude/settings.json", ".codebuddy/settings.json"):
        target = work / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(perms, encoding="utf-8")
    return work


def setup() -> int:
    from adapters.codebuddy.adapter import CodeBuddyAdapter
    from adapters.codebuddy.client import Api
    from controlplane.plane import ControlPlane
    from controlplane.project_service import ProjectService
    from controlplane.store import TaskStore

    # Every round starts on a brand-new ledger. setup() never deletes one.
    #
    # Deciding "is this scratch?" turned out to be the wrong question: a task can
    # sit at state='queued'/'verdict=pending' while `claim_next_ready()` has
    # already moved queue_status to 'dispatching', so a row-level census on
    # (state, verdict) calls an in-flight ledger scratch. Rather than add more
    # state checks to a delete decision, the delete decision is removed: a new
    # path every round, and whatever was there stays as evidence. The guard in
    # t62_sqlite_cleanup stays as a hard stop for anyone else who tries.
    ledger = fresh_ledger_path(LEDGER)
    if LEDGER.exists():
        print(json.dumps({
            "ledger_reused": False,
            "next_ledger": ledger.name,
            "kept_as_evidence": LEDGER.name,
            "reason": "each round uses a fresh ledger; nothing is deleted",
        }, ensure_ascii=False))
    if wal_size(ledger):
        raise SystemExit(
            f"{ledger.name}-wal still holds {wal_size(ledger)} bytes; stop the "
            "runtime and Worker before starting a new pair")

    # `run` and `status` read this pointer, and they fail closed without it: with
    # a new filename per round there is no safe default to fall back to.
    ACTIVE_LEDGER_FILE.write_text(str(ledger), encoding="utf-8")
    # The archive module finds ledgers by scanning `stage2c*.db`, but an explicit
    # log means it never has to guess which file belongs to which round.
    with open(STATE / "contrast-ledgers.txt", "a", encoding="utf-8") as log:
        log.write(str(ledger) + "\n")

    adapter = CodeBuddyAdapter(Api(base=BASE), autologin=True)
    store = TaskStore(ledger)
    plane = ControlPlane(adapter, store, require_registered_projects=True,
                         worker_instance_id="wi-t62c")
    service = ProjectService(plane)

    src_store = TaskStore(PILOT_LEDGER)
    src_service = ProjectService(ControlPlane(
        adapter, src_store, require_registered_projects=True))

    out = {}
    try:
        mem_tag, nomem_tag = arm_tags()
        for tag in (mem_tag, nomem_tag):
            work = build_copy(tag)
            project = plane.register_project(
                f"gui-post-training-{tag}", str(work), default_adapter="codebuddy")
            pid = project["id"]
            entry = None
            if tag == mem_tag:
                src = src_service.context.get_entry(SOURCE_PROJECT, SOURCE_ENTRY)
                entry = service.add_context_entry(pid, {
                    "kind": src["kind"], "title": src["title"], "body": src["body"],
                    "tags": list(src.get("tags") or []),
                    "verification": "verified",
                    "security_domain": src["security_domain"],
                    "source": f"human:t62-owner (reviewed in {SOURCE_PROJECT})",
                    "evidence_refs": list(src["evidence_refs"]),
                })
            task = service.submit_project_task(
                pid, TASK, assertions=ASSERTIONS,
                idempotency_key=f"t62c-{tag}", title=f"t62 contrast {tag}")
            refs = task.get("context_entry_refs") or []
            if isinstance(refs, str):
                refs = json.loads(refs)
            out[tag] = {"project_id": pid, "task_id": task["id"],
                        "memory_entry": entry["id"] if entry else None,
                        "context_version": task.get("context_version"),
                        "context_entry_refs": refs}
    finally:
        store.close()
        src_store.close()
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


def run() -> int:
    # Resolve the ledger BEFORE opening the log or spawning anything: a missing
    # pointer must not start a Runtime pointed at an old experiment.
    try:
        ledger = active_ledger()
    except NoActiveLedger as exc:
        print(json.dumps({"runtime_pid": None, "result": "BLOCKED",
                          "reason": str(exc)}, ensure_ascii=False))
        return 2
    log = open(STATE / "contrast-runtime.log", "ab")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    argv = [
        sys.executable, str(ROOT / "scripts" / "run_controlplane.py"),
        "--ledger", str(ledger), "--base", BASE,
        "--known-interval", "5", "--dispatch-interval", "2",
        "--max-concurrent", "1", "--scan-on-start",
    ]
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
             if os.name == "nt" else 0)
    proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, cwd=str(ROOT), env=env,
                            creationflags=flags)
    print(json.dumps({"runtime_pid": proc.pid}))
    return 0


def status() -> int:
    """Ready only when exactly one memory arm and one control arm are terminal.

    `all(...)` over an empty list is True, which used to let a ledger that had
    silently reverted to queued (T62-F07) report "done". An empty or single-arm
    ledger is now BLOCKED, never done.
    """
    try:
        ledger = active_ledger()
    except NoActiveLedger as exc:
        print(json.dumps({"done": False, "result": "BLOCKED",
                          "reason": str(exc), "ledger": None},
                         ensure_ascii=False, indent=1))
        return 2
    if not ledger.exists():
        print(json.dumps({"done": False, "result": "BLOCKED",
                          "reason": f"{ledger.name} does not exist"},
                         ensure_ascii=False, indent=1))
        return 2
    conn = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True, timeout=20)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(
            "select id, project_id, state, verdict, context_version, "
            "context_entry_refs from work_items")]
    finally:
        conn.close()

    problems = []
    if len(rows) != 2:
        problems.append(f"expected exactly 2 arms, ledger holds {len(rows)}")
    with_mem = [r for r in rows
                if int(r.get("context_version") or 0) > 0
                or (r.get("context_entry_refs") or "[]") not in ("[]", "")]
    without = [r for r in rows if r not in with_mem]
    if len(with_mem) != 1:
        problems.append(f"expected exactly 1 memory arm, found {len(with_mem)}")
    if len(without) != 1:
        problems.append(f"expected exactly 1 control arm, found {len(without)}")
    ids = [r["id"] for r in rows]
    if len(ids) != len(set(ids)):
        problems.append("duplicate task id in ledger")
    unfinished = [r["id"] for r in rows
                  if r["state"] not in ("succeeded", "failed")
                  or r["verdict"] not in ("verified", "rejected")]
    if unfinished:
        problems.append(f"not terminal yet: {', '.join(unfinished)}")
    if len(rows) != 2 and not unfinished:
        problems.append("arm set is wrong even though both rows are terminal")

    done = not problems
    print(json.dumps({"done": done,
                      "result": "READY" if done else "BLOCKED",
                      "problems": problems,
                      "ledger": ledger.name,
                      "tasks": rows}, ensure_ascii=False, indent=1))
    return 0 if done else 3


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    raise SystemExit({"setup": setup, "run": run, "status": status}[action]())
