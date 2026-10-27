from __future__ import annotations

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
    require_browser_authority_approval,
    resolve_browser_authority_manifest,
)
from .service_support import _normalize_text


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
        impact = browser_authority_impact(
            account_id=scope.account_id or "acc_default",
            project_id=_normalize_text(scope.project_id),
            extension_id=release.extension_id,
            install_id=_normalize_text(getattr(existing, "install_id", "")),
            old_release_id=_normalize_text(getattr(existing, "release_id", "")),
            new_release_id=release.release_id,
            descriptor_sha256=release.descriptor_sha256,
            candidate_tree_sha256=handoff.impact.candidate_tree_sha256,
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


__all__ = ["prepare_install_browser_authority"]
