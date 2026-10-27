from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.application.lifecycle.contracts import LifecycleOperationKind
from core.application.lifecycle.preservation_service import (
    UNKNOWN,
    DeclaredMigrationChange,
    PreservationManifest,
    PreservationService,
)
from core.infrastructure.lifecycle.docker_gateway import DockerPreservationGateway

PROJECT_HASH = "1" * 64
WORKFLOW_HASH = "2" * 64
USER_HASH = "3" * 64
VOLUME_ID = "5" * 64
SECRET_REF_HASH = "6" * 64

DATABASE_FIELDS = {
    "schema_fingerprint": "schema-before",
    "migrations": {"001.sql": "checksum-1"},
    "project_count": 2,
    "project_identities": PROJECT_HASH,
    "workflow_count": 3,
    "workflow_identities": WORKFLOW_HASH,
    "user_count": 1,
    "user_identities": USER_HASH,
    "task_count": 7,
    "job_count": 8,
    "memory_count": 9,
    "knowledge_document_count": 11,
    "embedded_knowledge_document_count": 10,
    "secret_ref_count": 2,
    "secret_ref_control_identities": SECRET_REF_HASH,
    "secret_ref_control_available": True,
    "secret_ref_control_decryptable": True,
}


class FakePreservationRepository:
    def __init__(self, fields: dict[str, object] | None = None, *, reachable: bool = True):
        self.fields = dict(fields or DATABASE_FIELDS)
        self.reachable = reachable

    def capture(self):
        return SimpleNamespace(database_reachable=self.reachable, fields=self.fields)


class FakeDockerGateway:
    def __init__(self, fields: dict[str, object] | None = None):
        self.fields = fields or {
            "compose_project": "flow-steward",
            "durable_volumes": ({"name": "flow-steward_fs-postgres", "volume_id": VOLUME_ID},),
        }

    def capture(self, project_name: str):
        assert project_name == "flow-steward"
        return SimpleNamespace(fields=self.fields)


def _service(
    *,
    database: FakePreservationRepository | None = None,
    docker: FakeDockerGateway | None = None,
) -> PreservationService:
    return PreservationService(
        repository=database or FakePreservationRepository(),
        docker_gateway=docker or FakeDockerGateway(),
    )


def _capture(service: PreservationService | None = None) -> PreservationManifest:
    return (service or _service()).capture(
        operation_id="operation-1",
        stage="before",
        compose_project="flow-steward",
        release="1.2.3",
        environment="development",
        storage_backend="bundled",
        storage_connectivity=True,
    )


def test_capture_contains_full_database_docker_release_and_storage_inventory() -> None:
    manifest = _capture()

    assert manifest.database_reachable is True
    assert manifest.fields["compose_project"] == "flow-steward"
    assert manifest.fields["durable_volumes"] == (
        {"name": "flow-steward_fs-postgres", "volume_id": VOLUME_ID},
    )
    assert manifest.fields["release"] == "1.2.3"
    assert manifest.fields["environment"] == "development"
    assert manifest.fields["storage_backend"] == "bundled"
    assert manifest.fields["storage_connectivity"] is True
    assert {**DATABASE_FIELDS}.items() <= manifest.fields.items()


def test_clone_restore_comparison_retains_target_topology_and_replaces_source_state() -> None:
    service = _service()
    source = _capture(service)
    target_before = replace(
        source,
        operation_id="restore-1",
        fields={
            **source.fields,
            "release": "9.9.9",
            "environment": "production",
            "storage_backend": "local_fs",
            "project_count": 99,
        },
    )
    after = replace(
        source,
        operation_id="restore-1",
        stage="after",
        fields={
            **source.fields,
            "compose_project": target_before.fields["compose_project"],
            "durable_volumes": target_before.fields["durable_volumes"],
            "environment": target_before.fields["environment"],
        },
    )

    comparison = service.compare_clone_restore(
        source=source,
        target_before=target_before,
        after=after,
    )

    assert comparison.success is True


def test_clone_restore_comparison_rejects_target_topology_or_source_state_drift() -> None:
    service = _service()
    source = _capture(service)
    target_before = replace(source, operation_id="restore-1")
    after = replace(
        source,
        operation_id="restore-1",
        stage="after",
        fields={
            **source.fields,
            "compose_project": "other-target",
            "project_count": 1,
        },
    )

    comparison = service.compare_clone_restore(
        source=source,
        target_before=target_before,
        after=after,
    )

    assert comparison.success is False
    assert {failure.field for failure in comparison.failures} >= {
        "compose_project",
        "project_count",
    }


