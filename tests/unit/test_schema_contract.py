from __future__ import annotations

import sys
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def test_monitoring_tables_are_required_by_the_runtime_schema_contract() -> None:
    from core.infrastructure.db.schema_contract import (
        REQUIRED_RUNTIME_TABLES,
        missing_required_tables,
    )

    monitoring_tables = {"fs_runtime_status_current", "fs_runtime_status_history"}
    existing = set(REQUIRED_RUNTIME_TABLES) - monitoring_tables
    assert missing_required_tables(_FakeBL(existing)) == [
        "fs_runtime_status_current",
        "fs_runtime_status_history",
    ]


def test_entrypoint_composition_maps_schema_contract_violation_to_lifecycle_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.application.lifecycle.errors import DatabaseSchemaRecoveryRequiredError
    from core.infrastructure.db.schema_contract import RuntimeSchemaContractViolation
    from core.infrastructure.runtime import entrypoint_db_adapters

    def fail_contract(_dsn: str, *, component: str) -> None:
        raise RuntimeSchemaContractViolation(f"{component} schema mismatch")

    monkeypatch.setattr(
        entrypoint_db_adapters,
        "enforce_runtime_schema_contract_dsn",
        fail_contract,
    )

    with pytest.raises(DatabaseSchemaRecoveryRequiredError, match="schema mismatch"):
        entrypoint_db_adapters.enforce_runtime_schema("postgresql://example", component="cli")


class _FakeBL:
    def __init__(
        self,
        existing: set[str],
        existing_columns: set[tuple[str, str]] | None = None,
    ) -> None:
        self._existing = set(existing)
        self._existing_columns = set(existing_columns or set())

    def _table_exists(self, table_name: str) -> bool:
        return table_name in self._existing

    def _column_exists(self, table_name: str, column_name: str) -> bool:
        return (table_name, column_name) in self._existing_columns

    def cursor(self):
        from core.infrastructure.db.schema_contract import expected_runtime_migrations

        return _ContractCursor(applied_migrations=list(expected_runtime_migrations().items()))


class _ContractCursor(AbstractContextManager):
    def __init__(
        self,
        *,
        applied_migrations: list[tuple[str, str]],
        relation_rows: list[tuple[str, str, str, bool, bool, bool]] | None = None,
        column_rows: list[tuple[str, str, str, bool, str | None]] | None = None,
        constraint_rows: list[tuple[str, str, str, bool, bool, bool]] | None = None,
        index_rows: list[tuple[Any, ...]] | None = None,
        trigger_rows: list[tuple[Any, ...]] | None = None,
        runtime_index_rows: list[tuple[str, str]] | None = None,
        invariant_row: tuple[bool, bool, bool, bool, bool] = (
            False,
            False,
            False,
            False,
            False,
        ),
    ) -> None:
        self.applied_migrations = applied_migrations
        self.relation_rows = relation_rows or [
            ("accounts", "r", "p", False, False, False),
            ("users", "r", "p", False, False, False),
            ("user_account_memberships", "r", "p", False, False, False),
        ]
        self.column_rows = column_rows or _canonical_column_rows()
        self.constraint_rows = constraint_rows or _canonical_constraint_rows()
        self.index_rows = index_rows or _canonical_index_rows()
        self.trigger_rows = trigger_rows or _canonical_trigger_rows()
        self.runtime_index_rows = runtime_index_rows or _canonical_runtime_index_rows()
        self.invariant_row = invariant_row
        self.statements: list[str] = []
        self._rows: list[Any] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: str, _params: object = None) -> None:
        normalized = " ".join(str(sql).split())
        self.statements.append(normalized)
        if "FROM public.platform_schema_migrations" in normalized:
            self._rows = list(self.applied_migrations)
        elif "relation_state.relkind" in normalized:
            self._rows = list(self.relation_rows)
        elif "column_shape.attname" in normalized:
            self._rows = list(self.column_rows)
        elif "constraint_state.contype" in normalized:
            self._rows = list(self.constraint_rows)
        elif "index_state.indisunique" in normalized:
            self._rows = list(self.index_rows)
        elif "FROM pg_indexes" in normalized:
            self._rows = list(self.runtime_index_rows)
        elif "trigger_state.tgtype" in normalized:
            self._rows = list(self.trigger_rows)
        elif "invalid_account_references" in normalized:
            self._rows = [self.invariant_row]
        else:
            raise AssertionError(f"unexpected runtime schema query: {normalized}")

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


