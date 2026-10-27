from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core.application.auth.secrets import decrypt_secret
from core.infrastructure.db import model_provider_store
from core.infrastructure.extension_runtime.oauth_token_store import DatabaseOAuthCallbackTokenStore

from .oauth_contract import compile_oauth_providers
from .oauth_lifecycle import (
    PreparedOAuthRevocation,
    execute_extension_oauth_revocation,
    prepare_extension_oauth_revocation,
)
from .uninstall_inventory_queries import uninstall_inventory_row
from .uninstall_models import UninstallContext, UninstallTarget

_CONFIG_COLUMNS = (
    "configuration_id",
    "tenant_id",
    "account_id",
    "project_id",
    "extension_install_id",
    "extension_id",
    "extension_release_id",
    "provider_id",
    "configuration_revision",
    "authorization_revision",
    "status",
    "non_secret_config",
    "secret_refs",
    "provider_security_fingerprint",
    "provider_revocation_fingerprint",
)
_CONNECTION_COLUMNS = (
    "connection_id",
    "account_id",
    "project_id",
    "extension_install_id",
    "extension_id",
    "connection_type",
    "credential_revision",
    "enabled",
    "connection_config",
    "secret_refs",
    "oauth_configuration_id",
    "oauth_provider_id",
    "oauth_extension_release_id",
    "oauth_authorization_revision",
    "oauth_provider_security_fingerprint",
    "oauth_provider_revocation_fingerprint",
)


@dataclass(frozen=True)
class PreparedOAuthUninstall:
    revocations: tuple[PreparedOAuthRevocation, ...]
    owns_oauth: bool


class GenericOAuthUninstallLifecycle:
    def __init__(self, *, lifecycle_transaction: Callable[[Any], Any]) -> None:
        self._lifecycle_transaction = lifecycle_transaction

    def prepare(
        self,
        context: UninstallContext,
        target: UninstallTarget,
        *,
        validate_locked_plan: Callable[[UninstallContext, Any], Any],
    ) -> PreparedOAuthUninstall:
        bl = context.bl
        install_id = str(context.confirmed_plan.get("install_id") or target.install_id).strip()
        with self._lifecycle_transaction(bl) as transaction_conn:
            validate_locked_plan(context, transaction_conn)
            install = bl.get_extension_install_for_operator(
                install_id, account_id=context.account_id
            )
            descriptor = (
                dict(install.get("compiled_manifest") or install.get("manifest") or {})
                if isinstance(install, dict)
                else {}
            )
            try:
                providers = {row.provider_id: row for row in compile_oauth_providers(descriptor)}
            except ValueError:
                providers = {}
            if (
                getattr(bl, "_table_exists", lambda *_args: False)("extension_oauth_configurations")
                is not True
            ):
                return PreparedOAuthUninstall((), bool(providers))
            revocations, has_configurations = self._prepare_locked(
                context,
                target,
                transaction_conn,
                install=install,
                install_id=install_id,
                providers=providers,
            )
            return PreparedOAuthUninstall(tuple(revocations), bool(providers) or has_configurations)

    @staticmethod
    def execute(prepared: PreparedOAuthUninstall) -> str:
        if not prepared.revocations:
            return "not_available"
        from core.infrastructure.extension_runtime.oauth_transport import revoke_token

        outcomes: list[bool] = []
        for revocation in prepared.revocations:
            outcomes.append(
                execute_extension_oauth_revocation(revocation, revoke_token=revoke_token)
            )
        return "succeeded" if all(outcomes) else "failed"

    def _prepare_locked(
        self,
        context: UninstallContext,
        target: UninstallTarget,
        transaction_conn: Any,
        *,
        install: Any,
        install_id: str,
        providers: dict[str, Any],
    ) -> tuple[list[PreparedOAuthRevocation], bool]:
        scope_sql, params = self._scope(context, target, install_id)
        with transaction_conn.cursor() as cur:
            cur.execute(
                "UPDATE extension_installs SET state = 'disabled', installed_at = now() WHERE account_id::text = %s AND install_id = %s AND extension_id = %s",
                (context.account_id, install_id, context.extension_id),
            )
            cur.execute(
                f"UPDATE extension_oauth_authorization_states SET consumed_at = COALESCE(consumed_at, now()), updated_at = now() WHERE {scope_sql} AND consumed_at IS NULL",  # nosec B608
                tuple(params),
            )
            cur.execute(
                f"SELECT {', '.join(_CONFIG_COLUMNS)} FROM extension_oauth_configurations WHERE {scope_sql} FOR UPDATE",  # nosec B608
                tuple(params),
            )
            configurations = [
                uninstall_inventory_row(raw, _CONFIG_COLUMNS) for raw in cur.fetchall() or []
            ]
            return (
                self._prepare_connections(
                    cur,
                    transaction_conn,
                    configurations,
                    install=install,
                    install_id=install_id,
                    extension_id=context.extension_id,
                    providers=providers,
                ),
                bool(configurations),
            )

    @staticmethod
    def _scope(
        context: UninstallContext, target: UninstallTarget, install_id: str
    ) -> tuple[str, list[Any]]:
        scope_sql = "extension_install_id = %s AND extension_id = %s"
        params: list[Any] = [install_id, context.extension_id]
        if not target.include_consumer_accounts:
            scope_sql += " AND account_id::text = %s"
            params.append(context.account_id)
        return scope_sql, params

    @staticmethod
    def _prepare_connections(
        cur: Any,
        transaction_conn: Any,
        configurations: list[dict[str, Any]],
        *,
        install: Any,
        install_id: str,
        extension_id: str,
        providers: dict[str, Any],
    ) -> list[PreparedOAuthRevocation]:
        token_store = DatabaseOAuthCallbackTokenStore(transaction_conn)

        def configuration_secret_value(*, account_id: str, ref_key: str) -> str:
            ciphertext = model_provider_store.get_secret_ref_ciphertext(
                transaction_conn, account_id=account_id, ref_key=ref_key
            )
            return decrypt_secret(ciphertext) if ciphertext else ""

        prepared_rows: list[PreparedOAuthRevocation] = []
        for configuration in configurations:
            provider = providers.get(str(configuration.get("provider_id") or ""))
            if provider is None or str(configuration.get("extension_release_id") or "") != str(
                install.get("release_id") if isinstance(install, dict) else ""
            ):
                continue
            cur.execute(
                f"SELECT {', '.join(_CONNECTION_COLUMNS)} FROM project_connections WHERE oauth_configuration_id = %s AND account_id = %s AND project_id = %s AND extension_install_id = %s AND extension_id = %s AND oauth_provider_id = %s FOR UPDATE",  # nosec B608
                (
                    configuration["configuration_id"],
                    configuration["account_id"],
                    configuration["project_id"],
                    install_id,
                    extension_id,
                    configuration["provider_id"],
                ),
            )
            for raw in cur.fetchall() or []:
                prepared = prepare_extension_oauth_revocation(
                    provider=provider,
                    configuration=configuration,
                    connection=uninstall_inventory_row(raw, _CONNECTION_COLUMNS),
                    token_snapshot=token_store.resolve_token_snapshot,
                    configuration_secret_value=configuration_secret_value,
                )
                if prepared is not None:
                    prepared_rows.append(prepared)
        return prepared_rows
