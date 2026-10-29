"""Strip machine-specific absolute paths from committed evidence.

Only path/username substitution: no values, timings, states or verdicts change.
Run before committing; the manifest of replacements is printed and stored in
`evidence/SANITIZATION.md`.
"""
from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVID = ROOT / "evidence"
HOME = os.path.expanduser("~")


def patterns() -> list[tuple[str, str]]:
    home_json = json.dumps(str(Path(HOME)))[1:-1]        # C:\\Users\\<name>
    repo_json = json.dumps(str(ROOT))[1:-1]              # F:\\coding-agent-control-plane
    return [
        (home_json, "<HOME>"),
        (str(Path(HOME)), "<HOME>"),
        (repo_json, "<REPO>"),
        (str(ROOT), "<REPO>"),
        (str(ROOT).replace("\\", "/"), "<REPO>"),
    ]


def regexes() -> list[tuple[str, str]]:
    """Escaped paths embedded in nested strings (e.g. a worker error text inside JSON).

    The generic ``/Users/<name>`` / ``C:\\Users\\<name>`` fallbacks catch evidence
    produced on a *different* machine (e.g. a macOS build host whose username is
    not the local one) — exact HOME substitution alone missed those (review P1).
    """
    import re

    user = Path(HOME).name
    return [
        (rf"[A-Za-z]:\\+Users\\+{re.escape(user)}", "<HOME>"),
        (rf"[A-Za-z]:\\+Users\\+[A-Za-z0-9._-]+", "<HOME>"),
        (rf"/[A-Za-z]/Users/{re.escape(user)}", "<HOME>"),
        # any POSIX-style home, including JSON-escaped "\/Users\/<name>"
        (r"[/\\]+Users[/\\]+[A-Za-z0-9._-]+", "<HOME>"),
    ]


def sanitize_text(text: str) -> tuple[str, int]:
    """Apply every substitution to one text blob. Returns (text, hits)."""
    import re

    hits = 0
    for a, b in patterns():
        if a and a in text:
            hits += text.count(a)
            text = text.replace(a, b)
    for pattern, repl in regexes():
        text, n = re.subn(pattern, repl, text)
        hits += n
    return text, hits


def write_evidence(path: Path, data) -> None:
    """Serialize evidence and strip machine paths BEFORE the bytes hit disk.

    Test scripts should use this instead of a raw ``write_text(json.dumps(...))``
    so committed evidence never leaks a local home directory again.
    """
    text = json.dumps(data, ensure_ascii=False, indent=2)
    text, _ = sanitize_text(text)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main() -> int:
    changed_files, total = [], 0
    for path in sorted(EVID.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in (".json", ".md", ".txt", ".log"):
            continue
        original = io.open(path, encoding="utf-8", errors="replace").read()
        text, hits = sanitize_text(original)
        if text != original:
            io.open(path, "w", encoding="utf-8", newline="\n").write(text)
            changed_files.append({"file": str(path.relative_to(ROOT)), "replacements": hits})
            total += hits
    manifest = {
        "note": "绝对路径替换为占位符；其余内容（状态、时间、判定）未改动",
        "placeholders": {"<HOME>": "本机用户目录", "<REPO>": "仓库根目录"},
        "files_changed": len(changed_files), "total_replacements": total,
        "detail": changed_files,
    }
    (EVID / "SANITIZATION.md").write_text(
        "# 证据脱敏记录\n\n"
        f"- 替换总数：{total}\n- 涉及文件：{len(changed_files)}\n\n"
        "| 文件 | 替换次数 |\n|---|---|\n"
        + "\n".join(f"| `{c['file']}` | {c['replacements']} |" for c in changed_files)
        + "\n\n占位符：`<HOME>` = 本机用户目录，`<REPO>` = 仓库根目录。\n"
        "所有状态、时间戳、HTTP 状态码、判定结论均未修改。\n",
        encoding="utf-8")
    print(json.dumps({"files_changed": len(changed_files), "total_replacements": total}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
