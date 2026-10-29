"""Create a fresh pair of throwaway Git repos for a test run.

Why unique names instead of "delete and recreate": on this machine the desktop
shell routes deletions through a trash shim that refuses to remove repos once a
worker has created a `.codebuddy/` worktree inside them (see
evidence/jobs/ENV-DEFECT.md). Creating `repo-a-<suffix>` keeps every run
isolated without needing to delete anything.

The chosen paths are written to `.poc-state/sandbox.json` so tests can pick them
up instead of hard-coding repository names.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SANDBOX = ROOT / "sandbox"
STATE = ROOT / ".poc-state" / "sandbox.json"


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def _force_rmtree(target: Path) -> None:
    def on_error(func, path, exc_info):      # noqa: ANN001 - shutil callback
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except Exception:
            pass

    shutil.rmtree(target, onerror=on_error)


def make_repo(target: Path) -> dict:
    if target.exists():
        _force_rmtree(target)
    target.mkdir(parents=True)
    (target / "README.md").write_text(f"# sandbox {target.name}\n\nPoC 临时仓库，可随时重建。\n",
                                     encoding="utf-8")
    git(target, "init", "-q", "-b", "main")
    git(target, "config", "user.name", "poc")
    git(target, "config", "user.email", "poc@local")
    git(target, "add", ".")
    git(target, "commit", "-q", "-m", f"init {target.name}")
    head = subprocess.run(["git", "-C", str(target), "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    return {"path": str(target), "head": head}


def main() -> int:
    SANDBOX.mkdir(exist_ok=True)
    suffix = sys.argv[1] if len(sys.argv) > 1 else time.strftime("%m%d-%H%M%S")
    out = {"created_at": time.time(), "suffix": suffix}
    for key, name in (("repo_a", "repo-a"), ("repo_b", "repo-b")):
        info = make_repo(SANDBOX / f"{name}-{suffix}")
        out[key] = info["path"]
        print(f"{name}-{suffix}: head={info['head']}")
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"sandbox state -> {STATE}")
    return 0


def load() -> dict:
    if not STATE.exists():
        raise SystemExit("no sandbox state; run scripts/make_sandbox.py first")
    return json.loads(STATE.read_text(encoding="utf-8"))


if __name__ == "__main__":
    sys.exit(main())
