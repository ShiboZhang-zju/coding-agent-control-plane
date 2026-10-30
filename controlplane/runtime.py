"""Long-running control-plane observation runtime.

The PoC exposed ``refresh_known_jobs()`` and ``discover_orphan_jobs()`` as
correctness-preserving primitives, but left the caller responsible for invoking
them.  This module turns those primitives into a lifecycle-managed runtime:

* recover persisted dispatch intents before dispatching more work;
* atomically claim and dispatch queued work under an Adapter concurrency limit;
* refresh known jobs frequently without coupling them to the slow full scan;
* scan for orphan/vanished jobs at a lower frequency;
* back off a failing path without stopping the other path;
* stop promptly and expose a compact, thread-safe status snapshot.

Scheduling and observation remain independent timed paths, so a slow Worker
query cannot stop ready work from being claimed, and a dispatch failure cannot
disable orphan reconciliation.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from controlplane.store import LedgerDegraded


@dataclass(frozen=True)
class RuntimeConfig:
    """Cadence and failure behaviour for :class:`ControlPlaneRuntime`."""

    known_refresh_interval_s: float = 5.0
    orphan_scan_interval_s: float = 60.0
    dispatch_interval_s: float = 0.5
    #: Goal-advancement cadence.  Only reached when a coordinator is attached;
    #: the LD model is called only on a real state change (see ``LDGoalRunner``).
    goal_interval_s: float = 5.0
    max_concurrent_tasks: int = 4
    max_dispatch_per_cycle: int = 4
    max_backoff_s: float = 300.0
    orphan_scan_on_start: bool = False
    idle_sleep_cap_s: float = 1.0

    def validate(self) -> None:
        values = {
            "known_refresh_interval_s": self.known_refresh_interval_s,
            "orphan_scan_interval_s": self.orphan_scan_interval_s,
            "dispatch_interval_s": self.dispatch_interval_s,
            "goal_interval_s": self.goal_interval_s,
            "max_backoff_s": self.max_backoff_s,
            "idle_sleep_cap_s": self.idle_sleep_cap_s,
            "max_concurrent_tasks": self.max_concurrent_tasks,
            "max_dispatch_per_cycle": self.max_dispatch_per_cycle,
        }
        bad = [name for name, value in values.items() if value <= 0]
        if bad:
            raise ValueError("runtime intervals must be positive: " + ", ".join(bad))


def _compact_checkpoint_sweep(result: dict | None) -> dict:
    if not isinstance(result, dict):
        return {"ok": False, "error": "checkpoint sweep produced no result"}
    return {
        "ok": result.get("ok", False),
        "checked": result.get("checked", 0),
        "released": result.get("released_count", 0),
        "blocked": result.get("blocked_count", 0),
        "blocked_items": (result.get("blocked") or [])[:5],
    }


def _path_status() -> dict[str, Any]:
    return {
        "runs": 0,
        "consecutive_errors": 0,
        "last_started_at": None,
        "last_finished_at": None,
        "last_duration_ms": None,
        "last_result": None,
        "last_error": None,
    }


class ControlPlaneRuntime:
    """Run automatic recovery and reconciliation for one control plane.

    ``run_forever`` is suitable for a foreground service process. ``start`` and
    ``stop`` provide an embeddable background-thread lifecycle for a future API
    server and for deterministic smoke tests.
    """

    def __init__(self, plane, config: RuntimeConfig | None = None, *,
                 owner: str | None = None,
                 goal_runner=None,
                 monotonic: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time):
        self.plane = plane
        self.config = config or RuntimeConfig()
        self.config.validate()
        self.owner = owner or "runtime-" + uuid.uuid4().hex[:10]
        # Optional deterministic goal scheduler (M4-C).  Attaching it here —
        # rather than starting a second autonomous loop — means the existing
        # lifecycle, backoff and status surface own it, and both paths share
        # one Ledger.  ``None`` keeps the pre-M4-C runtime exactly as it was.
        self.goal_runner = goal_runner
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._stop = threading.Event()
        self._state_lock = threading.RLock()
        self._cycle_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._prepared = False
        self._next_known = 0.0
        self._next_scan = 0.0
        self._next_dispatch = 0.0
        self._next_goal = 0.0
        self._goal_lock = threading.Lock()
        self._goal_thread: threading.Thread | None = None
        self._state: dict[str, Any] = {
            "running": False,
            "prepared": False,
            "started_at": None,
            "stopped_at": None,
            "cycles": 0,
            "last_error": None,
            "recovery": None,
            "dispatch_queue": _path_status(),
            "known_jobs": _path_status(),
            "orphan_scan": _path_status(),
            "goal_advancement": _path_status(),
            "goal_runner_enabled": goal_runner is not None,
        }

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def prepare(self) -> dict:
        """Recover in-flight intent exactly once before the loop starts."""
        with self._state_lock:
            if self._prepared:
                return dict(self._state.get("recovery") or {})

        # A degraded/empty ledger must not be mutated by an automatic loop.
        self.plane.store.assert_writable("runtime startup")
        recovery = self.plane.recover_inflight()
        now = self._monotonic()
        with self._state_lock:
            self._prepared = True
            self._next_known = now
            self._next_dispatch = now
            self._next_scan = (now if self.config.orphan_scan_on_start
                               else now + self.config.orphan_scan_interval_s)
            self._next_goal = (now if self.goal_runner is not None
                               else now + self.config.goal_interval_s)
            self._state["prepared"] = True
            self._state["recovery"] = recovery
        return dict(recovery)

    def start(self, *, name: str = "control-plane-runtime") -> bool:
        """Start in a daemon thread. Return False when already running."""
        with self._state_lock:
            if self._thread and self._thread.is_alive():
                return False
            self._stop.clear()
            thread = threading.Thread(target=self.run_forever, name=name, daemon=True)
            self._thread = thread
            thread.start()
        return True

    def request_stop(self) -> None:
        """Ask a foreground or background runtime to stop at its next wait."""
        self._stop.set()

    def stop(self, timeout_s: float = 10.0) -> bool:
        """Stop a background runtime and report whether it exited in time."""
        self.request_stop()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout_s))
        return not bool(thread and thread.is_alive())

    def run_forever(self) -> None:
        """Run until ``request_stop`` is called."""
        with self._state_lock:
            self._state["running"] = True
            self._state["started_at"] = self._state["started_at"] or self._wall_clock()
            self._state["stopped_at"] = None
            self._state["last_error"] = None
        try:
            self.prepare()
            self._start_goal_thread()
            while not self._stop.is_set():
                self.run_due()
                self._stop.wait(self.seconds_until_next())
        except LedgerDegraded as exc:
            with self._state_lock:
                self._state["last_error"] = f"{type(exc).__name__}: {exc}"
            raise
        except Exception as exc:  # noqa: BLE001 - service boundary records fatal errors
            with self._state_lock:
                self._state["last_error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self._join_goal_thread()
            with self._state_lock:
                self._state["running"] = False
                self._state["stopped_at"] = self._wall_clock()

    # ------------------------------------------------------------------ #
    # goal advancement (M4-C) — separate thread, shared Ledger
    # ------------------------------------------------------------------ #
    def _start_goal_thread(self) -> None:
        """Run goal advancement off the dispatch thread.

        A model call can take tens of seconds.  Putting it on the dispatch
        thread would stall ready work behind an LLM round trip, so goal
        advancement gets its own daemon thread while still sharing this
        runtime's lifecycle, backoff and status surface.  It is one thread, not
        a second autonomous loop, and ``_goal_lock`` guarantees only one
        advancement at a time even if ``run_due(force=True)`` also asks for it.
        """
        if self.goal_runner is None:
            return
        with self._state_lock:
            if self._goal_thread and self._goal_thread.is_alive():
                return
            thread = threading.Thread(target=self._goal_loop,
                                      name="control-plane-goal-runner", daemon=True)
            self._goal_thread = thread
        thread.start()

    def _join_goal_thread(self, timeout_s: float = 10.0) -> None:
        thread = self._goal_thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout_s))

    def _goal_loop(self) -> None:
        while not self._stop.is_set():
            with self._state_lock:
                now = self._monotonic()
                delay = self._next_goal - now
            if delay > 0.01:
                self._stop.wait(min(delay, self.config.idle_sleep_cap_s))
                continue
            # One path failure (a broken goal, a transient Git error) must not
            # kill goal advancement for every other goal.
            try:
                self._run_path("goal_advancement", self._advance_goals,
                               self.config.goal_interval_s)
            except Exception as exc:  # noqa: BLE001 - path bookkeeping is in _run_path
                with self._state_lock:
                    self._state["goal_advancement"]["last_error"] = (
                        f"{type(exc).__name__}: {exc}")

    def _advance_goals(self) -> dict[str, Any]:
        if not self._goal_lock.acquire(blocking=False):
            return {"ok": True, "skipped": "another advancement is in progress",
                    "advanced_count": 0}
        try:
            return self.goal_runner.advance_due()
        finally:
            self._goal_lock.release()

    # ------------------------------------------------------------------ #
    # cycles
    # ------------------------------------------------------------------ #
    def run_due(self, *, force: bool = False) -> dict[str, Any]:
        """Run paths that are due; ``force`` runs every path immediately.

        In the continuous loop goal advancement is owned by its own thread (see
        :meth:`_start_goal_thread`), so this method only runs it when
        ``force=True`` — that is the deterministic single-shot path used by
        ``--once`` and by tests.
        """
        self.prepare()
        with self._cycle_lock:
            now = self._monotonic()
            result: dict[str, Any] = {}
            if force or now >= self._next_dispatch:
                result["dispatch_queue"] = self._run_path(
                    "dispatch_queue", self._dispatch_ready,
                    self.config.dispatch_interval_s)
            now = self._monotonic()
            if force or now >= self._next_known:
                result["known_jobs"] = self._run_path(
                    "known_jobs", self.plane.refresh_known_jobs,
                    self.config.known_refresh_interval_s)
            now = self._monotonic()
            if force or now >= self._next_scan:
                result["orphan_scan"] = self._run_path(
                    "orphan_scan", self.plane.discover_orphan_jobs,
                    self.config.orphan_scan_interval_s)
            if self.goal_runner is not None and force:
                result["goal_advancement"] = self._run_path(
                    "goal_advancement", self._advance_goals,
                    self.config.goal_interval_s)
            with self._state_lock:
                self._state["cycles"] += 1
            return result

    def _dispatch_ready(self) -> dict[str, Any]:
        # Precondition sweep before claiming: a handoff whose protective ref
        # failed (or was never written because the process died) is promoted to
        # 'ready' only after its checkpoint is proven protected again.  Anything
        # still unprovable stays out of the claim set.
        checkpoint_sweep = None
        try:
            checkpoint_sweep = self.plane.reconcile_handoff_checkpoints()
        except Exception as exc:  # noqa: BLE001 - never block ordinary dispatch
            checkpoint_sweep = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        dispatched = []
        for _ in range(self.config.max_dispatch_per_cycle):
            rec = self.plane.dispatch_next(
                self.owner, concurrency_limit=self.config.max_concurrent_tasks)
            if not rec:
                break
            dispatched.append({
                "task_id": rec.get("task_id"),
                "worker_job_id": rec.get("worker_job_id"),
                "state": rec.get("state"),
                "queue_status": rec.get("queue_status"),
            })
        return {"ok": True, "dispatched": dispatched,
                "count": len(dispatched),
                "checkpoints": _compact_checkpoint_sweep(checkpoint_sweep),
                "active": self.plane.store.active_count(self.plane.adapter.name)}

    def _run_path(self, name: str, operation: Callable[[], dict],
                  base_interval_s: float) -> dict[str, Any]:
        started_wall = self._wall_clock()
        started_mono = self._monotonic()
        with self._state_lock:
            path = self._state[name]
            path["last_started_at"] = started_wall
            path["last_error"] = None
        try:
            raw = operation()
            compact = self._compact_result(name, raw)
            failed = raw.get("ok") is False
            error = raw.get("error") if failed else None
        except Exception as exc:  # noqa: BLE001 - one path must not stop the other
            raw = None
            compact = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            failed = True
            error = compact["error"]

        finished_wall = self._wall_clock()
        duration_ms = round((self._monotonic() - started_mono) * 1000, 1)
        with self._state_lock:
            path = self._state[name]
            path["runs"] += 1
            path["last_finished_at"] = finished_wall
            path["last_duration_ms"] = duration_ms
            path["last_result"] = compact
            path["last_error"] = error
            path["consecutive_errors"] = (path["consecutive_errors"] + 1
                                            if failed else 0)
            delay = self._delay(base_interval_s, path["consecutive_errors"])
            if name == "known_jobs":
                self._next_known = self._monotonic() + delay
            elif name == "dispatch_queue":
                self._next_dispatch = self._monotonic() + delay
            elif name == "goal_advancement":
                self._next_goal = self._monotonic() + delay
            else:
                self._next_scan = self._monotonic() + delay
        return compact

    def _delay(self, base: float, consecutive_errors: int) -> float:
        if consecutive_errors <= 0:
            return base
        return min(self.config.max_backoff_s, base * (2 ** min(consecutive_errors, 8)))

    @staticmethod
    def _compact_result(name: str, result: dict) -> dict[str, Any]:
        """Keep runtime status bounded; detailed history belongs in the ledger."""
        if name == "known_jobs":
            return {
                "ok": result.get("ok", True),
                "checked": result.get("checked", 0),
                "updated": len(result.get("updated") or []),
                "anomalies": len(result.get("anomalies") or []),
            }
        if name == "dispatch_queue":
            return {
                "ok": result.get("ok", True),
                "dispatched": result.get("count", 0),
                "active": result.get("active", 0),
                "items": result.get("dispatched") or [],
                "checkpoints": result.get("checkpoints") or {},
            }
        if name == "goal_advancement":
            if "advanced" not in result and "checked" not in result:
                return {"ok": result.get("ok", True),
                        "advanced": 0, "waiting": 0, "skipped": 0,
                        "failures": 0, "note": result.get("skipped")}
            return {
                "ok": result.get("ok", True),
                "checked": result.get("checked", 0),
                "advanced": len(result.get("advanced") or []),
                "waiting": len(result.get("waiting") or []),
                "skipped": len(result.get("skipped") or []),
                "failures": len(result.get("failures") or []),
                "items": (result.get("advanced") or [])[:5],
                "errors": (result.get("failures") or [])[:5],
            }
        return {
            "ok": result.get("ok", True),
            "total": result.get("total"),
            "orphan_count": result.get("orphan_count", 0),
            "vanished": len(result.get("vanished") or []),
            "error": result.get("error"),
        }

    def seconds_until_next(self) -> float:
        """Bounded wait so stop requests remain prompt.

        Goal advancement is deliberately excluded: it runs on its own thread and
        must only ever shorten its *own* wait, never make the dispatch/observe
        loop wake up more often.
        """
        with self._state_lock:
            now = self._monotonic()
            delay = max(0.01, min(self._next_dispatch,
                                  self._next_known, self._next_scan) - now)
        return min(delay, self.config.idle_sleep_cap_s)

    def status(self) -> dict[str, Any]:
        """Return a compact status snapshot safe for API/CLI serialization."""
        paths = ("dispatch_queue", "known_jobs", "orphan_scan", "goal_advancement")
        with self._state_lock:
            out = {
                **{k: v for k, v in self._state.items() if k not in paths},
                **{name: dict(self._state[name]) for name in paths},
                "thread_alive": bool(self._thread and self._thread.is_alive()),
                "goal_thread_alive": bool(self._goal_thread
                                          and self._goal_thread.is_alive()),
            }
            if self._prepared:
                now = self._monotonic()
                out["dispatch_queue"]["next_in_s"] = round(
                    max(0.0, self._next_dispatch - now), 3)
                out["known_jobs"]["next_in_s"] = round(max(0.0, self._next_known - now), 3)
                out["orphan_scan"]["next_in_s"] = round(max(0.0, self._next_scan - now), 3)
                out["goal_advancement"]["next_in_s"] = round(
                    max(0.0, self._next_goal - now), 3)
            return out

    def goal_status(self) -> dict[str, Any] | None:
        """Per-goal scheduler view (None when no coordinator is attached)."""
        if self.goal_runner is None:
            return None
        return self.goal_runner.status()
