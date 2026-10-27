"""Register and maintain extension callbacks with account and connection ownership."""

from __future__ import annotations

import hmac
import uuid
from dataclasses import dataclass
from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.extensions.callback_contract import (
    CALLBACK_TOKEN_REDACTION,
    _as_dict,
    _callback_auth,
    _extension_runtime_id,
    _normalize_text,
    callback_capability_ref,
    callback_secret_ref,
    callback_token_hash,
    callback_url,
    find_callback_declaration,
    generate_callback_secret,
    generate_callback_token,
)
from core.application.management.scope import ScopeContext
from core.application.workflows.project_credential_versions import guard_project_credential_versions
from core.infrastructure.db.db_pool import transaction_connection
from core.infrastructure.db.extension_response import normalize_extension_response
from core.infrastructure.extension_runtime.extension_runtime_helpers import (
    resolve_extension_runtime_record,
)

_MESSAGE_CONNECTION_ID_IS_NOT_VALID_FOR_THIS_PROJECT = "connection_id is not valid for this project"


_MESSAGE_PROJECT_SECRET_STORAGE_IS_UNAVAILABLE = (  # pragma: allowlist secret - user-facing error text
    "Project secret storage is unavailable"
)


EXTENSION_CALLBACK_REGISTRATION_STORAGE_IS_MESSAGE = (
    "Extension callback registration storage is unavailable"
)


class OAuthCallbackRegistrationPreconditionError(RuntimeError):
    """The persisted reserved registration cannot safely be used."""


class OAuthCallbackRegistrationStorageError(RuntimeError):
    """Required callback or encrypted-secret storage is unavailable."""


def _apply_project_state_updates(
    invocation: dict[str, Any],
    *,
    extension: dict[str, Any],
    bl: Any,
    scope: ScopeContext | None,
    project_id: str,
    connection_id: str | None = None,
    extension_install_id: str | None = None,
    account_id: str | None = None,
) -> None:
    if invocation.get("status") != "succeeded":
        return
    try:
        from core.application.extensions.response_envelope import process_project_state_updates

        process_project_state_updates(
            invocation,
            extension=extension,
            extension_id=_extension_runtime_id(extension),
            db=bl,
            account_id=_normalize_text(account_id)
            or _normalize_text(getattr(scope, "account_id", None))
            or DEFAULT_ACCOUNT_ID,
            project_id=_normalize_text(project_id),
            connection_id=_normalize_text(connection_id) or None,
            extension_install_id=_normalize_text(extension_install_id) or None,
            actor_id=_normalize_text(getattr(scope, "actor_id", None)) or None,
        )
    except Exception as exc:
        invocation["status"] = "failed"
        invocation["detail"] = "project_state_update_failed"
        invocation["errors"] = [
            *list(invocation.get("errors") or []),
            {"code": "project_state_update_failed", "message": str(exc)},
        ]


@dataclass(frozen=True)
class CallbackRegistrationCreateResult:
    registration: dict[str, Any]
    callback_url: str
    callback_token: str
    callback_secret: str

    def to_api_dict(
        self,
        *,
        include_token: bool = True,
        include_secret: bool = True,
        redact_url: bool = False,
    ) -> dict[str, Any]:
        payload = dict(self.registration)
        payload["callback_url"] = (
            self.callback_url.replace(self.callback_token, CALLBACK_TOKEN_REDACTION)
            if redact_url and self.callback_token
            else self.callback_url
        )
        if include_token:
            payload["callback_token"] = self.callback_token
        if include_secret and self.callback_secret:
            payload["callback_secret"] = self.callback_secret
        return payload


