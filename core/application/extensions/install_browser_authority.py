from __future__ import annotations

import hmac
from pathlib import Path
from typing import Any

from core.application.management.command_models import ExtensionReleaseInstallInput
from core.application.management.errors import ServiceValidationError
from core.application.management.scope import ScopeContext

from .browser_authority_approval import (
    BrowserAuthorityImpact,
    PreparedBrowserAuthorityApproval,
    browser_authority_approval_provenance,
    browser_authority_impact,
    candidate_browser_tree_sha256,
    current_browser_authority_impact,
    require_browser_authority_approval,
    resolve_browser_authority_manifest,
    with_browser_authority_approval,
)
from .service_support import _normalize_text


def _installed_release_tree_sha256(
    *,
    handoff: PreparedBrowserAuthorityApproval,
    scope: ScopeContext,
    release: Any,
    existing: Any | None,
) -> str:
    """Fail closed unless a scanned re-import publishes exactly the approved binding."""
    approved = handoff.impact
    tree_sha256 = candidate_browser_tree_sha256(dict(release.descriptor or {}))
    if (
        approved.project_id != _normalize_text(scope.project_id)
        or not approved.install_id
        or approved.install_id != _normalize_text(getattr(existing, "install_id", ""))
        or approved.old_release_id != _normalize_text(getattr(existing, "release_id", ""))
    ):
        raise ServiceValidationError(
            "Prevalidated browser authority does not match the installed release"
        )
    if not hmac.compare_digest(tree_sha256, approved.candidate_tree_sha256):
        raise ServiceValidationError(
            "Prevalidated browser authority does not match the candidate tree"
        )
    return tree_sha256


def prepare_install_browser_authority(
    *,
    command: ExtensionReleaseInstallInput,
    scope: ScopeContext,
    release: Any,
    existing: Any | None,
    handoff: PreparedBrowserAuthorityApproval | None,
) -> tuple[BrowserAuthorityImpact | None, dict[str, Any]]:
    """Validate browser authority and return the impact plus audit provenance."""
    if handoff is not None:
        if (
            handoff.impact.account_id != (scope.account_id or "acc_default")
            or handoff.impact.extension_id != release.extension_id
            or _normalize_text(handoff.candidate_version) != release.release_version
            or handoff.impact.impact_hash != command.approved_browser_authority_impact_hash
        ):
            raise ServiceValidationError(
                "Prevalidated browser authority does not match this install"
            )
        candidate_tree_sha256 = handoff.impact.candidate_tree_sha256
        if handoff.bind_installed_release:
            candidate_tree_sha256 = _installed_release_tree_sha256(
                handoff=handoff,
                scope=scope,
                release=release,
                existing=existing,
            )
        impact = browser_authority_impact(
            account_id=scope.account_id or "acc_default",
            project_id=_normalize_text(scope.project_id),
            extension_id=release.extension_id,
            install_id=_normalize_text(getattr(existing, "install_id", "")),
            old_release_id=_normalize_text(getattr(existing, "release_id", "")),
            new_release_id=release.release_id,
            descriptor_sha256=release.descriptor_sha256,
            candidate_tree_sha256=candidate_tree_sha256,
            manifest=resolve_browser_authority_manifest(release),
        )
        if impact.surface_fingerprint != handoff.impact.surface_fingerprint:
            raise ServiceValidationError(
                "Prevalidated browser authority surface does not match this release"
            )
        approval = browser_authority_approval_provenance(
            impact,
            actor_id=scope.actor_id or "api",
            approved_impact_hash=handoff.impact.impact_hash,
        )
        return impact, approval
    descriptor = dict(release.descriptor or {})
    impact = browser_authority_impact(
        account_id=scope.account_id or "acc_default",
        project_id=_normalize_text(scope.project_id),
        extension_id=release.extension_id,
        install_id=_normalize_text(getattr(existing, "install_id", "")),
        old_release_id=_normalize_text(getattr(existing, "release_id", "")),
        new_release_id=release.release_id,
        descriptor_sha256=release.descriptor_sha256,
        candidate_tree_sha256=candidate_browser_tree_sha256(descriptor),
        manifest=resolve_browser_authority_manifest(release),
    )
    require_browser_authority_approval(
        impact,
        approved_impact_hash=command.approved_browser_authority_impact_hash,
    )
    return impact, browser_authority_approval_provenance(
        impact,
        actor_id=scope.actor_id or "api",
    )


def persist_scanned_archive_browser_authority(
    *,
    handoff: PreparedBrowserAuthorityApproval | None,
    install_store: Any,
    runtime_store: Any,
    release_root: Path,
) -> None:
    """Bind the approved archive's browser authority to its published scan release."""
    if handoff is None:
        return
    approved = handoff.impact
    current = install_store.get(extension_id=approved.extension_id, account_id=approved.account_id)
    release = (
        runtime_store.get_release(current.release_id) if current and current.release_id else None
    )
    if release is None or release.release_version != handoff.candidate_version:
        raise ServiceValidationError("Approved browser authority release is missing")

    impact = current_browser_authority_impact(record=current, release=release)
    if (
        impact is None
        or not impact.approval_required
        or impact.bundle_root != str(release_root.resolve())
        or impact.surface_fingerprint != approved.surface_fingerprint
    ):
        raise ServiceValidationError(
            "Approved browser authority does not match the published bundle"
        )

    approval = browser_authority_approval_provenance(
        impact,
        actor_id=str(handoff.approval["actor_id"]),
        approved_at=float(handoff.approval["approved_at"]),
        approved_impact_hash=approved.impact_hash,
    )
    updated = install_store.set_provenance(
        extension_id=approved.extension_id,
        account_id=approved.account_id,
        provenance=with_browser_authority_approval(current.provenance, approval=approval),
    )
    if updated is None:
        raise ServiceValidationError("Approved browser authority could not be saved")


__all__ = ["persist_scanned_archive_browser_authority", "prepare_install_browser_authority"]
