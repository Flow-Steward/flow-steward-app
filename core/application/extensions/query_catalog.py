"""Catalog metadata helpers for installed extension projections."""

from __future__ import annotations

from typing import Any

from core.application.extensions.display_labels import descriptor_display_name

from .service_support import _contains_kind_from_artifact_kind, _normalize_text


def _catalog_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _descriptor_catalog_fields(release: Any | None) -> dict[str, Any]:
    if release is None:
        return {}
    descriptor = getattr(release, "descriptor", {})
    descriptor = descriptor if isinstance(descriptor, dict) else {}
    identity_raw = descriptor.get("identity")
    identity: dict[str, Any] = identity_raw if isinstance(identity_raw, dict) else {}
    vendor_raw = descriptor.get("vendor")
    vendor: dict[str, Any] = vendor_raw if isinstance(vendor_raw, dict) else {}
    listing_raw = descriptor.get("listing")
    listing: dict[str, Any] = listing_raw if isinstance(listing_raw, dict) else {}
    tags_raw = listing.get("tags") or identity.get("tags")
    tags = [
        _catalog_text(item)
        for item in (tags_raw if isinstance(tags_raw, list) else [])
        if _catalog_text(item)
    ]
    return {
        "display_name": descriptor_display_name(descriptor),
        "description": _catalog_text(identity.get("description") or descriptor.get("description")),
        "extension_type": _catalog_text(descriptor.get("extension_type") or descriptor.get("kind")),
        "vendor_name": _catalog_text(
            vendor.get("vendor_name")
            or vendor.get("name")
            or descriptor.get("vendor_name")
            or descriptor.get("author")
        ),
        "category": _catalog_text(listing.get("category")),
        "tags": list(dict.fromkeys(tags)),
        "support_url": _catalog_text(vendor.get("support_url") or descriptor.get("support_url")),
        "website_url": _catalog_text(
            vendor.get("website_url") or vendor.get("url") or descriptor.get("website_url")
        ),
    }


def _has_runtime_module(*, release: Any | None, release_artifacts: list[Any]) -> bool:
    if any(
        _contains_kind_from_artifact_kind(getattr(row, "artifact_kind", "")) == "runtime_module"
        for row in release_artifacts
    ):
        return True
    execution_modes = getattr(release, "execution_modes", None) if release is not None else None
    if isinstance(execution_modes, list) and any(_normalize_text(mode) for mode in execution_modes):
        return True
    descriptor = getattr(release, "descriptor", {}) if release is not None else {}
    descriptor = descriptor if isinstance(descriptor, dict) else {}
    runtime_raw = descriptor.get("runtime")
    runtime: dict[str, Any] = runtime_raw if isinstance(runtime_raw, dict) else {}
    # Any populated runtime section marks a runtime module: legacy descriptors
    # carry runtime.contract_version, bundle descriptors carry
    # runtime.extension_contract_v2 contract references.
    if runtime:
        return True
    entrypoint_raw = descriptor.get("entrypoint")
    entrypoint: dict[str, Any] = entrypoint_raw if isinstance(entrypoint_raw, dict) else {}
    if entrypoint.get("command") or _normalize_text(entrypoint.get("mode")):
        return True
    artifacts_raw = descriptor.get("artifacts")
    artifacts: dict[str, Any] = artifacts_raw if isinstance(artifacts_raw, dict) else {}
    extension_entries = (
        artifacts.get("extensions") if isinstance(artifacts.get("extensions"), list) else []
    )
    return bool(extension_entries)


__all__ = [
    "_descriptor_catalog_fields",
    "_has_runtime_module",
]
