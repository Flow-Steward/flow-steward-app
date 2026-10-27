# ruff: noqa: F403, F405, RUF022
# mypy: ignore-errors
from __future__ import annotations

from core.application.extensions.compiled_manifest_policies import (
    _validate_workflow_action_operation_parity,
)
from core.application.extensions.compiled_manifest_validation import (
    host_contract_errors,
)
from core.application.extensions.compiled_manifest_validation import (
    validate_compiled_manifest as validate_compiled_manifest,
)
from core.application.extensions.extension_contract_v2 import validate_extension_contract_v2
from core.infrastructure.extension_runtime.boundary import validate_extension_runtime_boundary
from core.infrastructure.extension_runtime.python_requirement_contract import (
    PYTHON_REQUIREMENTS_FIELD,
)

from .extension_action_manifest import _resolve_action_manifest, _resolve_query_manifest
from .extension_code_scan import scan_extension_code
from .extension_egress import validate_egress
from .extension_runtime_common import *  # NOSONAR
from .extension_runtime_manifest import (
    _discover_manifest_path,
    _load_manifest_file,
    _manifest_checksum,
    _normalize_manifest_basics,
    _resolve_schemas,
    default_extensions_root,
    extension_host_metadata,
)
from .extension_runtime_ui_manifest import (
    _resolve_ui_manifest,
)
from .extension_runtime_validation import (
    _as_dict,
    _as_list,
    _callback_definitions_value,
    _forbidden_contract_json_files,
    _issue,
    _normalize_event_subscriptions,
    _normalize_string_list,
    _split_issues,
    _workflow_trigger_definitions_value,
    _yaml_only_violation_issue,
)

# Compiled-bundle cache keyed by (bundle path, expected kind, logical dir name)
# and invalidated by a content signature (file count, total size, newest mtime).
# Bundle compilation is pure over the bundle's files, so a matching signature
# can safely reuse the previous result.
_COMPILE_CACHE: dict[tuple[str, str, str], tuple[tuple[int, int, int], dict]] = {}
_COMPILE_CACHE_MAX_ENTRIES = 128


def _bundle_content_signature(root: Path) -> tuple[int, int, int]:
    import os

    file_count = 0
    total_size = 0
    newest_mtime_ns = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name
            for name in dirnames
            if name not in _RUNTIME_CACHE_DIR_NAMES and not name.startswith(".")
        ]
        for filename in filenames:
            try:
                stat = os.stat(os.path.join(dirpath, filename))
            except OSError:
                continue
            file_count += 1
            total_size += stat.st_size
            newest_mtime_ns = max(newest_mtime_ns, stat.st_mtime_ns)
    return file_count, total_size, newest_mtime_ns


def _logical_bundle_dir_name(bundle_dir: str | Path, explicit: str = "") -> str:
    """The folder name the manifest should be validated against.

    A file-installed extension's canonical path is a **symlink** into an
    immutable release directory, so `Path(bundle_dir).resolve()` yields the
    release leaf — `1.0.1-<digest>` — and the manifest check then rejects the
    extension for not living in `audit_lifecycle_fixture`.

    That is `AUDIT-075`: install 1.0.0, update to 1.0.1, disable, and the enable
    fails with *"must live in folder 'audit_lifecycle_fixture', not
    '1.0.1-<digest>'"*, because enable compiles the canonical path while install
    passed the logical name explicitly.

    An explicit name from the caller always wins. Otherwise, when the requested
    path's own name differs from the resolved leaf, the requested name is the
    logical one — that is exactly the symlink case, and it leaves a plain
    directory untouched.
    """
    if str(explicit or "").strip():
        return str(explicit).strip()
    requested = Path(bundle_dir)
    try:
        resolved_name = requested.resolve().name
    except OSError:  # pragma: no cover - unreadable path is handled downstream
        return ""
    if requested.name and requested.name != resolved_name:
        return requested.name
    return ""


