# ruff: noqa: F403, F405, RUF022
# mypy: ignore-errors
from __future__ import annotations

import logging
from urllib.parse import urlparse

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.extension_runtime.python_requirement_contract import (
    PYTHON_REQUIREMENTS_FIELD,
    normalize_python_requirements,
)

from .extension_runtime_common import *  # NOSONAR
from .extension_runtime_common import AUTHORING_MANIFEST_ALLOWED_FIELDS
from .extension_runtime_validation import (
    _as_dict,
    _as_list,
    _callback_definitions_value,
    _issue,
    _normalize_event_subscriptions,
    _normalize_string_list,
    _validate_event_subscription_list,
    _workflow_trigger_definitions_value,
)

logger = logging.getLogger(__name__)


def default_extensions_root() -> str:
    configured = str(_runtime_env.get("FS_EXTENSIONS_ROOT", "")).strip()
    if configured:
        return configured
    return str((Path.cwd() / "extensions").resolve())


def extension_host_metadata(extension: dict[str, Any] | None) -> dict[str, Any]:
    """Host metadata for an extension, the same for every extension wherever it came from.

    The only host-side metadata is what the extension's own manifest declared under
    ``runtime_grants``. It is carried as declared: compile refuses a declaration over
    another extension's namespace, and every read goes through
    ``core.application.extensions.runtime_host_grants``, which limits it to the
    extension's own namespace again. There is no origin tier and no host policy file.
    """
    payload = _as_dict(extension)
    compiled_manifest = _as_dict(payload.get("compiled_manifest"))
    declared = compiled_manifest.get("runtime_grants", payload.get("runtime_grants"))
    return {"install_metadata": {"runtime_grants": _as_dict(declared)}}


def extension_broken_reason(extension: dict[str, Any] | None) -> str:
    payload = _as_dict(extension)
    bundle_root = str(payload.get("bundle_root") or "").strip()
    manifest_path = str(payload.get("manifest_path") or "").strip()
    if bundle_root and not Path(bundle_root).exists():
        return "Extension bundle is missing."
    if manifest_path and not Path(manifest_path).is_file():
        return "Extension manifest is missing."
    if str(payload.get("status") or "").strip().lower() == "invalid":
        return str(payload.get("scan_error") or "").strip() or "Extension validation failed."
    if _as_list(payload.get("compile_errors")) or _as_list(payload.get("validation_errors")):
        return str(payload.get("scan_error") or "").strip() or "Extension validation failed."
    return ""


def _load_data_file(path: Path) -> Any:
    suffix = path.suffix.lower()
    raw_text = path.read_text(encoding="utf-8")
    if suffix in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError as exc:
            raise ValueError(f"YAML support requires PyYAML: {exc}") from exc
        return yaml.safe_load(raw_text)
    raise ValueError(f"Unsupported extension contract file format '{suffix}'; use YAML")


def _load_manifest_file(path: Path) -> dict:
    data = _load_data_file(path)
    if not isinstance(data, dict):
        raise ValueError("Manifest must deserialize to an object")
    return data


def _normalize_command(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item or "").strip() for item in value if str(item or "").strip()]
    if isinstance(value, str):
        return [item for item in shlex.split(value) if item]
    return []


def _normalize_entrypoint(value: Any) -> dict:
    if isinstance(value, dict):
        normalized = dict(value)
        command = _normalize_command(normalized.get("command"))
        if command:
            normalized["command"] = command
        mode = str(normalized.get("mode") or "").strip() or "subprocess"
        normalized["mode"] = mode
        return normalized
    command = _normalize_command(value)
    if command:
        return {"mode": "subprocess", "command": command}
    return {}


