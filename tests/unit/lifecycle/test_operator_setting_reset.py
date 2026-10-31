"""A reset host setting gets its default back even when written outside the block."""

from __future__ import annotations

from pathlib import Path

from core.application.lifecycle.configuration_service import ConfigurationService
from core.application.lifecycle.contracts import DeploymentConfig
from core.infrastructure.lifecycle.configuration_store import (
    LifecycleConfigurationStore,
    _replace_managed_block,
)
from core.infrastructure.lifecycle.state_store import LifecycleStateStore

PRIVATE = "FS_ALLOW_PRIVATE_REMOTE_URLS"
_INSTALLED = "\n".join(
    (
        "# BEGIN FLOW STEWARD INSTALLER MANAGED",
        'FS_SECRET_KEY="keep fs secret"',
        'ENCRYPTION_KEY="keep encryption key"',
        'FS_BACKUP_ENCRYPTION_KEY="keep backup key"',
        'POSTGRES_PASSWORD="keep postgres password"',
        'RABBITMQ_PASS="keep rabbit password"',
        'FS_METRICS_TOKEN="keep metrics token"',
        'SECURITY_PII_SALT="keep pii salt"',
        "# END FLOW STEWARD INSTALLER MANAGED",
        # Where `install.sh --setting` appends a key the file did not have yet.
        f"{PRIVATE}=1",
        "",
    )
)


def _service(tmp_path: Path) -> ConfigurationService:
    store = LifecycleConfigurationStore(tmp_path)
    store.compose_env_path.parent.mkdir(parents=True, exist_ok=True)
    store.compose_env_path.write_text("FS_COMPOSE_PROJECT_NAME=flow-steward\n", encoding="utf-8")
    (tmp_path / ".env.prod").write_text(_INSTALLED, encoding="utf-8")
    return ConfigurationService(store=store, lifecycle_state=LifecycleStateStore(tmp_path))


def test_reset_setting_renders_its_default_and_drops_the_stale_assignment(tmp_path: Path) -> None:
    service = _service(tmp_path)
    reset = DeploymentConfig(
        environment="production", deployment_profile="scalable", file_storage="local_fs"
    )

    _target, _path, environment = service.prepare_environment(reset)
    managed = "\n".join(f"{name}={value}" for name, value in environment.items())
    rewritten = _replace_managed_block(_INSTALLED, managed, preserve_keys=frozenset())

    assert environment[PRIVATE] == "0"
    assert f"{PRIVATE}=1" not in rewritten
    assert f"{PRIVATE}=0" in rewritten


def test_a_set_setting_keeps_its_value(tmp_path: Path) -> None:
    service = _service(tmp_path)
    enabled = DeploymentConfig(
        environment="production",
        deployment_profile="scalable",
        file_storage="local_fs",
        operator_settings={PRIVATE: "1"},
    )

    _target, _path, environment = service.prepare_environment(enabled)

    assert environment[PRIVATE] == "1"
