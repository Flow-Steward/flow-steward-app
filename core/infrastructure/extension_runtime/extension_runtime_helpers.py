# mypy: ignore-errors
"""Helper utilities for extension runtime records and validation summaries."""

from __future__ import annotations

import copy
import uuid
from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID


def _to_json_obj(value):
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            import json

            return json.loads(value)
        except Exception:
            return value
    return value


def extension_identifier(payload: dict | None) -> str:
    row = _to_json_obj(payload) or {}
    compiled_manifest = _to_json_obj(row.get("compiled_manifest")) or {}
    return str(
        row.get("extension_key")
        or row.get("extension_id")
        or compiled_manifest.get("extension_id")
        or row.get("id")
        or ""
    ).strip()


def extension_manifest_has_ui(payload: dict | None) -> bool:
    row = _to_json_obj(payload) or {}
    ui_manifest = _to_json_obj(row.get("ui_manifest")) or {}
    pages = ui_manifest.get("pages")
    return isinstance(pages, list) and len(pages) > 0


def extension_operation_handler_mode(
    extension: dict | None, *, operation_type: str, operation_id: str
) -> str:
    payload = _to_json_obj(extension) or {}
    key = "query_definitions" if operation_type == "query" else "action_definitions"
    id_field = "query_id" if operation_type == "query" else "action_id"
    for row in payload.get(key) or []:
        item = _to_json_obj(row) or {}
        if str(item.get(id_field) or "").strip() != str(operation_id or "").strip():
            continue
        handler = _to_json_obj(item.get("handler")) or {}
        mode = str(handler.get("mode") or "extension").strip().lower() or "extension"
        return "platform" if mode == "core" else mode
    return "extension"


def extension_operation_failure_status(invocation: dict | None) -> int:
    payload = _to_json_obj(invocation) or {}
    detail = str(payload.get("detail") or "").strip().lower()
    response = _to_json_obj(payload.get("response")) or {}
    error_code = str(response.get("error_code") or "").strip().lower()
    if error_code == "template_not_found":
        return 404
    if error_code.endswith("_required"):
        return 400
    if "disabled" in detail:
        return 409
    if any(token in detail for token in ("unknown", "required", "requires", "unsupported")):
        return 400
    return 502


def merge_extension_runtime_rows(
    db_extension: dict | None, fs_extension: dict | None
) -> dict | None:
    if not db_extension and not fs_extension:
        return None
    if not db_extension:
        return copy.deepcopy(fs_extension)
    if not fs_extension:
        return copy.deepcopy(db_extension)

    merged = copy.deepcopy(db_extension)
    compiled_keys = (
        "extension_key",
        "kind",
        "display_name",
        "description",
        "bundle_root",
        "manifest_path",
        "features",
        "required_scopes",
        "supported_events",
        "event_subscriptions",
        "schemas",
        "healthcheck",
        "entrypoint",
        "manifest",
        "version",
        "checksum",
        "manifest_version",
        "status",
        "scan_error",
        "compile_errors",
        "compile_warnings",
        "validation_errors",
        "validation_warnings",
        "compiled_manifest",
        "query_definitions",
        "action_definitions",
        "callbacks",
        "ui_manifest",
        "lifecycle",
        "machine_auth",
        "account_scope",
        "install_metadata",
    )
    for key in compiled_keys:
        if key in fs_extension:
            merged[key] = copy.deepcopy(fs_extension.get(key))
    if str(merged.get("latest_version") or "").strip() == "":
        merged["latest_version"] = str(merged.get("version") or "").strip()
    return merged


def load_extension_from_filesystem(extension_id: str) -> dict | None:
    from core.application.extensions.identity import extension_folder_name
    from core.infrastructure.extension_runtime.extension_filesystem_loader import (
        load_extension_from_filesystem as load_installed_extension,
    )

    return load_installed_extension(
        extension_id,
        canonical_folder_name=extension_folder_name(extension_id),
    )


