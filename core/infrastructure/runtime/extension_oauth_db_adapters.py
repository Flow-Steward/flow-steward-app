"""Database composition adapters for extension-owned OAuth entrypoints."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from core.application.extensions.callbacks import (
    ExtensionCallbackRegistrationService,
    browser_oauth_callback_base_url,
)
from core.application.extensions.oauth_authorization import OAuthAuthorizationService
from core.application.extensions.oauth_callback import OAuthCallbackService
from core.application.extensions.oauth_configuration import OAuthConfigurationService
from core.application.extensions.oauth_configuration_secrets import (
    DatabaseOAuthConfigurationSecretStore,
)
from core.application.extensions.oauth_connection_lifecycle import (
    OAuthConnectionLifecycleService,
    build_database_oauth_connection_lifecycle_service,
)
from core.application.extensions.oauth_lifecycle import record_extension_oauth_lifecycle_audit
from core.application.extensions.oauth_repository import ExtensionOAuthRepository
from core.application.extensions.oauth_runtime import OAuthRuntimeTokenService
from core.application.management.errors import ServiceUnavailableError
from core.infrastructure.config.runtime_env import runtime_env
from core.infrastructure.db import model_provider_store, psycopg_runtime
from core.infrastructure.db.db_pool import transaction_connection
from core.infrastructure.extension_runtime.extension_runtime import run_extension_action
from core.infrastructure.extension_runtime.oauth_token_store import DatabaseOAuthCallbackTokenStore
from core.infrastructure.extension_runtime.oauth_transport import (
    exchange_code,
    refresh_token,
    revoke_token,
)
from core.infrastructure.security.secret_codec import decrypt_secret, encrypt_secret


def _oauth_connection(logger: Any) -> Any:
    connection = getattr(logger, "conn", None)
    if connection is None:
        raise ServiceUnavailableError("Database not available")
    return connection


def create_extension_oauth_configuration_service(logger: Any) -> OAuthConfigurationService:
    connection = _oauth_connection(logger)
    confirmation_secret = str(runtime_env.get("FS_SECRET_KEY") or "").strip()
    if not confirmation_secret:
        raise ServiceUnavailableError("OAuth confirmation signing is unavailable")
    token_store = DatabaseOAuthCallbackTokenStore(connection)

    def configuration_secret_value(*, account_id: str, ref_key: str) -> str:
        ciphertext = model_provider_store.get_secret_ref_ciphertext(
            connection, account_id=account_id, ref_key=ref_key
        )
        return decrypt_secret(ciphertext) if ciphertext else ""

    def revocation_audit(**values: Any) -> None:
        with transaction_connection(connection):
            record_extension_oauth_lifecycle_audit(logger, **values)

    return OAuthConfigurationService(
        repository=ExtensionOAuthRepository(connection),
        secret_store=DatabaseOAuthConfigurationSecretStore(connection),
        install_loader=lambda install_id, account_id: logger.get_extension_install_for_operator(
            install_id, account_id=account_id
        ),
        confirmation_key=confirmation_secret.encode(),
        transaction_factory=lambda: transaction_connection(connection),
        token_snapshot=token_store.resolve_token_snapshot,
        configuration_secret_value=configuration_secret_value,
        revoke_token=revoke_token,
        revocation_audit=revocation_audit,
    )


def create_extension_oauth_authorization_service(logger: Any) -> OAuthAuthorizationService:
    connection = _oauth_connection(logger)
    secret_store = DatabaseOAuthConfigurationSecretStore(connection)
    callback_service = ExtensionCallbackRegistrationService(logger, project_repository=logger)

    def register_callback(**values: Any) -> str:
        return callback_service.ensure_oauth_registration(
            **values, base_url=browser_oauth_callback_base_url()
        ).callback_url

    def callback_registration_ready(**values: Any) -> bool:
        try:
            browser_oauth_callback_base_url()
        except Exception:
            return False
        return callback_service.oauth_registration_is_ready(**values)

    return OAuthAuthorizationService(
        repository=ExtensionOAuthRepository(connection),
        install_loader=lambda install_id, account_id: logger.get_extension_install_for_operator(
            install_id, account_id=account_id
        ),
        register_callback=register_callback,
        secret_present=secret_store.present,
        encrypt_verifier=encrypt_secret,
        transaction_factory=lambda: transaction_connection(connection),
        callback_registration_ready=callback_registration_ready,
    )


def create_extension_oauth_callback_service(logger: Any) -> OAuthCallbackService:
    connection = _oauth_connection(logger)

    def configuration_secret_value(*, account_id: str, ref_key: str) -> str:
        ciphertext = model_provider_store.get_secret_ref_ciphertext(
            connection, account_id=account_id, ref_key=ref_key
        )
        return decrypt_secret(ciphertext) if ciphertext else ""

    @contextmanager
    def refresh_claim_connection():
        dsn = str(getattr(logger, "dsn", "") or "").strip()
        if not dsn:
            raise RuntimeError("extension OAuth refresh claim database unavailable")
        claim_connection = psycopg_runtime.connect(dsn)
        try:
            claim_connection.autocommit = True
            yield claim_connection
        finally:
            claim_connection.close()

    repository = ExtensionOAuthRepository(
        connection,
        refresh_claim_connection_factory=refresh_claim_connection,
    )
    token_store = DatabaseOAuthCallbackTokenStore(connection)
    runtime_service = OAuthRuntimeTokenService(
        repository=repository,
        token_store=token_store,
        install_loader=lambda install_id, account_id: logger.get_extension_install_for_operator(
            install_id, account_id=account_id
        ),
        transaction_factory=lambda: transaction_connection(connection),
        configuration_secret_value=configuration_secret_value,
        refresh_access_token=refresh_token,
    )

    return OAuthCallbackService(
        repository=repository,
        token_store=token_store,
        install_loader=lambda install_id, account_id: logger.get_extension_install_for_operator(
            install_id, account_id=account_id
        ),
        configuration_secret_value=configuration_secret_value,
        decrypt_verifier=decrypt_secret,
        exchange_code=exchange_code,
        transaction_factory=lambda: transaction_connection(connection),
        public_base_url=browser_oauth_callback_base_url(),
        invoke_post_connect=lambda **values: run_extension_action(
            values["extension"],
            action_id=values["action_id"],
            target=values["target"],
            context=values["context"],
            timeout_seconds=15,
            runtime_context={"source": "extension_oauth_post_connect"},
        ),
        resolve_runtime_access_token=runtime_service.resolve_runtime_access_token,
    )


def create_extension_oauth_connection_lifecycle_service(
    logger: Any,
) -> OAuthConnectionLifecycleService:
    return build_database_oauth_connection_lifecycle_service(logger)


__all__ = [
    "create_extension_oauth_authorization_service",
    "create_extension_oauth_callback_service",
    "create_extension_oauth_configuration_service",
    "create_extension_oauth_connection_lifecycle_service",
]
