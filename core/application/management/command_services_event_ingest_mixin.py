# ruff: noqa: F403, F405
# mypy: ignore-errors
from __future__ import annotations

from .command_services_common import *  # NOSONAR


class EventCommandServiceIngestMixin:
    def _provider_event_channel_ref(self, *, provider_key: str, payload: dict[str, Any]) -> str:
        canonical_provider = str(provider_key or "").strip().lower()
        if "." not in canonical_provider:
            canonical_provider = f"flowsteward.{canonical_provider}"
        provider_namespace = canonical_provider.rsplit(".", 1)[-1]
        nested_payload = self._to_json_obj(payload.get("payload")) or {}
        provider_payload = (
            self._to_json_obj(payload.get(canonical_provider))
            or self._to_json_obj(payload.get(provider_namespace))
            or self._to_json_obj(nested_payload.get(canonical_provider))
            or self._to_json_obj(nested_payload.get(provider_namespace))
            or {}
        )
        return str(
            provider_payload.get("channel_id")
            or provider_payload.get("chat_id")
            or payload.get("channel_id")
            or payload.get("chat_id")
            or ""
        ).strip()

    def _provider_human_decision_channel_authorized(
        self,
        bl: Any,
        *,
        project_id: str,
        provider_key: str,
        payload: dict[str, Any],
    ) -> bool:
        canonical_provider = str(provider_key or "").strip().lower()
        if "." not in canonical_provider:
            canonical_provider = f"flowsteward.{canonical_provider}"
        provider_namespace = canonical_provider.rsplit(".", 1)[-1]
        channel_ref = self._provider_event_channel_ref(
            provider_key=canonical_provider, payload=payload
        )
        if not channel_ref:
            return False
        try:
            connections = []
            if hasattr(bl, "list_project_connections"):
                rows = bl.list_project_connections(
                    project_id=str(project_id),
                    enabled_only=True,
                    extension_id=canonical_provider,
                )
                connections = list(rows) if isinstance(rows, list) else []
            if not connections and hasattr(bl, "get_project_connection"):
                connection = (
                    bl.get_project_connection(
                        project_id=str(project_id),
                        extension_id=canonical_provider,
                        enabled_only=True,
                    )
                    or {}
                )
                if isinstance(connection, dict):
                    connection.setdefault("extension_id", canonical_provider)
                connections = [connection] if connection else []
        except Exception:
            return False
        for connection in connections:
            normalized = self._to_json_obj(connection) or {}
            connection_extension_id = str(normalized.get("extension_id") or "").strip().lower()
            if connection_extension_id != canonical_provider:
                continue
            connection_config = self._to_json_obj(normalized.get("connection_config")) or {}
            connection_provider = (
                str(connection_config.get("provider_key") or normalized.get("provider_key") or "")
                .strip()
                .lower()
            )
            if connection_provider and connection_provider not in {
                canonical_provider,
                provider_namespace,
            }:
                continue
            channels = connection_config.get("channels")
            for raw_channel in channels if isinstance(channels, list) else []:
                channel = self._to_json_obj(raw_channel) or {}
                refs = {str(channel.get(key) or "").strip() for key in ("channel_id", "chat_id")}
                if channel_ref not in refs:
                    continue
                capabilities = self._to_json_obj(channel.get("capabilities")) or {}
                return channel.get("enabled") is not False and bool(
                    capabilities.get("human_decision")
                )
            fallback_ref = str(
                connection_config.get("channel_id") or connection_config.get("chat_id") or ""
            ).strip()
            if fallback_ref == channel_ref:
                return True
        return False

    def _provider_event_binding_authorized(
        self, bl: Any, *, project_id: str, provider_key: str
    ) -> bool:
        canonical_provider = str(provider_key or "").strip().lower()
        if "." not in canonical_provider:
            canonical_provider = f"flowsteward.{canonical_provider}"
        try:
            if hasattr(bl, "get_project_connection"):
                connection = bl.get_project_connection(
                    project_id=str(project_id),
                    extension_id=canonical_provider,
                    enabled_only=True,
                )
                if isinstance(connection, dict) and connection:
                    row_extension = (
                        str(connection.get("extension_id") or canonical_provider).strip().lower()
                    )
                    if row_extension == canonical_provider:
                        return True
            if hasattr(bl, "list_extension_callback_registrations"):
                rows = bl.list_extension_callback_registrations(
                    extension_id=canonical_provider,
                    project_id=str(project_id),
                )
                return any(
                    isinstance(row, dict)
                    and row.get("enabled") is not False
                    and str(row.get("extension_id") or canonical_provider).strip().lower()
                    == canonical_provider
                    for row in (rows if isinstance(rows, list) else [])
                )
        except Exception:
            return False
        return False

    def redrive_event(
        self,
        event_ref: str,
        *,
        scope: ScopeContext | None = None,
    ) -> EventRedriveResultView:
        resolved_scope = _service_scope(scope)
        # Redrive replays delivery side effects (re-queues failed/dead-lettered
        # dispatches). Event *visibility* (require_event_access below) is not the
        # same as authority to replay; require operator-level role.
        _require_role(resolved_scope, "operator")
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(ERR_DATABASE_NOT_CONFIGURED)
        if not hasattr(bl, "list_event_dispatches") or not hasattr(
            bl, "update_event_dispatch_status"
        ):
            raise ServiceUnavailableError("Event dispatch storage is unavailable")

        try:
            event = self._resolve_domain_event_by_ref(
                bl,
                event_ref,
                account_id=resolved_scope.account_id,
                allowed_project_ids=resolved_scope.allowed_project_ids,
                allowed_workflow_ids=resolved_scope.allowed_workflow_ids,
            )
        except TypeError:
            event = self._resolve_domain_event_by_ref(bl, event_ref)
        if not event:
            raise ServiceNotFoundError("Event not found")
        require_event_access(
            resolved_scope,
            dict(event),
        )
        event_id = str(event.get("event_id") or "").strip()
        if not event_id:
            raise ServiceNotFoundError("Event not found")
        event_status = str(event.get("status") or "").strip().lower()
        if event_status not in {"failed", "dead_lettered"}:
            raise ServiceConflictError("Only failed/dead_lettered events can be redriven.")

        dispatches = (
            bl.list_event_dispatches(
                event_id=event_id,
                account_id=resolved_scope.account_id,
                limit=1000,
            )
            or []
        )
        failed_dispatches = []
        seen_dispatch_ids: set[str] = set()
        for dispatch in dispatches:
            if not isinstance(dispatch, dict):
                continue
            dispatch_id = str(dispatch.get("dispatch_id") or "").strip()
            if not dispatch_id or dispatch_id in seen_dispatch_ids:
                continue
            seen_dispatch_ids.add(dispatch_id)

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
            state = str(dispatch.get("status") or "").strip().lower()
            if state in {"failed", "dead_lettered"}:
                failed_dispatches.append(dispatch_id)
        if not failed_dispatches:
            raise ServiceConflictError("No failed dispatches to redrive.")

        redriven: list[str] = []
        requested_by = (
            str(resolved_scope.actor_name or "").strip()
            or str(resolved_scope.actor_id or "").strip()
            or "operator"
        )
        for dispatch_id in failed_dispatches:
            try:
                updated = bl.update_event_dispatch_status(
                    dispatch_id,
                    status="queued",
                    reason_code="manual_redrive",
                    reason_detail=f"requested_by:{requested_by}",
                    next_attempt_at=None,
                )
            except Exception:
                updated = None
            if isinstance(updated, dict):
                redriven.append(str(updated.get("dispatch_id") or dispatch_id))
        if not redriven:
            raise ServiceUnavailableError("Failed to redrive dispatches.")

        if hasattr(bl, "record_domain_event"):
            try:
                audit_event_id = f"{event_id}:redrive:{uuid.uuid4().hex[:12]}"
                bl.record_domain_event(
                    event_id=audit_event_id,
                    event_type="event.redrive.requested",
                    account_id=str(event.get("account_id") or "default").strip() or "default",
                    severity="warning",
                    summary=f"Redrive requested for event {event_id}",
                    payload={
                        "source_event_id": event_id,
                        "source_event_status": event_status,
                        "requested_dispatches": list(failed_dispatches),
                        "redriven_dispatches": list(redriven),
                        "requested_by": requested_by,
                    },
                    actor={
                        "type": str(resolved_scope.principal_type or "operator").strip()
                        or "operator",
                        "id": requested_by,
                    },
                    urls={},
                    envelope=self._to_json_obj(event.get("envelope")) or {},
                    occurred_at=self._serialize_domain_event_datetime(
                        event.get("occurred_at") or event.get("created_at")
                    ),
                    project_id=event.get("project_id"),
                    workflow_id=str(event.get("workflow_id") or "").strip() or None,
                    job_id=event.get("job_id"),
                    group_id=str(event.get("group_id") or "").strip() or None,
                    step_id=str(event.get("step_id") or "").strip() or None,
                    step_execution_id=str(event.get("step_execution_id") or "").strip() or None,
                    source=str(event.get("source") or "").strip() or None,
                    aggregate_type=str(event.get("aggregate_type") or "").strip() or None,
                    aggregate_id=str(event.get("aggregate_id") or "").strip() or None,
                    correlation_id=str(event.get("correlation_id") or "").strip() or None,
                    status="recorded",
                )
            except Exception:
                pass

        return EventRedriveResultView(
            status="accepted",
            event_id=event_id,
            redriven_dispatches=redriven,
            requested_dispatches=failed_dispatches,
        )

    def ingest_provider_events(
        self,
        command: ProviderEventIngestInput,
        *,
        scope: ScopeContext | None = None,
    ) -> ProviderEventIngestResultView:
        resolved_scope = _service_scope(scope, project_id=command.project_id)
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(ERR_DATABASE_NOT_CONFIGURED)
        if not hasattr(bl, "record_domain_event"):
            raise ServiceUnavailableError("Domain event storage is unavailable")

        project_id = str(command.project_id or "").strip()
        if not project_id:
            raise ServiceValidationError(ERR_PROJECT_ID_REQUIRED)
        provider_key = str(command.provider_key or "").strip().lower()
        if not provider_key:
            raise ServiceValidationError("provider_key is required")

        project = bl.get_project(project_id) if hasattr(bl, "get_project") else None
        if not project:
            raise ServiceNotFoundError(ERR_PROJECT_NOT_FOUND)
        require_project_access(resolved_scope, dict(project))

        account_id = (
            str((project or {}).get("account_id") or "").strip()
            or str(resolved_scope.account_id or "").strip()
            or "acc_default"
        )
        result_payload = self._to_json_obj(command.result) or {}
        raw_events = list(command.events or [])
        if not raw_events:
            raise ServiceValidationError("events are required")

        event_ids: list[str] = []
        notification_deliveries: list[dict[str, Any]] = []
        default_event_source = str(
            result_payload.get("source") or f"provider:{provider_key}"
        ).strip()

        def _optional_job_id(*values: Any) -> str | None:
            for value in values:
                normalized = str(value or "").strip()
                if normalized:
                    return normalized
            return None

        for index, raw_event in enumerate(raw_events):
            event_payload = self._to_json_obj(raw_event) or {}
            normalized_ingress_event = normalize_ingress_provider_event(
                event_payload,
                provider_key=provider_key,
                default_source=default_event_source,
            )
            if normalized_ingress_event:
                event_payload = normalized_ingress_event
            event_type = str(event_payload.get("event_type") or "").strip()
            if not event_type:
                raise ServiceValidationError("event_type is required for provider events")

            severity = str(event_payload.get("severity") or "info").strip() or "info"
            summary = (
                str(
                    event_payload.get("summary")
                    or (self._to_json_obj(event_payload.get("payload")) or {}).get("summary")
                    or event_type
                ).strip()
                or event_type
            )
            payload = self._to_json_obj(event_payload.get("payload")) or {}
            if event_type in {
                EXTERNAL_COMMENT_RECEIVED_VALUE,
                "notification.interaction_received",
            }:
                channel_ref = self._provider_event_channel_ref(
                    provider_key=provider_key, payload=payload
                )
                authorized = (
                    self._provider_human_decision_channel_authorized(
                        bl,
                        project_id=str(project_id),
                        provider_key=provider_key,
                        payload=payload,
                    )
                    if channel_ref
                    else self._provider_event_binding_authorized(
                        bl,
                        project_id=str(project_id),
                        provider_key=provider_key,
                    )
                )
                if not authorized:
                    detail = (
                        "Provider human decision channel is not authorized"
                        if channel_ref
                        else "Provider callback or connection identity is not authorized"
                    )
                    raise ServiceForbiddenError(detail)
            metadata = self._to_json_obj(event_payload.get("metadata")) or {}
            workflow_id = str(event_payload.get("workflow_id") or "").strip() or None
            source = str(
                event_payload.get("source")
                or result_payload.get("source")
                or f"provider:{provider_key}"
            ).strip()
            correlation_id = (
                str(event_payload.get("correlation_id") or "").strip()
                or str(payload.get("correlation_id") or "").strip()
                or str(result_payload.get("correlation_id") or "").strip()
                or str(payload.get("issue_id") or payload.get("source_id") or "").strip()
                or f"{provider_key}:{project_id}:{index}"
            )
            envelope = build_event_envelope_v1(
                event_type=event_type,
                account_id=account_id,
                correlation_id=correlation_id,
                severity=severity,
                summary=summary,
                payload=payload,
                project_id=project_id,
                workflow_id=workflow_id,
                source=source,
                aggregate_type="project",
                aggregate_id=str(project_id),
                event_id=str(event_payload.get("event_id") or "").strip() or None,
                metadata=metadata,
            )

            event_job_id = _optional_job_id(
                event_payload.get("job_id"),
                payload.get("job_id"),
                result_payload.get("job_id"),
            )
            recorded = (
                bl.record_domain_event(
                    event_id=str(envelope.get("event_id") or ""),
                    event_type=str(envelope.get("event_type") or ""),
                    account_id=str(envelope.get("account_id") or ""),
                    severity=str(envelope.get("severity") or "info"),
                    summary=str(envelope.get("summary") or event_type),
                    payload=self._to_json_obj(envelope.get("payload")) or {},
                    actor=self._to_json_obj(envelope.get("actor")) or {},
                    urls=self._to_json_obj(envelope.get("urls")) or {},
                    envelope=envelope,
                    occurred_at=str(envelope.get("occurred_at") or ""),
                    project_id=project_id,
                    workflow_id=workflow_id,
                    job_id=event_job_id,
                    group_id=None,
                    step_id=None,
                    source=source,
                    aggregate_type="project",
                    aggregate_id=str(project_id),
                    correlation_id=correlation_id,
                    status="delivered",
                )
                or {}
            )
            normalized_event_id = str(
                (recorded or {}).get("event_id") or envelope.get("event_id") or ""
            ).strip()
            if normalized_event_id:
                event_ids.append(normalized_event_id)

            reaction_was_routed = False
            if event_type in {EXTERNAL_COMMENT_RECEIVED_VALUE, "notification.interaction_received"}:
                normalized_comment_event = normalize_ingress_provider_event(
                    event_payload,
                    provider_key=provider_key,
                    default_source=source or default_event_source,
                )
                if normalized_comment_event:
                    event_payload = normalized_comment_event
                    payload = self._to_json_obj(normalized_comment_event.get("payload")) or payload
                nested_payload = self._to_json_obj(payload.get("payload")) or {}
                if not isinstance(nested_payload, dict):
                    nested_payload = {}
                reaction_payload = dict(payload)
                for key, value in nested_payload.items():
                    reaction_payload.setdefault(str(key), value)
                reaction_job_id = _optional_job_id(
                    event_payload.get("job_id"),
                    payload.get("job_id"),
                    nested_payload.get("job_id"),
                    result_payload.get("job_id"),
                )
                if reaction_job_id:
                    reaction_workflow_id = workflow_id
                    job_store = self._get_job_store() if self._get_job_store else bl
                    if not reaction_workflow_id and hasattr(job_store, "get_job"):
                        try:
                            job_row = job_store.get_job(str(reaction_job_id)) or {}
                        except Exception:
                            job_row = {}
                        meta_row = self._to_json_obj(self._to_json_obj(job_row).get("meta")) or {}
                        reaction_workflow_id = (
                            str(
                                self._to_json_obj(meta_row).get("selected_workflow_id")
                                or self._to_json_obj(meta_row).get("requested_workflow_id")
                                or self._to_json_obj(meta_row).get("workflow_id")
                                or ""
                            ).strip()
                            or None
                        )
                    reaction_source_id = str(
                        event_payload.get("source_id")
                        or reaction_payload.get("source_id")
                        or normalized_event_id
                        or correlation_id
                    ).strip()
                    reaction_event_key = str(
                        event_payload.get("event_key")
                        or reaction_payload.get("event_key")
                        or event_type
                    ).strip()
                    reaction_dedupe_key = str(
                        event_payload.get("dedupe_key")
                        or reaction_payload.get("dedupe_key")
                        or build_ingress_dedupe_key(
                            provider_key=provider_key,
                            source_id=reaction_source_id,
                            event_key=reaction_event_key,
                            job_id=str(reaction_job_id),
                            payload=reaction_payload,
                        )
                    ).strip()
                    reaction = self.ingest_reaction_event(
                        ReactionEventIngestInput(
                            event_type=event_type,
                            job_id=str(reaction_job_id),
                            workflow_id=reaction_workflow_id,
                            source=provider_key,
                            source_id=reaction_source_id,
                            event_key=reaction_event_key,
                            dedupe_key=reaction_dedupe_key,
                            payload=reaction_payload,
                        ),
                        scope=resolved_scope,
                        # Bind the attacker-supplied reaction job to the project
                        # this provider ingress is pinned to (the registration
                        # project). Reject cross-project job ids before resuming
                        # (FS-DEEP-CAND-039 / FS-DEEP-CAND-048).
                        expected_project_id=project_id,
                    )
                    notification_deliveries.extend(
                        [
                            dict(item)
                            for item in (reaction.notification_deliveries or [])
                            if isinstance(item, dict)
                        ]
                    )
                    reaction_was_routed = True

            if not reaction_was_routed:
                notification_deliveries.extend(
                    self._dispatch_project_notification_event(
                        bl,
                        project_id=project_id,
                        event_type=event_type,
                        severity=severity,
                        workflow_id=workflow_id,
                        payload=payload,
                        event_id=normalized_event_id or None,
                        account_id=account_id,
                        correlation_id=correlation_id,
                    )
                    or []
                )

        return ProviderEventIngestResultView(
            provider_key=provider_key,
            project_id=project_id,
            event_ids=event_ids,
            event_count=len(raw_events),
            notification_deliveries=notification_deliveries,
        )


__all__ = [name for name in globals() if not name.startswith("__")]
