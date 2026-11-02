"""UI-0: serve the read-only project dashboard over loopback HTTP.

Usage
-----
    python scripts/dashboard_server.py --ledger .poc-state/ledger.db \
        --host 127.0.0.1 --port 8765

then open http://127.0.0.1:8765

What this process can and cannot do
-----------------------------------
It can: open the ledger with ``mode=ro`` + ``PRAGMA query_only=ON``, run the
existing ``ProjectService``/``LDGoalStore`` queries, and serve HTML/CSS/JS plus
a handful of JSON endpoints.

It cannot: create a ledger, migrate a schema, write a row, dispatch a Worker,
call an LLM, approve anything, or listen on a non-loopback interface.  There is
no route that accepts a request body, and the OS-level file mode plus
``query_only`` refuse writes even from code that tried.

Exit behaviour
--------------
Ctrl-C stops the server cleanly.  ``--probe`` performs one read and prints the
result without starting the HTTP server, which is handy for checking a ledger
before opening a browser.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dashboard.api import LOOPBACK_HOSTS, make_server  # noqa: E402
from dashboard.ledger import DashboardError, ReadOnlyLedger  # noqa: E402
from dashboard.projections import overview  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only dashboard for the agent control plane ledger.")
    parser.add_argument("--ledger", default=str(ROOT / ".poc-state" / "ledger.db"),
                        help="path to an existing SQLite ledger (never created)")
    parser.add_argument("--host", default="127.0.0.1",
                        help=f"bind address; loopback only {sorted(LOOPBACK_HOSTS)}")
    parser.add_argument("--port", type=int, default=8765, help="TCP port (0 = any free)")
    parser.add_argument("--probe", action="store_true",
                        help="read the ledger once, print the overview, and exit")
    return parser


def _probe(ledger_path: Path) -> int:
    """One read-only overview, printed as JSON.  Never starts a server."""
    ledger = ReadOnlyLedger(ledger_path)
    try:
        with ledger.session() as session:
            data = overview(session)
    except DashboardError as exc:
        print(json.dumps({"ok": False, "error": {"type": type(exc).__name__,
                                                 "message": str(exc)}},
                         ensure_ascii=False, indent=2))
        return 2
    print(json.dumps({"ok": True, "overview": data}, ensure_ascii=False,
                     indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ledger_path = Path(args.ledger)
    if not ledger_path.is_absolute():
        ledger_path = (Path.cwd() / ledger_path).resolve()

    if args.probe:
        return _probe(ledger_path)

    try:
        server = make_server(ledger_path, host=args.host, port=args.port)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    host, port = server.server_address[0], server.server_address[1]
    print(f"[dashboard] read-only; ledger={ledger_path}")
    print(f"[dashboard] listening on http://{host}:{port}  (Ctrl-C to stop)")
    if not ledger_path.exists():
        # Not fatal: the server must stay up so the UI can *show* the error.
        print(f"[dashboard] WARNING: ledger does not exist yet: {ledger_path}\n"
              f"[dashboard] the dashboard will report 'ledger_missing' and will "
              f"NOT create it", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[dashboard] stopping")
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
