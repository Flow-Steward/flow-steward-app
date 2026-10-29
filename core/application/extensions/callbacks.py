# mypy: ignore-errors
"""Generic extension callback intake and dispatch services."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import Request

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.extensions.callback_contract import (
    CALLBACK_CAPABILITY_REF_SUFFIX as CALLBACK_CAPABILITY_REF_SUFFIX,
)
from core.application.extensions.callback_contract import (
    CALLBACK_SECRET_BYTES as CALLBACK_SECRET_BYTES,
)
from core.application.extensions.callback_contract import (
    CALLBACK_SHARED_CREDENTIAL_REF_PREFIX as CALLBACK_SHARED_CREDENTIAL_REF_PREFIX,
)
from core.application.extensions.callback_contract import (
    CALLBACK_TOKEN_BYTES as CALLBACK_TOKEN_BYTES,
)
from core.application.extensions.callback_contract import (
    CALLBACK_TOKEN_REDACTION as CALLBACK_TOKEN_REDACTION,
)
from core.application.extensions.callback_contract import (
    OAuthCallbackPublicBaseURLUnavailable as OAuthCallbackPublicBaseURLUnavailable,
)
from core.application.extensions.callback_contract import (
    _as_dict,
    _base_url_from_request,
    _callback_auth,
    _extension_runtime_id,
    _normalize_text,
)
from core.application.extensions.callback_contract import (
    browser_oauth_callback_base_url as browser_oauth_callback_base_url,
)
from core.application.extensions.callback_contract import (
    callback_base_url_for_provider_setup as callback_base_url_for_provider_setup,
)
from core.application.extensions.callback_contract import (
    callback_capability_ref as callback_capability_ref,
)
from core.application.extensions.callback_contract import (
    callback_secret_ref as callback_secret_ref,
)
from core.application.extensions.callback_contract import (
    callback_token_hash as callback_token_hash,
)
from core.application.extensions.callback_contract import (
    callback_url as callback_url,
)
from core.application.extensions.callback_contract import (
    find_callback_declaration as find_callback_declaration,
)
from core.application.extensions.callback_contract import (
    generate_callback_secret as generate_callback_secret,
)
from core.application.extensions.callback_contract import (
    generate_callback_token as generate_callback_token,
)
from core.application.extensions.callback_contract import (
    redacted_callback_path as redacted_callback_path,
)
from core.application.extensions.callback_contract import (
    redacted_callback_url as redacted_callback_url,
)
from core.application.extensions.callback_registration import (
    EXTENSION_CALLBACK_REGISTRATION_STORAGE_IS_MESSAGE as EXTENSION_CALLBACK_REGISTRATION_STORAGE_IS_MESSAGE,
)
from core.application.extensions.callback_registration import (
    CallbackRegistrationCreateResult as CallbackRegistrationCreateResult,
)
from core.application.extensions.callback_registration import (
    ExtensionCallbackRegistrationService as ExtensionCallbackRegistrationService,
)
from core.application.extensions.callback_registration import (
    OAuthCallbackRegistrationPreconditionError as OAuthCallbackRegistrationPreconditionError,
)
from core.application.extensions.callback_registration import (
    OAuthCallbackRegistrationStorageError as OAuthCallbackRegistrationStorageError,
)
from core.application.extensions.callback_registration import (
    _apply_project_state_updates,
)
from core.application.management.scope import ScopeContext
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.config.size_limits import clamp_size_limit_bytes
from core.infrastructure.db.extension_response import normalize_extension_response

_MESSAGE_INVALID_CALLBACK_SIGNATURE = "Invalid callback signature"
_MAX_HUB_CHALLENGE_LENGTH = 512


NON_RETRYABLE_CALLBACK_ERROR_CODES = {
    "invalid_reject_comment_context",
}


# Public extension callback intake: cap the body before it is read, decoded into
# body_text, JSON-parsed, and base64-duplicated into the envelope so an
# unauthenticated-at-buffer-time caller cannot force unbounded in-memory work.
# Operator-tunable via env (read once at import; clamped to a safe range).
_MAX_CALLBACK_BODY_BYTES = clamp_size_limit_bytes(
    _runtime_env.get("FS_EXTENSION_CALLBACK_BODY_MAX_BYTES"),
    default=1 * 1024 * 1024,  # 1 MiB
    minimum=4 * 1024,  # 4 KiB
    maximum=64 * 1024 * 1024,  # 64 MiB
)


class CallbackBodyTooLarge(Exception):
    """Raised when a callback request body exceeds the intake size cap."""


@dataclass(frozen=True)
class CallbackAuthenticationResult:
    accepted: bool
    status_code: int = 401
    error: str = "Invalid callback secret"


async def _read_capped_callback_body(request: Request, *, cap: int) -> bytes:
    """Read the body, rejecting before materializing oversized input.

    Content-Length is checked first, then the cap is enforced during streaming so
    a chunked / mislabelled request cannot bypass the header pre-check.
    """
    from contextlib import suppress

    content_length = str(request.headers.get("content-length") or "").strip()
    if content_length.isdigit() and int(content_length) > cap:
        raise CallbackBodyTooLarge()
    cached = getattr(request, "_body", None)
    if isinstance(cached, (bytes, bytearray)):
        if len(cached) > cap:
            raise CallbackBodyTooLarge()
        return bytes(cached)
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > cap:
            raise CallbackBodyTooLarge()
        chunks.append(chunk)
    body = b"".join(chunks)
    with suppress(Exception):
        request._body = body
    return body


def _extension_error_codes(response: Any) -> list[str]:
    normalized = normalize_extension_response(_as_dict(response))
    codes: list[str] = []
    for item in normalized.errors:
        code = _normalize_text(item.get("code"))
        if code:
            codes.append(code)
    direct_error = _normalize_text(_as_dict(response).get("error"))
    if direct_error:
        codes.append(direct_error)
    return codes


class ExtensionCallbackDispatchService:
    def __init__(
        self,
        bl: Any,
        *,
        event_command_service_factory: Callable[[], Any] | None = None,
        connection_store: Any = None,
    ) -> None:
        self.bl = bl
        self.event_command_service_factory = event_command_service_factory
        # Project connections live on the project store, registrations and
        # installs on the identity store ``bl``; without a separate store the
        # one store has to hold both.
        self.connection_store = connection_store if connection_store is not None else bl

    def resolve_registration(self, raw_token: str) -> dict[str, Any] | None:
        if not hasattr(self.bl, "get_extension_callback_registration_by_token_hash"):
            return None
        return self.bl.get_extension_callback_registration_by_token_hash(
            callback_token_hash=callback_token_hash(raw_token)
        )

    def _secret_for_registration(self, registration: dict[str, Any]) -> str:
        return ExtensionCallbackRegistrationService(
            self.bl, project_repository=self.connection_store
        ).secret_for_registration(registration)

    async def authenticate(
        self,
        *,
        request: Request,
        registration: dict[str, Any],
        declaration: dict[str, Any],
    ) -> CallbackAuthenticationResult:
        auth = _callback_auth(declaration)
        if auth.get("type") == "none":
            return CallbackAuthenticationResult(True, 200, "")
        expected = self._secret_for_registration(registration)
        if auth.get("type") == "shared_secret_header":
            header_name = _normalize_text(auth.get("header"))
            received = _normalize_text(request.headers.get(header_name)) if header_name else ""
            accepted = bool(
                received
                and expected
                and hmac.compare_digest(received.encode("utf-8"), expected.encode("utf-8"))
            )
            return CallbackAuthenticationResult(accepted)
        if auth.get("type") == "hub_signature":
            return await self._authenticate_hub_signature(
                request=request, registration=registration, auth=auth
            )
        if auth.get("type") != "hmac_sha256":
            return CallbackAuthenticationResult(False)

        signature_header = _normalize_text(auth.get("signature_header"))
        timestamp_header = _normalize_text(auth.get("timestamp_header"))
        delivery_header = _normalize_text(auth.get("delivery_id_header"))
        signature = (
            _normalize_text(request.headers.get(signature_header)) if signature_header else ""
        )
        timestamp_text = (
            _normalize_text(request.headers.get(timestamp_header)) if timestamp_header else ""
        )
        delivery_id = (
            _normalize_text(request.headers.get(delivery_header)) if delivery_header else ""
        )
        if signature.lower().startswith("sha256="):
            signature = signature.split("=", 1)[1].strip()
        if len(signature) != 64 or not delivery_id or len(delivery_id) > 200:
            return CallbackAuthenticationResult(False, 401, _MESSAGE_INVALID_CALLBACK_SIGNATURE)
        try:
            delivered_at = int(timestamp_text)
            max_age = int(auth.get("max_age_seconds") or 300)
        except (TypeError, ValueError):
            return CallbackAuthenticationResult(False, 401, _MESSAGE_INVALID_CALLBACK_SIGNATURE)
        max_age = max(30, min(max_age, 3600))
        if abs(int(time.time()) - delivered_at) > max_age:
            return CallbackAuthenticationResult(False, 401, "Expired callback delivery")
        raw_body = await _read_capped_callback_body(request, cap=_MAX_CALLBACK_BODY_BYTES)
        canonical = f"{delivered_at}.{delivery_id}.".encode() + raw_body
        expected_signature = (
            hmac.new(expected.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
            if expected
            else ""
        )
        if not (
            signature
            and delivery_id
            and expected_signature
            and hmac.compare_digest(signature.encode("utf-8"), expected_signature.encode("utf-8"))
        ):
            return CallbackAuthenticationResult(False, 401, _MESSAGE_INVALID_CALLBACK_SIGNATURE)
        if not self.bl.claim_extension_callback_delivery(
            registration_id=_normalize_text(registration.get("id")),
            delivery_id=delivery_id,
            delivered_at=delivered_at,
        ):
            return CallbackAuthenticationResult(False, 409, "Callback delivery already processed")
        return CallbackAuthenticationResult(True, 200, "")

    def hub_challenge(self, request: Request, registration: dict[str, Any]) -> str | None:
        """Answer Meta's subscription handshake (``hub.mode=subscribe``).

        The registration's shared secret is the verify token the operator pastes
        into the provider's dashboard. Returns the challenge to echo, or ``None``
        when the request is not a valid handshake.
        """
        params = request.query_params
        challenge = _normalize_text(params.get("hub.challenge"))
        received = _normalize_text(params.get("hub.verify_token"))
        expected = self._secret_for_registration(registration)
        if (
            _normalize_text(params.get("hub.mode")) != "subscribe"
            or not challenge
            or len(challenge) > _MAX_HUB_CHALLENGE_LENGTH
            or not received
            or not expected
            or not hmac.compare_digest(received.encode("utf-8"), expected.encode("utf-8"))
        ):
            return None
        return challenge

    def _connection_secret(self, registration: dict[str, Any], secret_name: str) -> str:
        """Read one secret of the connection a registration belongs to."""
        getter = getattr(self.connection_store, "get_project_connection", None)
        if not callable(getter) or not secret_name:
            return ""
        connection = _as_dict(
            getter(
                project_id=_normalize_text(registration.get("project_id")),
                connection_id=_normalize_text(registration.get("connection_id")),
                extension_id=_normalize_text(registration.get("extension_id")),
                enabled_only=True,
            )
        )
        if not connection:
            return ""
        from core.application.extensions.operation_targets import provider_secrets_for_connection
        from core.application.management.project_connection_secrets import (
            resolve_connection_secrets,
        )

        secret_refs = _as_dict(connection.get("secret_refs"))
        secrets: dict[str, str] = {}
        if secret_refs.get("refs") and hasattr(self.bl, "cursor"):
            secrets = resolve_connection_secrets(
                self.bl,
                account_id=_normalize_text(connection.get("account_id")),
                secret_refs={"refs": _as_dict(secret_refs.get("refs"))},
            )
        for name, value in provider_secrets_for_connection(
            self.bl, connection, secret_refs
        ).items():
            secrets.setdefault(name, value)
        return _normalize_text(secrets.get(secret_name))

    async def _authenticate_hub_signature(
        self,
        *,
        request: Request,
        registration: dict[str, Any],
        auth: dict[str, Any],
    ) -> CallbackAuthenticationResult:
        """Check ``X-Hub-Signature-256``: HMAC-SHA256 of the raw body, keyed by the app secret.

        The key is a secret of the connection (``auth.secret_name``), not the
        registration's own secret: the provider signs with the secret of the app
        its operator created.
        """
        signature_header = _normalize_text(auth.get("signature_header"))
        signature = (
            _normalize_text(request.headers.get(signature_header)) if signature_header else ""
        )
        if signature.lower().startswith("sha256="):
            signature = signature.split("=", 1)[1].strip()
        app_secret = self._connection_secret(registration, _normalize_text(auth.get("secret_name")))
        if len(signature) != 64 or not app_secret:
            return CallbackAuthenticationResult(False, 401, _MESSAGE_INVALID_CALLBACK_SIGNATURE)
        raw_body = await _read_capped_callback_body(request, cap=_MAX_CALLBACK_BODY_BYTES)
        expected_signature = hmac.new(
            app_secret.encode("utf-8"), raw_body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(
            signature.lower().encode("utf-8"), expected_signature.encode("utf-8")
        ):
            return CallbackAuthenticationResult(False, 401, _MESSAGE_INVALID_CALLBACK_SIGNATURE)
        return CallbackAuthenticationResult(True, 200, "")

    def extension_for_registration(self, registration: dict[str, Any]) -> dict[str, Any] | None:
        extension_id = _normalize_text(registration.get("extension_id"))
        registration_service = ExtensionCallbackRegistrationService(
            self.bl, project_repository=self.connection_store
        )
        account_id = registration_service._registration_account_id(registration)
        extension = registration_service._extension(
            extension_id,
            account_id=account_id,
        )
        connection_id = _normalize_text(registration.get("connection_id"))
        project_id = _normalize_text(registration.get("project_id"))
        if not connection_id:
            return extension
        getter = getattr(self.connection_store, "get_project_connection", None)
        loader = getattr(self.bl, "get_extension_by_install_id", None)
        if not callable(getter) or not callable(loader):
            return None
        connection = _as_dict(
            getter(
                project_id=project_id,
                connection_id=connection_id,
                extension_id=extension_id,
                enabled_only=True,
            )
        )
        connection_account_id = _normalize_text(connection.get("account_id"))
        install_id = _normalize_text(connection.get("extension_install_id"))
        if (
            not connection
            or not install_id
            or _normalize_text(connection.get("connection_id")) != connection_id
            or _normalize_text(connection.get("project_id")) != project_id
            or _normalize_text(connection.get("account_id")) != account_id
            or _normalize_text(connection.get("extension_id")) != extension_id
        ):
            return None
        exact = _as_dict(loader(install_id, account_id=account_id or None))
        if (
            _normalize_text(exact.get("install_id") or exact.get("extension_install_id"))
            != install_id
            or _extension_runtime_id(exact) != extension_id
            or not bool(exact.get("enabled"))
        ):
            return None
        exact["extension_install_id"] = install_id
        exact["account_id"] = connection_account_id
        return exact

    def _provider_runtime_payload(self, registration: dict[str, Any]) -> dict[str, Any]:
        return ExtensionCallbackRegistrationService(
            self.bl, project_repository=self.connection_store
        )._provider_runtime_payload(registration)

    async def build_request_envelope(
        self,
        *,
        request: Request,
        registration: dict[str, Any],
        declaration: dict[str, Any],
        base_url: str | None = None,
    ) -> dict[str, Any]:
        raw_body = await _read_capped_callback_body(request, cap=_MAX_CALLBACK_BODY_BYTES)
        body_text = raw_body.decode("utf-8", errors="replace") if raw_body else ""
        parsed_json: Any = {}
        content_type = _normalize_text(request.headers.get("content-type")).lower()
        if raw_body and ("application/json" in content_type or content_type.endswith("+json")):
            try:
                parsed_json = json.loads(raw_body.decode("utf-8"))
            except Exception:
                parsed_json = {}
        parsed_json_obj = parsed_json if isinstance(parsed_json, dict) else {}
        extension_id = _normalize_text(registration.get("extension_id"))
        callback_id = _normalize_text(registration.get("callback_id"))
        auth = _callback_auth(declaration)
        auth_header = _normalize_text(auth.get("header") or auth.get("signature_header")).lower()
        headers = dict(request.headers)
        if auth_header:
            headers = {
                key: (CALLBACK_TOKEN_REDACTION if key.lower() == auth_header else value)
                for key, value in headers.items()
            }
        provider = self._provider_runtime_payload(registration)
        request_payload = {
            "method": request.method.upper(),
            "path": redacted_callback_path(extension_id, callback_id),
            "headers": headers,
            "query": dict(request.query_params),
            "json": parsed_json_obj,
            "body_text": body_text,
            "raw_body_base64": base64.b64encode(raw_body).decode("ascii"),
        }
        if provider:
            request_payload["provider"] = provider
        return {
            "callback": {
                "extension_id": extension_id,
                "callback_id": callback_id,
                "authenticated": True,
                "callback_url": redacted_callback_url(
                    base_url=base_url or _base_url_from_request(request),
                    extension_id=extension_id,
                    callback_id=callback_id,
                ),
                "project_id": _normalize_text(registration.get("project_id")),
                "connection_id": _normalize_text(registration.get("connection_id")),
            },
            "request": request_payload,
        }

    def dispatch(
        self,
        *,
        extension: dict[str, Any],
        declaration: dict[str, Any],
        envelope: dict[str, Any],
        scope: ScopeContext | None,
    ) -> tuple[dict[str, Any], int]:
        from core.application.management.command_services import (
            _route_extension_event_through_event_command_service,
        )
        from core.infrastructure.extension_runtime.extension_runtime import (
            emit_extension_event,
            run_extension_action,
        )

        action_id = _normalize_text(declaration.get("action_id"))
        callback_info = _as_dict(envelope.get("callback"))
        extension_install_id = _normalize_text(
            extension.get("extension_install_id") or extension.get("install_id")
        )
        trusted_account_id = (
            _normalize_text(extension.get("account_id"))
            or _normalize_text(getattr(scope, "account_id", None))
            or DEFAULT_ACCOUNT_ID
        )
        callback_project_id = _normalize_text(callback_info.get("project_id"))
        action_scope = ScopeContext(
            account_id=trusted_account_id,
            principal_type=scope.principal_type if scope else None,
            actor_id=scope.actor_id if scope else None,
            project_id=callback_project_id,
            workflow_id=_normalize_text(getattr(scope, "workflow_id", None)) or None,
        )
        invocation = run_extension_action(
            extension,
            action_id=action_id,
            input_payload=envelope,
            scope=action_scope,
            runtime_context={
                "logger": self.bl,
                "project_id": callback_project_id,
                "extension_install_id": extension_install_id,
                "account_id": trusted_account_id,
            },
        )
        _apply_project_state_updates(
            invocation,
            extension=extension,
            bl=self.bl,
            scope=action_scope,
            project_id=callback_project_id,
            connection_id=_normalize_text(callback_info.get("connection_id")) or None,
            extension_install_id=extension_install_id or None,
            account_id=trusted_account_id,
        )
        if invocation.get("status") != "succeeded":
            return {
                "ok": False,
                "error": _normalize_text(invocation.get("detail"))
                or "Extension callback action failed",
            }, 502

        response = normalize_extension_response(_as_dict(invocation.get("response")))
        if not response.ok:
            error_codes = set(_extension_error_codes(invocation.get("response")))
            if error_codes & NON_RETRYABLE_CALLBACK_ERROR_CODES:
                return response.model_dump(), 200
            return response.model_dump(), 502

        registration_project_id = _normalize_text(callback_info.get("project_id"))
        emitted = 0
        for event in response.events:
            event_project_id = registration_project_id
            if not event_project_id:
                return {"ok": False, "error": "project_id is required for event emission"}, 400
            event_payload = dict(event)
            event_payload["project_id"] = event_project_id
            event_scope = ScopeContext(
                account_id=trusted_account_id,
                principal_type=scope.principal_type if scope else None,
                actor_id=scope.actor_id if scope else None,
                project_id=event_project_id,
                workflow_id=_normalize_text(event.get("workflow_id")) or None,
            )
            if _route_extension_event_through_event_command_service(
                extension_id=extension.get("extension_key") or extension.get("extension_id") or "",
                event=event_payload,
                scope=event_scope,
                runtime_context={
                    "logger": self.bl,
                    "project_id": event_project_id,
                    "extension_install_id": extension_install_id,
                    "account_id": trusted_account_id,
                },
                event_command_service_factory=self.event_command_service_factory,
            ):
                emitted += 1
                continue
            emit_extension_event(
                extension,
                event_type=_normalize_text(event.get("event_type")),
                correlation_id=_normalize_text(event.get("correlation_id")),
                scope=event_scope,
                summary=_normalize_text(event.get("summary")),
                severity=_normalize_text(event.get("severity")) or "info",
                payload=_as_dict(event.get("payload")),
                project_id=event_project_id,
                workflow_id=_normalize_text(event.get("workflow_id")) or None,
                job_id=event.get("job_id"),
                source=_normalize_text(event.get("source")) or "extension_callback",
                metadata=_as_dict(event.get("metadata")),
                runtime_context={
                    "logger": self.bl,
                    "extension_install_id": extension_install_id,
                    "account_id": trusted_account_id,
                },
            )
            emitted += 1

        return {
            "ok": True,
            "result": response.result,
            "events_emitted": emitted,
        }, 200