def _canonical_column_rows() -> list[tuple[str, str, str, bool, str | None]]:
    return [
        ("users", "id", "uuid", True, "gen_random_uuid()"),
        ("users", "username", "text", True, None),
        ("users", "password_hash", "text", True, None),
        ("users", "display_name", "text", True, "''::text"),
        ("users", "is_active", "boolean", True, "true"),
        ("users", "created_at", "timestamp with time zone", True, "now()"),
        ("users", "updated_at", "timestamp with time zone", True, "now()"),
        ("users", "must_change_password", "boolean", True, "false"),
        ("users", "deleted_at", "timestamp with time zone", False, None),
        ("users", "deleted_by", "text", False, None),
        ("users", "timezone", "character varying(64)", False, None),
        ("users", "locale", "character varying(16)", False, None),
        (
            "users",
            "account_id",
            "uuid",
            True,
            "'00000000-0000-0000-0000-000000000002'::uuid",
        ),
        ("user_account_memberships", "id", "uuid", True, "gen_random_uuid()"),
        ("user_account_memberships", "user_id", "uuid", True, None),
        ("user_account_memberships", "account_id", "uuid", True, None),
        ("user_account_memberships", "is_default", "boolean", True, "false"),
        (
            "user_account_memberships",
            "created_at",
            "timestamp with time zone",
            True,
            "now()",
        ),
        (
            "user_account_memberships",
            "updated_at",
            "timestamp with time zone",
            True,
            "now()",
        ),
    ]


def _canonical_constraint_rows() -> list[tuple[str, str, str, bool, bool, bool]]:
    return [
        ("user_account_memberships_pkey", "p", "PRIMARY KEY (id)", False, False, True),
        (
            "user_account_memberships_user_id_account_id_key",
            "u",
            "UNIQUE (user_id, account_id)",
            False,
            False,
            True,
        ),
        (
            "user_account_memberships_user_id_fkey",
            "f",
            "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE",
            False,
            False,
            True,
        ),
        (
            "user_account_memberships_account_id_fkey",
            "f",
            "FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT",
            False,
            False,
            True,
        ),
    ]


def _canonical_index_rows() -> list[tuple[Any, ...]]:
    common = ("btree", False, False, False, True, True, True, True, False)
    return [
        ("idx_users_account_id", "users", *common, 1, 1, ("account_id",), None),
        ("idx_users_active", "users", *common, 1, 1, ("id",), "(deleted_at IS NULL)"),
        (
            "idx_users_timezone",
            "users",
            *common,
            1,
            1,
            ("timezone",),
            "(timezone IS NOT NULL)",
        ),
        (
            "idx_user_account_memberships_account",
            "user_account_memberships",
            *common,
            2,
            2,
            ("account_id", "user_id"),
            None,
        ),
        (
            "idx_user_account_memberships_default",
            "user_account_memberships",
            "btree",
            True,
            False,
            False,
            True,
            True,
            True,
            True,
            False,
            1,
            1,
            ("user_id",),
            "(is_default = true)",
        ),
    ]


