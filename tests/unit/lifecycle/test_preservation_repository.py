from __future__ import annotations

import json
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import pytest
from psycopg import IsolationLevel

from core.application.lifecycle.preservation_repository import (
    PreservationRepository as PreservationRepositoryPort,
)
from core.application.lifecycle.preservation_service import (
    MANIFEST_FIELD_NAMES,
    UNKNOWN,
    PreservationManifest,
)
from core.infrastructure.db.psycopg_runtime._connection import ConnectionAdapter
from core.infrastructure.lifecycle.preservation_repository import (
    PostgresPreservationRepository as PreservationRepository,
)
from core.infrastructure.lifecycle.preservation_repository import (
    PreservationManifestStore,
    stable_identity_hash,
)

VOLUME_ID = "5" * 64


class FakeTransaction(AbstractContextManager[None]):
    def __init__(self, connection: FakeConnection, *, force_rollback: bool):
        self.connection = connection
        self.force_rollback = force_rollback

    def __enter__(self) -> None:
        self.connection.transaction_entered = True
        self.connection.read_only_during_transaction = self.connection.read_only
        self.connection.isolation_level_during_transaction = self.connection.isolation_level
        self.connection.force_rollback = self.force_rollback

    def __exit__(self, exc_type, exc, tb) -> None:
        self.connection.transaction_exited = True
        self.connection.in_transaction = False


class FakeCursor(AbstractContextManager["FakeCursor"]):
    def __init__(self, connection: FakeConnection):
        self.connection = connection
        self.rows: list[Any] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def execute(self, sql: str, params=()) -> None:
        assert self.connection.in_transaction is True
        normalized = " ".join(sql.split())
        self.connection.statements.append(normalized)
        self.connection.parameters.append(tuple(params))
        if "FROM public.platform_schema_state" in normalized:
            self.rows = [("schema-fingerprint",)]
        elif "to_regclass('public.platform_schema_migrations')" in normalized:
            self.rows = [
                (
                    "public.platform_schema_migrations"
                    if self.connection.migration_table_exists
                    else None,
                )
            ]
        elif "FROM public.platform_schema_migrations" in normalized:
            self.rows = [("001.sql", "checksum-1"), ("002.sql", "checksum-2")]
        elif "FROM public.projects" in normalized:
            self.rows = [("project-b",), ("project-a",)]
        elif "FROM public.project_workflows" in normalized:
            self.rows = [("project-a", "workflow-a"), ("project-b", "workflow-b")]
        elif "FROM public.users" in normalized:
            self.rows = [("user-a",), ("user-b",)]
        elif "COUNT(DISTINCT (project_id, task_id))" in normalized:
            self.rows = [(5,)]
        elif "FROM public.jobs" in normalized:
            self.rows = [(6,)]
        elif "FROM public.memory_records" in normalized:
            self.rows = [(7,)]
        elif "embedding IS NOT NULL" in normalized and "workflow_knowledge_documents" in normalized:
            self.rows = [(6,)]
        elif "COUNT(*) FROM public.platform_workflow_knowledge_documents" in normalized:
            self.rows = [(8,)]
        elif "FROM public.secret_refs" in normalized:
            self.rows = list(self.connection.secret_refs)
        else:  # pragma: no cover - makes additions fail loudly
            raise AssertionError(f"unexpected query: {normalized}")

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class FakeConnection:
    def __init__(
        self,
        *,
        migration_table_exists: bool = True,
        secret_refs: tuple[tuple[str, str, str], ...] = (
            ("account-a", "provider.api_key", "ciphertext-must-never-serialize"),
        ),
    ):
        self.read_only = False
        self.isolation_level = IsolationLevel.READ_COMMITTED
        self.closed = False
        self.transaction_entered = False
        self.transaction_exited = False
        self.read_only_during_transaction = False
        self.isolation_level_during_transaction = None
        self.force_rollback = False
        self.in_transaction = False
        self.statements: list[str] = []
        self.parameters: list[tuple[Any, ...]] = []
        self.migration_table_exists = migration_table_exists
        self.secret_refs = secret_refs

    def transaction(self, *, force_rollback: bool = False) -> FakeTransaction:
        self.in_transaction = True
        return FakeTransaction(self, force_rollback=force_rollback)

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def close(self) -> None:
        self.closed = True


def test_repository_uses_one_read_only_transaction_and_closes_owned_connection() -> None:
    raw_connection = FakeConnection()
    connection = ConnectionAdapter(raw_connection)
    decrypted: list[str] = []
    repository = PreservationRepository(
        connection_factory=lambda: connection,
        decrypt_control=lambda value: decrypted.append(value) or "plain-control",
    )

    snapshot = repository.capture()

    assert snapshot.database_reachable is True
    assert raw_connection.read_only is False
    assert raw_connection.read_only_during_transaction is True
    assert raw_connection.isolation_level_during_transaction is IsolationLevel.REPEATABLE_READ
    assert raw_connection.isolation_level is IsolationLevel.READ_COMMITTED
    assert raw_connection.force_rollback is True
    assert raw_connection.transaction_entered is True
    assert raw_connection.transaction_exited is True
    assert raw_connection.closed is True
    assert decrypted == ["ciphertext-must-never-serialize"]


