# ruff: noqa: F403, F405
# mypy: ignore-errors
"""Read side and storage helpers of the project connection service."""

from __future__ import annotations

from .command_services_common import *  # NOSONAR


class ProjectConnectionServiceReadsMixin:
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
            "credentials_configuration_id",
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
        return [ProjectConnectionView(**self._with_sender_flags(bl, scope, row)) for row in rows]

    def _credentials_link(
        self,
        bl: Any,
        *,
        extension_id: Any,
        connection_type: str,
        configuration_id: str | None,
        account_id: str,
        project_id: str,
        extension_install_id: str,
        required: bool,
    ) -> str:
        """The OAuth configuration a credentials_from connection borrows from.

        A type that declares ``credentials_from`` needs one on creation; any
        other type may not have one.
        """
        from core.application.extensions.connection_credentials import (
            credentials_from_spec,
            validate_link,
        )

        normalized_extension_id = self._normalize_text(extension_id)
        requested = self._normalize_text(configuration_id)
        extension = (
            self._extension_runtime_row(bl=bl, extension_id=normalized_extension_id)
            if normalized_extension_id
            else {}
        )
        spec = credentials_from_spec(extension, connection_type) if extension else None
        if not requested:
            if spec is not None and required:
                raise ServiceValidationError(
                    "Choose the OAuth configuration this connection takes its credentials from",
                    payload={
                        "error": "credentials_configuration_required",
                        "code": "credentials_configuration_required",
                        "oauth_provider_id": spec.provider_id,
                    },
                )
            return ""
        return validate_link(
            bl,
            extension=extension,
            connection_type=connection_type,
            configuration_id=requested,
            account_id=account_id,
            project_id=project_id,
            extension_install_id=extension_install_id,
            extension_id=normalized_extension_id,
        )

    def _with_sender_flags(
        self, bl: Any, scope: ScopeContext, row: dict[str, Any] | None
    ) -> dict[str, Any]:
        from core.application.extensions.system_email_sender import (
            is_platform_admin,
            is_system_sender,
        )

        payload = self._normalize_connection_view_row(row)
        sender = is_system_sender(bl, self._normalize_text(payload.get("connection_id")))
        payload["system_email_sender"] = sender
        payload["system_email_sender_locked"] = sender and not is_platform_admin(scope)
        if self._normalize_text(payload.get("credentials_configuration_id")):
            from core.application.extensions.connection_credentials import credentials_status

            status = credentials_status(bl, dict(row or {}))
            payload["credentials_status"] = status
            if status:
                # Not ready until its OAuth configuration exists and is complete.
                payload["runtime_status"] = "not_ready"
                payload["last_error"] = status
        return payload

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
        return ProjectConnectionView(**self._with_sender_flags(bl, scope, row))

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
