# ruff: noqa: F405, I001, RUF100
# mypy: ignore-errors
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .http_app_request_helpers import *  # noqa: F403,F401  # NOSONAR
from .maintenance_http import maintenance_http_middleware
from .static_assets import ServerFaultStaticFiles
from core.infrastructure.extension_runtime.browser_permissions import (
    extension_browser_permission_snapshot,
)

_ExtensionScanLifecycleCallback = Callable[[Any, list[dict]], Any]
_extension_scan_lifecycle_callback: _ExtensionScanLifecycleCallback | None = None


def configure_extension_scan_lifecycle(callback: _ExtensionScanLifecycleCallback) -> None:
    """Inject the application policy used after an HTTP extension rescan."""
    if not callable(callback):
        raise TypeError("extension scan lifecycle callback must be callable")

    global _extension_scan_lifecycle_callback
    _extension_scan_lifecycle_callback = callback


def _require_extension_scan_lifecycle() -> _ExtensionScanLifecycleCallback:
    callback = _extension_scan_lifecycle_callback
    if callback is None:
        raise RuntimeError("extension scan lifecycle callback is not configured")
    return callback


def _sync_extension_scan(bl: Any, scanned: list[dict]) -> Any:
    return _require_extension_scan_lifecycle()(bl, scanned)


def _reconcile_extension_providers_on_boot(
    bl: Any,
    reconcile_extension_providers: Callable[[Any], list[dict[str, Any]]],
) -> None:
    """Bring the provider catalog in line with what is installed.

    A provider row is derived state, so it can drift while the platform is down —
    a release upgraded, a catalog restored from an older backup, a write lost to a
    crash between the install and the row. Without the row a notification route
    cannot be saved or delivered, so a drifted catalog is a provider that looks
    installed and silently does nothing. Reconciling on boot is what closes that.
    """
    reconciled = reconcile_extension_providers(bl)
    if reconciled:
        logger.info(
            "reconciled %d extension provider(s) against the install inventory",
            len(reconciled),
        )


def _sync_extension_scan_for_app(
    current_app: FastAPI,
    bl: Any,
    scanned: list[dict],
    *,
    reconcile_extension_providers: Callable[[Any], list[dict[str, Any]]],
) -> Any:
    result = _sync_extension_scan_without_providers(current_app, bl, scanned)
    # After, never before: the sync is what brings a freshly scanned release into
    # the inventory, and reconciling first would rebuild the catalog from the
    # descriptor the platform held before the upgrade.
    _reconcile_extension_providers_on_boot(bl, reconcile_extension_providers)
    return result


def _sync_extension_scan_without_providers(
    current_app: FastAPI, bl: Any, scanned: list[dict]
) -> Any:
    callback = _extension_scan_lifecycle_callback
    if callback is not None:
        return callback(bl, scanned)

    container = getattr(current_app.state, "runtime_container", None)
    service_factories = getattr(container, "service_factories", None)
    service_factory = (
        service_factories.get("extension_binding_service")
        if isinstance(service_factories, dict)
        else None
    )
    if callable(service_factory):
        sync_extension_scan = getattr(service_factory(), "sync_extension_scan", None)
        if callable(sync_extension_scan):
            return sync_extension_scan(scanned, _bl=bl)

    return _require_extension_scan_lifecycle()(bl, scanned)


def _extension_install_is_installed(
    connection: Any,
    *,
    extension_id: str,
    account_id: str,
) -> bool:
    store = _DBExtensionInstallStore(connection)
    return store.get(extension_id=extension_id, account_id=account_id) is not None


_control_config = _ControlConfig.from_env()

