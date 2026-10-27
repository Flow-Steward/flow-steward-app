# ruff: noqa: F403, F405
# mypy: ignore-errors
from __future__ import annotations

from .query_services_common import *  # NOSONAR


class JobQueryServiceJournalMixin:
    @staticmethod
    def _incident_root_cause(
        *,
        job: dict[str, Any],
        runtime: dict[str, Any],
        dispatches: list[dict[str, Any]],
        attempts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        latest_attempt = attempts[0] if attempts else {}
        latest_dispatch = dispatches[0] if dispatches else {}
        structured_error_raw = runtime.get("last_structured_error")
        structured_error = structured_error_raw if isinstance(structured_error_raw, dict) else {}
        message = (
            str(job.get("error") or "").strip()
            or str(runtime.get("last_stderr_excerpt") or "").strip()
            or str(structured_error.get("error_message") or "").strip()
            or str(latest_attempt.get("error_text") or "").strip()
            or str(latest_dispatch.get("reason_detail") or "").strip()
            or "No explicit root cause recorded."
        )
        low = message.lower()
        category = "unknown"
        if str(latest_dispatch.get("reason_code") or "").strip().lower().startswith("policy"):
            category = "policy_block"
        elif (
            str(latest_attempt.get("status") or "").strip().lower() == "timeout" or "timeout" in low
        ):
            category = "timeout"
        elif "tool" in low or "provider" in low or "connector" in low:
            category = "tool_failure"
        elif str(job.get("status") or "").strip().lower() in {
            "failed",
            "max_iterations",
        }:
            category = "step_error"

        return {
            "category": category,
            "message": message,
            "event_id": str((latest_dispatch or {}).get("event_id") or ""),
            "dispatch_id": str((latest_dispatch or {}).get("dispatch_id") or ""),
            "attempt_no": int((latest_attempt or {}).get("attempt_no") or 0),
            "reason_code": str((latest_dispatch or {}).get("reason_code") or ""),
            "attempt_status": str((latest_attempt or {}).get("status") or ""),
            "source": str(
                (latest_attempt or {}).get("handler")
                or (latest_dispatch or {}).get("target_type")
                or "runtime"
            ),
        }

    def get_incident_drilldown(
        self,
        job_id: str,
        *,
        limit: int = 100,
        scope: ScopeContext | None = None,
    ) -> JobIncidentDrilldownView:
        bl, job, resolved_scope = self._job_context(job_id, scope=scope)
        event_store = self._get_event_store() if self._get_event_store is not None else bl
        safe_limit = max(1, min(int(limit or 100), 200))
        runtime = self._build_runtime_payload(job, bl) or {}
        if not isinstance(runtime, dict):
            runtime = {}

        event_rows: list[dict[str, Any]] = []
        if hasattr(event_store, "list_domain_events"):
            event_rows = list(
                event_store.list_domain_events(
                    account_id=resolved_scope.account_id,
                    job_id=str(job_id),
                    limit=safe_limit,
                )
                or []
            )

        scoped_events: list[dict[str, Any]] = []
        for event in event_rows:
            if not isinstance(event, dict):
                continue
            try:
                require_event_access(resolved_scope, event)
            except ServiceForbiddenError:
                continue
            scoped_events.append(dict(event))

        dispatch_rows: list[dict[str, Any]] = []
        if hasattr(event_store, "list_event_dispatches"):
            for event in scoped_events:
                remaining = safe_limit - len(dispatch_rows)
                if remaining <= 0:
                    break
                event_id = str(event.get("event_id") or "").strip()
                if not event_id:
                    continue
                rows = (
                    event_store.list_event_dispatches(
                        event_id=event_id,
                        account_id=resolved_scope.account_id,
                        limit=remaining,
                    )
                    or []
                )
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    try:
                        require_event_access(
                            resolved_scope,
                            {
                                "account_id": row.get("account_id") or event.get("account_id"),
                                "project_id": row.get("project_id")
                                if row.get("project_id") is not None
                                else event.get("project_id"),
                                "workflow_id": row.get("workflow_id") or event.get("workflow_id"),
                            },
                        )
                    except ServiceForbiddenError:
                        continue
                    dispatch_rows.append(dict(row))
                    if len(dispatch_rows) >= safe_limit:
                        break

        seen_dispatches: set[str] = set()
        unique_dispatch_rows: list[dict[str, Any]] = []
        for row in dispatch_rows:
            dispatch_id = str(row.get("dispatch_id") or "").strip()
            if dispatch_id and dispatch_id in seen_dispatches:
                continue
            if dispatch_id:
                seen_dispatches.add(dispatch_id)
            unique_dispatch_rows.append(row)
        dispatch_rows = unique_dispatch_rows

        attempt_rows: list[dict[str, Any]] = []
        if hasattr(event_store, "list_event_delivery_attempts"):
            for dispatch in dispatch_rows:
                remaining = safe_limit - len(attempt_rows)
                if remaining <= 0:
                    break
                dispatch_id = str(dispatch.get("dispatch_id") or "").strip()
                if not dispatch_id:
                    continue
                rows = (
                    event_store.list_event_delivery_attempts(
                        dispatch_id=dispatch_id,
                        limit=remaining,
                    )
                    or []
                )
                for row in rows:
                    if isinstance(row, dict):
                        attempt_rows.append(dict(row))
                        if len(attempt_rows) >= safe_limit:
                            break

        root_cause = self._incident_root_cause(
            job=job,
            runtime=runtime,
            dispatches=dispatch_rows,
            attempts=attempt_rows,
        )

        failure_context = {
            "job_error": str(job.get("error") or "").strip(),
            "job_status": str(job.get("status") or "").strip(),
            "last_stderr_excerpt": str(runtime.get("last_stderr_excerpt") or "").strip(),
            "last_structured_error": (
                runtime.get("last_structured_error")
                if isinstance(runtime.get("last_structured_error"), dict)
                else {}
            ),
            "retryable_interrupted_step": (
                runtime.get("retryable_interrupted_step")
                if isinstance(runtime.get("retryable_interrupted_step"), dict)
                else {}
            ),
        }

        return JobIncidentDrilldownView(
            job_id=str(job_id),
            project_id=coerce_optional_project_id(job.get("project_id")),
            status=str(job.get("status") or "").strip(),
            root_cause=root_cause,
            failure_context=failure_context,
            related_events=scoped_events[:safe_limit],
            related_dispatches=dispatch_rows[:safe_limit],
            related_attempts=attempt_rows[:safe_limit],
        )

    def get_runtime_journal(
        self,
        job_id: str,
        *,
        limit: int = 500,
        scope: ScopeContext | None = None,
    ) -> JobRuntimeJournalView:
        bl, job, _ = self._job_context(job_id, scope=scope)
        meta = self._json_obj(job.get("meta"))
        if not isinstance(meta, dict):
            meta = {}
        journal = self._runtime_journal_from_job(job, meta)
        journal.extend(self._runtime_journal_from_events(bl, str(job_id)))
        journal.extend(self._runtime_journal_from_agent_messages(bl, str(job_id)))
        safe_limit = max(1, min(int(limit or 500), 500))
        journal = self._sort_runtime_journal(self._dedupe_runtime_journal(journal))[-safe_limit:]
        return JobRuntimeJournalView(job_id=str(job_id), runtime_journal=journal)

    def _runtime_journal_from_job(
        self, job: dict[str, Any], meta: dict[str, Any]
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        raw_journal = meta.get("runtime_journal")
        if isinstance(raw_journal, list):
            rows.extend(
                self._normalize_runtime_journal_entry(item, source="job_meta")
                for item in raw_journal
                if isinstance(item, dict)
            )
        runtime_state = self._json_obj(job.get("runtime_state"))
        if not isinstance(runtime_state, dict):
            runtime_state = self._json_obj(meta.get("runtime_state"))
        if isinstance(runtime_state, dict):
            orchestrator_journal = runtime_state.get("orchestrator_journal")
            if isinstance(orchestrator_journal, list):
                rows.extend(
                    self._normalize_runtime_journal_entry(
                        item,
                        source="orchestrator_journal",
                    )
                    for item in orchestrator_journal
                    if isinstance(item, dict)
                )
        return rows

    def _runtime_journal_from_events(self, bl: Any, job_id: str) -> list[dict[str, Any]]:
        if not callable(getattr(bl, "get_events", None)):
            return []
        interesting_events = {
            "approval",
            "approval_resume_decision",
            "approval_wait",
            "human_approval",
            "phase_complete",
            "phase_decision",
            "phase_entry_prepare",
            "phase_input_blocked",
            "routing_decision",
            "validation_fail",
            "workflow_complete",
        }
        rows = self._runtime_journal_event_rows(bl, str(job_id))
        normalized: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            event = self._serialize_event_row(dict(row))
            event_type = str(event.get("event_type") or event.get("event") or "").strip()
            if event_type not in interesting_events:
                continue
            detail: dict[str, Any] = (
                event.get("detail") if isinstance(event.get("detail"), dict) else {}
            )
            normalized.append(
                self._normalize_runtime_journal_entry(
                    {
                        "id": event.get("id"),
                        "timestamp": event.get("timestamp"),
                        "event": event_type,
                        "reason": detail.get("reason")
                        or detail.get("final_gate_reason")
                        or event.get("title")
                        or "",
                        "message": event.get("title") or event_type,
                        "phase": event.get("phase"),
                        "agent": event.get("agent"),
                        "severity": event.get("severity"),
                        "detail": detail,
                    },
                    source="platform_events",
                )
            )
        return normalized

    def _runtime_journal_event_rows(self, bl: Any, job_id: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        since_id: Any = 0
        page_size = 500
        max_pages = 40
        for _page in range(max_pages):
            try:
                page = bl.get_events(str(job_id), since_id=since_id, limit=page_size) or []
            except TypeError:
                try:
                    page = bl.get_events(str(job_id), since_id, page_size) or []
                except TypeError:
                    page = bl.get_events(str(job_id)) or []
            except Exception as exc:
                logger.debug("get_runtime_journal event merge failed: %s", exc)
                break
            page_rows = [dict(row) for row in page if isinstance(row, dict)]
            if not page_rows:
                break
            rows.extend(page_rows)
            last_id = str(page_rows[-1].get("id") or "").strip()
            if not last_id or len(page_rows) < page_size:
                break
            since_id = last_id
        return rows

    def _runtime_journal_from_agent_messages(self, bl: Any, job_id: str) -> list[dict[str, Any]]:
        if not callable(getattr(bl, "get_agent_messages", None)):
            return []
        try:
            rows = bl.get_agent_messages(str(job_id), newest=True, limit=1000) or []
        except TypeError:
            try:
                rows = bl.get_agent_messages(str(job_id), limit=1000) or []
            except TypeError:
                rows = bl.get_agent_messages(str(job_id)) or []
        except Exception as exc:
            logger.debug("get_runtime_journal message merge failed: %s", exc)
            return []
        normalized: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            message = self._serialize_agent_message_row(dict(row))
            status = str(message.get("status") or "").strip().lower()
            agent = str(message.get("from_agent") or message.get("agent") or "").strip()
            if status not in {"blocked", "fail", "failed", "pass", "passed"}:
                continue
            artifacts: dict[str, Any] = (
                message.get("artifacts") if isinstance(message.get("artifacts"), dict) else {}
            )
            timestamps: dict[str, Any] = (
                message.get("timestamps") if isinstance(message.get("timestamps"), dict) else {}
            )
            message_id = str(message.get("id") or "").strip()
            normalized.append(
                self._normalize_runtime_journal_entry(
                    {
                        "id": message_id,
                        "timestamp": message.get("timestamp") or timestamps.get("published_at"),
                        "event": f"agent_message_{status or 'reported'}",
                        "reason": message.get("summary") or "",
                        "message": message.get("summary") or "",
                        "step_id": message.get("step_id"),
                        "agent": agent,
                        "detail": {
                            "message_id": message_id,
                            "status": status,
                            "skill": message.get("skill"),
                            "correlation_id": message.get("correlation_id"),
                            "artifact_summary": self._runtime_journal_artifact_summary(artifacts),
                            "errors": self._bounded_text_list(message.get("errors"), limit=5),
                            "message_detail_ref": {
                                "kind": "agent_message",
                                "id": message_id,
                            }
                            if message_id
                            else {},
                        },
                    },
                    source="agent_messages",
                )
            )
        return normalized

    @staticmethod
    def _runtime_journal_artifact_summary(artifacts: dict[str, Any]) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        for key in (
            "next_action",
            "blocked_reason",
            "failure_class",
            "failure_summary",
        ):
            value = artifacts.get(key)
            if value is None or value == "":
                continue
            summary[key] = JobQueryServiceJournalMixin._bounded_text(value)
        checks = artifacts.get("checks_run")
        if isinstance(checks, list):
            summary["checks_run"] = [
                JobQueryServiceJournalMixin._runtime_journal_check_summary(item)
                for item in checks[:10]
                if isinstance(item, dict)
            ]
            if len(checks) > 10:
                summary["checks_truncated"] = len(checks) - 10
        elif isinstance(checks, dict):
            items = list(checks.items())
            summary["checks_run"] = [
                JobQueryServiceJournalMixin._runtime_journal_check_summary(
                    {"check": key, **value} if isinstance(value, dict) else {"check": key}
                )
                for key, value in items[:10]
            ]
            if len(items) > 10:
                summary["checks_truncated"] = len(items) - 10
        return summary

    @staticmethod
    def _runtime_journal_check_summary(check: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "check": check.get("check") or check.get("name"),
            "label": check.get("label"),
            "status": check.get("status"),
            "passed": check.get("passed"),
            "exit_code": check.get("exit_code"),
            "blocking": check.get("blocking"),
            "required": check.get("required"),
            "blocking_reason": JobQueryServiceJournalMixin._bounded_text(
                check.get("blocking_reason")
            ),
            "warning_reason": JobQueryServiceJournalMixin._bounded_text(
                check.get("warning_reason")
            ),
        }
        return {key: value for key, value in payload.items() if value is not None and value != ""}

    @staticmethod
    def _bounded_text(value: Any, *, limit: int = 500) -> str:
        text = str(value or "").strip()
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 15)].rstrip() + "… [truncated]"

    @staticmethod
    def _bounded_text_list(value: Any, *, limit: int = 5) -> list[str]:
        if not isinstance(value, list):
            return []
        return [JobQueryServiceJournalMixin._bounded_text(item) for item in value[: max(0, limit)]]

    @staticmethod
    def _normalize_runtime_journal_entry(entry: dict[str, Any], *, source: str) -> dict[str, Any]:
        detail: dict[str, Any] = (
            entry.get("detail") if isinstance(entry.get("detail"), dict) else {}
        )
        normalized = {
            "id": str(entry.get("id") or "").strip(),
            "timestamp": entry.get("timestamp"),
            "event": str(entry.get("event") or entry.get("action") or source).strip() or source,
            "reason": str(entry.get("reason") or detail.get("reason") or "").strip(),
            "message": str(entry.get("message") or entry.get("title") or "").strip(),
            # `phase` is the platform event's own stage label, not workflow scope.
            "phase": str(entry.get("phase") or "").strip(),
            "step_id": str(entry.get("step_id") or "").strip(),
            "agent": str(entry.get("agent") or entry.get("from_agent") or "").strip(),
            "severity": str(entry.get("severity") or "").strip(),
            "detail": detail,
            "source": source,
        }
        return {
            key: value for key, value in normalized.items() if value is not None and value != ""
        }

    @staticmethod
    def _dedupe_runtime_journal(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = (
                str(row.get("source") or ""),
                str(row.get("id") or ""),
                str(row.get("event") or ""),
                str(row.get("timestamp") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(row)
        return deduped

    @staticmethod
    def _sort_runtime_journal(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        def _sort_key(row: dict[str, Any]) -> tuple[str, str, str]:
            return (
                str(row.get("timestamp") or ""),
                str(row.get("source") or ""),
                str(row.get("id") or ""),
            )

        return sorted(rows, key=_sort_key)


__all__ = [
    "JobQueryServiceJournalMixin",
]
