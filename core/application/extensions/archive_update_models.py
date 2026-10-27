"""Typed request, prepared state, and rollback ownership for archive updates."""

from __future__ import annotations

import contextlib
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.errors import ServiceValidationError
from core.application.management.scope import ScopeContext, normalize_scope

from .binding_cleanup_service import ExtensionCleanupService
from .binding_context import (
    ExtensionInstallStoreType,
    ExtensionLifecycleContext,
    ExtensionRuntimeStoreType,
)
from .browser_authority_approval import PreparedBrowserAuthorityApproval
from .extension_state_service import ExtensionStateService
from .lifecycle_ports import ExtensionCommandServicePort
from .managed_release_service import ManagedReleaseService
from .semver import compare_semver, normalize_semver
from .update_failure_reporting import _CompensationFailureCapture


def account_id(scope: ScopeContext | None) -> str:
    return normalize_scope(scope).account_id or DEFAULT_ACCOUNT_ID


def validate_update_version_transition(
    *, extension_id: str, current_version: str, candidate_version: str
) -> str:
    normalized_current = normalize_semver(current_version, "current_version")
    normalized_candidate = normalize_semver(candidate_version, "candidate_version")
    comparison = compare_semver(
        normalized_candidate,
        normalized_current,
        field_name=f"extension '{extension_id}' version",
    )
    if comparison == 0:
        raise ServiceValidationError(
            f"Extension '{extension_id}' version '{normalized_candidate}' is already installed"
        )
    if comparison < 0:
        raise ServiceValidationError(
            f"Downgrade not supported for extension '{extension_id}': "
            f"{normalized_current} -> {normalized_candidate}"
        )
    return normalized_candidate


@dataclass(frozen=True)
class UpdateRequest:
    bl: Any
    install_store: ExtensionInstallStoreType
    runtime_store: ExtensionRuntimeStoreType
    bundle_dir: Path
    compiled: dict[str, Any]
    extensions_root: Path
    scope: ScopeContext | None
    current_install: Any
    descriptor_name: str = ""
    browser_authority_handoff: PreparedBrowserAuthorityApproval | None = None
    approved_browser_authority_impact_hash: str = ""


@dataclass
class PreparedUpdate:
    request: UpdateRequest
    extension_id: str
    candidate_version: str
    target_dir: Path
    previous_active_release_dir: Path | None
    staged_dir: Path
    previous_callback_ids: set[str]
    previous_version: str
    previous_enabled: bool
    previous_config: dict[str, Any]
    source_data_schema_version: int
    target_data_schema_version: int
    candidate_manifest: dict[str, Any]
    candidate_lifecycle: dict[str, Any]
    previous_release_id: str
    lifecycle_command: ExtensionCommandServicePort
    previous_install_payload: dict[str, Any] | None
    previous_release: Any | None
    previous_entitlement: Any | None
    previous_projections: list[Any]
    candidate_release_id: str = ""
    candidate_release_dir: Path | None = None
    migration_snapshot: dict[str, Any] | None = None
    migrated_rows: list[dict[str, Any]] = field(default_factory=list)
    migrated_install_config: dict[str, Any] = field(default_factory=dict)
    migration_staged_dir: Path | None = None
    migration_container_dir: Path | None = None


@dataclass
class UpdateRollbackState:
    prepared: PreparedUpdate
    context: ExtensionLifecycleContext
    managed_release: ManagedReleaseService
    cleanup: ExtensionCleanupService
    state: ExtensionStateService
    created_release_this_attempt: bool = False
    release_arbitration_rejected: bool = False
    published_target: bool = False
    lifecycle_commit_attempted: bool = False

    def rollback_update(self) -> list[BaseException]:
        if self.lifecycle_commit_attempted:
            return []
        failures: list[BaseException] = []
        self._rollback_published_files(failures)
        if not self.release_arbitration_rejected:
            self._rollback_persisted_state(failures)
        return failures

    @staticmethod
    def _attempt_compensation(failures: list[BaseException], label: str, callback: Any) -> None:
        with _CompensationFailureCapture(
            failures, f"Managed extension update compensation step failed: {label}."
        ):
            callback()

    def _rollback_published_files(self, failures: list[BaseException]) -> None:
        prepared = self.prepared
        if self.published_target:
            self._attempt_compensation(
                failures,
                "remove candidate activation",
                lambda: self.managed_release.remove_managed_simple_extension_active_root(
                    prepared.target_dir
                ),
            )
        previous_root = prepared.previous_active_release_dir
        if (
            not self.release_arbitration_rejected
            and previous_root is not None
            and previous_root.exists()
        ):
            self._attempt_compensation(
                failures,
                "reactivate previous bundle",
                lambda: self.managed_release.activate_managed_simple_extension_release(
                    active_root=prepared.target_dir,
                    release_root=previous_root,
                ),
            )
        if self.created_release_this_attempt and prepared.candidate_release_dir is not None:
            self._attempt_compensation(
                failures,
                "remove candidate release bundle",
                lambda: shutil.rmtree(prepared.candidate_release_dir),
            )

    def _rollback_persisted_state(self, failures: list[BaseException]) -> None:
        prepared = self.prepared
        request = prepared.request
        self._attempt_compensation(
            failures,
            "rescan previous bundle",
            lambda: self.state.scan_extensions(
                root=str(request.extensions_root),
                scope=request.scope,
                _bl=request.bl,
            ),
        )
        self._attempt_compensation(
            failures,
            "restore previous enabled state",
            lambda: self.state.set_enabled_state_locked(
                prepared.extension_id,
                bl=request.bl,
                version=prepared.previous_version or None,
                enabled=prepared.previous_enabled,
                config=prepared.previous_config,
                scope=request.scope,
                _skip_browser_authority_gate=True,
                _install_store=request.install_store,
                _runtime_store=request.runtime_store,
            ),
        )
        self._attempt_compensation(
            failures,
            "clean failed scan state",
            lambda: self.cleanup.cleanup_failed_simple_update_state(
                bl=request.bl,
                extension_id=prepared.extension_id,
                failed_version=prepared.candidate_version,
            ),
        )
        self._attempt_compensation(
            failures,
            "restore previous install release",
            lambda: prepared.lifecycle_command.rollback_install_release_failure(
                install_store=request.install_store,
                runtime_store=request.runtime_store,
                extension_id=prepared.extension_id,
                account_id=account_id(request.scope),
                failed_install_id=str(getattr(request.current_install, "install_id", "") or ""),
                previous=prepared.previous_install_payload,
                previous_release=prepared.previous_release,
                previous_entitlement=prepared.previous_entitlement,
                previous_projections=prepared.previous_projections,
            ),
        )
        if (
            prepared.candidate_release_id
            and prepared.candidate_release_id != prepared.previous_release_id
        ):
            self._attempt_compensation(
                failures,
                "remove candidate descriptor release",
                lambda: prepared.lifecycle_command.cleanup_release_if_unreferenced(
                    install_store=request.install_store,
                    release_id=prepared.candidate_release_id,
                    extension_id=prepared.extension_id,
                ),
            )

    def cleanup_prepublication_paths(self) -> None:
        for path in (
            self.prepared.migration_container_dir,
            self.prepared.migration_staged_dir,
            self.prepared.staged_dir,
        ):
            if path is None:
                continue
            with contextlib.suppress(OSError):
                self.managed_release.remove_managed_extension_staging_path(path)


__all__ = [
    "PreparedUpdate",
    "UpdateRequest",
    "UpdateRollbackState",
    "account_id",
    "validate_update_version_transition",
]