def _canonical_runtime_index_rows() -> list[tuple[str, str]]:
    return [
        (
            "ux_extension_project_state_install_scope_key",
            "CREATE UNIQUE INDEX ux_extension_project_state_install_scope_key ON public.extension_project_state USING btree (account_id, project_id, extension_id, COALESCE(extension_install_id, ''::text), namespace, state_key, COALESCE(connection_id, ''::text))",
        ),
        (
            "idx_extension_oauth_configurations_scope",
            "CREATE INDEX idx_extension_oauth_configurations_scope ON public.extension_oauth_configurations USING btree (tenant_id, account_id, project_id, extension_install_id, extension_id, provider_id, created_at, configuration_id)",
        ),
        (
            "idx_extension_oauth_configurations_project_scope",
            "CREATE INDEX idx_extension_oauth_configurations_project_scope ON public.extension_oauth_configurations USING btree (project_id, tenant_id, account_id)",
        ),
        (
            "idx_extension_oauth_configurations_install",
            "CREATE INDEX idx_extension_oauth_configurations_install ON public.extension_oauth_configurations USING btree (extension_install_id, account_id, extension_id)",
        ),
        (
            "idx_extension_oauth_configurations_release",
            "CREATE INDEX idx_extension_oauth_configurations_release ON public.extension_oauth_configurations USING btree (extension_release_id, extension_id)",
        ),
        (
            "idx_extension_oauth_authorization_states_scope",
            "CREATE INDEX idx_extension_oauth_authorization_states_scope ON public.extension_oauth_authorization_states USING btree (tenant_id, account_id, project_id, extension_install_id, extension_id, provider_id, configuration_id, state_hash)",
        ),
        (
            "idx_extension_oauth_authorization_states_configuration_scope",
            "CREATE INDEX idx_extension_oauth_authorization_states_configuration_scope ON public.extension_oauth_authorization_states USING btree (configuration_id, tenant_id, account_id, project_id, extension_install_id, extension_id, provider_id)",
        ),
        (
            "idx_extension_oauth_authorization_states_release",
            "CREATE INDEX idx_extension_oauth_authorization_states_release ON public.extension_oauth_authorization_states USING btree (extension_release_id, extension_id)",
        ),
        (
            "idx_extension_oauth_authorization_states_expiry",
            "CREATE INDEX idx_extension_oauth_authorization_states_expiry ON public.extension_oauth_authorization_states USING btree (expires_at) WHERE (consumed_at IS NULL)",
        ),
        (
            "idx_extension_oauth_authorization_states_existing_connection",
            "CREATE INDEX idx_extension_oauth_authorization_states_existing_connection ON public.extension_oauth_authorization_states USING btree (existing_connection_id, configuration_id) WHERE (existing_connection_id IS NOT NULL)",
        ),
        (
            "idx_project_connections_oauth_configuration",
            "CREATE INDEX idx_project_connections_oauth_configuration ON public.project_connections USING btree (oauth_configuration_id, account_id, project_id, extension_install_id, extension_id, oauth_provider_id) WHERE (oauth_configuration_id IS NOT NULL)",
        ),
        (
            "idx_project_connections_oauth_release",
            "CREATE INDEX idx_project_connections_oauth_release ON public.project_connections USING btree (oauth_extension_release_id, extension_id) WHERE (oauth_configuration_id IS NOT NULL)",
        ),
        (
            "idx_mcp_connector_oauth_states_connector",
            "CREATE INDEX idx_mcp_connector_oauth_states_connector ON public.mcp_connector_oauth_states USING btree (connector_id, expires_at)",
        ),
        (
            "idx_mcp_connector_oauth_states_expiry",
            "CREATE INDEX idx_mcp_connector_oauth_states_expiry ON public.mcp_connector_oauth_states USING btree (expires_at) WHERE (consumed_at IS NULL)",
        ),
    ]


def _canonical_trigger_rows() -> list[tuple[Any, ...]]:
    return [
        (
            "trg_user_account_memberships_updated",
            "user_account_memberships",
            "public",
            "registry_update_timestamp",
            19,
            "O",
            0,
            False,
            False,
            0,
            "",
            None,
            0,
            "trigger",
        )
    ]


