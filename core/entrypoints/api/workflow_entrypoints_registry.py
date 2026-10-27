from core.application.webhooks.signature_verifiers import (
    WEBHOOK_SIGNATURE_AUTH_MODE,
    WebhookSignatureConfigError,
    normalize_webhook_signature_config,
    webhook_signature_reserved_headers,
)

from .workflow_entrypoints_common import (
    APPLICATION_JSON_VALUE,
    DUPLICATE_KEY_VALUE,
    FORM_URLENCODED_VALUE,
    MULTIPART_FORM_DATA_VALUE,
    PUBLIC_WORKFLOW_WEBHOOK_PATH_PREFIX,
    REQUEST_BODY_EXCEEDS_SIZE_LIMIT_VALUE,
    SHA256_VALUE,
    Any,
    HTTPException,
    RegistryManager,
    Request,
    RequestPolicyError,
    WorkflowInterfaceService,
    _normalize_response_mode,
    hashlib,
    ipaddress,
    public_base_url,
    run_in_threadpool,
    secrets,
    suppress,
    tempfile,
    uuid,
    workflow_entrypoint_repository,
)


def _public_workflow_webhook_path(entry_key: str) -> str:
    return f"{PUBLIC_WORKFLOW_WEBHOOK_PATH_PREFIX}{str(entry_key or '').strip()}"


def _absolute_url_for_request(request: Request | None, path: str) -> str:
    # Only the configured canonical public base URL is trusted for generated
    # webhook URLs. When it is absent we return the relative path rather than
    # falling back to the request-derived origin (Host/forwarded), which an
    # attacker can spoof to poison the published/copied callback URL
    # (FS-DEEP-CAND-062). The `request` argument is retained for signature
    # compatibility but is no longer used to build absolute URLs.
    del request
    base_url = public_base_url().rstrip("/")
    return f"{base_url}{path}" if base_url else path


def _absolute_url_source(request: Request | None) -> str:
    if public_base_url().rstrip("/"):
        return "platform_public_base_url"
    # No canonical base URL configured: the URL is emitted as a relative path and
    # is never derived from the (spoofable) request origin.
    return "relative_path_fallback"


def _is_provider_trigger_scope_conflict(exc: Exception) -> bool:
    text = str(exc or "").lower()
    if "workflow_provider_triggers_trigger_scope_key" in text:
        return True
    return DUPLICATE_KEY_VALUE in text and "workflow_provider_triggers" in text


def _workflow_registry_db():
    registry = RegistryManager.instance()
    db = getattr(registry, "db_store", None)
    if db is None:
        raise RuntimeError("Workflow registry storage is unavailable")
    return db


def _load_workflow_manifest(workflow_id: str, *, include_trashed: bool = False) -> dict[str, Any]:
    db = _workflow_registry_db()
    workflow = (
        db.get_workflow(str(workflow_id or "").strip(), include_trashed=True)
        if include_trashed
        else db.get_workflow(str(workflow_id or "").strip())
    )
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return dict(workflow)


def _save_workflow_manifest(workflow: dict[str, Any], *, author: str) -> None:
    db = _workflow_registry_db()
    saved = dict(workflow)
    for key in ("phases", "_phases"):
        saved.pop(key, None)
    db.save_workflow(saved, author=author, expected_revision=int(workflow["revision"]))
    RegistryManager.instance().reload_all()


def _normalize_input_mapping(raw: Any) -> dict[str, Any]:
    return dict(raw) if isinstance(raw, dict) else {}