def test_repository_restores_borrowed_connection_read_only_state_without_closing() -> None:
    connection = FakeConnection()

    class BorrowedConnection(AbstractContextManager[FakeConnection]):
        def __enter__(self) -> FakeConnection:
            return connection

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    repository = PreservationRepository(
        connection_factory=BorrowedConnection,
        decrypt_control=lambda _value: "plain-control",
    )

    assert repository.capture().database_reachable is True
    assert connection.read_only is False
    assert connection.closed is False


def test_repository_captures_full_counts_hashes_and_secret_control_proof() -> None:
    repository = PreservationRepository(
        connection_factory=FakeConnection,
        decrypt_control=lambda _value: "plain-control",
    )

    snapshot = repository.capture()

    assert snapshot.fields == {
        "schema_fingerprint": "schema-fingerprint",
        "migrations": {"001.sql": "checksum-1", "002.sql": "checksum-2"},
        "project_count": 2,
        "project_identities": stable_identity_hash(("project-a", "project-b")),
        "workflow_count": 2,
        "workflow_identities": stable_identity_hash(
            (("project-a", "workflow-a"), ("project-b", "workflow-b"))
        ),
        "user_count": 2,
        "user_identities": stable_identity_hash(("user-a", "user-b")),
        "task_count": 5,
        "job_count": 6,
        "memory_count": 7,
        "knowledge_document_count": 8,
        "embedded_knowledge_document_count": 6,
        "secret_ref_count": 1,
        "secret_ref_control_identities": stable_identity_hash((("account-a", "provider.api_key"),)),
        "secret_ref_control_available": True,
        "secret_ref_control_decryptable": True,
    }
    serialized = json.dumps(snapshot.fields, sort_keys=True)
    for forbidden in (
        "project-a",
        "workflow-a",
        "user-a",
        "main",
        "release",
        "provider.api_key",
        "ciphertext-must-never-serialize",
        "plain-control",
    ):
        assert forbidden not in serialized


def test_repository_checks_every_secret_ref_without_serializing_ciphertext() -> None:
    connection = FakeConnection()
    decrypted: list[str] = []
    repository = PreservationRepository(
        connection_factory=lambda: connection,
        decrypt_control=lambda value: decrypted.append(value) or "plain-control",
    )

    assert repository.capture().database_reachable is True

    control_index = next(
        index
        for index, statement in enumerate(connection.statements)
        if "FROM public.secret_refs" in statement
    )
    assert "WHERE ref_key = %s" not in connection.statements[control_index]
    assert decrypted == ["ciphertext-must-never-serialize"]


def test_empty_secret_ref_set_has_a_valid_empty_control_proof() -> None:
    repository = PreservationRepository(
        connection_factory=lambda: FakeConnection(secret_refs=()),
        decrypt_control=lambda _value: "unused",
    )

    snapshot = repository.capture()

    assert snapshot.database_reachable is True
    assert snapshot.fields["secret_ref_count"] == 0
    assert snapshot.fields["secret_ref_control_identities"] == stable_identity_hash(())
    assert snapshot.fields["secret_ref_control_available"] is True
    assert snapshot.fields["secret_ref_control_decryptable"] is True


def test_decrypt_failure_marks_secret_control_unusable_without_serializing_secrets() -> None:
    repository = PreservationRepository(
        connection_factory=FakeConnection,
        decrypt_control=lambda _value: (_ for _ in ()).throw(ValueError("bad ciphertext")),
    )

    snapshot = repository.capture()

    assert snapshot.database_reachable is True
    assert snapshot.fields["secret_ref_count"] == 1
    assert snapshot.fields["secret_ref_control_available"] is True
    assert snapshot.fields["secret_ref_control_decryptable"] is False
    assert "ciphertext-must-never-serialize" not in json.dumps(snapshot.fields)


def test_connection_adapter_forwards_transaction_characteristic_setters() -> None:
    raw = FakeConnection()
    connection = ConnectionAdapter(raw)

    connection.read_only = True
    connection.isolation_level = IsolationLevel.REPEATABLE_READ

    assert raw.read_only is True
    assert raw.isolation_level is IsolationLevel.REPEATABLE_READ
    assert "read_only" not in connection.__dict__
    assert "isolation_level" not in connection.__dict__


def test_repository_reports_unreachable_observations_as_unknown_not_zero() -> None:
    def unavailable():
        raise OSError("database offline")

    repository = PreservationRepository(
        connection_factory=unavailable,
        decrypt_control=lambda _value: "unused",
    )

    snapshot = repository.capture()

    assert snapshot.database_reachable is False
    assert snapshot.fields
    assert set(snapshot.fields.values()) == {UNKNOWN}


