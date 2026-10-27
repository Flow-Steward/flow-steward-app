"""Runtime schema contract checks for app, control, and worker startup."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from core.infrastructure.db import psycopg_runtime
from core.infrastructure.db.schema_contract_catalog import (
    _EXPECTED_MEMBERSHIP_CONSTRAINTS,
    _EXPECTED_MEMBERSHIP_TRIGGER,
    _EXPECTED_USER_ADMIN_COLUMNS,
    _EXPECTED_USER_ADMIN_INDEXES,
    _REQUIRED_RUNTIME_INDEX_DEFINITIONS,
)
from core.infrastructure.db.schema_contract_catalog import (
    REQUIRED_RUNTIME_COLUMNS as REQUIRED_RUNTIME_COLUMNS,
)
from core.infrastructure.db.schema_contract_catalog import (
    REQUIRED_RUNTIME_INDEXES as REQUIRED_RUNTIME_INDEXES,
)
from core.infrastructure.db.schema_contract_catalog import (
    REQUIRED_RUNTIME_TABLES as REQUIRED_RUNTIME_TABLES,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


class RuntimeSchemaContractViolation(RuntimeError):
    """Technical schema mismatch that entrypoint composition maps to lifecycle policy."""


class _ConnectionSchemaInspector:
    """Expose the minimal schema-inspection surface over a raw connection."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def _table_exists(self, table_name: str) -> bool:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT pg_catalog.to_regclass(%s) IS NOT NULL",
                (f"public.{table_name}",),
            )
            row = cur.fetchone()
        return bool(_row_values(row, ("exists",))[0]) if row is not None else False

    def _column_exists(self, table_name: str, column_name: str) -> bool:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = %s
                      AND column_name = %s
                )
                """,
                (table_name, column_name),
            )
            row = cur.fetchone()
        return bool(_row_values(row, ("exists",))[0]) if row is not None else False

    def cursor(self):
        return self._conn.cursor()


def enforce_runtime_schema_contract_dsn(dsn: str, *, component: str) -> None:
    """Validate runtime schema from a read-only transaction on a raw connection."""
    normalized_dsn = str(dsn or "").strip()
    if not normalized_dsn:
        raise RuntimeSchemaContractViolation(
            f"{component} schema contract check failed: no database DSN is configured."
        )

    conn = psycopg_runtime.connect(normalized_dsn)
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
        enforce_runtime_schema_contract(
            _ConnectionSchemaInspector(conn),
            component=component,
        )
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()


def missing_required_tables(bl, required_tables: Iterable[str] | None = None) -> list[str]:
    required = tuple(required_tables or REQUIRED_RUNTIME_TABLES)
    table_exists = getattr(bl, "_table_exists", None)
    if not callable(table_exists):
        return []
    missing: list[str] = []
    for table_name in required:
        try:
            if not bool(table_exists(str(table_name))):
                missing.append(str(table_name))
        except Exception:
            missing.append(str(table_name))
    return missing


def missing_required_columns(
    bl,
    required_columns: Iterable[tuple[str, str]] | None = None,
) -> list[str]:
    required = tuple(required_columns or REQUIRED_RUNTIME_COLUMNS)
    column_exists = getattr(bl, "_column_exists", None)
    if not callable(column_exists):
        return []
    missing: list[str] = []
    for table_name, column_name in required:
        table = str(table_name)
        column = str(column_name)
        try:
            if not bool(column_exists(table, column)):
                missing.append(f"{table}.{column}")
        except Exception:
            missing.append(f"{table}.{column}")
    return missing


def missing_required_indexes(bl, required_indexes: Iterable[str] | None = None) -> list[str]:
    """Return critical indexes absent from the live public schema.

    These expression/partial indexes are required by write paths and cannot be
    inferred from table and column presence alone.
    """
    required = tuple(required_indexes or REQUIRED_RUNTIME_INDEXES)
    cursor_factory = getattr(bl, "cursor", None)
    if not callable(cursor_factory):
        return []
    try:
        with cursor_factory() as cur:
            cur.execute(
                "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' AND indexname = ANY(%s)",
                (list(required),),
            )
            rows = cur.fetchall()
    except Exception:
        return list(required)
    found = {
        str(_row_values(row, ("indexname", "indexdef"))[0]): str(
            _row_values(row, ("indexname", "indexdef"))[1]
        )
        for row in rows
    }
    missing: list[str] = []
    for name in required:
        indexdef = found.get(name, "")
        expected_fragments = _REQUIRED_RUNTIME_INDEX_DEFINITIONS.get(name, ())
        if not indexdef or any(fragment not in indexdef for fragment in expected_fragments):
            missing.append(name)
    return missing


def enforce_runtime_schema_contract(
    bl,
    *,
    component: str,
    required_tables: Iterable[str] | None = None,
    required_columns: Iterable[tuple[str, str]] | None = None,
) -> None:
    missing = missing_required_tables(bl, required_tables=required_tables)
    missing_columns = missing_required_columns(bl, required_columns=required_columns)
    missing_indexes = missing_required_indexes(bl)
    if not missing and not missing_columns and not missing_indexes:
        _enforce_migration_history(bl, component=component)
        _enforce_user_admin_relation_properties(bl, component=component)
        _enforce_user_admin_exact_catalog(bl, component=component)
        _enforce_user_admin_data_invariants(bl, component=component)
        return
    required = tuple(required_tables or REQUIRED_RUNTIME_TABLES)
    required_column_count = len(tuple(required_columns or REQUIRED_RUNTIME_COLUMNS))
    details = []
    if missing:
        details.append(f"missing required tables {missing}")
    if missing_columns:
        details.append(f"missing required columns {missing_columns}")
    if missing_indexes:
        details.append(f"missing required indexes {missing_indexes}")
    raise RuntimeSchemaContractViolation(
        f"{component} schema contract check failed: {'; '.join(details)}. "
        f"Expected baseline contains {len(required)} tables and "
        f"{required_column_count} columns from the canonical schema."
    )


def enforce_canonical_baseline_contract(
    bl,
    *,
    component: str,
    include_data_invariants: bool = True,
) -> None:
    """Prove a canonical baseline before creating migration history.

    The migration tracking table is the only allowed missing runtime object.
    Migration SQL bodies are deliberately not executed by this check.
    """
    required_tables = tuple(
        table for table in REQUIRED_RUNTIME_TABLES if table != "platform_schema_migrations"
    )
    missing = missing_required_tables(bl, required_tables=required_tables)
    missing_columns = missing_required_columns(bl)
    if missing or missing_columns:
        details = []
        if missing:
            details.append(f"missing required tables {missing}")
        if missing_columns:
            details.append(f"missing required columns {missing_columns}")
        raise RuntimeSchemaContractViolation(
            f"{component} schema contract check failed: {'; '.join(details)}. "
            "Migration history was not created because the canonical baseline "
            "could not be proven."
        )
    _enforce_user_admin_relation_properties(bl, component=component)
    _enforce_user_admin_exact_catalog(bl, component=component)
    if include_data_invariants:
        _enforce_user_admin_data_invariants(bl, component=component)


def enforce_canonical_baseline_contract_connection(
    conn,
    *,
    component: str,
    include_data_invariants: bool = True,
) -> None:
    """Validate a raw connection as a canonical baseline without mutation."""
    enforce_canonical_baseline_contract(
        _ConnectionSchemaInspector(conn),
        component=component,
        include_data_invariants=include_data_invariants,
    )


def expected_runtime_migrations(*, root: Path | None = None) -> dict[str, str]:
    """Return the exact ordered SQL migration manifest required by runtimes."""
    repo_root = Path(root) if root is not None else REPO_ROOT
    migrations_dir = repo_root / "db" / "migrations"
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(migrations_dir.glob("*.sql"), key=lambda item: item.name)
    }


def _row_values(row: object, names: tuple[str, ...]) -> tuple[Any, ...]:
    if isinstance(row, Mapping):
        return tuple(row[name] for name in names)
    return tuple(row)  # type: ignore[arg-type]


def _cursor_factory(bl):
    cursor = getattr(bl, "cursor", None)
    if not callable(cursor):
        raise RuntimeSchemaContractViolation(
            "runtime schema contract requires a read-only database cursor"
        )
    return cursor


def _enforce_migration_history(bl, *, component: str) -> None:
    expected = expected_runtime_migrations()
    with _cursor_factory(bl)() as cur:
        cur.execute(
            """
            SELECT migration_id, checksum
            FROM public.platform_schema_migrations
            ORDER BY migration_id
            """
        )
        rows = cur.fetchall() or []
    actual = {
        str(migration_id): str(checksum)
        for migration_id, checksum in (
            _row_values(row, ("migration_id", "checksum")) for row in rows
        )
    }
    if actual == expected:
        return
    missing = sorted(set(expected).difference(actual))
    unknown = sorted(set(actual).difference(expected))
    drifted = sorted(
        migration_id
        for migration_id in set(expected).intersection(actual)
        if expected[migration_id] != actual[migration_id]
    )
    raise RuntimeSchemaContractViolation(
        f"{component} schema contract check failed: migration history mismatch "
        f"(missing={missing}, drifted={drifted}, unknown={unknown}). "
        "Run flow-steward database upgrade with the matching release before startup."
    )


def _enforce_user_admin_relation_properties(bl, *, component: str) -> None:
    expected_names = {"accounts", "users", "user_account_memberships"}
    with _cursor_factory(bl)() as cur:
        cur.execute(
            """
            SELECT relation_state.relname,
                   relation_state.relkind,
                   relation_state.relpersistence,
                   relation_state.relispartition,
                   relation_state.relrowsecurity,
                   relation_state.relforcerowsecurity
            FROM pg_catalog.pg_class AS relation_state
            JOIN pg_catalog.pg_namespace AS relation_namespace
              ON relation_namespace.oid = relation_state.relnamespace
            WHERE relation_namespace.nspname = 'public'
              AND relation_state.relname = ANY(%s)
            ORDER BY relation_state.relname
            """,
            (sorted(expected_names),),
        )
        rows = cur.fetchall() or []
    invalid: list[str] = []
    seen: set[str] = set()
    for row in rows:
        name, relkind, persistence, is_partition, row_security, force_row_security = _row_values(
            row,
            (
                "relname",
                "relkind",
                "relpersistence",
                "relispartition",
                "relrowsecurity",
                "relforcerowsecurity",
            ),
        )
        relation_name = str(name)
        seen.add(relation_name)
        if (
            str(relkind) != "r"
            or str(persistence) != "p"
            or bool(is_partition)
            or bool(row_security)
            or bool(force_row_security)
        ):
            invalid.append(relation_name)
    invalid.extend(sorted(expected_names.difference(seen)))
    if invalid:
        raise RuntimeSchemaContractViolation(
            f"{component} schema contract check failed: incompatible user-admin "
            f"relation properties {sorted(set(invalid))}."
        )


def _enforce_user_admin_exact_catalog(bl, *, component: str) -> None:
    _enforce_user_admin_columns(bl, component=component)
    _enforce_membership_constraints(bl, component=component)
    _enforce_user_admin_indexes(bl, component=component)
    _enforce_membership_trigger(bl, component=component)


def _enforce_user_admin_columns(bl, *, component: str) -> None:
    with _cursor_factory(bl)() as cur:
        cur.execute(
            """
            SELECT table_relation.relname,
                   column_shape.attname,
                   pg_catalog.format_type(column_shape.atttypid, column_shape.atttypmod),
                   column_shape.attnotnull,
                   pg_catalog.pg_get_expr(
                       column_default.adbin,
                       column_default.adrelid,
                       false
                   )
            FROM pg_catalog.pg_attribute AS column_shape
            JOIN pg_catalog.pg_class AS table_relation
              ON table_relation.oid = column_shape.attrelid
            JOIN pg_catalog.pg_namespace AS table_namespace
              ON table_namespace.oid = table_relation.relnamespace
            LEFT JOIN pg_catalog.pg_attrdef AS column_default
              ON column_default.adrelid = column_shape.attrelid
             AND column_default.adnum = column_shape.attnum
            WHERE table_namespace.nspname = 'public'
              AND table_relation.relname = ANY(%s)
              AND column_shape.attnum > 0
              AND NOT column_shape.attisdropped
            ORDER BY table_relation.relname, column_shape.attnum
            """,
            (["user_account_memberships", "users"],),
        )
        rows = cur.fetchall() or []
    actual = {
        (str(table_name), str(column_name)): (
            str(data_type),
            bool(not_null),
            None if column_default is None else str(column_default),
        )
        for table_name, column_name, data_type, not_null, column_default in (
            _row_values(
                row,
                ("relname", "attname", "data_type", "attnotnull", "column_default"),
            )
            for row in rows
        )
    }
    if actual != _EXPECTED_USER_ADMIN_COLUMNS:
        raise RuntimeSchemaContractViolation(
            f"{component} schema contract check failed: exact user-admin column catalog mismatch."
        )


def _enforce_membership_constraints(bl, *, component: str) -> None:
    with _cursor_factory(bl)() as cur:
        cur.execute(
            """
            SELECT constraint_state.conname,
                   constraint_state.contype,
                   pg_catalog.pg_get_constraintdef(constraint_state.oid, false),
                   constraint_state.condeferrable,
                   constraint_state.condeferred,
                   constraint_state.convalidated
            FROM pg_catalog.pg_constraint AS constraint_state
            WHERE constraint_state.conrelid =
                  'public.user_account_memberships'::regclass
            ORDER BY constraint_state.conname
            """
        )
        rows = cur.fetchall() or []
    actual = {
        str(name): (
            str(constraint_type),
            str(definition),
            bool(deferrable),
            bool(deferred),
            bool(validated),
        )
        for name, constraint_type, definition, deferrable, deferred, validated in (
            _row_values(
                row,
                (
                    "conname",
                    "contype",
                    "definition",
                    "condeferrable",
                    "condeferred",
                    "convalidated",
                ),
            )
            for row in rows
        )
    }
    if actual != _EXPECTED_MEMBERSHIP_CONSTRAINTS:
        raise RuntimeSchemaContractViolation(
            f"{component} schema contract check failed: exact membership constraint "
            "catalog mismatch."
        )


def _enforce_user_admin_indexes(bl, *, component: str) -> None:
    with _cursor_factory(bl)() as cur:
        cur.execute(
            """
            SELECT index_relation.relname,
                   table_relation.relname,
                   access_method.amname,
                   index_state.indisunique,
                   index_state.indisprimary,
                   index_state.indisexclusion,
                   index_state.indimmediate,
                   index_state.indisvalid,
                   index_state.indisready,
                   index_state.indislive,
                   index_state.indnullsnotdistinct,
                   index_state.indnkeyatts,
                   index_state.indnatts,
                   ARRAY(
                       SELECT pg_catalog.pg_get_indexdef(
                           index_state.indexrelid,
                           key_position,
                           false
                       )
                       FROM generate_series(1, index_state.indnkeyatts) AS key_position
                       ORDER BY key_position
                   ),
                   pg_catalog.pg_get_expr(
                       index_state.indpred,
                       index_state.indrelid,
                       false
                   )
            FROM pg_catalog.pg_index AS index_state
            JOIN pg_catalog.pg_class AS index_relation
              ON index_relation.oid = index_state.indexrelid
            JOIN pg_catalog.pg_namespace AS index_namespace
              ON index_namespace.oid = index_relation.relnamespace
            JOIN pg_catalog.pg_class AS table_relation
              ON table_relation.oid = index_state.indrelid
            JOIN pg_catalog.pg_namespace AS table_namespace
              ON table_namespace.oid = table_relation.relnamespace
            JOIN pg_catalog.pg_am AS access_method
              ON access_method.oid = index_relation.relam
            WHERE index_namespace.nspname = 'public'
              AND table_namespace.nspname = 'public'
              AND index_relation.relname = ANY(%s)
              AND index_relation.relkind = 'i'
              AND index_state.indexprs IS NULL
            ORDER BY index_relation.relname
            """,
            (sorted(_EXPECTED_USER_ADMIN_INDEXES),),
        )
        rows = cur.fetchall() or []
    actual: dict[str, tuple[Any, ...]] = {}
    for row in rows:
        values = _row_values(
            row,
            (
                "index_name",
                "table_name",
                "amname",
                "indisunique",
                "indisprimary",
                "indisexclusion",
                "indimmediate",
                "indisvalid",
                "indisready",
                "indislive",
                "indnullsnotdistinct",
                "indnkeyatts",
                "indnatts",
                "key_columns",
                "predicate",
            ),
        )
        actual[str(values[0])] = (
            str(values[1]),
            str(values[2]),
            *(bool(value) for value in values[3:11]),
            int(values[11]),
            int(values[12]),
            tuple(str(value) for value in (values[13] or ())),
            None if values[14] is None else str(values[14]),
        )
    if actual != _EXPECTED_USER_ADMIN_INDEXES:
        raise RuntimeSchemaContractViolation(
            f"{component} schema contract check failed: exact user-admin index catalog mismatch."
        )


def _enforce_membership_trigger(bl, *, component: str) -> None:
    with _cursor_factory(bl)() as cur:
        cur.execute(
            """
            SELECT trigger_state.tgname,
                   table_relation.relname,
                   function_namespace.nspname,
                   trigger_function.proname,
                   trigger_state.tgtype,
                   trigger_state.tgenabled,
                   trigger_state.tgconstraint,
                   trigger_state.tgdeferrable,
                   trigger_state.tginitdeferred,
                   trigger_state.tgnargs,
                   trigger_state.tgattr::text,
                   pg_catalog.pg_get_expr(
                       trigger_state.tgqual,
                       trigger_state.tgrelid,
                       false
                   ),
                   trigger_function.pronargs,
                   pg_catalog.format_type(trigger_function.prorettype, NULL)
            FROM pg_catalog.pg_trigger AS trigger_state
            JOIN pg_catalog.pg_class AS table_relation
              ON table_relation.oid = trigger_state.tgrelid
            JOIN pg_catalog.pg_namespace AS table_namespace
              ON table_namespace.oid = table_relation.relnamespace
            JOIN pg_catalog.pg_proc AS trigger_function
              ON trigger_function.oid = trigger_state.tgfoid
            JOIN pg_catalog.pg_namespace AS function_namespace
              ON function_namespace.oid = trigger_function.pronamespace
            WHERE table_namespace.nspname = 'public'
              AND table_relation.relname = 'user_account_memberships'
              AND trigger_state.tgname = 'trg_user_account_memberships_updated'
              AND NOT trigger_state.tgisinternal
            """
        )
        rows = cur.fetchall() or []
    actual: dict[str, tuple[Any, ...]] = {}
    for row in rows:
        values = _row_values(
            row,
            (
                "tgname",
                "table_name",
                "function_schema",
                "function_name",
                "tgtype",
                "tgenabled",
                "tgconstraint",
                "tgdeferrable",
                "tginitdeferred",
                "tgnargs",
                "tgattr",
                "predicate",
                "pronargs",
                "return_type",
            ),
        )
        actual[str(values[0])] = (
            str(values[1]),
            str(values[2]),
            str(values[3]),
            int(values[4]),
            str(values[5]),
            int(values[6]),
            bool(values[7]),
            bool(values[8]),
            int(values[9]),
            str(values[10]),
            None if values[11] is None else str(values[11]),
            int(values[12]),
            str(values[13]),
        )
    expected = {"trg_user_account_memberships_updated": _EXPECTED_MEMBERSHIP_TRIGGER}
    if actual != expected:
        raise RuntimeSchemaContractViolation(
            f"{component} schema contract check failed: exact membership trigger catalog mismatch."
        )


def _enforce_user_admin_data_invariants(bl, *, component: str) -> None:
    with _cursor_factory(bl)() as cur:
        cur.execute(
            """
            SELECT
                EXISTS (
                    SELECT 1
                    FROM public.users AS u
                    LEFT JOIN public.accounts AS user_account ON user_account.id = u.account_id
                    WHERE user_account.id IS NULL
                    UNION ALL
                    SELECT 1
                    FROM public.user_account_memberships AS membership
                    LEFT JOIN public.accounts AS member_account
                      ON member_account.id = membership.account_id
                    WHERE member_account.id IS NULL
                ) AS invalid_account_references,
                EXISTS (
                    SELECT 1
                    FROM public.user_account_memberships AS membership
                    LEFT JOIN public.users AS member_user
                      ON member_user.id = membership.user_id
                    WHERE member_user.id IS NULL
                ) AS invalid_user_references,
                EXISTS (
                    SELECT 1
                    FROM public.users AS u
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM public.user_account_memberships AS membership
                        WHERE membership.user_id = u.id
                    )
                ) AS user_without_membership,
                EXISTS (
                    SELECT u.id
                    FROM public.users AS u
                    LEFT JOIN public.user_account_memberships AS default_membership
                      ON default_membership.user_id = u.id
                     AND default_membership.is_default
                    GROUP BY u.id
                    HAVING count(default_membership.id) <> 1
                ) AS invalid_default_membership_count,
                EXISTS (
                    SELECT 1
                    FROM public.users AS u
                    JOIN public.user_account_memberships AS default_membership
                      ON default_membership.user_id = u.id
                     AND default_membership.is_default
                    WHERE u.account_id IS DISTINCT FROM default_membership.account_id
                ) AS account_default_mismatch
            """
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeSchemaContractViolation(
            f"{component} schema contract check failed: invariant query failed"
        )
    (
        invalid_account,
        invalid_user,
        without_membership,
        invalid_default,
        account_mismatch,
    ) = _row_values(
        row,
        (
            "invalid_account_references",
            "invalid_user_references",
            "user_without_membership",
            "invalid_default_membership_count",
            "account_default_mismatch",
        ),
    )
    if bool(invalid_account):
        raise RuntimeSchemaContractViolation(
            f"{component} schema contract check failed: invalid account reference."
        )
    if bool(invalid_user):
        raise RuntimeSchemaContractViolation(
            f"{component} schema contract check failed: invalid membership user reference."
        )
    if bool(without_membership):
        raise RuntimeSchemaContractViolation(
            f"{component} schema contract check failed: user without membership."
        )
    if bool(invalid_default):
        raise RuntimeSchemaContractViolation(
            f"{component} schema contract check failed: invalid default membership count."
        )
    if bool(account_mismatch):
        raise RuntimeSchemaContractViolation(
            f"{component} schema contract check failed: users.account_id does not match "
            "the default membership."
        )
