"""T15d: prove which subprocess calls still create visible consoles.

Runs from a *detached* (console-less) process, like our real test runs. It samples
visible top-level windows while spawning git.exe twice:
  * via winproc.run  -> must create 0 windows
  * via raw subprocess.run -> the old behaviour, expected to flash
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import winproc  # noqa: E402

user32 = ctypes.windll.user32
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def windows() -> set[tuple[str, int]]:
    found: set[tuple[str, int]] = set()

    def cb(hwnd, _l):
        if user32.IsWindowVisible(hwnd):
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            found.add((cls.value, pid.value))
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found


def main() -> int:
    log = {"test": "T15d-self-flash", "detached": os.environ.get("POC_HIDDEN_DESKTOP") is not None}

    baseline = windows()
    seen: dict[tuple[str, int], dict] = {}
    stop = threading.Event()

    def sample() -> None:
        while not stop.is_set():
            for key in windows() - baseline:
                seen.setdefault(key, {"class": key[0], "pid": key[1]})
            time.sleep(0.05)

    thread = threading.Thread(target=sample, daemon=True)
    thread.start()
    time.sleep(0.5)

    # 1) hidden helper: should be invisible
    for _ in range(6):
        winproc.run(["git", "--version"], timeout=20)
    time.sleep(0.6)
    hidden_count = len(seen)
    hidden_windows = [dict(v, phase="hidden") for v in seen.values()]

    # 2) raw subprocess: the old, flashing behaviour
    seen.clear()
    for _ in range(6):
        subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=20)
    time.sleep(0.6)
    raw_count = len(seen)
    raw_windows = [dict(v, phase="raw") for v in seen.values()]

    stop.set()
    thread.join(timeout=2)

    log.update({"windows_from_hidden_calls": hidden_count,
                "windows_from_raw_calls": raw_count,
                "samples": hidden_windows + raw_windows,
                "pass": hidden_count == 0})
    (ROOT / "evidence" / "environment").mkdir(parents=True, exist_ok=True)
    (ROOT / "evidence" / "environment" / "T15d-self-flash.json").write_text(
        json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"hidden": hidden_count, "raw": raw_count, "pass": log["pass"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