def resolve_extension_runtime_record(
    extension_id: str,
    *,
    get_logger_only=None,
    account_id: str | None = None,
) -> tuple[dict | None, Any]:
    normalized_id = str(extension_id or "").strip()
    if not normalized_id:
        return None, None

    try:
        is_uuid_install_id = bool(uuid.UUID(normalized_id))
    except ValueError:
        is_uuid_install_id = False
    if normalized_id.startswith("pi_") or is_uuid_install_id:
        return resolve_extension_runtime_record_by_install_id(
            normalized_id,
            get_logger_only=get_logger_only,
            account_id=account_id,
        )

    bl = None
    if callable(get_logger_only):
        try:
            bl = get_logger_only()
        except Exception:
            bl = None

    from core.application.extensions.identity import is_canonical_extension_id

    if not is_canonical_extension_id(normalized_id):
        return None, bl

    db_extension = None
    resolved_extension_id = normalized_id
    normalized_account_id = str(account_id or DEFAULT_ACCOUNT_ID).strip() or DEFAULT_ACCOUNT_ID
    if bl:
        if not db_extension:
            db_extension = bl.get_extension(
                normalized_id,
                account_id=normalized_account_id,
            )
        if db_extension:
            resolved_extension_id = extension_identifier(db_extension) or normalized_id

    fs_lookup_id = resolved_extension_id or normalized_id
    fs_extension = load_extension_from_filesystem(fs_lookup_id)
    if (
        not fs_extension
        and normalized_id
        and resolved_extension_id
        and resolved_extension_id != normalized_id
    ):
        fs_extension = load_extension_from_filesystem(normalized_id)

    if db_extension or fs_extension:
        from core.infrastructure.extension_runtime.extension_runtime import (
            normalize_extension_row_for_runtime,
        )

        merged = merge_extension_runtime_rows(
            normalize_extension_row_for_runtime(db_extension) if db_extension else None,
            normalize_extension_row_for_runtime(fs_extension) if fs_extension else None,
        )
        if merged is not None and bl is not None:
            _overlay_canonical_install_enabled_state(
                merged,
                bl=bl,
                extension_id=resolved_extension_id or normalized_id,
                account_id=normalized_account_id,
            )
        return merged, bl
    return None, bl


def resolve_extension_runtime_record_by_install_id(
    install_id: str,
    *,
    get_logger_only=None,
    account_id: str | None = None,
) -> tuple[dict | None, Any]:
    """Resolve an active install without treating its ID as a package ID."""
    normalized_install_id = str(install_id or "").strip()
    if not normalized_install_id:
        return None, None

    bl = None
    if callable(get_logger_only):
        try:
            bl = get_logger_only()
        except Exception:
            bl = None
    if bl is None:
        return None, None

    normalized_account_id = str(account_id or DEFAULT_ACCOUNT_ID).strip() or DEFAULT_ACCOUNT_ID
    runtime_gate_lookup = (
        bl.get_extension_by_install_id_for_runtime_gate
        if callable(getattr(type(bl), "get_extension_by_install_id_for_runtime_gate", None))
        else bl.get_extension_by_install_id
    )
    db_extension = runtime_gate_lookup(
        normalized_install_id,
        account_id=normalized_account_id,
    )
    if not db_extension:
        return None, bl

    from core.infrastructure.extension_runtime.extension_runtime import (
        normalize_extension_row_for_runtime,
    )

    merged = merge_extension_runtime_rows(normalize_extension_row_for_runtime(db_extension), None)
    if merged is not None:
        from core.infrastructure.extension_runtime.extension_runtime_manifest import (
            extension_host_metadata,
        )

        resolved_install_id = str(
            merged.get("extension_install_id") or merged.get("install_id") or normalized_install_id
        ).strip()
        if resolved_install_id != normalized_install_id:
            return None, bl
        merged["extension_install_id"] = resolved_install_id
        merged["install_id"] = resolved_install_id
        merged.update(extension_host_metadata(merged))
    return merged, bl


def _overlay_canonical_install_enabled_state(
    record: dict,
    *,
    bl: Any,
    extension_id: str,
    account_id: str,
) -> None:
    """Make extension_installs.state authoritative for the runtime enabled gate.

    The legacy enabled_extensions flag is kept as a synced projection, but when
    an install record exists its state decides enablement — this prevents the
    two stores from disagreeing about whether an extension may execute.
    """
    try:
        from core.application.extensions.service_support import (
            _get_default_extension_install_store,
        )

        store = _get_default_extension_install_store(get_logger=lambda: bl)
        install = store.get(extension_id=extension_id, account_id=account_id)
    except Exception:
        return
    install_id = str(getattr(install, "install_id", "") or "").strip()
    if install_id:
        record["extension_install_id"] = install_id
        record["install_id"] = install_id
    state = str(getattr(install, "state", "") or "").strip().lower()
    if state:
        record["enabled"] = state == "active"