def _normalize_provider_trigger_configs(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    configs: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        trigger_id = str(item.get("trigger_id") or item.get("id") or uuid.uuid4().hex).strip()
        provider_key = str(item.get("provider_key") or "").strip().lower()
        if not trigger_id or not provider_key:
            continue
        configs.append(
            {
                "trigger_id": trigger_id,
                "provider_key": provider_key,
                "display_name": str(item.get("display_name") or "").strip(),
                "event": str(item.get("event") or item.get("event_type") or "").strip(),
                "enabled": bool(item.get("enabled", True)),
                "trigger_config": dict(item.get("trigger_config") or {})
                if isinstance(item.get("trigger_config"), dict)
                else {},
            }
        )
    return configs


def _workflow_trigger_document(workflow: dict[str, Any]) -> dict[str, Any]:
    trigger = workflow.get("trigger")
    return dict(trigger) if isinstance(trigger, dict) else {}


def _provider_trigger_configs_for_workflow(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    return _normalize_provider_trigger_configs(
        _workflow_trigger_document(workflow).get("provider_configs")
    )


def _save_provider_trigger_configs_for_workflow(
    workflow_id: str,
    provider_configs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    workflow = _load_workflow_manifest(workflow_id)
    trigger_doc = _workflow_trigger_document(workflow)
    trigger_doc["provider_configs"] = _normalize_provider_trigger_configs(provider_configs)
    workflow["trigger"] = trigger_doc
    _save_workflow_manifest(workflow, author="workflow_entrypoints_api")
    return _normalize_provider_trigger_configs(trigger_doc["provider_configs"])


def _default_secret_ref(entry_key: str) -> str:
    clean = "".join(
        ch if ch.isalnum() or ch in {"_", "-", "."} else "_" for ch in str(entry_key or "").strip()
    )
    return f"workflow_entrypoint.{clean or 'entrypoint'}.secret"


def _default_auth_secret_ref(entry_key: str, auth_mode: str) -> str:
    suffix = str(auth_mode or "bearer_token").strip().lower() or "bearer_token"
    return f"{_default_secret_ref(entry_key)}.{suffix}"


def _generate_entry_key() -> str:
    return secrets.token_urlsafe(24)


def _normalize_auth_mode(raw: Any) -> str:
    mode = str(raw or "bearer_token").strip().lower() or "bearer_token"
    if mode not in {
        "shared_secret_header",
        "bearer_token",
        "basic_auth",
        "hmac_sha256",
        WEBHOOK_SIGNATURE_AUTH_MODE,
    }:
        raise RequestPolicyError("Unsupported auth_mode", status_code=400)
    return mode


def _normalize_allowed_content_types(
    raw: Any,
    *,
    reject_empty: bool = False,
) -> list[str]:
    if raw is None:
        return [
            APPLICATION_JSON_VALUE,
            MULTIPART_FORM_DATA_VALUE,
            FORM_URLENCODED_VALUE,
            "text/plain",
        ]
    if not isinstance(raw, list):
        return [
            APPLICATION_JSON_VALUE,
            MULTIPART_FORM_DATA_VALUE,
            FORM_URLENCODED_VALUE,
            "text/plain",
        ]
    allowed: list[str] = []
    for item in raw:
        text = str(item or "").strip().lower()
        if text:
            allowed.append(text)
    normalized = sorted(set(allowed))
    if normalized:
        return normalized
    if reject_empty:
        raise RequestPolicyError(
            "At least one allowed content type must be selected",
            status_code=400,
        )
    return []


def _normalize_source_restrictions(raw: Any) -> dict[str, Any]:
    payload = dict(raw) if isinstance(raw, dict) else {}
    exact_ips = [
        str(item or "").strip()
        for item in (payload.get("exact_ips") or payload.get("ip_allowlist") or [])
        if str(item or "").strip()
    ]
    cidrs = [
        str(item or "").strip()
        for item in (payload.get("cidrs") or payload.get("cidr_allowlist") or [])
        if str(item or "").strip()
    ]
    return {
        "exact_ips": exact_ips,
        "cidrs": cidrs,
    }


def _resolve_provider_trigger_project_binding(
    bl,
    *,
    workflow_id: str,
    explicit_project_id: str | None = None,
) -> tuple[str | None, dict[str, Any]]:
    clean_project_id = str(explicit_project_id or "").strip()
    if not clean_project_id:
        return None, {}
    return workflow_entrypoint_repository.resolve_project_run_config_for_workflow_project(
        bl,
        workflow_id,
        clean_project_id,
    )


def _normalize_limit(
    value: Any,
    *,
    default: int,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    try:
        normalized = int(value)
    except Exception:
        normalized = default
    normalized = max(minimum, normalized)
    return min(normalized, maximum) if maximum is not None else normalized


def _normalize_auth_config_payload(
    auth_mode: str,
    auth_config: Any,
    *,
    entry_key: str,
    existing_auth_config: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    payload = dict(auth_config) if isinstance(auth_config, dict) else {}
    existing = dict(existing_auth_config or {})
    if auth_mode == "shared_secret_header":
        header_name = (
            str(
                payload.get("header_name") or existing.get("header_name") or "X-Workflow-Secret"
            ).strip()
            or "X-Workflow-Secret"
        )
        secret_ref = str(
            existing.get("secret_ref") or _default_auth_secret_ref(entry_key, auth_mode)
        ).strip()
        return {"header_name": header_name, "secret_ref": secret_ref}, secret_ref
    if auth_mode == "bearer_token":
        secret_ref = str(
            existing.get("secret_ref") or _default_auth_secret_ref(entry_key, auth_mode)
        ).strip()
        return {"secret_ref": secret_ref}, secret_ref
    if auth_mode == "basic_auth":
        username = str(payload.get("username") or existing.get("username") or "workflow").strip()
        secret_ref = str(
            existing.get("password_secret_ref") or _default_auth_secret_ref(entry_key, auth_mode)
        ).strip()
        return {"username": username, "password_secret_ref": secret_ref}, secret_ref
    if auth_mode == WEBHOOK_SIGNATURE_AUTH_MODE:
        secret_ref = str(
            existing.get("secret_ref") or _default_auth_secret_ref(entry_key, auth_mode)
        ).strip()
        try:
            return (
                normalize_webhook_signature_config(
                    payload or existing,
                    secret_ref=secret_ref,
                ),
                secret_ref,
            )
        except WebhookSignatureConfigError as exc:
            raise RequestPolicyError(str(exc), status_code=400) from exc
    header_name = (
        str(
            payload.get("header_name") or existing.get("header_name") or "X-Workflow-Signature"
        ).strip()
        or "X-Workflow-Signature"
    )
    prefix = str(payload.get("prefix") or existing.get("prefix") or SHA256_VALUE).strip()
    secret_ref = str(
        existing.get("secret_ref") or _default_auth_secret_ref(entry_key, auth_mode)
    ).strip()
    return {"header_name": header_name, "prefix": prefix, "secret_ref": secret_ref}, secret_ref


def _entrypoint_project_binding_id(entrypoint: dict[str, Any]) -> str:
    return str(entrypoint.get("project_id") or "").strip()


def _public_auth_config(entrypoint: dict[str, Any]) -> dict[str, Any]:
    return dict(entrypoint.get("auth_config_jsonb") or {})


def _effective_entrypoint_auth_mode(entrypoint: dict[str, Any]) -> str:
    raw_auth_mode = str(entrypoint.get("auth_mode") or "").strip().lower()
    if raw_auth_mode:
        return _normalize_auth_mode(raw_auth_mode)
    if str(entrypoint.get("secret_ref") or "").strip():
        return "shared_secret_header"
    return ""


def _entrypoint_secret_reveal(
    auth_mode: str,
    auth_config: dict[str, Any],
    *,
    secret_value: str,
) -> dict[str, Any]:
    if auth_mode == "shared_secret_header":
        return {
            "auth_mode": auth_mode,
            "header_name": str(auth_config.get("header_name") or "X-Workflow-Secret"),
            "secret": secret_value,
        }
    if auth_mode == "basic_auth":
        return {
            "auth_mode": auth_mode,
            "username": str(auth_config.get("username") or "workflow"),
            "password": secret_value,
        }
    if auth_mode == "hmac_sha256":
        return {
            "auth_mode": auth_mode,
            "header_name": str(auth_config.get("header_name") or "X-Workflow-Signature"),
            "prefix": str(auth_config.get("prefix") or SHA256_VALUE),
            "secret": secret_value,
        }
    if auth_mode == WEBHOOK_SIGNATURE_AUTH_MODE:
        signature = auth_config.get("signature")
        signature = dict(signature) if isinstance(signature, dict) else {}
        timestamp = auth_config.get("timestamp")
        timestamp = dict(timestamp) if isinstance(timestamp, dict) else {}
        reveal = {
            "auth_mode": auth_mode,
            "header_name": str(signature.get("header") or ""),
            "parser": str(signature.get("parser") or "raw"),
            "encoding": str(signature.get("encoding") or "hex"),
            "secret": secret_value,
        }
        # Only echo the non-secret shape knobs the operator needs when wiring the
        # source system; never the signature or timestamp values themselves.
        for key in ("prefix", "value_key"):
            if signature.get(key):
                reveal[key] = str(signature[key])
        timestamp_source = str(timestamp.get("source") or "none")
        if timestamp_source != "none":
            reveal["timestamp_source"] = timestamp_source
            for key in ("header", "key", "unit"):
                if timestamp.get(key):
                    reveal[f"timestamp_{key}"] = str(timestamp[key])
        return reveal
    return {"auth_mode": auth_mode, "token": secret_value}


def _request_ip_for_security(request: Request) -> str:
    client_host = str(getattr(getattr(request, "client", None), "host", "") or "").strip()
    return client_host or "unknown"


def _source_ip_allowed(source_restrictions: dict[str, Any], request: Request) -> bool:
    restrictions = dict(source_restrictions or {})
    exact_ips = {
        str(item or "").strip()
        for item in (restrictions.get("exact_ips") or [])
        if str(item or "").strip()
    }
    cidr_ranges = [
        str(item or "").strip()
        for item in (restrictions.get("cidrs") or [])
        if str(item or "").strip()
    ]
    if not exact_ips and not cidr_ranges:
        return True
    client_ip = _request_ip_for_security(request)
    if client_ip in exact_ips:
        return True
    try:
        client_addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    for cidr in cidr_ranges:
        try:
            network = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        if client_addr in network:
            return True
    return False


def _content_type_base(request: Request) -> str:
    return str(request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()


def _enforce_content_type_policy(allowed_content_types: list[str], request: Request) -> None:
    allowed = {
        str(item or "").strip().lower()
        for item in (allowed_content_types or [])
        if str(item or "").strip()
    }
    if not allowed:
        return
    content_type = _content_type_base(request)
    if content_type not in allowed:
        raise RequestPolicyError(
            "Content type is not allowed for this workflow entrypoint", status_code=415
        )


def _enforce_content_length_limit(request: Request, *, max_body_bytes: int) -> None:
    content_length = str(request.headers.get("content-length") or "").strip()
    if not content_length:
        return
    try:
        value = int(content_length)
    except ValueError:
        return
    if value > max_body_bytes:
        raise RequestPolicyError(REQUEST_BODY_EXCEEDS_SIZE_LIMIT_VALUE, status_code=413)


async def _read_body_with_cap(request: Request, *, max_body_bytes: int) -> bytes:
    """Stream the request body with a hard cap.

    The Content-Length pre-check (``_enforce_content_length_limit``) silently
    returns when the header is absent or non-numeric, so a chunked / mislabelled
    request would otherwise reach ``await request.body()`` and buffer the entire
    payload before the post-read length check. This reader enforces the cap
    *during* buffering: it rejects with HTTP 413 as soon as the accumulated bytes
    exceed ``max_body_bytes`` instead of materializing an attacker-sized body.
    """
    cap = max(1, int(max_body_bytes or 0))
    # If a prior call already buffered the body, reuse it (and re-check the cap).
    cached = getattr(request, "_body", None)
    if isinstance(cached, (bytes, bytearray)):
        if len(cached) > cap:
            raise RequestPolicyError(REQUEST_BODY_EXCEEDS_SIZE_LIMIT_VALUE, status_code=413)
        return bytes(cached)
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > cap:
            raise RequestPolicyError(REQUEST_BODY_EXCEEDS_SIZE_LIMIT_VALUE, status_code=413)
        chunks.append(chunk)
    body = b"".join(chunks)
    # Cache so downstream request.body()/request.form() can re-read the payload;
    # consuming request.stream() directly would otherwise leave them empty.
    with suppress(Exception):
        request._body = body
    return body


async def _stage_request_body_with_cap(request: Request, *, max_body_bytes: int):
    """Spool a transport body to disk without retaining it in request._body."""
    cap = max(1, int(max_body_bytes or 0))
    staged = tempfile.TemporaryFile(mode="w+b")
    digest = hashlib.sha256()
    total = 0
    try:
        async for chunk in request.stream():
            if not chunk:
                continue
            total += len(chunk)
            if total > cap:
                raise RequestPolicyError(REQUEST_BODY_EXCEEDS_SIZE_LIMIT_VALUE, status_code=413)
            digest.update(chunk)
            await run_in_threadpool(staged.write, chunk)
        await run_in_threadpool(staged.flush)
        await run_in_threadpool(staged.seek, 0)
        return staged, digest.hexdigest()
    except BaseException:
        staged.close()
        raise


async def _install_staged_request_replay(request: Request, staged: Any) -> None:
    """Make Starlette parse a seekable staged body in fixed-size chunks."""
    await run_in_threadpool(staged.seek, 0)

    async def _receive() -> dict[str, Any]:
        chunk = await run_in_threadpool(staged.read, 1024 * 1024)
        return {
            "type": "http.request",
            "body": chunk,
            "more_body": bool(chunk),
        }

    request._receive = _receive
    request._stream_consumed = False
    request._form = None
    request.__dict__.pop("_body", None)


def _normalize_mapping(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    mapping: dict[str, str] = {}
    for key, value in raw.items():
        target = str(key or "").strip()
        source = str(value or "").strip()
        if target and source:
            mapping[target] = source
    return mapping


def _payload_summary(
    *,
    body_payload: Any,
    form_fields: dict[str, Any],
    query_params: dict[str, Any],
    file_refs: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "body_keys": sorted(body_payload.keys()) if isinstance(body_payload, dict) else [],
        "form_keys": sorted(form_fields.keys()),
        "query_keys": sorted(str(key) for key in query_params),
        "file_count": len(file_refs or []),
        "file_names": [
            str(item.get("filename") or "").strip()
            for item in (file_refs or [])
            if str(item.get("filename") or "").strip()
        ],
    }


def _delivery_meta(
    *,
    request: Request,
    auth_mode: str,
    auth_outcome: str,
    http_status: int,
    response_mode: Any,
    validation_errors: list[dict[str, Any]] | None = None,
    payload_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "source_ip": _request_ip_for_security(request),
        "auth_mode": auth_mode,
        "auth_outcome": auth_outcome,
        "auth_category": auth_mode,
        "http_status": http_status,
        "content_type": _content_type_base(request),
        "response_mode": _normalize_response_mode(response_mode),
        "validation_errors": list(validation_errors or []),
        "payload_summary": dict(payload_summary or {}),
    }


def _mapping_selector_supported(source: str) -> bool:
    root_name, _, tail = str(source or "").strip().partition(".")
    if root_name != "headers":
        return root_name in {"body", "query", "form", "files"}
    header_name = str(tail or "").strip().lower()
    blocked_headers = {
        "authorization",
        "cookie",
        "set-cookie",
        "idempotency-key",
        "x-request-id",
        "x-correlation-id",
        "x-api-key",
    }
    return bool(header_name) and header_name not in blocked_headers and "secret" not in header_name


def _mapping_selector_allowed_for_auth(source: str, auth_config: dict[str, Any] | None) -> bool:
    if not _mapping_selector_supported(source):
        return False
    root_name, _, tail = str(source or "").strip().partition(".")
    if root_name != "headers":
        return True
    configured_auth_headers = {
        name
        for name in (
            str((auth_config or {}).get("header_name") or "").strip().lower(),
            *webhook_signature_reserved_headers(auth_config),
        )
        if name
    }
    return str(tail or "").strip().lower() not in configured_auth_headers


def _entrypoint_readiness(entrypoint: dict[str, Any]) -> dict[str, Any]:
    workflow_id = str(entrypoint.get("workflow_id") or "").strip()
    if not workflow_id:
        return {
            "status": "invalid",
            "errors": [
                {
                    "code": "workflow.input.workflow_id_required",
                    "message": "Workflow entrypoint is missing workflow_id",
                    "path": "workflow_id",
                    "channel": "webhook",
                }
            ],
        }
    service = WorkflowInterfaceService()
    interface = service.interface_for_workflow_id(workflow_id)
    validation = service.validate_inputs(interface, {}, channel="webhook")
    errors = [
        dict(item)
        for item in (validation.errors or [])
        if str(item.get("code") or "").strip() != "workflow.input.required"
    ]
    mapping = _normalize_mapping(entrypoint.get("input_mapping_jsonb"))
    allowed_content_types = _normalize_allowed_content_types(
        entrypoint.get("allowed_content_types_jsonb")
    )
    automap_body_fields_supported = any(
        content_type in {APPLICATION_JSON_VALUE, MULTIPART_FORM_DATA_VALUE, FORM_URLENCODED_VALUE}
        for content_type in allowed_content_types
    )
    expected_inputs = {
        str(row.name or "").strip(): row
        for row in (interface.expected_inputs or [])
        if str(row.name or "").strip()
    }
    for target, source in mapping.items():
        if target not in expected_inputs:
            errors.append(
                {
                    "code": "workflow.input.mapping_target_unknown",
                    "message": f"Input mapping target '{target}' is not a declared Expected Input",
                    "path": f"input_mapping.{target}",
                    "channel": "webhook",
                }
            )
            continue
        if not _mapping_selector_supported(source):
            errors.append(
                {
                    "code": "workflow.input.mapping_selector_invalid",
                    "message": f"Unsupported mapping selector '{source}'",
                    "path": f"input_mapping.{target}",
                    "channel": "webhook",
                }
            )
    for name, row in expected_inputs.items():
        if not bool(getattr(row, "required", False)):
            continue
        source = mapping.get(name)
        if source:
            if _mapping_selector_supported(source):
                continue
        elif automap_body_fields_supported:
            # Field-mode webhooks automap body.<expected_input_name> by default.
            continue
        errors.append(
            {
                "code": "workflow.input.mapping_required",
                "message": (
                    f"Required workflow input '{name}' is not reachable from webhook mapping"
                    if automap_body_fields_supported
                    else (
                        f"Required workflow input '{name}' needs an explicit mapping because "
                        "the selected content types do not expose body.<input_name> automap"
                    )
                ),
                "path": f"input_mapping.{name}",
                "channel": "webhook",
            }
        )
    return {
        "status": "ready" if not errors else "invalid",
        "errors": errors,
    }


__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