# Expose thresholds as module-level constants so existing scan/classify helpers
# keep working without signature changes.
_WATCHDOG_STUCK_SECONDS = _control_config.watchdog_stuck_seconds
_WATCHDOG_ORPHAN_GRACE_SECONDS = _control_config.watchdog_orphan_grace_seconds
_WATCHDOG_PICKUP_GRACE_SECONDS = _control_config.watchdog_pickup_grace_seconds
_WATCHDOG_MAX_JOURNAL_ENTRIES = _control_config.watchdog_max_journal_entries
_WATCHDOG_AUTO_APPLY = _control_config.watchdog_auto_apply
_RETRYABLE_INTERRUPTED_STEP_STATUS = "retryable_interrupted_step"
_SCHEDULED_JOBS_BATCH_LIMIT = _control_config.scheduled_jobs_batch_limit
_SCHEDULED_JOBS_ENABLED = _control_config.scheduled_jobs_enabled
_SCHEDULED_ACTIVE_JOB_STATUSES = {
    "pending",
    "in_progress",
    "awaiting_approval",
    "awaiting_timer",
    "awaiting_child",
}
_resume_shutdown_event = threading.Event()
_resume_threads_lock = threading.Lock()
_resume_threads: set[threading.Thread] = set()
_resume_fenced_project_ids: set[str] = set()
_RESUME_JOIN_TIMEOUT_SECONDS = max(
    1.0,
    float(str(CONFIG.get("resume_join_timeout_seconds") or 15.0)),
)
_JOB_RUNTIME_PAYLOAD_TIMEOUT_SECONDS = max(
    0.1,
    float(str(CONFIG.get("job_runtime_payload_timeout_seconds") or 2.5)),
)


@dataclass(frozen=True)
class _LifecycleRuntimeBindings:
    get_logger_only: Callable[[], Any]
    prewarm_state_reader_graph: Callable[[], None]
    watchdog_scan: Callable[..., Any]
    scheduled_jobs_scan: Callable[..., Any]
    dashboard_rollup_scan: Callable[..., Any]
    external_effect_maintenance_scan: Callable[..., Any]
    reconcile_extension_providers: Callable[[Any], list[dict[str, Any]]]
    public_submission_pause_active: Callable[[], bool]
    local_auth_enabled: Callable[[], bool]
    external_auth_enabled: Callable[[], bool]
    is_public_auth_path: Callable[[str], bool]
    route_registered: Callable[[str, str], bool]
    resolve_request_principal: Callable[..., Any]
    normalize_live_principal: Callable[..., Any]
    auth_tables_available: Callable[[Any], bool]
    get_user_by_session_token: Callable[..., Any]
    local_auth_bootstrap_required: Callable[..., bool]
    auth_account_tables_available: Callable[[Any], bool]
    roles_intersect: Callable[[list[str], set[str]], bool]
    is_admin_only_route: Callable[[str], bool]
    is_registry_read_route: Callable[[str], bool]
    is_self_service_auth_write_route: Callable[[str], bool]
    csrf_request_valid: Callable[[Request], bool]
    start_runtime_reporting: Callable[[], Any] | None = None


def _configure_lifecycle_runtime(
    app: FastAPI,
    bindings: _LifecycleRuntimeBindings,
) -> None:
    """Install lifecycle collaborators on one application instance."""
    app.state.lifecycle_runtime_bindings = bindings
    app.state.lifecycle_control_threads = {}


def _require_lifecycle_runtime_bindings(app: FastAPI) -> _LifecycleRuntimeBindings:
    bindings = getattr(app.state, "lifecycle_runtime_bindings", None)
    if bindings is None:
        raise RuntimeError("HTTP lifecycle runtime has not been configured")
    return bindings


def _watchdog_runtime_config(*, auto_apply: bool | None = None):
    return _watchdog_runtime.build_watchdog_runtime_config(
        control_config=_control_config,
        watchdog_stuck_seconds=_WATCHDOG_STUCK_SECONDS,
        watchdog_orphan_grace_seconds=_WATCHDOG_ORPHAN_GRACE_SECONDS,
        watchdog_pickup_grace_seconds=_WATCHDOG_PICKUP_GRACE_SECONDS,
        watchdog_max_journal_entries=_WATCHDOG_MAX_JOURNAL_ENTRIES,
    )(auto_apply=auto_apply)


def _scheduled_jobs_runtime_config():
    return _dataclass_replace(
        _control_config,
        scheduled_jobs_enabled=bool(_SCHEDULED_JOBS_ENABLED),
        scheduled_jobs_batch_limit=max(1, int(_SCHEDULED_JOBS_BATCH_LIMIT)),
    )


def _resume_shutdown_in_progress() -> bool:
    is_finalizing = getattr(sys, "is_finalizing", None)
    return _resume_shutdown_event.is_set() or bool(is_finalizing and is_finalizing())