def compile_extension_bundle(
    bundle_dir: str | Path,
    expected_kind: str | None = None,
    *,
    logical_bundle_dir_name: str = "",
) -> dict:
    import copy

    resolved = Path(bundle_dir).resolve()
    if not resolved.is_dir():
        return _compile_extension_bundle_uncached(
            bundle_dir,
            expected_kind,
            logical_bundle_dir_name=logical_bundle_dir_name,
        )
    cache_key = (
        str(resolved),
        str(expected_kind or "").strip(),
        # Derived, not just passed: two callers reaching the same release through
        # different paths (the canonical symlink and the release dir) must not
        # share a cache entry, because they validate against different names.
        _logical_bundle_dir_name(bundle_dir, logical_bundle_dir_name),
    )
    signature = _bundle_content_signature(resolved)
    cached = _COMPILE_CACHE.get(cache_key)
    if cached is not None and cached[0] == signature:
        return copy.deepcopy(cached[1])
    result = _compile_extension_bundle_uncached(
        bundle_dir,
        expected_kind,
        logical_bundle_dir_name=logical_bundle_dir_name,
    )
    if len(_COMPILE_CACHE) >= _COMPILE_CACHE_MAX_ENTRIES:
        _COMPILE_CACHE.clear()
    _COMPILE_CACHE[cache_key] = (signature, copy.deepcopy(result))
    return result


