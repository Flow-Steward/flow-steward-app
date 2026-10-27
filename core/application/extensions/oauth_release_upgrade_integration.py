"""Extension install workflow integration for OAuth release impact."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.application.management.command_models import ExtensionReleaseInstallInput

from .command_context import ExtensionCommandContext
from .oauth_release_upgrade import (
    OAuthReleaseUpgradeService,
    PreparedOAuthReleaseTransition,
    resolve_oauth_release_contract,
)
from .oauth_release_upgrade_repository import PostgresOAuthReleaseUpgradeRepository


def _text(value: Any) -> str:
    return str(value or "").strip()


def _has_oauth_contract(descriptor: dict[str, Any]) -> bool:
    contract = resolve_oauth_release_contract(descriptor)
    return bool(contract.get("oauth_providers"))


@dataclass(frozen=True)
class PreparedOAuthReleaseApplication:
    service: OAuthReleaseUpgradeService
    transition: PreparedOAuthReleaseTransition

    def apply(self) -> None:
        self.service.apply(self.transition)


def prepare_oauth_release_transition(
    *,
    context: ExtensionCommandContext,
    command: ExtensionReleaseInstallInput,
    existing: Any | None,
    previous_release: Any | None,
    candidate_release: Any,
) -> PreparedOAuthReleaseApplication | None:
    if (
        existing is None
        or previous_release is None
        or _text(getattr(existing, "release_id", ""))
        == _text(getattr(candidate_release, "release_id", ""))
    ):
        return None
    old_descriptor = dict(getattr(previous_release, "descriptor", {}) or {})
    new_descriptor = dict(getattr(candidate_release, "descriptor", {}) or {})
    if not (_has_oauth_contract(old_descriptor) or _has_oauth_contract(new_descriptor)):
        return None
    connection = context.persistence_connection()
    if connection is None:
        return None
    service = OAuthReleaseUpgradeService(PostgresOAuthReleaseUpgradeRepository(connection))
    transition = service.prepare(
        account_id=_text(getattr(existing, "account_id", "")),
        install_id=_text(getattr(existing, "install_id", "")),
        extension_id=_text(getattr(existing, "extension_id", "")),
        old_release_id=_text(getattr(previous_release, "release_id", "")),
        new_release_id=_text(getattr(candidate_release, "release_id", "")),
        old_descriptor=old_descriptor,
        new_descriptor=new_descriptor,
        approved_impact_hash=command.approved_oauth_impact_hash,
    )
    return PreparedOAuthReleaseApplication(service=service, transition=transition)
