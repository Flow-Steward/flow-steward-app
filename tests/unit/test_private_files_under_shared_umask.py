"""Condition 1 of the owner's decision B: umask 0002 widens nothing that must stay private.

The runtime images now run every process under umask 0002 (infra/docker/sitecustomize.py)
so shared trees stay group-writable. A file or directory created without an explicit mode
would follow it -- a secret written that way would become group-writable. Each writer of a
secret or of installation state is run here under that umask, and what it leaves must
carry no group or other permission at all.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.application.lifecycle.contracts import DeploymentConfig
from core.infrastructure.installer.state_store import InstallerStateStore
from core.infrastructure.installer.storage_identity import BundledStorageIdentityService
from core.infrastructure.installer.worker_identity_secret_store import (
    FileWorkerIdentitySecretStore,
)
from core.infrastructure.lifecycle.configuration_store import LifecycleConfigurationStore
from core.infrastructure.lifecycle.state_store import LifecycleStateStore
from core.infrastructure.runtime.compact.runtime_secrets import establish_runtime_secrets


@pytest.fixture(autouse=True)
def shared_umask() -> Iterator[None]:
    previous = os.umask(0o002)
    try:
        yield
    finally:
        os.umask(previous)


def _private(path: Path) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode & 0o077 == 0, f"{path.name} is {oct(mode)}"


def _installer(root: Path) -> list[Path]:
    store = InstallerStateStore(root / ".flow-steward")
    written = [
        store.ensure_runtime_directory(),
        store.write_secret_output("bootstrap-token", "secret"),
        store.write_bootstrap_token_state(
            token="secret", expires_at=datetime.now(UTC) + timedelta(hours=1)
        ),
    ]
    env = root / ".env.prod"
    store.update_env_values(env, {"FS_SECRET_KEY": "secret"})
    store.write_owner_only_text(root / ".flow-steward/installed-releases.json", "{}\n")
    log = store.open_log("umask")
    log.close()
    return [*written, env, root / ".flow-steward/installed-releases.json", log.path]


def _seaweed_identity(root: Path) -> list[Path]:
    path = root / ".flow-steward/runtime/seaweedfs-s3.json"
    path.parent.mkdir(parents=True, mode=0o700)
    BundledStorageIdentityService(path).ensure()
    return [path]


def test_bundled_storage_identity_replace_is_owner_only_and_source_owned(tmp_path: Path) -> None:
    path = tmp_path / ".flow-steward/runtime/seaweedfs-s3.json"
    path.parent.mkdir(parents=True, mode=0o700)
    identity = BundledStorageIdentityService(path)
    identity.ensure(
        existing_access_key="target-access",
        existing_secret_key="target-secret",  # pragma: allowlist secret - synthetic fixture
    )

    identity.replace(
        access_key="source-access",
        secret_key="source-secret",  # pragma: allowlist secret - synthetic fixture
    )

    assert identity.read() == {
        "aws_access_key_id": "source-access",
        "aws_secret_access_key": "source-secret",  # pragma: allowlist secret - expected fixture
        "aws_session_token": "",
    }
    _private(path)


def _worker_token(root: Path) -> list[Path]:
    path = root / "state/worker-mcp-token"
    path.parent.mkdir(parents=True, mode=0o700)
    FileWorkerIdentitySecretStore(path).replace("token")
    return [path]


def _compact_runtime_secrets(root: Path) -> list[Path]:
    path = root / "state/runtime-secrets.json"
    establish_runtime_secrets(state_path=path, environment={})
    return [path]


def _lifecycle(root: Path) -> list[Path]:
    state = LifecycleStateStore(root)
    previous = state.save_previous_config("op-umask", {"configuration": {}})
    log = state.open_log("op-umask")
    log.close()
    with state.operation_lock("op-umask", mutating=True):
        pass
    config_store = LifecycleConfigurationStore(root)
    config_store.write_active(DeploymentConfig())
    config_store.write_compose_environment({"FS_COMPOSE_PROJECT_NAME": "fs-umask"})
    env = root / ".env"
    config_store.write_runtime_environment(env, {"FS_SECRET_KEY": "secret"})
    return [
        previous,
        log.path,
        state.lock_path,
        config_store.active_path,
        config_store.compose_env_path,
        env,
    ]


@pytest.mark.parametrize(
    "writer",
    [_installer, _seaweed_identity, _worker_token, _compact_runtime_secrets, _lifecycle],
    ids=lambda writer: writer.__name__.strip("_"),
)
def test_secrets_and_state_stay_private_under_the_shared_umask(
    tmp_path: Path, writer: Callable[[Path], list[Path]]
) -> None:
    written = writer(tmp_path)

    assert written  # non-vacuity
    for path in written:
        _private(path)
