"""Named dispatch policies.

There is no implicit default that lets a caller run an un-isolated shell command.
Every dispatch names its policy; the unsafe one is deliberately ugly to type and
is recorded in the ledger as a policy risk.
"""
from __future__ import annotations

from adapters.base import DispatchPolicy

# What an unattended caller (Foreman, scheduler) gets: an agent session in its
# own git worktree.
AGENT_WORKTREE = DispatchPolicy(mode="agent", isolation="worktree",
                                permission_mode="acceptEdits")

# Same, but for read-mostly tasks where a worktree is not needed yet.
AGENT_IN_PLACE = DispatchPolicy(mode="agent", isolation="none",
                                permission_mode="acceptEdits")


def poc_shell(permission_mode: str = "default") -> DispatchPolicy:
    """Test-only: raw shell job with **no filesystem isolation**.

    Only valid for throw-away sandbox repos. The ledger records
    `UNISOLATED_SHELL` for every work item dispatched this way.
    """
    return DispatchPolicy(mode="shell", isolation="none",
                          permission_mode=permission_mode,
                          allow_unisolated_shell=True)


def agent_worktree(permission_mode: str = "acceptEdits") -> DispatchPolicy:
    return DispatchPolicy(mode="agent", isolation="worktree",
                          permission_mode=permission_mode)
