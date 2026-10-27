from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any

from fastapi import Request

from core.application.webhooks.signature_verifiers import (
    WEBHOOK_SIGNATURE_AUTH_MODE,
    verify_webhook_signature,
)
from core.application.workflows.workflow_entrypoint_auth import (
    WorkflowEntrypointAuthOutcome,
    WorkflowEntrypointAuthRejectionReason,
)

from .workflow_entrypoints_common import (
    INVALID_BASIC_AUTH_CREDENTIALS_VALUE,
    SHA256_VALUE,
)

#: Auth modes whose validation needs the exact raw request body, so the route
#: must read or stage the body *before* it authenticates.
BODY_DEPENDENT_AUTH_MODES = frozenset({"hmac_sha256", WEBHOOK_SIGNATURE_AUTH_MODE})


def is_body_dependent_auth_mode(auth_mode: str) -> bool:
    """Return True when authenticating this mode requires the raw request body."""
    return str(auth_mode or "").strip() in BODY_DEPENDENT_AUTH_MODES


_REPLAY_KEY_PREFIXES = {
    "hmac_sha256": "hmac-sha256",
    WEBHOOK_SIGNATURE_AUTH_MODE: "webhook-signature",
}

_AUTH_REJECTION_HTTP_STATUS = {
    WorkflowEntrypointAuthRejectionReason.AUTH_FAILED: 401,
    WorkflowEntrypointAuthRejectionReason.AUTH_MODE_NOT_CONFIGURED: 503,
    WorkflowEntrypointAuthRejectionReason.CREDENTIAL_NOT_CONFIGURED: 503,
}


def workflow_entrypoint_auth_http_status(outcome: WorkflowEntrypointAuthOutcome) -> int:
    """Map semantic application auth failures at the HTTP adapter boundary."""
    if outcome.ok:
        return 200
    return _AUTH_REJECTION_HTTP_STATUS.get(
        outcome.reason or WorkflowEntrypointAuthRejectionReason.AUTH_FAILED,
        401,
    )


def _validate_workflow_entrypoint_auth(
    *,
    bl: Any,
    project_id: str | None,
    clean_entry_key: str,
    selected_workflow_id: str,
    entrypoint: dict[str, Any],
    request: Request,
    raw_body: bytes,
    auth_mode: str,
    response_mode: Any,
    raw_body_file: Any | None = None,
) -> WorkflowEntrypointAuthOutcome:
    _ = clean_entry_key, selected_workflow_id, response_mode
    auth_config = dict(entrypoint.get("auth_config_jsonb") or {})
    supported_auth_modes = {
        "bearer_token",
        "shared_secret_header",
        "basic_auth",
        "hmac_sha256",
        WEBHOOK_SIGNATURE_AUTH_MODE,
    }
    if not auth_mode or auth_mode not in supported_auth_modes:
        return WorkflowEntrypointAuthOutcome.rejected(
            error="Webhook authentication mode is not configured",
            reason=WorkflowEntrypointAuthRejectionReason.AUTH_MODE_NOT_CONFIGURED,
        )
    secret_ref = str(
        auth_config.get("secret_ref")
        or auth_config.get("password_secret_ref")
        or entrypoint.get("secret_ref")
        or ""
    ).strip()
    expected_secret = ""
    if auth_mode and secret_ref and hasattr(bl, "get_project_secret"):
        try:
            expected_secret = str(
                bl.get_project_secret(
                    project_id=str(project_id),
                    secret_key=secret_ref,
                )
                or ""
            ).strip()
        except Exception:
            expected_secret = ""
    if auth_mode and not expected_secret:
        return WorkflowEntrypointAuthOutcome.rejected(
            error="Webhook credential is not configured",
            reason=WorkflowEntrypointAuthRejectionReason.CREDENTIAL_NOT_CONFIGURED,
        )
    if auth_mode == "bearer_token":
        auth_header = str(request.headers.get("authorization") or "").strip()
        received_token = (
            auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
        )
        if not received_token or not hmac.compare_digest(received_token, expected_secret):
            return WorkflowEntrypointAuthOutcome.rejected(error="Invalid bearer token")
    elif auth_mode == "shared_secret_header":
        header_name = str(auth_config.get("header_name") or "X-Workflow-Secret").strip()
        received_secret = str(request.headers.get(header_name) or "").strip()
        if not received_secret or not hmac.compare_digest(received_secret, expected_secret):
            return WorkflowEntrypointAuthOutcome.rejected(
                error="Invalid workflow entrypoint secret"
            )
    elif auth_mode == "basic_auth":
        auth_header = str(request.headers.get("authorization") or "").strip()
        if not auth_header.lower().startswith("basic "):
            return WorkflowEntrypointAuthOutcome.rejected(
                error=INVALID_BASIC_AUTH_CREDENTIALS_VALUE
            )
        try:
            decoded = base64.b64decode(auth_header[6:].strip()).decode("utf-8")
            received_username, _, received_password = decoded.partition(":")
        except Exception:
            return WorkflowEntrypointAuthOutcome.rejected(
                error=INVALID_BASIC_AUTH_CREDENTIALS_VALUE
            )
        expected_username = str(auth_config.get("username") or "workflow").strip()
        if not hmac.compare_digest(received_username, expected_username) or not hmac.compare_digest(
            received_password,
            expected_secret,
        ):
            return WorkflowEntrypointAuthOutcome.rejected(
                error=INVALID_BASIC_AUTH_CREDENTIALS_VALUE
            )
    elif auth_mode == "hmac_sha256":
        header_name = str(auth_config.get("header_name") or "X-Workflow-Signature").strip()
        prefix = str(auth_config.get("prefix") or SHA256_VALUE).strip()
        received_signature = str(request.headers.get(header_name) or "").strip()
        if prefix and received_signature.startswith(prefix):
            received_signature = received_signature[len(prefix) :]
        signer = hmac.new(expected_secret.encode("utf-8"), digestmod=hashlib.sha256)
        if raw_body_file is not None:
            raw_body_file.seek(0)
            while chunk := raw_body_file.read(1024 * 1024):
                signer.update(chunk)
            raw_body_file.seek(0)
        else:
            signer.update(raw_body)
        expected_signature = signer.hexdigest()
        if not received_signature or not hmac.compare_digest(
            received_signature, expected_signature
        ):
            return WorkflowEntrypointAuthOutcome.rejected(error="Invalid HMAC signature")
    elif auth_mode == WEBHOOK_SIGNATURE_AUTH_MODE:
        verification = verify_webhook_signature(
            headers=dict(request.headers),
            secret=expected_secret,
            config=auth_config,
            raw_body=raw_body,
            raw_body_file=raw_body_file,
        )
        if not verification.ok:
            if verification.reason == "config_invalid":
                # A stored verifier config that no longer normalizes is a server
                # defect, not a caller error: fail closed with a service error.
                return WorkflowEntrypointAuthOutcome.rejected(
                    error="Webhook signature verification is not configured correctly",
                    reason=WorkflowEntrypointAuthRejectionReason.AUTH_MODE_NOT_CONFIGURED,
                )
            return WorkflowEntrypointAuthOutcome.rejected(error=str(verification.error))
    return WorkflowEntrypointAuthOutcome.accepted()


