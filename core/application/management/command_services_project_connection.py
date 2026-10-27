# ruff: noqa: F403, F405
# mypy: ignore-errors
from __future__ import annotations

from core.application.management.project_connection_contract import ProjectConnectionContract

from .command_services_common import *  # NOSONAR


def cleanup_project_connection_owned_data(
    bl: Any,
    *,
    connection: dict[str, Any],
    cur: Any | None = None,
    oauth_revocations: list[dict[str, Any]] | None = None,
) -> None:
    """Remove one connection and its host-owned state using the caller transaction."""
    connection_id = str(connection.get("connection_id") or "").strip()
    account_id = str(connection.get("account_id") or "").strip()
    if not connection_id or not account_id:
        raise ServiceValidationError("connection_id and account_id are required for cleanup")

    captured_revocations = oauth_revocations if oauth_revocations is not None else []

    def _capture_revocation(_cursor: Any) -> None:
        try:
            from core.application.auth.oauth_client import prepare_connection_oauth_revocation

            prepared = prepare_connection_oauth_revocation(bl, connection=connection)
            if prepared:
                captured_revocations.append(prepared)
        except Exception:
            logger.warning(
                "OAuth token revocation capture failed during project connection delete",
                extra={"connection_id": connection_id},
            )

    def _delete(cursor: Any) -> None:
        cursor.execute(
            """
            SELECT connection_id
              FROM project_connections
             WHERE connection_id = %s
               AND account_id::text = %s
             FOR UPDATE
            """,
            (connection_id, account_id),
        )
        if not cursor.fetchone():
            return
        _capture_revocation(cursor)
        cursor.execute(
            "DELETE FROM project_connection_oauth_states WHERE connection_id = %s",
            (connection_id,),
        )
        cursor.execute(
            "DELETE FROM project_connection_sessions WHERE connection_id = %s",
            (connection_id,),
        )
        cursor.execute(
            """
            DELETE FROM secret_refs
             WHERE account_id::text = %s
               AND ref_key LIKE %s
            """,
            (account_id, f"project_connection:{connection_id}:%"),
        )
        cursor.execute(
            "DELETE FROM extension_project_state WHERE connection_id = %s",
            (connection_id,),
        )
        cursor.execute(
            "DELETE FROM project_connections WHERE connection_id = %s AND account_id::text = %s",
            (connection_id, account_id),
        )

    if cur is not None:
        _delete(cur)
        return
    from core.infrastructure.db.db_pool import transaction_connection

    with transaction_connection(bl) as transaction_conn, transaction_conn.cursor() as owned_cur:
        _delete(owned_cur)
    if captured_revocations:
        from core.application.auth.oauth_client import execute_prepared_oauth_revocation

        for prepared in captured_revocations:
            execute_prepared_oauth_revocation(prepared)


