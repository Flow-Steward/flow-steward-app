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


def _postgres_identity_fingerprint(identity: bytes, *, installation_id: str) -> str:
    """Fingerprint the database identity without a fast hash of its password.

    The identity carries POSTGRES_PASSWORD, which an operator may have chosen, and
    the fingerprint is printed in the clone plan: a plain SHA-256 of it could be
    guessed offline. The same password-hashing KDF as user passwords keeps it
    slow, and the installation salt keeps it deterministic for one target.
    """
    from core.application.auth.config import AUTH_PASSWORD_ITERATIONS

    salt = b"flow-steward-clone-postgres-identity\0" + installation_id.encode("utf-8")
    return hashlib.pbkdf2_hmac("sha256", identity, salt, AUTH_PASSWORD_ITERATIONS).hex()


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
        postgres_identity_fingerprint=_postgres_identity_fingerprint(
            postgres_identity, installation_id=installation_id
        ),
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
