"""Recording an applied approval decision in the journal (ADR 0082).

``record_decision`` is called by ``ApprovalCommandService`` after the decision is
persisted, in the decision's transaction inside a savepoint, so a journal failure is
logged and never loses the decision.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from core.application.workflows.approval_review import (
    captured_review,
    public_review_changes,
    public_review_decision,
)

from .approval_assignees import job_workflow_ids
from .approval_journal import ApprovalJournalEntry, ApprovalJournalRepository
from .approval_repository import checked_review_decision, decode_job_meta
from .command_models_api_inputs import ApprovalDecisionInput
from .scope import ScopeContext

logger = logging.getLogger(__name__)

_JOURNAL_SAVEPOINT = "approval_decision_journal_entry"


def _one(cur: Any) -> dict[str, Any] | None:
    row = cur.fetchone()
    if row is None:
        return None
    if isinstance(row, Mapping):
        return dict(row)
    names = [column[0] for column in (cur.description or [])]
    return dict(zip(names, row, strict=False))


def decided_by_kind(scope: ScopeContext, decision: ApprovalDecisionInput) -> str:
    """``external`` for a decision relayed for an outside actor, else :func:`_scope_kind`."""
    if decision.external_actor is not None:
        return "external"
    return _scope_kind(scope)


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _decision_action(decision: ApprovalDecisionInput) -> str:
    action = str(decision.action or "").strip()
    if action:
        return action
    return "approve" if decision.approved else "reject"


def _review_values(
    payload: Mapping[str, Any], decision: ApprovalDecisionInput
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The reviewed values at decision time and what the decider changed, both masked.

    Masked exactly as the job's own decision history is: the journal is read by
    people who may not see secret values. ``changed_values`` maps a field to its
    ``before``/``after``; a gate without review fields has neither.
    """
    review = captured_review(payload)
    if review is None:
        return {}, {}
    checked = checked_review_decision(payload, decision)
    if checked is None:
        return {}, {}
    values, changes = checked
    reviewed = public_review_decision(review, values).get("values") or {}
    changed = {
        str(change.get("field") or ""): {
            "before": change.get("before"),
            "after": change.get("after"),
        }
        for change in public_review_changes(review, changes)
        if str(change.get("field") or "")
    }
    return dict(reviewed), changed


def build_journal_entry(
    *,
    row: Mapping[str, Any],
    payload: Mapping[str, Any],
    decision: ApprovalDecisionInput,
    scope: ScopeContext,
    workflow_name: str | None = None,
) -> ApprovalJournalEntry:
    """The journal row of one applied decision, from the locked job row and its gate."""
    meta = decode_job_meta(row.get("meta"))
    submitted_by = meta.get("submitted_by")
    started_by = (
        _text(submitted_by.get("id")) if isinstance(submitted_by, Mapping) else _text(submitted_by)
    )
    reviewed, changed = _review_values(payload, decision)
    external_actor = (
        decision.external_actor.model_dump(mode="json")
        if decision.external_actor is not None
        else None
    )
    kind = decided_by_kind(scope, decision)
    return ApprovalJournalEntry(
        account_id=str(row.get("account_id") or scope.account_id),
        job_id=str(row.get("id") or ""),
        workflow_id=_text(row.get("selected_workflow") or row.get("selected_workflow_key")),
        workflow_name=_text(workflow_name),
        project_id=_text(row.get("project_id")),
        step_id=_text(payload.get("step_id")),
        step_label=_text(payload.get("stage")),
        started_by=started_by,
        started_at=row.get("started_at") or row.get("queued_at"),
        decided_by=scope.actor_name,
        decided_by_user_id=scope.user_id if kind == "user" else None,
        decided_by_kind=kind,
        external_actor=external_actor,
        action=_decision_action(decision),
        comment=decision.comment or None,
        reviewed_values=reviewed,
        changed_values=changed,
    )


def _workflow_name(cur: Any, *, account_id: str, workflow_id: str | None) -> str | None:
    if not workflow_id:
        return None
    cur.execute(
        """SELECT display_name FROM public.registry_workflows
            WHERE workflow_key = %s AND deleted_at IS NULL AND account_id = %s
            LIMIT 1""",
        (workflow_id, account_id),
    )
    found = _one(cur)
    return _text((found or {}).get("display_name"))