def _compile_extension_bundle_uncached(
    bundle_dir: str | Path,
    expected_kind: str | None = None,
    *,
    logical_bundle_dir_name: str = "",
) -> dict:
    resolved_bundle = Path(bundle_dir).resolve()
    logical_bundle_dir_name = _logical_bundle_dir_name(bundle_dir, logical_bundle_dir_name)
    kind = str(expected_kind or "").strip()

    if not resolved_bundle.exists() or not resolved_bundle.is_dir():
        return {
            "bundle_root": str(resolved_bundle),
            "status": "invalid",
            "scan_error": f"Bundle '{resolved_bundle}' does not exist",
            "compile_errors": [
                _issue(
                    severity="error",
                    code="bundle_not_found",
                    message=f"Bundle '{resolved_bundle}' does not exist",
                    file_path=str(resolved_bundle),
                )
            ],
            "compile_warnings": [],
            "validation_errors": [],
            "validation_warnings": [],
            "compiled_manifest": {},
        }

    forbidden_contract_files = _forbidden_contract_json_files(resolved_bundle)
    yaml_only_issues = [
        _yaml_only_violation_issue(resolved_bundle, path) for path in forbidden_contract_files
    ]

    manifest_path = _discover_manifest_path(resolved_bundle)
    if manifest_path is None:
        compile_errors = [
            _issue(
                severity="error",
                code="missing_manifest",
                message="No manifest file found",
                file_path=str(resolved_bundle),
                hint="Add extension.yaml.",
            ),
            *yaml_only_issues,
        ]
        return {
            "extension_key": resolved_bundle.name,
            "kind": kind,
            "display_name": resolved_bundle.name,
            "description": "",
            "bundle_root": str(resolved_bundle),
            "manifest_path": "",
            "features": [],
            "required_scopes": [],
            "supported_events": [],
            "event_subscriptions": [],
            "schemas": {},
            "healthcheck": {},
            "entrypoint": {},
            "manifest": {},
            PYTHON_REQUIREMENTS_FIELD: [],
            "version": "",
            "checksum": "",
            "manifest_version": 0,
            "status": "invalid",
            "scan_error": "No manifest file found",
            "compile_errors": compile_errors,
            "compile_warnings": [],
            "validation_errors": [],
            "validation_warnings": [],
            "compiled_manifest": {},
            "query_definitions": [],
            "action_definitions": [],
            "ui_manifest": {},
            "lifecycle": {
                "scan": "failed",
                "compile": "failed",
                "validate": "blocked",
                "preview": "blocked",
                "enable": "blocked",
            },
        }

    issues: list[dict] = list(yaml_only_issues)
    try:
        manifest = _load_manifest_file(manifest_path)
    except Exception as exc:
        return {
            "extension_key": resolved_bundle.name,
            "kind": kind,
            "display_name": resolved_bundle.name,
            "description": "",
            "bundle_root": str(resolved_bundle),
            "manifest_path": str(manifest_path.resolve()),
            "features": [],
            "required_scopes": [],
            "supported_events": [],
            "event_subscriptions": [],
            "schemas": {},
            "healthcheck": {},
            "entrypoint": {},
            "manifest": {},
            PYTHON_REQUIREMENTS_FIELD: [],
            "version": "",
            "checksum": "",
            "manifest_version": 0,
            "status": "invalid",
            "scan_error": str(exc),
            "compile_errors": [
                _issue(
                    severity="error",
                    code="manifest_load_error",
                    message=str(exc),
                    file_path=str(manifest_path),
                )
            ],
            "compile_warnings": [],
            "validation_errors": [],
            "validation_warnings": [],
            "compiled_manifest": {},
            "query_definitions": [],
            "action_definitions": [],
            "ui_manifest": {},
            "lifecycle": {
                "scan": "failed",
                "compile": "failed",
                "validate": "blocked",
                "preview": "blocked",
                "enable": "blocked",
            },
        }

    normalized = _normalize_manifest_basics(
        resolved_bundle,
        manifest_path,
        kind,
        manifest,
        issues,
        logical_bundle_dir_name,
    )
    # The pages resolve first: the dictionaries are checked against the keys
    # their text uses as well as the contract's.
    resolved_ui_manifest = _resolve_ui_manifest(
        bundle_root=resolved_bundle,
        manifest_path=manifest_path,
        ui_manifest_ref=str(normalized.get("ui_manifest_ref") or "").strip(),
        issues=issues,
    )
    contract_validation = validate_extension_contract_v2(
        manifest,
        bundle_root=resolved_bundle,
        ui_manifest=resolved_ui_manifest,
    )
    for section, rows in _as_dict(contract_validation.get("normalized_contract")).items():
        normalized[section] = rows
    normalized["locale_bundles"] = _as_dict(contract_validation.get("locale_bundles"))
    normalized["locale_codes"] = _as_list(contract_validation.get("locale_codes"))

    normalized["schemas"] = _resolve_schemas(
        bundle_root=resolved_bundle,
        manifest_path=manifest_path,
        schemas=_as_dict(normalized.get("schemas")),
        issues=issues,
    )

    normalized["ui_manifest"] = resolved_ui_manifest

    normalized["query_definitions"] = _resolve_query_manifest(
        bundle_root=resolved_bundle,
        manifest_path=manifest_path,
        ref_text=str(normalized.get("query_manifest_ref") or "").strip(),
        issues=issues,
    )
    normalized["action_definitions"] = _resolve_action_manifest(
        bundle_root=resolved_bundle,
        manifest_path=manifest_path,
        ref_text=str(normalized.get("action_manifest_ref") or "").strip(),
        issues=issues,
    )
    issues.extend(
        validate_extension_runtime_boundary(
            bundle_root=resolved_bundle,
            extension_id=str(normalized.get("extension_key") or "").strip(),
            entrypoint=_as_dict(normalized.get("entrypoint")),
            healthcheck=_as_dict(normalized.get("healthcheck")),
        )
    )

    compiled_manifest = {
        "extension_id": normalized.get("extension_key"),
        "kind": normalized.get("kind"),
        "version": normalized.get("version"),
        "manifest_version": int(normalized.get("manifest_version") or 0),
        "display_name": normalized.get("display_name"),
        "description": normalized.get("description"),
        "author": normalized.get("author"),
        "vendor_name": normalized.get("vendor_name"),
        "support_url": normalized.get("support_url"),
        "website_url": normalized.get("website_url"),
        "vendor": _as_dict(normalized.get("vendor")),
        "listing": _as_dict(normalized.get("listing")),
        "issues_url": normalized.get("issues_url"),
        "package": _as_dict(normalized.get("package")),
        "billing_mode": normalized.get("billing_mode"),
        "upgrade_notes": normalized.get("upgrade_notes"),
        "screenshots": _as_list(normalized.get("screenshots")),
        "step_ui_manifest": _as_list(normalized.get("step_ui_manifest")),
        "runtime": _as_dict(normalized.get("runtime")),
        "features": _normalize_string_list(normalized.get("features")),
        "required_scopes": _normalize_string_list(normalized.get("required_scopes")),
        "runtime_grants": normalized.get("runtime_grants") or {},
        "supported_events": _normalize_string_list(normalized.get("supported_events")),
        "supported_url_schemes": _normalize_string_list(normalized.get("supported_url_schemes")),
        "event_subscriptions": _normalize_event_subscriptions(
            normalized.get("event_subscriptions")
        ),
        "entrypoint": _as_dict(normalized.get("entrypoint")),
        "healthcheck": _as_dict(normalized.get("healthcheck")),
        "schemas": _as_dict(normalized.get("schemas")),
        "ui_manifest_ref": str(normalized.get("ui_manifest_ref") or "").strip(),
        "query_manifest_ref": str(normalized.get("query_manifest_ref") or "").strip(),
        "action_manifest_ref": str(normalized.get("action_manifest_ref") or "").strip(),
        "ui_manifest": _as_dict(normalized.get("ui_manifest")),
        "query_definitions": _as_list(normalized.get("query_definitions")),
        "action_definitions": _as_list(normalized.get("action_definitions")),
        "machine_auth": _as_dict(normalized.get("machine_auth")),
        "oauth_providers": _as_list(normalized.get("oauth_providers")),
        "operation_manifest": _as_list(normalized.get("operation_manifest")),
        "account_scope": _as_dict(normalized.get("account_scope")),
        "callbacks": _callback_definitions_value(normalized.get("callbacks")),
        "connection_types": _as_list(normalized.get("connection_types")),
        "workflow_triggers": _workflow_trigger_definitions_value(
            normalized.get("workflow_triggers")
        ),
        "external_effects": _as_list(normalized.get("external_effects")),
        "artifact_policies": _as_list(normalized.get("artifact_policies")),
        "project_state": _as_dict(normalized.get("project_state")),
        "lifecycle": _as_dict(normalized.get("extension_lifecycle")),
        "locale_bundles": _as_dict(normalized.get("locale_bundles")),
        "locale_codes": _as_list(normalized.get("locale_codes")),
        "release_stage": str(normalized.get("release_stage") or "active"),
        "manifest_path": str(normalized.get("manifest_path") or ""),
        "bundle_root": str(normalized.get("bundle_root") or ""),
        "bundle_dir_name": str(normalized.get("bundle_dir_name") or ""),
        "expected_bundle_dir_name": str(normalized.get("expected_bundle_dir_name") or ""),
        "dependencies": _as_dict(normalized.get("dependencies")),
        PYTHON_REQUIREMENTS_FIELD: _as_list(normalized.get(PYTHON_REQUIREMENTS_FIELD)),
    }

    validation = validate_compiled_manifest(compiled_manifest)
    compile_errors, compile_warnings = _split_issues(issues)
    validation_errors = [dict(item) for item in validation.get("errors") or []]
    validation_errors.extend(host_contract_errors(compiled_manifest))
    validation_errors.extend(
        _validate_workflow_action_operation_parity(
            compiled_manifest,
            contract_present=bool(contract_validation.get("present")),
        )
    )
    validation_warnings = [dict(item) for item in validation.get("warnings") or []]
    validation_errors.extend(dict(item) for item in contract_validation.get("errors") or [])
    validation_warnings.extend(dict(item) for item in contract_validation.get("warnings") or [])
    # What the extension's own code does in plain sight; not the security boundary.
    code_errors, code_warnings = scan_extension_code(
        str(normalized.get("bundle_root") or ""),
        [
            ("entrypoint", _as_dict(compiled_manifest.get("entrypoint")).get("command")),
            ("health", _as_dict(compiled_manifest.get("healthcheck")).get("command")),
        ],
    )
    validation_errors.extend(code_errors)
    validation_warnings.extend(code_warnings)
    # Where the extension says it connects; declared, shown, not enforced.
    egress, egress_errors, egress_warnings = validate_egress(
        {"egress": normalized.get("egress")} if normalized.get("egress") is not None else {},
        _as_list(compiled_manifest.get("connection_types")),
    )
    compiled_manifest["egress"] = egress
    validation_errors.extend(egress_errors)
    validation_warnings.extend(egress_warnings)

    all_errors = len(compile_errors) + len(validation_errors)
    normalized["checksum"] = _manifest_checksum(manifest_path, _as_dict(normalized.get("manifest")))
    normalized["compiled_manifest"] = compiled_manifest
    normalized["compile_errors"] = compile_errors
    normalized["compile_warnings"] = compile_warnings
    normalized["validation_errors"] = validation_errors
    normalized["validation_warnings"] = validation_warnings
    normalized["scan_error"] = (
        ""
        if all_errors == 0
        else "; ".join(
            [
                str(item.get("message") or "").strip()
                for item in (compile_errors + validation_errors)
                if str(item.get("message") or "").strip()
            ]
        )[:2000]
    )

    normalized["status"] = "valid" if all_errors == 0 else "invalid"
    release_stage = str(normalized.get("release_stage") or "active").strip().lower()
    normalized["lifecycle"] = {
        "scan": "ok",
        "compile": "ok" if not compile_errors else "failed",
        "validate": "ok" if not validation_errors else "failed",
        "preview": "ready" if all_errors == 0 else "blocked",
        "enable": "eligible" if all_errors == 0 and release_stage == "active" else "blocked",
        "enable_reason": "placeholder" if release_stage == "placeholder" else "",
    }
    return normalized


