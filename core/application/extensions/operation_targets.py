from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.application.management.errors import ServiceNotFoundError, ServiceValidationError
from core.application.management.scope import ScopeContext
from core.infrastructure.db import project_connection_store
from core.infrastructure.db.db_pool import transaction_connection

from .lifecycle_ports import require_project_access

_RUNTIME_OAUTH_CONFIG_FIELDS = frozenset(
    {
        "authorization_intent_id",
        "last_error_code",
        "lifecycle_status",
        "safe_metadata",
        "subject",
        "token_type",
        "verified_display_name",
        "verified_email",
    }
)


def _runtime_connection_config(connection: dict[str, Any]) -> dict[str, Any]:
    """Project only closed, non-secret connection metadata to an extension action."""
    raw = connection.get("connection_config")
    config = dict(raw) if isinstance(raw, dict) else {}
    raw_oauth = config.get("oauth")
    oauth = dict(raw_oauth) if isinstance(raw_oauth, dict) else {}
    return {
        **{key: value for key, value in config.items() if key != "oauth"},
        "oauth": {
            key: value for key, value in oauth.items() if key in _RUNTIME_OAUTH_CONFIG_FIELDS
        },
    }


def _browser_oauth_configuration(bl: Any, *, connection: dict[str, Any]) -> dict[str, str]:
    """Project only manifest-declared, non-secret OAuth configuration to a browser action."""
    from core.application.extensions.oauth_repository import ExtensionOAuthRepository

    conn = getattr(bl, "conn", None)
    install_loader = getattr(bl, "get_extension_install_for_operator", None)
    project_loader = getattr(bl, "get_project", None)
    if conn is None or not callable(install_loader) or not callable(project_loader):
        return {}
    project = project_loader(str(connection.get("project_id") or "").strip())
    if not isinstance(project, dict):
        return {}
    tenant_id = str(project.get("tenant_id") or "").strip()
    account_id = str(connection.get("account_id") or "").strip()
    install_id = str(connection.get("extension_install_id") or "").strip()
    provider_id = str(connection.get("oauth_provider_id") or "").strip()
    configuration_id = str(connection.get("oauth_configuration_id") or "").strip()
    if not all((tenant_id, account_id, install_id, provider_id, configuration_id)):
        return {}
    install = install_loader(install_id, account_id=account_id)
    install = dict(install) if isinstance(install, dict) else {}
    extension_id = str(connection.get("extension_id") or "").strip()
    release_id = str(install.get("release_id") or "").strip()
    if not release_id or str(install.get("extension_id") or "").strip() != extension_id:
        return {}
    manifest = dict(install.get("compiled_manifest") or {})
    contract = dict(manifest.get("extension_contract_v2_normalized") or {})
    if not contract:
        contract = dict(
            dict(manifest.get("runtime") or {}).get("extension_contract_v2_normalized") or {}
        )
    provider = next(
        (
            row
            for row in contract.get("oauth_providers") or []
            if isinstance(row, dict) and str(row.get("provider_id") or "").strip() == provider_id
        ),
        {},
    )
    allowed = {
        str(field.get("field_id") or "").strip()
        for field in provider.get("credential_fields") or []
        if isinstance(field, dict)
        and field.get("browser_exposable") is True
        and field.get("secret") is False
    }
    if not allowed:
        return {}
    configuration = ExtensionOAuthRepository(conn).get_configuration_for_scope(
        tenant_id=tenant_id,
        account_id=account_id,
        project_id=str(connection.get("project_id") or "").strip(),
        extension_install_id=install_id,
        extension_id=str(connection.get("extension_id") or "").strip(),
        provider_id=provider_id,
        configuration_id=configuration_id,
    )
    configuration = dict(configuration) if isinstance(configuration, dict) else {}
    expected_scope = {
        "tenant_id": tenant_id,
        "account_id": account_id,
        "project_id": str(connection.get("project_id") or "").strip(),
        "extension_install_id": install_id,
        "extension_id": extension_id,
        "extension_release_id": release_id,
        "provider_id": provider_id,
        "configuration_id": configuration_id,
    }
    if not all(
        str(configuration.get(key) or "").strip() == expected
        for key, expected in expected_scope.items()
    ):
        return {}
    values = (
        dict(configuration.get("non_secret_config") or {})
        if isinstance(configuration.get("non_secret_config"), dict)
        else {}
    )
    return {
        field_id: value
        for field_id, value in values.items()
        if field_id in allowed and isinstance(value, str)
    }