def _unregister_resume_thread(thread: threading.Thread) -> None:
    return _graph_resume_runtime.unregister_resume_thread(
        thread,
        resume_threads=_resume_threads,
        resume_threads_lock=_resume_threads_lock,
    )


def _snapshot_resume_threads() -> list[threading.Thread]:
    return _graph_resume_runtime.snapshot_resume_threads(
        resume_threads=_resume_threads,
        resume_threads_lock=_resume_threads_lock,
    )


def _join_resume_threads(timeout_seconds: float = _RESUME_JOIN_TIMEOUT_SECONDS) -> int:
    return _graph_resume_runtime.join_resume_threads(
        resume_threads=_resume_threads,
        resume_threads_lock=_resume_threads_lock,
        timeout_seconds=timeout_seconds,
    )


async def _startup(current_app: FastAPI):
    """Start background services on app boot."""
    _resume_shutdown_event.clear()
    bindings = _require_lifecycle_runtime_bindings(current_app)
    startup_handler = _app_lifecycle_runtime.build_startup_handler(
        config=CONFIG,
        env_flag=_env_flag,
        get_logger_only=bindings.get_logger_only,
        init_sentry=init_sentry,
        prewarm_state_reader_graph=bindings.prewarm_state_reader_graph,
        start_control_threads=lambda **kwargs: _start_control_threads(**kwargs),
        control_config=_control_config,
        watchdog_scan_fn=bindings.watchdog_scan,
        scheduled_jobs_scan_fn=bindings.scheduled_jobs_scan,
        dashboard_rollup_scan_fn=bindings.dashboard_rollup_scan,
        external_effect_maintenance_scan_fn=bindings.external_effect_maintenance_scan,
        maintenance_active_fn=bindings.public_submission_pause_active,
        sync_extension_scan=lambda bl, scanned: _sync_extension_scan_for_app(
            current_app,
            bl,
            scanned,
            reconcile_extension_providers=bindings.reconcile_extension_providers,
        ),
        ensure_canonical_guardrails_seeded_fn=_ensure_canonical_guardrails_seeded,
        extension_install_is_installed_fn=_extension_install_is_installed,
        logger=logger,
        start_monitoring_fn=bindings.start_runtime_reporting,
    )
    payload = await startup_handler()
    current_app.state.lifecycle_control_threads = payload["control_threads"]
    current_app.state.lifecycle_monitoring_handle = payload.get("monitoring_handle")


@asynccontextmanager
async def _lifespan(current_app: FastAPI):
    lifespan_handler = _app_lifecycle_runtime.build_lifespan_handler(
        startup_handler=lambda: _startup(current_app),
        shutdown_app_fn=_app_lifecycle_runtime.shutdown_app,
        resume_shutdown_event=_resume_shutdown_event,
        join_resume_threads=_join_resume_threads,
        stop_control_threads=_stop_control_threads,
        get_control_threads=lambda: getattr(
            current_app.state,
            "lifecycle_control_threads",
            {},
        ),
        logger=logger,
        resume_join_timeout_seconds=_RESUME_JOIN_TIMEOUT_SECONDS,
        get_monitoring_handle=lambda: getattr(
            current_app.state, "lifecycle_monitoring_handle", None
        ),
    )
    async with asynccontextmanager(lifespan_handler)(current_app):
        yield


def _internal_openapi_routes(current_app: FastAPI) -> list[APIRoute]:
    selected: list[APIRoute] = []
    for route in current_app.routes:
        if not isinstance(route, APIRoute) or not route.include_in_schema:
            continue
        path = str(route.path)
        if not path.startswith("/api"):
            continue
        if path in _PUBLIC_HEALTH_PATHS or path in _AUTH_PUBLIC_PATHS_SOURCE:
            continue
        if any(path.startswith(prefix) for prefix in _INTERNAL_OPENAPI_EXCLUDED_PREFIXES):
            continue
        selected.append(route)
    return selected


