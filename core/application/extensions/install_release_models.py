from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from core.application.management.command_models import (
    ExtensionReleaseInstallInput,
    ExtensionRuntimeHealthResultView,
)
from core.application.management.scope import ScopeContext

from .browser_authority_approval import BrowserAuthorityImpact
from .oauth_release_upgrade_integration import PreparedOAuthReleaseApplication

DependencyValidator = Callable[..., list[dict[str, Any]]]
HandshakeRunner = Callable[..., ExtensionRuntimeHealthResultView]
ProjectionPublisher = Callable[..., None]
RemoteUrlValidator = Callable[..., str]


class ExtensionInstallLifecyclePort(Protocol):
    def install_record_payload(self, existing: Any | None) -> dict[str, Any] | None: ...

    def rollback_install_release_failure(
        self,
        *,
        install_store: Any,
        runtime_store: Any,
        extension_id: str,
        account_id: str,
        failed_install_id: str,
        previous: dict[str, Any] | None,
        previous_release: Any | None = None,
        previous_entitlement: Any | None = None,
        previous_projections: list[Any] | None = None,
    ) -> None: ...

    def remove_obsolete_install_projections(
        self,
        *,
        runtime_store: Any,
        account_id: str,
        install_id: str,
        release_id: str,
    ) -> None: ...

    def cleanup_release_if_unreferenced(
        self,
        *,
        install_store: Any,
        release_id: str,
        extension_id: str,
    ) -> None: ...


@dataclass(frozen=True)
class PreviousInstallState:
    payload: dict[str, Any] | None
    release: Any | None
    entitlement: Any | None
    projections: list[Any]


@dataclass(frozen=True)
class PreparedInstall:
    command: ExtensionReleaseInstallInput
    scope: ScopeContext
    account_id: str
    actor_id: str
    runtime_store: Any
    install_store: Any
    release: Any
    existing: Any | None
    runtime_mode: str
    remote_base_url: str
    dependency_warnings: list[dict[str, Any]]
    previous: PreviousInstallState
    oauth_transition: PreparedOAuthReleaseApplication | None
    browser_authority_impact: BrowserAuthorityImpact | None
    browser_authority_approval: dict[str, Any]


__all__ = [
    "DependencyValidator",
    "ExtensionInstallLifecyclePort",
    "HandshakeRunner",
    "PreparedInstall",
    "PreviousInstallState",
    "ProjectionPublisher",
    "RemoteUrlValidator",
]
