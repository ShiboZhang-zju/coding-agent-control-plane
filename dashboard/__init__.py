"""UI-0: a strictly read-only operator dashboard over the existing ledger.

This package contains *no* domain rules of its own.  Every number it shows is
computed by :class:`controlplane.project_service.ProjectService` from the
SQLite ledger, and every goal row comes from :class:`ld.goal_store.LDGoalStore`.
The dashboard adds exactly two things:

1. a **read-only** way to obtain those objects (see :mod:`dashboard.ledger`);
2. an **explicit field whitelist** that decides what may leave the process
   (see :mod:`dashboard.projections`).

That split is deliberate: "the dashboard is read-only" is then a property of
one small module plus the connection mode, not a promise spread over hundreds
of lines of rendering code.

Not implemented on purpose: no writes, no approvals, no dispatch, no LLM call,
no WebSocket/SSE, no second copy of task state.  UI-1 owns write actions.
"""
