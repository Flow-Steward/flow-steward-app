"""Typed public ports and shared policy for extension lifecycle orchestration."""

from __future__ import annotations

from typing import Any, Protocol

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.auth.principal import principal_type_has_minimum_role
from core.application.extensions.json_schema_subset import (
    validate_json_schema_value as _validate_json_schema_value,
)
from core.application.management.command_models_extensions import (
    ExtensionDescriptorImportFileInput,
    ExtensionDescriptorValidateInput,
    ExtensionDescriptorValidateResultView,
    ExtensionInstallResultView,
    ExtensionReleaseImportResultView,
    ExtensionReleaseInstallInput,
    ExtensionToggleResultView,
    ExtensionUninstallResultView,
)
from core.application.management.errors import (
    ServiceForbiddenError,
    ServiceValidationError,
)
from core.application.management.guards import require_project_access as _require_project_access
from core.application.management.query_models import ExtensionDetailView
from core.application.management.scope import ScopeContext, normalize_scope

from .browser_authority_approval import PreparedBrowserAuthorityApproval

ERR_DATABASE_NOT_CONFIGURED = "Database not configured"
ERR_EXTENSION_NOT_FOUND = "Extension not found"


def service_scope(scope: ScopeContext | None) -> ScopeContext:
    return normalize_scope(scope)


def require_role(scope: ScopeContext, minimum_role: str) -> None:
    """Enforce the public principal-type hierarchy for extension operations."""
    principal_type = str(scope.principal_type or "").strip().lower()
    required = str(minimum_role or "").strip().lower()
    if required not in {"viewer", "operator", "admin"}:
        raise ServiceForbiddenError(f"Unknown role requirement: {minimum_role}")
    if not principal_type:
        raise ServiceForbiddenError("Authentication required")
    if principal_type_has_minimum_role(principal_type, required):
        return
    raise ServiceForbiddenError(f"{required.title()} role required")


def require_global_extension_lifecycle_scope(scope: ScopeContext) -> None:
    require_role(scope, "admin")
    account_id = str(scope.account_id or DEFAULT_ACCOUNT_ID).strip() or DEFAULT_ACCOUNT_ID
    if account_id != DEFAULT_ACCOUNT_ID:
        raise ServiceValidationError(
            "Extension install/uninstall is deployment-scoped and is only supported "
            "for the default account"
        )


def require_project_access(scope: ScopeContext, project: dict[str, Any]) -> None:
    """Apply the public project access policy at an extension boundary."""
    _require_project_access(scope, project)


def _json_schema_type_matches(expected_type: str, value: Any) -> bool:
    normalized_type = str(expected_type or "").strip().lower()
    if normalized_type == "object":
        return isinstance(value, dict)
    if normalized_type == "array":
        return isinstance(value, list)
    if normalized_type == "string":
        return isinstance(value, str)
    if normalized_type == "boolean":
        return isinstance(value, bool)
    if normalized_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if normalized_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if normalized_type == "null":
        return value is None
    return True


def validate_json_schema_value(
    *,
    schema: dict[str, Any],
    value: Any,
    path: str,
    errors: list[str],
) -> None:
    """Validate the JSON Schema subset the platform executes.

    Kept as a re-export so existing importers do not move. The implementation
    lives in one place — there used to be two near-identical copies here and in
    command_services_common, and they had already drifted.
    """
    _validate_json_schema_value(schema=schema, value=value, path=path, errors=errors)


class ExtensionCommandServicePort(Protocol):
    def validate_descriptor(
        self,
        inp: ExtensionDescriptorValidateInput,
        *,
        scope: ScopeContext | None = None,
    ) -> ExtensionDescriptorValidateResultView: ...

    def import_descriptor_file(
        self,
        inp: ExtensionDescriptorImportFileInput,
        *,
        scope: ScopeContext | None = None,
        source_kind_override: str = "",
        allow_same_version_enrichment: bool = False,
        _browser_authority_handoff: PreparedBrowserAuthorityApproval | None = None,
    ) -> ExtensionReleaseImportResultView: ...

    def uninstall_extension(
        self,
        extension_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> ExtensionUninstallResultView: ...

    def install_release(
        self,
        inp: ExtensionReleaseInstallInput,
        *,
        scope: ScopeContext | None = None,
        allow_same_version_enrichment: bool = False,
        _browser_authority_handoff: PreparedBrowserAuthorityApproval | None = None,
    ) -> ExtensionInstallResultView: ...

    def enable_extension(
        self,
        extension_id: str,
        *,
        scope: ScopeContext | None = None,
        approved_browser_authority_impact_hash: str = "",
    ) -> ExtensionToggleResultView: ...

    def authorize_enable_extension(
        self,
        extension_id: str,
        *,
        scope: ScopeContext | None = None,
        approved_browser_authority_impact_hash: str = "",
    ) -> None: ...

    def disable_extension(
        self,
        extension_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> ExtensionToggleResultView: ...

    def install_record_payload(self, existing: Any | None) -> dict[str, Any] | None: ...

    def rollback_install_release_failure(
        self,
        *,
        install_store: Any,
        runtime_store: Any,
        extension_id: str,
        account_id: str,
        failed_install_id: str,
        previous: dict[str, Any] | None,
        previous_release: Any | None = None,
        previous_entitlement: Any | None = None,
        previous_projections: list[Any] | None = None,
    ) -> None: ...

    def cleanup_release_if_unreferenced(
        self,
        *,
        install_store: Any,
        release_id: str,
        extension_id: str,
    ) -> None: ...

    def cleanup_release_artifacts(self, release_id: str) -> None: ...

    def descriptor_validation_or_error(
        self, result: ExtensionDescriptorValidateResultView
    ) -> dict[str, Any]: ...

    def validate_release_dependencies_for_install(
        self,
        *,
        release: Any,
        account_id: str,
        runtime_store: Any,
        install_store: Any,
    ) -> list[dict[str, Any]]: ...


class ExtensionQueryServicePort(Protocol):
    def get_extension(
        self,
        extension_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> ExtensionDetailView: ...


__all__ = [
    "ERR_DATABASE_NOT_CONFIGURED",
    "ERR_EXTENSION_NOT_FOUND",
    "ExtensionCommandServicePort",
    "ExtensionQueryServicePort",
    "require_global_extension_lifecycle_scope",
    "require_project_access",
    "require_role",
    "service_scope",
    "validate_json_schema_value",
]
