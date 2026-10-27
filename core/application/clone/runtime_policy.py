"""Application-owned Clone policy used by installed-runtime adapters."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from core.application.clone.planning import (
    CloneTargetShell,
    PreparedCloneRestore,
    merge_source_configuration_with_shell,
)
from core.application.lifecycle.contracts import DeploymentConfig


def uses_compact_durable_state_transfer(config: DeploymentConfig) -> bool:
    """Compact host placement keeps only durable shell data in the role-owned volume."""
    return config.deployment_profile == "compact" and config.data_placement == "host_directories"


def build_clone_target_shell(
    *,
    config: DeploymentConfig,
    installation_id: str,
    compose_project: str,
    environment: Mapping[str, str],
    host_uid: int,
    host_gid: int,
    filesystem_roots: Mapping[str, str],
) -> CloneTargetShell:
    """Capture the complete target-owned shell before destructive mutation."""
    postgres_user = str(environment.get("POSTGRES_USER") or "flow_steward")
    postgres_identity = json.dumps(
        {
            "database": str(environment.get("POSTGRES_DB") or "flow_steward"),
            "password": str(environment.get("POSTGRES_PASSWORD") or ""),
            "user": postgres_user,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return CloneTargetShell.from_config(
        installation_id=installation_id,
        compose_project=compose_project,
        config=config,
        host_uid=host_uid,
        host_gid=host_gid,
        filesystem_roots=dict(filesystem_roots),
        postgres_roles=(postgres_user,),
        postgres_identity_fingerprint=hashlib.sha256(postgres_identity).hexdigest(),
    )


def restored_clone_configuration(prepared: PreparedCloneRestore) -> DeploymentConfig:
    """Build final config only from authenticated source evidence and saved target shell."""
    return merge_source_configuration_with_shell(
        source=prepared.source_configuration,
        target_shell=prepared.plan.retained_shell,
    )


__all__ = [
    "build_clone_target_shell",
    "restored_clone_configuration",
    "uses_compact_durable_state_transfer",
]