def _rewrite_openapi_refs(value: Any, ref_map: dict[str, str]) -> Any:
    if isinstance(value, dict):
        rewritten: dict[str, Any] = {}
        for key, item in value.items():
            if key == "$ref" and isinstance(item, str):
                rewritten[key] = ref_map.get(item, item)
            else:
                rewritten[key] = _rewrite_openapi_refs(item, ref_map)
        return rewritten
    if isinstance(value, list):
        return [_rewrite_openapi_refs(item, ref_map) for item in value]
    return value


def _alias_openapi_component_schemas(
    schema: dict[str, Any],
    schema_aliases: dict[str, str],
) -> dict[str, Any]:
    components = schema.get("components")
    if not isinstance(components, dict):
        return schema
    schemas = components.get("schemas")
    if not isinstance(schemas, dict):
        return schema

    ref_map = {
        f"#/components/schemas/{source}": f"#/components/schemas/{target}"
        for source, target in schema_aliases.items()
        if source in schemas
    }
    aliased_schemas: dict[str, Any] = {}
    for schema_name, component_schema in schemas.items():
        target_name = schema_aliases.get(schema_name, schema_name)
        aliased_schemas[target_name] = _rewrite_openapi_refs(component_schema, ref_map)
    components["schemas"] = aliased_schemas
    return _rewrite_openapi_refs(schema, ref_map)


def _route_openapi_key(route: APIRoute, method: str) -> tuple[str, str]:
    return method.upper(), str(route.path)


def _route_map(routes: list[APIRoute]) -> dict[tuple[str, str], APIRoute]:
    mapped: dict[tuple[str, str], APIRoute] = {}
    for route in routes:
        for method in route.methods or set():
            mapped[_route_openapi_key(route, str(method))] = route
    return mapped


def _internal_success_schema_from_annotation(route: APIRoute) -> dict[str, Any] | None:
    response_model = getattr(route, "response_model", None)
    if response_model is not None:
        try:
            return TypeAdapter(response_model).json_schema()
        except Exception:
            pass
    try:
        annotation = inspect.signature(route.endpoint).return_annotation
    except (TypeError, ValueError):
        return None
    if annotation is inspect.Signature.empty:
        return None
    annotation_name = getattr(annotation, "__name__", "")
    if annotation_name in {"Response", "StreamingResponse", "FileResponse"}:
        return None
    try:
        return TypeAdapter(annotation).json_schema()
    except Exception:
        return None


def _internal_override_schema_from_type(
    schema: dict[str, Any],
    schema_type: Any,
) -> dict[str, Any] | None:
    try:
        generated = TypeAdapter(schema_type).json_schema(
            ref_template="#/components/schemas/{model}"
        )
    except Exception:
        return None
    defs = generated.pop("$defs", None)
    if isinstance(defs, dict):
        components = schema.setdefault("components", {})
        schemas = components.setdefault("schemas", {})
        if isinstance(schemas, dict):
            schemas.update(defs)
    return generated


def _strip_documented_additional_properties(node: Any) -> Any:
    if isinstance(node, dict):
        if (
            node.get("type") == "object"
            and node.get("additionalProperties") is True
            and isinstance(node.get("properties"), dict)
            and node["properties"]
        ):
            node.pop("additionalProperties", None)
        for value in node.values():
            _strip_documented_additional_properties(value)
    elif isinstance(node, list):
        for item in node:
            _strip_documented_additional_properties(item)
    return node


def _internal_loose_success_schema(
    *,
    method: str,
    path: str,
    operation: dict[str, Any],
    route: APIRoute | None,
) -> dict[str, Any]:
    title_source = (
        str(operation.get("summary") or "").strip()
        or str(operation.get("operationId") or "").strip()
        or f"{method} {path}"
    )
    title = " ".join(word.capitalize() for word in re.split(r"[^A-Za-z0-9]+", title_source) if word)
    endpoint_name = str(getattr(getattr(route, "endpoint", None), "__name__", "") or "").strip()
    is_array_like = endpoint_name.startswith(("list_", "search_")) or path.endswith(
        ("/history", "/inputs", "/outputs", "/skills", "/tools", "/agents")
    )
    if is_array_like:
        return {
            "type": "array",
            "title": f"{title} Response",
            "description": f"JSON array response returned by `{method} {path}`.",
            "items": {
                "type": "object",
                "additionalProperties": True,
            },
        }
    return {
        "type": "object",
        "title": f"{title} Response",
        "description": f"JSON object response returned by `{method} {path}`.",
        "additionalProperties": True,
    }