def _resolve_extension_owned_oauth_runtime(
    bl: Any,
    *,
    connection: dict[str, Any],
    get_project_by_id: Callable[[str], Any] | None = None,
) -> dict[str, str]:
    from contextlib import contextmanager

    from core.application.auth.secrets import decrypt_secret
    from core.application.extensions.oauth_repository import ExtensionOAuthRepository
    from core.application.extensions.oauth_runtime import OAuthRuntimeTokenService
    from core.infrastructure.db import model_provider_store, psycopg_runtime
    from core.infrastructure.extension_runtime.oauth_token_store import (
        DatabaseOAuthCallbackTokenStore,
    )
    from core.infrastructure.extension_runtime.oauth_transport import refresh_token

    conn = getattr(bl, "conn", None)
    install_loader = getattr(bl, "get_extension_install_for_operator", None)
    project_loader = get_project_by_id or getattr(bl, "get_project", None)
    if conn is None or not callable(install_loader) or not callable(project_loader):
        raise RuntimeError("extension OAuth runtime dependencies unavailable")
    project_id = str(connection.get("project_id") or "").strip()
    project = project_loader(project_id)
    if not isinstance(project, dict):
        raise RuntimeError("extension OAuth project scope unavailable")
    tenant_id = str(project.get("tenant_id") or "").strip()
    if not tenant_id:
        raise RuntimeError("extension OAuth tenant scope unavailable")

    def configuration_secret_value(*, account_id: str, ref_key: str) -> str:
        ciphertext = model_provider_store.get_secret_ref_ciphertext(
            conn,
            account_id=account_id,
            ref_key=ref_key,
        )
        return decrypt_secret(ciphertext) if ciphertext else ""

    @contextmanager
    def refresh_claim_connection():
        dsn = str(getattr(bl, "dsn", "") or "").strip()
        if not dsn:
            raise RuntimeError("extension OAuth refresh claim database unavailable")
        claim_conn = psycopg_runtime.connect(dsn)
        try:
            claim_conn.autocommit = True
            yield claim_conn
        finally:
            claim_conn.close()

    service = OAuthRuntimeTokenService(
        repository=ExtensionOAuthRepository(
            conn,
            refresh_claim_connection_factory=refresh_claim_connection,
        ),
        token_store=DatabaseOAuthCallbackTokenStore(conn),
        install_loader=lambda install_id, account_id: install_loader(
            install_id, account_id=account_id
        ),
        transaction_factory=lambda: transaction_connection(conn),
        configuration_secret_value=configuration_secret_value,
        refresh_access_token=refresh_token,
    )
    return service.resolve_runtime_access_token(
        tenant_id=tenant_id,
        account_id=str(connection.get("account_id") or "").strip(),
        project_id=project_id,
        extension_install_id=str(connection.get("extension_install_id") or "").strip(),
        extension_id=str(connection.get("extension_id") or "").strip(),
        provider_id=str(connection.get("oauth_provider_id") or "").strip(),
        configuration_id=str(connection.get("oauth_configuration_id") or "").strip(),
        connection_id=str(connection.get("connection_id") or "").strip(),
        connection_type=str(connection.get("connection_type") or "").strip(),
    )


def _direct_action_project_id(
    *,
    scope: ScopeContext | None,
    context: dict[str, Any],
    target: dict[str, Any],
) -> str:
    return (
        str(getattr(scope, "project_id", "") or "").strip()
        or str((context or {}).get("project_id") or "").strip()
        or str((target or {}).get("project_id") or "").strip()
    )


