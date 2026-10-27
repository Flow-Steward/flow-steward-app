#!/usr/bin/env python3
# mypy: ignore-errors
"""Validate extension bundles and optional health checks.

The public CLI binds directly to :func:`validate_extensions`.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import yaml

from core.application.extensions.identity import extension_folder_name, is_canonical_extension_id
from core.application.extensions.json_schema_subset import unsupported_schema_keywords
from core.application.tooling.extensions.models import (
    ExtensionValidationRequest,
    ExtensionValidationResult,
)
from core.application.tooling.extensions.workflow_annotations import workflow_annotation_errors
from core.infrastructure.extension_runtime.bundle_icon import extension_icon_issues
from core.infrastructure.extension_runtime.extension_runtime import (
    compile_extension_bundle,
    compile_extension_preview,
    default_extensions_root,
    run_extension_health,
)


def _extension_label(item: dict[str, Any]) -> str:
    key = str(item.get("extension_key") or "").strip() or "unknown"
    kind = str(item.get("kind") or "").strip() or "unknown_kind"
    version = str(item.get("version") or "").strip() or "n/a"
    return f"{key} [{kind}] v{version}"


def _print_manifest_result(item: dict[str, Any]) -> None:
    status = str(item.get("status") or "").strip().lower()
    label = _extension_label(item)
    if status == "valid":
        lifecycle = item.get("lifecycle") or {}
        compile_status = str((lifecycle or {}).get("compile") or "ok").strip()
        validate_status = str((lifecycle or {}).get("validate") or "ok").strip()
        print(
            f"[PASS] {label} manifest valid (compile={compile_status} validate={validate_status})",
            flush=True,
        )
        return
    errors = list(item.get("compile_errors") or []) + list(item.get("validation_errors") or [])
    first = dict(errors[0] or {}) if errors else {}
    first_msg = str(first.get("message") or "").strip()
    first_field = str(first.get("field_path") or "").strip()
    first_file = str(first.get("file_path") or "").strip()
    hint = str(first.get("hint") or "").strip()
    error = str(item.get("scan_error") or first_msg or "manifest validation failed").strip()
    suffix_parts = []
    if first_file:
        suffix_parts.append(f"file={first_file}")
    if first_field:
        suffix_parts.append(f"field={first_field}")
    if hint:
        suffix_parts.append(f"hint={hint}")
    suffix = f" ({'; '.join(suffix_parts)})" if suffix_parts else ""
    print(f"[FAIL] {label} manifest invalid :: {error}{suffix}", flush=True)


def _print_health_result(item: dict[str, Any], health: dict[str, Any]) -> bool:
    label = _extension_label(item)
    status = str(health.get("status") or "").strip().lower()
    detail = str(health.get("detail") or "").strip()
    if status == "healthy":
        print(f"[PASS] {label} health healthy", flush=True)
        return True
    print(f"[FAIL] {label} health {status or 'unknown'} :: {detail}", flush=True)
    return False


def _is_valid(item: dict[str, Any]) -> bool:
    return str(item.get("status") or "").strip().lower() == "valid"


def _result_has_failures(item: dict[str, Any], health: dict[str, Any] | None = None) -> bool:
    health_status = str((health or {}).get("status") or "").strip().lower()
    return (
        not _is_valid(item)
        or bool(list(item.get("compile_errors") or []))
        or bool(list(item.get("validation_errors") or []))
        or (health is not None and health_status != "healthy")
    )


def _marketplace_errors(item: dict[str, Any]) -> list[str]:
    compiled = item.get("compiled_manifest")
    manifest = item.get("manifest")
    if isinstance(compiled, dict):
        payload = compiled
    elif isinstance(manifest, dict):
        payload = manifest
    else:
        payload = {}

    errors: list[str] = []
    required_text_fields = ("extension_id", "display_name", "description", "kind", "version")
    for field in required_text_fields:
        if not str(payload.get(field) or "").strip():
            errors.append(f"missing required marketplace field: {field}")

    vendor = payload.get("vendor")
    if (
        not isinstance(vendor, dict)
        or not str(vendor.get("name") or vendor.get("vendor_name") or "").strip()
    ):
        errors.append("vendor.name is required for marketplace")

    listing = payload.get("listing")
    if not isinstance(listing, dict):
        errors.append("listing section is required for marketplace")
    else:
        if not str(listing.get("category") or "").strip():
            errors.append("listing.category is required for marketplace")
        if not str(listing.get("summary") or "").strip():
            errors.append("listing.summary is required for marketplace")

    scopes = payload.get("required_scopes")
    if not isinstance(scopes, list) or not [s for s in scopes if str(s or "").strip()]:
        errors.append("required_scopes must contain at least one scope")

    machine_auth = payload.get("machine_auth")
    if not isinstance(machine_auth, dict):
        errors.append("machine_auth section is required for marketplace")
    else:
        if not str(machine_auth.get("service_account_key") or "").strip():
            errors.append("machine_auth.service_account_key is required")
        auth_scopes = machine_auth.get("required_scopes")
        if not isinstance(auth_scopes, list) or not [
            s for s in auth_scopes if str(s or "").strip()
        ]:
            errors.append("machine_auth.required_scopes must contain at least one scope")

    account_scope = payload.get("account_scope")
    if not isinstance(account_scope, dict):
        errors.append("account_scope section is required for marketplace")
    else:
        if not str(account_scope.get("scope_type") or "").strip():
            errors.append("account_scope.scope_type is required")
        if not str(account_scope.get("description") or "").strip():
            errors.append("account_scope.description is required")
    return errors


def _ui_pages_structure_errors(bundle_root: Path) -> list[str]:
    ui_manifest_path = bundle_root / "ui" / "ui_manifest.yaml"
    pages_dir = bundle_root / "ui" / "pages"
    if not ui_manifest_path.exists():
        return ["missing ui/ui_manifest.yaml"]
    if not pages_dir.exists():
        return ["missing ui/pages directory"]

    page_files = sorted(pages_dir.glob("*.yaml"))
    if not page_files:
        return ["ui/pages must contain at least one *.yaml page file"]

    try:
        payload = yaml.safe_load(ui_manifest_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        return [f"ui/ui_manifest.yaml is invalid YAML: {exc}"]

    pages = payload.get("pages")
    if not isinstance(pages, list) or not pages:
        return ["ui/ui_manifest.yaml.pages must be a non-empty list"]

    errors: list[str] = []
    for idx, page in enumerate(pages):
        if not isinstance(page, dict):
            errors.append(f"ui_manifest.pages[{idx}] must be an object")
            continue
        ref = str(page.get("ref") or "").strip()
        if not ref:
            errors.append(f"ui_manifest.pages[{idx}] must use ref: pages/<name>.yaml")
            continue
        if not ref.startswith("pages/") or not ref.endswith(".yaml"):
            errors.append(f"ui_manifest.pages[{idx}] ref must point to pages/*.yaml (got: {ref})")
            continue
        target = (ui_manifest_path.parent / ref).resolve()
        if not target.exists():
            errors.append(f"ui_manifest.pages[{idx}] ref target not found: {ref}")
    return errors


def _artifact_module_errors(bundle_root: Path) -> list[str]:
    descriptor_path = bundle_root / "extension-descriptor.yaml"
    if not descriptor_path.is_file():
        return []
    try:
        descriptor = yaml.safe_load(descriptor_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        return [f"extension-descriptor.yaml is invalid YAML: {exc}"]
    errors = workflow_annotation_errors(descriptor, bundle_root)
    artifacts = descriptor.get("artifacts") if isinstance(descriptor, dict) else {}
    for artifact_kind, rows in (artifacts or {}).items():
        for index, row in enumerate(rows if isinstance(rows, list) else []):
            ref = str((row or {}).get("ref") or "").strip() if isinstance(row, dict) else ""
            if not ref:
                continue
            artifact_path = (bundle_root / ref).resolve()
            try:
                artifact_path.relative_to(bundle_root.resolve())
            except ValueError:
                errors.append(f"artifacts.{artifact_kind}[{index}] escapes the bundle: {ref}")
                continue
            if not artifact_path.is_file():
                errors.append(f"artifacts.{artifact_kind}[{index}] ref target not found: {ref}")
                continue
            try:
                payload = yaml.safe_load(artifact_path.read_text(encoding="utf-8")) or {}
            except Exception as exc:
                errors.append(f"{ref} is invalid YAML: {exc}")
                continue
            implementation = payload.get("implementation") if isinstance(payload, dict) else {}
            module = str((implementation or {}).get("module") or "").strip()
            if module == "core" or module.startswith("core."):
                errors.append(
                    f"{ref} implementation.module must not reference private core: {module}"
                )
    return errors


def _unexecutable_schema_errors(bundle_root: Path) -> list[str]:
    """Report schema keywords the platform will not execute.

    Deliberately narrow: this is not schema validation, only the one question an
    author cannot answer for themselves - will the platform actually run what I
    declared? Getting that answer here, before install, is the difference
    between fixing a schema and shipping a rule that never fires.
    """
    findings: list[str] = []
    for path in sorted(bundle_root.rglob("*.schema.yaml")):
        try:
            schema = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            findings.append(f"{path.name}: could not be read as YAML ({exc})")
            continue
        findings.extend(
            f"{path.name}: {finding}" for finding in unsupported_schema_keywords(schema)
        )
    manifest = bundle_root / "extension.yaml"
    if manifest.is_file():
        try:
            payload = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            findings.append(f"extension.yaml: could not be read as YAML ({exc})")
        else:
            # Inline connection/secret schemas in the manifest are what the
            # platform actually serves, so they matter at least as much as the
            # files beside them.
            for label, schema in _inline_schemas(payload):
                findings.extend(
                    f"extension.yaml {label}: {finding}"
                    for finding in unsupported_schema_keywords(schema)
                )
    return findings


def _inline_schemas(node: Any, path: str = "") -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            where = f"{path}.{key}" if path else str(key)
            if key in {"connection_schema", "secret_schema"} and isinstance(value, dict):
                found.append((where, value))
            found.extend(_inline_schemas(value, where))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_inline_schemas(value, f"{path}[{index}]"))
    return found


def _validate_one(request: ExtensionValidationRequest, root: str) -> tuple[int, dict[str, Any]]:
    extension_id = str(request.extension_id or "").strip()
    bundle_name = (
        extension_folder_name(extension_id)
        if is_canonical_extension_id(extension_id)
        else extension_id
    )
    bundle_root = Path(root).resolve() / bundle_name
    item = dict(compile_extension_bundle(bundle_root))
    key = str(item.get("extension_key") or extension_id).strip()
    preview = compile_extension_preview(item) if key else {}
    health: dict[str, Any] = {}
    marketplace_errors: list[str] = []
    ui_pages_errors: list[str] = []
    artifact_module_errors: list[str] = []
    schema_keyword_errors: list[str] = []

    _print_manifest_result(item)

    # The logo is checked whatever the manifest says, so one run names every
    # problem a developer has to fix before the bundle can ship.
    icon_errors = extension_icon_issues(bundle_root)
    for issue in icon_errors:
        print(
            f"[FAIL] {_extension_label(item)} icon invalid :: {issue['message']} "
            f"(file={issue['file_path']}; field={issue['field_path']}; hint={issue['hint']})",
            flush=True,
        )
    if not icon_errors:
        print(f"[PASS] {_extension_label(item)} icon.png valid", flush=True)

    if _is_valid(item) and request.health:
        health = dict(run_extension_health(item) or {})
        _print_health_result(item, health)

    if _is_valid(item) and bool(request.marketplace):
        marketplace_errors = _marketplace_errors(item)
        if marketplace_errors:
            print(
                f"[FAIL] {_extension_label(item)} marketplace checks failed :: {marketplace_errors[0]}",
                flush=True,
            )
        else:
            print(f"[PASS] {_extension_label(item)} marketplace checks passed", flush=True)

    if _is_valid(item):
        ui_pages_errors = _ui_pages_structure_errors(bundle_root)
        if ui_pages_errors:
            print(
                f"[FAIL] {_extension_label(item)} UI pages checks failed :: {ui_pages_errors[0]}",
                flush=True,
            )
        else:
            print(f"[PASS] {_extension_label(item)} UI pages checks passed", flush=True)
        artifact_module_errors = _artifact_module_errors(bundle_root)
        if artifact_module_errors:
            print(
                f"[FAIL] {_extension_label(item)} artifact module checks failed :: "
                f"{artifact_module_errors[0]}",
                flush=True,
            )
        schema_keyword_errors = _unexecutable_schema_errors(bundle_root)
        if schema_keyword_errors:
            print(
                f"[FAIL] {_extension_label(item)} schema keyword checks failed :: "
                f"{schema_keyword_errors[0]}",
                flush=True,
            )
        else:
            print(
                f"[PASS] {_extension_label(item)} schema keywords are all executable",
                flush=True,
            )

    failed = (
        _result_has_failures(item, health if request.health else None)
        or bool(marketplace_errors)
        or bool(ui_pages_errors)
        or bool(artifact_module_errors)
        or bool(schema_keyword_errors)
        or bool(icon_errors)
    )
    payload = {
        "ok": not failed,
        "root": root,
        "extension_id": extension_id,
        "extension": item,
        "preview": preview,
        "health": health,
        "marketplace": {
            "checked": bool(request.marketplace),
            "errors": marketplace_errors,
            "ok": not bool(marketplace_errors),
        },
        "ui_pages": {
            "errors": ui_pages_errors,
            "ok": not bool(ui_pages_errors),
        },
        "artifact_modules": {
            "errors": artifact_module_errors,
            "ok": not bool(artifact_module_errors),
        },
        "schema_keywords": {
            "errors": schema_keyword_errors,
            "ok": not bool(schema_keyword_errors),
        },
        "icon": {
            "errors": icon_errors,
            "ok": not bool(icon_errors),
        },
    }
    if failed:
        print(f"[summary] extension={key or extension_id} ok=false", flush=True)
    else:
        print(f"[summary] extension={key or extension_id} ok=true", flush=True)

    return (1 if failed else 0), payload


def validate_extensions(request: ExtensionValidationRequest) -> ExtensionValidationResult:
    """Validate an extension bundle without invoking a CLI parser."""

    command = ExtensionValidationRequest.model_validate(request)
    root = str(command.root or "").strip() or default_extensions_root()
    output = io.StringIO()
    with redirect_stdout(output):
        exit_code, payload = _validate_one(command, root)
    return ExtensionValidationResult(
        status="passed" if exit_code == 0 else "failed",
        extension_id=command.extension_id,
        exit_code=exit_code,
        output=output.getvalue(),
        payload=payload,
    )
