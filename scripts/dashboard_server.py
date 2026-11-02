"""Serve UI-0 read-only, or explicitly enable the UI-1 LD chat.

Usage
-----
    python scripts/dashboard_server.py --ledger .poc-state/ledger.db \
        --host 127.0.0.1 --port 8765

then open http://127.0.0.1:8765

What this process can and cannot do
-----------------------------------
By default it is UI-0: dashboard projections use ``mode=ro`` plus
``PRAGMA query_only=ON`` and every write method is rejected.  With the explicit
``--enable-chat`` flag it adds one bearer-authenticated, same-origin POST route
that delegates sentences to the existing ``LDChat`` service graph.  That path
may create a Goal or confirm a staged resume exactly as the CLI does; it cannot
approve work, execute arbitrary commands, or bypass the Control Plane.

Both modes are loopback-only.  UI-1 requires an existing ledger and does not
claim the separate ``LDGoalRunner`` process is online.  A configured intent
model is used only under the existing LDChat security-domain rules.

Exit behaviour
--------------
Ctrl-C stops the server cleanly.  ``--probe`` performs one read and prints the
result without starting the HTTP server, which is handy for checking a ledger
before opening a browser.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dashboard.api import LOOPBACK_HOSTS, make_server  # noqa: E402
from dashboard.chat import ChatBridgeError  # noqa: E402
from dashboard.ledger import DashboardError, ReadOnlyLedger  # noqa: E402
from dashboard.projections import overview  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=("Project dashboard; read-only unless --enable-chat "
                     "explicitly enables the restricted LDChat endpoint."))
    parser.add_argument("--ledger", default=str(ROOT / ".poc-state" / "ledger.db"),
                        help="path to an existing SQLite ledger (never created)")
    parser.add_argument("--host", default="127.0.0.1",
                        help=f"bind address; loopback only {sorted(LOOPBACK_HOSTS)}")
    parser.add_argument("--port", type=int, default=8765, help="TCP port (0 = any free)")
    parser.add_argument("--probe", action="store_true",
                        help="read the ledger once, print the overview, and exit")
    parser.add_argument(
        "--enable-chat", action="store_true",
        help=("enable the authenticated UI-1 LDChat write endpoint; without "
              "this flag the server remains strict UI-0 read-only"))
    parser.add_argument(
        "--chat-token", default=None,
        help=("local operator bearer token (or DASHBOARD_CHAT_TOKEN); at least "
              "20 characters. Prefer the environment variable so the token "
              "does not appear in shell history/process arguments"))
    parser.add_argument("--model-base-url", default=None,
                        help="chat intent endpoint (or LD_MODEL_BASE_URL)")
    parser.add_argument("--model-name", default=None,
                        help="chat intent model id (or LD_MODEL_NAME)")
    parser.add_argument("--model-api-key", default=None,
                        help="endpoint key (or LD_MODEL_API_KEY); never logged")
    parser.add_argument("--model-timeout-s", type=float, default=None,
                        help="intent call deadline (or LD_MODEL_TIMEOUT_S)")
    parser.add_argument(
        "--model-domains", default=None,
        help=("security domains the intent model may see (or LD_MODEL_DOMAINS); "
              "local messages stay in-process unless local is explicitly granted"))
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
        chat_token = args.chat_token or os.environ.get("DASHBOARD_CHAT_TOKEN")
        server = make_server(
            ledger_path, host=args.host, port=args.port,
            enable_chat=args.enable_chat, chat_token=chat_token,
            model_base_url=args.model_base_url, model_name=args.model_name,
            model_api_key=args.model_api_key,
            model_timeout_s=args.model_timeout_s,
            model_domains=args.model_domains)
    except (ValueError, ChatBridgeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    host, port = server.server_address[0], server.server_address[1]
    mode = "UI-1 chat enabled" if args.enable_chat else "UI-0 read-only"
    print(f"[dashboard] {mode}; ledger={ledger_path}")
    print(f"[dashboard] listening on http://{host}:{port}  (Ctrl-C to stop)")
    if args.enable_chat:
        caps = server.app.chat.capabilities
        print("[dashboard] chat auth: bearer token required (token not logged)")
        print("[dashboard] intent routing: " + caps["intent_routing"])
        print("[dashboard] goal runner liveness: unknown (no durable heartbeat)")
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
