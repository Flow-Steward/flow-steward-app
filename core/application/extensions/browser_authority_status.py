from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .browser_authority_approval import (
    BROWSER_AUTHORITY_PROVENANCE_KEY,
    browser_authority_approval_matches,
    browser_authority_impact,
    browser_authority_manifest_sha256,
    candidate_browser_tree_sha256,
    resolve_browser_authority_manifest,
)
from .extension_runtime import ExtensionReleaseRecord
from .identity import extension_folder_name
from .install import ExtensionInstallRecord
from .service_support import _normalize_text


def _unverifiable_status(*, approved: bool = False) -> dict[str, Any]:
    return {
        "approval_required": True,
        "approved": approved,
        "matches": False,
        "warning": "External browser authority could not be verified.",
    }


def _resolve_authority_release(
    record: ExtensionInstallRecord,
    release: ExtensionReleaseRecord | None,
) -> tuple[Any | None, dict[str, Any] | None]:
    if release is not None:
        return release, None
    from core.infrastructure.extension_runtime.extension_runtime import (
        compile_extension_bundle,
        default_extensions_root,
    )

    try:
        bundle_root = Path(default_extensions_root()) / extension_folder_name(record.extension_id)
    except ValueError:
        bundle_root = None
    if bundle_root is None or not bundle_root.exists():
        return None, {"approval_required": False, "approved": False, "matches": True}
    compiled = compile_extension_bundle(bundle_root)
    if _normalize_text(compiled.get("status")).lower() != "valid":
        return None, _unverifiable_status()
    descriptor_sha256 = browser_authority_manifest_sha256(compiled)
    return (
        SimpleNamespace(
            extension_id=record.extension_id,
            release_id=_normalize_text(record.release_id) or descriptor_sha256,
            descriptor=compiled,
            descriptor_sha256=descriptor_sha256,
            source_kind="extension_scan",
            provenance=dict(record.provenance or {}),
        ),
        None,
    )


def browser_authority_status(
    *,
    record: ExtensionInstallRecord,
    release: ExtensionReleaseRecord | None,
) -> dict[str, Any]:
    """Project current browser-authority approval state for an installed extension."""
    authority_release, early_status = _resolve_authority_release(record, release)
    if early_status is not None:
        return early_status
    if authority_release is None:
        raise RuntimeError("Browser authority release resolution returned no result")
    stored = dict(record.provenance or {}).get(BROWSER_AUTHORITY_PROVENANCE_KEY)
    stored = dict(stored) if isinstance(stored, dict) else {}
    descriptor = dict(authority_release.descriptor or {})
    try:
        impact = browser_authority_impact(
            account_id=record.account_id,
            project_id="",
            extension_id=record.extension_id,
            install_id=_normalize_text(record.install_id),
            old_release_id=_normalize_text(authority_release.release_id),
            new_release_id=_normalize_text(authority_release.release_id),
            descriptor_sha256=authority_release.descriptor_sha256,
            candidate_tree_sha256=candidate_browser_tree_sha256(descriptor),
            manifest=resolve_browser_authority_manifest(authority_release),
        )
    except Exception:
        return _unverifiable_status(approved=bool(stored))
    return {
        "approval_required": impact.approval_required,
        "current_authority_fingerprint": impact.authority_fingerprint,
        "approved_impact_hash": _normalize_text(stored.get("approved_impact_hash")),
        "approved_authority_fingerprint": _normalize_text(stored.get("authority_fingerprint")),
        "authority_contract_version": impact.authority_contract_version,
        "approved": bool(stored),
        "matches": browser_authority_approval_matches(record.provenance, impact),
        "warning": (
            "This extension runs externally supplied browser code in the signed-in page."
            if impact.approval_required
            else ""
        ),
    }


__all__ = ["browser_authority_status"]
