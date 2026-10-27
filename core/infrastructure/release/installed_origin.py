"""Read the installed release origin without inventing official metadata."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Literal, Protocol

from core.application.installer.contracts import InstallConfig, InstallJournal
from core.application.lifecycle.contracts import DeploymentConfig
from core.application.lifecycle.errors import LifecycleError
from core.application.release.installed_origin import (
    InstalledReleaseOrigin,
    OfficialInstallationOrigin,
    SourceInstallationOrigin,
)


class _OriginState(Protocol):
    def load_installed_release(self): ...

    def load_pending_release(self): ...

    def load_release_handoff(self): ...

    def load_journal(self) -> InstallJournal | None: ...


class _OriginConfiguration(Protocol):
    def read_active(self) -> DeploymentConfig | None: ...

    def read_durable_environment(self, config: DeploymentConfig) -> dict[str, str]: ...


class SourceInstallationProvenance(Protocol):
    def validated_source_revision(self, config: InstallConfig) -> str: ...


class InstalledReleaseOriginReader:
    """Classify one completed installation from durable and runtime evidence."""

    def __init__(
        self,
        *,
        state: _OriginState,
        configuration: _OriginConfiguration,
        source_provenance: SourceInstallationProvenance,
        platform: Callable[[], Literal["linux/amd64", "linux/arm64"]],
    ) -> None:
        self._state = state
        self._configuration = configuration
        self._source_provenance = source_provenance
        self._platform = platform

    def load(self) -> InstalledReleaseOrigin:
        try:
            installed = self._state.load_installed_release()
        except (OSError, ValueError) as exc:
            raise self._invalid("The official installed release record is invalid.") from exc
        if installed is not None:
            return OfficialInstallationOrigin(release=installed)

        if self._state.load_pending_release() is not None:
            raise self._invalid(
                "An official release is pending without a completed installed identity."
            )
        if self._state.load_release_handoff() is not None:
            raise self._invalid(
                "An official release handoff is unfinished without a completed identity."
            )

        journal = self._state.load_journal()
        if journal is None or journal.status != "completed":
            raise LifecycleError.stable_failure(
                code="release_installed_state_missing",
                cause="No completed official or source installation is recorded.",
                remediation="Complete or repair the installation before using release commands.",
                step="release_state",
            )
        active = self._configuration.read_active()
        if active is None:
            raise self._invalid("The completed source installation has no active configuration.")
        if journal.config.environment != "production" or active.environment != "production":
            raise self._invalid(
                "The completed source installation is not a production installation."
            )
        config = InstallConfig.model_validate(active.model_dump(mode="json"))
        environment = self._configuration.read_durable_environment(active)
        if (
            str(environment.get("FS_STACK_ENVIRONMENT") or "").strip() != active.environment
            or str(environment.get("FS_DEPLOYMENT_PROFILE") or "").strip()
            != active.deployment_profile
        ):
            raise self._invalid(
                "The active source configuration and durable runtime context disagree."
            )
        if str(environment.get("FS_INSTALL_MODE") or "").strip() != "source":
            raise self._invalid("The completed installation is not explicitly recorded as source.")
        installation_id = str(environment.get("FS_COMPOSE_PROJECT_NAME") or "").strip()
        if not installation_id or installation_id != journal.project_identity:
            raise self._invalid("The source installation identity does not match its journal.")
        release_repository = str(environment.get("FS_RELEASE_REPOSITORY") or "").strip()
        if not release_repository:
            raise self._invalid("The source installation has no configured release repository.")
        try:
            revision = self._source_provenance.validated_source_revision(config)
        except LifecycleError:
            raise
        except Exception as exc:
            raise self._invalid("The source installation image provenance is invalid.") from exc
        persisted_revision = str(environment.get("FS_BUILD_SHA") or "").strip()
        if persisted_revision != revision:
            raise self._invalid(
                "The source installation revision does not match its validated images."
            )
        try:
            return SourceInstallationOrigin(
                commit_sha=revision,
                deployment_profile=config.deployment_profile,
                platform=self._platform(),
                installation_id=installation_id,
                configuration_sha256=_configuration_sha256(config),
                release_repository=release_repository,
            )
        except ValueError as exc:
            raise self._invalid("The source installation origin is invalid.") from exc

    @staticmethod
    def _invalid(cause: str) -> LifecycleError:
        return LifecycleError.stable_failure(
            code="release_installed_origin_invalid",
            cause=cause,
            remediation="Run flow-steward doctor and repair installed provenance before retrying.",
            step="release_state",
        )


def _configuration_sha256(config: DeploymentConfig) -> str:
    payload = json.dumps(
        config.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


__all__ = ["InstalledReleaseOriginReader", "SourceInstallationProvenance"]