class _ContractBL(_FakeBL):
    def __init__(self, cursor: _ContractCursor) -> None:
        from core.infrastructure.db.schema_contract import (
            REQUIRED_RUNTIME_COLUMNS,
            REQUIRED_RUNTIME_TABLES,
        )

        super().__init__(set(REQUIRED_RUNTIME_TABLES), set(REQUIRED_RUNTIME_COLUMNS))
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def test_enforce_runtime_schema_contract_passes_when_all_tables_exist():
    from core.infrastructure.db.schema_contract import (
        REQUIRED_RUNTIME_COLUMNS,
        REQUIRED_RUNTIME_TABLES,
        enforce_runtime_schema_contract,
    )

    bl = _FakeBL(set(REQUIRED_RUNTIME_TABLES), set(REQUIRED_RUNTIME_COLUMNS))
    enforce_runtime_schema_contract(bl, component="test")


def test_enforce_runtime_schema_contract_raises_with_missing_tables():
    from core.infrastructure.db.schema_contract import (
        REQUIRED_RUNTIME_TABLES,
        enforce_runtime_schema_contract,
    )

    existing = set(REQUIRED_RUNTIME_TABLES[:-2])
    bl = _FakeBL(existing)

    with pytest.raises(RuntimeError, match="missing required tables"):
        enforce_runtime_schema_contract(bl, component="test")


def test_canonical_baseline_contract_allows_only_missing_migration_tracking_table():
    from core.infrastructure.db.schema_contract import (
        REQUIRED_RUNTIME_COLUMNS,
        REQUIRED_RUNTIME_TABLES,
        enforce_canonical_baseline_contract,
    )

    existing = set(REQUIRED_RUNTIME_TABLES) - {"platform_schema_migrations"}
    cursor = _ContractCursor(applied_migrations=[])
    bl = _ContractBL(cursor)
    bl._existing = existing
    bl._existing_columns = set(REQUIRED_RUNTIME_COLUMNS)

    enforce_canonical_baseline_contract(bl, component="database recovery")

    assert not any("platform_schema_migrations" in statement for statement in cursor.statements)


def test_canonical_baseline_contract_rejects_any_other_missing_table():
    from core.infrastructure.db.schema_contract import (
        REQUIRED_RUNTIME_COLUMNS,
        REQUIRED_RUNTIME_TABLES,
        enforce_canonical_baseline_contract,
    )

    existing = set(REQUIRED_RUNTIME_TABLES) - {
        "platform_schema_migrations",
        "users",
    }
    bl = _FakeBL(existing, set(REQUIRED_RUNTIME_COLUMNS))

    with pytest.raises(RuntimeError, match="users"):
        enforce_canonical_baseline_contract(bl, component="database recovery")


def test_enforce_runtime_schema_contract_raises_with_missing_columns():
    from core.infrastructure.db.schema_contract import (
        REQUIRED_RUNTIME_COLUMNS,
        REQUIRED_RUNTIME_TABLES,
        enforce_runtime_schema_contract,
    )

    existing_columns = set(REQUIRED_RUNTIME_COLUMNS)
    existing_columns.remove(("project_connections", "extension_install_id"))
    bl = _FakeBL(set(REQUIRED_RUNTIME_TABLES), existing_columns)

    with pytest.raises(RuntimeError, match=r"project_connections\.extension_install_id"):
        enforce_runtime_schema_contract(bl, component="test")


def test_startup_schema_contract_never_attempts_ddl_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.infrastructure.db import schema_contract

    calls: list[str] = []
    monkeypatch.setattr(
        schema_contract,
        "_attempt_schema_reconcile",
        lambda _component: calls.append("reconcile"),
        raising=False,
    )
    bl = _FakeBL(existing=set(), existing_columns=set())

    with pytest.raises(RuntimeError, match="missing required tables"):
        schema_contract.enforce_runtime_schema_contract(bl, component="test")

    assert calls == []


