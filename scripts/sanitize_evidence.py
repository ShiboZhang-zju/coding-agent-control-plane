"""Strip machine paths and credential-like values from committed evidence.

Paths are replaced without changing timings, states or verdicts. New structured
evidence also redacts password/token/authorization/cookie/secret fields before
serialization. Run the CLI before committing to sanitize legacy text files; its
path-replacement manifest is stored in `evidence/SANITIZATION.md`.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
EVID = ROOT / "evidence"
HOME = os.path.expanduser("~")

SENSITIVE_KEYS = {
    "password", "passwd", "authorization", "cookie", "api_key", "apikey",
    "access_token", "refresh_token", "client_secret", "private_key", "secret",
}


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
        (r"[A-Za-z]:\\+Users\\+[A-Za-z0-9._-]+", "<HOME>"),
        (rf"/[A-Za-z]/Users/{re.escape(user)}", "<HOME>"),
        # any POSIX-style home, including JSON-escaped "\/Users\/<name>"
        (r"[/\\]+Users[/\\]+[A-Za-z0-9._-]+", "<HOME>"),
    ]


def sanitize_text(text: str) -> tuple[str, int]:
    """Apply every substitution to one text blob. Returns (text, hits)."""
    hits = 0
    for a, b in patterns():
        if a and a in text:
            hits += text.count(a)
            text = text.replace(a, b)
    for pattern, repl in regexes():
        text, n = re.subn(pattern, repl, text)
        hits += n
    # Evidence may contain a diagnostic string rather than a structured mapping.
    # Redact the two common inline credential shapes before it reaches disk.
    text, n = re.subn(
        r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+",
        "Bearer <redacted>", text)
    hits += n
    text, n = re.subn(
        r"(?i)\b(password|passwd|token|authorization|cookie|api[_-]?key|"
        r"client[_-]?secret)\s*([:=])\s*(\"[^\"]*\"|'[^']*'|[^\s,;]+)",
        lambda match: f"{match.group(1)}{match.group(2)}<redacted>", text)
    hits += n
    return text, hits


def redact_sensitive(value: Any) -> Any:
    """Recursively remove credential-like values while preserving structure."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")
            sensitive = (normalized in SENSITIVE_KEYS
                         or normalized.endswith("_token")
                         or normalized.startswith("token_"))
            out[key] = "<redacted>" if sensitive else redact_sensitive(item)
        return out
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, str):
        return sanitize_text(value)[0]
    return value


def write_evidence(path: Path, data) -> None:
    """Serialize evidence and strip machine paths BEFORE the bytes hit disk.

    Test scripts should use this instead of a raw ``write_text(json.dumps(...))``
    so committed evidence never leaks a local home directory again.
    """
    text = json.dumps(redact_sensitive(data), ensure_ascii=False, indent=2)
    text, _ = sanitize_text(text)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replace prevents a crash from leaving a half-written JSON artifact.
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent,
        prefix=path.name + ".", suffix=".tmp", delete=False)
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write(text)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


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