def _normalize_internal_success_responses(
    schema: dict[str, Any],
    routes: list[APIRoute],
) -> dict[str, Any]:
    route_map = _route_map(routes)
    for path, methods in (schema.get("paths") or {}).items():
        if not isinstance(methods, dict):
            continue
        for method, operation in methods.items():
            if method.startswith("x-") or not isinstance(operation, dict):
                continue
            method_upper = str(method).upper()
            route = route_map.get((method_upper, str(path)))
            for status_code, response in (operation.get("responses") or {}).items():
                if not str(status_code).startswith(("2", "3")) or not isinstance(response, dict):
                    continue
                override = _INTERNAL_OPENAPI_NON_JSON_SUCCESS_OVERRIDES.get(
                    (method_upper, str(path))
                )
                if override:
                    response["content"] = dict(override["content"])
                    continue
                content = response.get("content")
                if not isinstance(content, dict):
                    continue
                json_content = content.get("application/json")
                if not isinstance(json_content, dict):
                    continue
                override_model = _INTERNAL_OPENAPI_SUCCESS_MODEL_OVERRIDES.get(
                    (method_upper, str(path))
                )
                if override_model is not None:
                    override_schema = _internal_override_schema_from_type(schema, override_model)
                    if override_schema is not None:
                        json_content["schema"] = override_schema
                override_example = _INTERNAL_OPENAPI_SUCCESS_EXAMPLE_OVERRIDES.get(
                    (method_upper, str(path))
                )
                if override_example is not None:
                    json_content["example"] = json.loads(json.dumps(override_example))
                existing_schema = json_content.get("schema")
                if existing_schema not in (None, {}):
                    if "example" not in json_content and "examples" not in json_content:
                        inferred_example = _internal_success_example_from_schema(
                            schema,
                            json_content.get("schema"),
                        )
                        if inferred_example is not None:
                            json_content["example"] = inferred_example
                    continue
                inferred_schema = (
                    _internal_success_schema_from_annotation(route) if route is not None else None
                )
                json_content["schema"] = inferred_schema or _internal_loose_success_schema(
                    method=method_upper,
                    path=str(path),
                    operation=operation,
                    route=route,
                )
                if "example" not in json_content and "examples" not in json_content:
                    inferred_example = _internal_success_example_from_schema(
                        schema,
                        json_content.get("schema"),
                    )
                if inferred_example is not None:
                    json_content["example"] = inferred_example
                final_example = _INTERNAL_OPENAPI_SUCCESS_EXAMPLE_OVERRIDES.get(
                    (method_upper, str(path))
                )
                if final_example is not None:
                    json_content["example"] = json.loads(json.dumps(final_example))
                example_value = json_content.get("example")
                schema_type = (
                    json_content.get("schema", {}).get("type")
                    if isinstance(json_content.get("schema"), dict)
                    else None
                )
                if (
                    schema_type == "object"
                    and isinstance(example_value, list)
                    and len(example_value) == 1
                    and isinstance(example_value[0], dict)
                ):
                    json_content["example"] = example_value[0]
    return _strip_documented_additional_properties(schema)