def _presented_secret_material(
    auth_mode: str, request: Request, auth_config: dict[str, Any]
) -> str:
    """Return the credential material presented on the request for replay keying.

    The value is hashed into the replay key only; it is never logged or returned.
    Two identical deliveries (same credential + same body) produce the same key,
    so a captured-and-replayed reusable-secret webhook collides with the original
    reservation and is rejected.
    """
    if auth_mode == "bearer_token":
        return str(request.headers.get("authorization") or "").strip()
    if auth_mode == "shared_secret_header":
        header_name = str(auth_config.get("header_name") or "X-Workflow-Secret").strip()
        return str(request.headers.get(header_name) or "").strip()
    if auth_mode == "basic_auth":
        return str(request.headers.get("authorization") or "").strip()
    if auth_mode == WEBHOOK_SIGNATURE_AUTH_MODE:
        signature = auth_config.get("signature")
        signature = signature if isinstance(signature, dict) else {}
        header_name = str(signature.get("header") or "").strip()
        return str(request.headers.get(header_name) or "").strip() if header_name else ""
    # hmac_sha256
    header_name = str(auth_config.get("header_name") or "X-Workflow-Signature").strip()
    return str(request.headers.get(header_name) or "").strip()


def _derive_secret_replay_key(
    *,
    auth_mode: str,
    entrypoint: dict[str, Any],
    request: Request,
    raw_body: bytes,
    selected_workflow_id: str,
    raw_body_sha256: str = "",
) -> str:
    """Derive a deterministic replay-protection idempotency key for secret auth.

    Applies to hmac_sha256 (which previously had this protection inline) and to
    reusable-secret modes (bearer_token / shared_secret_header / basic_auth) when
    the operator did not configure header idempotency. Closes FS-DEEP-CAND-052/054
    by failing closed: a replayed delivery reuses the same credential + body and
    therefore the same reservation key, which is rejected as a duplicate.
    """
    auth_config = dict(entrypoint.get("auth_config_jsonb") or {})
    secret_material = _presented_secret_material(auth_mode, request, auth_config)
    digest = hashlib.sha256(
        "|".join(
            (
                str(auth_mode or ""),
                str(selected_workflow_id or ""),
                str(raw_body_sha256 or hashlib.sha256(raw_body).hexdigest()),
                hashlib.sha256(secret_material.encode("utf-8")).hexdigest(),
            )
        ).encode("utf-8")
    ).hexdigest()
    prefix = _REPLAY_KEY_PREFIXES.get(auth_mode, "reusable-secret")
    return f"{prefix}:{digest}"


__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