def test_capture_maps_unreachable_database_observations_to_unknown_not_zero() -> None:
    service = _service(
        database=FakePreservationRepository(
            dict.fromkeys(DATABASE_FIELDS, UNKNOWN),
            reachable=False,
        )
    )

    manifest = _capture(service)

    assert manifest.database_reachable is False
    assert manifest.fields["project_count"] == UNKNOWN
    assert manifest.fields["job_count"] == UNKNOWN
    assert manifest.fields["schema_fingerprint"] == UNKNOWN
    assert 0 not in (
        manifest.fields["project_count"],
        manifest.fields["job_count"],
    )


def test_capture_rejects_unknown_adapter_fields_and_disconnected_string() -> None:
    service = _service(
        database=FakePreservationRepository({**DATABASE_FIELDS, "api_token": "unsafe"})
    )

    with pytest.raises(ValueError, match="unexpected fields"):
        _capture(service)
    _raises_callable_140_1 = _service().capture
    with pytest.raises(ValueError, match="storage connectivity"):
        _raises_callable_140_1(
            operation_id="operation-1",
            stage="before",
            compose_project="flow-steward",
            release="1.2.3",
            environment="development",
            storage_backend="bundled",
            storage_connectivity="disconnected",
        )


def test_capture_rejects_unhashed_secret_control_identity() -> None:
    service = _service(
        database=FakePreservationRepository(
            {**DATABASE_FIELDS, "secret_ref_control_identities": "account-a:provider.api_key"}
        )
    )

    with pytest.raises(ValueError, match="stable hash"):
        _capture(service)


def test_required_unknown_blocks_full_preservation_success() -> None:
    before = _capture()
    after = replace(
        before,
        stage="after",
        fields={**before.fields, "memory_count": UNKNOWN},
    )

    comparison = _service().compare(
        before,
        after,
        operation=LifecycleOperationKind.STACK_RESTART,
    )

    assert comparison.success is False
    assert any(
        failure.field == "memory_count" and failure.reason == "required_observation_unknown"
        for failure in comparison.failures
    )


def test_comparison_rejects_mismatched_or_reversed_manifest_pairs() -> None:
    before = _capture()
    different_operation = replace(before, operation_id="operation-2", stage="after")
    reversed_stage = replace(before, stage="after")

    _raises_callable_189_1 = _service().compare
    with pytest.raises(ValueError, match="operation"):
        _raises_callable_189_1(
            before, different_operation, operation=LifecycleOperationKind.STACK_RESTART
        )
    _raises_callable_195_1 = _service().compare
    with pytest.raises(ValueError, match=r"before.*after"):
        _raises_callable_195_1(
            reversed_stage, before, operation=LifecycleOperationKind.STACK_RESTART
        )


def test_comparison_rejects_partial_manifests_even_for_docker_only_scope() -> None:
    complete = _capture()
    partial_fields = {
        "compose_project": complete.fields["compose_project"],
        "durable_volumes": complete.fields["durable_volumes"],
    }

    _raises_callable_210_1 = _service().compare
    _raises_input_210_2 = replace(complete, database_reachable=False, fields=partial_fields)
    _raises_input_210_3 = replace(complete, stage="after", fields=partial_fields)
    with pytest.raises(ValueError, match="missing fields"):
        _raises_callable_210_1(
            _raises_input_210_2, _raises_input_210_3, operation=LifecycleOperationKind.STACK_STOP
        )


def test_environment_switch_requires_stable_release_schema_migrations_and_identity() -> None:
    before = _capture()
    after = replace(
        before,
        stage="after",
        fields={
            **before.fields,
            "environment": "production",
            "job_count": 9,
            "memory_count": 10,
        },
    )

    passing = _service().compare(
        before,
        after,
        operation=LifecycleOperationKind.ENVIRONMENT_SWITCH,
    )
    changed_release = replace(
        after,
        fields={**after.fields, "release": "1.2.4"},
    )
    failing = _service().compare(
        before,
        changed_release,
        operation=LifecycleOperationKind.ENVIRONMENT_SWITCH,
    )

    assert passing.success is True
    assert failing.success is False
    assert any(
        failure.field == "release" and failure.policy == "must_equal"
        for failure in failing.failures
    )


