from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.disposable_installer import DisposableInstallerProject

ROOT = Path(__file__).resolve().parents[2]

CORE_SERVICES = {
    "traefik",
    "pg-fs",
    "rabbitmq-fs",
    "fs-mcp",
    "fs-app",
    "fs-worker",
    "control-plane",
    "fs-cli",
}
DEVELOPMENT_RUNNING_SERVICES = {
    "fs-frontend",
    "fs-test-runner",
    "fs-audit",
    "pg-audit",
    "rabbitmq-audit",
}
DEVELOPMENT_ON_DEMAND_SERVICES = {"mailpit", "sonarqube", "sonar-scanner"}
CORE_VOLUMES = {"fs-postgres", "fs-rabbitmq"}
DEVELOPMENT_VOLUMES = {
    "fs-frontend-node-modules",
}

pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.fixture
def disposable_project(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> DisposableInstallerProject:
    parameters = getattr(getattr(request.node, "callspec", None), "params", {})
    external_storage = parameters.get("storage") == "external_s3"
    project = DisposableInstallerProject.create(
        work_root=tmp_path,
        source_root=ROOT,
        external_storage=external_storage,
        external_tls=external_storage,
    )
    try:
        yield project
    finally:
        if not project.cleaned:
            project.cleanup()


def _expected_inventory(environment: str, storage: str) -> dict[str, set[str]]:
    running = set(CORE_SERVICES)
    volumes = set(CORE_VOLUMES)
    if environment == "development":
        running.update(DEVELOPMENT_RUNNING_SERVICES)
        volumes.update(DEVELOPMENT_VOLUMES)
    if storage == "bundled":
        running.add("seaweedfs")
        volumes.add("seaweedfs-data")
    return {"running_services": running, "named_volumes": volumes}


def _assert_inventory(
    inventory: dict[str, object],
    *,
    environment: str,
    storage: str,
    retained_volumes: set[str] | None = None,
) -> None:
    expected = _expected_inventory(environment, storage)
    assert inventory["selected_environment"] == environment
    assert inventory["selected_storage"] == storage
    assert set(inventory["running_services"]) == expected["running_services"]
    assert set(inventory["named_volumes"]) == expected["named_volumes"].union(
        retained_volumes or set()
    )
    assert "fs-cli" in inventory["running_services"]
    if environment == "production":
        assert DEVELOPMENT_RUNNING_SERVICES.isdisjoint(inventory["materialized_services"])
        assert DEVELOPMENT_ON_DEMAND_SERVICES.isdisjoint(inventory["materialized_services"])


@pytest.mark.parametrize("environment", ("production", "development"))
@pytest.mark.parametrize("storage", ("external_s3", "bundled"))
def test_profile_inventory_and_both_direction_round_trip_preserve_data(
    environment: str,
    storage: str,
    disposable_project: DisposableInstallerProject,
) -> None:
    installed = disposable_project.install(
        environment=environment,
        renderer="non_interactive",
        storage=storage,
    )
    assert installed.returncode == 0, installed.combined_output

    initial = disposable_project.capture_deployment_profile(stage="initial")
    _assert_inventory(initial, environment=environment, storage=storage)
    sentinels = disposable_project.seed_transition_sentinels()

    target = "development" if environment == "production" else "production"
    switched = disposable_project.switch_environment(target)
    assert switched.returncode == 0, switched.combined_output
    retained = disposable_project.capture_deployment_profile(stage="switched-retained-cli")
    assert retained["cli_runtime_container_id"] == initial["cli_runtime_container_id"]
    reconciled_cli_id = disposable_project.reconcile_and_prove_cli_runtime_reuse()
    assert reconciled_cli_id != retained["cli_runtime_container_id"]
    switched_inventory = disposable_project.capture_deployment_profile(stage="switched")
    assert switched_inventory["cli_runtime_container_id"] == reconciled_cli_id
    initial_container_ids = initial["container_ids"]
    switched_container_ids = switched_inventory["container_ids"]
    assert isinstance(initial_container_ids, dict)
    assert isinstance(switched_container_ids, dict)
    assert switched_container_ids["pg-fs"] == initial_container_ids["pg-fs"]
    assert switched_container_ids["rabbitmq-fs"] == initial_container_ids["rabbitmq-fs"]
    _assert_inventory(
        switched_inventory,
        environment=target,
        storage=storage,
        retained_volumes=set(initial["named_volumes"]),
    )
    disposable_project.assert_transition_sentinels(sentinels)

    restored = disposable_project.switch_environment(environment)
    assert restored.returncode == 0, restored.combined_output
    restored_retained = disposable_project.capture_deployment_profile(stage="restored-retained-cli")
    assert restored_retained["cli_runtime_container_id"] == reconciled_cli_id
    restored_cli_id = disposable_project.reconcile_and_prove_cli_runtime_reuse()
    assert restored_cli_id != restored_retained["cli_runtime_container_id"]
    restored_inventory = disposable_project.capture_deployment_profile(stage="restored")
    assert restored_inventory["cli_runtime_container_id"] == restored_cli_id
    restored_container_ids = restored_inventory["container_ids"]
    assert isinstance(restored_container_ids, dict)
    assert restored_container_ids["pg-fs"] == initial_container_ids["pg-fs"]
    assert restored_container_ids["rabbitmq-fs"] == initial_container_ids["rabbitmq-fs"]
    _assert_inventory(
        restored_inventory,
        environment=environment,
        storage=storage,
        retained_volumes=set(switched_inventory["named_volumes"]),
    )
    disposable_project.assert_transition_sentinels(sentinels)
    assert {
        key: restored_inventory[key] for key in ("running_services", "materialized_services")
    } == {key: initial[key] for key in ("running_services", "materialized_services")}
    assert set(restored_inventory["named_volumes"]) == set(
        switched_inventory["named_volumes"]
    ).union(initial["named_volumes"])

    disposable_project.cleanup()
    assert disposable_project.cleanup_proof() == {
        "containers": [],
        "networks": [],
        "volumes": [],
        "images": [],
        "paths": [],
    }


def test_development_bundled_to_production_external_s3_and_back(
    disposable_project: DisposableInstallerProject,
) -> None:
    installed = disposable_project.install(
        environment="development",
        renderer="non_interactive",
        storage="bundled",
    )
    assert installed.returncode == 0, installed.combined_output

    initial = disposable_project.capture_deployment_profile(stage="mixed-initial")
    _assert_inventory(initial, environment="development", storage="bundled")
    sentinels = disposable_project.seed_transition_sentinels()

    switched = disposable_project.switch_environment(
        "production",
        storage="external_s3",
    )
    assert switched.returncode == 0, switched.combined_output
    retained = disposable_project.capture_deployment_profile(stage="mixed-retained-cli")
    # Installing the custom external-S3 CA changes the active env descriptor before
    # this launcher starts, so that launcher reconciles once. The transition it then
    # executes must retain that container; the next launcher reconciles the published
    # Production mount contract below.
    assert retained["cli_runtime_container_id"] != initial["cli_runtime_container_id"]
    external_cli_id = disposable_project.reconcile_and_prove_cli_runtime_reuse()
    assert external_cli_id != retained["cli_runtime_container_id"]
    external_inventory = disposable_project.capture_deployment_profile(stage="mixed-external")
    assert external_inventory["cli_runtime_container_id"] == external_cli_id
    initial_container_ids = initial["container_ids"]
    external_container_ids = external_inventory["container_ids"]
    assert isinstance(initial_container_ids, dict)
    assert isinstance(external_container_ids, dict)
    assert external_container_ids["pg-fs"] == initial_container_ids["pg-fs"]
    assert external_container_ids["rabbitmq-fs"] == initial_container_ids["rabbitmq-fs"]
    _assert_inventory(
        external_inventory,
        environment="production",
        storage="external_s3",
        retained_volumes=set(initial["named_volumes"]),
    )
    database_sentinels = sentinels["database"]
    assert isinstance(database_sentinels, dict)
    disposable_project.assert_database_transition_sentinels(database_sentinels)
    disposable_project.assert_storage_transition_sentinel_unavailable(sentinels)
    assert disposable_project.storage_round_trip() == b"installer-proof"

    restored = disposable_project.switch_environment(
        "development",
        storage="bundled",
    )
    assert restored.returncode == 0, restored.combined_output
    restored_retained = disposable_project.capture_deployment_profile(
        stage="mixed-restored-retained-cli"
    )
    assert restored_retained["cli_runtime_container_id"] == external_cli_id
    restored_cli_id = disposable_project.reconcile_and_prove_cli_runtime_reuse()
    assert restored_cli_id != restored_retained["cli_runtime_container_id"]
    restored_inventory = disposable_project.capture_deployment_profile(stage="mixed-restored")
    assert restored_inventory["cli_runtime_container_id"] == restored_cli_id
    restored_container_ids = restored_inventory["container_ids"]
    assert isinstance(restored_container_ids, dict)
    assert restored_container_ids["pg-fs"] == initial_container_ids["pg-fs"]
    assert restored_container_ids["rabbitmq-fs"] == initial_container_ids["rabbitmq-fs"]
    _assert_inventory(
        restored_inventory,
        environment="development",
        storage="bundled",
        retained_volumes=set(external_inventory["named_volumes"]),
    )
    disposable_project.assert_transition_sentinels(sentinels)
    assert {
        key: restored_inventory[key] for key in ("running_services", "materialized_services")
    } == {key: initial[key] for key in ("running_services", "materialized_services")}
    assert set(restored_inventory["named_volumes"]) == set(
        external_inventory["named_volumes"]
    ).union(initial["named_volumes"])

    disposable_project.cleanup()
    assert disposable_project.cleanup_proof() == {
        "containers": [],
        "networks": [],
        "volumes": [],
        "images": [],
        "paths": [],
    }
