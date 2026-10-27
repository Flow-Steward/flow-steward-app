"""Resolve browser assets declared by installed extension UI contracts."""

from __future__ import annotations

import hmac
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from core.application.management.errors import ServiceNotFoundError
from core.infrastructure.extension_runtime.browser_permissions import browser_permissions

_MESSAGE_EXTENSION_BROWSER_ASSET_NOT_FOUND = "Extension browser asset not found"

_ASSET_REF = re.compile(r"assets/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.js")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_MAX_BROWSER_ASSET_BYTES = 256 * 1024


def _picker_declarations(value: Any) -> Iterator[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        declaration = value.get("external_resource_picker")
        if isinstance(declaration, Mapping):
            yield declaration
        if str(value.get("type") or "").strip() == "external_resource_picker":
            data = value.get("data")
            if isinstance(data, Mapping):
                yield data
        for nested in value.values():
            yield from _picker_declarations(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _picker_declarations(nested)


def _declared_asset_refs(manifest: Mapping[str, Any]) -> set[str]:
    declared: set[str] = set()
    for declaration in _picker_declarations(manifest):
        scripts = declaration.get("scripts")
        if not isinstance(scripts, list):
            continue
        for script in scripts:
            if isinstance(script, Mapping) and set(script) == {"asset_ref"}:
                asset_ref = script.get("asset_ref")
                if isinstance(asset_ref, str) and _ASSET_REF.fullmatch(asset_ref):
                    declared.add(asset_ref)
    return declared


def resolve_declared_browser_asset(manifest: Mapping[str, Any], *, asset_ref: str) -> Path:
    """Return one regular bounded JS file explicitly declared by a Picker component."""
    normalized_ref = str(asset_ref or "").strip()
    if _ASSET_REF.fullmatch(normalized_ref) is None or normalized_ref not in _declared_asset_refs(
        manifest
    ):
        raise ServiceNotFoundError(_MESSAGE_EXTENSION_BROWSER_ASSET_NOT_FOUND)
    bundle_root = Path(str(manifest.get("bundle_root") or "").strip()).resolve()
    candidate = (bundle_root / normalized_ref).resolve()
    if (
        bundle_root == candidate
        or bundle_root not in candidate.parents
        or not candidate.is_file()
        or candidate.stat().st_size > _MAX_BROWSER_ASSET_BYTES
    ):
        raise ServiceNotFoundError(_MESSAGE_EXTENSION_BROWSER_ASSET_NOT_FOUND)
    return candidate


def resolve_authorized_browser_asset(
    manifest: Mapping[str, Any],
    *,
    asset_ref: str,
    extension_id: str,
    install_id: str,
    release_id: str,
    descriptor_sha256: str,
    project_id: str,
    scope: Any,
) -> Path:
    """Resolve one asset only when its immutable install and project authority matches."""
    authority = manifest.get("browser_asset_authority")
    if not isinstance(authority, Mapping):
        raise ServiceNotFoundError(_MESSAGE_EXTENSION_BROWSER_ASSET_NOT_FOUND)
    expected = {
        "extension_id": str(extension_id or "").strip(),
        "install_id": str(install_id or "").strip(),
        "release_id": str(release_id or "").strip(),
        "descriptor_sha256": str(descriptor_sha256 or "").strip().lower(),
        "project_id": str(project_id or "").strip(),
        "account_id": str(getattr(scope, "account_id", "") or "").strip(),
    }
    if (
        not all(expected.values())
        or _SHA256_HEX.fullmatch(expected["descriptor_sha256"]) is None
        or str(authority.get("install_state") or "").strip().lower() != "active"
    ):
        raise ServiceNotFoundError(_MESSAGE_EXTENSION_BROWSER_ASSET_NOT_FOUND)
    allowed_project_ids = getattr(scope, "allowed_project_ids", frozenset())
    if str(getattr(scope, "project_id", "") or "").strip() != expected["project_id"] or (
        allowed_project_ids and expected["project_id"] not in allowed_project_ids
    ):
        raise ServiceNotFoundError(_MESSAGE_EXTENSION_BROWSER_ASSET_NOT_FOUND)
    for key, expected_value in expected.items():
        actual_value = str(authority.get(key) or "").strip()
        if key == "descriptor_sha256":
            actual_value = actual_value.lower()
        if not hmac.compare_digest(actual_value, expected_value):
            raise ServiceNotFoundError(_MESSAGE_EXTENSION_BROWSER_ASSET_NOT_FOUND)
    return resolve_declared_browser_asset(manifest, asset_ref=asset_ref)


__all__ = [
    "browser_permissions",
    "resolve_authorized_browser_asset",
    "resolve_declared_browser_asset",
]
