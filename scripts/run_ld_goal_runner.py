"""Run the deterministic LD goal runner as its own service (M4-C).

Why a separate process option exists
------------------------------------
``ControlPlaneRuntime`` can host the goal runner in-process
(``scripts/run_controlplane.py --advance-goals``).  That is the normal
deployment.  This entry point exists for the case the task brief allows and
that is sometimes preferable in practice: the control plane must be able to run
without importing a model client at all (a pure observation/dispatch box), while
the LD model client runs somewhere else.

Both options share the same SQLite Ledger and the same ``LDGoalRunner``; there
is exactly one automatic-wakeup implementation, never two.

The runner is a **scheduler**: it calls the model only when the observed state
of a goal could produce a different decision.  It never uploads a prompt, never
approves work and never dispatches a Worker.

Examples::

    python scripts/run_ld_goal_runner.py --once
    python scripts/run_ld_goal_runner.py --interval 5
    python scripts/run_ld_goal_runner.py --once --model-domains local
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.project_service import ProjectService  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from ld.coordinator import LDCoordinator  # noqa: E402
from ld.goal_runner import LDGoalRunner  # noqa: E402
from ld.goal_store import LDGoalStore  # noqa: E402
from ld.model import OpenAIChatModel  # noqa: E402
from ld.tools import LDTools  # noqa: E402


class AdapterRef:
    """Name-only Adapter reference: this process never calls a Worker."""

    def __init__(self, name: str):
        self.name = name


def _model_from_args(args) -> OpenAIChatModel | None:
    base_url = (args.model_base_url or os.environ.get("LD_MODEL_BASE_URL", "")).strip()
    model_name = (args.model_name or os.environ.get("LD_MODEL_NAME", "")).strip()
    if not base_url or not model_name:
        return None
    return OpenAIChatModel(
        base_url=base_url, model=model_name,
        api_key=args.model_api_key or os.environ.get("LD_MODEL_API_KEY"),
        timeout_s=(args.model_timeout_s
                   or float(os.environ.get("LD_MODEL_TIMEOUT_S", "60"))))


def main() -> int:
    ap = argparse.ArgumentParser(
        description="deterministic LD goal scheduler (wakes the LD on state change)")
    ap.add_argument("--ledger", type=Path, default=None,
                    help="SQLite ledger path (default: .poc-state/ledger.db)")
    ap.add_argument("--interval", type=float, default=5.0,
                    help="poll cadence; the model is only called on a state change")
    ap.add_argument("--once", action="store_true",
                    help="run one sweep and exit (deterministic; used by tests)")
    ap.add_argument("--max-decisions", type=int, default=1,
                    help="model decisions allowed per goal per sweep")
    ap.add_argument("--max-tasks", type=int, default=1,
                    help="task submissions allowed per goal per sweep")
    ap.add_argument("--max-model-failures", type=int, default=3,
                    help="consecutive model failures before the goal goes to human")
    ap.add_argument("--base-backoff-s", type=float, default=5.0)
    ap.add_argument("--max-backoff-s", type=float, default=300.0)
    ap.add_argument("--lease-s", type=float, default=120.0)
    ap.add_argument("--model-base-url", default=None)
    ap.add_argument("--model-name", default=None)
    ap.add_argument("--model-api-key", default=None)
    ap.add_argument("--model-timeout-s", type=float, default=None)
    ap.add_argument("--model-domains", default=None,
                    help="comma-separated security domains the endpoint may see "
                         "(or LD_MODEL_DOMAINS). Default: none, so a 'local' goal "
                         "is never sent to an endpoint without an explicit grant")
    ap.add_argument("--status", action="store_true",
                    help="print the per-goal scheduler view and exit")
    args = ap.parse_args()

    store = TaskStore(args.ledger)
    goal_store = LDGoalStore(store.path, lease_s=args.lease_s)
    goal_runner = None
    try:
        read_plane = ControlPlane(AdapterRef("ledger-only"), store)
        read_service = ProjectService(read_plane)

        def submission_service(project_id: str) -> ProjectService:
            project = read_plane.project(project_id)
            adapter_name = project.get("default_adapter")
            if not adapter_name:
                raise ValueError(f"project {project_id} has no default_adapter")
            return ProjectService(ControlPlane(
                AdapterRef(adapter_name), store, require_registered_projects=True))

        tools = LDTools(read_service,
                        submission_service_factory=submission_service)
        model = _model_from_args(args)
        domain_spec = args.model_domains or os.environ.get("LD_MODEL_DOMAINS", "")
        domains = tuple(item.strip() for item in domain_spec.split(",")
                        if item.strip())
        coordinator = LDCoordinator(
            tools, model, model_authorized_security_domains=domains)
        goal_runner = LDGoalRunner(
            coordinator, goal_store,
            max_decisions=args.max_decisions, max_tasks=args.max_tasks,
            max_model_failures=args.max_model_failures,
            base_backoff_s=args.base_backoff_s,
            max_backoff_s=args.max_backoff_s)

        if args.status:
            print(json.dumps(goal_runner.status(), ensure_ascii=False, indent=2))
            return 0
        if args.once:
            print(json.dumps(goal_runner.advance_due(), ensure_ascii=False,
                             indent=2, default=str))
            return 0

        stop = {"flag": False}

        def request_stop(_signum, _frame) -> None:
            stop["flag"] = True

        signal.signal(signal.SIGINT, request_stop)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, request_stop)
        print(json.dumps({"event": "goal_runner_starting",
                          "ledger": str(store.path),
                          "interval_s": args.interval,
                          "model_configured": model is not None,
                          "model_domains": list(domains)}, ensure_ascii=False))
        while not stop["flag"]:
            try:
                result = goal_runner.advance_due()
            except Exception as exc:  # noqa: BLE001 - a sweep failure is not fatal
                result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            advanced = len(result.get("advanced") or [])
            failed = len(result.get("failures") or [])
            if advanced or failed:
                print(json.dumps({"event": "goal_sweep", "advanced": advanced,
                                  "failures": failed,
                                  "items": result.get("items")},
                                 ensure_ascii=False, default=str))
            deadline = time.monotonic() + args.interval
            while not stop["flag"] and time.monotonic() < deadline:
                time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
        print(json.dumps({"event": "goal_runner_stopped"}, ensure_ascii=False))
        return 0
    finally:
        if goal_runner is not None:
            goal_runner.close()
        goal_store.close()
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