def test_absent_post_1_0_migration_table_is_a_known_empty_manifest() -> None:
    repository = PreservationRepository(
        connection_factory=lambda: FakeConnection(migration_table_exists=False),
        decrypt_control=lambda _value: "plain-control",
    )

    snapshot = repository.capture()

    assert snapshot.database_reachable is True
    assert snapshot.fields["schema_fingerprint"] == "schema-fingerprint"
    assert snapshot.fields["migrations"] == {}


def test_repository_preserves_all_rows_including_soft_deleted_identity_records() -> None:
    connection = FakeConnection()
    repository = PreservationRepository(
        connection_factory=lambda: connection,
        decrypt_control=lambda _value: "plain-control",
    )

    assert repository.capture().database_reachable is True

    identity_and_count_statements = [
        statement
        for statement in connection.statements
        if any(
            table in statement
            for table in (
                "public.projects",
                "public.project_workflows",
                "public.users",
                "public.jobs",
            )
        )
    ]
    assert identity_and_count_statements
    assert all("deleted_at" not in statement for statement in identity_and_count_statements)


def test_empty_identity_sets_have_stable_non_disclosing_hashes() -> None:
    assert stable_identity_hash(()) == stable_identity_hash([])
    assert stable_identity_hash(()) != ""
    assert len(stable_identity_hash(())) == 64


def test_identity_hash_uses_set_semantics_independent_of_order_and_duplicates() -> None:
    assert stable_identity_hash(("identity-b", "identity-a", "identity-a")) == (
        stable_identity_hash(("identity-a", "identity-b"))
    )


def test_application_repository_module_is_a_port_without_infrastructure_operations() -> None:
    source = Path("core/application/lifecycle/preservation_repository.py").read_text(
        encoding="utf-8"
    )

    assert PreservationRepositoryPort
    for forbidden in ("SELECT ", ".cursor(", ".transaction(", "decrypt_control", "tempfile"):
        assert forbidden not in source


def _manifest() -> PreservationManifest:
    fields = dict.fromkeys(MANIFEST_FIELD_NAMES, UNKNOWN)
    fields.update(
        {
            "compose_project": "flow-steward",
            "durable_volumes": ({"name": "flow-steward_fs-postgres", "volume_id": VOLUME_ID},),
            "release": "1.2.3",
            "environment": "development",
            "storage_backend": "bundled",
            "storage_connectivity": True,
            "migrations": {},
            "project_identities": stable_identity_hash(("project-a",)),
            "secret_ref_control_available": True,
            "secret_ref_control_decryptable": True,
        }
    )
    return PreservationManifest(
        operation_id="operation-1",
        stage="before",
        database_reachable=True,
        fields=fields,
    )


def test_manifest_store_writes_atomic_owner_only_json(tmp_path: Path) -> None:
    store = PreservationManifestStore(tmp_path)

    path = store.save(_manifest())

    assert path == (
        tmp_path / ".flow-steward" / "operations" / "operation-1.before-preservation.json"
    )
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 1
    assert list(path.parent.glob("*.tmp")) == []


def test_manifest_store_loads_exact_durable_before_evidence_for_crash_resume(
    tmp_path: Path,
) -> None:
    store = PreservationManifestStore(tmp_path)
    manifest = _manifest()
    store.save(manifest)

    loaded = store.load(manifest.operation_id, "before")

    assert loaded is not None
    assert loaded.to_dict() == json.loads(json.dumps(manifest.to_dict()))
    assert store.load("missing-operation", "before") is None


def test_manifest_store_rejects_unsafe_ids_and_secret_payload_shapes(tmp_path: Path) -> None:
    store = PreservationManifestStore(tmp_path)

    _raises_input_525_1 = _manifest().__class__(
        **{**_manifest().__dict__, "operation_id": "../escape"}
    )
    with pytest.raises(ValueError, match="operation id"):
        store.save(_raises_input_525_1)
    unsafe = _manifest()
    unsafe = PreservationManifest(
        operation_id=unsafe.operation_id,
        stage=unsafe.stage,
        database_reachable=unsafe.database_reachable,
        fields={**unsafe.fields, "encrypted_value": "ciphertext"},
    )
    with pytest.raises(ValueError, match="unexpected fields"):
        store.save(unsafe)


def test_manifest_store_rejects_unknown_fields_and_runtime_invalid_stage(
    tmp_path: Path,
) -> None:
    store = PreservationManifestStore(tmp_path)
    manifest = _manifest()
    unknown_field = PreservationManifest(
        operation_id=manifest.operation_id,
        stage=manifest.stage,
        database_reachable=manifest.database_reachable,
        fields={**manifest.fields, "api_token": "must-not-persist"},
    )
    invalid_stage = PreservationManifest(
        operation_id=manifest.operation_id,
        stage="../../after",  # type: ignore[arg-type]
        database_reachable=manifest.database_reachable,
        fields=manifest.fields,
    )

    with pytest.raises(ValueError, match="unexpected fields"):
        store.save(unknown_field)
    with pytest.raises(ValueError, match="stage"):
        store.save(invalid_stage)