def _step_display_name(
    cur: Any, *, workflow_ids: list[str], step_id: str | None, account_id: str
) -> str | None:
    """The step's display name in the account's workflow definition, or ``None``."""
    if not step_id or not workflow_ids:
        return None
    cur.execute(
        """SELECT display_name FROM public.registry_workflow_steps
            WHERE owner_workflow_id = ANY(%s) AND step_id = %s
              AND display_name <> '' AND account_id = %s
            LIMIT 1""",
        (workflow_ids, step_id, account_id),
    )
    found = _one(cur)
    return _text((found or {}).get("display_name"))


def _user_display_name(
    cur: Any, *, account_id: str, user_id: str | None = None, username: str | None = None
) -> str | None:
    """A user's display name by id, else by username within the account; ``None`` if none."""
    if user_id:
        cur.execute(
            """SELECT display_name FROM public.users
                WHERE id::text = %s AND account_id::text = %s
                LIMIT 1""",
            (str(user_id), str(account_id)),
        )
    elif username:
        cur.execute(
            """SELECT display_name FROM public.users
                WHERE username = %s AND account_id::text = %s
                LIMIT 1""",
            (str(username), str(account_id)),
        )
    else:
        return None
    found = _one(cur)
    return _text((found or {}).get("display_name"))


#: ``submitted_by`` kinds whose id is a username of the account's ``users``.
_PERSON_STARTER_KINDS = frozenset({"", "person", "user"})


def _started_by_display_name(cur: Any, *, account_id: str, submitted_by: Any) -> str | None:
    """The display name of who or what started the run, by the kind of its starter.

    A person (also through a personal token, recorded as the token's owner; a bare
    string from an older run) is read from ``users`` by username. A service account
    is named by itself and any other source (schedule, webhook, provider, workflow,
    automation) by the ``display_name`` stamped with it; neither is looked up in
    ``users``, where its id would never match.
    """
    if isinstance(submitted_by, Mapping):
        starter_id = _text(submitted_by.get("id"))
        kind = str(submitted_by.get("kind") or "").strip().lower()
        own_name = _text(submitted_by.get("display_name"))
    else:
        starter_id, kind, own_name = _text(submitted_by), "", None
    if not starter_id:
        return None
    if kind not in _PERSON_STARTER_KINDS:
        return own_name or starter_id
    return _user_display_name(cur, account_id=account_id, username=starter_id) or own_name


def _with_names(
    conn: Any,
    entry: ApprovalJournalEntry,
    *,
    workflow_ids: list[str],
    submitted_by: Any = None,
) -> ApprovalJournalEntry:
    """The entry with the names a reader sees, as they are when the decision is recorded.

    The workflow's display name; the step label the gate carried, else the step's
    display name in the workflow definition, else its id; the display name of who
    started the run (from the job's ``submitted_by``, see
    :func:`_started_by_display_name`) and of who decided (a person only, read by
    user id, else username).
    """
    ids = [item for item in dict.fromkeys([entry.workflow_id or "", *workflow_ids]) if item]
    with conn.cursor() as cur:
        workflow_name = _workflow_name(
            cur, account_id=entry.account_id, workflow_id=entry.workflow_id
        )
        step_label = entry.step_label or (
            _step_display_name(
                cur, workflow_ids=ids, step_id=entry.step_id, account_id=entry.account_id
            )
            or entry.step_id
        )
        started_by_name = _started_by_display_name(
            cur,
            account_id=entry.account_id,
            submitted_by=submitted_by if submitted_by is not None else entry.started_by,
        )
        decided_by_name = (
            _user_display_name(
                cur,
                account_id=entry.account_id,
                user_id=entry.decided_by_user_id,
                username=entry.decided_by,
            )
            if entry.decided_by_kind == "user"
            else None
        )
    return ApprovalJournalEntry(
        **{
            **asdict(entry),
            "workflow_name": workflow_name or entry.workflow_name,
            "step_label": step_label,
            "started_by_display_name": started_by_name,
            "decided_by_display_name": decided_by_name,
        }
    )