# ".fs-python" is the installed-dependency cache (PYTHON_DEPS_ROOT_DIRNAME): a folder
# holding only that is what an uninstall or a failed install left, not an extension.
_RUNTIME_CACHE_DIR_NAMES = frozenset(
    {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".fs-python"}
)
_RUNTIME_CACHE_FILE_SUFFIXES = frozenset({".pyc", ".pyo"})


def _runtime_cache_artifact_state(path: Path) -> tuple[bool, bool]:
    if path.name in _RUNTIME_CACHE_DIR_NAMES:
        return True, True
    if path.is_file():
        is_cache_file = path.suffix in _RUNTIME_CACHE_FILE_SUFFIXES
        return is_cache_file, is_cache_file
    if not path.is_dir():
        return False, False

    try:
        children = list(path.iterdir())
    except OSError:
        return False, False
    if not children:
        return True, False

    saw_cache_artifact = False
    for child in children:
        child_only_cache, child_saw_cache = _runtime_cache_artifact_state(child)
        if not child_only_cache:
            return False, saw_cache_artifact or child_saw_cache
        saw_cache_artifact = saw_cache_artifact or child_saw_cache
    return True, saw_cache_artifact


def _contains_only_runtime_cache_artifacts(path: Path) -> bool:
    only_cache_artifacts, saw_cache_artifact = _runtime_cache_artifact_state(path)
    return only_cache_artifacts and saw_cache_artifact