def list_extension_runtime_records(*, account_id: str) -> list[dict[str, Any]]:
    """List deployment runtime bundles without reading tenant-owned install state."""
    _ = account_id
    from core.infrastructure.extension_runtime.extension_runtime_compile import scan_extensions

    return [dict(row) for row in scan_extensions()]


def build_extension_compiled_manifest(extension: dict | None) -> dict:
    from core.infrastructure.extension_runtime.extension_runtime import (
        normalize_extension_row_for_runtime,
    )

    row = normalize_extension_row_for_runtime(extension)
    compiled_manifest = copy.deepcopy(_to_json_obj(row.get("compiled_manifest")) or {})
    ui_manifest = _to_json_obj(row.get("ui_manifest") or compiled_manifest.get("ui_manifest")) or {}

    compiled_manifest.setdefault("extension_id", extension_identifier(row))
    compiled_manifest.setdefault("kind", str(row.get("kind") or "").strip())
    compiled_manifest.setdefault(
        "version",
        str(row.get("version") or row.get("latest_version") or "").strip(),
    )
    compiled_manifest.setdefault(
        "display_name",
        str(row.get("display_name") or extension_identifier(row)).strip(),
    )
    compiled_manifest.setdefault("description", str(row.get("description") or "").strip())
    compiled_manifest.setdefault("manifest_version", int(row.get("manifest_version") or 0))
    compiled_manifest.setdefault("features", copy.deepcopy(row.get("features") or []))
    compiled_manifest.setdefault("required_scopes", copy.deepcopy(row.get("required_scopes") or []))
    compiled_manifest.setdefault(
        "supported_events", copy.deepcopy(row.get("supported_events") or [])
    )
    compiled_manifest.setdefault(
        "event_subscriptions", copy.deepcopy(row.get("event_subscriptions") or [])
    )
    compiled_manifest.setdefault("entrypoint", copy.deepcopy(row.get("entrypoint") or {}))
    compiled_manifest.setdefault("healthcheck", copy.deepcopy(row.get("healthcheck") or {}))
    compiled_manifest.setdefault("schemas", copy.deepcopy(row.get("schemas") or {}))
    compiled_manifest.setdefault(
        "query_definitions", copy.deepcopy(row.get("query_definitions") or [])
    )
    compiled_manifest.setdefault(
        "action_definitions", copy.deepcopy(row.get("action_definitions") or [])
    )
    normalized_contract = (
        _to_json_obj(
            row.get("extension_contract_v2_normalized")
            or compiled_manifest.get("extension_contract_v2_normalized")
        )
        or {}
    )
    operation_manifest = normalized_contract.get("operation_manifest") or row.get(
        "operation_manifest"
    )
    if isinstance(operation_manifest, list) and operation_manifest:
        compiled_manifest.setdefault("operation_manifest", copy.deepcopy(operation_manifest))
    connection_types = normalized_contract.get("connection_types") or row.get("connection_types")
    if isinstance(connection_types, list) and connection_types:
        compiled_manifest.setdefault("connection_types", copy.deepcopy(connection_types))
    compiled_manifest.setdefault("callbacks", copy.deepcopy(row.get("callbacks") or []))
    compiled_manifest.setdefault("machine_auth", copy.deepcopy(row.get("machine_auth") or {}))
    compiled_manifest.setdefault("account_scope", copy.deepcopy(row.get("account_scope") or {}))
    compiled_manifest.setdefault(
        "python_requirements", copy.deepcopy(row.get("python_requirements") or [])
    )
    compiled_manifest.setdefault("manifest_path", str(row.get("manifest_path") or "").strip())
    compiled_manifest.setdefault("bundle_root", str(row.get("bundle_root") or "").strip())

    if ui_manifest:
        compiled_manifest["ui_manifest"] = copy.deepcopy(ui_manifest)

    locale_bundles = _to_json_obj(row.get("locale_bundles")) or {}
    if locale_bundles:
        compiled_manifest["locale_bundles"] = copy.deepcopy(locale_bundles)
        compiled_manifest["locale_codes"] = sorted(locale_bundles.keys())

    issues: list[dict] = []
    for key in ("compile_errors", "compile_warnings", "validation_errors", "validation_warnings"):
        for item in row.get(key) or []:
            payload = _to_json_obj(item) or {}
            if payload:
                issues.append(copy.deepcopy(payload))

    compiled_manifest["validation_errors"] = issues
    compiled_manifest["compile_errors"] = copy.deepcopy(row.get("compile_errors") or [])
    compiled_manifest["compile_warnings"] = copy.deepcopy(row.get("compile_warnings") or [])
    compiled_manifest["validation_warnings"] = copy.deepcopy(row.get("validation_warnings") or [])
    compiled_manifest["compile_status"] = "error" if row.get("compile_errors") else "ok"
    if row.get("validation_errors"):
        compiled_manifest["validation_status"] = "error"
    elif row.get("compile_warnings") or row.get("validation_warnings"):
        compiled_manifest["validation_status"] = "warning"
    else:
        compiled_manifest["validation_status"] = "ok"
    return compiled_manifest


