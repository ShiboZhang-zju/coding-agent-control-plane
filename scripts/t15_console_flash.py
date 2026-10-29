"""T15: who creates the console windows that flash on screen during a dispatch?

Samples visible top-level windows (class + pid) every 0.4s while dispatching one
shell job and one agent job, then reports which window classes appeared and how
long they stayed. Evidence only — it does not change anything.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.codebuddy.client import Api, write_evidence  # noqa: E402

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def exe_name(pid: int) -> str:
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return "?"
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
        return "?"
    finally:
        kernel32.CloseHandle(h)


def visible_windows() -> dict[tuple[str, int], dict]:
    found: dict[tuple[str, int], dict] = {}

    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        found[(cls.value, pid.value)] = {"class": cls.value, "title": buf.value,
                                         "pid": pid.value}
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found


def probe(api: Api, label: str, prompt: str, cwd: str, seconds: float,
          bash: bool = False) -> dict:
    before = visible_windows()
    seen: dict[tuple[str, int], dict] = {}
    t0 = time.time()
    disp = None
    first = True
    while time.time() - t0 < seconds:
        now = visible_windows()
        for key in set(now) - set(before):
            entry = seen.setdefault(key, dict(now[key], exe=exe_name(key[1]),
                                              first_seen=round(time.time() - t0, 2),
                                              last_seen=None))
            entry["last_seen"] = round(time.time() - t0, 2)
        if first and time.time() - t0 >= 1.0:
            body = {"prompt": prompt, "cwd": cwd, "name": f"T15-{label}"}
            if bash:
                body["bash"] = True
            else:
                body["permissionMode"] = "acceptEdits"
                body["bgIsolation"] = "worktree"
            disp = api.call("POST", "/api/v1/jobs", body)
            first = False
        time.sleep(0.4)
    return {"label": label, "bash": bash, "dispatch_http": (disp or {}).get("status"),
            "windows": list(seen.values()),
            "count": len(seen)}


def main() -> int:
    api = Api()
    api.login()
    log = {"test": "T15-console-flash", "started_at": time.time(), "probes": []}
    log["baseline_window_classes"] = sorted({v["class"] for v in visible_windows().values()})

    log["probes"].append(probe(
        api, "shell", "timeout /t 5 /nobreak >nul & echo t15 > t15.txt",
        str(ROOT / "sandbox" / "repo-a-t11"), 12, bash=True))
    log["probes"].append(probe(
        api, "agent", "在这个仓库根目录创建 t15_agent.txt，内容一行 t15，然后结束。",
        str(ROOT / "sandbox" / "repo-a-t11"), 35))

    for p in log["probes"]:
        p["window_classes"] = sorted({win["class"] for win in p["windows"]})
    write_evidence(ROOT / "evidence" / "environment" / "T15-console-flash.json", log)
    for p in log["probes"]:
        print(f"== {p['label']} (http {p['dispatch_http']}) windows={p['count']}")
        for win in p["windows"]:
            print(f"   {win['first_seen']:>5}s-{win['last_seen']:>5}s  "
                  f"{win['class']:<32} pid={win['pid']:<6} {win['title'][:50]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