def _internal_success_example_from_schema(
    openapi_schema: dict[str, Any],
    schema_node: Any,
    *,
    _seen_refs: set[str] | None = None,
) -> Any:
    if not isinstance(schema_node, dict):
        return None
    seen_refs = set(_seen_refs or set())
    ref = schema_node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
        if ref in seen_refs:
            return None
        seen_refs.add(ref)
        component_name = ref.split("/")[-1]
        component_schema = ((openapi_schema.get("components") or {}).get("schemas") or {}).get(
            component_name
        )
        if isinstance(component_schema, dict):
            example = _internal_success_example_from_schema(
                openapi_schema,
                component_schema,
                _seen_refs=seen_refs,
            )
            if example is not None:
                return example
    if "example" in schema_node:
        return json.loads(json.dumps(schema_node["example"]))
    examples = schema_node.get("examples")
    if isinstance(examples, list) and examples:
        return json.loads(json.dumps(examples[0]))
    if "default" in schema_node and schema_node.get("default") is not None:
        return json.loads(json.dumps(schema_node["default"]))
    enum_values = schema_node.get("enum")
    if isinstance(enum_values, list) and enum_values:
        return json.loads(json.dumps(enum_values[0]))
    for branch_key in ("allOf", "oneOf", "anyOf"):
        branches = schema_node.get(branch_key)
        if isinstance(branches, list):
            for branch in branches:
                if isinstance(branch, dict) and branch.get("type") == "null":
                    continue
                example = _internal_success_example_from_schema(
                    openapi_schema,
                    branch,
                    _seen_refs=seen_refs,
                )
                if example is not None:
                    return example
    schema_type = schema_node.get("type")
    if schema_type == "array":
        item_schema = schema_node.get("items")
        item_example = _internal_success_example_from_schema(
            openapi_schema,
            item_schema,
            _seen_refs=seen_refs,
        )
        if item_example is not None:
            return [item_example]
        return []
    if schema_type == "object":
        properties = schema_node.get("properties")
        if isinstance(properties, dict):
            payload: dict[str, Any] = {}
            for prop_name, prop_schema in properties.items():
                example = _internal_success_example_from_schema(
                    openapi_schema,
                    prop_schema,
                    _seen_refs=seen_refs,
                )
                if example is not None:
                    payload[str(prop_name)] = example
            if payload:
                return payload
        return {}
    if schema_type == "string":
        schema_format = str(schema_node.get("format") or "").strip().lower()
        if schema_format == "uri":
            return "https://example.com/resource"
        if schema_format == "date-time":
            return "2026-05-28T12:00:00Z"
        if schema_format == "date":
            return "2026-05-28"
        if schema_format == "uuid":
            return "00000000-0000-0000-0000-000000000000"
        return ""
    if schema_type == "integer":
        return 0
    if schema_type == "number":
        return 0
    if schema_type == "boolean":
        return False
    return None


def _build_curated_openapi_schema(
    *,
    current_app: FastAPI,
    title: str,
    version: str,
    description: str,
    routes: list[APIRoute],
    tags: list[dict[str, str]] | None = None,
    security_scheme_name: str | None = None,
    schema_aliases: dict[str, str] | None = None,
    customize: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    _ = current_app
    schema = get_openapi(
        title=title,
        version=version,
        description=description,
        routes=routes,
    )
    if tags:
        schema["tags"] = tags
    if security_scheme_name:
        components = schema.setdefault("components", {})
        security_schemes = components.setdefault("securitySchemes", {})
        security_schemes[security_scheme_name] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "API token",
            "description": "Bearer token for external API integrations.",
        }
        schema["security"] = [{security_scheme_name: []}]
    if customize is not None:
        customize(schema)
    if schema_aliases:
        schema = _alias_openapi_component_schemas(schema, schema_aliases)
    elif not security_scheme_name:
        schema = _normalize_internal_success_responses(schema, routes)
    return schema


def _register_curated_openapi_routes(current_app: FastAPI, *, public_openapi_policy) -> None:
    docs_config = load_openapi_docs_config()
    schema_cache: dict[str, dict[str, Any]] = {}

    if docs_config.public_openapi_enabled:

        @current_app.get(PUBLIC_OPENAPI_PATH, include_in_schema=False)
        async def openapi_public_json() -> JSONResponse:
            if "public" not in schema_cache:
                schema_cache["public"] = _build_curated_openapi_schema(
                    current_app=current_app,
                    title="Flow Steward Public API",
                    version="v1",
                    description=public_openapi_policy.description,
                    routes=public_openapi_policy.select_routes(current_app),
                    tags=public_openapi_policy.tags,
                    security_scheme_name="BearerAuth",
                    schema_aliases=public_openapi_policy.schema_aliases,
                    customize=public_openapi_policy.apply,
                )
            return JSONResponse(schema_cache["public"])

    if docs_config.public_docs_available:

        @current_app.get(PUBLIC_DOCS_PATH, include_in_schema=False)
        async def public_docs() -> Any:
            return get_swagger_ui_html(
                openapi_url=PUBLIC_OPENAPI_PATH,
                title="Flow Steward Public API Docs",
            )

    if docs_config.internal_openapi_enabled:

        @current_app.get(INTERNAL_OPENAPI_PATH, include_in_schema=False)
        async def openapi_internal_json() -> JSONResponse:
            if "internal" not in schema_cache:
                schema_cache["internal"] = _build_curated_openapi_schema(
                    current_app=current_app,
                    title="Flow Steward Internal API",
                    version=str(current_app.version or ""),
                    description=_INTERNAL_OPENAPI_DESCRIPTION,
                    routes=_internal_openapi_routes(current_app),
                )
            return JSONResponse(schema_cache["internal"])

    if docs_config.internal_docs_available:

        @current_app.get(INTERNAL_DOCS_PATH, include_in_schema=False)
        async def internal_docs() -> Any:
            return get_swagger_ui_html(
                openapi_url=INTERNAL_OPENAPI_PATH,
                title="Flow Steward Internal API Docs",
            )