def normalize_extension_issue(item: Any, *, default_severity: str) -> dict | None:
    payload = _to_json_obj(item)
    if payload:
        normalized = copy.deepcopy(payload)
    else:
        message = str(item or "").strip()
        if not message:
            return None
        normalized = {"message": message}
    normalized["severity"] = (
        str(normalized.get("severity") or default_severity).strip().lower() or default_severity
    )
    return normalized


def extension_issue_fingerprint(item: dict | None) -> tuple[str, str, str, str, str]:
    payload = _to_json_obj(item) or {}
    return (
        str(payload.get("severity") or "").strip().lower(),
        str(payload.get("code") or "").strip(),
        str(payload.get("message") or "").strip(),
        str(payload.get("file") or "").strip(),
        str(payload.get("path") or "").strip(),
    )


def extension_validation_summary(compiled_manifest: dict | None) -> dict:
    manifest = _to_json_obj(compiled_manifest) or {}

    compile_errors: list[dict] = []
    compile_warnings: list[dict] = []
    validation_errors: list[dict] = []
    validation_warnings: list[dict] = []

    for item in manifest.get("compile_errors") or []:
        normalized = normalize_extension_issue(item, default_severity="error")
        if normalized:
            compile_errors.append(normalized)
    for item in manifest.get("compile_warnings") or []:
        normalized = normalize_extension_issue(item, default_severity="warning")
        if normalized:
            compile_warnings.append(normalized)

    seen_validation: set[tuple[str, str, str, str, str]] = set()
    compile_seen = {
        extension_issue_fingerprint(item) for item in [*compile_errors, *compile_warnings]
    }

    for item in manifest.get("validation_errors") or []:
        normalized = normalize_extension_issue(item, default_severity="error")
        if not normalized:
            continue
        fingerprint = extension_issue_fingerprint(normalized)
        if fingerprint in compile_seen or fingerprint in seen_validation:
            continue
        seen_validation.add(fingerprint)
        if normalized["severity"] == "warning":
            validation_warnings.append(normalized)
        else:
            validation_errors.append(normalized)

    for item in manifest.get("validation_warnings") or []:
        normalized = normalize_extension_issue(item, default_severity="warning")
        if not normalized:
            continue
        fingerprint = extension_issue_fingerprint(normalized)
        if fingerprint in compile_seen or fingerprint in seen_validation:
            continue
        seen_validation.add(fingerprint)
        validation_warnings.append(normalized)

    ui_manifest = _to_json_obj(manifest.get("ui_manifest")) or {}
    pages = ui_manifest.get("pages")
    page_count = len(pages) if isinstance(pages, list) else 0

    error_count = len(compile_errors) + len(validation_errors)
    warning_count = len(compile_warnings) + len(validation_warnings)
    if error_count:
        status = "error"
    elif warning_count:
        status = "warning"
    else:
        status = "ok"

    return {
        "status": status,
        "page_count": page_count,
        "error_count": error_count,
        "warning_count": warning_count,
        "compile_errors": compile_errors,
        "compile_warnings": compile_warnings,
        "validation_errors": validation_errors,
        "validation_warnings": validation_warnings,
    }


def extension_page_projects(bl, *, account_id: str | None = None) -> list[dict]:
    if not bl or not hasattr(bl, "get_projects"):
        return []
    try:
        rows = bl.get_projects(account_id=account_id) or []
    except TypeError:
        rows = bl.get_projects() or []
    except Exception:
        return []

    projects: list[dict] = []
    for row in rows:
        payload = _to_json_obj(row) or {}
        project_id = str(payload.get("id") or "").strip()
        if not project_id:
            continue
        name = (
            str(payload.get("name") or f"Project {project_id}").strip() or f"Project {project_id}"
        )
        projects.append(
            {
                "id": project_id,
                "name": name,
                "account_id": str(payload.get("account_id") or account_id or "").strip(),
            }
        )
    return projects


