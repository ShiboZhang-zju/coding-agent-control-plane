"""Run the CodeBuddy-backed control plane reconciliation loop.

The worker HTTP service is managed separately by ``scripts/serve_ctl.py``. This
process owns only the control-plane ledger and automatic observation lifecycle.

``--advance-goals`` additionally attaches the deterministic LD goal scheduler
(M4-C), which is what makes "one Human Owner goal" actually need only one
invocation: after a Worker verdict the LD is woken without a human re-running
``ld run-goal``.  Goal advancement runs on its own thread, so a slow model call
never blocks ready work from being dispatched.

Examples::

    python scripts/run_controlplane.py
    python scripts/run_controlplane.py --once --scan-on-start
    python scripts/run_controlplane.py --advance-goals --model-domains local
    python scripts/detached.py start controlplane scripts/run_controlplane.py
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.codebuddy.adapter import CodeBuddyAdapter  # noqa: E402
from adapters.codebuddy.client import Api  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.project_service import ProjectService  # noqa: E402
from controlplane.runtime import ControlPlaneRuntime, RuntimeConfig  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from ld.coordinator import LDCoordinator  # noqa: E402
from ld.goal_runner import LDGoalRunner  # noqa: E402
from ld.goal_store import LDGoalStore  # noqa: E402
from ld.model import OpenAIChatModel  # noqa: E402
from ld.tools import LDTools  # noqa: E402


def build_goal_runner(plane: ControlPlane, store: TaskStore, args,
                      goal_store: LDGoalStore):
    """Attach the LD to this runtime without giving the runtime a model.

    The coordinator stays the only thing that decides; the runner only decides
    *when*.  The ledger is shared, so task truth has a single source.
    """
    base_url = (args.model_base_url or os.environ.get("LD_MODEL_BASE_URL", "")).strip()
    model_name = (args.model_name or os.environ.get("LD_MODEL_NAME", "")).strip()
    model = None
    if base_url and model_name:
        model = OpenAIChatModel(
            base_url=base_url, model=model_name,
            api_key=args.model_api_key or os.environ.get("LD_MODEL_API_KEY"),
            timeout_s=(args.model_timeout_s
                       or float(os.environ.get("LD_MODEL_TIMEOUT_S", "60"))))
    domain_spec = args.model_domains or os.environ.get("LD_MODEL_DOMAINS", "")
    domains = tuple(item.strip() for item in domain_spec.split(",") if item.strip())
    coordinator = LDCoordinator(
        LDTools(ProjectService(plane)), model,
        model_authorized_security_domains=domains)
    return LDGoalRunner(
        coordinator, goal_store,
        max_decisions=args.goal_max_decisions, max_tasks=args.goal_max_tasks,
        max_model_failures=args.goal_max_model_failures,
        base_backoff_s=args.goal_base_backoff,
        max_backoff_s=args.goal_max_backoff,
        lifetime_max_decisions=args.goal_lifetime_decisions,
        lifetime_max_tasks=args.goal_lifetime_tasks)


def main() -> int:
    ap = argparse.ArgumentParser(description="automatic control-plane reconciliation runtime")
    ap.add_argument("--base", default="http://127.0.0.1:8787",
                    help="loopback worker HTTP base URL")
    ap.add_argument("--ledger", type=Path, default=None,
                    help="SQLite ledger path (default: .poc-state/ledger.db)")
    ap.add_argument("--node-id", default="local", choices=["local"],
                    help="stable execution node identity (M3; only local is executable)")
    ap.add_argument("--worker-instance-id", default=None,
                    help="stable Worker instance identity persisted in SessionReference")
    ap.add_argument("--known-interval", type=float, default=5.0)
    ap.add_argument("--scan-interval", type=float, default=60.0)
    ap.add_argument("--dispatch-interval", type=float, default=0.5)
    ap.add_argument("--max-concurrent", type=int, default=4)
    ap.add_argument("--max-dispatch-per-cycle", type=int, default=4)
    ap.add_argument("--max-backoff", type=float, default=300.0)
    ap.add_argument("--scan-on-start", action="store_true")
    ap.add_argument("--once", action="store_true",
                    help="run one forced known-job refresh and orphan scan, then exit")
    ap.add_argument("--advance-goals", action="store_true",
                    help="attach the deterministic LD goal scheduler (M4-C): the LD "
                         "is woken automatically when a goal's observed state changes")
    ap.add_argument("--goal-interval", type=float, default=5.0)
    ap.add_argument("--goal-max-decisions", type=int, default=1)
    ap.add_argument("--goal-max-tasks", type=int, default=1)
    ap.add_argument("--goal-lifetime-decisions", type=int, default=None,
                    help="lifetime model-decision budget per goal (default: "
                         "ld.goal_store.DEFAULT_LIFETIME_DECISIONS); stamped "
                         "once, never enlarged by scheduling or resume")
    ap.add_argument("--goal-lifetime-tasks", type=int, default=None,
                    help="lifetime WorkItem submission budget per goal "
                         "(default: ld.goal_store.DEFAULT_LIFETIME_TASKS)")
    ap.add_argument("--goal-max-model-failures", type=int, default=3)
    ap.add_argument("--goal-base-backoff", type=float, default=5.0)
    ap.add_argument("--goal-max-backoff", type=float, default=300.0)
    ap.add_argument("--goal-lease-s", type=float, default=120.0)
    ap.add_argument("--model-base-url", default=None)
    ap.add_argument("--model-name", default=None)
    ap.add_argument("--model-api-key", default=None)
    ap.add_argument("--model-timeout-s", type=float, default=None)
    ap.add_argument("--model-domains", default=None,
                    help="comma-separated security domains the model endpoint may "
                         "see (or LD_MODEL_DOMAINS). Default: none")
    args = ap.parse_args()

    store = TaskStore(args.ledger)
    runtime = None
    goal_store = None
    goal_runner = None
    try:
        adapter = CodeBuddyAdapter(Api(base=args.base), autologin=True)
        plane = ControlPlane(
            adapter, store, require_registered_projects=True,
            node_id=args.node_id, worker_instance_id=args.worker_instance_id)
        if args.advance_goals:
            goal_store = LDGoalStore(store.path, lease_s=args.goal_lease_s)
            goal_runner = build_goal_runner(plane, store, args, goal_store)
        runtime = ControlPlaneRuntime(
            plane,
            RuntimeConfig(
                known_refresh_interval_s=args.known_interval,
                orphan_scan_interval_s=args.scan_interval,
                dispatch_interval_s=args.dispatch_interval,
                goal_interval_s=args.goal_interval,
                max_concurrent_tasks=args.max_concurrent,
                max_dispatch_per_cycle=args.max_dispatch_per_cycle,
                max_backoff_s=args.max_backoff,
                orphan_scan_on_start=args.scan_on_start,
            ),
            goal_runner=goal_runner,
        )

        if args.once:
            result = runtime.run_due(force=True)
            print(json.dumps({"result": result, "runtime": runtime.status(),
                              "goals": runtime.goal_status()},
                             ensure_ascii=False, indent=2, default=str))
            return 0

        def request_stop(_signum, _frame) -> None:
            runtime.request_stop()

        signal.signal(signal.SIGINT, request_stop)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, request_stop)

        print(json.dumps({
            "event": "runtime_starting",
            "worker": adapter.name,
            "node_id": plane.node_id,
            "worker_instance_id": plane.worker_instance_id,
            "ledger": str(store.path),
            "known_interval_s": args.known_interval,
            "orphan_scan_interval_s": args.scan_interval,
            "dispatch_interval_s": args.dispatch_interval,
            "max_concurrent_tasks": args.max_concurrent,
            "goal_advancement": args.advance_goals,
            "goal_interval_s": args.goal_interval if args.advance_goals else None,
        }, ensure_ascii=False))
        runtime.run_forever()
        print(json.dumps({"event": "runtime_stopped", "runtime": runtime.status(),
                          "goals": runtime.goal_status()},
                         ensure_ascii=False, default=str))
        return 0
    finally:
        if runtime:
            runtime.request_stop()
        if runtime:
            runtime.stop(timeout_s=10.0)
        if goal_runner is not None:
            goal_runner.close()
        if goal_store is not None:
            goal_store.close()
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
