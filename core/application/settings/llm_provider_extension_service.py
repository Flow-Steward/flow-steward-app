"""Operator service for LLM provider extensions: discover, group, persist models.

Pure helpers (stable provider_key derivation, upstream-owner grouping, display
label composition from descriptor-provided supplier/owner labels) plus a refresh
entrypoint that calls the extension's ``list_models`` query in the install's
own runtime (an extension subprocess, or a remote HTTPS runtime where one is
configured) and persists discovered models. Credentials reuse the account-scoped
``secret_refs`` store. No vendor labels are hardcoded here — labels are composed
from the descriptor's ``supplier_label`` / ``upstream_owner_label``.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Callable
from typing import Any

from core.application.settings import llm_provider_extension_repository as store

CREDENTIAL_REF_PREFIX = "llmext_cred_"
PROVIDER_KEY_PREFIX = "llmext_"
_PAGINATION_CAP = 20
_LIST_MODELS_LIMIT = 200
_MAX_DISCOVERY_MODELS = 1_000
_MAX_DISCOVERY_BYTES = 5 * 1024 * 1024
_MAX_MODEL_METADATA_BYTES = 64 * 1024
_DISCOVERY_TIME_BUDGET_SECONDS = 60
_ENDPOINT_GROUPING = {"endpoint", "connection", "install", "none"}


def _slug(value: Any, *, max_len: int = 0) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    if max_len and len(text) > max_len:
        text = text[:max_len].strip("_")
    return text


def credential_ref_key(install_id: str) -> str:
    """Account-scoped secret_refs key for an install's upstream credentials."""
    normalized = _slug(install_id, max_len=40) or "install"
    digest = hashlib.sha256(str(install_id).encode("utf-8")).hexdigest()[:12]
    return f"{CREDENTIAL_REF_PREFIX}{normalized}_{digest}"


# Labels the LLM provider panel shows for contract fields; messages use them so
# users never read internal field names.
_FIELD_LABELS = {
    "upstream_base_url": "Base URL",
    "api_key": "API key",  # pragma: allowlist secret
    "runtime_bearer_token": "Runtime bearer token",
}


def llm_field_label(name: Any) -> str:
    text = str(name or "").strip()
    if text in _FIELD_LABELS:
        return _FIELD_LABELS[text]
    words = text.replace("_", " ")
    return words[:1].upper() + words[1:]


