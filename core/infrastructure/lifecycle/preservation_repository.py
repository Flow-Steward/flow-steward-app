"""PostgreSQL capture and owner-only storage adapters for preservation manifests."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, Literal, cast

from psycopg import IsolationLevel

from core.application.lifecycle.preservation_repository import DatabasePreservationSnapshot
from core.application.lifecycle.preservation_service import (
    DATABASE_FIELD_NAMES,
    UNKNOWN,
    PreservationManifest,
    validate_manifest,
)

_SAFE_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_MISSING = object()
_SECRET_FIELD_NAMES = {
    "ciphertext",
    "encrypted_value",
    "plaintext",
    "ref_key",
    "secret_value",
    "password",
    "token",
}


class PostgresPreservationRepository:
    """Capture a consistent, non-mutating PostgreSQL preservation snapshot."""

    def __init__(
        self,
        *,
        connection_factory: Callable[[], Any],
        decrypt_control: Callable[[str], str],
    ) -> None:
        self._connection_factory = connection_factory
        self._decrypt_control = decrypt_control

    @classmethod
    def from_dsn(
        cls,
        *,
        dsn: str,
        decrypt_control: Callable[[str], str],
    ) -> PostgresPreservationRepository:
        """Build the PostgreSQL adapter without exposing DB imports to entrypoints."""
        from core.infrastructure.db.psycopg_runtime import connect

        return cls(
            connection_factory=lambda: connect(str(dsn)),
            decrypt_control=decrypt_control,
        )

    def capture(self) -> DatabasePreservationSnapshot:
        try:
            with self._connection() as connection:
                previous_read_only = getattr(connection, "read_only", _MISSING)
                previous_isolation_level = getattr(connection, "isolation_level", _MISSING)
                try:
                    connection.isolation_level = IsolationLevel.REPEATABLE_READ
                    connection.read_only = True
                    with connection.transaction(force_rollback=True):
                        fields = self._capture_fields(connection)
                finally:
                    if previous_read_only is not _MISSING:
                        connection.read_only = previous_read_only
                    if previous_isolation_level is not _MISSING:
                        connection.isolation_level = previous_isolation_level
        except Exception:
            return DatabasePreservationSnapshot(
                database_reachable=False,
                fields=dict.fromkeys(DATABASE_FIELD_NAMES, UNKNOWN),
            )
        return DatabasePreservationSnapshot(database_reachable=True, fields=fields)

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        resource = self._connection_factory()
        enter = getattr(resource, "__enter__", None)
        exit_ = getattr(resource, "__exit__", None)
        if callable(enter) and callable(exit_):
            with resource as connection:
                yield connection
            return
        try:
            yield resource
        finally:
            close = getattr(resource, "close", None)
            if callable(close):
                close()

    def _capture_fields(self, connection: Any) -> dict[str, Any]:
        schema_fingerprint = _scalar(
            connection,
            """
            SELECT schema_fingerprint
            FROM public.platform_schema_state
            WHERE schema_key = 'canonical'
            """,
        )
        migrations_table = _scalar(
            connection,
            "SELECT to_regclass('public.platform_schema_migrations')",
        )
        migrations = (
            {
                str(migration_id): str(checksum)
                for migration_id, checksum in _rows(
                    connection,
                    """
                    SELECT migration_id, checksum
                    FROM public.platform_schema_migrations
                    ORDER BY migration_id
                    """,
                )
            }
            if migrations_table is not None
            else {}
        )
        projects = _rows(
            connection,
            "SELECT id FROM public.projects ORDER BY id",
        )
        workflows = _rows(
            connection,
            """
            SELECT project_id, workflow_id
            FROM public.project_workflows
            ORDER BY project_id, workflow_id
            """,
        )
        users = _rows(
            connection,
            "SELECT id FROM public.users ORDER BY id",
        )
        encrypted_controls = _rows(
            connection,
            """
            SELECT account_id, ref_key, encrypted_value
            FROM public.secret_refs
            ORDER BY account_id, ref_key
            """,
        )
        control_identities: list[tuple[str, str]] = []
        control_available = True
        control_decryptable = True
        for account_id, ref_key, encrypted_value in encrypted_controls:
            identity = (str(account_id or "").strip(), str(ref_key or "").strip())
            if not all(identity):
                control_available = False
            else:
                control_identities.append(identity)
            try:
                if not bool(str(encrypted_value or "")) or not bool(
                    self._decrypt_control(str(encrypted_value))
                ):
                    control_decryptable = False
            except Exception:
                control_decryptable = False
        control_identity_hash = (
            stable_identity_hash(control_identities)
            if control_available and len(control_identities) == len(encrypted_controls)
            else UNKNOWN
        )
        return {
            "schema_fingerprint": (
                str(schema_fingerprint) if schema_fingerprint not in (None, "") else UNKNOWN
            ),
            "migrations": migrations,
            "project_count": len(projects),
            "project_identities": stable_identity_hash(row[0] for row in projects),
            "workflow_count": len(workflows),
            "workflow_identities": stable_identity_hash(workflows),
            "user_count": len(users),
            "user_identities": stable_identity_hash(row[0] for row in users),
            "task_count": _count(
                connection,
                """
                SELECT COUNT(DISTINCT (project_id, task_id))
                FROM public.jobs
                """,
            ),
            "job_count": _count(
                connection,
                "SELECT COUNT(*) FROM public.jobs",
            ),
            "memory_count": _count(connection, "SELECT COUNT(*) FROM public.memory_records"),
            "knowledge_document_count": _count(
                connection,
                "SELECT COUNT(*) FROM public.platform_workflow_knowledge_documents",
            ),
            "embedded_knowledge_document_count": _count(
                connection,
                """
                SELECT COUNT(*)
                FROM public.platform_workflow_knowledge_documents
                WHERE embedding IS NOT NULL OR embedding_jsonb IS NOT NULL
                """,
            ),
            "secret_ref_count": len(encrypted_controls),
            "secret_ref_control_identities": control_identity_hash,
            "secret_ref_control_available": control_available,
            "secret_ref_control_decryptable": control_decryptable,
        }


class PreservationManifestStore:
    """Persist secret-free manifests below the lifecycle operation directory."""

    def __init__(self, project_root: Path) -> None:
        root = Path(project_root).resolve()
        state_root = root if root.name == ".flow-steward" else root / ".flow-steward"
        self.operations_dir = state_root / "operations"

    def save(self, manifest: PreservationManifest) -> Path:
        if not _SAFE_OPERATION_ID.fullmatch(manifest.operation_id):
            raise ValueError("Preservation operation id contains unsafe characters")
        validate_manifest(manifest)
        payload = manifest.to_dict()
        _assert_secret_free(payload)
        path = self.operations_dir / f"{manifest.operation_id}.{manifest.stage}-preservation.json"
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        _atomic_owner_only_write(path, text)
        return path

    def load(
        self,
        operation_id: str,
        stage: Literal["before", "after"],
    ) -> PreservationManifest | None:
        if not _SAFE_OPERATION_ID.fullmatch(operation_id) or stage not in {"before", "after"}:
            raise ValueError("Preservation manifest identity is invalid")
        path = self.operations_dir / f"{operation_id}.{stage}-preservation.json"
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        expected = {
            "operation_id",
            "stage",
            "database_reachable",
            "fields",
            "schema_version",
            "captured_at",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise ValueError("Preservation manifest has an invalid persisted shape")
        _assert_secret_free(payload)
        manifest = PreservationManifest(
            operation_id=str(payload["operation_id"]),
            stage=cast(Literal["before", "after"], payload["stage"]),
            database_reachable=bool(payload["database_reachable"]),
            fields=payload["fields"],
            schema_version=payload["schema_version"],
            captured_at=str(payload["captured_at"]),
        )
        validate_manifest(manifest, expected_stage=stage)
        return manifest


def stable_identity_hash(values: Iterable[Any]) -> str:
    """Hash a complete stable identity set without exposing its members."""
    canonical_items = sorted(
        {
            json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
            for value in values
        }
    )
    payload = json.dumps(canonical_items, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _rows(
    connection: Any,
    sql: str,
    params: tuple[Any, ...] = (),
) -> list[tuple[Any, ...]]:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return [_tuple_row(row) for row in (cursor.fetchall() or [])]


def _row(
    connection: Any,
    sql: str,
    params: tuple[Any, ...] = (),
) -> tuple[Any, ...] | None:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        row = cursor.fetchone()
    return None if row is None else _tuple_row(row)


def _scalar(connection: Any, sql: str, params: tuple[Any, ...] = ()) -> Any:
    row = _row(connection, sql, params)
    return None if row is None else row[0]


def _count(connection: Any, sql: str) -> int:
    value = _scalar(connection, sql)
    if value is None:
        raise ValueError("Required preservation count was unavailable")
    count = int(value)
    if count < 0:
        raise ValueError("Required preservation count was negative")
    return count


def _tuple_row(row: Any) -> tuple[Any, ...]:
    if isinstance(row, Mapping):
        return tuple(row.values())
    return tuple(row)


def _assert_secret_free(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).strip().lower()
            if normalized in _SECRET_FIELD_NAMES or normalized.endswith(
                ("_ciphertext", "_plaintext", "_ref_key")
            ):
                raise ValueError(f"Refusing to persist secret-shaped field: {key}")
            _assert_secret_free(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _assert_secret_free(nested)


def _atomic_owner_only_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    descriptor, raw_temp = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temp_path = Path(raw_temp)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        path.chmod(0o600)
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        temp_path.unlink(missing_ok=True)
        raise
