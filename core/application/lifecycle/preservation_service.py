"""Application policy for lifecycle preservation manifests."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Protocol

from core.application.lifecycle.contracts import LifecycleOperationKind

UNKNOWN = "unknown"
_SAFE_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_STABLE_HASH = re.compile(r"^[0-9a-f]{64}$")

DATABASE_FIELD_NAMES = (
    "schema_fingerprint",
    "migrations",
    "project_count",
    "project_identities",
    "workflow_count",
    "workflow_identities",
    "user_count",
    "user_identities",
    "task_count",
    "job_count",
    "memory_count",
    "knowledge_document_count",
    "embedded_knowledge_document_count",
    "secret_ref_count",
    "secret_ref_control_identities",
    "secret_ref_control_available",
    "secret_ref_control_decryptable",
)
MANIFEST_FIELD_NAMES = (
    "compose_project",
    "durable_volumes",
    "release",
    "environment",
    "storage_backend",
    "storage_connectivity",
    *DATABASE_FIELD_NAMES,
)

_IDENTITY_FIELDS = (
    "compose_project",
    "durable_volumes",
    "release",
    "environment",
    "schema_fingerprint",
    "migrations",
    "project_identities",
    "workflow_identities",
    "user_identities",
    "secret_ref_control_identities",
    "storage_backend",
)
_COUNT_FIELDS = (
    "project_count",
    "workflow_count",
    "user_count",
    "task_count",
    "job_count",
    "memory_count",
    "knowledge_document_count",
    "embedded_knowledge_document_count",
    "secret_ref_count",
)
_EXISTENCE_FIELDS = (
    "secret_ref_control_available",
    "storage_connectivity",
)
_DOCKER_IDENTITY_FIELDS = ("compose_project", "durable_volumes")


class ComparisonPolicy(StrEnum):
    MUST_EQUAL = "must_equal"
    MUST_RETAIN = "must_retain"
    MUST_NOT_DECREASE = "must_not_decrease"
    MUST_EXIST = "must_exist"
    MUST_DECRYPT = "must_decrypt"
    MIGRATION_EXPECTED_CHANGE = "migration_expected_change"


@dataclass(frozen=True)
class DeclaredMigrationChange:
    """Exact post-upgrade values declared by the selected release."""

    release: str
    schema_fingerprint: str
    migrations: Mapping[str, str]


@dataclass(frozen=True)
class PreservationManifest:
    """One secret-free preservation observation."""

    operation_id: str
    stage: Literal["before", "after"]
    database_reachable: bool
    fields: Mapping[str, Any]
    schema_version: Literal[1] = 1
    captured_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-compatible persisted manifest shape."""
        return asdict(self)


@dataclass(frozen=True)
class PreservationFailure:
    field: str
    policy: str
    reason: str
    before: Any = None
    after: Any = None


@dataclass(frozen=True)
class PreservationComparison:
    success: bool
    compared_fields: tuple[str, ...]
    failures: tuple[PreservationFailure, ...] = ()


class _DatabaseSnapshot(Protocol):
    database_reachable: bool
    fields: Mapping[str, Any]


class _DockerSnapshot(Protocol):
    fields: Mapping[str, Any]


class _DatabaseRepository(Protocol):
    def capture(self) -> _DatabaseSnapshot: ...


class _DockerGateway(Protocol):
    def capture(self, project_name: str) -> _DockerSnapshot: ...