def test_schema_contract_source_contains_no_startup_ddl_reconcile() -> None:
    root = Path(__file__).resolve().parents[2]
    source = (root / "core/infrastructure/db/schema_contract.py").read_text(encoding="utf-8")
    startup_sources = "\n".join(
        (root / relative).read_text(encoding="utf-8")
        for relative in (
            "core/infrastructure/runtime/app_lifecycle_runtime.py",
            "core/entrypoints/cli/control_process.py",
            "core/infrastructure/runtime/worker/worker_schema_compat.py",
        )
    )

    assert "FS_SCHEMA_AUTO_RECONCILE" not in source
    assert "reconcile_canonical_schema" not in source
    assert "reconcile_and_enforce_runtime_schema_contract" not in startup_sources
    assert "FS_ENFORCE_SCHEMA_COMPAT" not in startup_sources


def test_dsn_schema_preflight_uses_read_only_transaction_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.infrastructure.db import schema_contract

    events: list[str] = []

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql: str, _params: object = None) -> None:
            assert "SET TRANSACTION READ ONLY" in sql
            events.append("read-only")

    class _Connection:
        autocommit = True

        def cursor(self):
            return _Cursor()

        def rollback(self) -> None:
            events.append("rollback")

        def close(self) -> None:
            events.append("close")

    monkeypatch.setattr(
        schema_contract.psycopg_runtime,
        "connect",
        lambda dsn: events.append(f"connect:{dsn}") or _Connection(),
        raising=False,
    )
    monkeypatch.setattr(
        schema_contract,
        "enforce_runtime_schema_contract",
        lambda _inspector, *, component: events.append(f"enforce:{component}"),
    )

    schema_contract.enforce_runtime_schema_contract_dsn(
        "postgresql://example",
        component="serve",
    )

    assert events == [
        "connect:postgresql://example",
        "read-only",
        "enforce:serve",
        "rollback",
        "close",
    ]


def test_expected_runtime_migration_history_covers_every_sql_file() -> None:
    from core.infrastructure.db.schema_contract import expected_runtime_migrations

    root = Path(__file__).resolve().parents[2]
    expected = expected_runtime_migrations(root=root)

    assert tuple(expected) == tuple(
        path.name for path in sorted((root / "db/migrations").glob("*.sql"))
    )
    assert all(len(checksum) == 64 for checksum in expected.values())


def test_platform_action_log_project_fk_cascades_with_project_hard_delete() -> None:
    root = Path(__file__).resolve().parents[2]
    schema = (root / "db/schema/001_schema_part_09.sql").read_text(encoding="utf-8")

    expected_fk = "FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE"
    assert expected_fk in " ".join(schema.split())


