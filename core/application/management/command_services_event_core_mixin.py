# ruff: noqa: F403, F405
# mypy: ignore-errors
from __future__ import annotations

from .command_services_common import *  # NOSONAR


class EventCommandServiceCoreMixin:
    def __init__(
        self,
        *,
        get_logger: Callable[[], Any],
        get_job_store: Callable[[], Any] | None = None,
        get_project_run_config: Callable[[Any], dict[str, Any] | None] | None = None,
        resolve_domain_event_by_ref: Callable[..., dict[str, Any] | None],
        to_json_obj: Callable[[Any], Any],
        serialize_domain_event_datetime: Callable[[Any], str | None],
        sanitize_external_input: Callable[[str], str] | None = None,
        enqueue_task: Callable[..., Any] | None = None,
        resolve_workflow_selection: Callable[..., tuple[str, str]] | None = None,
        workflow_availability_assert: Callable[..., Any] | None = None,
        workflow_exists: Callable[[str], bool] | None = None,
        evaluate_workflow_reactions: Callable[..., list[dict[str, Any]]] | None = None,
        reaction_action_result_executed: Callable[[dict[str, Any]], bool] | None = None,
        dispatch_project_notification_event: Callable[..., list[dict[str, Any]]] | None = None,
        fetch_provider_attachments: Callable[
            ..., list[dict[str, Any]] | list[ProviderWebhookFileInput]
        ]
        | None = None,
        attachment_max_size_bytes: int = 20 * 1024 * 1024,
        attachment_max_files: int = 10,
        allowed_attachment_mimes: set[str] | None = None,
        allowed_attachment_extensions: set[str] | None = None,
        artifact_plane: Any | None = None,
    ):
        self._get_logger = get_logger
        self._get_job_store = get_job_store
        self._get_project_run_config = get_project_run_config
        self._resolve_domain_event_by_ref = resolve_domain_event_by_ref
        self._to_json_obj = to_json_obj
        self._serialize_domain_event_datetime = serialize_domain_event_datetime
        self._sanitize_external_input = sanitize_external_input or (
            lambda value: str(value or "").strip()
        )
        self._enqueue_task = enqueue_task
        self._resolve_workflow_selection = resolve_workflow_selection
        self._workflow_availability_assert = workflow_availability_assert
        self._workflow_exists = workflow_exists or (lambda workflow_id: bool(workflow_id))
        self._evaluate_workflow_reactions = evaluate_workflow_reactions or (lambda bl, event: [])
        self._reaction_action_result_executed = reaction_action_result_executed or (
            lambda result: bool((result or {}).get("executed"))
        )
        self._dispatch_project_notification_event = dispatch_project_notification_event or (
            lambda *args, **kwargs: []
        )
        self._fetch_provider_attachments = fetch_provider_attachments
        self._attachment_max_size_bytes = int(attachment_max_size_bytes or 0)
        self._attachment_max_files = int(attachment_max_files or 0)
        self._artifact_plane = artifact_plane
        self._allowed_attachment_mimes = {
            str(item).strip().lower()
            for item in (allowed_attachment_mimes or set())
            if str(item).strip()
        }
        self._allowed_attachment_extensions = {
            str(item).strip().lower()
            for item in (allowed_attachment_extensions or set())
            if str(item).strip()
        }

    def _resolve_selected_workflow(
        self,
        *,
        project_config: dict[str, Any],
        requested_workflow_id: str,
    ) -> tuple[str, str]:
        requested = str(requested_workflow_id or "").strip()
        if not requested:
            raise ServiceValidationError("workflow_id is required")
        project_workflows = (
            project_config.get("project_workflows") if isinstance(project_config, dict) else None
        )
        if self._resolve_workflow_selection is None:
            return requested, "explicit_request"
        try:
            return self._resolve_workflow_selection(requested, project_workflows)
        except ValueError as exc:
            raise ServiceValidationError(str(exc)) from exc

    def _assert_can_start_workflow(
        self,
        *,
        workflow_id: str,
        account_id: str,
        project_id: Any = None,
    ) -> None:
        normalized_workflow_id = str(workflow_id or "").strip()
        if not normalized_workflow_id:
            return
        if self._workflow_availability_assert is None:
            return
        self._workflow_availability_assert(
            workflow_id=normalized_workflow_id,
            account_id=str(account_id or "").strip() or "acc_default",
            project_id=str(project_id) if project_id is not None else None,
        )

    def _find_active_provider_job(
        self,
        bl: Any,
        *,
        provider_key: str,
        task_id: str,
        project_id: str,
    ) -> dict[str, Any] | None:
        normalized_task_id = str(task_id or "").strip()
        normalized_provider_key = str(provider_key or "").strip().lower()
        normalized_project_id = str(project_id or "").strip()
        if (
            not normalized_task_id
            or not normalized_provider_key
            or not normalized_project_id
            or not getattr(bl, "conn", None)
        ):
            return None

        cursor_kwargs: dict[str, Any] = {}
        try:
            from core.infrastructure.db.psycopg_runtime import extras as _pge2

            cursor_kwargs["cursor_factory"] = _pge2.RealDictCursor
        except Exception:
            pass

        with bl.conn.cursor(**cursor_kwargs) as cur:
            cur.execute(
                """
                SELECT id, project_id, thread_id, status, meta
                FROM jobs
                WHERE task_id = %s
                  AND project_id = %s
                  AND status IN ('awaiting_approval', 'in_progress')
                  AND (
                    COALESCE(meta->>'external_provider_key', '') = %s
                    OR
                    task_source = %s
                    OR task_source LIKE %s
                  )
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (
                    normalized_task_id,
                    normalized_project_id,
                    normalized_provider_key,
                    normalized_provider_key,
                    f"{normalized_provider_key}_%",
                ),
            )
            row = cur.fetchone()
            if not row:
                return None
            if isinstance(row, dict):
                result = dict(row)
                if str(result.get("project_id") or "").strip() != normalized_project_id:
                    return None
                return result
            if isinstance(row, (tuple, list)):
                cols = (
                    [str(d[0]) for d in (cur.description or [])]
                    if getattr(cur, "description", None)
                    else []
                )
                if cols and len(cols) == len(row):
                    result = {cols[idx]: row[idx] for idx in range(len(cols))}
                    if str(result.get("project_id") or "").strip() != normalized_project_id:
                        return None
                    return result
            try:
                result = dict(row)
                if str(result.get("project_id") or "").strip() != normalized_project_id:
                    return None
                return result
            except Exception:
                return None

    @staticmethod
    def _provider_comment_dedupe_key(
        *,
        provider_key: str,
        task_id: str,
        comment_id: str,
        comment_text: str,
    ) -> str:
        normalized_provider_key = str(provider_key or "").strip().lower() or "external"
        normalized_task_id = str(task_id or "").strip()
        normalized_comment_id = str(comment_id or "").strip()
        return build_ingress_dedupe_key(
            provider_key=normalized_provider_key,
            source_id=normalized_comment_id,
            event_key=EXTERNAL_COMMENT_RECEIVED_VALUE,
            payload={
                "task_id": normalized_task_id,
                "comment_id": normalized_comment_id,
                "text": str(comment_text or ""),
            },
        )

    @staticmethod
    def _is_provider_comment_event(
        *,
        metadata: Mapping[str, Any] | None,
        trigger_config: Mapping[str, Any] | None,
    ) -> bool:
        metadata_payload = dict(metadata or {})
        trigger_payload = dict(trigger_config or {})
        if str(metadata_payload.get("comment_id") or "").strip():
            return True
        if "comment_text" in metadata_payload:
            return True
        event_name = str(
            metadata_payload.get("event")
            or trigger_payload.get("event")
            or metadata_payload.get("event_type")
            or trigger_payload.get("event_type")
            or ""
        ).strip()
        if not event_name:
            return False
        normalized_tokens = {
            token for token in re.split(r"[^a-z0-9]+", event_name.lower()) if token
        }
        return "comment" in normalized_tokens

    def _resolved_provider_attachments(
        self,
        *,
        bl: Any,
        command: ProviderWebhookIngestInput,
        metadata: dict[str, Any],
        trigger_config: dict[str, Any],
        scope: ScopeContext,
    ) -> list[ProviderWebhookFileInput]:
        declared = list(command.files or [])
        if not declared:
            return []

        resolved: list[ProviderWebhookFileInput] = []
        unresolved: list[ProviderWebhookFileInput] = []
        for attachment in declared:
            if bytes(attachment.content or b""):
                resolved.append(attachment)
            else:
                unresolved.append(attachment)

        if unresolved and self._fetch_provider_attachments is not None:
            fetched_rows = (
                self._fetch_provider_attachments(
                    provider_key=str(command.provider_key or "").strip().lower(),
                    project_id=str(command.project_id or "").strip(),
                    project_config=dict(command.project_config or {}),
                    task_id=str(command.task_id or command.source_id or "").strip(),
                    metadata=metadata,
                    trigger_config=trigger_config,
                    files=[item.model_dump() for item in unresolved],
                    bl=bl,
                    scope=scope,
                )
                or []
            )
            for row in fetched_rows:
                if isinstance(row, ProviderWebhookFileInput):
                    resolved.append(row)
                elif isinstance(row, dict):
                    resolved.append(ProviderWebhookFileInput(**row))

        return resolved

    def _validated_provider_attachment_rows(
        self,
        attachments: list[ProviderWebhookFileInput],
    ) -> list[dict[str, Any]]:
        if self._attachment_max_files > 0 and len(attachments) > self._attachment_max_files:
            raise ServiceValidationError(
                f"Too many files — maximum {self._attachment_max_files} files per task"
            )

        rows: list[dict[str, Any]] = []
        for attachment in attachments:
            filename = str(attachment.filename or "").strip()
            if not filename:
                continue
            if not _attachment_filename_is_safe(filename):
                raise ServiceValidationError(f"Invalid filename: {filename}")
            mime_type = (
                str(attachment.mime_type or MIME_APPLICATION_OCTET_STREAM).strip()
                or MIME_APPLICATION_OCTET_STREAM
            )
            if not _attachment_type_allowed(
                filename=filename,
                mime_type=mime_type,
                allowed_extensions=self._allowed_attachment_extensions,
                allowed_mimes=self._allowed_attachment_mimes,
            ):
                raise ServiceValidationError(f"Unsupported file type: {filename} ({mime_type})")

            content = bytes(attachment.content or b"")
            if not content:
                continue
            if (
                self._attachment_max_size_bytes > 0
                and len(content) > self._attachment_max_size_bytes
            ):
                raise ServiceValidationError(
                    f"File too large: {filename} ({len(content) // 1024} KB, max 20 MB)"
                )
            rows.append(
                {
                    "filename": filename,
                    "mime_type": mime_type,
                    "size_bytes": len(content),
                    "content": content,
                }
            )
        return rows

    @staticmethod
    def _merge_attachment_metadata(
        bl: Any, job_id: Any, attachment_metadata: list[dict[str, Any]]
    ) -> None:
        if not attachment_metadata or not hasattr(bl, "get_job") or not hasattr(bl, "update_job"):
            return
        try:
            job_row = bl.get_job(str(job_id)) or {}
            existing_meta = (job_row or {}).get("meta") or {}
            if isinstance(existing_meta, str):
                existing_meta = json.loads(existing_meta)
            if not isinstance(existing_meta, dict):
                existing_meta = {}
            existing_meta["files"] = attachment_metadata
            bl.update_job(str(job_id), meta=existing_meta)
        except Exception as exc:
            logger.warning("Could not update job meta with files metadata: %s", exc)


__all__ = [name for name in globals() if not name.startswith("__")]