def test_environment_switch_retains_existing_volume_identities_while_allowing_additions() -> None:
    before = _capture()
    original_volumes = before.fields["durable_volumes"]
    assert isinstance(original_volumes, tuple)
    added_volume = {
        "name": "flow-steward_fs-cache",
        "volume_id": "b" * 64,
    }
    after_with_addition = replace(
        before,
        stage="after",
        fields={
            **before.fields,
            "environment": "production",
            "durable_volumes": (*original_volumes, added_volume),
        },
    )

    passing = _service().compare(
        before,
        after_with_addition,
        operation=LifecycleOperationKind.ENVIRONMENT_SWITCH,
    )
    removed = _service().compare(
        before,
        replace(after_with_addition, fields={**after_with_addition.fields, "durable_volumes": ()}),
        operation=LifecycleOperationKind.ENVIRONMENT_SWITCH,
    )
    replaced = _service().compare(
        before,
        replace(
            after_with_addition,
            fields={
                **after_with_addition.fields,
                "durable_volumes": (
                    {**original_volumes[0], "volume_id": "c" * 64},
                    added_volume,
                ),
            },
        ),
        operation=LifecycleOperationKind.ENVIRONMENT_SWITCH,
    )

    assert passing.success is True
    assert removed.success is False
    assert replaced.success is False
    assert {failure.field for failure in (*removed.failures, *replaced.failures)} == {
        "durable_volumes"
    }


def test_profile_configuration_switch_retains_existing_volumes_and_allows_addition() -> None:
    before = _capture()
    original_volumes = before.fields["durable_volumes"]
    assert isinstance(original_volumes, tuple)
    added_volume = {
        "name": "flow-steward_fs-compact-data",
        "volume_id": "b" * 64,
    }
    after = replace(
        before,
        stage="after",
        fields={
            **before.fields,
            "durable_volumes": (*original_volumes, added_volume),
        },
    )

    passing = _service().compare(
        before,
        after,
        operation=LifecycleOperationKind.CONFIG_APPLY,
        allow_volume_additions=True,
    )
    removed = _service().compare(
        before,
        replace(after, fields={**after.fields, "durable_volumes": (added_volume,)}),
        operation=LifecycleOperationKind.CONFIG_APPLY,
        allow_volume_additions=True,
    )

    assert passing.success is True
    assert removed.success is False
    assert removed.failures[0].field == "durable_volumes"


def test_release_upgrade_allows_only_exact_declared_release_schema_and_migrations() -> None:
    before = _capture()
    expected = DeclaredMigrationChange(
        release="1.3.0",
        schema_fingerprint="schema-after",
        migrations={"001.sql": "checksum-1", "002.sql": "checksum-2"},
    )
    after = replace(
        before,
        stage="after",
        fields={
            **before.fields,
            "release": expected.release,
            "schema_fingerprint": expected.schema_fingerprint,
            "migrations": expected.migrations,
            "job_count": 10,
        },
    )

    passing = _service().compare(
        before,
        after,
        operation=LifecycleOperationKind.RELEASE_UPGRADE,
        declared_migration_change=expected,
    )
    drifted = replace(
        after,
        fields={
            **after.fields,
            "migrations": {**expected.migrations, "unexpected.sql": "checksum-x"},
        },
    )
    failing = _service().compare(
        before,
        drifted,
        operation=LifecycleOperationKind.RELEASE_UPGRADE,
        declared_migration_change=expected,
    )

    assert passing.success is True
    assert failing.success is False
    assert any(
        failure.field == "migrations" and failure.policy == "migration_expected_change"
        for failure in failing.failures
    )


def test_release_upgrade_requires_declaration_before_comparison_can_succeed() -> None:
    before = _capture()
    after = replace(
        before,
        stage="after",
        fields={**before.fields, "release": "1.3.0"},
    )

    comparison = _service().compare(
        before,
        after,
        operation=LifecycleOperationKind.RELEASE_UPGRADE,
    )

    assert comparison.success is False
    assert any(failure.reason == "expected_change_not_declared" for failure in comparison.failures)


