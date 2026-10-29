"""T15b: which processes are born and die during one agent dispatch?

Polls the process table every 100ms (Toolhelp32) and reports every pid that
appeared during the window, with its lifetime and parent, plus any visible
window class it owned. This is the factual answer to "why do terminals flash".
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

kernel32 = ctypes.windll.kernel32
TH32CS_SNAPPROCESS = 0x2
MAX_PATH = 260


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_char * MAX_PATH)]


def snapshot() -> dict[int, tuple[str, int]]:
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    out: dict[int, tuple[str, int]] = {}
    if snap == -1:
        return out
    entry = PROCESSENTRY32()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
    ok = kernel32.Process32First(snap, ctypes.byref(entry))
    while ok:
        out[entry.th32ProcessID] = (entry.szExeFile.decode("mbcs", "replace"),
                                    entry.th32ParentProcessID)
        ok = kernel32.Process32Next(snap, ctypes.byref(entry))
    kernel32.CloseHandle(snap)
    return out


def main() -> int:
    api = Api()
    api.login()
    cwd = str(ROOT / "sandbox" / "repo-a-t11")
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0

    before = snapshot()
    born: dict[int, dict] = {}
    t0 = time.time()
    dispatched = False
    while time.time() - t0 < seconds:
        now = snapshot()
        for pid, (name, ppid) in now.items():
            if pid not in before:
                rec = born.setdefault(pid, {"pid": pid, "name": name, "ppid": ppid,
                                            "parent": before.get(ppid, ("?", 0))[0],
                                            "first": round(time.time() - t0, 2),
                                            "last": None})
                rec["last"] = round(time.time() - t0, 2)
        if not dispatched and time.time() - t0 >= 1.0:
            disp = api.call("POST", "/api/v1/jobs", {
                "prompt": "在这个仓库根目录创建 t15b_agent.txt，内容一行 t15b，然后结束。",
                "cwd": cwd, "permissionMode": "acceptEdits", "bgIsolation": "worktree",
                "name": "T15b-agent"})
            dispatched = True
        time.sleep(0.1)

    rows = sorted(born.values(), key=lambda r: r["first"])
    for r in rows:
        r["lifetime_s"] = round((r["last"] or 0) - r["first"], 2)
    summary: dict[str, int] = {}
    for r in rows:
        summary[r["name"]] = summary.get(r["name"], 0) + 1
    short_lived = [r for r in rows if r["lifetime_s"] <= 3]

    log = {"test": "T15b-process-flash", "cwd": cwd, "window_s": seconds,
           "dispatch_http": disp["status"] if dispatched else None,
           "born_count": len(rows), "by_exe": summary,
           "short_lived_count": len(short_lived),
           "processes": rows}
    write_evidence(ROOT / "evidence" / "environment" / "T15b-process-flash.json", log)

    print(f"dispatch http={log['dispatch_http']} born={len(rows)} short_lived={len(short_lived)}")
    print("by exe:", json.dumps(summary, ensure_ascii=False))
    for r in rows[:25]:
        print(f"  {r['first']:>6}s-{r['last']:>6}s ({r['lifetime_s']:>5}s) "
              f"{r['name']:<22} pid={r['pid']:<7} parent={r['parent']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