class ProjectConnectionService(ProjectConnectionContract):
    """CRUD for project connections — project-scoped extension/connector activation."""

    def __init__(
        self,
        *,
        get_logger: Callable[[], Any],
        get_extension_install_store: Callable[[], Any] | None = None,
        get_extension_runtime_store: Callable[[], Any] | None = None,
    ):
        self._get_logger = get_logger
        self._get_extension_install_store = get_extension_install_store
        self._get_extension_runtime_store = get_extension_runtime_store

    def _extension_install_store(self, bl: Any) -> Any:
        if self._get_extension_install_store is not None:
            return self._get_extension_install_store()
        if bl is not None and hasattr(bl, "conn"):
            from core.application.extensions.install import DBExtensionInstallStore

            return DBExtensionInstallStore(bl.conn)
        from core.application.extensions.install import ExtensionInstallStore

        return ExtensionInstallStore()

    def _extension_runtime_store(self, bl: Any) -> Any:
        if self._get_extension_runtime_store is not None:
            return self._get_extension_runtime_store()
        if bl is not None and hasattr(bl, "conn"):
            from core.application.extensions.runtime_store import DBExtensionRuntimeStore

            return DBExtensionRuntimeStore(bl.conn)
        from core.application.extensions.runtime_store import ExtensionRuntimeStore

        return ExtensionRuntimeStore()

    def _normalize_connection_view_row(self, row: dict[str, Any] | None) -> dict[str, Any]:
        payload = dict(row or {})
        for key in (
            "account_id",
            "display_name",
            "connection_kind",
            "connection_type",
            "environment",
            "extension_install_id",
            "runtime_mode",
            "remote_base_url",
            "health_status",
            "runtime_status",
            "last_error",
        ):
            payload[key] = self._normalize_text(payload.get(key))

        if payload.get("extension_id") is not None:
            payload["extension_id"] = self._normalize_text(payload.get("extension_id")) or None

        try:
            payload["credential_revision"] = max(1, int(payload.get("credential_revision") or 1))
        except (TypeError, ValueError):
            payload["credential_revision"] = 1

        for key, default in (
            ("connection_config", {}),
            ("ingress_config", {}),
            ("workflow_mappings", {}),
            ("secret_refs", {}),
            ("connection_routes", []),
            ("tags", []),
        ):
            if payload.get(key) is None:
                payload[key] = default
        payload["secret_refs"] = self._normalize_secret_refs(payload.get("secret_refs"))

        return payload

    @staticmethod
    def _require_project(bl: Any, project_id: Any) -> dict[str, Any]:
        project = bl.get_project(project_id) if hasattr(bl, "get_project") else None
        if not isinstance(project, dict):
            raise ServiceNotFoundError(ERR_PROJECT_NOT_FOUND)
        return project

    def _require_project_in_scope(
        self, bl: Any, project_id: Any, scope: ScopeContext
    ) -> dict[str, Any]:
        project = self._require_project(bl, project_id)
        require_project_access(scope, project)
        return project

    def _project_connection_runtime_defaults(
        self,
        *,
        bl: Any,
        account_id: str,
        connection_kind: str,
        extension_id: str | None,
        extension_install_id: str,
        runtime_mode: str,
        remote_base_url: str,
        health_status: str,
        runtime_status: str,
        last_seen_ok_at: float | None,
        last_error: str,
    ) -> dict[str, Any]:
        normalized_kind = self._normalize_text(connection_kind).lower() or "extension"
        payload: dict[str, Any] = {
            "connection_kind": normalized_kind,
            "extension_id": self._normalize_text(extension_id) or None,
            "extension_install_id": "",
            "runtime_mode": None,
            "remote_base_url": "",
            "health_status": "",
            "runtime_status": "",
            "last_seen_ok_at": None,
            "last_error": "",
        }
        if normalized_kind != "extension_runtime":
            normalized_extension_id = self._normalize_text(payload["extension_id"])
            if not normalized_extension_id:
                raise ServiceValidationError(
                    "extension_id is required for extension and mcp_connector connections"
                )
            normalized_install_id = self._normalize_text(extension_install_id)
            if normalized_kind == "extension" and not normalized_install_id:
                raise ServiceValidationError(
                    "extension_install_id is required for extension connections"
                )
            if normalized_install_id:
                install_store = self._extension_install_store(bl)
                install_record = (
                    install_store.get_by_install_id(normalized_install_id)
                    if hasattr(install_store, "get_by_install_id")
                    else None
                )
                if not install_record:
                    raise ServiceValidationError(
                        "extension_install_id does not reference an installed extension"
                    )
                install_visibility = self._normalize_text(
                    getattr(install_record, "visibility", "")
                ).lower()
                if (
                    self._normalize_text(getattr(install_record, "account_id", ""))
                    != self._normalize_text(account_id)
                    and install_visibility != "global"
                ):
                    raise ServiceValidationError(
                        "extension_install_id is not visible for this account"
                    )
                if (
                    self._normalize_text(getattr(install_record, "extension_id", ""))
                    != normalized_extension_id
                ):
                    raise ServiceValidationError("extension_install_id does not match extension_id")
                payload["extension_install_id"] = normalized_install_id
            return payload

        normalized_install_id = self._normalize_text(extension_install_id)
        if not normalized_install_id:
            raise ServiceValidationError(
                "extension_install_id is required for extension_runtime connections"
            )

        install_store = self._extension_install_store(bl)
        install_record = (
            install_store.get_by_install_id(normalized_install_id)
            if hasattr(install_store, "get_by_install_id")
            else None
        )
        if not install_record:
            raise ServiceValidationError(
                "extension_install_id does not reference an installed extension"
            )
        install_visibility = self._normalize_text(getattr(install_record, "visibility", "")).lower()
        if (
            self._normalize_text(getattr(install_record, "account_id", ""))
            != self._normalize_text(account_id)
            and install_visibility != "global"
        ):
            raise ServiceValidationError("extension_install_id is not visible for this account")

        runtime_store = self._extension_runtime_store(bl)
        descriptor_modes: list[str] = []
        release_id = self._normalize_text(getattr(install_record, "release_id", ""))
        if release_id and hasattr(runtime_store, "get_release"):
            release = runtime_store.get_release(release_id)
            descriptor = dict(getattr(release, "descriptor", {}) or {}) if release else {}
            distribution = descriptor.get("distribution") if isinstance(descriptor, dict) else {}
            execution_modes = (
                distribution.get("execution_modes") if isinstance(distribution, dict) else []
            )
            if isinstance(execution_modes, list):
                descriptor_modes = [
                    self._normalize_text(mode).lower()
                    for mode in execution_modes
                    if self._normalize_text(mode)
                ]
        descriptor_modes = list(dict.fromkeys(descriptor_modes))
        if not descriptor_modes:
            raise ServiceValidationError(
                "extension runtime descriptor is not available for this install"
            )

        resolved_runtime_mode = (
            self._normalize_text(runtime_mode).lower()
            or self._normalize_text(getattr(install_record, "runtime_mode", "")).lower()
        )
        if not resolved_runtime_mode:
            if len(descriptor_modes) == 1:
                resolved_runtime_mode = descriptor_modes[0]
            else:
                raise ServiceValidationError(
                    "runtime_mode is required for this extension_runtime connection"
                )
        if resolved_runtime_mode not in descriptor_modes:
            raise ServiceValidationError(
                f"runtime_mode '{resolved_runtime_mode}' is not supported by descriptor execution_modes {descriptor_modes}"
            )

        resolved_remote_base_url = self._normalize_text(remote_base_url) or self._normalize_text(
            getattr(install_record, "remote_base_url", "")
        )
        if resolved_runtime_mode == "remote_https" and not resolved_remote_base_url:
            raise ServiceValidationError(
                "remote_base_url is required for remote_https extension_runtime connections"
            )
        if resolved_remote_base_url:
            # SSRF store-time guard: an extension_runtime connection's
            # remote_base_url later receives remote runtime bearer requests, so
            # reject private/loopback/link-local targets before persisting.
            from core.application.extensions.service_support import (
                validate_remote_url,
            )

            try:
                resolved_remote_base_url = validate_remote_url(
                    resolved_remote_base_url, purpose="remote_base_url"
                )
            except ValueError as exc:
                raise ServiceValidationError(f"remote_base_url is not allowed: {exc}") from exc

        payload.update(
            {
                "extension_id": None,
                "extension_install_id": normalized_install_id,
                "runtime_mode": resolved_runtime_mode,
                "remote_base_url": resolved_remote_base_url,
                "health_status": self._normalize_text(health_status)
                or self._normalize_text(getattr(install_record, "health_status", "")),
                "runtime_status": self._normalize_text(runtime_status)
                or self._normalize_text(getattr(install_record, "runtime_status", "")),
                "last_seen_ok_at": last_seen_ok_at,
                "last_error": self._normalize_text(last_error)
                or self._normalize_text(getattr(install_record, "last_error", "")),
            }
        )
        return payload

    def create_connection(
        self,
        project_id: Any,
        inp: ProjectConnectionCreateInput,
        *,
        scope: ScopeContext | None = None,
    ) -> ProjectConnectionView:
        from .command_models import ProjectConnectionCreateInput
        from .query_models import ProjectConnectionView

        inp = ProjectConnectionCreateInput.model_validate(inp)
        scope = _service_scope(scope, project_id=project_id)
        _require_role(scope, "operator")
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(ERR_DATABASE_NOT_CONFIGURED)
        project = self._require_project_in_scope(bl, str(project_id), scope)

        from core.application.management.project_connection import (
            ProjectConnection,
            _generate_connection_id,
        )

        normalized_account_id = (
            self._normalize_text(project.get("account_id") or scope.account_id or "acc_default")
            or "acc_default"
        )
        runtime_payload = self._project_connection_runtime_defaults(
            bl=bl,
            account_id=normalized_account_id,
            connection_kind=inp.connection_kind,
            extension_id=inp.extension_id,
            extension_install_id=inp.extension_install_id,
            runtime_mode=inp.runtime_mode,
            remote_base_url=inp.remote_base_url,
            health_status=inp.health_status,
            runtime_status=inp.runtime_status,
            last_seen_ok_at=inp.last_seen_ok_at,
            last_error=inp.last_error,
        )
        resolved_connection_type = (
            inp.connection_type
            or inp.connection_config.get("connection_type")
            or runtime_payload["extension_id"]
            or runtime_payload["extension_install_id"]
            or runtime_payload["connection_kind"]
        )
        connection_config = dict(inp.connection_config)
        platform_secret_ref_names = self._oauth_platform_secret_ref_names(
            bl=bl,
            extension_id=runtime_payload["extension_id"],
            connection_type=resolved_connection_type,
            account_id=normalized_account_id,
            connection_config=connection_config,
        )
        connection_id = _generate_connection_id()
        sanitized_secret_refs = self._sanitize_public_secret_refs(
            inp.secret_refs,
            connection_id=connection_id,
            platform_secret_ref_names=platform_secret_ref_names,
        )

        self._validate_connection_contract_against_extension_schemas(
            bl=bl,
            extension_id=runtime_payload["extension_id"],
            connection_type=resolved_connection_type,
            connection_config=inp.connection_config,
            secret_refs=sanitized_secret_refs,
        )

        conn = ProjectConnection(
            connection_id=connection_id,
            project_id=str(project_id),
            extension_id=runtime_payload["extension_id"],
            account_id=normalized_account_id,
            display_name=inp.display_name
            or inp.connection_config.get("display_name")
            or inp.connection_type
            or runtime_payload["extension_id"]
            or runtime_payload["extension_install_id"],
            connection_kind=runtime_payload["connection_kind"],
            connection_type=resolved_connection_type,
            environment=inp.environment,
            extension_install_id=runtime_payload["extension_install_id"],
            runtime_mode=runtime_payload["runtime_mode"],
            remote_base_url=runtime_payload["remote_base_url"],
            health_status=runtime_payload["health_status"],
            runtime_status=runtime_payload["runtime_status"],
            last_seen_ok_at=runtime_payload["last_seen_ok_at"],
            last_error=runtime_payload["last_error"],
            enabled=inp.enabled,
            connection_config=connection_config,
            secret_refs=sanitized_secret_refs,
            connection_routes=inp.connection_routes,
            ingress_config=inp.ingress_config,
            workflow_mappings=inp.workflow_mappings,
            tags=inp.tags,
        )

        try:
            if hasattr(bl, "create_project_connection"):
                bl.create_project_connection(conn.model_dump(mode="json"))
            else:
                self._insert_connection(bl, conn)
        except Exception as exc:
            raise ServiceError(
                f"Failed to persist project connection: {exc}",
                status_code=500,
            ) from exc

        return ProjectConnectionView(
            **self._normalize_connection_view_row(conn.model_dump(mode="json"))
        )

    def update_connection(
        self,
        connection_id: str,
        inp: ProjectConnectionUpdateInput,
        *,
        project_id: Any = 0,
        scope: ScopeContext | None = None,
    ) -> ProjectConnectionView:
        from .command_models import ProjectConnectionUpdateInput
        from .query_models import ProjectConnectionView

        inp = ProjectConnectionUpdateInput.model_validate(inp)
        scope = _service_scope(scope, project_id=project_id)
        _require_role(scope, "operator")
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(ERR_DATABASE_NOT_CONFIGURED)

        if not project_id:
            raise ServiceValidationError("project_id is required for update_connection")
        project = self._require_project_in_scope(bl, str(project_id), scope)

        existing = self._get_connection_row(bl, connection_id)
        if not existing:
            raise ServiceNotFoundError(f"Connection '{connection_id}' not found")
        # Always verify project ownership — prevents cross-project updates
        if str(existing.get("project_id")) != str(project_id):
            raise ServiceNotFoundError(
                f"Connection '{connection_id}' not found in project {project_id}"
            )
        try:
            require_project_access(
                scope,
                {
                    "id": str(existing.get("project_id") or ""),
                    "account_id": self._normalize_text(
                        existing.get("account_id") or project.get("account_id")
                    ),
                },
            )
        except ServiceForbiddenError:
            raise ServiceNotFoundError(
                f"Connection '{connection_id}' not found in project {project_id}"
            ) from None

        updates: dict[str, Any] = {}
        for field in (
            "enabled",
            "display_name",
            "connection_type",
            "environment",
            "extension_install_id",
            "runtime_mode",
            "remote_base_url",
            "health_status",
            "runtime_status",
            "last_seen_ok_at",
            "last_error",
            "connection_config",
            "secret_refs",
            "connection_routes",
            "ingress_config",
            "workflow_mappings",
            "tags",
        ):
            val = getattr(inp, field, None)
            if val is not None:
                updates[field] = val

        if self._normalize_text(existing.get("oauth_configuration_id")) and (
            "connection_config" in updates
        ):
            existing_config = (
                dict(existing.get("connection_config"))
                if isinstance(existing.get("connection_config"), dict)
                else {}
            )
            requested_config = (
                dict(updates.get("connection_config"))
                if isinstance(updates.get("connection_config"), dict)
                else {}
            )
            if isinstance(existing_config.get("oauth"), dict):
                requested_config["oauth"] = dict(existing_config["oauth"])
            if requested_config == existing_config:
                updates.pop("connection_config", None)
            else:
                updates["connection_config"] = requested_config

        if (
            "connection_config" in updates
            or "secret_refs" in updates
            or "connection_type" in updates
        ):
            try:
                current_revision = int(existing.get("credential_revision") or 1)
            except (TypeError, ValueError):
                current_revision = 1
            updates["credential_revision"] = max(1, current_revision) + 1

        if (
            isinstance(updates.get("connection_config"), dict)
            and "oauth_auth" in updates["connection_config"]
        ):
            raise ServiceValidationError(
                "connection_config.oauth_auth is platform-owned and must be declared by the extension contract"
            )

        if "secret_refs" in updates:
            platform_secret_ref_names = self._oauth_platform_secret_ref_names(
                bl=bl,
                extension_id=updates.get("extension_id", existing.get("extension_id")),
                connection_type=self._normalize_text(
                    updates.get("connection_type", existing.get("connection_type"))
                ),
                account_id=self._normalize_text(
                    existing.get("account_id") or project.get("account_id")
                ),
                connection_config=updates.get(
                    "connection_config", existing.get("connection_config")
                ),
            )
            sanitized_secret_refs = self._sanitize_public_secret_refs(
                updates["secret_refs"],
                connection_id=connection_id,
                existing_secret_refs=existing.get("secret_refs"),
                platform_secret_ref_names=platform_secret_ref_names,
            )
            updates["secret_refs"] = self._merge_existing_internal_secret_refs(
                secret_refs=sanitized_secret_refs,
                existing_secret_refs=existing.get("secret_refs"),
                platform_secret_ref_names=platform_secret_ref_names,
            )

        effective_kind = (
            self._normalize_text(existing.get("connection_kind")).lower() or "extension"
        )
        if effective_kind == "extension" and "extension_install_id" in updates:
            requested_install_id = self._normalize_text(updates["extension_install_id"])
            existing_install_id = self._normalize_text(existing.get("extension_install_id"))
            if existing_install_id and requested_install_id != existing_install_id:
                raise ServiceValidationError(
                    "extension_install_id cannot be changed for an existing connection"
                )
            validated_binding = self._project_connection_runtime_defaults(
                bl=bl,
                account_id=self._normalize_text(
                    project.get("account_id") or scope.account_id or "acc_default"
                ),
                connection_kind=effective_kind,
                extension_id=existing.get("extension_id"),
                extension_install_id=requested_install_id,
                runtime_mode="",
                remote_base_url="",
                health_status="",
                runtime_status="",
                last_seen_ok_at=None,
                last_error="",
            )
            updates["extension_install_id"] = validated_binding["extension_install_id"]
        if effective_kind == "extension_runtime":
            runtime_payload = self._project_connection_runtime_defaults(
                bl=bl,
                account_id=self._normalize_text(
                    project.get("account_id") or scope.account_id or "acc_default"
                ),
                connection_kind=effective_kind,
                extension_id=existing.get("extension_id"),
                extension_install_id=self._normalize_text(
                    updates.get("extension_install_id", existing.get("extension_install_id"))
                ),
                runtime_mode=self._normalize_text(
                    updates.get("runtime_mode", existing.get("runtime_mode"))
                ),
                remote_base_url=self._normalize_text(
                    updates.get("remote_base_url", existing.get("remote_base_url"))
                ),
                health_status=self._normalize_text(
                    updates.get("health_status", existing.get("health_status"))
                ),
                runtime_status=self._normalize_text(
                    updates.get("runtime_status", existing.get("runtime_status"))
                ),
                last_seen_ok_at=updates.get("last_seen_ok_at", existing.get("last_seen_ok_at")),
                last_error=self._normalize_text(
                    updates.get("last_error", existing.get("last_error"))
                ),
            )
            updates["extension_install_id"] = runtime_payload["extension_install_id"]
            updates["runtime_mode"] = runtime_payload["runtime_mode"]
            updates["remote_base_url"] = runtime_payload["remote_base_url"]
            updates["health_status"] = runtime_payload["health_status"]
            updates["runtime_status"] = runtime_payload["runtime_status"]
            updates["last_seen_ok_at"] = runtime_payload["last_seen_ok_at"]
            updates["last_error"] = runtime_payload["last_error"]
            updates["extension_id"] = None

        if (
            "connection_config" in updates
            or "secret_refs" in updates
            or "connection_type" in updates
        ):
            self._validate_connection_contract_against_extension_schemas(
                bl=bl,
                extension_id=updates.get("extension_id", existing.get("extension_id")),
                connection_type=self._normalize_text(
                    updates.get("connection_type", existing.get("connection_type"))
                ),
                connection_config=updates.get(
                    "connection_config", existing.get("connection_config")
                ),
                secret_refs=updates.get("secret_refs", existing.get("secret_refs")),
            )

        should_invalidate_sessions = "credential_revision" in updates
        if updates:
            self._update_connection_row(bl, connection_id, updates)
            if should_invalidate_sessions:
                self._invalidate_connection_sessions(bl, connection_id)

        updated = self._get_connection_row(bl, connection_id) or existing
        return ProjectConnectionView(**self._normalize_connection_view_row(updated))

    def save_connection_secrets(
        self,
        connection_id: str,
        secrets: dict[str, Any],
        *,
        project_id: Any = 0,
        scope: ScopeContext | None = None,
    ) -> ProjectConnectionView:
        from .query_models import ProjectConnectionView

        scope = _service_scope(scope, project_id=project_id)
        _require_role(scope, "operator")
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(ERR_DATABASE_NOT_CONFIGURED)
        if not project_id:
            raise ServiceValidationError("project_id is required for save_connection_secrets")
        project = self._require_project_in_scope(bl, str(project_id), scope)
        existing = self._get_connection_row(bl, connection_id)
        if not existing or str(existing.get("project_id")) != str(project_id):
            raise ServiceNotFoundError(
                f"Connection '{connection_id}' not found in project {project_id}"
            )
        require_project_access(
            scope,
            {
                "id": str(existing.get("project_id") or ""),
                "account_id": self._normalize_text(
                    existing.get("account_id") or project.get("account_id")
                ),
            },
        )

        from core.application.management import project_connection_secrets

        platform_secret_ref_names = self._oauth_platform_secret_ref_names(
            bl=bl,
            extension_id=existing.get("extension_id"),
            connection_type=self._normalize_text(existing.get("connection_type")),
            account_id=self._normalize_text(
                existing.get("account_id") or project.get("account_id")
            ),
            connection_config=existing.get("connection_config"),
        )
        sanitized_secrets = self._sanitize_public_secret_values(
            dict(secrets or {}),
            platform_secret_ref_names=platform_secret_ref_names,
        )
        candidate_secret_refs = self._normalize_secret_refs(existing.get("secret_refs"))
        candidate_refs = dict(candidate_secret_refs.get("refs") or {})
        for secret_name in sanitized_secrets:
            candidate_refs[secret_name] = candidate_refs.get(secret_name) or secret_name
        candidate_secret_refs["refs"] = candidate_refs
        self._validate_connection_contract_against_extension_schemas(
            bl=bl,
            extension_id=existing.get("extension_id"),
            connection_type=self._normalize_text(existing.get("connection_type")),
            connection_config=existing.get("connection_config"),
            secret_refs=candidate_secret_refs,
        )

        saved_refs = project_connection_secrets.save_connection_secrets(
            bl,
            account_id=self._normalize_text(existing.get("account_id")),
            connection_id=self._normalize_text(existing.get("connection_id")),
            secrets=sanitized_secrets,
        )
        merged_secret_refs = self._normalize_secret_refs(existing.get("secret_refs"))
        refs = dict(merged_secret_refs.get("refs") or {})
        refs.update(dict(saved_refs.get("refs") or {}))
        merged_secret_refs["refs"] = refs
        try:
            current_revision = int(existing.get("credential_revision") or 1)
        except (TypeError, ValueError):
            current_revision = 1
        self._update_connection_row(
            bl,
            connection_id,
            {
                "secret_refs": merged_secret_refs,
                "credential_revision": max(1, current_revision) + 1,
            },
        )
        self._invalidate_connection_sessions(bl, connection_id)
        updated = self._get_connection_row(bl, connection_id) or existing
        return ProjectConnectionView(**self._normalize_connection_view_row(updated))

    def delete_connection(
        self,
        connection_id: str,
        *,
        project_id: Any = 0,
        scope: ScopeContext | None = None,
    ) -> dict[str, Any]:
        scope = _service_scope(scope, project_id=project_id)
        _require_role(scope, "operator")
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(ERR_DATABASE_NOT_CONFIGURED)

        if not project_id:
            raise ServiceValidationError("project_id is required for delete_connection")
        project = self._require_project_in_scope(bl, str(project_id), scope)

        existing = self._get_connection_row(bl, connection_id)
        if not existing:
            raise ServiceNotFoundError(f"Connection '{connection_id}' not found")
        # Always verify project ownership — prevents cross-project deletes
        if str(existing.get("project_id")) != str(project_id):
            raise ServiceNotFoundError(
                f"Connection '{connection_id}' not found in project {project_id}"
            )
        try:
            require_project_access(
                scope,
                {
                    "id": str(existing.get("project_id") or ""),
                    "account_id": self._normalize_text(
                        existing.get("account_id") or project.get("account_id")
                    ),
                },
            )
        except ServiceForbiddenError:
            raise ServiceNotFoundError(
                f"Connection '{connection_id}' not found in project {project_id}"
            ) from None

        configuration_id = self._normalize_text(existing.get("oauth_configuration_id"))
        if configuration_id:
            from core.application.extensions.oauth_connection_lifecycle import (
                build_database_oauth_connection_lifecycle_service,
            )

            build_database_oauth_connection_lifecycle_service(bl).disconnect(
                scope,
                self._normalize_text(existing.get("extension_install_id")),
                self._normalize_text(existing.get("oauth_provider_id")),
                configuration_id,
                connection_id,
            )
        cleanup_project_connection_owned_data(bl, connection=existing)
        return {"status": "deleted", "connection_id": connection_id}

    def list_connections(
        self,
        project_id: Any,
        *,
        scope: ScopeContext | None = None,
    ) -> list[ProjectConnectionView]:
        from .query_models import ProjectConnectionView

        scope = _service_scope(scope, project_id=project_id)
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(ERR_DATABASE_NOT_CONFIGURED)
        self._require_project_in_scope(bl, str(project_id), scope)

        rows = self._list_connection_rows(bl, project_id)
        return [ProjectConnectionView(**self._normalize_connection_view_row(row)) for row in rows]

    def get_connection(
        self,
        connection_id: str,
        *,
        project_id: Any = 0,
        scope: ScopeContext | None = None,
    ) -> ProjectConnectionView:
        from .query_models import ProjectConnectionView

        scope = _service_scope(scope, project_id=project_id)
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(ERR_DATABASE_NOT_CONFIGURED)

        if not project_id:
            raise ServiceValidationError("project_id is required for get_connection")
        project = self._require_project_in_scope(bl, str(project_id), scope)

        row = self._get_connection_row(bl, connection_id)
        if not row:
            raise ServiceNotFoundError(f"Connection '{connection_id}' not found")
        # Always verify project ownership — prevents cross-project reads
        if str(row.get("project_id")) != str(project_id):
            raise ServiceNotFoundError(
                f"Connection '{connection_id}' not found in project {project_id}"
            )
        try:
            require_project_access(
                scope,
                {
                    "id": str(row.get("project_id") or ""),
                    "account_id": self._normalize_text(
                        row.get("account_id") or project.get("account_id")
                    ),
                },
            )
        except ServiceForbiddenError:
            raise ServiceNotFoundError(
                f"Connection '{connection_id}' not found in project {project_id}"
            ) from None
        return ProjectConnectionView(**self._normalize_connection_view_row(row))

    # --- DB helpers ---

    def _insert_connection(self, bl: Any, conn: Any) -> None:
        if not hasattr(bl, "cursor"):
            return
        try:
            project_connection_store.insert_project_connection(bl, conn)
        except Exception as exc:
            logger.warning("Failed to insert project_connection: %s", exc)
            raise  # propagate to caller for proper error handling

    def _get_connection_row(self, bl: Any, connection_id: str) -> dict[str, Any] | None:
        if not hasattr(bl, "cursor"):
            return None
        try:
            return project_connection_store.get_project_connection_row(bl, connection_id)
        except Exception as exc:
            logger.warning("Failed to read project_connection '%s': %s", connection_id, exc)
            raise  # propagate — table may not exist

    def _list_connection_rows(self, bl: Any, project_id: Any) -> list[dict[str, Any]]:
        if not hasattr(bl, "cursor"):
            return []
        try:
            return project_connection_store.list_project_connection_rows(bl, project_id)
        except Exception as exc:
            logger.warning("Failed to list project_connections for project %s: %s", project_id, exc)
            raise  # propagate — table may not exist

    def _update_connection_row(self, bl: Any, connection_id: str, updates: dict[str, Any]) -> None:
        if not hasattr(bl, "cursor"):
            return
        try:
            project_connection_store.update_project_connection_row(bl, connection_id, updates)
        except Exception as exc:
            logger.warning("Failed to update project_connection: %s", exc)
            raise

    def _delete_connection_row(self, bl: Any, connection_id: str) -> None:
        if not hasattr(bl, "cursor"):
            return
        try:
            project_connection_store.delete_project_connection_row(bl, connection_id)
        except Exception as exc:
            logger.warning("Failed to delete project_connection: %s", exc)
            raise

    def _delete_connection_secret_refs(
        self,
        bl: Any,
        *,
        account_id: str,
        connection_id: str,
    ) -> None:
        if not hasattr(bl, "cursor"):
            return
        try:
            from core.application.management.project_connection_secrets import (
                delete_connection_secret_refs,
            )

            delete_connection_secret_refs(
                bl,
                account_id=account_id,
                connection_id=connection_id,
            )
        except Exception:
            logger.warning("Failed to remove protected project connection references")
            raise

    def _invalidate_connection_sessions(self, bl: Any, connection_id: str) -> None:
        if not hasattr(bl, "cursor"):
            return
        try:
            from core.application.management.project_connection_sessions import (
                invalidate_connection_sessions,
            )

            invalidate_connection_sessions(bl, connection_id=connection_id)
        except Exception as exc:
            logger.warning("Failed to invalidate project connection sessions: %s", exc)

    def _get_connection_row_for_extension_install(
        self,
        bl: Any,
        *,
        project_id: Any,
        extension_install_id: str,
    ) -> dict[str, Any] | None:
        if not hasattr(bl, "cursor"):
            return None
        return project_connection_store.get_project_connection_row_for_extension_install(
            bl,
            project_id=project_id,
            extension_install_id=extension_install_id,
        )


__all__ = [name for name in globals() if not name.startswith("__")]
