from __future__ import annotations

import hmac
from types import SimpleNamespace
from typing import Any

from core.application.management.scope import ScopeContext
from core.infrastructure.extension_runtime.extension_runtime import (
    compile_extension_bundle,
    default_extensions_root,
)

from .browser_authority_approval import (
    PreparedBrowserAuthorityApproval,
    browser_authority_approval_matches,
    browser_authority_approval_provenance,
    browser_authority_impact,
    browser_authority_manifest_sha256,
    candidate_browser_tree_sha256,
    current_browser_authority_impact,
    require_browser_authority_approval,
    resolve_browser_authority_manifest,
    with_browser_authority_approval,
)
from .identity import managed_extension_dir
from .service_support import _normalize_text


def authorize_extension_enable_browser_authority(
    *,
    extension_id: str,
    scope: ScopeContext,
    account_id: str,
    store: Any,
    record: Any,
    release: Any | None,
    approved_impact_hash: str,
) -> Any:
    """Persist browser authority approval required by an enable transition."""
    authority_release = release
    if authority_release is None:
        try:
            bundle_root = managed_extension_dir(default_extensions_root(), extension_id)
        except ValueError:
            bundle_root = None
        if bundle_root is not None and bundle_root.exists():
            compiled = compile_extension_bundle(bundle_root)
            if _normalize_text(compiled.get("status")).lower() == "valid":
                descriptor_sha256 = browser_authority_manifest_sha256(compiled)
                authority_release = SimpleNamespace(
                    extension_id=extension_id,
                    release_id=_normalize_text(record.release_id) or descriptor_sha256,
                    descriptor=compiled,
                    descriptor_sha256=descriptor_sha256,
                    source_kind="extension_scan",
                    provenance=dict(record.provenance or {}),
                )
    if authority_release is None:
        return record
    descriptor = dict(authority_release.descriptor or {})
    impact = browser_authority_impact(
        account_id=account_id,
        project_id=_normalize_text(scope.project_id),
        extension_id=record.extension_id,
        install_id=_normalize_text(record.install_id),
        old_release_id=_normalize_text(authority_release.release_id),
        new_release_id=_normalize_text(authority_release.release_id),
        descriptor_sha256=authority_release.descriptor_sha256,
        candidate_tree_sha256=candidate_browser_tree_sha256(descriptor),
        manifest=resolve_browser_authority_manifest(authority_release),
    )
    if browser_authority_approval_matches(record.provenance, impact):
        return record
    require_browser_authority_approval(
        impact,
        approved_impact_hash=approved_impact_hash,
    )
    approval = browser_authority_approval_provenance(
        impact,
        actor_id=scope.actor_id or "api",
    )
    updated = store.set_provenance(
        extension_id=record.extension_id,
        account_id=account_id,
        provenance=with_browser_authority_approval(
            record.provenance,
            approval=approval,
        ),
    )
    return updated if updated is not None else record


def scanned_release_browser_authority_handoff(
    *,
    record: Any,
    release: Any,
    scope: ScopeContext,
    approved_impact_hash: str,
) -> PreparedBrowserAuthorityApproval | None:
    """Carry an approval of the installed release's current bundle into its re-import.

    The page preview binds the installed release. Re-importing the same bundle can
    persist a new release, because the descriptor carries the compiled UI. The
    install accepts this handoff only for the same account, project, install, old
    release, version, approved hash, byte-identical candidate tree and browser
    surface (``prepare_install_browser_authority``).
    """
    approved = _normalize_text(approved_impact_hash)
    if not approved:
        return None
    impact = current_browser_authority_impact(record=record, release=release)
    if not impact.approval_required or not hmac.compare_digest(approved, impact.impact_hash):
        return None
    return PreparedBrowserAuthorityApproval(
        impact=impact,
        approval=browser_authority_approval_provenance(
            impact,
            actor_id=scope.actor_id or "api",
        ),
        candidate_version=_normalize_text(getattr(release, "release_version", "")),
        bind_installed_release=True,
    )


__all__ = [
    "authorize_extension_enable_browser_authority",
    "scanned_release_browser_authority_handoff",
]
