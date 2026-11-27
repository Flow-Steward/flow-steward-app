"""The approval decision journal: one append-only row per applied decision (ADR 0082).

Every decision that ``ApprovalCommandService`` applies appends exactly one row,
whether or not any module reads the journal (:mod:`.approval_journal_recording`
builds and writes it). ``jobs.meta.approvals`` stays the job's own history; the
journal is additional and outlives the job only when the account is entitled to a
longer journal retention.

Core exposes no update. Rows go only through :meth:`ApprovalJournalRepository.purge`,
which the retention pass calls.

The caller owns the connection and its transaction. Rows come back as dicts whether
the cursor yields mappings or tuples.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

# Building an entry from a decision lives in approval_journal_recording: the
# retention pass of the control role imports this module for purge only and must not
# pull the approval runtime into its source slice.

APPROVAL_JOURNAL_FEATURE = "approval_journal"
APPROVAL_JOURNAL_RETENTION_SETTING = "approval_journal_retention_days"
DECIDED_BY_KINDS = ("user", "service_account", "external", "automation")
_MAX_LIMIT = 500

_COLUMNS = (
    "id",
    "account_id",
    "job_id",
    "workflow_id",
    "workflow_name",
    "project_id",
    "step_id",
    "step_label",
    "started_by",
    "started_by_display_name",
    "started_at",
    "decided_by",
    "decided_by_display_name",
    "decided_by_user_id",
    "decided_by_kind",
    "external_actor",
    "action",
    "comment",
    "reviewed_values",
    "changed_values",
    "decided_at",
)
_JSON_COLUMNS = frozenset({"external_actor", "reviewed_values", "changed_values"})


@dataclass(frozen=True)
class ApprovalJournalEntry:
    """One applied decision as the journal keeps it."""

    account_id: str
    job_id: str
    decided_by: str
    decided_by_kind: str
    action: str
    workflow_id: str | None = None
    workflow_name: str | None = None
    project_id: str | None = None
    step_id: str | None = None
    step_label: str | None = None
    started_by: str | None = None
    #: The starter's and the decider's display names as they were when the decision
    #: was recorded; ``None`` when the person has none (or is not a user).
    started_by_display_name: str | None = None
    started_at: Any = None
    decided_by_display_name: str | None = None
    decided_by_user_id: str | None = None
    external_actor: dict[str, Any] | None = None
    comment: str | None = None
    reviewed_values: dict[str, Any] = field(default_factory=dict)
    changed_values: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def __post_init__(self) -> None:
        if self.decided_by_kind not in DECIDED_BY_KINDS:
            raise ValueError(f"decided_by_kind must be one of {DECIDED_BY_KINDS}")


def _rows(cur: Any) -> list[dict[str, Any]]:
    rows = cur.fetchall() or []
    names = [column[0] for column in (cur.description or [])]
    return [
        dict(row) if isinstance(row, Mapping) else dict(zip(names, row, strict=False))
        for row in rows
    ]


def _one(cur: Any) -> dict[str, Any] | None:
    row = cur.fetchone()
    if row is None:
        return None
    if isinstance(row, Mapping):
        return dict(row)
    names = [column[0] for column in (cur.description or [])]
    return dict(zip(names, row, strict=False))


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


#: Whether the decision's run still has a page. A job in the trash keeps one (its
#: detail route reads it regardless of ``deleted_at`` and it can be restored), so only
#: a hard-deleted job, or one of another account, reads as gone.
_JOB_EXISTS_SQL = (
    "EXISTS (SELECT 1 FROM public.jobs job "
    "WHERE job.id = j.job_id AND job.account_id = j.account_id) AS job_exists"
)


def _entry_view(row: Mapping[str, Any]) -> dict[str, Any]:
    view: dict[str, Any] = {}
    for column in _COLUMNS:
        value = row.get(column)
        if column in _JSON_COLUMNS:
            value = _json_value(value)
        elif isinstance(value, datetime):
            value = value.isoformat()
        elif isinstance(value, uuid.UUID):
            value = str(value)
        view[column] = value
    if "job_exists" in row:
        view["job_exists"] = bool(row.get("job_exists"))
    return view


class ApprovalJournalRepository:
    """Append, list and purge journal rows. No update exists."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def append(self, entry: ApprovalJournalEntry | Mapping[str, Any]) -> None:
        if not isinstance(entry, ApprovalJournalEntry):
            # A mapping is validated exactly as the dataclass is.
            entry = ApprovalJournalEntry(**dict(entry))
        values = asdict(entry)
        columns = [column for column in _COLUMNS if column != "decided_at"]
        params = [
            json.dumps(values.get(column), sort_keys=True, default=str)
            if column in _JSON_COLUMNS and values.get(column) is not None
            else values.get(column)
            for column in columns
        ]
        placeholders = ", ".join(
            "%s::jsonb" if column in _JSON_COLUMNS else "%s" for column in columns
        )
        with self.conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO public.approval_decision_journal ({', '.join(columns)}) "  # nosec B608 - fixed column list
                f"VALUES ({placeholders})",
                tuple(params),
            )

    def list_entries(
        self,
        account_id: str,
        *,
        workflow_id: str | None = None,
        decided_by: str | None = None,
        date_from: datetime | str | None = None,
        date_to: datetime | str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """One page of an account's journal, newest first, with the matching total.

        Each item carries ``job_exists``: ``False`` once the run was hard-deleted, so a
        reader can show a plain marker instead of a dead link. A trashed run still
        exists (its page opens and it can be restored).
        """
        clauses = ["j.account_id = %s"]
        params: list[Any] = [str(account_id)]
        if str(workflow_id or "").strip():
            clauses.append("j.workflow_id = %s")
            params.append(str(workflow_id).strip())
        if str(decided_by or "").strip():
            clauses.append("j.decided_by = %s")
            params.append(str(decided_by).strip())
        if date_from:
            clauses.append("j.decided_at >= %s")
            params.append(date_from)
        if date_to:
            clauses.append("j.decided_at < %s")
            params.append(date_to)
        where = " AND ".join(clauses)
        bounded_limit = max(1, min(_MAX_LIMIT, int(limit or 50)))
        bounded_offset = max(0, int(offset or 0))
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT count(*) AS total FROM public.approval_decision_journal j WHERE {where}",  # nosec B608 - fixed clauses
                tuple(params),
            )
            total_row = _one(cur) or {}
            cur.execute(
                f"SELECT {', '.join(f'j.{column}' for column in _COLUMNS)}, {_JOB_EXISTS_SQL} "  # nosec B608 - fixed clauses
                f"FROM public.approval_decision_journal j "
                f"WHERE {where} ORDER BY j.decided_at DESC, j.id DESC LIMIT %s OFFSET %s",
                (*params, bounded_limit, bounded_offset),
            )
            rows = _rows(cur)
        total = total_row.get("total")
        if total is None and total_row:
            total = next(iter(total_row.values()))
        return {"items": [_entry_view(row) for row in rows], "total": int(total or 0)}

    def purge(
        self,
        account_id: str | None = None,
        *,
        older_than: datetime | None,
        only_orphaned: bool,
    ) -> int:
        """Delete journal rows; the number deleted.

        ``older_than`` limits the purge to rows decided before it (``None``: any
        age). ``only_orphaned`` limits it to rows whose job no longer exists, which
        is how rows go with their job by default. ``account_id`` ``None`` purges
        every account.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if account_id is not None:
            clauses.append("j.account_id = %s")
            params.append(str(account_id))
        if older_than is not None:
            clauses.append("j.decided_at < %s")
            params.append(older_than)
        if only_orphaned:
            clauses.append("NOT EXISTS (SELECT 1 FROM public.jobs job WHERE job.id = j.job_id)")
        where = " AND ".join(clauses) if clauses else "TRUE"
        with self.conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM public.approval_decision_journal j WHERE {where}",  # nosec B608 - fixed clauses
                tuple(params),
            )
            return int(getattr(cur, "rowcount", 0) or 0)


def _retention_days(value: Any) -> int | None:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return None


def require_journal_retention_entitled(
    settings: Mapping[str, Any],
    *,
    account_id: str,
    entitlement_service: Any,
    stored_days: Any = None,
) -> None:
    """Refuse raising ``approval_journal_retention_days`` for an unentitled account.

    Raises ``FeatureNotEntitledError`` (403, ``feature_not_entitled``) only for a
    positive value that differs from ``stored_days``, the account's saved value.
    Zero, an absent field, an unreadable value and the unchanged saved value pass, so
    an account whose entitlement ended still saves the rest of the form (and can set
    0). ``stored_days`` of ``None`` means the saved value is unknown: any positive
    value then needs the entitlement. Without an entitlement service nothing is
    entitled (ADR 0082, section 3).
    """
    if not isinstance(settings, Mapping) or APPROVAL_JOURNAL_RETENTION_SETTING not in settings:
        return
    days = _retention_days(settings.get(APPROVAL_JOURNAL_RETENTION_SETTING))
    if days is None or days <= 0:
        return
    if stored_days is not None and _retention_days(stored_days) == days:
        return
    is_entitled = getattr(entitlement_service, "is_entitled", None)
    if callable(is_entitled) and is_entitled(str(account_id or ""), APPROVAL_JOURNAL_FEATURE):
        return
    from core.application.settings.platform_contracts import FeatureNotEntitledError

    raise FeatureNotEntitledError(APPROVAL_JOURNAL_FEATURE)


__all__ = [
    "APPROVAL_JOURNAL_FEATURE",
    "APPROVAL_JOURNAL_RETENTION_SETTING",
    "DECIDED_BY_KINDS",
    "ApprovalJournalEntry",
    "ApprovalJournalRepository",
    "require_journal_retention_entitled",
]