class PreservationService:
    """Capture and compare manifests without technical Docker or SQL decisions."""

    def __init__(
        self,
        *,
        repository: _DatabaseRepository,
        docker_gateway: _DockerGateway,
    ) -> None:
        self._repository = repository
        self._docker_gateway = docker_gateway

    def capture(
        self,
        *,
        operation_id: str,
        stage: Literal["before", "after"],
        compose_project: str,
        release: str,
        environment: str,
        storage_backend: str,
        storage_connectivity: bool | Literal["unknown"],
    ) -> PreservationManifest:
        """Capture all preservation facts while retaining explicit unknowns."""
        if storage_connectivity not in (True, False, UNKNOWN):
            raise ValueError("Preservation storage connectivity must be true, false, or unknown")
        try:
            database = self._repository.capture()
        except Exception:
            database = _UnavailableDatabaseSnapshot()
        database_fields = _closed_snapshot_fields(
            database.fields,
            allowed=DATABASE_FIELD_NAMES,
            label="database",
        )
        try:
            docker = self._docker_gateway.capture(compose_project)
        except Exception:
            docker_fields = {
                "compose_project": compose_project or UNKNOWN,
                "durable_volumes": UNKNOWN,
            }
        else:
            docker_fields = _closed_snapshot_fields(
                docker.fields,
                allowed=_DOCKER_IDENTITY_FIELDS,
                label="Docker",
            )
        fields: dict[str, Any] = {
            "compose_project": compose_project or UNKNOWN,
            "durable_volumes": UNKNOWN,
            "release": release or UNKNOWN,
            "environment": environment or UNKNOWN,
            "storage_backend": storage_backend or UNKNOWN,
            "storage_connectivity": storage_connectivity,
            **database_fields,
            **docker_fields,
        }
        manifest = PreservationManifest(
            operation_id=operation_id,
            stage=stage,
            database_reachable=database.database_reachable,
            fields=fields,
        )
        validate_manifest(manifest, expected_stage=stage)
        return manifest

    def compare(
        self,
        before: PreservationManifest,
        after: PreservationManifest,
        *,
        operation: LifecycleOperationKind,
        declared_migration_change: DeclaredMigrationChange | None = None,
        allow_volume_additions: bool = False,
    ) -> PreservationComparison:
        """Apply the closed preservation policy for one lifecycle operation."""
        validate_manifest(before, expected_stage="before")
        validate_manifest(after, expected_stage="after")
        if before.operation_id != after.operation_id:
            raise ValueError("Preservation manifests belong to different operation ids")
        policies, expected = _comparison_plan(
            operation,
            database_was_reachable=before.database_reachable,
            declared_migration_change=declared_migration_change,
            allow_volume_additions=allow_volume_additions,
        )
        failures = tuple(
            failure
            for name, policy in policies.items()
            if (
                failure := _compare_field(
                    name,
                    before.fields.get(name, UNKNOWN),
                    after.fields.get(name, UNKNOWN),
                    policy,
                    expected=expected.get(name, UNKNOWN),
                )
            )
            is not None
        )
        return PreservationComparison(
            success=not failures,
            compared_fields=tuple(policies),
            failures=failures,
        )

    def compare_clone_restore(
        self,
        *,
        source: PreservationManifest,
        target_before: PreservationManifest,
        after: PreservationManifest,
    ) -> PreservationComparison:
        """Verify source replacement while retaining the target Docker identity."""
        validate_manifest(source, expected_stage="before")
        validate_manifest(target_before, expected_stage="before")
        validate_manifest(after, expected_stage="after")
        if target_before.operation_id != after.operation_id:
            raise ValueError("Clone target preservation manifests belong to different operations")

        failures: list[PreservationFailure] = []
        for name, policy in {
            "compose_project": ComparisonPolicy.MUST_EQUAL,
            "durable_volumes": ComparisonPolicy.MUST_RETAIN,
            "environment": ComparisonPolicy.MUST_EQUAL,
        }.items():
            failure = _compare_field(
                name,
                target_before.fields.get(name, UNKNOWN),
                after.fields.get(name, UNKNOWN),
                policy,
                expected=UNKNOWN,
            )
            if failure is not None:
                failures.append(failure)

        source_equal = tuple(
            name
            for name in (*_IDENTITY_FIELDS, *_COUNT_FIELDS)
            if name not in (*_DOCKER_IDENTITY_FIELDS, "environment")
        )
        for name in source_equal:
            failure = _compare_field(
                name,
                source.fields.get(name, UNKNOWN),
                after.fields.get(name, UNKNOWN),
                ComparisonPolicy.MUST_EQUAL,
                expected=UNKNOWN,
            )
            if failure is not None:
                failures.append(failure)
        for name in _EXISTENCE_FIELDS:
            failure = _compare_field(
                name,
                source.fields.get(name, UNKNOWN),
                after.fields.get(name, UNKNOWN),
                ComparisonPolicy.MUST_EXIST,
                expected=UNKNOWN,
            )
            if failure is not None:
                failures.append(failure)
        decrypt_failure = _compare_field(
            "secret_ref_control_decryptable",
            source.fields.get("secret_ref_control_decryptable", UNKNOWN),
            after.fields.get("secret_ref_control_decryptable", UNKNOWN),
            ComparisonPolicy.MUST_DECRYPT,
            expected=UNKNOWN,
        )
        if decrypt_failure is not None:
            failures.append(decrypt_failure)
        compared = (
            "compose_project",
            "durable_volumes",
            "environment",
            *source_equal,
            *_EXISTENCE_FIELDS,
            "secret_ref_control_decryptable",
        )
        return PreservationComparison(
            success=not failures,
            compared_fields=compared,
            failures=tuple(failures),
        )