def scan_extensions(root: str | None = None) -> list[dict]:
    extensions_root = Path(root or default_extensions_root()).resolve()
    if not extensions_root.exists():
        return []

    discovered: list[dict] = []
    folder_names_seen: dict[str, str] = {}
    for child in sorted(extensions_root.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith(".") or child.name == "__pycache__":
            continue
        if _discover_manifest_path(child) is None and _contains_only_runtime_cache_artifacts(child):
            continue
        compiled = compile_extension_bundle(child)
        compiled.update(extension_host_metadata(compiled))
        compiled.setdefault("bundle_dir_name", child.name)
        normalized_folder = child.name.lower()
        existing_folder = folder_names_seen.get(normalized_folder)
        if existing_folder and existing_folder != child.name:
            collision_issue = _issue(
                severity="error",
                code="duplicate_extension_folder",
                message=f"Duplicate extension folder name '{child.name}' conflicts with '{existing_folder}'",
                file_path=str(child),
                field_path="bundle_root",
            )
            compiled.setdefault("compile_errors", []).append(collision_issue)
            compiled["status"] = "invalid"
            compiled["scan_error"] = str(collision_issue.get("message") or "")
        folder_names_seen.setdefault(normalized_folder, child.name)
        discovered.append(compiled)
    by_extension_id: dict[str, list[dict]] = {}
    for item in discovered:
        extension_id = str(item.get("extension_key") or "").strip()
        if extension_id:
            by_extension_id.setdefault(extension_id, []).append(item)
    for extension_id, items in by_extension_id.items():
        roots = {str(item.get("bundle_root") or "").strip() for item in items}
        if len(roots) <= 1:
            continue
        for item in items:
            collision_issue = _issue(
                severity="error",
                code="duplicate_extension_id",
                message=(
                    f"Duplicate extension_id '{extension_id}' found in multiple local extension folders"
                ),
                file_path=str(item.get("manifest_path") or item.get("bundle_root") or ""),
                field_path="extension_id",
            )
            item.setdefault("validation_errors", []).append(collision_issue)
            item["status"] = "invalid"
            item["scan_error"] = str(collision_issue.get("message") or "")
    return discovered


def compile_extension_preview(extension: dict) -> dict:
    compiled_manifest = _as_dict(extension.get("compiled_manifest"))
    return {
        "extension_id": str(extension.get("extension_key") or "").strip(),
        "kind": str(extension.get("kind") or "").strip(),
        "version": str(extension.get("version") or "").strip(),
        "status": str(extension.get("status") or "invalid").strip() or "invalid",
        "manifest_version": int(extension.get("manifest_version") or 0),
        "required_scopes": _normalize_string_list(extension.get("required_scopes")),
        "features": _normalize_string_list(extension.get("features")),
        "supported_events": _normalize_string_list(extension.get("supported_events")),
        "event_subscriptions": _normalize_event_subscriptions(
            extension.get("event_subscriptions")
            or _as_dict(extension.get("compiled_manifest")).get("event_subscriptions")
        ),
        "query_ids": [
            str(_as_dict(item).get("query_id") or "").strip()
            for item in _as_list(extension.get("query_definitions"))
            if str(_as_dict(item).get("query_id") or "").strip()
        ],
        "action_ids": [
            str(_as_dict(item).get("action_id") or "").strip()
            for item in _as_list(extension.get("action_definitions"))
            if str(_as_dict(item).get("action_id") or "").strip()
        ],
        "callbacks": _as_list(
            extension.get("callbacks")
            or _as_dict(extension.get("compiled_manifest")).get("callbacks")
        ),
        "external_effects": _as_list(
            extension.get("external_effects")
            or _as_dict(extension.get("compiled_manifest")).get("external_effects")
        ),
        "project_state": _as_dict(
            extension.get("project_state")
            or _as_dict(extension.get("compiled_manifest")).get("project_state")
        ),
        "ui": {
            "page_count": len(_as_list(_as_dict(extension.get("ui_manifest")).get("pages"))),
        },
        "lifecycle": _as_dict(extension.get("lifecycle")),
        "compile_errors": _as_list(extension.get("compile_errors")),
        "validation_errors": _as_list(extension.get("validation_errors")),
        "compiled_manifest": compiled_manifest,
    }


__all__ = [
    "validate_compiled_manifest",
    "compile_extension_bundle",
    "scan_extensions",
    "compile_extension_preview",
]
