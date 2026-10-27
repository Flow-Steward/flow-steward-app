# ruff: noqa: F403, F405, RUF022
"""Workflow admin route/helper module extracted from workflows."""

from __future__ import annotations

from pydantic import ConfigDict

from core.entrypoints.admin.workflows_common import *  # NOSONAR
from core.entrypoints.admin.workflows_scope import *  # NOSONAR


def _resolve_extension_contract(install_id: str, *, account_id: str = "") -> dict | None:
    db = _get_db()
    dsn = str(getattr(db, "dsn", "") or "").strip()
    if not dsn:
        return None
    try:
        from core.application.extensions.workflow_install_contract import (
            resolve_workflow_extension_install_contract,
        )
        from core.infrastructure.runtime.entrypoint_db_adapters import create_identity_store

        logger_store = create_identity_store(dsn)
        return resolve_workflow_extension_install_contract(
            logger_store.conn,
            install_id=install_id,
            account_id=account_id,
            default_account_id=DEFAULT_ACCOUNT_ID,
        )
    except Exception as exc:
        logger.warning("extension contract lookup failed for install_id=%s: %s", install_id, exc)
        return None


@router.get(
    "/meta/extension-form-schema",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def get_extension_step_form_schema(
    request: Request,
    install_id: Annotated[str, Query(description="Extension install ID")],
    operation_id: Annotated[str, Query(description="Operation ID from the connector manifest")],
    locale: Annotated[str, Query(description="Locale for translated labels")] = "",
):
    """Return rendered Flow Steward-owned form schema for a connector step.

    The form schema comes from the extension's step_ui_manifest.
    Flow Steward owns all rendering — extensions contribute only schema, never code.
    """
    scope = _request_scope(request)
    contract = _resolve_extension_contract(
        install_id, account_id=str(getattr(scope, "account_id", "") or "")
    )
    if contract is None:
        raise HTTPException(
            status_code=404,
            detail=f"Extension contract not found for install '{install_id}'. "
            "Extension contract storage may not be configured yet.",
        )
    _enforce_extension_contract_scope(scope, contract)
    from core.application.extensions.extension_projection_v2 import render_step_form
    from core.application.settings.ui_i18n import resolve_ui_locale

    # resolve_ui_locale reads ?locale= first, then the user's profile language.
    _ = locale
    result = render_step_form(
        contract=contract,
        operation_id=operation_id,
        locale=resolve_ui_locale(request),
        locale_bundles=contract.get("locale_bundles"),
    )
    if result.get("errors"):
        logger.warning(
            "Extension form schema errors for install=%s op=%s: %s",
            install_id,
            operation_id,
            result["errors"],
        )
    return result


class ResourceQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    install_id: str
    query_id: str
    account_id: str = ""
    project_id: str = ""
    connection_id: str = ""
    search: str = ""
    cursor: str = ""
    limit: int = 50

    @field_validator("project_id", mode="before")
    @classmethod
    def _normalize_project_id(cls, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (bool, list, tuple, set, dict)):
            raise ValueError("project_id must be a scalar identifier")
        return str(value).strip()


@router.post(
    "/meta/extension-resource-query",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def execute_extension_resource_query(body: ResourceQueryRequest, request: Request):
    """Execute a resource query from an extension's resource_query_manifest.

    Resource queries power picker fields in connector step forms.
    The actual data fetching is delegated to the extension's runtime provider.
    """
    scope = _request_scope(request)
    contract = _resolve_extension_contract(
        body.install_id,
        account_id=str(getattr(scope, "account_id", "") or ""),
    )
    if contract is None:
        raise HTTPException(
            status_code=404,
            detail=f"Extension contract not found for install '{body.install_id}'. "
            "Extension contract storage may not be configured yet.",
        )
    _enforce_extension_contract_scope(scope, contract)

    from core.application.extensions.extension_projection_v2 import execute_resource_query
    from core.infrastructure.runtime.entrypoint_db_adapters import create_identity_store

    normalized_project_id = ""
    normalized_connection_id = ""
    if body.project_id or body.connection_id:
        db = _get_db()
        normalized_project_id = _enforce_project_scope(db, scope, body.project_id)
        normalized_connection_id = _enforce_project_connection_scope(
            db,
            project_id=normalized_project_id,
            connection_id=body.connection_id,
        )

    def _get_logger_only():
        db = _get_db()
        dsn = str(getattr(db, "dsn", "") or "").strip()
        return create_identity_store(dsn) if dsn else None

    runtime_container = getattr(request.app.state, "runtime_container", None)
    runtime_registries = getattr(runtime_container, "registries", None)
    provider_factory = (
        runtime_registries.get("extension_query_provider_factory")
        if isinstance(runtime_registries, dict)
        else None
    )
    if not callable(provider_factory):
        raise HTTPException(
            status_code=503,
            detail="Extension runtime provider registry is not available.",
        )
    provider = provider_factory(
        body.install_id,
        get_logger_only=_get_logger_only,
    )
    if provider is None:
        raise HTTPException(
            status_code=503,
            detail=f"Extension runtime provider not available for install '{body.install_id}'.",
        )
    return execute_resource_query(
        contract=contract,
        query_id=body.query_id,
        provider=provider,
        account_id=_normalize_text(getattr(scope, "account_id", "")),
        project_id=normalized_project_id,
        connection_id=normalized_connection_id,
        search=body.search,
        cursor=body.cursor,
        limit=body.limit,
    )


__all__ = [
    "_resolve_extension_contract",
    "get_extension_step_form_schema",
    "ResourceQueryRequest",
    "execute_extension_resource_query",
]