class ExtensionCallbackRegistrationService:
    def __init__(self, bl: Any, *, project_repository: Any | None = None) -> None:
        self.bl = bl
        self.project_repository = project_repository or bl

    def _account_id_for_project(self, project_id: str) -> str:
        normalized_project_id = _normalize_text(project_id)
        if not normalized_project_id or not hasattr(self.project_repository, "get_project"):
            return ""
        project = _as_dict(self.project_repository.get_project(normalized_project_id))
        return _normalize_text(project.get("account_id"))

    def _registration_account_id(self, registration: dict[str, Any]) -> str:
        explicit_account_id = _normalize_text(registration.get("account_id"))
        if explicit_account_id:
            return explicit_account_id
        return self._account_id_for_project(_normalize_text(registration.get("project_id")))

    def _extension(
        self, extension_id: str, *, account_id: str | None = None
    ) -> dict[str, Any] | None:
        normalized_extension_id = _normalize_text(extension_id)
        normalized_account_id = _normalize_text(account_id)
        if self.bl is not None and hasattr(self.bl, "get_extension"):
            try:
                extension = self.bl.get_extension(
                    normalized_extension_id,
                    account_id=normalized_account_id or None,
                )
            except TypeError:
                extension = self.bl.get_extension(normalized_extension_id)
                if (
                    normalized_account_id
                    and _normalize_text(_as_dict(extension).get("account_id"))
                    != normalized_account_id
                ):
                    extension = None
            if extension:
                return extension
            if normalized_account_id:
                return None
        extension, _ = resolve_extension_runtime_record(
            normalized_extension_id,
            get_logger_only=lambda: self.bl,
            account_id=normalized_account_id or None,
        )
        return extension

    def _require_callback(
        self,
        extension_id: str,
        callback_id: str,
        *,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        extension = self._extension(extension_id, account_id=account_id)
        callback = find_callback_declaration(extension, callback_id)
        if callback is None:
            raise ValueError(
                f"Extension '{_normalize_text(extension_id)}' does not declare callback "
                f"'{_normalize_text(callback_id)}'"
            )
        return callback

    def _require_connection_owner(
        self,
        *,
        connection_id: str | None,
        required: bool,
        project_id: str,
        account_id: str,
        extension_id: str,
    ) -> str | None:
        normalized_connection_id = _normalize_text(connection_id)
        if not normalized_connection_id:
            if required:
                raise ValueError("connection_id is required for this callback")
            return None
        row = None
        getter = getattr(self.bl, "get_project_connection", None)
        if callable(getter):
            try:
                row = getter(
                    project_id=project_id,
                    connection_id=normalized_connection_id,
                    extension_id=extension_id,
                    enabled_only=False,
                )
            except Exception:
                row = None
        if row is None and hasattr(self.bl, "cursor"):
            try:
                from core.infrastructure.db import project_connection_store

                row = project_connection_store.get_project_connection_row(
                    self.bl,
                    normalized_connection_id,
                )
            except Exception:
                row = None
        connection = _as_dict(row)
        if not connection:
            raise ValueError(_MESSAGE_CONNECTION_ID_IS_NOT_VALID_FOR_THIS_PROJECT)
        if _normalize_text(connection.get("connection_id")) != normalized_connection_id:
            raise ValueError(_MESSAGE_CONNECTION_ID_IS_NOT_VALID_FOR_THIS_PROJECT)
        if _normalize_text(connection.get("project_id")) != _normalize_text(project_id):
            raise ValueError(_MESSAGE_CONNECTION_ID_IS_NOT_VALID_FOR_THIS_PROJECT)
        if _normalize_text(connection.get("account_id")) != _normalize_text(account_id):
            raise ValueError("connection_id is not valid for this account")
        if _normalize_text(connection.get("extension_id")) != _normalize_text(extension_id):
            raise ValueError("connection_id does not belong to this extension")
        return normalized_connection_id

    def create_registration(
        self,
        *,
        extension_id: str,
        callback_id: str,
        project_id: str,
        connection_id: str | None = None,
        base_url: str,
        enabled: bool = True,
    ) -> CallbackRegistrationCreateResult:
        if not hasattr(self.bl, "create_extension_callback_registration"):
            raise RuntimeError(EXTENSION_CALLBACK_REGISTRATION_STORAGE_IS_MESSAGE)
        normalized_extension_id = _normalize_text(extension_id)
        normalized_callback_id = _normalize_text(callback_id)
        normalized_project_id = _normalize_text(project_id)
        if not normalized_project_id:
            raise ValueError("project_id is required")

        account_id = self._account_id_for_project(normalized_project_id)
        if not account_id:
            raise ValueError("project account not found")

        declaration = self._require_callback(
            normalized_extension_id,
            normalized_callback_id,
            account_id=account_id,
        )
        if _normalize_text(declaration.get("purpose")).lower() == "oauth_authorization":
            raise ValueError("OAuth authorization callbacks are managed by core")
        normalized_connection_id = self._require_connection_owner(
            connection_id=connection_id,
            required=bool(declaration.get("connection_scoped")),
            project_id=normalized_project_id,
            account_id=account_id,
            extension_id=normalized_extension_id,
        )
        raw_token = generate_callback_token()
        raw_secret = ""
        auth = _callback_auth(declaration)
        auth_config: dict[str, Any] = {}
        if auth.get("type") in {"shared_secret_header", "hmac_sha256"}:
            if not hasattr(self.bl, "save_project_secret"):
                raise RuntimeError(_MESSAGE_PROJECT_SECRET_STORAGE_IS_UNAVAILABLE)
            raw_secret = generate_callback_secret()

        registration_id = str(uuid.uuid4())
        secret_ref = callback_secret_ref(registration_id) if raw_secret else ""
        if raw_secret:
            with transaction_connection(self.bl.conn) as connection, connection.cursor() as cursor:
                guard_project_credential_versions(
                    cursor, project_id=normalized_project_id, secret_key=secret_ref
                )
                saved = self.bl.save_project_secret(
                    project_id=normalized_project_id,
                    secret_key=secret_ref,
                    secret_value=raw_secret,
                    active=True,
                    metadata={
                        "source": "extension_callback_registration",
                        "extension_id": normalized_extension_id,
                        "callback_id": normalized_callback_id,
                    },
                )
                if not saved:
                    raise RuntimeError(_MESSAGE_PROJECT_SECRET_STORAGE_IS_UNAVAILABLE)
            auth_config["secret_ref"] = secret_ref

        registration = self.bl.create_extension_callback_registration(
            registration_id=registration_id,
            extension_id=normalized_extension_id,
            callback_id=normalized_callback_id,
            callback_token_hash=callback_token_hash(raw_token),
            project_id=normalized_project_id,
            connection_id=normalized_connection_id,
            enabled=bool(enabled),
            auth_type=_normalize_text(auth.get("type")).lower() or "none",
            auth_config=auth_config,
        )
        if not registration:
            raise RuntimeError(EXTENSION_CALLBACK_REGISTRATION_STORAGE_IS_MESSAGE)
        return CallbackRegistrationCreateResult(
            registration=dict(registration or {}),
            callback_url=callback_url(
                base_url=base_url,
                extension_id=normalized_extension_id,
                callback_id=normalized_callback_id,
                callback_token=raw_token,
            ),
            callback_token=raw_token,
            callback_secret=raw_secret,
        )

    def ensure_oauth_registration(
        self,
        *,
        extension_id: str,
        callback_id: str,
        project_id: str,
        base_url: str,
    ) -> CallbackRegistrationCreateResult:
        """Return one stable, encrypted-capability OAuth callback registration.

        The caller owns the surrounding transaction. The storage lock serializes
        both first creation and recovery so no implicit token rotation can occur.
        """
        normalized_extension_id = _normalize_text(extension_id)
        normalized_callback_id = _normalize_text(callback_id)
        normalized_project_id = _normalize_text(project_id)
        if not all((normalized_extension_id, normalized_callback_id, normalized_project_id)):
            raise ValueError("extension_id, callback_id, and project_id are required")
        account_id = self._account_id_for_project(normalized_project_id)
        if not account_id:
            raise ValueError("project account not found")
        declaration = self._require_callback(
            normalized_extension_id,
            normalized_callback_id,
            account_id=account_id,
        )
        if _normalize_text(declaration.get("purpose")).lower() != "oauth_authorization":
            raise ValueError("Callback is not reserved for OAuth authorization")
        if not hasattr(self.bl, "lock_extension_callback_registration_scope"):
            raise OAuthCallbackRegistrationStorageError(
                EXTENSION_CALLBACK_REGISTRATION_STORAGE_IS_MESSAGE
            )
        self.bl.lock_extension_callback_registration_scope(
            extension_id=normalized_extension_id,
            callback_id=normalized_callback_id,
            project_id=normalized_project_id,
            connection_id=None,
        )
        rows = self.bl.list_extension_callback_registrations(
            extension_id=normalized_extension_id,
            callback_id=normalized_callback_id,
            project_id=normalized_project_id,
        )
        registrations = [row for row in rows if not _normalize_text(row.get("connection_id"))]
        if len(registrations) > 1:
            raise OAuthCallbackRegistrationPreconditionError(
                "OAuth callback registration identity is ambiguous"
            )
        if registrations:
            registration = dict(registrations[0])
            if (
                not bool(registration.get("enabled"))
                or _normalize_text(registration.get("auth_type")).lower() != "none"
            ):
                raise OAuthCallbackRegistrationPreconditionError(
                    "OAuth callback registration is unavailable"
                )
            auth_config = _as_dict(registration.get("auth_config"))
            token_ref = _normalize_text(auth_config.get("capability_token_ref"))
            expected_token_ref = callback_capability_ref(_normalize_text(registration.get("id")))
            raw_token = (
                _normalize_text(
                    self.bl.get_project_secret(
                        project_id=normalized_project_id,
                        secret_key=token_ref,
                    )
                )
                if token_ref == expected_token_ref and hasattr(self.bl, "get_project_secret")
                else ""
            )
            if not raw_token or not hmac.compare_digest(
                callback_token_hash(raw_token),
                _normalize_text(registration.get("callback_token_hash")),
            ):
                raise OAuthCallbackRegistrationPreconditionError(
                    "OAuth callback capability is unavailable"
                )
            return CallbackRegistrationCreateResult(
                registration=registration,
                callback_url=callback_url(
                    base_url=base_url,
                    extension_id=normalized_extension_id,
                    callback_id=normalized_callback_id,
                    callback_token=raw_token,
                ),
                callback_token=raw_token,
                callback_secret="",
            )

        if not hasattr(self.bl, "save_project_secret") or not hasattr(
            self.bl, "create_extension_callback_registration"
        ):
            raise OAuthCallbackRegistrationStorageError(
                EXTENSION_CALLBACK_REGISTRATION_STORAGE_IS_MESSAGE
            )
        registration_id = str(uuid.uuid4())
        raw_token = generate_callback_token()
        token_ref = callback_capability_ref(registration_id)
        with self.bl.conn.cursor() as cursor:
            guard_project_credential_versions(
                cursor, project_id=normalized_project_id, secret_key=token_ref
            )
        saved = self.bl.save_project_secret(
            project_id=normalized_project_id,
            secret_key=token_ref,
            secret_value=raw_token,
            active=True,
            metadata={
                "source": "extension_oauth_callback_registration",
                "extension_id": normalized_extension_id,
                "callback_id": normalized_callback_id,
            },
        )
        if not saved:
            raise OAuthCallbackRegistrationStorageError(
                _MESSAGE_PROJECT_SECRET_STORAGE_IS_UNAVAILABLE
            )
        registration = self.bl.create_extension_callback_registration(
            registration_id=registration_id,
            extension_id=normalized_extension_id,
            callback_id=normalized_callback_id,
            callback_token_hash=callback_token_hash(raw_token),
            project_id=normalized_project_id,
            connection_id=None,
            enabled=True,
            auth_type="none",
            auth_config={"capability_token_ref": token_ref},
        )
        if not registration or _normalize_text(registration.get("id")) != registration_id:
            raise OAuthCallbackRegistrationStorageError(
                EXTENSION_CALLBACK_REGISTRATION_STORAGE_IS_MESSAGE
            )
        return CallbackRegistrationCreateResult(
            registration=dict(registration),
            callback_url=callback_url(
                base_url=base_url,
                extension_id=normalized_extension_id,
                callback_id=normalized_callback_id,
                callback_token=raw_token,
            ),
            callback_token=raw_token,
            callback_secret="",
        )

    def oauth_registration_is_ready(
        self,
        *,
        extension_id: str,
        callback_id: str,
        project_id: str,
    ) -> bool:
        """Check an existing OAuth registration without creating or rotating it."""
        normalized_extension_id = _normalize_text(extension_id)
        normalized_callback_id = _normalize_text(callback_id)
        normalized_project_id = _normalize_text(project_id)
        if not all((normalized_extension_id, normalized_callback_id, normalized_project_id)):
            return False
        account_id = self._account_id_for_project(normalized_project_id)
        if not account_id:
            return False
        try:
            declaration = self._require_callback(
                normalized_extension_id,
                normalized_callback_id,
                account_id=account_id,
            )
        except Exception:
            return False
        if _normalize_text(declaration.get("purpose")).lower() != "oauth_authorization":
            return False
        rows = self.bl.list_extension_callback_registrations(
            extension_id=normalized_extension_id,
            callback_id=normalized_callback_id,
            project_id=normalized_project_id,
        )
        registrations = [row for row in rows if not _normalize_text(row.get("connection_id"))]
        if len(registrations) != 1:
            return False
        registration = dict(registrations[0])
        if (
            not bool(registration.get("enabled"))
            or _normalize_text(registration.get("auth_type")).lower() != "none"
        ):
            return False
        auth_config = _as_dict(registration.get("auth_config"))
        token_ref = _normalize_text(auth_config.get("capability_token_ref"))
        expected_token_ref = callback_capability_ref(_normalize_text(registration.get("id")))
        raw_token = (
            _normalize_text(
                self.bl.get_project_secret(
                    project_id=normalized_project_id,
                    secret_key=token_ref,
                )
            )
            if token_ref == expected_token_ref and hasattr(self.bl, "get_project_secret")
            else ""
        )
        return bool(raw_token) and hmac.compare_digest(
            callback_token_hash(raw_token),
            _normalize_text(registration.get("callback_token_hash")),
        )

    def rotate_registration_token(
        self,
        *,
        registration_id: str,
        base_url: str,
        registration: dict[str, Any] | None = None,
    ) -> CallbackRegistrationCreateResult:
        if not hasattr(self.bl, "get_extension_callback_registration_by_id") or not hasattr(
            self.bl, "update_extension_callback_registration_token"
        ):
            raise RuntimeError(EXTENSION_CALLBACK_REGISTRATION_STORAGE_IS_MESSAGE)
        registration = registration or self.bl.get_extension_callback_registration_by_id(
            registration_id
        )
        if not registration:
            raise ValueError("callback registration not found")
        declaration = self._require_callback(
            _normalize_text(registration.get("extension_id")),
            _normalize_text(registration.get("callback_id")),
            account_id=self._registration_account_id(registration),
        )
        if _normalize_text(declaration.get("purpose")).lower() == "oauth_authorization":
            raise ValueError("OAuth authorization callback rotation is managed by core")
        raw_token = generate_callback_token()
        updated = self.bl.update_extension_callback_registration_token(
            registration_id=_normalize_text(registration_id),
            callback_token_hash=callback_token_hash(raw_token),
        )
        if not updated:
            raise RuntimeError(EXTENSION_CALLBACK_REGISTRATION_STORAGE_IS_MESSAGE)
        return CallbackRegistrationCreateResult(
            registration=dict(updated),
            callback_url=callback_url(
                base_url=base_url,
                extension_id=_normalize_text(registration.get("extension_id")),
                callback_id=_normalize_text(registration.get("callback_id")),
                callback_token=raw_token,
            ),
            callback_token=raw_token,
            callback_secret="",
        )

    def _provider_runtime_payload(self, registration: dict[str, Any]) -> dict[str, Any]:
        extension_id = _normalize_text(registration.get("extension_id"))
        project_id = _normalize_text(registration.get("project_id"))
        connection_id = _normalize_text(registration.get("connection_id"))
        if not extension_id or not project_id:
            return {}
        try:
            from core.application.extensions.provider_runtime import (
                resolve_extension_runtime_config,
            )

            resolved = resolve_extension_runtime_config(
                extension_id,
                {"project_id": project_id, "connection_id": connection_id},
                bl=self.bl,
                project_id=project_id,
            )
        except Exception:
            return {}
        project_connection = _as_dict(resolved.get("project_connection"))
        resolved_connection_id = _normalize_text(project_connection.get("connection_id"))
        if connection_id and resolved_connection_id != connection_id:
            return {}
        return {
            "provider_key": extension_id,
            "integration_key": extension_id,
            "provider_config": _as_dict(resolved.get("config")),
            "binding_config": _as_dict(project_connection.get("connection_config")),
            "project_id": project_id,
            "connection_id": resolved_connection_id or connection_id,
            "ingress_config": _as_dict(project_connection.get("ingress_config")),
        }

    def secret_for_registration(self, registration: dict[str, Any]) -> str:
        auth_config = _as_dict(registration.get("auth_config"))
        secret_ref = _normalize_text(auth_config.get("secret_ref"))
        project_id = _normalize_text(registration.get("project_id"))
        if not secret_ref or not project_id or not hasattr(self.bl, "get_project_secret"):
            return ""
        return _normalize_text(
            self.bl.get_project_secret(project_id=project_id, secret_key=secret_ref)
        )

    def invoke_registration_action(
        self,
        *,
        registration: dict[str, Any],
        action_id: str,
        scope: ScopeContext | None,
        callback_url_value: str = "",
        callback_secret_value: str = "",
        runtime: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], int]:
        from core.infrastructure.extension_runtime.extension_runtime import run_extension_action

        extension_id = _normalize_text(registration.get("extension_id"))
        project_id = _normalize_text(registration.get("project_id"))
        extension = self._extension(
            extension_id,
            account_id=self._registration_account_id(registration),
        )
        if not extension:
            return {"ok": False, "error": "Extension not found"}, 404

        input_payload: dict[str, Any] = {
            "runtime": {"fake_mode": False, **_as_dict(runtime)},
        }
        if callback_url_value:
            input_payload["callback_url"] = callback_url_value
        if callback_secret_value:
            input_payload["callback_secret"] = callback_secret_value
        provider = self._provider_runtime_payload(registration)
        if provider:
            input_payload["provider"] = provider

        invocation = run_extension_action(
            extension,
            action_id=_normalize_text(action_id),
            context={
                "project_id": project_id,
                "connection_id": _normalize_text(registration.get("connection_id")),
            },
            input_payload=input_payload,
            runtime_context={
                "logger": self.bl,
                "project_id": project_id,
                "connection_id": _normalize_text(registration.get("connection_id")),
                "source": "extension_callback_registration",
            },
            scope=scope,
        )
        _apply_project_state_updates(
            invocation,
            extension=extension,
            bl=self.bl,
            scope=scope,
            project_id=project_id,
            connection_id=_normalize_text(registration.get("connection_id")) or None,
        )
        if invocation.get("status") != "succeeded":
            return {
                "ok": False,
                "error": _normalize_text(invocation.get("detail"))
                or "Extension callback setup action failed",
            }, 502

        response = normalize_extension_response(_as_dict(invocation.get("response")))
        if not response.ok:
            return response.model_dump(), 502
        return {
            "ok": True,
            "result": response.result,
            "reload": response.reload,
        }, 200