@pytest.mark.parametrize("history_kind", ["missing", "drifted", "unknown"])
def test_runtime_schema_contract_rejects_migration_history_mismatch(
    history_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.infrastructure.db import schema_contract
    from core.infrastructure.db.schema_contract import enforce_runtime_schema_contract

    expected = {"20990101000000_probe.sql": "a" * 64}
    monkeypatch.setattr(
        schema_contract, "expected_runtime_migrations", lambda **_kwargs: dict(expected)
    )
    rows = list(expected.items())
    if history_kind == "missing":
        rows.pop()
    elif history_kind == "drifted":
        rows[-1] = (rows[-1][0], "0" * 64)
    else:
        rows.append(("20990101000000_unknown.sql", "f" * 64))
    bl = _ContractBL(_ContractCursor(applied_migrations=rows))

    with pytest.raises(RuntimeError, match="migration history"):
        enforce_runtime_schema_contract(bl, component="test")


@pytest.mark.parametrize(
    ("invariants", "message"),
    [
        ((True, False, False, False, False), "account reference"),
        ((False, True, False, False, False), "user reference"),
        ((False, False, True, False, False), "without membership"),
        ((False, False, False, True, False), "default membership"),
        ((False, False, False, False, True), "does not match"),
    ],
)
def test_runtime_schema_contract_rejects_user_membership_data_drift(
    invariants: tuple[bool, bool, bool, bool, bool],
    message: str,
) -> None:
    from core.infrastructure.db.schema_contract import (
        enforce_runtime_schema_contract,
        expected_runtime_migrations,
    )

    cursor = _ContractCursor(
        applied_migrations=list(expected_runtime_migrations().items()),
        invariant_row=invariants,
    )

    _raises_input_574_1 = _ContractBL(cursor)
    with pytest.raises(RuntimeError, match=message):
        enforce_runtime_schema_contract(_raises_input_574_1, component="test")

    assert cursor.statements
    assert all(
        not any(
            statement.lstrip().upper().startswith(keyword)
            for keyword in ("ALTER ", "CREATE ", "DELETE ", "DROP ", "INSERT ", "UPDATE ")
        )
        for statement in cursor.statements
    )


@pytest.mark.parametrize("catalog_kind", ["columns", "constraints", "indexes", "trigger"])
def test_runtime_schema_contract_rejects_exact_user_admin_catalog_drift(
    catalog_kind: str,
) -> None:
    from core.infrastructure.db.schema_contract import (
        enforce_runtime_schema_contract,
        expected_runtime_migrations,
    )

    kwargs: dict[str, Any] = {}
    if catalog_kind == "columns":
        kwargs["column_rows"] = _canonical_column_rows()[:-1]
    elif catalog_kind == "constraints":
        rows = _canonical_constraint_rows()
        rows[-1] = (*rows[-1][:-1], False)
        kwargs["constraint_rows"] = rows
    elif catalog_kind == "indexes":
        rows = _canonical_index_rows()
        changed = list(rows[0])
        changed[7] = False
        rows[0] = tuple(changed)
        kwargs["index_rows"] = rows
    else:
        rows = _canonical_trigger_rows()
        rows[0] = (*rows[0][:5], "D", *rows[0][6:])
        kwargs["trigger_rows"] = rows
    cursor = _ContractCursor(
        applied_migrations=list(expected_runtime_migrations().items()),
        **kwargs,
    )

    _raises_input_618_1 = _ContractBL(cursor)
    with pytest.raises(RuntimeError, match="catalog"):
        enforce_runtime_schema_contract(_raises_input_618_1, component="test")


def test_runtime_schema_contract_rejects_noncanonical_relation_properties() -> None:
    from core.infrastructure.db.schema_contract import (
        enforce_runtime_schema_contract,
        expected_runtime_migrations,
    )

    cursor = _ContractCursor(
        applied_migrations=list(expected_runtime_migrations().items()),
        relation_rows=[
            ("accounts", "r", "p", False, False, False),
            ("users", "r", "p", False, False, False),
            ("user_account_memberships", "r", "u", False, False, False),
        ],
    )

    _raises_input_637_1 = _ContractBL(cursor)
    with pytest.raises(RuntimeError, match="relation properties"):
        enforce_runtime_schema_contract(_raises_input_637_1, component="test")


def test_required_runtime_schema_includes_extension_install_tables_and_columns():
    from core.infrastructure.db.schema_contract import (
        REQUIRED_RUNTIME_COLUMNS,
        REQUIRED_RUNTIME_TABLES,
    )

    assert "extension_installs" in REQUIRED_RUNTIME_TABLES
    assert "extension_releases" in REQUIRED_RUNTIME_TABLES
    assert "extension_registry_projections" in REQUIRED_RUNTIME_TABLES
    assert "extension_entitlements" in REQUIRED_RUNTIME_TABLES
    assert "extension_usage_events" in REQUIRED_RUNTIME_TABLES
    assert "generated_extension_jobs" in REQUIRED_RUNTIME_TABLES
    assert ("project_connections", "extension_install_id") in REQUIRED_RUNTIME_COLUMNS
    required_install_columns = {
        "extension_id",
        "version",
        "account_id",
        "install_id",
        "release_id",
        "descriptor_schema_version",
        "descriptor_origin",
        "descriptor_source",
        "provenance_jsonb",
        "runtime_mode",
        "remote_base_url",
        "handshake_status",
        "health_status",
        "runtime_status",
        "last_handshake_at",
        "last_health_at",
        "last_error",
        "visibility",
        "state",
        "installed_by",
        "installed_at",
        "config_jsonb",
    }
    actual_install_columns = {
        column for table, column in REQUIRED_RUNTIME_COLUMNS if table == "extension_installs"
    }
    assert required_install_columns <= actual_install_columns


def test_required_runtime_tables_include_project_secrets():
    from core.infrastructure.db.schema_contract import REQUIRED_RUNTIME_TABLES

    assert "project_secrets" in REQUIRED_RUNTIME_TABLES


def test_required_runtime_columns_require_project_bound_workflow_entrypoints():
    from core.infrastructure.db.schema_contract import REQUIRED_RUNTIME_COLUMNS

    assert ("workflow_entrypoints", "project_id") in REQUIRED_RUNTIME_COLUMNS


def test_required_runtime_tables_include_notification_tables():
    from core.infrastructure.db.schema_contract import REQUIRED_RUNTIME_TABLES

    assert "notification_routes" in REQUIRED_RUNTIME_TABLES
    assert "notification_delivery_logs" in REQUIRED_RUNTIME_TABLES


def test_required_runtime_columns_include_structured_extension_trace_identity() -> None:
    from core.infrastructure.db.schema_contract import REQUIRED_RUNTIME_COLUMNS

    assert {
        ("extension_invocation_logs", "job_id"),
        ("extension_invocation_logs", "workflow_id"),
        ("extension_invocation_logs", "group_id"),
        ("extension_invocation_logs", "step_id"),
        ("extension_invocation_logs", "step_execution_id"),
    } <= set(REQUIRED_RUNTIME_COLUMNS)


def test_required_runtime_schema_includes_external_effect_output_reconciliation_shape():
    from core.infrastructure.db.schema_contract import (
        REQUIRED_RUNTIME_COLUMNS,
        REQUIRED_RUNTIME_TABLES,
    )

    assert "external_effect_output_reconciliations" in REQUIRED_RUNTIME_TABLES
    assert {
        ("external_effect_output_reconciliations", "effect_id"),
        ("external_effect_output_reconciliations", "account_id"),
        ("external_effect_output_reconciliations", "claim_token"),
        ("external_effect_output_reconciliations", "claim_version"),
        ("external_effect_output_reconciliations", "minted_artifact_ids_jsonb"),
        ("external_effect_output_reconciliations", "minted_dataset_ids_jsonb"),
        ("external_effect_output_reconciliations", "lifecycle_state"),
        ("external_effect_output_reconciliations", "expires_at"),
    } <= set(REQUIRED_RUNTIME_COLUMNS)


def test_required_runtime_tables_require_extension_callback_registrations():
    from core.infrastructure.db.schema_contract import REQUIRED_RUNTIME_TABLES

    assert "extension_callback_registrations" in REQUIRED_RUNTIME_TABLES


def test_required_runtime_tables_include_auth_password_resets():
    from core.infrastructure.db.schema_contract import REQUIRED_RUNTIME_TABLES

    assert "auth_password_resets" in REQUIRED_RUNTIME_TABLES


def test_required_runtime_schema_includes_canonical_user_admin_shape() -> None:
    from core.infrastructure.db.schema_contract import (
        REQUIRED_RUNTIME_COLUMNS,
        REQUIRED_RUNTIME_TABLES,
    )

    assert "accounts" in REQUIRED_RUNTIME_TABLES
    assert "user_account_memberships" in REQUIRED_RUNTIME_TABLES
    assert {
        ("users", "must_change_password"),
        ("users", "deleted_at"),
        ("users", "deleted_by"),
        ("users", "timezone"),
        ("users", "locale"),
        ("users", "account_id"),
    } <= set(REQUIRED_RUNTIME_COLUMNS)


def test_required_runtime_tables_include_scheduled_job_tables():
    from core.infrastructure.db.schema_contract import REQUIRED_RUNTIME_TABLES

    assert "scheduled_jobs" in REQUIRED_RUNTIME_TABLES
    assert "scheduled_job_runs" in REQUIRED_RUNTIME_TABLES
    assert "scheduled_job_execution_locks" in REQUIRED_RUNTIME_TABLES


def test_canonical_schema_persists_workflow_revision_and_schedule_admission() -> None:
    schema = (
        Path(__file__)
        .resolve()
        .parents[2]
        .joinpath("db/schema/001_schema_part_04.sql")
        .read_text(encoding="utf-8")
    )
    workflow_table = schema.split("CREATE TABLE public.registry_workflows (", 1)[1].split(");", 1)[
        0
    ]
    schedule_table = schema.split("CREATE TABLE public.scheduled_jobs (", 1)[1].split(");", 1)[0]

    assert "revision integer DEFAULT 1 NOT NULL" in workflow_table
    assert "admission_allowed boolean DEFAULT false NOT NULL" in schedule_table
    assert "admission_reason text DEFAULT 'not_evaluated'::text NOT NULL" in schedule_table
    assert "admission_workflow_revision integer" in schedule_table
    assert "admission_checked_at timestamp with time zone" in schedule_table


def test_required_runtime_tables_include_project_execution_slots():
    from core.infrastructure.db.schema_contract import REQUIRED_RUNTIME_TABLES

    assert "project_execution_slots" in REQUIRED_RUNTIME_TABLES
    assert "project_execution_leases" not in REQUIRED_RUNTIME_TABLES


def test_terminal_curation_outbox_event_identity_is_unique_in_canonical_schema() -> None:
    root = Path(__file__).resolve().parents[2]
    constraints = (root / "db/schema/001_schema_part_05.sql").read_text(encoding="utf-8")

    assert "domain_event_outbox_event_id_key UNIQUE (event_id)" in constraints


def test_domain_event_outbox_has_owner_token_version_lease_fencing() -> None:
    root = Path(__file__).resolve().parents[2]
    schema = (root / "db/schema/001_schema_part_01.sql").read_text(encoding="utf-8")

    assert "claim_owner text" in schema
    assert "claim_token text" in schema
    assert "claim_version bigint DEFAULT 0 NOT NULL" in schema
    assert "claim_expires_at timestamp with time zone" in schema
    assert "domain_event_outbox_claim_version_check" in schema


def test_baseline_retires_management_chat_storage_only() -> None:
    from pathlib import Path

    schema = "\n".join(path.read_text() for path in Path("db/schema").glob("*.sql"))
    for table in (
        "chat_sessions",
        "chat_messages",
        "chat_session_summaries",
        "ai_control_usage_events",
    ):
        assert f"public.{table}" not in schema
    for table in ("jobs", "platform_agent_messages", "report_runs"):
        assert f"CREATE TABLE public.{table} " in schema
    assert "thread_id" in schema


def test_snapshot_heads_and_receipts_are_required_by_runtime_schema_contract() -> None:
    from core.infrastructure.db.schema_contract import (
        REQUIRED_RUNTIME_COLUMNS,
        REQUIRED_RUNTIME_TABLES,
    )

    assert {"fs_tabular_snapshot_heads", "fs_tabular_snapshot_commits"} <= set(
        REQUIRED_RUNTIME_TABLES
    )
    assert {
        (table, column)
        for table, columns in {
            "fs_tabular_snapshot_heads": (
                "account_id",
                "project_id",
                "workflow_id",
                "profile_key",
                "revision",
                "snapshot_artifact_id",
                "metadata_jsonb",
            ),
            "fs_tabular_snapshot_commits": (
                "account_id",
                "project_id",
                "workflow_id",
                "profile_key",
                "candidate_artifact_id",
                "revision",
                "previous_snapshot_artifact_id",
                "metadata_jsonb",
            ),
        }.items()
        for column in columns
    } <= set(REQUIRED_RUNTIME_COLUMNS)
