"""Installed Clone paths must match both runtime profile and data placement."""

from pathlib import Path

import pytest

from core.application.clone.contracts import CloneDataClass
from core.application.lifecycle.contracts import DeploymentConfig
from core.entrypoints.cli.compact_platform_runtime import CompactPlatformRuntime
from core.entrypoints.cli.lifecycle_runtime import LifecycleCLIRuntime


@pytest.mark.parametrize("profile", ("compact", "scalable"))
@pytest.mark.parametrize("placement", ("host_directories", "compact_volume"))
def test_clone_source_roots_follow_runtime_profile_and_retained_placement(
    profile: str,
    placement: str,
) -> None:
    runtime_class = CompactPlatformRuntime if profile == "compact" else LifecycleCLIRuntime
    runtime = object.__new__(runtime_class)
    config = DeploymentConfig(
        environment="production",
        deployment_profile=profile,
        data_placement=placement,
    )
    roots = runtime._clone_data_roots(config, {})

    if placement == "compact_volume":
        assert roots == {
            CloneDataClass.EXTENSIONS: Path("/data/extensions"),
            CloneDataClass.LOCAL_ARTIFACTS: Path("/data/artifacts"),
            CloneDataClass.DURABLE_STATE: Path("/data/state"),
        }
    elif profile == "compact":
        assert roots == {
            CloneDataClass.EXTENSIONS: Path("/data/extensions"),
            CloneDataClass.LOCAL_ARTIFACTS: Path("/data/artifacts"),
            CloneDataClass.DURABLE_STATE: Path("/data/state/.flow-steward"),
        }
    else:
        assert roots[CloneDataClass.EXTENSIONS] == Path("/app/extensions")
        assert roots[CloneDataClass.LOCAL_ARTIFACTS] == Path("/app/logs/artifacts")


def test_compact_host_directory_target_identity_preserves_volume_state_subroot() -> None:
    runtime = object.__new__(CompactPlatformRuntime)
    config = DeploymentConfig(
        environment="production",
        deployment_profile="compact",
        data_placement="host_directories",
    )
    roots = runtime._clone_shell_filesystem_roots(
        config,
        {
            "FS_DOCKER_HOST_PROJECT_ROOT": "/srv/target",
            "FS_EXTENSIONS_DIR": "../../extensions",
            "FS_LOGS_DIR": "/mnt/target-logs",
            "FS_INSTALLER_STATE_DIR": "/mnt/target-installer-state",
        },
    )
    assert roots == {
        "extensions": "/srv/target/extensions",
        "local_artifacts": "/mnt/target-logs/artifacts",
        "durable_state": "volume:fs-compact-data/state/.flow-steward",
    }


def test_compact_host_directory_transfer_stays_in_installer_inbox_and_outbox() -> None:
    runtime = object.__new__(CompactPlatformRuntime)
    config = DeploymentConfig(
        environment="production",
        deployment_profile="compact",
        data_placement="host_directories",
    )
    output = runtime._clone_output_root(config, {})
    incoming = runtime._clone_input_root(config, {})
    assert output.parts[-2:] == ("clone", "outbox")
    assert incoming.parts[-2:] == ("clone", "inbox")
    assert output.parent == incoming.parent
    assert output != Path("/app/backups")
    assert incoming != Path("/data/backups")