@dataclass(frozen=True)
class _UnavailableDatabaseSnapshot:
    database_reachable: bool = False
    fields: Mapping[str, Any] = field(
        default_factory=lambda: dict.fromkeys(DATABASE_FIELD_NAMES, UNKNOWN)
    )


def _comparison_plan(
    operation: LifecycleOperationKind,
    *,
    database_was_reachable: bool,
    declared_migration_change: DeclaredMigrationChange | None,
    allow_volume_additions: bool = False,
) -> tuple[dict[str, ComparisonPolicy], dict[str, Any]]:
    if operation is LifecycleOperationKind.STACK_STOP or (
        operation
        in {
            LifecycleOperationKind.STACK_START,
            LifecycleOperationKind.STACK_RESTART,
            LifecycleOperationKind.STACK_REBUILD,
        }
        and not database_was_reachable
    ):
        return (
            dict.fromkeys(_DOCKER_IDENTITY_FIELDS, ComparisonPolicy.MUST_EQUAL),
            {},
        )

    equal_fields = list(_IDENTITY_FIELDS)
    expected: dict[str, Any] = {}
    if operation is LifecycleOperationKind.ENVIRONMENT_SWITCH:
        equal_fields.remove("environment")
        equal_fields.remove("storage_backend")
        equal_fields.remove("durable_volumes")
    elif operation is LifecycleOperationKind.CONFIG_APPLY and allow_volume_additions:
        equal_fields.remove("durable_volumes")
    elif operation is LifecycleOperationKind.RELEASE_UPGRADE:
        for name in ("release", "schema_fingerprint", "migrations"):
            equal_fields.remove(name)
        if declared_migration_change is not None:
            expected = {
                "release": declared_migration_change.release,
                "schema_fingerprint": declared_migration_change.schema_fingerprint,
                "migrations": dict(declared_migration_change.migrations),
            }

    policies = dict.fromkeys(equal_fields, ComparisonPolicy.MUST_EQUAL)
    if operation is LifecycleOperationKind.ENVIRONMENT_SWITCH or (
        operation is LifecycleOperationKind.CONFIG_APPLY and allow_volume_additions
    ):
        policies["durable_volumes"] = ComparisonPolicy.MUST_RETAIN
    policies.update(dict.fromkeys(_COUNT_FIELDS, ComparisonPolicy.MUST_NOT_DECREASE))
    policies.update(dict.fromkeys(_EXISTENCE_FIELDS, ComparisonPolicy.MUST_EXIST))
    policies["secret_ref_control_decryptable"] = ComparisonPolicy.MUST_DECRYPT
    if operation is LifecycleOperationKind.RELEASE_UPGRADE:
        policies.update(
            dict.fromkeys(
                ("release", "schema_fingerprint", "migrations"),
                ComparisonPolicy.MIGRATION_EXPECTED_CHANGE,
            )
        )
    return policies, expected


def _compare_field(
    name: str,
    before: Any,
    after: Any,
    policy: ComparisonPolicy,
    *,
    expected: Any,
) -> PreservationFailure | None:
    if policy is ComparisonPolicy.MIGRATION_EXPECTED_CHANGE:
        if before == UNKNOWN:
            return PreservationFailure(
                field=name,
                policy=policy.value,
                reason="required_observation_unknown",
                before=before,
                after=after,
            )
        if expected == UNKNOWN:
            return PreservationFailure(
                name,
                policy.value,
                "expected_change_not_declared",
                before,
                after,
            )
        if after != expected:
            return PreservationFailure(
                name,
                policy.value,
                "unexpected_migration_change",
                before,
                after,
            )
        return None
    if before == UNKNOWN or after == UNKNOWN:
        return PreservationFailure(
            field=name,
            policy=policy.value,
            reason="required_observation_unknown",
            before=before,
            after=after,
        )
    if policy is ComparisonPolicy.MUST_EQUAL and not _structurally_equal(before, after):
        return PreservationFailure(name, policy.value, "values_differ", before, after)
    if policy is ComparisonPolicy.MUST_RETAIN and not _retains_all(before, after):
        return PreservationFailure(name, policy.value, "required_value_removed", before, after)
    if policy is ComparisonPolicy.MUST_NOT_DECREASE:
        if not _nonnegative_int(before) or not _nonnegative_int(after):
            return PreservationFailure(name, policy.value, "invalid_count", before, after)
        if after < before:
            return PreservationFailure(name, policy.value, "count_decreased", before, after)
    if policy is ComparisonPolicy.MUST_EXIST:
        if not _exists(before):
            return PreservationFailure(
                name,
                policy.value,
                "required_value_missing_before",
                before,
                after,
            )
        if not _exists(after):
            return PreservationFailure(name, policy.value, "required_value_missing", before, after)
    if policy is ComparisonPolicy.MUST_DECRYPT:
        if before is not True:
            return PreservationFailure(
                name,
                policy.value,
                "control_not_decryptable_before",
                before,
                after,
            )
        if after is not True:
            return PreservationFailure(name, policy.value, "control_not_decryptable", before, after)
    return None