def _validate_entrypoint_timeout(
    *,
    entrypoint: dict,
    issues: list[dict],
    manifest_path: Path,
) -> None:
    policy = entrypoint.get("timeout_policy")
    if policy is not None:
        expected = {
            "env": "FS_EXTENSION_OPERATION_TIMEOUT_SECONDS",
            "default_seconds": 300,
            "minimum_seconds": 30,
            "maximum_seconds": 3600,
            "headroom_seconds": 30,
        }
        normalized = dict(policy) if isinstance(policy, dict) else {}
        try:
            env_name = str(normalized.get("env") or "").strip()
            default = int(normalized.get("default_seconds"))
            minimum = int(normalized.get("minimum_seconds"))
            maximum = int(normalized.get("maximum_seconds"))
            headroom = int(normalized.get("headroom_seconds"))
        except (TypeError, ValueError):
            env_name, default, minimum, maximum, headroom = "", 0, 0, 0, 0
        if (
            not isinstance(policy, dict)
            or set(normalized) != set(expected)
            or {
                "env": env_name,
                "default_seconds": default,
                "minimum_seconds": minimum,
                "maximum_seconds": maximum,
                "headroom_seconds": headroom,
            }
            != expected
        ):
            issues.append(
                _issue(
                    severity="error",
                    code="invalid_timeout_policy",
                    message=(
                        "entrypoint.timeout_policy must declare the canonical "
                        "FS_EXTENSION_OPERATION_TIMEOUT_SECONDS policy"
                    ),
                    file_path=str(manifest_path),
                    field_path="entrypoint.timeout_policy",
                )
            )
        else:
            entrypoint["timeout_policy"] = {
                "env": env_name,
                "default_seconds": default,
                "minimum_seconds": minimum,
                "maximum_seconds": maximum,
                "headroom_seconds": headroom,
            }
    if "timeout_seconds" not in entrypoint:
        return
    raw_timeout = entrypoint.get("timeout_seconds")
    try:
        timeout_seconds = int(raw_timeout)
    except (TypeError, ValueError):
        issues.append(
            _issue(
                severity="error",
                code="invalid_timeout",
                message="entrypoint.timeout_seconds must be an integer from 1 to 600",
                file_path=str(manifest_path),
                field_path="entrypoint.timeout_seconds",
            )
        )
        return
    if timeout_seconds < 1 or timeout_seconds > 600:
        issues.append(
            _issue(
                severity="error",
                code="invalid_timeout",
                message="entrypoint.timeout_seconds must be an integer from 1 to 600",
                file_path=str(manifest_path),
                field_path="entrypoint.timeout_seconds",
            )
        )
        return
    entrypoint["timeout_seconds"] = timeout_seconds


def _normalize_healthcheck(value: Any) -> dict:
    if isinstance(value, dict):
        normalized = dict(value)
        command = _normalize_command(normalized.get("command") or normalized.get("health_command"))
        if command:
            normalized["command"] = command
        timeout_seconds = normalized.get("timeout_seconds")
        try:
            normalized["timeout_seconds"] = (
                max(1, int(timeout_seconds)) if timeout_seconds is not None else 10
            )
        except (TypeError, ValueError):
            normalized["timeout_seconds"] = 10
        normalized.pop("health_command", None)
        return normalized
    command = _normalize_command(value)
    if command:
        return {"command": command, "timeout_seconds": 10}
    return {}