def get_extension_query_provider(install_id: str, *, get_logger_only=None):
    """Return a callable that executes resource queries against an extension runtime.

    Returns None if no runtime provider is available for this install.
    The provider accepts a dict (query request) and returns a dict
    with ``items``, ``next_cursor``, and optionally ``total``.

    Runtime mode is resolved from install state (`runtime_mode` + related fields):
    local installs execute through subprocess runtime; remote installs execute
    through the remote runtime adapter.
    """
    normalized_install_id = str(install_id or "").strip()
    if not normalized_install_id:
        return None

    logger_provider = get_logger_only if callable(get_logger_only) else lambda: None

    extension, bl = resolve_extension_runtime_record(
        normalized_install_id,
        get_logger_only=logger_provider,
    )
    if not extension:
        return None
    if extension.get("enabled") is False:
        return None

    compiled_manifest = build_extension_compiled_manifest(extension)
    extension = dict(extension)
    extension.setdefault("compiled_manifest", compiled_manifest)

    def _provider(request_payload: dict[str, Any]) -> dict[str, Any]:
        request = _to_json_obj(request_payload) or {}
        query_id = str(request.get("query_id") or "").strip()
        if not query_id:
            return {"items": [], "next_cursor": "", "total": 0}

        scope = _to_json_obj(request.get("scope")) or {}
        context = {
            "account_id": str(scope.get("account_id") or "").strip(),
            "project_id": str(scope.get("project_id") or "").strip(),
            "connection_id": str(scope.get("connection_id") or "").strip(),
        }
        params = {
            "search": str(request.get("search") or "").strip(),
            "cursor": str(request.get("cursor") or "").strip(),
            "limit": int(request.get("limit") or 50),
            "query_manifest": _to_json_obj(request.get("query_manifest")) or {},
        }
        target: dict[str, Any] = {}
        query_manifest = params["query_manifest"]
        connection_id = context["connection_id"]
        if (
            bl is not None
            and connection_id
            and isinstance(query_manifest, dict)
            and str(query_manifest.get("operation_id") or "").strip()
            and str(query_manifest.get("connection_type_id") or "").strip()
        ):
            from core.application.extensions.operation_support import (
                extension_operation_contract,
            )
            from core.application.extensions.operation_targets import (
                _direct_action_connection_target,
            )

            extension_id = str(
                extension.get("extension_key") or extension.get("extension_id") or ""
            ).strip()
            operation_id = str(query_manifest.get("operation_id") or "").strip()
            operation_contract = extension_operation_contract(
                extension,
                operation_id=operation_id,
            )
            required_connection_type_ids = operation_contract.connection_type_ids
            required_oauth_scopes = operation_contract.required_oauth_scopes

            runtime_connection = _direct_action_connection_target(
                bl,
                connection_id=connection_id,
                account_id=context["account_id"],
                project_id=context["project_id"],
                extension_id=extension_id,
                extension_install_id=normalized_install_id,
                required_connection_type_ids=required_connection_type_ids,
                required_oauth_scopes=required_oauth_scopes,
            )
            target = {
                "connection_id": runtime_connection["connection_id"],
                "connection": runtime_connection,
            }

        runtime_context: dict[str, Any] = {}
        if bl is not None:
            runtime_context["logger"] = bl
        for key in (
            "account_id",
            "project_id",
            "workflow_id",
            "job_id",
            "group_id",
            "for_each_id",
            "step_id",
            "step_execution_id",
        ):
            value = scope.get(key)
            if value not in (None, ""):
                runtime_context[key] = value

        from core.infrastructure.extension_runtime.extension_runtime import run_extension_query

        invocation = run_extension_query(
            extension,
            query_id=query_id,
            context=context,
            target=target,
            params=params,
            runtime_context=runtime_context,
        )
        if str(invocation.get("status") or "").strip().lower() != "succeeded":
            return {"items": [], "next_cursor": "", "total": 0}

        response = _to_json_obj(invocation.get("response")) or {}
        result_payload = _to_json_obj(response.get("result")) or {}
        container = (
            result_payload if isinstance(result_payload, dict) and result_payload else response
        )
        items = container.get("items")
        if not isinstance(items, list):
            items = []
        next_cursor = str(container.get("next_cursor") or response.get("next_cursor") or "").strip()
        total_raw = container.get("total")
        try:
            total = int(total_raw) if total_raw is not None else len(items)
        except (TypeError, ValueError):
            total = len(items)
        return {"items": items, "next_cursor": next_cursor, "total": total}

    return _provider