def test_counts_may_grow_but_must_not_decrease_and_audit_growth_is_irrelevant() -> None:
    before = _capture()
    after = replace(
        before,
        stage="after",
        fields={
            **before.fields,
            "project_count": 3,
            "job_count": 20,
        },
    )

    passing = _service().compare(
        before,
        after,
        operation=LifecycleOperationKind.STACK_REBUILD,
    )
    decreased = replace(after, fields={**after.fields, "task_count": 6})
    failing = _service().compare(
        before,
        decreased,
        operation=LifecycleOperationKind.STACK_REBUILD,
    )

    assert passing.success is True
    assert failing.success is False
    assert any(
        failure.field == "task_count" and failure.policy == "must_not_decrease"
        for failure in failing.failures
    )
    assert "audit_event_count" not in before.fields
    assert all(failure.field != "audit_event_count" for failure in passing.failures)


def test_persisted_volume_list_matches_fresh_tuple_snapshot() -> None:
    fresh = _capture()
    persisted = replace(
        fresh,
        fields={
            **fresh.fields,
            "durable_volumes": list(fresh.fields["durable_volumes"]),
        },
    )
    after = replace(fresh, stage="after")

    comparison = _service().compare(
        persisted,
        after,
        operation=LifecycleOperationKind.STACK_REBUILD,
    )

    assert comparison.success is True


def test_secret_control_and_storage_connectivity_have_explicit_policies() -> None:
    before = _capture()
    after = replace(
        before,
        stage="after",
        fields={
            **before.fields,
            "secret_ref_control_available": False,
            "secret_ref_control_decryptable": False,
            "storage_connectivity": False,
        },
    )

    comparison = _service().compare(
        before,
        after,
        operation=LifecycleOperationKind.STACK_RESTART,
    )

    policies = {failure.field: failure.policy for failure in comparison.failures}
    assert policies["secret_ref_control_available"] == "must_exist"
    assert policies["secret_ref_control_decryptable"] == "must_decrypt"
    assert policies["storage_connectivity"] == "must_exist"


def test_required_controls_must_exist_and_decrypt_on_both_sides() -> None:
    after = _capture()
    before = replace(
        after,
        fields={
            **after.fields,
            "secret_ref_control_available": False,
            "secret_ref_control_decryptable": False,
            "storage_connectivity": False,
        },
    )

    comparison = _service().compare(
        before,
        replace(after, stage="after"),
        operation=LifecycleOperationKind.STACK_RESTART,
    )

    failures = {failure.field: failure.reason for failure in comparison.failures}
    assert failures["secret_ref_control_available"] == "required_value_missing_before"
    assert failures["secret_ref_control_decryptable"] == "control_not_decryptable_before"
    assert failures["storage_connectivity"] == "required_value_missing_before"


def test_empty_secret_ref_control_proofs_compare_successfully() -> None:
    empty_control_hash = "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
    before = replace(
        _capture(),
        fields={
            **DATABASE_FIELDS,
            "compose_project": "flow-steward",
            "durable_volumes": ({"name": "flow-steward_fs-postgres", "volume_id": VOLUME_ID},),
            "release": "1.2.3",
            "environment": "development",
            "storage_backend": "bundled",
            "storage_connectivity": True,
            "secret_ref_count": 0,
            "secret_ref_control_identities": empty_control_hash,
            "secret_ref_control_available": True,
            "secret_ref_control_decryptable": True,
        },
    )
    after = replace(before, stage="after")

    comparison = _service().compare(
        before,
        after,
        operation=LifecycleOperationKind.ENVIRONMENT_SWITCH,
    )

    assert comparison.success is True


@pytest.mark.parametrize(
    ("changes", "expected_fields"),
    [
        (
            {
                "secret_ref_count": 0,
                "secret_ref_control_identities": UNKNOWN,
                "secret_ref_control_available": False,
                "secret_ref_control_decryptable": False,
            },
            {"secret_ref_count", "secret_ref_control_identities"},
        ),
        (
            {
                "secret_ref_count": 1,
                "secret_ref_control_identities": "7" * 64,
            },
            {"secret_ref_count", "secret_ref_control_identities"},
        ),
        (
            {"secret_ref_control_identities": "8" * 64},
            {"secret_ref_control_identities"},
        ),
    ],
    ids=("all-lost", "one-lost", "wrong-control"),
)
def test_secret_control_identity_set_is_bound_across_manifests(
    changes: dict[str, object], expected_fields: set[str]
) -> None:
    before = _capture()
    after = replace(before, stage="after", fields={**before.fields, **changes})

    comparison = _service().compare(
        before,
        after,
        operation=LifecycleOperationKind.STACK_RESTART,
    )

    assert comparison.success is False
    failure_fields = {failure.field for failure in comparison.failures}
    assert expected_fields <= failure_fields


