# ruff: noqa: F403, F405
# mypy: ignore-errors
from __future__ import annotations

from .command_services_common import *  # NOSONAR


class ProjectCommandServiceConfigMixin:
    @staticmethod
    def _resolve_model_provider_id(
        provider: str, conn: Any = None, account_id: str = DEFAULT_ACCOUNT_ID
    ) -> str | None:
        """If a provider family's only credential source is a DB-backed named_provider,
        return its id so get_chat_model() can resolve the API key at runtime."""
        if not provider or conn is None:
            return None
        try:
            from core.application.settings.model_catalog import detect_provider_sources

            sources = detect_provider_sources(account_id=account_id, conn=conn)
            family_sources = sources.get(provider, [])
            # Auto-wire the account-scoped encrypted provider record. Environment
            # API-key sources are deliberately not part of model routing.
            named = [s for s in family_sources if s.source_type == "named_provider"]
            if named:
                return str(named[0].source_ref or "").strip() or None
        except Exception:
            pass
        return None

    def _enrich_agents_config_with_provider_ids(
        self,
        agents_config: dict[str, Any],
        conn: Any = None,
        account_id: str = DEFAULT_ACCOUNT_ID,
    ) -> None:
        """Auto-wire model_provider_id for providers backed only by DB credentials."""
        default_provider = str(agents_config.get("default_provider") or "").strip()
        if default_provider:
            pid = self._resolve_model_provider_id(default_provider, conn, account_id)
            if pid is not None:
                agents_config["model_provider_id"] = pid
            elif "model_provider_id" in agents_config:
                del agents_config["model_provider_id"]

        per_agent = (agents_config.get("agents") or {}).get("config") or {}
        for agent_cfg in per_agent.values():
            if not isinstance(agent_cfg, dict):
                continue
            ap = str(agent_cfg.get("provider") or "").strip()
            if ap:
                pid = self._resolve_model_provider_id(ap, conn, account_id)
                if pid is not None:
                    agent_cfg["model_provider_id"] = pid
                elif "model_provider_id" in agent_cfg:
                    del agent_cfg["model_provider_id"]

    @staticmethod
    def _validate_agents_config_models(
        agents_config: dict[str, Any], *, conn: Any = None, account_id: str = DEFAULT_ACCOUNT_ID
    ) -> None:
        """Validate routes against the active account-scoped model options."""
        try:
            from core.application.settings.model_catalog import (
                validate_chat_provider_model_pair,
            )
        except ImportError:
            return

        default_provider = str(agents_config.get("default_provider") or "").strip()
        default_model = str(agents_config.get("default_model") or "").strip()
        valid, err = validate_chat_provider_model_pair(
            default_provider, default_model, account_id=account_id, conn=conn
        )
        if not valid:
            raise ServiceValidationError(f"Default model config: {err}")

        per_agent = (agents_config.get("agents") or {}).get("config") or {}
        for agent_name, agent_cfg in per_agent.items():
            if not isinstance(agent_cfg, dict):
                continue
            ap = str(agent_cfg.get("provider") or "").strip()
            am = str(agent_cfg.get("model") or "").strip()
            valid, err = validate_chat_provider_model_pair(ap, am, account_id=account_id, conn=conn)
            if not valid:
                raise ServiceValidationError(f"Agent '{agent_name}' model config: {err}")

    def _save_project_profile(self, profile_payload: dict[str, Any]) -> None:
        if self._write_project_profile_to_kb is None:
            return
        try:
            self._write_project_profile_to_kb(profile_payload)
        except Exception as exc:
            logger.warning("project profile sync failed (non-fatal): %s", exc)

    def _schedule_project_profile_sync(self, profile_payload: dict[str, Any]) -> None:
        if self._write_project_profile_to_kb is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("project profile sync skipped: no running event loop")
            return

        task = loop.create_task(asyncio.to_thread(self._save_project_profile, profile_payload))

        def _log_task_result(done_task: asyncio.Future[Any]) -> None:
            try:
                done_task.result()
            except Exception:
                logger.warning("project profile background sync failed", exc_info=True)

        task.add_done_callback(_log_task_result)

    @staticmethod
    def _create_project_revision(
        bl: Any,
        project_id: Any,
        *,
        author: str,
        action: str,
        source: str,
    ) -> None:
        if not hasattr(bl, "create_project_settings_revision"):
            return
        try:
            bl.create_project_settings_revision(
                project_id,
                author=str(author or "system").strip() or "system",
                action=action,
                source=source,
            )
        except Exception as exc:
            logger.debug("Project settings revision write failed (non-fatal): %s", exc)

    def _project_policy_payload(self, form_data: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._build_project_policy_from_form(form_data)
        except ValueError as exc:
            raise ServiceValidationError(str(exc)) from exc

    @staticmethod
    def _project_policy_form_present(form_data: dict[str, Any]) -> bool:
        return any(str(key).startswith("policy_") for key in form_data)

    def _project_profile_payload(
        self,
        *,
        project_id: Any | None,
        account_id: Any | None,
        name: str,
        agents_config: dict[str, Any],
        project_rules: str,
        assigned_data_connections: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        def json_value(value: Any) -> Any:
            if isinstance(value, datetime):
                return value.isoformat()
            if isinstance(value, Mapping):
                return {str(key): json_value(item) for key, item in value.items()}
            if isinstance(value, (list, tuple, set)):
                return [json_value(item) for item in value]
            return value

        return json_value(
            {
                "id": str(project_id or "").strip(),
                "account_id": str(account_id or "").strip(),
                "name": str(name or "").strip(),
                "agents_config": agents_config,
                "project_rules": str(project_rules or "").strip(),
                "assigned_data_connections": [
                    dict(item)
                    for item in (assigned_data_connections or [])
                    if isinstance(item, dict)
                ],
            }
        )

    @staticmethod
    def _normalized_text(value: Any) -> str:
        return str(value or "").strip()

    @classmethod
    def _normalized_text_or_none(cls, value: Any) -> str | None:
        text = cls._normalized_text(value)
        return text or None

    def _project_update_payload(
        self,
        *,
        command: ProjectUpdateInput,
        agents_config: dict[str, Any],
        selected_workflow_ids: list[str],
        resolved_default_workflow: str,
        project_policy: dict[str, Any] | None,
        existing_project: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        form_data = self._normalized_form_data(command.form_data)
        existing = dict(existing_project or {})

        def command_has_value(field: str) -> bool:
            value = getattr(command, field)
            if isinstance(value, str):
                return bool(value.strip())
            if isinstance(value, list | tuple | set | dict):
                return bool(value)
            return value is not None

        def update_value(field: str) -> Any:
            if field in form_data or command_has_value(field):
                return getattr(command, field)
            return existing.get(field)

        return {
            "name": self._normalized_text(command.name),
            "workspace_id": self._required_workspace_id(command.workspace_id),
            "agents_config": agents_config,
            "project_rules": self._normalized_text_or_none(update_value("project_rules")),
            "description": self._normalized_text_or_none(update_value("description")),
            "operator_default_workflow_id": resolved_default_workflow or None,
            "project_workflow_ids": selected_workflow_ids,
            "project_policy": project_policy,
        }

    def _persist_update_project(
        self,
        *,
        bl: Any,
        project_id: Any,
        command: ProjectUpdateInput,
        agents_config: dict[str, Any],
        selected_workflow_ids: list[str],
        resolved_default_workflow: str,
        project_policy: dict[str, Any] | None,
        account_id: str,
        actor_id: str,
        existing_project: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        update_payload = self._project_update_payload(
            command=command,
            agents_config=agents_config,
            selected_workflow_ids=selected_workflow_ids,
            resolved_default_workflow=resolved_default_workflow,
            project_policy=project_policy,
            existing_project=existing_project,
        )
        project_update_fields = dict(update_payload)
        project_update_fields.pop("project_workflow_ids", None)
        project_update_fields.pop("operator_default_workflow_id", None)
        try:
            from core.infrastructure.db.db_pool import transaction_connection

            with transaction_connection(bl) as transaction_conn:
                bl.update_project(project_id, **project_update_fields)
                self._persist_authoritative_project_workflow_bindings(
                    bl=bl,
                    project_id=str(project_id),
                    workflow_ids=selected_workflow_ids,
                    operator_default_workflow_id=resolved_default_workflow,
                    account_id=account_id,
                    actor_id=actor_id,
                    transaction_conn=transaction_conn,
                )
        except (ServiceUnavailableError, ServiceValidationError):
            raise
        except Exception as exc:
            raise ServiceError("Failed to update project") from exc
        return update_payload

    def _create_project_payload(
        self,
        *,
        command: ProjectCreateInput,
        resolved: ScopeContext,
        agents_config: dict[str, Any],
        selected_workflow_ids: list[str],
        resolved_default_workflow: str,
        project_policy: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "name": self._normalized_text(command.name),
            "workspace_id": self._required_workspace_id(command.workspace_id),
            "agents_config": dict(agents_config),
            "project_rules": self._normalized_text_or_none(command.project_rules),
            "description": self._normalized_text_or_none(command.description),
            "operator_default_workflow_id": resolved_default_workflow or None,
            "project_workflow_ids": selected_workflow_ids,
            "project_policy": project_policy,
            "account_id": resolved.account_id,
            "tenant_id": resolved.tenant_id,
        }

    @staticmethod
    def _required_workspace_id(workspace_id: Any) -> str:
        normalized = str(workspace_id or "").strip()
        if not normalized:
            raise ServiceValidationError("workspace_id is required")
        return normalized

    @staticmethod
    def _normalized_data_connection_ids(connection_ids: list[str] | None) -> list[str]:
        normalized_ids: list[str] = []
        seen_ids: set[str] = set()
        for raw_id in connection_ids or []:
            connection_id = str(raw_id or "").strip()
            if not connection_id:
                continue
            if connection_id in seen_ids:
                raise ServiceValidationError(f"Duplicate data connection id '{connection_id}'")
            seen_ids.add(connection_id)
            normalized_ids.append(connection_id)
        return normalized_ids

    def _persist_project_data_connections(
        self,
        *,
        bl: Any,
        project_id: Any,
        data_connection_ids: list[str] | None,
    ) -> list[dict[str, Any]]:
        setter = getattr(bl, "set_project_data_connections", None)
        if not callable(setter):
            if data_connection_ids:
                raise ServiceUnavailableError("Project data connection storage is unavailable")
            return []
        requested_ids = self._normalized_data_connection_ids(data_connection_ids)
        try:
            return setter(str(project_id), requested_ids)
        except ValueError as exc:
            raise ServiceValidationError(str(exc)) from exc

    @staticmethod
    def _data_connection_ids(rows: list[dict[str, Any]] | None) -> list[str]:
        return [
            str(item.get("connection_id") or "").strip()
            for item in (rows or [])
            if isinstance(item, dict) and str(item.get("connection_id") or "").strip()
        ]

    @staticmethod
    def _require_workspace_in_scope(
        bl: Any,
        workspace_id: str,
        *,
        account_id: str,
        tenant_id: str | None,
        allowed_workspace_ids: frozenset[str],
    ) -> dict[str, Any]:
        normalized_workspace_id = str(workspace_id or "").strip()
        if not normalized_workspace_id:
            raise ServiceValidationError("workspace_id is required")
        if allowed_workspace_ids and normalized_workspace_id not in allowed_workspace_ids:
            raise ServiceForbiddenError("Workspace is outside allowed scope")
        normalized_account_id = str(account_id or "").strip()
        normalized_tenant_id = str(tenant_id or "").strip()
        with bl.conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, account_id, tenant_id, workspace_key, display_name, status, deleted_at
                FROM workspaces
                WHERE id = %s
                  AND account_id = %s
                  AND COALESCE(tenant_id::text, '') = %s
                  AND deleted_at IS NULL
                  AND status = 'active'
                LIMIT 1
                """,
                (normalized_workspace_id, normalized_account_id, normalized_tenant_id),
            )
            row = cur.fetchone()
        if not row:
            raise ServiceNotFoundError("Workspace not found")
        if isinstance(row, dict):
            return dict(row)
        return {
            "id": row[0],
            "account_id": row[1],
            "tenant_id": row[2],
            "workspace_key": row[3],
            "display_name": row[4],
            "status": row[5],
            "deleted_at": row[6],
        }

    @staticmethod
    def _persist_create_project(bl: Any, payload: dict[str, Any]) -> str:
        try:
            return str(bl.add_project(**payload))
        except ValueError as exc:
            from core.infrastructure.db.project_registration_store import PROJECT_ALREADY_EXISTS

            if str(exc) == PROJECT_ALREADY_EXISTS:
                raise ServiceConflictError(PROJECT_ALREADY_EXISTS) from exc
            raise ServiceError("Failed to create project") from exc
        except Exception as exc:
            raise ServiceError("Failed to create project") from exc

    @staticmethod
    def _project_name_exists(bl: Any, *, name: str, account_id: str) -> bool:
        if not hasattr(bl, "get_projects"):
            return False
        try:
            rows = bl.get_projects(account_id=account_id)
        except TypeError:
            rows = bl.get_projects()
        except Exception:
            return False
        normalized_name = str(name or "").strip().casefold()
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            if str(row.get("name") or "").strip().casefold() == normalized_name:
                return True
        return False

    @staticmethod
    def _timezone_from_user_row(row: Any) -> str:
        if not row:
            return ""
        return str(row[0] or "").strip()

    def _resolved_user_timezone(self, *, bl: Any, resolved: ScopeContext) -> str:
        user_tz = (
            str(resolved.user_timezone or "").strip() if hasattr(resolved, "user_timezone") else ""
        )
        if user_tz or not hasattr(bl, "conn"):
            return user_tz
        actor_username = str(resolved.actor_id or "").strip()
        if not actor_username:
            return ""
        try:
            with bl.conn.cursor() as tz_cur:
                tz_cur.execute(
                    "SELECT timezone FROM users WHERE username = %s LIMIT 1",
                    (actor_username,),
                )
                return self._timezone_from_user_row(tz_cur.fetchone())
        except Exception:
            return ""

    def _set_initial_project_schedule_timezone(
        self,
        *,
        bl: Any,
        project_id: Any,
        resolved: ScopeContext,
    ) -> None:
        try:
            from core.application.settings.timezone_service import default_project_schedule_tz

            initial_tz = default_project_schedule_tz(
                self._resolved_user_timezone(bl=bl, resolved=resolved) or None
            )
            if initial_tz == "UTC":
                return
            with bl.conn.cursor() as tz_cur:
                tz_cur.execute(
                    "UPDATE projects SET schedule_timezone = %s WHERE id = %s",
                    (initial_tz, project_id),
                )
        except Exception:
            pass  # Non-fatal: defaults to UTC from migration


__all__ = [name for name in globals() if not name.startswith("__")]
