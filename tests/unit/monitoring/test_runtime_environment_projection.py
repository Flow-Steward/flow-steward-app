import json
from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize(
    "filename,service",
    [
        ("docker-compose.yaml", "control-plane"),
        ("docker-compose.prod.yaml", "control-plane"),
        ("docker-compose.compact.yaml", "compact-runtime"),
    ],
)
def test_runtime_receives_config_owned_monitoring_scope_and_policy(filename, service):
    root = Path(__file__).resolve().parents[3]
    document = yaml.safe_load((root / "infra/docker" / filename).read_text())
    environment = document["services"][service]["environment"]
    for name in (
        "FS_INSTALLATION_ID",
        "FS_COMPOSE_PROJECT_NAME",
        "FS_DEPLOYMENT_PROFILE",
        "FS_MONITORING_EXPECTATIONS",
        "FS_MONITORING_DEPENDENCIES",
        "FS_MONITORING_INTERVAL_SECONDS",
        "FS_MONITORING_STALE_SECONDS",
        "FS_MONITORING_HISTORY_ENABLED",
        "FS_MONITORING_CURRENT_RETENTION_DAYS",
        "FS_MONITORING_HISTORY_RETENTION_DAYS",
        "FS_MONITORING_CLEANUP_BATCH_SIZE",
        "FS_MONITORING_QUEUE_MAX_MESSAGES",
        "FS_MONITORING_QUEUE_MAX_BYTES",
        "FS_MONITORING_OPERATION_TIMEOUT_SECONDS",
    ):
        assert name in environment, name


@pytest.mark.parametrize("role", ["app", "worker", "control", "compact"])
def test_runtime_packages_include_every_canonical_schema_part(role):
    root = Path(__file__).resolve().parents[3]
    manifest = json.loads((root / f"infra/docker/source-slices/{role}.json").read_text())
    actual = {item["path"] for item in manifest["package_data_paths"]}
    required = {
        path.relative_to(root).as_posix()
        for path in (root / "db/schema").glob("001_schema_part_*.sql")
    }
    assert required <= actual