def _require_direct_action_project_access(
    bl: Any,
    project_id: str,
    scope: ScopeContext,
    *,
    get_project_by_id: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    project_getter = get_project_by_id or getattr(bl, "get_project", None)
    if not callable(project_getter):
        raise ServiceNotFoundError(f"Project '{project_id}' not found")
    project = project_getter(project_id)
    if not isinstance(project, dict) or not project:
        raise ServiceNotFoundError(f"Project '{project_id}' not found")
    require_project_access(scope, project)
    return project


def _target_connection_id_for_external_effect(target: dict[str, Any]) -> str:
    connection = target.get("connection")
    connection = connection if isinstance(connection, dict) else {}
    forbidden_top_level = {
        "config",
        "connection_config",
        "provider_config",
        "secret_refs",
        "secrets",
    }
    forbidden_nested = forbidden_top_level - {"secret_refs"}
    if any(key in target for key in forbidden_top_level):
        raise ServiceValidationError(
            "External-effect actions accept only target.connection_id; connection config and secrets are resolved server-side"
        )
    if any(key in connection for key in forbidden_nested):
        raise ServiceValidationError(
            "External-effect actions accept only target.connection_id; connection config and secrets are resolved server-side"
        )
    return (
        str(target.get("connection_id") or "").strip()
        or str(connection.get("connection_id") or "").strip()
    )


def _target_connection_id(target: dict[str, Any]) -> str:
    connection = target.get("connection")
    connection = connection if isinstance(connection, dict) else {}
    return (
        str(target.get("connection_id") or "").strip()
        or str(connection.get("connection_id") or "").strip()
    )


def _load_direct_action_connection_row(
    bl: Any,
    *,
    connection_id: str,
    account_id: str,
    project_id: str,
    extension_id: str,
    extension_install_id: str | None = None,
) -> dict[str, Any]:
    normalized_connection_id = str(connection_id or "").strip()
    if not normalized_connection_id or bl is None:
        return {}
    row = None
    getter = getattr(bl, "get_project_connection", None)
    if callable(getter):
        try:
            row = getter(
                project_id=project_id or None,
                connection_id=normalized_connection_id,
                extension_id=extension_id,
                enabled_only=True,
            )
        except Exception:
            row = None
    if row is None and callable(getattr(bl, "cursor", None)):
        try:
            row = project_connection_store.get_project_connection_row(bl, normalized_connection_id)
        except Exception:
            row = None
    connection = dict(row) if isinstance(row, dict) else {}
    if not connection:
        return {}
    if str(connection.get("connection_id") or "").strip() != normalized_connection_id:
        return {}
    if account_id and str(connection.get("account_id") or "").strip() != account_id:
        return {}
    if project_id and str(connection.get("project_id") or "").strip() != project_id:
        return {}
    if (
        extension_id
        and str(connection.get("extension_id") or "").strip().lower() != extension_id.lower()
    ):
        return {}
    if (
        extension_install_id
        and str(connection.get("extension_install_id") or "").strip()
        != str(extension_install_id).strip()
    ):
        return {}
    if connection.get("enabled") is False:
        return {}
    return connection


def _direct_action_connection_target(
    bl: Any,
    *,
    connection_id: str,
    account_id: str,
    project_id: str,
    extension_id: str,
    extension_install_id: str | None = None,
    required_connection_type_ids: frozenset[str] = frozenset(),
    required_oauth_scopes: frozenset[str] = frozenset(),
    browser_safe: bool = False,
    get_project_by_id: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    connection = _load_direct_action_connection_row(
        bl,
        connection_id=connection_id,
        account_id=account_id,
        project_id=project_id,
        extension_id=extension_id,
        extension_install_id=extension_install_id,
    )
    if not connection:
        raise ServiceValidationError(
            "target.connection_id must reference an enabled project connection for this account, project, and extension"
        )
    declared_connection_types = frozenset(
        str(value).strip() for value in (required_connection_type_ids or ()) if str(value).strip()
    )
    raw_granted_scopes = connection.get("oauth_granted_scopes")
    granted_scopes = (
        {
            str(value).strip()
            for value in raw_granted_scopes
            if isinstance(value, str) and str(value).strip()
        }
        if isinstance(raw_granted_scopes, list)
        else set()
    )
    if (
        declared_connection_types
        and str(connection.get("connection_type") or "").strip() not in declared_connection_types
    ) or not set(required_oauth_scopes).issubset(granted_scopes):
        raise ServiceValidationError(f"connection_runtime_requirement_failed:{connection_id}")
    extension_oauth = bool(
        str(connection.get("oauth_configuration_id") or "").strip()
        and str(connection.get("oauth_provider_id") or "").strip()
    )
    if extension_oauth:
        runtime_config = _runtime_connection_config(connection)
        browser_target = {
            "connection_id": str(connection.get("connection_id") or "").strip(),
            "account_id": str(connection.get("account_id") or "").strip(),
            "project_id": str(connection.get("project_id") or "").strip(),
            "display_name": str(connection.get("display_name") or "").strip(),
            "connection_type": str(connection.get("connection_type") or "").strip(),
            "environment": str(connection.get("environment") or "").strip(),
            "credential_revision": int(connection.get("credential_revision") or 1),
            "oauth_configuration_id": str(connection.get("oauth_configuration_id") or "").strip(),
            "oauth_provider_id": str(connection.get("oauth_provider_id") or "").strip(),
            "config": {"oauth": dict(runtime_config.get("oauth") or {})},
            "oauth_configuration": _browser_oauth_configuration(bl, connection=connection),
        }
        if browser_safe:
            return browser_target
        try:
            oauth_secrets = _resolve_extension_owned_oauth_runtime(
                bl,
                connection=connection,
                get_project_by_id=get_project_by_id,
            )
        except Exception as exc:
            raise ServiceValidationError(
                f"connection_oauth_resolution_failed:{connection_id}:{exc}"
            ) from exc
        return {**browser_target, "config": runtime_config, "secrets": oauth_secrets}
    if browser_safe:
        return {
            "connection_id": str(connection.get("connection_id") or "").strip(),
            "account_id": str(connection.get("account_id") or "").strip(),
            "project_id": str(connection.get("project_id") or "").strip(),
            "display_name": str(connection.get("display_name") or "").strip(),
            "connection_type": str(connection.get("connection_type") or "").strip(),
            "environment": str(connection.get("environment") or "").strip(),
            "credential_revision": int(connection.get("credential_revision") or 1),
            "config": {},
        }
    secret_refs = dict(connection.get("secret_refs") or {})
    secrets: dict[str, str] = {}
    if secret_refs and callable(getattr(bl, "cursor", None)):
        try:
            from core.application.management.project_connection_secrets import (
                resolve_connection_secrets,
            )

            secrets = resolve_connection_secrets(
                bl,
                account_id=str(connection.get("account_id") or "").strip(),
                secret_refs=secret_refs,
            )
        except Exception as exc:
            raise ServiceValidationError(
                f"connection_secret_resolution_failed:{connection_id}:{exc}"
            ) from exc
    for name, value in provider_secrets_for_connection(bl, connection, secret_refs).items():
        secrets.setdefault(name, value)
    if callable(getattr(bl, "cursor", None)):
        try:
            from core.application.auth.oauth_client import (
                resolve_oauth_secrets_for_runtime,
            )

            oauth_secrets = resolve_oauth_secrets_for_runtime(bl, connection=connection)
        except Exception as exc:
            raise ServiceValidationError(
                f"connection_oauth_resolution_failed:{connection_id}:{exc}"
            ) from exc
        if oauth_secrets:
            secrets.update(oauth_secrets)
    return {
        "connection_id": str(connection.get("connection_id") or "").strip(),
        "account_id": str(connection.get("account_id") or "").strip(),
        "project_id": str(connection.get("project_id") or "").strip(),
        "display_name": str(connection.get("display_name") or "").strip(),
        "connection_type": str(connection.get("connection_type") or "").strip(),
        "environment": str(connection.get("environment") or "").strip(),
        "credential_revision": int(connection.get("credential_revision") or 1),
        "config": dict(connection.get("connection_config") or {}),
        "secret_refs": secret_refs,
        "secrets": secrets,
    }


def provider_secrets_for_connection(
    bl: Any, connection: dict[str, Any], secret_refs: dict[str, Any]
) -> dict[str, str]:
    """Return the secrets a connection keeps in ``provider_secrets``.

    The extension's secrets form saves a token there, and the connection only
    records which keys it owns (``secret_refs.provider_secrets``). Without this
    read an operation never received the token, although inbound polling did.
    """
    declared = secret_refs.get("provider_secrets")
    reader = getattr(bl, "get_provider_secret", None)
    if not isinstance(declared, dict) or not callable(reader):
        return {}
    provider_key = str(declared.get("provider_key") or "").strip()
    keys = declared.get("keys")
    project_id = str(connection.get("project_id") or "").strip()
    if not provider_key or not project_id or not isinstance(keys, dict):
        return {}
    found: dict[str, str] = {}
    for name, secret_key in keys.items():
        value = reader(
            provider_key=provider_key,
            secret_key=str(secret_key or "").strip(),
            project_id=project_id,
            include_global=False,
        )
        if value:
            found[str(name).strip()] = value
    return found


__all__ = [
    "_direct_action_connection_target",
    "_direct_action_project_id",
    "_load_direct_action_connection_row",
    "_require_direct_action_project_access",
    "_target_connection_id",
    "_target_connection_id_for_external_effect",
    "provider_secrets_for_connection",
]
