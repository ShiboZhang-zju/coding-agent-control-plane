"""Windows-safe child processes: never allocate a console window.

Why this exists
---------------
A console child process (git.exe, tasklist.exe, cmd.exe ...) allocates its own
console **if the parent has none**, and that console is a visible window. Our
detached runners (`DETACHED_PROCESS` / `CREATE_NO_WINDOW`) deliberately have no
console, so every `subprocess.run(["git", ...])` inside them used to flash a
window on the user's desktop (evidence/environment/CONSOLE-FLASH.md).

`run()` / `popen()` here always pass `CREATE_NO_WINDOW` on Windows, so the child
gets an invisible console. Behaviour (stdout/stderr) is unchanged.
"""
from __future__ import annotations

import os
import subprocess
from typing import Any, Sequence

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def _hidden_kwargs(extra_flags: int = 0) -> dict[str, Any]:
    if os.name != "nt":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0            # SW_HIDE
    return {"startupinfo": si,
            "creationflags": CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | extra_flags}


def run(args: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """subprocess.run with an invisible console; unknown kwargs pass through."""
    opts: dict[str, Any] = {"capture_output": True, "text": True}
    opts.update(kwargs)
    opts.update({k: v for k, v in _hidden_kwargs().items() if k not in opts})
    return subprocess.run(list(args), **opts)


def popen(args: Sequence[str], *, stdout=None, stderr=None, stdin=subprocess.DEVNULL,
          cwd: str | None = None, env: dict | None = None,
          extra_flags: int = 0, **kwargs: Any) -> subprocess.Popen:
    """subprocess.Popen with an invisible console."""
    opts: dict[str, Any] = {"stdout": stdout, "stderr": stderr, "stdin": stdin,
                            "cwd": cwd, "env": env, "close_fds": True}
    opts.update(kwargs)
    opts.update({k: v for k, v in _hidden_kwargs(extra_flags).items() if k not in opts})
    return subprocess.Popen(list(args), **opts)