def test_stack_stop_compares_docker_identity_without_claiming_offline_db_counts() -> None:
    before = _capture()
    offline_fields = {
        **before.fields,
        **dict.fromkeys(DATABASE_FIELDS, UNKNOWN),
    }
    after = replace(
        before,
        stage="after",
        database_reachable=False,
        fields=offline_fields,
    )

    passing = _service().compare(
        before,
        after,
        operation=LifecycleOperationKind.STACK_STOP,
    )
    changed_volume = replace(
        after,
        fields={**after.fields, "durable_volumes": ()},
    )
    failing = _service().compare(
        before,
        changed_volume,
        operation=LifecycleOperationKind.STACK_STOP,
    )

    assert passing.success is True
    assert set(passing.compared_fields) == {"compose_project", "durable_volumes"}
    assert failing.success is False
    assert failing.failures[0].field == "durable_volumes"


def test_stack_start_uses_docker_only_scope_when_database_was_offline_before() -> None:
    before = replace(_capture(), database_reachable=False)
    before = replace(
        before,
        fields={
            **before.fields,
            **dict.fromkeys(DATABASE_FIELDS, UNKNOWN),
        },
    )
    after = replace(before, stage="after")

    comparison = _service().compare(
        before,
        after,
        operation=LifecycleOperationKind.STACK_START,
    )

    assert comparison.success is True
    assert set(comparison.compared_fields) == {"compose_project", "durable_volumes"}


class FakeProcessRunner:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command, *, cwd, timeout):
        del cwd, timeout
        self.commands.append(tuple(command))
        return self.outcomes.pop(0)


def test_docker_gateway_keeps_subprocess_details_outside_application_policy(
    tmp_path: Path,
) -> None:
    runner = FakeProcessRunner(
        [
            SimpleNamespace(returncode=0, stdout="flow-steward_fs-postgres\n", stderr=""),
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "Name": "flow-steward_fs-postgres",
                        "Driver": "local",
                        "Scope": "local",
                        "CreatedAt": "2026-07-24T00:00:00Z",
                        "Labels": {
                            "com.docker.compose.project": "flow-steward",
                            "com.docker.compose.volume": "fs-postgres",
                        },
                        "Options": None,
                    }
                )
                + "\n",
                stderr="",
            ),
        ]
    )
    gateway = DockerPreservationGateway(project_root=tmp_path, runner=runner)

    snapshot = gateway.capture("flow-steward")

    assert snapshot.fields["compose_project"] == "flow-steward"
    volumes = snapshot.fields["durable_volumes"]
    assert isinstance(volumes, tuple)
    assert volumes[0]["name"] == "flow-steward_fs-postgres"
    assert len(volumes[0]["volume_id"]) == 64
    assert runner.commands[0][:3] == ("docker", "volume", "ls")
    assert runner.commands[1][:3] == ("docker", "volume", "inspect")


def test_docker_inventory_failure_is_unknown_not_empty(tmp_path: Path) -> None:
    runner = FakeProcessRunner(
        [SimpleNamespace(returncode=1, stdout="", stderr="daemon unavailable")]
    )
    gateway = DockerPreservationGateway(project_root=tmp_path, runner=runner)

    snapshot = gateway.capture("flow-steward")

    assert snapshot.fields["compose_project"] == "flow-steward"
    assert snapshot.fields["durable_volumes"] == UNKNOWN


def test_docker_inventory_timeout_is_unknown_not_an_uncaught_process_error(
    tmp_path: Path,
) -> None:
    def timeout_runner(command, *, cwd, timeout):
        del cwd
        raise subprocess.TimeoutExpired(command, timeout)

    gateway = DockerPreservationGateway(project_root=tmp_path, runner=timeout_runner)

    snapshot = gateway.capture("flow-steward")

    assert snapshot.fields["durable_volumes"] == UNKNOWN