def record_decision(
    conn: Any,
    *,
    row: Mapping[str, Any],
    payload: Mapping[str, Any],
    decision: ApprovalDecisionInput,
    scope: ScopeContext,
    in_transaction: bool,
) -> bool:
    """Append the journal row of an applied decision; ``False`` when it could not be.

    ``in_transaction`` wraps the write in a savepoint, so a failed insert rolls back
    only itself and the decision's transaction commits. A failure is logged with the
    job id and never raised: the journal must not lose a decision.
    """
    if conn is None or not callable(getattr(conn, "cursor", None)):
        logger.error(
            "Approval decision journal skipped for job %s: no usable connection",
            row.get("id"),
        )
        return False
    savepoint_open = False
    try:
        if in_transaction:
            with conn.cursor() as cur:
                cur.execute(f"SAVEPOINT {_JOURNAL_SAVEPOINT}")
            savepoint_open = True
        entry = build_journal_entry(row=row, payload=payload, decision=decision, scope=scope)
        entry = _with_names(
            conn,
            entry,
            workflow_ids=job_workflow_ids(row),
            submitted_by=decode_job_meta(row.get("meta")).get("submitted_by"),
        )
        ApprovalJournalRepository(conn).append(entry)
        if savepoint_open:
            with conn.cursor() as cur:
                cur.execute(f"RELEASE SAVEPOINT {_JOURNAL_SAVEPOINT}")
        return True
    except Exception:
        logger.exception(
            "Approval decision journal write failed for job %s; the decision stands",
            row.get("id"),
        )
        if savepoint_open:
            try:
                with conn.cursor() as cur:
                    cur.execute(f"ROLLBACK TO SAVEPOINT {_JOURNAL_SAVEPOINT}")
                    cur.execute(f"RELEASE SAVEPOINT {_JOURNAL_SAVEPOINT}")
            except Exception:
                logger.exception("Approval decision journal savepoint rollback failed")
                raise
        return False


def _scope_kind(scope: ScopeContext) -> str:
    """Who acted: ``service_account``, ``user`` (a person with a user id) or ``automation``.

    The system actor (local CLI, internal callers) and machine tokens are service
    accounts. Anything else without a user id (a reaction, a dispatched command, a
    worker) is ``automation``: never recorded as a person.
    """
    principal_type = str(scope.principal_type or "").strip().lower()
    auth_source = str(scope.auth_source or "").strip().lower()
    if principal_type in {"service_account", "system_actor"} or auth_source == "machine":
        return "service_account"
    if str(scope.user_id or "").strip():
        return "user"
    return "automation"


def record_override(
    conn: Any,
    *,
    job: Mapping[str, Any],
    scope: ScopeContext,
    workflow_id: str | None,
    step_id: str | None,
    original_value: Any,
    override_value: Any,
    reason: str | None,
) -> bool:
    """Journal a decision override of an Approval step's outcome (action ``override``).

    Written right after the override is stored, outside its transaction. A failure
    is logged with the job id and never raised: the override stands.
    """
    if conn is None or not callable(getattr(conn, "cursor", None)):
        logger.error(
            "Approval decision journal skipped for override of job %s: no usable connection",
            job.get("id"),
        )
        return False
    try:
        meta = decode_job_meta(job.get("meta"))
        submitted_by = meta.get("submitted_by")
        kind = _scope_kind(scope)
        entry = ApprovalJournalEntry(
            account_id=str(job.get("account_id") or scope.account_id),
            job_id=str(job.get("id") or ""),
            workflow_id=_text(workflow_id),
            project_id=_text(job.get("project_id")),
            step_id=_text(step_id),
            started_by=(
                _text(submitted_by.get("id"))
                if isinstance(submitted_by, Mapping)
                else _text(submitted_by)
            ),
            started_at=job.get("started_at") or job.get("queued_at"),
            decided_by=scope.actor_name,
            decided_by_user_id=scope.user_id if kind == "user" else None,
            decided_by_kind=kind,
            action="override",
            comment=_text(reason),
            changed_values={"outcome": {"before": original_value, "after": override_value}},
        )
        entry = _with_names(
            conn, entry, workflow_ids=job_workflow_ids(job), submitted_by=submitted_by
        )
        ApprovalJournalRepository(conn).append(entry)
        return True
    except Exception:
        logger.exception(
            "Approval decision journal write failed for the override of job %s; "
            "the override stands",
            job.get("id"),
        )
        return False


__all__ = [
    "build_journal_entry",
    "decided_by_kind",
    "record_decision",
    "record_override",
]
