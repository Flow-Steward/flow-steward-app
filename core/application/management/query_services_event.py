# ruff: noqa: F403, F405
from __future__ import annotations

from .query_services_common import *  # NOSONAR


class EventQueryService:
    def __init__(
        self,
        *,
        get_logger: Callable[[], Any],
        event_project_name: Callable[[Any, Any, dict[str, str]], str],
        resolve_domain_event_by_ref: Callable[..., dict[str, Any] | None],
        serialize_domain_event_summary: Callable[..., dict[str, Any]],
        serialize_domain_event_datetime: Callable[[Any], str | None],
        to_json_obj: Callable[[Any], Any],
        workflow_exists: Callable[[str], bool] | None = None,
    ):
        self._get_logger = get_logger
        self._event_project_name = event_project_name
        self._resolve_domain_event_by_ref = resolve_domain_event_by_ref
        self._serialize_domain_event_summary = serialize_domain_event_summary
        self._serialize_domain_event_datetime = serialize_domain_event_datetime
        self._to_json_obj = to_json_obj
        self._workflow_exists = workflow_exists or (
            lambda workflow_id: bool(str(workflow_id or "").strip())
        )

    @staticmethod
    def _event_row_accessible(scope: ScopeContext, row: dict[str, Any]) -> bool:
        try:
            require_event_access(scope, row)
        except ServiceForbiddenError:
            return False
        return True

    def _resolve_event_for_scope(
        self,
        bl: Any,
        event_ref: str,
        scope: ScopeContext,
    ) -> dict[str, Any] | None:
        try:
            event = self._resolve_domain_event_by_ref(
                bl,
                event_ref,
                account_id=scope.account_id,
                allowed_project_ids=scope.allowed_project_ids,
                allowed_workflow_ids=scope.allowed_workflow_ids,
            )
        except TypeError:
            event = self._resolve_domain_event_by_ref(bl, event_ref)
        if not isinstance(event, dict):
            return None
        require_event_access(scope, event)
        return dict(event)

    @staticmethod
    def _parse_iso_date_param(raw: str) -> date | None:
        text = str(raw or "").strip()
        if not text:
            return None
        return date.fromisoformat(text)

    def list_events(
        self,
        *,
        workflow_id: str = "",
        event_family: str = "",
        event_category: str = "",
        event_type: str = "",
        status: str = "",
        from_date: str = "",
        to_date: str = "",
        page: int = 1,
        per_page: int = 0,
        limit: int = 25,
        scope: ScopeContext | None = None,
    ) -> EventsListView:
        resolved_scope = _service_scope(scope)
        bl = self._get_logger()
        if not bl or not hasattr(bl, "list_domain_events"):
            return EventsListView(events=[], total=0)

        try:
            from_day = self._parse_iso_date_param(from_date)
            to_day = self._parse_iso_date_param(to_date)
        except ValueError as exc:
            raise ServiceValidationError("Invalid date format. Use YYYY-MM-DD.") from exc
        if from_day and to_day and from_day > to_day:
            raise ServiceValidationError("from_date must be <= to_date")

        requested_per_page = int(per_page or 0) or int(limit or 25)
        safe_limit = max(1, min(requested_per_page, 500))
        normalized_page = max(1, int(page or 1))
        offset = (normalized_page - 1) * safe_limit
        normalized_workflow_id = str(workflow_id or "").strip() or None
        normalized_event_family = str(event_family or "").strip().lower() or None
        normalized_event_category = str(event_category or "").strip().lower() or None
        normalized_event_type = str(event_type or "").strip() or None
        normalized_status = str(status or "").strip().lower() or None
        event_type_prefix = None
        if normalized_event_type:
            event_type_prefix = None
        elif normalized_event_family and normalized_event_category:
            event_type_prefix = f"{normalized_event_family}.{normalized_event_category}"
        elif normalized_event_family:
            event_type_prefix = normalized_event_family
        # Use the effective timezone from scope so date filters align with
        # the user's calendar day, not UTC midnight.
        from core.application.settings.timezone_service import (
            day_boundaries_utc,
            resolve_effective_user_tz,
        )

        effective_tz = resolve_effective_user_tz(
            resolved_scope.user_timezone or resolved_scope.project_timezone
        )
        if from_day and to_day:
            occurred_from, _ = day_boundaries_utc(from_day.isoformat(), effective_tz)
            _, occurred_to = day_boundaries_utc(to_day.isoformat(), effective_tz)
        elif from_day:
            occurred_from, _ = day_boundaries_utc(from_day.isoformat(), effective_tz)
            occurred_to = None
        elif to_day:
            occurred_from = None
            _, occurred_to = day_boundaries_utc(to_day.isoformat(), effective_tz)
        else:
            occurred_from = None
            occurred_to = None

        single_project_scope = (
            len(resolved_scope.allowed_project_ids) == 1 and not resolved_scope.allowed_workflow_ids
        )
        single_workflow_scope = (
            len(resolved_scope.allowed_workflow_ids) == 1 and not resolved_scope.allowed_project_ids
        )
        combined_single_scope = (
            len(resolved_scope.allowed_project_ids) == 1
            and len(resolved_scope.allowed_workflow_ids) == 1
        )

        if single_project_scope or single_workflow_scope or combined_single_scope:
            scoped_project_id = (
                str(next(iter(resolved_scope.allowed_project_ids)))
                if resolved_scope.allowed_project_ids
                else None
            )
            scoped_workflow_id = (
                str(next(iter(resolved_scope.allowed_workflow_ids)))
                if resolved_scope.allowed_workflow_ids
                else None
            )
            rows = (
                bl.list_domain_events(
                    account_id=resolved_scope.account_id,
                    project_id=scoped_project_id,
                    workflow_id=normalized_workflow_id or scoped_workflow_id,
                    event_type=normalized_event_type,
                    event_type_prefix=event_type_prefix,
                    status=normalized_status,
                    occurred_from=occurred_from,
                    occurred_to=occurred_to,
                    limit=safe_limit,
                    offset=offset,
                )
                or []
            )
            total = len(rows)
            if hasattr(bl, "count_domain_events"):
                counted_total = bl.count_domain_events(
                    account_id=resolved_scope.account_id,
                    project_id=scoped_project_id,
                    workflow_id=normalized_workflow_id or scoped_workflow_id,
                    event_type=normalized_event_type,
                    event_type_prefix=event_type_prefix,
                    status=normalized_status,
                    occurred_from=occurred_from,
                    occurred_to=occurred_to,
                )
                try:
                    total = int(counted_total)
                except (TypeError, ValueError):
                    total = len(rows)
            rows = [
                dict(raw_row)
                for raw_row in rows
                if isinstance(raw_row, dict)
                and self._event_row_accessible(resolved_scope, dict(raw_row))
            ]
            if len(rows) < min(total, safe_limit):
                total = len(rows)
        elif resolved_scope.allowed_project_ids or resolved_scope.allowed_workflow_ids:
            scan_offset = 0
            batch_size = max(100, min(safe_limit, 500))
            accessible_rows: list[dict[str, Any]] = []
            while True:
                batch = (
                    bl.list_domain_events(
                        account_id=resolved_scope.account_id,
                        event_type=normalized_event_type,
                        event_type_prefix=event_type_prefix,
                        status=normalized_status,
                        occurred_from=occurred_from,
                        occurred_to=occurred_to,
                        limit=batch_size,
                        offset=scan_offset,
                    )
                    or []
                )
                if not batch:
                    break
                for raw_row in batch:
                    if not isinstance(raw_row, dict):
                        continue
                    row = dict(raw_row)
                    if self._event_row_accessible(resolved_scope, row):
                        accessible_rows.append(row)
                scan_offset += len(batch)
                if len(batch) < batch_size:
                    break
            total = len(accessible_rows)
            rows = accessible_rows[offset : offset + safe_limit]
        else:
            rows = (
                bl.list_domain_events(
                    account_id=resolved_scope.account_id,
                    workflow_id=normalized_workflow_id,
                    event_type=normalized_event_type,
                    event_type_prefix=event_type_prefix,
                    status=normalized_status,
                    occurred_from=occurred_from,
                    occurred_to=occurred_to,
                    limit=safe_limit,
                    offset=offset,
                )
                or []
            )
            total = (
                bl.count_domain_events(
                    account_id=resolved_scope.account_id,
                    workflow_id=normalized_workflow_id,
                    event_type=normalized_event_type,
                    event_type_prefix=event_type_prefix,
                    status=normalized_status,
                    occurred_from=occurred_from,
                    occurred_to=occurred_to,
                )
                if hasattr(bl, "count_domain_events")
                else len(rows)
            )
            # Fail-soft: skip rows that fail scope checks so one stale/malformed
            # row does not take down the whole endpoint response.
            scoped_rows: list[dict[str, Any]] = []
            dropped_rows = 0
            for raw_row in rows:
                if not isinstance(raw_row, dict):
                    continue
                row = dict(raw_row)
                if not self._event_row_accessible(resolved_scope, row):
                    dropped_rows += 1
                    continue
                scoped_rows.append(row)
            rows = scoped_rows
            if dropped_rows:
                total = max(0, int(total) - dropped_rows)

        project_name_cache: dict[str, str] = {}
        events = []
        for raw_row in rows:
            if not isinstance(raw_row, dict):
                continue
            row = dict(raw_row)
            project_name = self._event_project_name(bl, row.get("project_id"), project_name_cache)
            events.append(
                DomainEventSummaryView.model_validate(
                    self._serialize_domain_event_summary(row, project_name=project_name)
                )
            )

        facet_rows: list[dict[str, Any]] = []
        facet_loader = getattr(bl, "list_domain_event_facets", None)
        if callable(facet_loader):
            if single_project_scope or single_workflow_scope or combined_single_scope:
                scoped_project_id = (
                    str(next(iter(resolved_scope.allowed_project_ids)))
                    if resolved_scope.allowed_project_ids
                    else None
                )
                scoped_workflow_id = (
                    str(next(iter(resolved_scope.allowed_workflow_ids)))
                    if resolved_scope.allowed_workflow_ids
                    else None
                )
                facet_rows = (
                    facet_loader(
                        account_id=resolved_scope.account_id,
                        project_id=scoped_project_id,
                        workflow_id=normalized_workflow_id or scoped_workflow_id,
                        event_type=normalized_event_type,
                        event_type_prefix=event_type_prefix,
                        occurred_from=occurred_from,
                        occurred_to=occurred_to,
                    )
                    or []
                )
            elif resolved_scope.allowed_project_ids or resolved_scope.allowed_workflow_ids:
                facet_rows = (
                    facet_loader(
                        account_id=resolved_scope.account_id,
                        workflow_id=normalized_workflow_id,
                        event_type=normalized_event_type,
                        event_type_prefix=event_type_prefix,
                        occurred_from=occurred_from,
                        occurred_to=occurred_to,
                    )
                    or []
                )
                facet_rows = [
                    dict(raw_row)
                    for raw_row in facet_rows
                    if isinstance(raw_row, dict)
                    and self._event_row_accessible(
                        resolved_scope,
                        {
                            "project_id": raw_row.get("project_id"),
                            "workflow_id": raw_row.get("workflow_id"),
                            "account_id": resolved_scope.account_id,
                        },
                    )
                ]
            else:
                facet_rows = (
                    facet_loader(
                        account_id=resolved_scope.account_id,
                        workflow_id=normalized_workflow_id,
                        event_type=normalized_event_type,
                        event_type_prefix=event_type_prefix,
                        occurred_from=occurred_from,
                        occurred_to=occurred_to,
                    )
                    or []
                )
            if not isinstance(facet_rows, list):
                facet_rows = []

        def _event_segments(value: Any) -> tuple[str, str]:
            meta = event_type_meta(value)
            return meta["event_family"], meta["event_category"]

        workflow_counts: dict[str, int] = {}
        family_counts: dict[str, int] = {}
        category_counts: dict[str, int] = {}
        event_type_counts: dict[str, int] = {}
        status_counts: dict[str, int] = {}
        for raw_row in facet_rows:
            if not isinstance(raw_row, dict):
                continue
            count = int(raw_row.get("count") or 0)
            workflow_key = str(raw_row.get("workflow_id") or "").strip()
            event_type_key = str(raw_row.get("event_type") or "").strip()
            status_key = str(raw_row.get("status") or "").strip().lower()
            family_key, category_key = _event_segments(event_type_key)
            if workflow_key:
                workflow_counts[workflow_key] = workflow_counts.get(workflow_key, 0) + count
            if family_key:
                family_counts[family_key] = family_counts.get(family_key, 0) + count
            if category_key:
                category_counts[category_key] = category_counts.get(category_key, 0) + count
            if event_type_key:
                event_type_counts[event_type_key] = event_type_counts.get(event_type_key, 0) + count
            if status_key:
                status_counts[status_key] = status_counts.get(status_key, 0) + count

        facets = {
            "workflows": [
                {"value": key, "label": key, "count": value}
                for key, value in sorted(
                    workflow_counts.items(),
                    key=lambda item: (-item[1], item[0]),
                )
            ],
            "event_families": [
                {
                    "value": key,
                    "label": event_type_meta(key).get("event_family_label") or key,
                    "count": value,
                }
                for key, value in sorted(
                    family_counts.items(),
                    key=lambda item: (-item[1], item[0]),
                )
            ],
            "event_categories": [
                {
                    "value": key,
                    "label": event_type_meta(f"x.{key}").get("event_category_label") or key,
                    "count": value,
                }
                for key, value in sorted(
                    category_counts.items(),
                    key=lambda item: (-item[1], item[0]),
                )
            ],
            "event_types": [
                {
                    "value": key,
                    "label": event_type_meta(key).get("event_type_label") or key,
                    "count": value,
                }
                for key, value in sorted(
                    event_type_counts.items(),
                    key=lambda item: (-item[1], item[0]),
                )
            ],
            "statuses": [
                {
                    "value": key,
                    "label": event_status_meta(key).get("label") or key,
                    "count": value,
                    "tone": event_status_meta(key).get("tone") or "gray",
                }
                for key, value in sorted(
                    status_counts.items(),
                    key=lambda item: (-item[1], item[0]),
                )
            ],
        }

        return EventsListView(
            events=events,
            total=int(total),
            page=normalized_page,
            per_page=safe_limit,
            total_pages=max(1, -(-int(total) // safe_limit)),
            facets=facets,
        )

    def get_event_detail(
        self, event_ref: str, *, scope: ScopeContext | None = None
    ) -> DomainEventDetailView:
        resolved_scope = _service_scope(scope)
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(DATABASE_NOT_CONFIGURED_MESSAGE)
        event = self._resolve_event_for_scope(bl, event_ref, resolved_scope)
        if not event:
            raise ServiceNotFoundError("Event not found")

        event_id = str(event.get("event_id") or "").strip()
        if not event_id:
            raise ServiceNotFoundError("Event not found")

        project_name_cache: dict[str, str] = {}
        project_name = self._event_project_name(bl, event.get("project_id"), project_name_cache)
        summary = DomainEventSummaryView.model_validate(
            self._serialize_domain_event_summary(event, project_name=project_name)
        )

        if not hasattr(bl, "list_event_dispatches"):
            raise ServiceUnavailableError("Event dispatch storage is unavailable")
        if not hasattr(bl, "list_event_delivery_attempts") and not hasattr(
            bl, "list_event_delivery_attempts_by_dispatch_ids"
        ):
            raise ServiceUnavailableError("Event delivery attempt storage is unavailable")

        try:
            dispatch_rows = (
                bl.list_event_dispatches(
                    event_id=event_id,
                    account_id=resolved_scope.account_id,
                    limit=500,
                )
                or []
            )
        except Exception as exc:
            raise ServiceUnavailableError("Event dispatch lookup failed") from exc

        dispatch_ids = [
            str(dispatch.get("dispatch_id") or "").strip()
            for dispatch in dispatch_rows
            if isinstance(dispatch, dict) and str(dispatch.get("dispatch_id") or "").strip()
        ]
        attempts_by_dispatch: dict[str, list[dict[str, Any]]] = {}
        if dispatch_ids and hasattr(bl, "list_event_delivery_attempts_by_dispatch_ids"):
            try:
                attempts_lookup = (
                    bl.list_event_delivery_attempts_by_dispatch_ids(
                        dispatch_ids=dispatch_ids,
                        limit_per_dispatch=200,
                    )
                    or {}
                )
                attempts_by_dispatch = attempts_lookup if isinstance(attempts_lookup, dict) else {}
            except Exception as exc:
                raise ServiceUnavailableError("Event delivery attempt lookup failed") from exc

        dispatches: list[DomainEventDispatchView] = []
        for dispatch in dispatch_rows:
            if not isinstance(dispatch, dict):
                continue
            dispatch_id = str(dispatch.get("dispatch_id") or "").strip()
            if not dispatch_id:
                raise ServiceUnavailableError("Malformed event dispatch row")

            require_event_access(
                resolved_scope,
                {
                    "account_id": (
                        dispatch.get("account_id")
                        or event.get("account_id")
                        or resolved_scope.account_id
                    ),
                    "project_id": dispatch.get("project_id")
                    if dispatch.get("project_id") is not None
                    else event.get("project_id"),
                    "workflow_id": dispatch.get("workflow_id") or event.get("workflow_id"),
                },
            )

            if attempts_by_dispatch:
                attempts = attempts_by_dispatch.get(dispatch_id, [])
            else:
                try:
                    attempts = (
                        bl.list_event_delivery_attempts(dispatch_id=dispatch_id, limit=200) or []
                    )
                except Exception as exc:
                    raise ServiceUnavailableError("Event delivery attempt lookup failed") from exc

            attempts_payload: list[DomainEventDispatchAttemptView] = []
            for attempt in attempts:
                if not isinstance(attempt, dict):
                    continue
                attempt_status_meta = event_status_meta(attempt.get("status") or "queued")
                attempts_payload.append(
                    DomainEventDispatchAttemptView(
                        id=attempt.get("id"),
                        dispatch_id=str(attempt.get("dispatch_id") or "").strip(),
                        status=attempt_status_meta["code"],
                        status_label=attempt_status_meta["label"],
                        status_tone=attempt_status_meta["tone"],
                        handler=str(attempt.get("handler") or "").strip() or None,
                        error_text=str(attempt.get("error_text") or "").strip() or None,
                        attempt_no=attempt.get("attempt_no"),
                        attempted_at=self._serialize_domain_event_datetime(
                            attempt.get("finished_at")
                            or attempt.get("started_at")
                            or attempt.get("created_at")
                        ),
                        created_at=self._serialize_domain_event_datetime(attempt.get("created_at")),
                    )
                )

            target = str(dispatch.get("target_label") or "").strip()
            if not target:
                target_type = str(dispatch.get("target_type") or "").strip()
                target_id = str(dispatch.get("target_id") or "").strip()
                target = (
                    f"{target_type}:{target_id}".strip(":") if (target_type or target_id) else "—"
                )
            dispatch_status_meta = event_status_meta(dispatch.get("status") or "matched")
            dispatches.append(
                DomainEventDispatchView(
                    id=dispatch.get("id"),
                    dispatch_id=dispatch_id,
                    target=target,
                    target_type=str(dispatch.get("target_type") or "").strip() or None,
                    target_id=str(dispatch.get("target_id") or "").strip() or None,
                    status=dispatch_status_meta["code"],
                    status_label=dispatch_status_meta["label"],
                    status_tone=dispatch_status_meta["tone"],
                    reason_code=str(dispatch.get("reason_code") or "").strip() or None,
                    reason_detail=str(dispatch.get("reason_detail") or "").strip() or None,
                    attempt_count=dispatch.get("attempt_count"),
                    max_attempts=dispatch.get("max_attempts"),
                    created_at=self._serialize_domain_event_datetime(dispatch.get("created_at")),
                    attempts=attempts_payload,
                )
            )

        commands: list[dict[str, Any]] = []
        if hasattr(bl, "list_command_executions"):
            try:
                command_rows = bl.list_command_executions(event_id=event_id, limit=200) or []
            except Exception as exc:
                raise ServiceUnavailableError("Event command chain lookup failed") from exc
            for command in command_rows:
                if isinstance(command, dict):
                    commands.append(_enrich_event_command_row(command))

        return DomainEventDetailView(
            **summary.dump(),
            payload=self._to_json_obj(event.get("payload")) or {},
            actor=self._to_json_obj(event.get("actor")) or {},
            urls=self._to_json_obj(event.get("urls")) or {},
            envelope=self._to_json_obj(event.get("envelope")) or {},
            source=str(event.get("source") or "").strip() or None,
            aggregate_type=str(event.get("aggregate_type") or "").strip() or None,
            aggregate_id=str(event.get("aggregate_id") or "").strip() or None,
            correlation_id=str(event.get("correlation_id") or "").strip() or None,
            dispatches=dispatches,
            commands=commands,
        )

    def list_workflow_reactions(
        self,
        workflow_id: str,
        *,
        scope_kind: str = "workflow",
        scope_id: str = "",
        scope: ScopeContext | None = None,
    ) -> WorkflowReactionsView:
        resolved = _service_scope(scope, workflow_id=workflow_id)
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(DATABASE_NOT_CONFIGURED_MESSAGE)
        if not self._workflow_exists(workflow_id):
            raise ServiceNotFoundError("Workflow not found")
        require_workflow_access(resolved, workflow_id)
        if resolved.project_id is not None:
            if not hasattr(bl, "get_project"):
                raise ServiceUnavailableError("Project storage is not available")
            project = bl.get_project(resolved.project_id)
            if not project:
                raise ServiceNotFoundError(PROJECT_NOT_FOUND_MESSAGE)
            require_project_access(resolved, project)

        try:
            rows = (
                bl.list_workflow_reactions(
                    workflow_id, enabled_only=False, account_id=resolved.account_id
                )
                if hasattr(bl, "list_workflow_reactions")
                else []
            )
            target_kind, target_scope_id, scoped_rows = serialize_scoped_reaction_rows(
                rows or [],
                scope_kind=scope_kind,
                scope_id=scope_id,
            )
            available_trigger_sources = build_workflow_reaction_trigger_sources(
                bl,
                project_id=resolved.project_id,
                scope_kind=target_kind,
            )
        except ValueError as exc:
            raise ServiceValidationError(str(exc)) from exc

        return WorkflowReactionsView(
            workflow_id=str(workflow_id or "").strip(),
            scope_kind=target_kind,
            scope_id=target_scope_id,
            project_id=resolved.project_id,
            available_trigger_sources=available_trigger_sources,
            reactions=[WorkflowReactionRuleView.model_validate(row) for row in scoped_rows],
        )


__all__ = [
    "EventQueryService",
]