def require_llm_provider_connection_config(
    contract: Any,
    value: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return one complete effective config or reject a missing required field."""
    from core.application.extensions.extension_descriptor import canonical_llm_field_name

    effective = dict(value or {})
    required = sorted(
        {
            canonical_llm_field_name(field)
            for field in (getattr(contract, "required_connection_config", None) or [])
            if str(field).strip()
        }
    )
    missing = [field for field in required if not str(effective.get(field) or "").strip()]
    if missing:
        labels = ", ".join(llm_field_label(field) for field in missing)
        raise ValueError(f"Fill in the required setting: {labels}")
    return effective


def declared_connection_config_fields(contract: Any) -> set[str]:
    """Every plaintext connection field the manifest declares, required or optional."""
    from core.application.extensions.extension_descriptor import canonical_llm_field_name

    return {
        canonical_llm_field_name(field)
        for field in [
            *(getattr(contract, "required_connection_config", None) or []),
            *(getattr(contract, "optional_connection_config", None) or []),
        ]
        if str(field).strip()
    }


def _safe_failure_category(invocation: dict[str, Any]) -> str:
    from core.infrastructure.db.remote_extension_runtime import safe_remote_error_category

    raw_response = invocation.get("response")
    response: dict[str, Any] = raw_response if isinstance(raw_response, dict) else {}
    raw_errors = response.get("errors")
    errors: list[Any] = raw_errors if isinstance(raw_errors, list) else []
    first_error: dict[str, Any] = errors[0] if errors and isinstance(errors[0], dict) else {}
    raw_error = response.get("error")
    error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
    return safe_remote_error_category(
        first_error.get("code"),
        error.get("code"),
        response.get("provider_error_code"),
        invocation.get("detail"),
    )


def _invoke_in_process(
    extension: dict[str, Any],
    *,
    op_kind: str,
    op_id: str,
    payload: dict[str, Any],
    account_id: str,
    credentials: dict[str, Any],
    connection_config: dict[str, Any],
    timeout_seconds: float | None,
    get_logger_only: Callable[[], Any] | None,
) -> dict[str, Any]:
    from core.infrastructure.extension_runtime.extension_runtime_entrypoints import (
        run_extension_action,
        run_extension_query,
    )
    from core.infrastructure.extension_runtime.extension_runtime_helpers import (
        resolve_extension_runtime_record_by_install_id,
    )

    install_id = str(
        extension.get("install_id") or extension.get("extension_install_id") or ""
    ).strip()
    record, _ = resolve_extension_runtime_record_by_install_id(
        install_id, get_logger_only=get_logger_only, account_id=account_id
    )
    if not record:
        return {"status": "failed", "detail": "model_unavailable", "response": {}}
    # The provider's own account-scoped settings reach its subprocess as
    # host-authorized resource snapshots, never as part of the operation input.
    from core.application.extensions.url_safety import allow_private_provider_addresses

    runtime_context = {
        "account_id": account_id,
        "extension_install_id": install_id,
        "resources": {
            "provider_secrets": dict(credentials),
            "connection_config": dict(connection_config),
        },
        # The host's network decision, so the extension's own URL guard follows
        # the administrator's setting without reading host environment.
        "network_policy": {"allow_private_addresses": allow_private_provider_addresses()},
    }
    timeout = max(1, math.ceil(timeout_seconds)) if timeout_seconds else None
    context = {"account_id": account_id}
    if op_kind == "query":
        invocation = run_extension_query(
            record,
            query_id=op_id,
            context=context,
            params=payload,
            timeout_seconds=timeout,
            runtime_context=runtime_context,
        )
    else:
        invocation = run_extension_action(
            record,
            action_id=op_id,
            context=context,
            input_payload=payload,
            timeout_seconds=timeout,
            runtime_context=runtime_context,
        )
    if invocation.get("status") == "succeeded":
        return {"status": "succeeded", "detail": "", "response": invocation.get("response")}
    return {"status": "failed", "detail": _safe_failure_category(invocation), "response": {}}


def invoke_llm_provider_operation(
    extension: dict[str, Any],
    *,
    op_kind: str,
    op_id: str,
    payload: dict[str, Any],
    account_id: str,
    credentials: dict[str, Any] | None = None,
    connection_config: dict[str, Any] | None = None,
    timeout_seconds: float | None = None,
    idempotency_key: str = "",
    get_logger_only: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Run one llm_provider query or action in the install's own runtime mode.

    Returns ``{"status", "detail", "response"}``; a failed ``detail`` is one
    safe error category.  A remote HTTPS runtime keeps its host-to-runtime
    bearer; an in-process provider receives only its declared provider
    secrets and connection config.
    """
    from core.infrastructure.db.remote_extension_runtime import (
        is_remote_runtime_enabled,
        remote_action,
        remote_query,
    )

    runtime_credentials = dict(credentials or {})
    runtime_bearer_token = str(runtime_credentials.pop("runtime_bearer_token", "") or "").strip()
    config = dict(connection_config or {})
    if not is_remote_runtime_enabled(extension):
        return _invoke_in_process(
            extension,
            op_kind=op_kind,
            op_id=op_id,
            payload=payload,
            account_id=account_id,
            credentials=runtime_credentials,
            connection_config=config,
            timeout_seconds=timeout_seconds,
            get_logger_only=get_logger_only,
        )
    remote = dict(extension)
    if runtime_bearer_token:
        remote["remote_bearer_token"] = runtime_bearer_token
        remote["handshake_status"] = "unknown"
        remote["health_status"] = "unknown"
    if runtime_credentials:
        remote["connection_credentials"] = runtime_credentials
    if config:
        remote["connection_config"] = config
    context = {"account_id": account_id}
    if op_kind == "query":
        return remote_query(
            remote,
            query_id=op_id,
            query_payload=payload,
            context=context,
            runtime_context=context,
            timeout_seconds=timeout_seconds,
        )
    return remote_action(
        remote,
        action_id=op_id,
        input_payload=payload,
        context=context,
        runtime_context=context,
        timeout_seconds=timeout_seconds,
        idempotency_key=idempotency_key,
    )


def derive_provider_key(*, install_id: str, supplier_key: str, owner_key: str | None) -> str:
    """Stable provider_key saved in configs (never derived from display labels)."""
    supplier = _slug(supplier_key) or "ext"
    owner = _slug(owner_key) or "default"
    install = _slug(install_id, max_len=16) or "install"
    identity = "\x1f".join((str(supplier_key), str(owner_key or ""), str(install_id)))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return f"{PROVIDER_KEY_PREFIX}{supplier}_{owner}_{install}_{digest}"


def compose_group_label(
    *,
    strategy: str,
    supplier_label: str,
    owner_label: str | None,
    install_display_name: str,
) -> str:
    """Compose a provider group display label from descriptor metadata.

    - upstream_owner aggregator: ``"Model Owner (Aggregator)"`` (owner ≠ supplier)
    - direct official provider:  the supplier/owner label
    - endpoint/connection (e.g. a self-hosted runtime): ``"<supplier>: <connection display name>"``
    """
    supplier = str(supplier_label or "").strip() or "Extension"
    if str(strategy or "").strip().lower() in _ENDPOINT_GROUPING:
        name = str(install_display_name or "").strip()
        return f"{supplier}: {name}" if name else supplier
    owner = str(owner_label or "").strip()
    if owner and owner != supplier:
        return f"{owner} ({supplier})"
    return owner or supplier


def _model_supports_structured(model: dict[str, Any], capabilities: list[str]) -> bool:
    if bool(model.get("supports_structured_output")):
        return True
    return "structured_output" in {str(c).strip() for c in capabilities}


def _canonical_capabilities(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise RuntimeError("invalid_response: model capabilities must be an array")
    normalized: set[str] = set()
    for capability in value:
        if not isinstance(capability, str) or not capability.strip():
            raise RuntimeError("invalid_response: model capabilities must be non-empty strings")
        normalized.add(capability.strip())
    return sorted(normalized)


def group_discovered_models(
    contract: Any,
    models: list[dict[str, Any]],
    *,
    install_id: str,
    install_display_name: str = "",
) -> list[dict[str, Any]]:
    """Group provider-returned models into provider groups per the contract.

    Returns a list of group dicts ready for persistence. ``model_id`` is kept
    exactly as returned by the provider.
    """
    grouping = getattr(contract, "model_grouping", None)
    strategy = str(getattr(grouping, "strategy", "upstream_owner") or "upstream_owner")
    owner_key_field = str(getattr(grouping, "owner_key_field", "upstream_owner_key"))
    owner_label_field = str(getattr(grouping, "owner_label_field", "upstream_owner_label"))
    by_owner = strategy.strip().lower() not in _ENDPOINT_GROUPING

    supplier_key = str(getattr(contract, "supplier_key", "") or "")
    supplier_label = str(getattr(contract, "supplier_label", "") or "")

    groups: dict[str, dict[str, Any]] = {}
    for model in models or []:
        model_id = str((model or {}).get("model_id") or "").strip()
        if not model_id:
            continue
        owner_key = str((model or {}).get(owner_key_field) or "").strip() if by_owner else ""
        owner_label = str((model or {}).get(owner_label_field) or "").strip() if by_owner else ""
        group_key = owner_key or "default"
        if group_key not in groups:
            provider_key = derive_provider_key(
                install_id=install_id, supplier_key=supplier_key, owner_key=owner_key
            )
            label = compose_group_label(
                strategy=strategy,
                supplier_label=supplier_label,
                owner_label=owner_label,
                install_display_name=install_display_name,
            )
            groups[group_key] = {
                "provider_key": provider_key,
                "display_label": label,
                "supplier_key": supplier_key,
                "supplier_label": supplier_label,
                "upstream_owner_key": owner_key or None,
                "upstream_owner_label": owner_label or None,
                "models": [],
            }
        capabilities = _canonical_capabilities((model or {}).get("capabilities"))
        supports_structured_output = _model_supports_structured(model, capabilities)
        if supports_structured_output:
            capabilities = sorted({*capabilities, "structured_output"})
        groups[group_key]["models"].append(
            {
                "model_id": model_id,  # exact provider value — never rewritten
                "display_label": str((model or {}).get("label") or model_id),
                "upstream_owner_key": owner_key or None,
                "upstream_owner_label": owner_label or None,
                "capabilities": capabilities,
                "supports_structured_output": supports_structured_output,
                "metadata": (model or {}).get("metadata") or {},
                "upstream_revision": str((model or {}).get("upstream_revision") or "") or None,
                "upstream_digest": str((model or {}).get("upstream_digest") or "") or None,
            }
        )
    return list(groups.values())


def fetch_models_via_remote_runtime(
    install_record: dict[str, Any],
    contract: Any,
    *,
    account_id: str,
    credentials: dict[str, Any] | None,
    connection_config: dict[str, Any] | None = None,
    timeout_seconds: int | None = None,
    get_logger_only: Callable[[], Any] | None = None,
) -> list[dict[str, Any]]:
    """Invoke the extension's list_models query (paginated) in its own runtime."""
    query_id = str(getattr(contract.operations.list_models, "query_id", "") or "llm.list_models")

    collected: list[dict[str, Any]] = []
    seen_model_ids: set[str] = set()
    collected_bytes = 0
    cursor = ""
    seen_cursors: set[str] = set()
    time_budget = min(
        max(1, int(timeout_seconds or _DISCOVERY_TIME_BUDGET_SECONDS)),
        _DISCOVERY_TIME_BUDGET_SECONDS,
    )
    deadline = time.monotonic() + time_budget
    for _ in range(_PAGINATION_CAP):
        if cursor in seen_cursors:
            raise RuntimeError("invalid_response: discovery cursor repeated")
        seen_cursors.add(cursor)
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            raise RuntimeError("timeout: discovery time budget exceeded")
        invocation = invoke_llm_provider_operation(
            install_record,
            op_kind="query",
            op_id=query_id,
            payload={"cursor": cursor, "limit": _LIST_MODELS_LIMIT},
            account_id=account_id,
            credentials=credentials,
            connection_config=connection_config,
            timeout_seconds=max(1, int(remaining_seconds)),
            get_logger_only=get_logger_only,
        )
        if invocation.get("status") != "succeeded":
            raise RuntimeError(str(invocation.get("detail") or "list_models failed"))
        response = invocation.get("response")
        response = response if isinstance(response, dict) else {}
        result = response.get("result")
        result = result if isinstance(result, dict) else {}
        page = result.get("models")
        # Refresh must never reconcile a partial/malformed response into an
        # empty catalog.  Reference runtimes explicitly attest a complete
        # snapshot; anything else preserves the last successful rows.
        if result.get("snapshot_complete") is not True or not isinstance(page, list):
            raise RuntimeError(
                "invalid_response: discovery did not return a complete model snapshot"
            )
        if len(page) > _LIST_MODELS_LIMIT:
            raise RuntimeError("invalid_response: discovery page limit exceeded")
        for model in page:
            if not isinstance(model, dict):
                raise RuntimeError("invalid_response: discovery model must be an object")
            model_id = str(model.get("model_id") or "").strip()
            if not model_id:
                raise RuntimeError("invalid_response: discovery model_id is required")
            if model_id in seen_model_ids:
                raise RuntimeError("invalid_response: duplicate model_id in discovery snapshot")
            metadata = model.get("metadata") or {}
            try:
                metadata_bytes = len(
                    json.dumps(metadata, separators=(",", ":"), sort_keys=True).encode()
                )
                model_bytes = len(json.dumps(model, separators=(",", ":"), sort_keys=True).encode())
            except (TypeError, ValueError) as exc:
                raise RuntimeError("invalid_response: discovery model is not JSON-safe") from exc
            if metadata_bytes > _MAX_MODEL_METADATA_BYTES:
                raise RuntimeError("invalid_response: discovery model metadata limit exceeded")
            collected_bytes += model_bytes
            if collected_bytes > _MAX_DISCOVERY_BYTES:
                raise RuntimeError("invalid_response: discovery byte limit exceeded")
            seen_model_ids.add(model_id)
            collected.append(model)
            if len(collected) > _MAX_DISCOVERY_MODELS:
                raise RuntimeError("invalid_response: discovery model limit exceeded")
        cursor = str(result.get("next_cursor") or "").strip()
        if not cursor:
            break
    if cursor:
        raise RuntimeError("invalid_response: discovery pagination limit reached")
    return collected


def persist_provider_groups(
    conn,
    *,
    account_id: str,
    install_id: str,
    extension_id: str,
    contract: Any,
    groups: list[dict[str, Any]],
    credential_ref: str | None,
    connection_config: dict[str, Any] | None = None,
    _transaction_wrapped: bool = False,
) -> dict[str, Any]:
    """Upsert provider sources + discovered models for the given groups."""
    # A discovery response is a complete snapshot.  Never expose a partially
    # persisted reconciliation when a later source/model operation fails.
    from core.infrastructure.db.db_pool import active_transaction_connection, transaction_connection

    if (
        not _transaction_wrapped
        and active_transaction_connection(conn) is None
        and (getattr(conn, "_pool", None) is not None or hasattr(conn, "autocommit"))
    ):
        with transaction_connection(conn) as transaction_conn:
            return persist_provider_groups(
                transaction_conn,
                account_id=account_id,
                install_id=install_id,
                extension_id=extension_id,
                contract=contract,
                groups=groups,
                credential_ref=credential_ref,
                connection_config=connection_config,
                _transaction_wrapped=True,
            )
    operations = contract.operations
    models_query_id = str(getattr(operations.list_models, "query_id", "") or "llm.list_models")
    chat_action_id = str(getattr(operations.chat, "action_id", "") or "llm.chat")
    structured_chat_action_id = None
    if getattr(operations, "structured_chat", None) is not None:
        structured_chat_action_id = (
            str(getattr(operations.structured_chat, "action_id", "") or "") or None
        )

    summary: list[dict[str, Any]] = []
    for group in groups:
        source_id = store.upsert_source(
            conn,
            account_id=account_id,
            extension_install_id=install_id,
            extension_id=extension_id,
            provider_key=group["provider_key"],
            display_label=group["display_label"],
            supplier_key=group["supplier_key"],
            supplier_label=group["supplier_label"],
            upstream_owner_key=group["upstream_owner_key"],
            upstream_owner_label=group["upstream_owner_label"],
            models_query_id=models_query_id,
            chat_action_id=chat_action_id,
            structured_chat_action_id=structured_chat_action_id,
            credential_secret_ref=credential_ref,
            capabilities={},
            connection_config=connection_config,
        )
        if not source_id:
            continue
        discovered = store.upsert_discovered_models(
            conn,
            account_id=account_id,
            provider_source_id=source_id,
            models=group["models"],
        )
        # Drop models that disappeared from this group's latest discovery so an
        # upstream-removed model stops being catalog/runtime-valid.
        keep_model_ids = [str(m.get("model_id") or "") for m in group["models"]]
        store.reconcile_models_for_source(
            conn,
            account_id=account_id,
            provider_source_id=source_id,
            keep_model_ids=keep_model_ids,
        )
        summary.append(
            {
                "provider_key": group["provider_key"],
                "source_id": source_id,
                "display_label": group["display_label"],
                "discovered_models": discovered,
            }
        )
    # Drop provider groups no longer returned by the latest discovery (cascades
    # to their models). Last, so removed-group models go with their source.
    removed_sources = store.reconcile_sources_for_install(
        conn,
        account_id=account_id,
        extension_install_id=install_id,
        keep_provider_keys=[g["provider_key"] for g in groups],
    )
    return {
        "sources": summary,
        "group_count": len(groups),
        "removed_sources": removed_sources,
    }


def encode_credentials(secrets: dict[str, Any]) -> str:
    """Serialize a multi-secret credential payload for encryption."""
    return json.dumps({str(k): str(v) for k, v in (secrets or {}).items()})
