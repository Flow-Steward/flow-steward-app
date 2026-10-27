"""Immutable, target-bound planning contracts for destructive Clone restore."""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.application.clone.contracts import CloneManifest
from core.application.lifecycle.contracts import DeploymentConfig
from core.application.release.contracts import ReleaseIdentity

_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CloneTargetShell(_ClosedModel):
    """Explicit target-owned deployment shell retained across replacement."""

    installation_id: str = Field(min_length=1, max_length=255)
    compose_project: str = Field(min_length=1, max_length=255)
    configuration: DeploymentConfig
    host_uid: int = Field(ge=0)
    host_gid: int = Field(ge=0)
    filesystem_roots: dict[str, str]
    postgres_roles: tuple[str, ...]
    postgres_identity_fingerprint: str = Field(pattern=_SHA256_PATTERN)

    @classmethod
    def from_config(
        cls,
        *,
        installation_id: str,
        compose_project: str,
        config: DeploymentConfig,
        host_uid: int,
        host_gid: int,
        filesystem_roots: dict[str, str],
        postgres_roles: tuple[str, ...],
        postgres_identity_fingerprint: str,
    ) -> Self:
        return cls(
            installation_id=installation_id,
            compose_project=compose_project,
            configuration=config,
            host_uid=host_uid,
            host_gid=host_gid,
            filesystem_roots=filesystem_roots,
            postgres_roles=postgres_roles,
            postgres_identity_fingerprint=postgres_identity_fingerprint,
        )


class CloneReplacementPlan(_ClosedModel):
    """Canonical replacement set bound to one current target shell."""

    schema_version: Literal[1] = 1
    source_installation_id: str
    source_release_version: str
    source_release: ReleaseIdentity
    archive_sha256: str = Field(pattern=_SHA256_PATTERN)
    target_installation_id: str
    target_profile: Literal["compact", "scalable"]
    target_platform: Literal["linux/amd64", "linux/arm64"]
    retained_shell: CloneTargetShell
    replaced_data_classes: tuple[str, ...]
    external_effect_categories: tuple[str, ...]
    plan_digest: str = Field(pattern=_SHA256_PATTERN)

    @classmethod
    def build(
        cls,
        *,
        source_installation_id: str,
        source_release_version: str,
        source_release: ReleaseIdentity,
        archive_sha256: str,
        target_shell: CloneTargetShell,
        target_platform: Literal["linux/amd64", "linux/arm64"],
        replaced_data_classes: tuple[str, ...],
        external_effect_categories: tuple[str, ...],
    ) -> Self:
        canonical_classes = tuple(sorted(set(replaced_data_classes)))
        canonical_effects = tuple(sorted(set(external_effect_categories)))
        values = {
            "schema_version": 1,
            "source_installation_id": source_installation_id,
            "source_release_version": source_release_version,
            "source_release": source_release.model_dump(mode="json"),
            "archive_sha256": archive_sha256,
            "target_installation_id": target_shell.installation_id,
            "target_profile": target_shell.configuration.deployment_profile,
            "target_platform": target_platform,
            "retained_shell": target_shell.model_dump(mode="json"),
            "replaced_data_classes": canonical_classes,
            "external_effect_categories": canonical_effects,
        }
        return cls(
            source_installation_id=source_installation_id,
            source_release_version=source_release_version,
            source_release=source_release,
            archive_sha256=archive_sha256,
            target_installation_id=target_shell.installation_id,
            target_profile=target_shell.configuration.deployment_profile,
            target_platform=target_platform,
            retained_shell=target_shell,
            replaced_data_classes=canonical_classes,
            external_effect_categories=canonical_effects,
            plan_digest=_plan_digest(values),
        )

    @model_validator(mode="after")
    def validate_digest(self) -> Self:
        values = self.model_dump(mode="json", exclude={"plan_digest"})
        if self.plan_digest != _plan_digest(values):
            raise ValueError("Clone replacement plan digest does not match its content")
        if self.target_installation_id != self.retained_shell.installation_id:
            raise ValueError("Clone replacement plan target identity changed")
        if self.target_profile != self.retained_shell.configuration.deployment_profile:
            raise ValueError("Clone replacement plan target profile changed")
        return self


class PreparedCloneRestore(_ClosedModel):
    """Authenticated private staging plus immutable review and execution plan."""

    archive_path: str
    operation_id: str
    staged_root: str
    manifest: CloneManifest
    source_configuration: DeploymentConfig
    exact_release: ReleaseIdentity
    plan: CloneReplacementPlan


_TARGET_CONFIGURATION_FIELDS = (
    "environment",
    "deployment_profile",
    "data_placement",
    "runtime_http_port",
    "redirect_http_port",
    "public_host",
    "public_port",
    "ingress_mode",
    "tls_mode",
    "reverse_proxy_backend_host",
    "reverse_proxy_backend_port",
    "reverse_proxy_mcp_port",
    "proxy_placement",
    "proxy_kind",
    "proxy_trusted_ips",
    "traefik_network",
    "traefik_https_entrypoint",
    "traefik_http_entrypoint",
    "traefik_cert_resolver",
    "proxy_network",
    "acme_contact",
    "acme_directory_url",
    "extension_installer_origin",
    "certificate_path",
    "private_key_path",
)


def merge_source_configuration(
    *,
    source: DeploymentConfig,
    target: DeploymentConfig,
) -> DeploymentConfig:
    """Import source-owned configuration while retaining the target deployment shell."""
    if source.deployment_profile != target.deployment_profile:
        raise ValueError("Clone source and target deployment profiles must match")
    retained = {name: getattr(target, name) for name in _TARGET_CONFIGURATION_FIELDS}
    return source.model_copy(update=retained)


def merge_source_configuration_with_shell(
    *,
    source: DeploymentConfig,
    target_shell: CloneTargetShell,
) -> DeploymentConfig:
    """Merge authenticated source config with the pre-mutation target shell."""
    return merge_source_configuration(source=source, target=target_shell.configuration)


def _plan_digest(values: object) -> str:
    payload = json.dumps(
        values,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "CloneReplacementPlan",
    "CloneTargetShell",
    "PreparedCloneRestore",
    "merge_source_configuration",
    "merge_source_configuration_with_shell",
]
