# ruff: noqa: F403, F405
# mypy: ignore-errors
from __future__ import annotations

import json

from core.application.observability_vocabulary import job_status_meta, normalize_job_status
from core.application.registry.workflow_secret_defaults import strip_workflow_secret_authority
from core.application.workflows.workflow_contract_summary import archived_contract_summary
from core.application.workflows.workflow_portability_common import _redact_sensitive

from .query_services_common import *  # NOSONAR

_MESSAGE_NOT_FOUND = "Message not found"


class JobQueryServiceDetailsMixin:
    def get_child_invocations(
        self, job_id: str, *, scope: ScopeContext | None = None
    ) -> dict[str, Any]:
        """Allowlisted immediate-child links, authorized independently of their parent."""
        from core.application.jobs.child_invocation_projection import project_child_invocations

        _bl, parent, resolved = self._job_context(job_id, scope=scope)
        return project_child_invocations(
            parent,
            scope=resolved,
            read_child=lambda child_id: self._job_context(child_id, scope=resolved)[1],
        )

    def list_decision_overrides(
        self,
        job_id: str,
        *,
        active_only: bool = False,
        domain: str = "",
        limit: int = 200,
        scope: ScopeContext | None = None,
    ) -> JobDecisionOverridesView:
        bl, _, _ = self._job_context(job_id, scope=scope)
        list_rows = getattr(bl, "list_job_decision_overrides", None)
        if not callable(list_rows):
            return JobDecisionOverridesView(job_id=str(job_id), overrides=[])
        try:
            rows = list_rows(
                str(job_id),
                active_only=bool(active_only),
                domain=str(domain or "").strip() or None,
                limit=max(1, min(int(limit or 200), 1000)),
            )
        except ValueError as exc:
            raise ServiceValidationError(str(exc)) from exc
        return JobDecisionOverridesView(
            job_id=str(job_id),
            overrides=[
                self._serialize_decision_override_row(dict(row))
                for row in (rows or [])
                if isinstance(row, dict)
            ],
        )

    def list_errors(
        self,
        job_id: str,
        *,
        limit: int = 200,
        scope: ScopeContext | None = None,
    ) -> JobErrorsView:
        bl, _, _ = self._job_context(job_id, scope=scope)
        list_rows = getattr(bl, "list_job_errors", None)
        if not callable(list_rows):
            return JobErrorsView(job_id=str(job_id), errors=[])
        rows = list_rows(str(job_id), limit=max(1, min(int(limit or 200), 1000)))
        return JobErrorsView(
            job_id=str(job_id),
            errors=[
                self._serialize_job_error_row(dict(row))
                for row in (rows or [])
                if isinstance(row, dict)
            ],
        )

    def get_job_definition(
        self,
        job_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> JobDefinitionView:
        """The definition the job pinned from the snapshot archive.

        Shown for active and historical runs alike, even after the workflow was
        edited or deleted and its checkpoint is gone. A job without a pinned
        snapshot says so; it never gets the current definition instead.
        """
        from core.application.jobs.workflow_definition_snapshot_repository import (
            WorkflowDefinitionSnapshotRepository,
        )

        bl, _, _ = self._job_context(job_id, scope=scope)
        conn = getattr(bl, "conn", None)
        row = (
            WorkflowDefinitionSnapshotRepository(conn).load_for_job(str(job_id))
            if conn is not None
            else None
        )
        if not row:
            return JobDefinitionView(
                job_id=str(job_id),
                available=False,
                unavailable_reason="definition_snapshot_unavailable",
            )
        definition = row.get("definition_jsonb")
        if isinstance(definition, str):
            definition = json.loads(definition)
        definition = definition if isinstance(definition, dict) else {}
        protected = definition.get("protected")
        if not isinstance(protected, bool):
            return JobDefinitionView(
                job_id=str(job_id),
                available=False,
                unavailable_reason="definition_visibility_unknown",
            )
        definition = strip_workflow_secret_authority(definition)
        if protected:
            # A protected author graph is read-only, never hidden: its real
            # structure with sensitive configuration redacted.
            definition = {
                **definition,
                **{
                    key: [_redact_sensitive(item) for item in definition.get(key) or []]
                    for key in ("steps", "groups", "for_each")
                },
                "settings": _redact_sensitive(definition.get("settings") or {}),
            }
        graph = definition.get("execution_graph")
        if not isinstance(graph, dict):
            settings = definition.get("settings")
            graph = settings.get("execution_graph") if isinstance(settings, dict) else None
        return JobDefinitionView(
            job_id=str(job_id),
            available=True,
            snapshot_id=str(row.get("id") or "") or None,
            workflow_id=str(row.get("workflow_id") or "") or None,
            revision=row.get("revision"),
            digest=str(row.get("digest") or "") or None,
            schema_version=row.get("schema_version"),
            contract_summary=archived_contract_summary(definition),
            steps=[dict(item) for item in definition.get("steps") or [] if isinstance(item, dict)],
            groups=[
                dict(item) for item in definition.get("groups") or [] if isinstance(item, dict)
            ],
            for_each=[
                dict(item) for item in definition.get("for_each") or [] if isinstance(item, dict)
            ],
            execution_graph=dict(graph) if isinstance(graph, dict) else {},
            protected=protected,
        )

    def get_causal_chain(
        self,
        job_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> JobCausalChainView:
        bl, _, _ = self._job_context(job_id, scope=scope)
        return JobCausalChainView.model_validate(self._build_job_causal_chain(bl, str(job_id)))

    def get_cost_breakdown(
        self,
        job_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> JobCostBreakdownView:
        bl, _, _ = self._job_context(job_id, scope=scope)
        rows = bl.get_job_cost_breakdown(str(job_id)) or []
        breakdown: list[dict[str, Any]] = []
        known_cost = 0.0
        unknown_cli_tokens = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_unknown_tokens = int(row.get("unknown_cli_tokens") or 0)
            row_known_cost = float(row.get("known_cost_usd") or row.get("cost_usd") or 0)
            row_cost_knownness = (
                "unknown" if row_unknown_tokens > 0 and row.get("cost_usd") is None else "known"
            )
            payload = {
                "agent": row.get("agent"),
                "model_used": row.get("model_used"),
                "calls": int(row.get("calls") or 0),
                "tokens_prompt": int(row.get("tokens_prompt") or 0),
                "tokens_completion": int(row.get("tokens_completion") or 0),
                "tokens_total": int(row.get("tokens_total") or 0),
                "cost_usd": None if row_cost_knownness == "unknown" else row_known_cost,
                "known_cost_usd": row_known_cost,
                "unknown_cli_tokens": row_unknown_tokens,
                "unknown_cost_calls": int(row.get("unknown_cost_calls") or 0),
                "cost_knownness": row_cost_knownness,
            }
            known_cost += row_known_cost
            unknown_cli_tokens += row_unknown_tokens
            breakdown.append(payload)
        if unknown_cli_tokens > 0 and known_cost > 0:
            total_knownness = "partial"
        elif unknown_cli_tokens > 0:
            total_knownness = "unknown"
        else:
            total_knownness = "known"
        return JobCostBreakdownView(
            breakdown=breakdown,
            total_cost_usd=known_cost,
            known_cost_usd=known_cost,
            unknown_cli_tokens=unknown_cli_tokens,
            cost_knownness=total_knownness,
        )

    def list_events(
        self,
        job_id: str,
        *,
        since_id: Any = 0,
        limit: int = 200,
        scope: ScopeContext | None = None,
    ) -> JobEventsView:
        bl, _, _ = self._job_context(job_id, scope=scope)
        since_filter = str(since_id or "").strip()
        if since_filter == "0":
            since_filter = ""
        safe_limit = max(1, min(int(limit or 200), 2000))
        rows = bl.get_events(str(job_id), since_id=since_filter, limit=safe_limit) or []
        events = [self._serialize_event_row(dict(row)) for row in rows if isinstance(row, dict)]
        for event in events:
            if isinstance(event, dict):
                event["id"] = str(event.get("id") or "").strip()
        last_id = str(events[-1].get("id") or "") if events else since_filter
        return JobEventsView(events=events, last_id=last_id if last_id != "0" else "")

    def list_messages(
        self,
        job_id: str,
        *,
        since_id: Any = 0,
        before_id: Any = 0,
        newest: bool = False,
        step_id: str = "",
        producer: str = "",
        skill: str = "",
        status: str = "",
        limit: int = 25,
        scope: ScopeContext | None = None,
    ) -> JobMessagesView:
        bl, _, _ = self._job_context(job_id, scope=scope)
        step_filter = str(step_id or "").strip() or None
        producer_filter = str(producer or "").strip() or None
        skill_filter = str(skill or "").strip() or None
        status_filter = str(status or "").strip() or None
        try:
            safe_limit = max(1, min(int(limit or 25), 2000))
        except (TypeError, ValueError):
            safe_limit = 25
        since_filter = str(since_id or "").strip()
        if since_filter == "0":
            since_filter = ""
        before_filter = str(before_id or "").strip() or None
        if before_filter == "0":
            before_filter = None
        rows = (
            bl.get_agent_messages(
                str(job_id),
                since_id=since_filter,
                before_id=before_filter,
                newest=bool(newest),
                limit=safe_limit + 1,
                step_id=step_filter,
                from_agent=producer_filter,
                skill=skill_filter,
                status=status_filter,
            )
            or []
        )
        has_more = len(rows) > safe_limit
        if has_more:
            rows = rows[-safe_limit:] if newest or before_filter else rows[:safe_limit]
        messages = [
            self._serialize_agent_message_row(dict(row)) for row in rows if isinstance(row, dict)
        ]
        for message in messages:
            if isinstance(message, dict):
                message["id"] = str(message.get("id") or "").strip()
        last_id = str(messages[-1].get("id") or "") if messages else since_filter
        return JobMessagesView(
            messages=messages,
            last_id=last_id if last_id != "0" else "",
            has_more=has_more,
        )

    def get_message_detail(
        self,
        message_id: Any,
        *,
        scope: ScopeContext | None = None,
    ) -> JobMessageDetailView:
        bl = self._logger_or_raise()
        row = bl.get_agent_message_by_id(str(message_id))
        if not row:
            raise ServiceNotFoundError(_MESSAGE_NOT_FOUND)
        job_id = str((row or {}).get("job_id") or "").strip()
        if job_id:
            _, _, _ = self._job_context(job_id, scope=scope)
        else:
            require_account_access(self._scope(scope), dict(row))
        return JobMessageDetailView(message=self._serialize_agent_message_row(dict(row)))

    def get_step_output_value(
        self,
        job_id: str,
        message_id: Any,
        field: str,
        *,
        scope: ScopeContext | None = None,
    ) -> dict[str, Any]:
        """The full value of one output a job step produced (not the preview)."""
        from core.application.reports.step_full_value import full_step_output_value

        bl, _, _ = self._job_context(job_id, scope=scope)
        row = bl.get_agent_message_by_id(str(message_id))
        if not row or str((row or {}).get("job_id") or "").strip() != str(job_id).strip():
            raise ServiceNotFoundError(_MESSAGE_NOT_FOUND)
        message = self._serialize_agent_message_row(dict(row))
        result = full_step_output_value(message, field)
        if not result["found"]:
            raise ServiceNotFoundError("Value not found")
        return {"field": str(field), "value": result["value"]}

    def get_replay_step(
        self,
        job_id: str,
        *,
        message_id: Any = 0,
        correlation_id: str = "",
        scope: ScopeContext | None = None,
    ) -> JobReplayStepView:
        bl, _, _ = self._job_context(job_id, scope=scope)
        if not callable(self._resolve_replay_message_row):
            raise ServiceUnavailableError("Replay trace resolution is not configured")
        row, error, status_code = self._resolve_replay_message_row(
            bl,
            job_id=str(job_id),
            message_id=str(message_id or "").strip(),
            correlation_id=str(correlation_id or ""),
        )
        if error:
            if int(status_code or 500) == 404:
                raise ServiceNotFoundError(error)
            if int(status_code or 500) == 400:
                raise ServiceValidationError(error)
            raise ServiceError(error, status_code=int(status_code or 500))
        if not row:
            raise ServiceNotFoundError(_MESSAGE_NOT_FOUND)
        return JobReplayStepView.model_validate(
            self._collect_replay_step_trace(bl, str(job_id), dict(row))
        )

    def list_step_replays(
        self,
        job_id: str,
        *,
        limit: int = 25,
        message_id: Any = 0,
        correlation_id: str = "",
        scope: ScopeContext | None = None,
    ) -> JobStepReplaysView:
        bl, _, _ = self._job_context(job_id, scope=scope)
        list_rows = getattr(bl, "list_job_step_replays", None)
        if not callable(list_rows):
            raise ServiceError("Step replay storage is not available", status_code=501)
        rows = (
            list_rows(
                str(job_id),
                message_id=str(message_id or "").strip() or None,
                correlation_id=str(correlation_id or "").strip() or None,
                limit=max(1, min(int(limit or 50), 500)),
            )
            or []
        )
        return JobStepReplaysView(
            job_id=str(job_id),
            step_replays=[
                self._serialize_job_step_replay_row(dict(row))
                for row in (rows or [])
                if isinstance(row, dict)
            ],
        )

    def list_logs(
        self,
        job_id: str,
        *,
        since_id: Any = 0,
        before_id: Any = 0,
        newest: bool = False,
        limit: int = 25,
        scope: ScopeContext | None = None,
    ) -> JobLogsView:
        bl, job, _ = self._job_context(job_id, scope=scope)
        try:
            safe_limit = max(1, min(int(limit or 25), 2000))
        except (TypeError, ValueError):
            safe_limit = 25
        since_filter = str(since_id or "").strip()
        if since_filter == "0":
            since_filter = ""
        before_filter = str(before_id or "").strip() or None
        if before_filter == "0":
            before_filter = None
        terminal_default_page = (
            bool(job_status_meta(normalize_job_status(job.get("status")))["terminal"])
            and not since_filter
            and before_filter is None
        )
        effective_newest = bool(newest) or terminal_default_page
        backward_page = effective_newest or before_filter is not None
        rows = (
            bl.get_logs(
                str(job_id),
                since_id=since_filter,
                before_id=before_filter,
                newest=effective_newest,
                limit=safe_limit + 1,
            )
            or []
        )
        has_more = len(rows) > safe_limit
        if has_more:
            rows = rows[-safe_limit:] if backward_page else rows[:safe_limit]
        logs = [
            self._serialize_log_row(dict(row), str(job_id)) for row in rows if isinstance(row, dict)
        ]
        for log in logs:
            if isinstance(log, dict):
                log["id"] = str(log.get("id") or "").strip()
        last_id = str(logs[-1].get("id") or "") if logs else since_filter
        return JobLogsView(
            logs=logs,
            status=str(job.get("status") or ""),
            last_id=last_id if last_id != "0" else "",
            next_before_id=str(logs[0].get("id") or "")
            if logs and backward_page and has_more
            else "",
            has_more=has_more,
        )

    def get_logs_page(
        self,
        job_id: str,
        *,
        initial_limit: int = 25,
        scope: ScopeContext | None = None,
    ) -> JobLogsPageView:
        bl, job, _ = self._job_context(job_id, scope=scope)
        safe_limit = max(1, min(int(initial_limit or 25), 200))

        log_rows = bl.get_logs(str(job_id), limit=safe_limit + 1, newest=True) or []
        log_has_more = len(log_rows) > safe_limit
        if log_has_more:
            log_rows = log_rows[1:]

        event_rows = bl.get_events(str(job_id), limit=25) or []

        message_rows = bl.get_agent_messages(str(job_id), limit=safe_limit + 1, newest=True) or []
        message_has_more = len(message_rows) > safe_limit
        if message_has_more:
            message_rows = message_rows[1:]

        runtime = self._build_runtime_payload(job, bl) or {}
        if not isinstance(runtime, dict):
            runtime = {}

        failure_statuses = {
            "failed",
            "max_iterations",
            "retryable_interrupted_step",
        }
        job_failure_reason = ""
        if str(job.get("status") or "").strip().lower() in failure_statuses:
            structured_error = runtime.get("last_structured_error")
            if not isinstance(structured_error, dict):
                structured_error = {}
            job_failure_reason = (
                str(job.get("error") or "").strip()
                or str(runtime.get("last_stderr_excerpt") or "").strip()
                or str(structured_error.get("error_message") or "").strip()
                or "No failure reason recorded."
            )

        decision_overrides = self.list_decision_overrides(
            str(job_id),
            active_only=False,
            limit=200,
            scope=scope,
        ).overrides
        job_errors = self.list_errors(
            str(job_id),
            limit=200,
            scope=scope,
        ).errors

        return JobLogsPageView(
            job=self._project_job_row_payload(job),
            logs=[self._project_job_row_payload(row) for row in log_rows],
            events=[self._project_job_row_payload(row) for row in event_rows],
            messages=[self._project_job_row_payload(row) for row in message_rows],
            log_has_more=log_has_more,
            message_has_more=message_has_more,
            runtime=runtime,
            decision_overrides=decision_overrides,
            job_errors=job_errors,
            job_failure_reason=job_failure_reason,
            project_name=str(self._resolve_job_project_name(bl, job) or "").strip(),
        )

    def list_reactions(
        self,
        job_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> JobReactionsView:
        self._job_context(job_id, scope=scope)
        event_store = self._get_event_store() if self._get_event_store else self._get_logger()
        list_rows = getattr(event_store, "list_job_reaction_events", None)
        if not callable(list_rows):
            raise ServiceUnavailableError("Reaction projection is not available")
        rows = list_rows(str(job_id)) or []
        reactions = [self._project_job_row_payload(row) for row in rows]
        return JobReactionsView(job_id=str(job_id), reactions=reactions)


__all__ = [
    "JobQueryServiceDetailsMixin",
]