def _nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _structurally_equal(left: Any, right: Any) -> bool:
    """Compare persisted JSON sequences and fresh immutable snapshots equally."""
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(
            _structurally_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return set(left) == set(right) and all(
            _structurally_equal(left[key], right[key]) for key in left
        )
    return left == right


def _retains_all(before: Any, after: Any) -> bool:
    if not isinstance(before, (list, tuple)) or not isinstance(after, (list, tuple)):
        return False
    remaining = list(after)
    for expected in before:
        for index, observed in enumerate(remaining):
            if _structurally_equal(expected, observed):
                del remaining[index]
                break
        else:
            return False
    return True


def _exists(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return value not in (None, "", (), [], {})


def validate_manifest(
    manifest: PreservationManifest,
    *,
    expected_stage: Literal["before", "after"] | None = None,
) -> None:
    """Validate runtime manifest invariants that static annotations cannot enforce."""
    if manifest.schema_version != 1:
        raise ValueError("Unsupported preservation manifest schema version")
    if not _SAFE_OPERATION_ID.fullmatch(str(manifest.operation_id)):
        raise ValueError("Preservation operation id contains unsafe characters")
    if manifest.stage not in {"before", "after"}:
        raise ValueError("Preservation manifest stage must be before or after")
    if expected_stage is not None and manifest.stage != expected_stage:
        raise ValueError("Preservation comparison requires a before and after manifest")
    unexpected = sorted(set(manifest.fields).difference(MANIFEST_FIELD_NAMES))
    if unexpected:
        raise ValueError(f"Preservation manifest contains unexpected fields: {unexpected}")
    missing = sorted(set(MANIFEST_FIELD_NAMES).difference(manifest.fields))
    if missing:
        raise ValueError(f"Preservation manifest is missing fields: {missing}")
    for name in _COUNT_FIELDS:
        if name in manifest.fields:
            value = manifest.fields[name]
            if value != UNKNOWN and not _nonnegative_int(value):
                raise ValueError(f"Preservation manifest has invalid count field: {name}")
    for name in (
        "project_identities",
        "workflow_identities",
        "user_identities",
        "secret_ref_control_identities",
    ):
        if name in manifest.fields:
            value = manifest.fields[name]
            if value != UNKNOWN and not _STABLE_HASH.fullmatch(str(value)):
                raise ValueError(f"Preservation identity field is not a stable hash: {name}")
    for name in (
        "secret_ref_control_available",
        "secret_ref_control_decryptable",
        "storage_connectivity",
    ):
        if name in manifest.fields and manifest.fields[name] not in (True, False, UNKNOWN):
            raise ValueError(f"Preservation control field is invalid: {name}")
    _validate_volume_identities(manifest.fields.get("durable_volumes", UNKNOWN))


def _closed_snapshot_fields(
    fields: Mapping[str, Any],
    *,
    allowed: tuple[str, ...],
    label: str,
) -> dict[str, Any]:
    unexpected = sorted(set(fields).difference(allowed))
    if unexpected:
        raise ValueError(f"{label} preservation snapshot contains unexpected fields: {unexpected}")
    return {name: fields.get(name, UNKNOWN) for name in allowed}


def _validate_volume_identities(value: Any) -> None:
    if value == UNKNOWN:
        return
    if not isinstance(value, (list, tuple)):
        raise ValueError("Preservation durable volumes must be a sequence or unknown")
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"name", "volume_id"}:
            raise ValueError("Preservation durable volume identity has an invalid shape")
        if not str(item["name"]).strip() or not _STABLE_HASH.fullmatch(str(item["volume_id"])):
            raise ValueError("Preservation durable volume identity is incomplete")
