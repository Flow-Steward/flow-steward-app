"""Extension provider webhook security operations."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from core.application.extensions.provider_runtime import _as_dict, _as_str
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

_MESSAGE_INVALID_WEBHOOK_SIGNATURE = "Invalid webhook signature"


def resolve_provider_webhook_secret(
    provider_key: str,
    *,
    bl=None,
    project_id: str | None = None,
    include_global: bool = True,
) -> str:
    normalized_provider_key = _as_str(provider_key).lower()
    secret = ""
    if bl and hasattr(bl, "get_provider_secret"):
        try:
            raw_secret = bl.get_provider_secret(
                provider_key=normalized_provider_key,
                secret_key="webhook_secret",  # pragma: allowlist secret
                project_id=(str(project_id) if project_id is not None else None),
                include_global=include_global,
            )
            secret = _as_str(raw_secret) if isinstance(raw_secret, str) else ""
        except Exception:
            secret = ""
    return secret


def resolve_provider_webhook_project_secret(
    provider_key: str,
    *,
    project_id: str,
    bl=None,
) -> str:
    """Resolve a *project-scoped* webhook secret only (no provider-global fallback).

    Returns the empty string when no project-bound secret is configured for the
    resolved routing project, so callers can distinguish "no per-project secret"
    from "project secret present".
    """
    normalized_project_id = _as_str(project_id).strip()
    if not normalized_project_id:
        return ""
    return resolve_provider_webhook_secret(
        provider_key,
        bl=bl,
        project_id=normalized_project_id,
        include_global=False,
    )


def verify_hmac_signature(raw_body: bytes, provided_signature: str, secret: str) -> bool:
    payload = bytes(raw_body or b"")
    provided = _as_str(provided_signature)
    secret_value = _as_str(secret)
    if not payload or not provided or not secret_value:
        return False
    if "=" in provided:
        _prefix, _sep, remainder = provided.partition("=")
        if remainder:
            provided = remainder
    expected = hmac.new(secret_value.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided.lower(), expected.lower())


def enforce_provider_webhook_signature(
    provider_key: str,
    *,
    request_headers: dict[str, Any] | None,
    raw_body: bytes,
    bl=None,
    required: bool = False,
    match_required: bool = True,
) -> tuple[bool, str | None]:
    """Pre-routing webhook authentication gate.

    Verifies the request signature against the provider-global webhook secret.

    ``match_required`` controls whether a *value* mismatch is fatal here. When the
    caller will re-verify the signature against the resolved routing project's
    effective secret afterwards (see :func:`enforce_provider_webhook_project_secret`),
    pass ``match_required=False`` so this stays a cheap pre-routing gate (secret
    configured + signature header present) and DEFERS the authoritative value check
    to the post-routing step. This is what lets a legitimate request signed with a
    *per-project* secret (which intentionally differs from the global secret) pass
    the gate and be authenticated against the project secret after routing.
    """
    normalized_provider_key = _as_str(provider_key).lower()
    secret = resolve_provider_webhook_secret(normalized_provider_key, bl=bl)
    if not secret:
        if required:
            return False, f"{normalized_provider_key} webhook secret is not configured"
        return True, None

    headers = {
        str(key or "").strip().lower(): _as_str(value)
        for key, value in _as_dict(request_headers).items()
        if _as_str(key)
    }
    candidate_header_names = [
        "x-signature",
        f"x-{normalized_provider_key}-signature",
        f"{normalized_provider_key}-hook-signature",
        f"x-{normalized_provider_key}-hook-signature",
    ]
    signature = next(
        (headers.get(name, "") for name in candidate_header_names if headers.get(name, "")), ""
    )
    if not signature:
        return False, "Missing webhook signature"
    if not match_required:
        # Defer the authoritative value check to the post-routing effective-secret
        # verification; the request might be signed with the routing project's
        # per-project secret rather than the global one.
        return True, None
    if not verify_hmac_signature(raw_body, signature, secret):
        return False, _MESSAGE_INVALID_WEBHOOK_SIGNATURE
    return True, None


def _extract_webhook_signature(
    request_headers: dict[str, Any] | None,
    normalized_provider_key: str,
) -> str:
    headers = {
        str(key or "").strip().lower(): _as_str(value)
        for key, value in _as_dict(request_headers).items()
        if _as_str(key)
    }
    candidate_header_names = [
        "x-signature",
        f"x-{normalized_provider_key}-signature",
        f"{normalized_provider_key}-hook-signature",
        f"x-{normalized_provider_key}-hook-signature",
    ]
    return next(
        (headers.get(name, "") for name in candidate_header_names if headers.get(name, "")), ""
    )


def provider_webhook_requires_project_secret() -> bool:
    """Opt-in hardening: require payload/query-routed provider webhooks to present a
    per-project secret, rejecting projects that have none.

    Defaults to False. By default the post-routing check verifies the signature
    against the routing project's *effective* secret (the per-project secret when
    configured, otherwise the provider-global secret). A project that DOES configure
    its own webhook secret is fully bound to it — a globally-signed request can no
    longer be routed into that project (FS-DEEP-CAND-069 closed for those projects).
    Projects without a per-project secret still accept the global secret (the
    documented residual), so existing global-secret-only deployments keep working.

    Operators who provision per-project secrets everywhere can set
    ``FS_PROVIDER_WEBHOOK_REQUIRE_PROJECT_SECRET=1`` to additionally reject any
    payload-routed project that lacks its own secret (full fail-closed binding).
    """
    # Read via a direct os.getenv literal (not the env_flag wrapper) so the env
    # audit scanner attributes this code-read location to the variable.
    raw = _runtime_env.get("FS_PROVIDER_WEBHOOK_REQUIRE_PROJECT_SECRET")
    if raw is None:
        return False
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def enforce_provider_webhook_project_secret(
    provider_key: str,
    *,
    project_id: str | None,
    request_headers: dict[str, Any] | None,
    raw_body: bytes,
    bl=None,
    require_project_secret: bool | None = None,
) -> tuple[bool, str | None]:
    """Authoritative post-routing webhook signature check bound to the routing
    project (FS-DEEP-CAND-069).

    The generic webhook intake gates the request pre-routing (secret configured +
    signature present) but defers the authoritative value check to here, once the
    routing project — possibly selected from attacker-controlled payload/query
    hints — is known. The signature is verified against the project's *effective*
    secret:

    * If the routing project has its OWN per-project secret, the signature MUST
      match it. The provider-global secret is NOT sufficient, so a request signed
      with the global (or another project's) secret cannot be routed into a project
      that has configured its own secret.
    * Otherwise the signature is verified against the provider-global secret (the
      pre-routing gate deferred this value check). This keeps global-secret-only
      deployments working; it is the documented residual that a globally-signed
      request can still reach a project that has not configured its own secret.

    ``require_project_secret`` (default from
    :func:`provider_webhook_requires_project_secret`) optionally hardens the
    no-per-project-secret case to fail closed instead of falling back to global.
    """
    normalized_provider_key = _as_str(provider_key).lower()
    normalized_project_id = _as_str(project_id).strip()
    if not normalized_project_id:
        return True, None
    if require_project_secret is None:
        require_project_secret = provider_webhook_requires_project_secret()
    project_secret = resolve_provider_webhook_project_secret(
        normalized_provider_key,
        project_id=normalized_project_id,
        bl=bl,
    )
    if project_secret:
        effective_secret = project_secret
        mismatch_error = "Webhook signature does not match routing project secret"
    else:
        if require_project_secret:
            return False, (
                "Routing project has no configured webhook secret; refusing to route "
                "a globally-signed webhook into a payload-selected project"
            )
        effective_secret = resolve_provider_webhook_secret(normalized_provider_key, bl=bl)
        if not effective_secret:
            # No secret configured at all; the pre-routing gate (required=True)
            # already rejected this case, so treat as a no-op here.
            return True, None
        mismatch_error = _MESSAGE_INVALID_WEBHOOK_SIGNATURE
    signature = _extract_webhook_signature(request_headers, normalized_provider_key)
    if not signature:
        return False, "Missing webhook signature for routing project"
    if not verify_hmac_signature(raw_body, signature, effective_secret):
        return False, mismatch_error
    return True, None


def enforce_provider_webhook_effective_signature(
    provider_key: str,
    *,
    project_id: str | None,
    request_headers: dict[str, Any] | None,
    raw_body: bytes,
    bl=None,
) -> tuple[bool, str | None]:
    """Authoritative pre-side-effect webhook signature verification.

    Verifies the request signature against the routing project's *effective* secret
    BEFORE any extension action / side effect runs (no ``run_extension_action``
    before an HMAC value check). When a routing project is known and has its own
    per-project secret the signature must match it; otherwise it must match the
    provider-global secret.

    Unlike :func:`enforce_provider_webhook_project_secret`, this NEVER no-ops on a
    missing/unknown project — when the project is not yet known (e.g. a task
    provider whose project is only resolved by the extension), it falls back to a
    strict global-secret value check so that no extension runs on an
    unauthenticated request. The per-project binding for the *final* routing
    project is re-checked afterwards via
    :func:`enforce_provider_webhook_project_secret`.
    """
    normalized_provider_key = _as_str(provider_key).lower()
    normalized_project_id = _as_str(project_id).strip()
    project_secret = (
        resolve_provider_webhook_project_secret(
            normalized_provider_key,
            project_id=normalized_project_id,
            bl=bl,
        )
        if normalized_project_id
        else ""
    )
    effective_secret = project_secret or resolve_provider_webhook_secret(
        normalized_provider_key, bl=bl
    )
    if not effective_secret:
        # No webhook secret configured at all; the pre-routing gate (required=True)
        # already rejected this case.
        return True, None
    signature = _extract_webhook_signature(request_headers, normalized_provider_key)
    if not signature:
        return False, "Missing webhook signature"
    if not verify_hmac_signature(raw_body, signature, effective_secret):
        return False, _MESSAGE_INVALID_WEBHOOK_SIGNATURE
    return True, None
