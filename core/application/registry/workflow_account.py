"""The account whose workflows a request or a job works on (ADR 0020, step C4b).

Workflow identity is account-qualified: two accounts may own a workflow with the
same id. Every workflow read and write therefore names an account. Most code passes
it explicitly; code that does not (the many helpers that predate account-qualified
identity) reads the account bound here:

- an authenticated request binds its principal's account for the rest of the
  request (``auth_http_runtime``), so a request of account B never reaches account
  A's workflow with the same id;
- a worker binds the job's account while it runs that job.

Nothing bound and no explicit account is refused (ADR 0020, step C5): a read or
write that cannot name its account raises :class:`WorkflowAccountMissingError`
instead of silently working on the installation's account. Bootstrap and
installation paths that act for the installation account (database init and seed,
the process-wide workflow cache, upgrade tooling) say so explicitly with
:func:`installation_account_scope`. The binding is a :class:`contextvars.ContextVar`,
so it follows the request task and the worker call, never another request or job.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.infrastructure.config.account_scope import MissingAccountScopeError

_BOUND_WORKFLOW_ACCOUNT: ContextVar[str | None] = ContextVar(
    "flow_steward_workflow_account", default=None
)


class WorkflowAccountMissingError(MissingAccountScopeError):
    """A registry read or write named no account and none is bound.

    A ``ValueError`` (through :class:`MissingAccountScopeError`), so handlers of
    invalid input refuse the operation instead of running it for another account.
    """

    def __init__(self, detail: str = "") -> None:
        super().__init__("registry operation", detail or "no account given and none bound")


def _clean(value: Any) -> str:
    return str(value or "").strip()


def bound_workflow_account() -> str | None:
    """The account bound to the current request or job, if any."""
    return _BOUND_WORKFLOW_ACCOUNT.get()


def bind_workflow_account(account_id: Any) -> None:
    """Bind *account_id* for the rest of the current request task.

    Used where the binding ends with the task (the HTTP request); a worker uses
    :func:`workflow_account_scope`, which restores the previous binding.
    """
    _BOUND_WORKFLOW_ACCOUNT.set(_clean(account_id) or None)


@contextmanager
def workflow_account_scope(account_id: Any) -> Iterator[str]:
    """Bind *account_id* while the block runs, then restore the previous binding.

    An empty *account_id* is refused: a scope without an account would otherwise
    clear the binding or hand the block to whichever account happens to be bound.
    """
    owner = _clean(account_id)
    if not owner:
        raise WorkflowAccountMissingError("an account scope was opened without an account")
    token = _BOUND_WORKFLOW_ACCOUNT.set(owner)
    try:
        yield owner
    finally:
        _BOUND_WORKFLOW_ACCOUNT.reset(token)


@contextmanager
def account_scope_if_named(account_id: Any) -> Iterator[str | None]:
    """Bind *account_id* when it names one; otherwise bind nothing.

    For a unit of work whose row normally carries its account (a job): with one,
    the work runs in that account; without one, nothing is bound and every
    registry read or write inside refuses (:class:`WorkflowAccountMissingError`)
    instead of falling back to the installation account. The unit's own checks
    can still refuse it first.
    """
    owner = _clean(account_id)
    if not owner:
        yield None
        return
    with workflow_account_scope(owner) as bound:
        yield bound


@contextmanager
def installation_account_scope() -> Iterator[str]:
    """Bind the installation account for an explicitly documented bootstrap path.

    Only code that acts for the installation itself uses this: database init and
    seed, the process-wide workflow cache, and upgrade or install tooling that has
    no request or job. Everything else names its account or runs inside a request
    or job that bound one.
    """
    with workflow_account_scope(DEFAULT_ACCOUNT_ID) as account_id:
        yield account_id


def workflow_owner(account_id: Any = None) -> str:
    """The account a workflow read or write works on.

    An explicit *account_id* wins; otherwise the account bound to the current
    request or job. With neither, the operation is refused
    (:class:`WorkflowAccountMissingError`); it never falls back to the
    installation account.
    """
    owner = _clean(account_id) or _clean(_BOUND_WORKFLOW_ACCOUNT.get())
    if not owner:
        raise WorkflowAccountMissingError()
    return owner


__all__ = [
    "WorkflowAccountMissingError",
    "account_scope_if_named",
    "bind_workflow_account",
    "bound_workflow_account",
    "installation_account_scope",
    "workflow_account_scope",
    "workflow_owner",
]
