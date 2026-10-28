"""(Re)create the two throwaway Git repos used by the PoC. Never touches anything
outside `sandbox/`."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SANDBOX = ROOT / "sandbox"
REPOS = ("repo-a", "repo-b")


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def main() -> int:
    SANDBOX.mkdir(exist_ok=True)
    for name in REPOS:
        target = SANDBOX / name
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        (target / "README.md").write_text(f"# sandbox {name}\n\nPoC 临时仓库，随时可重建。\n",
                                          encoding="utf-8")
        git(target, "init", "-q", "-b", "main")
        git(target, "config", "user.name", "poc")
        git(target, "config", "user.email", "poc@local")
        git(target, "add", ".")
        git(target, "commit", "-q", "-m", f"init {name}")
        head = subprocess.run(["git", "-C", str(target), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        print(f"{name}: head={head}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
