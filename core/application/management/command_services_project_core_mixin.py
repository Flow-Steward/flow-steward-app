# ruff: noqa: F403, F405
# mypy: ignore-errors
from __future__ import annotations

from core.application.extensions.projected_dataset_bridge import projection_bridge_id

from .command_services_common import *  # NOSONAR


class ProjectCommandServiceCoreMixin:
    def __init__(
        self,
        *,
        get_logger: Callable[[], Any],
        resolve_workflow_selection: Callable[..., tuple[str, str]] | None = None,
        validate_explicit_workflow_override: Callable[..., Any] | None = None,
        sanitize_external_input: Callable[[str], str] | None = None,
        enqueue_task: Callable[..., int] | None = None,
        enqueue_task_in_queue: Callable[..., Any] | None = None,
        parse_test_mode: Callable[[Any, bool], bool] | None = None,
        normalize_cron_expr: Callable[[str], str] | None = None,
        cron_next_run_at: Callable[..., datetime] | None = None,
        form_flag: Callable[[Any], bool] | None = None,
        run_scheduled_job_once: Callable[..., dict[str, Any]] | None = None,
        parse_agent_model_overrides: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        build_project_policy_from_form: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        list_workflow_choices: Callable[..., list[dict[str, Any]]] | None = None,
        workflow_availability_assert: Callable[..., Any] | None = None,
        write_project_profile_to_kb: Callable[[dict[str, Any]], None] | None = None,
        dispatch_project_notification_event: Callable[..., list[dict[str, Any]]] | None = None,
        to_json_obj: Callable[[Any], Any] | None = None,
        monotonic: Callable[[], float] | None = None,
        attachment_max_size_bytes: int = 20 * 1024 * 1024,
        attachment_max_files: int = 10,
        allowed_attachment_mimes: set[str] | None = None,
        allowed_attachment_extensions: set[str] | None = None,
        artifact_plane: Any | None = None,
        idempotency_result_retention_days: int = 30,
    ):
        self._get_logger = get_logger
        self._resolve_workflow_selection = resolve_workflow_selection
        self._validate_explicit_workflow_override = validate_explicit_workflow_override
        self._sanitize_external_input = sanitize_external_input or (lambda value: str(value or ""))
        self._enqueue_task = enqueue_task
        self._enqueue_task_in_queue = enqueue_task_in_queue
        self._parse_test_mode = parse_test_mode
        self._normalize_cron_expr = normalize_cron_expr or (lambda value: str(value or "").strip())
        self._cron_next_run_at = cron_next_run_at
        self._form_flag = form_flag or (lambda value: bool(value))
        self._run_scheduled_job_once = run_scheduled_job_once
        self._parse_agent_model_overrides = parse_agent_model_overrides or (lambda form_data: {})
        self._build_project_policy_from_form = build_project_policy_from_form or (
            lambda form_data: {}
        )
        self._list_workflow_choices = list_workflow_choices or (lambda **kwargs: [])
        self._workflow_availability_assert = workflow_availability_assert
        self._write_project_profile_to_kb = write_project_profile_to_kb
        self._dispatch_project_notification_event = dispatch_project_notification_event
        self._to_json_obj = to_json_obj or (lambda value: value)
        self._monotonic = monotonic or time.monotonic
        self._attachment_max_size_bytes = int(attachment_max_size_bytes or 0)
        self._attachment_max_files = int(attachment_max_files or 0)
        self._artifact_plane = artifact_plane
        self._idempotency_result_retention_days = min(
            3650, max(1, int(idempotency_result_retention_days))
        )
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

    def _logger(self) -> Any:
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(ERR_DATABASE_NOT_CONFIGURED)
        return bl

    def _extension_workflow_ids(self, *, account_id: str) -> set[str]:
        bl = self._logger()
        dsn = str(getattr(bl, "dsn", "") or "").strip()
        if not dsn:
            return set()
        try:
            from core.application.extensions.service_support import (
                get_default_extension_runtime_store,
            )
            from core.infrastructure.db.platform_stores import PlatformIdentityStore

            runtime_store = get_default_extension_runtime_store(
                get_logger=lambda: PlatformIdentityStore(dsn)
            )
        except Exception as exc:
            logger.debug("extension runtime store unavailable for workflow choices: %s", exc)
            return set()
        if runtime_store is None or not hasattr(runtime_store, "list_projections"):
            return set()
        normalized_account_id = str(account_id or DEFAULT_ACCOUNT_ID).strip() or DEFAULT_ACCOUNT_ID
        try:
            rows = runtime_store.list_projections(
                account_id=normalized_account_id,
                artifact_kind="workflow",
                only_active=True,
            )
        except Exception as exc:
            logger.debug(
                "extension runtime workflow lookup failed account_id=%s: %s",
                normalized_account_id,
                exc,
            )
            return set()

        workflow_ids: set[str] = set()
        for row in rows or []:
            if hasattr(row, "model_dump"):
                projection = row.model_dump(mode="json")
            elif isinstance(row, dict):
                projection = dict(row)
            else:
                projection = {}
            snapshot = projection.get("projection")
            snapshot = dict(snapshot) if isinstance(snapshot, dict) else {}
            install_id = str(projection.get("install_id") or "").strip()
            workflow_id = str(
                snapshot.get("local_workflow_id")
                or snapshot.get("workflow_id")
                or projection.get("artifact_id")
                or ""
            ).strip()
            if install_id and workflow_id:
                workflow_ids.add(projection_bridge_id(install_id, workflow_id))
        return workflow_ids

    @staticmethod
    def _scope(
        scope: ScopeContext | None,
        *,
        project_id: Any = None,
        workflow_id: Any = None,
    ) -> ScopeContext:
        return _service_scope(scope, project_id=project_id, workflow_id=workflow_id)

    @staticmethod
    def _require_project(bl: Any, project_id: Any) -> dict[str, Any]:
        project = bl.get_project(project_id)
        if not project:
            raise ServiceNotFoundError(ERR_PROJECT_NOT_FOUND)
        return project

    @staticmethod
    def _require_project_in_scope(bl: Any, project_id: Any, scope: ScopeContext) -> dict[str, Any]:
        project = bl.get_project(project_id)
        if not project:
            raise ServiceNotFoundError(ERR_PROJECT_NOT_FOUND)
        require_project_access(scope, project)
        return project

    def _project_run_config(
        self,
        bl: Any,
        project_id: Any,
        *,
        project: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not hasattr(bl, "get_project_run_config"):
            raise ServiceUnavailableError("Project runtime config is unavailable")
        run_cfg = bl.get_project_run_config(project_id) or {}
        normalized = dict(run_cfg) if isinstance(run_cfg, dict) else {}
        project_row = project if isinstance(project, dict) else {}
        project_name = self._normalized_text(
            normalized.get("project_name") or project_row.get("name")
        )
        if project_name:
            normalized["project_name"] = project_name
        return normalized

    @staticmethod
    def _coerce_int_id(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ServiceValidationError("Invalid identifier") from exc

    def _scheduled_job(
        self,
        bl: Any,
        project_id: Any,
        scheduled_job_id: str,
        *,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        if not hasattr(bl, "get_scheduled_job"):
            raise ServiceUnavailableError(ERR_SCHEDULED_JOBS_SCHEMA_UNAVAILABLE)
        normalized_account_id = str(account_id or "").strip() or None
        try:
            scheduled_job = bl.get_scheduled_job(
                str(project_id),
                str(scheduled_job_id),
                account_id=normalized_account_id,
            )
        except TypeError:
            scheduled_job = bl.get_scheduled_job(str(project_id), str(scheduled_job_id))
        if not scheduled_job:
            raise ServiceNotFoundError(SCHEDULED_JOB_NOT_FOUND_MESSAGE)
        return scheduled_job if isinstance(scheduled_job, dict) else dict(scheduled_job)

    def _resolve_submission_project(
        self,
        bl: Any,
        command: ProjectTaskEnqueueInput,
        *,
        allow_implicit_project: bool,
    ) -> tuple[str, dict[str, Any], str, dict[str, Any], bool]:
        if command.project_id:
            project_id = str(command.project_id).strip()
            project = self._require_project(bl, project_id)
            run_cfg = self._project_run_config(bl, project_id, project=project)
            project_name = str(run_cfg.get("project_name") or project.get("name") or "").strip()
            return project_id, project, project_name, run_cfg, False

        _ = allow_implicit_project
        raise ServiceValidationError(ERR_PROJECT_ID_REQUIRED)

    def _resolve_selected_workflow(
        self,
        *,
        project_config: dict[str, Any],
        requested_workflow_id: str,
    ) -> tuple[str, str]:
        if self._resolve_workflow_selection is None:
            raise ServiceUnavailableError(ERR_WORKFLOW_RESOLUTION_UNAVAILABLE)
        requested = str(requested_workflow_id or "").strip()
        project_workflows = (
            project_config.get("project_workflows") if isinstance(project_config, dict) else None
        )
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

    def _bind_direct_workflow_inputs(
        self,
        command: ProjectTaskEnqueueInput,
        trusted_internal: bool,
        workflow_interface: Any,
        provided_workflow_inputs: dict[str, Any],
        attachment_rows: list[dict[str, Any]],
    ) -> bool:
        """Bind actual files for direct contract callers and report their input policy."""
        source = str(command.task_source or "").strip()
        declared_direct_inputs = source == "mcp_workflow" or (
            trusted_internal
            and source == "manual_ui"
            and "expected_workflow_revision" in (command.job_meta or {})
        )
        if declared_direct_inputs:
            self._bind_declared_workflow_file_inputs(
                workflow_interface, provided_workflow_inputs, attachment_rows
            )
        return declared_direct_inputs

    @staticmethod
    def _bind_declared_workflow_file_inputs(
        workflow_interface: Any,
        provided_workflow_inputs: dict[str, Any],
        attachment_rows: list[dict[str, Any]],
    ) -> None:
        """Validate named uploads and bind their content descriptors before admission."""
        file_fields = {
            row.name: row for row in workflow_interface.expected_inputs if row.type == "file"
        }
        uploads = {row.get("input_name"): row for row in attachment_rows}
        if set(uploads) - set(file_fields):
            raise ServiceValidationError("Upload input_name must name a declared file input")
        for name, field in file_fields.items():
            if name in provided_workflow_inputs:
                raise ServiceValidationError(
                    f"File input '{name}' must be supplied through files, not JSON inputs"
                )
            if field.required and name not in uploads:
                raise ServiceValidationError(
                    f"Workflow file input '{name}' requires an actual upload in files",
                    payload={"code": "workflow_file_input_required"},
                )
        for name, row in uploads.items():
            provided_workflow_inputs[name] = {
                "filename": row["filename"],
                "content_type": row["mime_type"],
                "size_bytes": row["size_bytes"],
                "sha256": row["sha256"],
            }

    def _validated_attachment_rows(self, command: ProjectTaskEnqueueInput) -> list[dict[str, Any]]:
        attachments = list(command.files or [])
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

            staged_path = str(attachment.content_path or "").strip()
            if staged_path and attachment.content is not None:
                raise ServiceValidationError(
                    "Attachment cannot contain both body bytes and staged content"
                )
            if staged_path:
                if not os.path.isfile(staged_path):
                    raise ServiceValidationError(f"Staged attachment is unavailable: {filename}")
                digest = hashlib.sha256()
                content_size = 0
                try:
                    with open(staged_path, "rb") as staged_file:
                        while chunk := staged_file.read(1024 * 1024):
                            content_size += len(chunk)
                            digest.update(chunk)
                except OSError as exc:
                    raise ServiceValidationError(
                        f"Could not read staged attachment: {filename}"
                    ) from exc
                expected_size = int(attachment.size_bytes or 0)
                expected_sha = str(attachment.sha256 or "").strip().lower()
                actual_sha = digest.hexdigest()
                if content_size != expected_size or actual_sha != expected_sha:
                    raise ServiceValidationError(
                        f"Staged attachment integrity check failed: {filename}"
                    )
                content = None
            else:
                content = bytes(attachment.content or b"")
                content_size = len(content)
                actual_sha = hashlib.sha256(content).hexdigest()
            if (
                self._attachment_max_size_bytes > 0
                and content_size > self._attachment_max_size_bytes
            ):
                raise ServiceValidationError(
                    f"File too large: {filename} ({content_size // 1024} KB, max 20 MB)"
                )
            rows.append(
                {
                    "filename": filename,
                    **({"input_name": attachment.input_name} if attachment.input_name else {}),
                    "mime_type": mime_type,
                    "size_bytes": content_size,
                    "content": content,
                    "content_path": staged_path,
                    "sha256": actual_sha,
                }
            )
        return rows

    @staticmethod
    def _attachment_metadata_patch(
        job_row: dict[str, Any] | None, attachment_metadata: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Build the complete job-meta patch without persisting partial state."""
        job_row = job_row or {}
        existing_meta = (job_row or {}).get("meta") or {}
        if isinstance(existing_meta, str):
            existing_meta = json.loads(existing_meta)
        existing_meta = {} if not isinstance(existing_meta, dict) else dict(existing_meta)
        existing_meta["files"] = attachment_metadata
        # The invocation snapshot (what job detail renders) is built at enqueue time
        # from the task envelope, BEFORE attachments are persisted via the
        # pre-publish hook — so its file_count/file_names/files/task_envelope.files
        # are empty. Backfill them here so uploaded files stay visible in job detail
        # and reach the workflow as execution context.
        from core.application.workflows.workflow_start_runtime import _invocation_file_summaries

        file_names = [
            str(row.get("filename") or "").strip()
            for row in attachment_metadata
            if isinstance(row, dict) and str(row.get("filename") or "").strip()
        ]
        invocation = existing_meta.get("invocation")
        if isinstance(invocation, dict):
            invocation = dict(invocation)
            invocation["files"] = _invocation_file_summaries(attachment_metadata)
            summary = invocation.get("request_context_summary")
            if isinstance(summary, dict):
                summary = dict(summary)
                summary["file_count"] = len(attachment_metadata)
                summary["file_names"] = file_names
                invocation["request_context_summary"] = summary
            envelope = invocation.get("task_envelope")
            if isinstance(envelope, dict):
                envelope = dict(envelope)
                envelope["files"] = attachment_metadata
                invocation["task_envelope"] = envelope
            existing_meta["invocation"] = invocation
        # Keep the top-level task_envelope (execution context) in sync too.
        top_envelope = existing_meta.get("task_envelope")
        if isinstance(top_envelope, dict):
            top_envelope = dict(top_envelope)
            top_envelope["files"] = attachment_metadata
            existing_meta["task_envelope"] = top_envelope
        return existing_meta

    @staticmethod
    def _merge_attachment_metadata(
        bl: Any, job_id: Any, attachment_metadata: list[dict[str, Any]]
    ) -> None:
        if not attachment_metadata or not hasattr(bl, "get_job") or not hasattr(bl, "update_job"):
            return
        existing_meta = ProjectCommandServiceCoreMixin._attachment_metadata_patch(
            bl.get_job(str(job_id)) or {}, attachment_metadata
        )
        bl.update_job(str(job_id), meta=existing_meta)

    def _normalized_form_data(self, form_data: dict[str, Any] | None) -> dict[str, Any]:
        return dict(form_data or {}) if isinstance(form_data, dict) else {}

    def _object_dict(self, value: Any) -> dict[str, Any]:
        parsed = self._to_json_obj(value)
        if isinstance(parsed, dict):
            return dict(parsed)
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str):
            try:
                loaded = json.loads(value)
            except Exception:
                return {}
            if isinstance(loaded, dict):
                return dict(loaded)
        return {}

    def _selected_workflow_ids_for_create(
        self,
        command: ProjectCreateInput,
        *,
        account_id: str,
    ) -> list[str]:
        selected = [
            str(wf or "").strip() for wf in (command.workflow_ids or []) if str(wf or "").strip()
        ]
        selected = list(dict.fromkeys(selected))
        form_data = command.form_data if isinstance(command.form_data, dict) else {}
        if selected or "workflow_ids" in form_data:
            return selected
        return [
            str(wf.get("workflow_id") or "").strip()
            for wf in self._list_workflow_choices(
                enabled_only=True,
                account_id=account_id,
            )
            if isinstance(wf, dict) and str(wf.get("workflow_id") or "").strip()
        ]

    @staticmethod
    def _selected_workflow_ids_for_update(command: ProjectUpdateInput) -> list[str]:
        return list(
            dict.fromkeys(
                str(wf or "").strip()
                for wf in (command.workflow_ids or [])
                if str(wf or "").strip()
            )
        )

    def _resolved_operator_default_workflow(
        self, operator_default_workflow_id: str, selected_workflow_ids: list[str]
    ) -> str:
        resolved_default = ""
        if str(operator_default_workflow_id or "").strip():
            if self._resolve_workflow_selection is None:
                raise ServiceUnavailableError(ERR_WORKFLOW_RESOLUTION_UNAVAILABLE)
            try:
                resolved_default, _ = self._resolve_workflow_selection(
                    str(operator_default_workflow_id or "").strip(),
                    [{"workflow_id": wf_id, "enabled": True} for wf_id in selected_workflow_ids],
                )
            except ValueError as exc:
                raise ServiceValidationError(str(exc)) from exc
        return resolved_default

    def _create_agents_config(
        self, command: ProjectCreateInput, form_data: dict[str, Any]
    ) -> dict[str, Any]:
        agents_config: dict[str, Any] = {}
        if str(command.default_provider or "").strip():
            agents_config["default_provider"] = str(command.default_provider).strip()
        if str(command.default_model or "").strip():
            agents_config["default_model"] = str(command.default_model).strip()
        agent_cfg = self._parse_agent_model_overrides(form_data)
        if agent_cfg:
            agents_config["agents"] = {"config": agent_cfg}
        return agents_config

    def _update_agents_config(
        self,
        existing_project: dict[str, Any],
        command: ProjectUpdateInput,
        form_data: dict[str, Any],
    ) -> dict[str, Any]:
        agents_config = self._object_dict((existing_project or {}).get("agents_config"))

        if "default_provider" in form_data:
            if str(command.default_provider or "").strip():
                agents_config["default_provider"] = str(command.default_provider).strip()
            elif "default_provider" in agents_config:
                del agents_config["default_provider"]

        if "default_model" in form_data:
            if str(command.default_model or "").strip():
                agents_config["default_model"] = str(command.default_model).strip()
            elif "default_model" in agents_config:
                del agents_config["default_model"]

        agent_override_fields = {
            str(key)
            for key in form_data
            if str(key).startswith("agent_provider_") or str(key).startswith("agent_model_")
        }
        if agent_override_fields:
            agent_cfg = self._parse_agent_model_overrides(form_data)
            current_agents = self._object_dict(agents_config.get("agents"))
            current_agents["config"] = agent_cfg
            agents_config["agents"] = current_agents

        # Preserve unknown stored keys, including inert legacy tool definitions.
        return agents_config


__all__ = [name for name in globals() if not name.startswith("__")]