def _manifest_checksum(path: Path, payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    material = path.as_posix().encode("utf-8") + b"\0" + encoded
    return hashlib.sha256(material).hexdigest()


def _discover_manifest_path(bundle_dir: Path) -> Path | None:
    for filename in MANIFEST_FILENAMES:
        candidate = bundle_dir / filename
        if candidate.is_file():
            return candidate
    return None


def _detect_manifest_version(manifest: dict) -> int:
    raw = manifest.get("manifest_version")
    return 2 if raw == 2 else 0


def _resolve_ref_path(
    *,
    bundle_root: Path,
    parent_path: Path,
    ref_text: str,
) -> Path:
    raw_ref = str(ref_text or "").strip()
    if not raw_ref:
        raise ValueError("ref is required")
    if raw_ref.startswith("/"):
        raise ValueError(f"Absolute ref '{raw_ref}' is not allowed")

    candidate = (parent_path.parent / raw_ref).resolve()
    bundle_root_resolved = bundle_root.resolve()
    if bundle_root_resolved != candidate and bundle_root_resolved not in candidate.parents:
        raise ValueError(f"Ref '{raw_ref}' escapes extension bundle root")
    if not candidate.exists() or not candidate.is_file():
        raise ValueError(f"Ref '{raw_ref}' does not exist")
    return candidate


def _resolve_object_ref(
    *,
    bundle_root: Path,
    parent_path: Path,
    ref_text: str,
    issues: list[dict],
    field_path: str,
) -> tuple[Any, str]:
    try:
        target_path = _resolve_ref_path(
            bundle_root=bundle_root,
            parent_path=parent_path,
            ref_text=ref_text,
        )
    except Exception as exc:
        issues.append(
            _issue(
                severity="error",
                code="ref_resolution_error",
                message=str(exc),
                file_path=str(parent_path),
                field_path=field_path,
                hint="Fix the ref path so it stays inside the extension bundle.",
            )
        )
        return None, ""

    try:
        data = _load_data_file(target_path)
    except Exception as exc:
        issues.append(
            _issue(
                severity="error",
                code="ref_load_error",
                message=f"Could not load ref '{ref_text}': {exc}",
                file_path=str(target_path),
                field_path=field_path,
                hint="Ensure the referenced extension contract file is valid YAML.",
            )
        )
        return None, str(target_path)
    return data, str(target_path)


def _normalize_manifest_basics(
    bundle_dir: Path,
    manifest_path: Path,
    expected_kind: str,
    manifest: dict,
    issues: list[dict],
    bundle_dir_name: str = "",
) -> dict:
    manifest_version = _detect_manifest_version(manifest)
    if manifest_version != 2:
        issues.append(
            _issue(
                severity="error",
                code="manifest_v2_required",
                message="Canonical extension packages require manifest_version: 2.",
                file_path=str(manifest_path),
                field_path="manifest_version",
                hint="Set manifest_version: 2 and use the current extension package contract.",
            )
        )
    for field_name in sorted(set(manifest) - AUTHORING_MANIFEST_ALLOWED_FIELDS):
        issues.append(
            _issue(
                severity="error",
                code="unknown_manifest_field",
                message=f"Manifest field '{field_name}' is not supported.",
                file_path=str(manifest_path),
                field_path=field_name,
                hint="Remove the field or use a field defined by the manifest v2 standard.",
            )
        )

    kind = str(manifest.get("kind") or "").strip()
    if not kind:
        issues.append(
            _issue(
                severity="error",
                code="missing_kind",
                message="Manifest kind is required",
                file_path=str(manifest_path),
                field_path="kind",
            )
        )
    elif kind not in EXTENSION_KINDS:
        issues.append(
            _issue(
                severity="error",
                code="invalid_kind",
                message=f"Unsupported manifest kind '{kind}'",
                file_path=str(manifest_path),
                field_path="kind",
                hint=f"Use one of: {', '.join(EXTENSION_KINDS)}",
            )
        )
    if expected_kind and kind != expected_kind:
        issues.append(
            _issue(
                severity="error",
                code="kind_mismatch",
                message=f"Manifest kind '{kind}' does not match folder kind '{expected_kind}'",
                file_path=str(manifest_path),
                field_path="kind",
            )
        )

    version = str(manifest.get("version") or "").strip()
    if not version:
        issues.append(
            _issue(
                severity="error",
                code="missing_version",
                message="Manifest version is required",
                file_path=str(manifest_path),
                field_path="version",
            )
        )

    if not isinstance(manifest.get("lifecycle"), dict):
        issues.append(
            _issue(
                severity="error",
                code="missing_lifecycle",
                message="Manifest lifecycle is required by the clean-slate extension contract",
                file_path=str(manifest_path),
                field_path="lifecycle",
            )
        )

    raw_extension_id = manifest.get("extension_id")
    extension_key = str(raw_extension_id or "").strip()
    if not extension_key:
        issues.append(
            _issue(
                severity="error",
                code="missing_extension_id",
                message="Manifest extension_id is required",
                file_path=str(manifest_path),
                field_path="extension_id",
            )
        )
    else:
        try:
            extension_key = normalize_extension_id(extension_key, "extension_id")
        except ValueError as exc:
            issues.append(
                _issue(
                    severity="error",
                    code="invalid_extension_id",
                    message=str(exc),
                    file_path=str(manifest_path),
                    field_path="extension_id",
                    hint="Use a lowercase dot-separated package id such as acme.notification-provider.",
                )
            )

    expected_bundle_dir = ""
    if extension_key:
        try:
            expected_bundle_dir = extension_folder_name(extension_key)
        except ValueError:
            expected_bundle_dir = ""
    logical_bundle_dir_name = str(bundle_dir_name or bundle_dir.name).strip()
    if expected_bundle_dir and logical_bundle_dir_name != expected_bundle_dir:
        issues.append(
            _issue(
                severity="error",
                code="extension_folder_mismatch",
                message=(
                    f"Extension '{extension_key}' must live in folder "
                    f"'{expected_bundle_dir}', not '{logical_bundle_dir_name}'"
                ),
                file_path=str(manifest_path),
                field_path="extension_id",
                hint="Rename the extension folder to match the canonical package id.",
            )
        )

    entrypoint = _normalize_entrypoint(manifest.get("entrypoint"))
    if not entrypoint:
        issues.append(
            _issue(
                severity="error",
                code="missing_entrypoint",
                message="Manifest entrypoint is required",
                file_path=str(manifest_path),
                field_path="entrypoint",
            )
        )
    else:
        _validate_entrypoint_timeout(
            entrypoint=entrypoint,
            issues=issues,
            manifest_path=manifest_path,
        )

    health = _normalize_healthcheck(manifest.get("health"))

    schemas = manifest.get("schemas")

    features = _normalize_string_list(manifest.get("features"))
    event_subscriptions = _normalize_event_subscriptions(manifest.get("event_subscriptions"))
    account_scope = _as_dict(manifest.get("account_scope"))
    if "subscriptions" in account_scope:
        _validate_event_subscription_list(
            issues=issues,
            subscriptions_value=account_scope.get("subscriptions"),
            manifest_path=str(manifest_path),
            field_path="account_scope.subscriptions",
        )
        account_scope["subscriptions"] = _normalize_event_subscriptions(
            account_scope.get("subscriptions")
        )
    if "event_subscriptions" in account_scope:
        _validate_event_subscription_list(
            issues=issues,
            subscriptions_value=account_scope.get("event_subscriptions"),
            manifest_path=str(manifest_path),
            field_path="account_scope.event_subscriptions",
        )
        account_scope["event_subscriptions"] = _normalize_event_subscriptions(
            account_scope.get("event_subscriptions")
        )
    _validate_event_subscription_list(
        issues=issues,
        subscriptions_value=manifest.get("event_subscriptions"),
        manifest_path=str(manifest_path),
        field_path="event_subscriptions",
    )

    supported_events = sorted(
        {
            str(subscription.get("event_type") or "").strip()
            for subscription in event_subscriptions
            if str(subscription.get("event_type") or "").strip()
        }
    )

    if not features:
        issues.append(
            _issue(
                severity="error",
                code="missing_features",
                message="Manifest v2 requires at least one feature",
                file_path=str(manifest_path),
                field_path="features",
            )
        )
    if not _normalize_command(health.get("command")):
        issues.append(
            _issue(
                severity="error",
                code="missing_health",
                message="Manifest v2 requires health.command",
                file_path=str(manifest_path),
                field_path="health.command",
                hint='Add health: {command: ["python3", "health.py"], timeout_seconds: 5}.',
            )
        )

    python_requirements, python_requirement_issues = normalize_python_requirements(
        manifest.get(PYTHON_REQUIREMENTS_FIELD),
        manifest_path=str(manifest_path),
    )
    issues.extend(python_requirement_issues)

    display_name = str(manifest.get("display_name") or "").strip()
    description = str(manifest.get("description") or "").strip()
    if not display_name:
        issues.append(
            _issue(
                severity="error",
                code="missing_display_name",
                message="Manifest v2 requires display_name.",
                file_path=str(manifest_path),
                field_path="display_name",
            )
        )
    if not description:
        issues.append(
            _issue(
                severity="error",
                code="missing_description",
                message="Manifest v2 requires description.",
                file_path=str(manifest_path),
                field_path="description",
            )
        )

    raw_vendor = manifest.get("vendor")
    if raw_vendor not in (None, "") and not isinstance(raw_vendor, dict):
        issues.append(
            _issue(
                severity="error",
                code="invalid_vendor",
                message="vendor must be an object.",
                file_path=str(manifest_path),
                field_path="vendor",
            )
        )
    vendor = _as_dict(raw_vendor)
    vendor_name = str(
        vendor.get("name")
        or vendor.get("vendor_name")
        or manifest.get("vendor_name")
        or manifest.get("author")
        or ""
    ).strip()
    if not vendor_name:
        issues.append(
            _issue(
                severity="error",
                code="missing_vendor_name",
                message="Manifest v2 requires vendor.name.",
                file_path=str(manifest_path),
                field_path="vendor.name",
            )
        )
    normalized_vendor = {
        key: value
        for key, value in {
            "name": vendor_name,
            "support_url": str(
                vendor.get("support_url") or manifest.get("support_url") or ""
            ).strip(),
            "website_url": str(
                vendor.get("website_url") or vendor.get("url") or manifest.get("website_url") or ""
            ).strip(),
        }.items()
        if value
    }
    for url_field in ("support_url", "website_url"):
        url_value = str(normalized_vendor.get(url_field) or "").strip()
        if url_value:
            parsed = urlparse(url_value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                issues.append(
                    _issue(
                        severity="error",
                        code="invalid_vendor_url",
                        message=f"vendor.{url_field} must be an absolute HTTP(S) URL.",
                        file_path=str(manifest_path),
                        field_path=f"vendor.{url_field}",
                    )
                )

    raw_listing = manifest.get("listing")
    if raw_listing not in (None, "") and not isinstance(raw_listing, dict):
        issues.append(
            _issue(
                severity="error",
                code="invalid_listing",
                message="listing must be an object.",
                file_path=str(manifest_path),
                field_path="listing",
            )
        )
    listing = _as_dict(raw_listing)
    category = str(listing.get("category") or "").strip().lower()
    if category and not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,39}", category):
        issues.append(
            _issue(
                severity="error",
                code="invalid_listing_category",
                message="listing.category must be a lowercase slug.",
                file_path=str(manifest_path),
                field_path="listing.category",
            )
        )
    normalized_listing = {
        key: value
        for key, value in {
            "category": category,
            "tags": _normalize_string_list(listing.get("tags")),
            "summary": str(listing.get("summary") or description).strip(),
        }.items()
        if value
    }
    release_stage = str(manifest.get("release_stage") or "active").strip().lower()
    if release_stage not in {"active", "placeholder"}:
        issues.append(
            _issue(
                severity="error",
                code="invalid_release_stage",
                message="release_stage must be 'active' or 'placeholder'.",
                file_path=str(manifest_path),
                field_path="release_stage",
            )
        )

    return {
        "extension_key": extension_key,
        "bundle_dir_name": logical_bundle_dir_name,
        "expected_bundle_dir_name": expected_bundle_dir,
        "kind": kind,
        "version": version,
        "manifest_version": manifest_version,
        "display_name": display_name or extension_key,
        "description": description,
        "author": str(manifest.get("author") or "").strip(),
        "vendor_name": vendor_name,
        "support_url": str(normalized_vendor.get("support_url") or ""),
        "website_url": str(normalized_vendor.get("website_url") or ""),
        "vendor": normalized_vendor,
        "listing": normalized_listing,
        "issues_url": str(manifest.get("issues_url") or "").strip(),
        "package": _as_dict(manifest.get("package")),
        "billing_mode": str(manifest.get("billing_mode") or "").strip(),
        "upgrade_notes": str(manifest.get("upgrade_notes") or "").strip(),
        "screenshots": _as_list(manifest.get("screenshots")),
        "runtime": _as_dict(manifest.get("runtime")),
        "bundle_root": str(bundle_dir.resolve()),
        "manifest_path": str(manifest_path.resolve()),
        "features": features,
        "required_scopes": _normalize_string_list(manifest.get("required_scopes")),
        "runtime_grants": manifest.get("runtime_grants") or {},
        "supported_events": supported_events,
        "supported_url_schemes": _normalize_string_list(manifest.get("supported_url_schemes")),
        "event_subscriptions": event_subscriptions,
        "schemas": _as_dict(schemas),
        "healthcheck": health,
        "entrypoint": entrypoint,
        "ui_manifest_ref": str(manifest.get("ui_manifest") or "").strip(),
        "query_manifest_ref": str(manifest.get("query_manifest") or "").strip(),
        "action_manifest_ref": str(manifest.get("action_manifest") or "").strip(),
        "machine_auth": _as_dict(manifest.get("machine_auth")),
        "account_scope": account_scope,
        "callbacks": _callback_definitions_value(manifest.get("callbacks")),
        "workflow_triggers": _workflow_trigger_definitions_value(manifest.get("workflow_triggers")),
        "external_effects": manifest.get("external_effects") or [],
        "artifact_policies": manifest.get("artifact_policies") or [],
        # Kept as written: absent (None) and declared-empty ({}) mean different things.
        "egress": manifest.get("egress"),
        "project_state": _as_dict(manifest.get("project_state")),
        "extension_lifecycle": _as_dict(manifest.get("lifecycle")),
        "release_stage": release_stage,
        "dependencies": _as_dict(manifest.get("dependencies")),
        PYTHON_REQUIREMENTS_FIELD: python_requirements,
        "manifest": manifest,
    }


def _resolve_schemas(
    *,
    bundle_root: Path,
    manifest_path: Path,
    schemas: dict,
    issues: list[dict],
) -> dict:
    resolved: dict[str, Any] = {}
    for schema_key, schema_value in _as_dict(schemas).items():
        normalized_key = str(schema_key or "").strip()
        if not normalized_key:
            continue
        if isinstance(schema_value, str):
            data, _ = _resolve_object_ref(
                bundle_root=bundle_root,
                parent_path=manifest_path,
                ref_text=schema_value,
                issues=issues,
                field_path=f"schemas.{normalized_key}",
            )
            if isinstance(data, dict):
                resolved[normalized_key] = data
            elif data is not None:
                issues.append(
                    _issue(
                        severity="error",
                        code="invalid_schema_ref",
                        message=f"Schema ref '{schema_value}' must resolve to an object",
                        file_path=str(manifest_path),
                        field_path=f"schemas.{normalized_key}",
                    )
                )
            continue
        if isinstance(schema_value, dict):
            resolved[normalized_key] = dict(schema_value)
            continue
        issues.append(
            _issue(
                severity="error",
                code="invalid_schema",
                message=f"Schema '{normalized_key}' must be an object or ref string",
                file_path=str(manifest_path),
                field_path=f"schemas.{normalized_key}",
            )
        )
    return resolved


__all__ = [
    "default_extensions_root",
    "extension_host_metadata",
    "extension_broken_reason",
    "_load_data_file",
    "_load_manifest_file",
    "_normalize_command",
    "_normalize_entrypoint",
    "_validate_entrypoint_timeout",
    "_normalize_healthcheck",
    "_manifest_checksum",
    "_discover_manifest_path",
    "_detect_manifest_version",
    "_resolve_ref_path",
    "_resolve_object_ref",
    "_normalize_manifest_basics",
    "_resolve_schemas",
]
