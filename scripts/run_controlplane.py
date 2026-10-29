"""Run the CodeBuddy-backed control plane reconciliation loop.

The worker HTTP service is managed separately by ``scripts/serve_ctl.py``. This
process owns only the control-plane ledger and automatic observation lifecycle.

Examples::

    python scripts/run_controlplane.py
    python scripts/run_controlplane.py --once --scan-on-start
    python scripts/detached.py start controlplane scripts/run_controlplane.py
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.codebuddy.adapter import CodeBuddyAdapter  # noqa: E402
from adapters.codebuddy.client import Api  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.runtime import ControlPlaneRuntime, RuntimeConfig  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="automatic control-plane reconciliation runtime")
    ap.add_argument("--base", default="http://127.0.0.1:8787",
                    help="loopback worker HTTP base URL")
    ap.add_argument("--ledger", type=Path, default=None,
                    help="SQLite ledger path (default: .poc-state/ledger.db)")
    ap.add_argument("--known-interval", type=float, default=5.0)
    ap.add_argument("--scan-interval", type=float, default=60.0)
    ap.add_argument("--dispatch-interval", type=float, default=0.5)
    ap.add_argument("--max-concurrent", type=int, default=4)
    ap.add_argument("--max-dispatch-per-cycle", type=int, default=4)
    ap.add_argument("--max-backoff", type=float, default=300.0)
    ap.add_argument("--scan-on-start", action="store_true")
    ap.add_argument("--once", action="store_true",
                    help="run one forced known-job refresh and orphan scan, then exit")
    args = ap.parse_args()

    store = TaskStore(args.ledger)
    runtime = None
    try:
        adapter = CodeBuddyAdapter(Api(base=args.base), autologin=True)
        plane = ControlPlane(adapter, store, require_registered_projects=True)
        runtime = ControlPlaneRuntime(
            plane,
            RuntimeConfig(
                known_refresh_interval_s=args.known_interval,
                orphan_scan_interval_s=args.scan_interval,
                dispatch_interval_s=args.dispatch_interval,
                max_concurrent_tasks=args.max_concurrent,
                max_dispatch_per_cycle=args.max_dispatch_per_cycle,
                max_backoff_s=args.max_backoff,
                orphan_scan_on_start=args.scan_on_start,
            ),
        )

        if args.once:
            result = runtime.run_due(force=True)
            print(json.dumps({"result": result, "runtime": runtime.status()},
                             ensure_ascii=False, indent=2))
            return 0

        def request_stop(_signum, _frame) -> None:
            runtime.request_stop()

        signal.signal(signal.SIGINT, request_stop)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, request_stop)

        print(json.dumps({
            "event": "runtime_starting",
            "worker": adapter.name,
            "ledger": str(store.path),
            "known_interval_s": args.known_interval,
            "orphan_scan_interval_s": args.scan_interval,
            "dispatch_interval_s": args.dispatch_interval,
            "max_concurrent_tasks": args.max_concurrent,
        }, ensure_ascii=False))
        runtime.run_forever()
        print(json.dumps({"event": "runtime_stopped", "runtime": runtime.status()},
                         ensure_ascii=False))
        return 0
    finally:
        if runtime:
            runtime.request_stop()
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