def _create_base_app() -> FastAPI:
    from core.infrastructure.config.version import APP_VERSION

    app = FastAPI(
        title="Flow Steward",
        version=APP_VERSION,
        lifespan=_lifespan,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    if _APP_GZIP_ENABLED:
        app.add_middleware(GZipMiddleware, minimum_size=_GZIP_MINIMUM_SIZE, compresslevel=6)
    return app


static_versioned_asset = _serve_request_runtime.build_static_versioned_asset_handler(
    static_root=_STATIC_ROOT,
)


def _asset_url(path: str) -> str:
    normalized = str(path or "").lstrip("/")
    if not normalized:
        return f"/static-v/{quote(_STATIC_ASSET_VERSION, safe='')}/"
    return f"/static-v/{quote(_STATIC_ASSET_VERSION, safe='')}/{normalized}"


def _register_static_assets(app: FastAPI) -> None:
    app.mount("/static", ServerFaultStaticFiles(directory="assets"), name="static")
    app.get("/static-v/{asset_version}/{file_path:path}")(static_versioned_asset)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon_redirect() -> RedirectResponse:
        return RedirectResponse(_asset_url("favicon.svg"), status_code=307)


def _validation_errors_without_url(
    exc: RequestValidationError | ValidationError,
) -> list[dict[str, Any]]:
    return _serve_request_runtime.validation_errors_without_url(
        exc,
        err_validation=ERR_VALIDATION,
    )


request_validation_exception_handler = (
    _serve_request_runtime.build_request_validation_exception_handler(
        validation_errors_without_url_fn=lambda exc: _validation_errors_without_url(exc),
        error_payload_fn=lambda **kwargs: _error_payload(**kwargs),
        request_correlation_id_fn=lambda request: _request_correlation_id(request),
    )
)


def _register_core_exception_handlers(app: FastAPI) -> None:
    app.exception_handler(RequestValidationError)(request_validation_exception_handler)


def _build_route_registered_resolver(app: FastAPI):
    return lambda path, method: _serve_request_runtime.route_registered(
        app.router,
        path,
        method,
    )


static_asset_cache_middleware = _serve_request_runtime.build_static_asset_cache_middleware(
    static_cache_max_age_seconds=_STATIC_CACHE_MAX_AGE_SECONDS,
)


async def _local_auth_middleware(request: Request, call_next):
    bindings = _require_lifecycle_runtime_bindings(request.app)
    handler = _auth_http_runtime.build_local_auth_middleware_handler(
        local_auth_enabled_fn=bindings.local_auth_enabled,
        external_auth_enabled=bindings.external_auth_enabled,
        is_public_auth_path_fn=bindings.is_public_auth_path,
        route_registered_fn=bindings.route_registered,
        request_principal_resolver_fn=bindings.resolve_request_principal,
        normalize_live_principal_fn=bindings.normalize_live_principal,
        get_logger_only_fn=bindings.get_logger_only,
        auth_tables_available_fn=bindings.auth_tables_available,
        get_user_by_session_token_fn=bindings.get_user_by_session_token,
        local_auth_bootstrap_required_fn=bindings.local_auth_bootstrap_required,
        auth_account_tables_available_fn=bindings.auth_account_tables_available,
        roles_intersect_fn=bindings.roles_intersect,
        is_admin_only_route_fn=bindings.is_admin_only_route,
        is_registry_read_route_fn=bindings.is_registry_read_route,
        is_self_service_auth_write_route_fn=bindings.is_self_service_auth_write_route,
        csrf_request_valid_fn=bindings.csrf_request_valid,
        auth_cookie_name=_AUTH_COOKIE_NAME,
        auth_unsafe_methods=_AUTH_UNSAFE_METHODS,
        auth_admin_roles=_AUTH_ADMIN_ROLES,
        auth_write_roles=_AUTH_WRITE_ROLES,
        err_database_not_configured=ERR_DATABASE_NOT_CONFIGURED,
        err_auth_tables_not_available=ERR_AUTH_TABLES_NOT_AVAILABLE,
        err_authentication_required=ERR_AUTHENTICATION_REQUIRED,
        err_account_disabled=ERR_ACCOUNT_DISABLED,
        err_admin_role_required=ERR_ADMIN_ROLE_REQUIRED,
        err_operator_role_required=ERR_OPERATOR_ROLE_REQUIRED,
        path_bootstrap=PATH_BOOTSTRAP,
        path_login=PATH_LOGIN,
    )
    return await handler(request, call_next)


local_auth_middleware = _local_auth_middleware


async def _nul_text_validation_middleware(request: Request, call_next):
    location = await _serve_request_runtime.request_nul_text_location(request)
    if location is not None:
        payload = _error_payload(
            status_code=422,
            code="validation_error",
            message="Request validation failed",
            details={
                "errors": [
                    {
                        "type": "value_error.nul_character",
                        "loc": location,
                        "msg": "Text fields must not contain NUL characters",
                    }
                ]
            },
            correlation_id=_request_correlation_id(request),
        )
        return JSONResponse(payload, status_code=422)
    return await call_next(request)


async def _correlation_logging_middleware(request: Request, call_next):
    correlation_id = _request_correlation_id(request)
    request.state.correlation_id = correlation_id
    token = _runtime_logging.set_correlation_id(correlation_id)
    try:
        response = await call_next(request)
    finally:
        _runtime_logging.reset_correlation_id(token)
    response.headers.setdefault("X-Correlation-ID", correlation_id)
    return response


async def _security_headers_middleware(request: Request, call_next):
    """Add protective security headers to all responses.

    - X-Frame-Options: SAMEORIGIN — prevents clickjacking (framing from external sites)
    - X-Content-Type-Options: nosniff — prevents MIME-type sniffing attacks
    - Referrer-Policy: strict-origin-when-cross-origin — limits referrer leakage
    - Content-Security-Policy — restricts script/style/image/connect surfaces
    - Permissions-Policy — disables browser features the app does not use
    - Cross-Origin-* — isolates the app from ambient cross-origin embedding
    """
    response = await call_next(request)
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    browser_permissions = extension_browser_permission_snapshot()

    def browser_sources(directive: str) -> str:
        origins = " ".join(browser_permissions.get(directive, ()))
        return "'self'" + (f" {origins}" if origins else "")

    response.headers.setdefault(
        "Strict-Transport-Security",
        "max-age=63072000; includeSubDomains",
    )
    response.headers.setdefault(
        "Content-Security-Policy",
        (
            "default-src 'self'; "
            "base-uri 'self'; "
            "object-src 'none'; "
            "frame-ancestors 'self'; "
            f"script-src {browser_sources('script-src')}; "
            "style-src 'self'; "
            "style-src-attr 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "font-src 'self' data:; "
            f"connect-src {browser_sources('connect-src')}; "
            f"frame-src {browser_sources('frame-src')}; "
            "form-action 'self'"
        ),
    )
    response.headers.setdefault(
        "Permissions-Policy",
        (
            "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
            "magnetometer=(), microphone=(), payment=(), usb=()"
        ),
    )
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    response.headers.setdefault("Cross-Origin-Embedder-Policy", "credentialless")
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    return response


def _register_core_http_middleware(app: FastAPI) -> None:
    app.middleware("http")(static_asset_cache_middleware)
    app.middleware("http")(local_auth_middleware)
    app.middleware("http")(_nul_text_validation_middleware)
    app.middleware("http")(_security_headers_middleware)
    app.middleware("http")(maintenance_http_middleware)
    app.middleware("http")(_correlation_logging_middleware)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
