"""Database contract for provider-neutral, extension-owned OAuth persistence."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
import yaml
from psycopg.errors import CheckViolation, ForeignKeyViolation, LockNotAvailable, UniqueViolation

from core.application.extensions.oauth_configuration import (
    OAuthConfigurationSave,
    OAuthConfigurationService,
)
from core.application.extensions.oauth_configuration_secrets import (
    configuration_secret_ref_key,
)
from core.application.extensions.oauth_connection_lifecycle import (
    OAuthConnectionLifecycleService,
)
from core.application.extensions.oauth_contract import (
    compile_oauth_providers,
    provider_revocation_fingerprint,
    provider_security_fingerprint,
)
from core.application.extensions.oauth_release_upgrade import OAuthReleaseUpgradeService
from core.application.extensions.oauth_release_upgrade_repository import (
    PostgresOAuthReleaseUpgradeRepository,
)
from core.application.extensions.oauth_repository import ExtensionOAuthRepository
from core.application.management.errors import ServicePreconditionError
from core.application.management.project_connection import ProjectConnection
from core.application.management.project_connection_secrets import connection_secret_ref_key
from core.application.management.scope import ScopeContext
from core.infrastructure.db import project_connection_store
from core.infrastructure.db.logger_extensions_accounts_callbacks_mixin import (
    PlatformLoggerExtensionsAccountsCallbacksMixin,
)
from tests import _disposable_db
from tests._db_env import resolve_test_db_dsn

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PARTS = tuple(
    REPO_ROOT / f"db/schema/001_schema_part_{part}.sql" for part in ("02", "06", "08", "09", "10")
)

TENANT_A = UUID("10000000-0000-0000-0000-000000000001")
TENANT_B = UUID("20000000-0000-0000-0000-000000000001")
ACCOUNT_A = UUID("10000000-0000-0000-0000-000000000002")
ACCOUNT_B = UUID("20000000-0000-0000-0000-000000000002")
WORKSPACE_A = UUID("10000000-0000-0000-0000-000000000003")
WORKSPACE_B = UUID("20000000-0000-0000-0000-000000000003")
PROJECT_A = UUID("10000000-0000-0000-0000-000000000004")
PROJECT_B = UUID("20000000-0000-0000-0000-000000000004")
INSTALL_A = "install_oauth_a"
INSTALL_B = "install_oauth_b"
RELEASE_A = "release_oauth_a"
RELEASE_B = "release_oauth_b"
RELEASE_A_NEXT = "release_oauth_a_next"
PROVIDER_ID = "fixture_oauth"


def _lifecycle_manifest() -> dict[str, Any]:
    manifest = yaml.safe_load(
        (REPO_ROOT / "tests/fixtures/extensions/fixture_extension_oauth/extension.yaml").read_text(
            encoding="utf-8"
        )
    )
    manifest["action_definitions"] = [
        {"action_id": "verify_identity", "handler": {"mode": "extension"}}
    ]
    return manifest


class _MemoryConfigurationSecrets:
    def __init__(self, values: dict[tuple[str, str], str]) -> None:
        self.values = values
        self.writes: list[tuple[str, str, str]] = []
        self.deletes: list[tuple[str, str]] = []

    def present(self, *, account_id: str, ref_key: str) -> bool:
        return (account_id, ref_key) in self.values

    def write(self, *, account_id: str, ref_key: str, value: str) -> None:
        self.values[(account_id, ref_key)] = value
        self.writes.append((account_id, ref_key, value))

    def delete(self, *, account_id: str, ref_key: str) -> None:
        self.values.pop((account_id, ref_key), None)
        self.deletes.append((account_id, ref_key))


@pytest.fixture(scope="module")
def oauth_database() -> Iterator[Any]:
    """Start an isolated, random-port PostgreSQL without applying migration ledger rows."""
    configured_dsn = resolve_test_db_dsn()
    is_audit_database = (
        "host=pg-audit" in configured_dsn or "@pg-audit:" in configured_dsn
    ) and "flow_steward_audit" in configured_dsn
    if is_audit_database:
        yield SimpleNamespace(dsn=configured_dsn)
        return

    reason = _disposable_db.unavailable_reason()
    if reason:
        pytest.skip(f"disposable PostgreSQL unavailable: {reason}")
    database = _disposable_db.start_disposable_db()
    try:
        yield database
    finally:
        _disposable_db.stop_disposable_db(database.container)


@pytest.fixture
def db_conn(
    oauth_database: Any,
) -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(oauth_database.dsn)
    conn.autocommit = False
    try:
        _seed_scopes(conn)
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _seed_scopes(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO accounts (id, tenant_id, account_key, display_name)
               VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
            (
                (ACCOUNT_A, TENANT_A, "oauth-a", "OAuth A"),
                (ACCOUNT_B, TENANT_B, "oauth-b", "OAuth B"),
            ),
        )
        cur.executemany(
            """INSERT INTO workspaces
                   (id, tenant_id, account_id, workspace_key, display_name)
               VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
            (
                (WORKSPACE_A, TENANT_A, ACCOUNT_A, "oauth-a", "OAuth A"),
                (WORKSPACE_B, TENANT_B, ACCOUNT_B, "oauth-b", "OAuth B"),
            ),
        )
        cur.executemany(
            """INSERT INTO projects (id, tenant_id, account_id, workspace_id, name)
               VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
            (
                (PROJECT_A, TENANT_A, ACCOUNT_A, WORKSPACE_A, "oauth-a"),
                (PROJECT_B, TENANT_B, ACCOUNT_B, WORKSPACE_B, "oauth-b"),
            ),
        )
        cur.executemany(
            """INSERT INTO extension_releases
                   (release_id, extension_id, release_version, descriptor_sha256)
                   VALUES (%s, %s, %s, repeat('a', 64)) ON CONFLICT (release_id) DO NOTHING""",
            (
                (RELEASE_A, "fixture_oauth_a", "1.0.0"),
                (RELEASE_A_NEXT, "fixture_oauth_a", "2.0.0"),
                (RELEASE_B, "fixture_oauth_b", "1.0.0"),
            ),
        )
        cur.executemany(
            """INSERT INTO extension_installs
                   (extension_id, account_id, install_id, release_id)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (extension_id, account_id) DO NOTHING""",
            (
                ("fixture_oauth_a", ACCOUNT_A, INSTALL_A, RELEASE_A),
                ("fixture_oauth_b", ACCOUNT_B, INSTALL_B, RELEASE_B),
            ),
        )


def _configuration_values(*, configuration_id: UUID | None = None) -> dict[str, object]:
    return {
        "configuration_id": configuration_id or uuid4(),
        "tenant_id": TENANT_A,
        "account_id": ACCOUNT_A,
        "project_id": PROJECT_A,
        "extension_install_id": INSTALL_A,
        "extension_id": "fixture_oauth_a",
        "extension_release_id": RELEASE_A,
        "provider_id": PROVIDER_ID,
        "display_name": "Primary",
        "configuration_revision": 1,
        "authorization_revision": 1,
        "status": "ready_for_authorization",
        "non_secret_config": {"client_id": "client-a"},
        "secret_refs": {"client_secret": "secret-ref-a"},
        "provider_security_fingerprint": "security-a",
        "provider_revocation_fingerprint": "revocation-a",
        "last_error": {},
    }


def _state_values(configuration_id: UUID, *, existing_connection_id: str | None = None):
    return {
        "state_hash": f"state-{uuid4().hex}",
        "tenant_id": TENANT_A,
        "account_id": ACCOUNT_A,
        "project_id": PROJECT_A,
        "extension_install_id": INSTALL_A,
        "extension_id": "fixture_oauth_a",
        "extension_release_id": RELEASE_A,
        "callback_id": "oauth_callback",
        "provider_id": PROVIDER_ID,
        "configuration_id": configuration_id,
        "configuration_revision": 1,
        "authorization_revision": 1,
        "connection_type": "fixture_mail",
        "intent_id": "connect",
        "existing_connection_id": existing_connection_id,
        "display_name": "Mailbox",
        "intent_payload": {"email_hint": "person@example.test"},
        "requested_scopes": ["fixture.read"],
        "redirect_uri": "https://app.example.test/callbacks/oauth",
        "pkce_verifier_encrypted": "encrypted-verifier",
        "provider_security_fingerprint": "security-a",
        "expires_at": "2099-01-01T00:00:00+00:00",
    }


def _seed_recoverable_disconnect(
    db_conn: psycopg.Connection,
    *,
    recovery_metadata: dict[str, str],
) -> tuple[ExtensionOAuthRepository, UUID, str, dict[str, Any]]:
    manifest = _lifecycle_manifest()
    provider = compile_oauth_providers(manifest)[0]
    configuration_id = uuid4()
    configuration_ref = configuration_secret_ref_key(str(configuration_id), "client_secret")
    repo = ExtensionOAuthRepository(db_conn)
    configuration_values = {
        **_configuration_values(configuration_id=configuration_id),
        "provider_id": provider.provider_id,
        "secret_refs": {"refs": {"client_secret": configuration_ref}},
        "provider_security_fingerprint": provider_security_fingerprint(provider),
        "provider_revocation_fingerprint": provider_revocation_fingerprint(provider),
    }
    configuration = repo.create_configuration(**configuration_values)
    assert configuration is not None
    connection_id = f"conn_recovery_{uuid4().hex}"
    token_ref = connection_secret_ref_key(connection_id, "_oauth_refresh_token")
    with db_conn.cursor() as cur:
        cur.execute(
            """INSERT INTO project_connections (
                   connection_id, project_id, extension_id, account_id,
                   extension_install_id, connection_type, oauth_configuration_id,
                   oauth_provider_id, oauth_extension_release_id,
                   oauth_authorization_revision, oauth_requested_scopes,
                   oauth_granted_scopes, oauth_provider_security_fingerprint,
                   oauth_provider_revocation_fingerprint, credential_revision,
                   enabled, connection_config, secret_refs
               ) VALUES (
                   %s, %s, 'fixture_oauth_a', %s, %s, 'example_mail', %s,
                   %s, %s, 1, '["mail.read"]'::jsonb, '["mail.read"]'::jsonb,
                   %s, %s, 4, false, %s::jsonb, %s::jsonb
               )""",
            (
                connection_id,
                PROJECT_A,
                ACCOUNT_A,
                INSTALL_A,
                configuration_id,
                provider.provider_id,
                RELEASE_A,
                provider_security_fingerprint(provider),
                provider_revocation_fingerprint(provider),
                psycopg.types.json.Jsonb(
                    {"oauth": {"lifecycle_status": "disconnecting", **recovery_metadata}}
                ),
                psycopg.types.json.Jsonb({"refs": {"_oauth_refresh_token": token_ref}}),
            ),
        )
    return repo, configuration_id, connection_id, manifest


def _database_lifecycle_service(
    db_conn: psycopg.Connection,
    *,
    manifest: dict[str, Any],
    deleted_refs: list[str],
    revoked_tokens: list[str],
) -> OAuthConnectionLifecycleService:
    return OAuthConnectionLifecycleService(
        repository=ExtensionOAuthRepository(db_conn),
        install_loader=lambda install_id, account_id: (
            {
                "extension_id": "fixture_oauth_a",
                "release_id": RELEASE_A,
                "compiled_manifest": manifest,
            }
            if install_id == INSTALL_A and account_id == str(ACCOUNT_A)
            else None
        ),
        token_snapshot=lambda **_values: {"refresh_token": "refresh-canary"},
        configuration_secret_value=lambda **_values: "client-secret-canary",
        delete_secret=lambda **values: deleted_refs.append(values["ref_key"]),
        revoke_token=lambda **values: revoked_tokens.append(values["token"]),
        transaction_factory=lambda: db_conn.transaction(),
    )


def _oauth_scope() -> ScopeContext:
    return ScopeContext(
        tenant_id=str(TENANT_A),
        account_id=str(ACCOUNT_A),
        project_id=str(PROJECT_A),
        principal_type="operator",
    )


def test_canonical_schema_declares_generic_contract() -> None:
    canonical = "\n".join(path.read_text(encoding="utf-8") for path in SCHEMA_PARTS)

    assert "extension_oauth_configurations" in canonical
    assert "extension_oauth_authorization_states" in canonical
    assert "oauth_configuration_id" in canonical
    assert "provider_security_fingerprint" in canonical
    assert "pkce_verifier_encrypted" in canonical
    assert "intent_payload" in canonical
    assert "existing_connection_id text" in canonical


def test_schema_columns_constraints_indexes_and_no_global_provider_fk(db_conn) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            """SELECT table_name, column_name, is_nullable, data_type
               FROM information_schema.columns
               WHERE table_schema = 'public'
                 AND table_name IN (
                     'extension_oauth_configurations',
                     'extension_oauth_authorization_states',
                     'project_connections'
                 )"""
        )
        columns = {(row[0], row[1]): (row[2], row[3]) for row in cur.fetchall()}
        assert columns[("extension_oauth_authorization_states", "existing_connection_id")][0] == (
            "YES"
        )
        assert columns[("extension_oauth_authorization_states", "expires_at")][1] == (
            "timestamp with time zone"
        )
        assert columns[("project_connections", "oauth_configuration_id")][0] == "YES"
        for column in (
            "extension_id",
            "secret_refs",
            "configuration_revision",
            "authorization_revision",
            "provider_security_fingerprint",
            "provider_revocation_fingerprint",
            "created_at",
            "updated_at",
        ):
            assert ("extension_oauth_configurations", column) in columns

        cur.execute(
            """SELECT conname, pg_get_constraintdef(oid)
               FROM pg_constraint
               WHERE conrelid IN (
                   'extension_oauth_configurations'::regclass,
                   'extension_oauth_authorization_states'::regclass,
                   'project_connections'::regclass
               )"""
        )
        constraints = dict(cur.fetchall())
        assert all("providers" not in definition for definition in constraints.values())
        assert (
            "tenant_id, account_id, project_id, extension_install_id, extension_id, provider_id"
        ) in (constraints["extension_oauth_authorization_states_configuration_scope_fkey"])
        assert (
            "extension_release_id"
            not in constraints["extension_oauth_authorization_states_configuration_scope_fkey"]
        )
        assert (
            "oauth_configuration_id"
            in (constraints["project_connections_oauth_configuration_scope_fkey"])
        )
        assert "incomplete" in constraints["extension_oauth_configurations_status_check"]

        cur.execute(
            """SELECT indexname, indexdef FROM pg_indexes
               WHERE schemaname = 'public'
                 AND indexname LIKE '%extension_oauth%'"""
        )
        indexes = dict(cur.fetchall())
        assert {
            "idx_extension_oauth_configurations_scope",
            "idx_extension_oauth_configurations_project_scope",
            "idx_extension_oauth_configurations_install",
            "idx_extension_oauth_authorization_states_scope",
            "idx_extension_oauth_authorization_states_configuration_scope",
            "idx_extension_oauth_authorization_states_expiry",
            "idx_extension_oauth_authorization_states_existing_connection",
        } <= indexes.keys()
        assert (
            "(project_id, tenant_id, account_id)"
            in indexes["idx_extension_oauth_configurations_project_scope"]
        )
        assert (
            "(configuration_id, tenant_id, account_id, project_id, "
            "extension_install_id, extension_id, provider_id)"
        ) in indexes["idx_extension_oauth_authorization_states_configuration_scope"]


def test_configuration_status_and_exact_lineage_constraints(db_conn) -> None:
    repo = ExtensionOAuthRepository(db_conn)
    values = _configuration_values()
    created = repo.create_configuration(**values)
    assert created is not None

    invalid = dict(_configuration_values(), status="connected")
    with pytest.raises(CheckViolation), db_conn.transaction():
        repo.create_configuration(**invalid)

    wrong_tenant = dict(_configuration_values(), tenant_id=TENANT_B)
    with pytest.raises(ForeignKeyViolation), db_conn.transaction():
        repo.create_configuration(**wrong_tenant)


def test_configuration_crud_is_revisioned_and_scope_bound(db_conn) -> None:
    repo = ExtensionOAuthRepository(db_conn)
    created = repo.create_configuration(**_configuration_values())
    scope = {
        "tenant_id": TENANT_A,
        "account_id": ACCOUNT_A,
        "project_id": PROJECT_A,
        "extension_install_id": INSTALL_A,
        "extension_id": "fixture_oauth_a",
        "provider_id": PROVIDER_ID,
        "configuration_id": created["configuration_id"],
    }
    list_scope = {key: value for key, value in scope.items() if key != "configuration_id"}
    assert [row["configuration_id"] for row in repo.list_configurations(**list_scope)] == [
        created["configuration_id"]
    ]

    updated = repo.update_configuration(
        **scope,
        expected_configuration_revision=1,
        extension_release_id=RELEASE_A,
        display_name="Renamed",
        configuration_revision=2,
        authorization_revision=1,
        status="verified",
        non_secret_config={"client_id": "client-a"},
        secret_refs={"client_secret": "secret-ref-a"},
        provider_security_fingerprint="security-a",
        provider_revocation_fingerprint="revocation-a",
        last_error={},
    )
    assert updated is not None
    assert updated["configuration_revision"] == 2
    assert updated["display_name"] == "Renamed"
    assert (
        repo.update_configuration(
            **scope,
            expected_configuration_revision=1,
            extension_release_id=RELEASE_A,
            display_name="Stale",
            configuration_revision=2,
            authorization_revision=1,
            status="verified",
            non_secret_config={},
            secret_refs={},
            provider_security_fingerprint="security-a",
            provider_revocation_fingerprint="revocation-a",
            last_error={},
        )
        is None
    )


def test_project_connection_model_and_store_persist_exact_oauth_configuration(db_conn) -> None:
    repo = ExtensionOAuthRepository(db_conn)
    configuration = repo.create_configuration(**_configuration_values())
    connection = ProjectConnection(
        connection_id="conn_store_oauth",
        project_id=str(PROJECT_A),
        extension_id="fixture_oauth_a",
        account_id=str(ACCOUNT_A),
        extension_install_id=INSTALL_A,
        connection_type="fixture_mail",
        oauth_configuration_id=str(configuration["configuration_id"]),
        oauth_provider_id=PROVIDER_ID,
        oauth_extension_release_id=RELEASE_A,
        oauth_authorization_revision=1,
        oauth_requested_scopes=["fixture.read"],
        oauth_granted_scopes=["fixture.read"],
        oauth_provider_security_fingerprint="security-a",
        oauth_provider_revocation_fingerprint="revocation-a",
    )

    project_connection_store.insert_project_connection(db_conn, connection)
    stored = project_connection_store.get_project_connection_row(db_conn, connection.connection_id)
    assert stored is not None
    assert stored["oauth_configuration_id"] == configuration["configuration_id"]
    assert stored["oauth_provider_id"] == PROVIDER_ID
    assert stored["oauth_requested_scopes"] == ["fixture.read"]


def test_new_state_is_nullable_and_does_not_materialize_connection(db_conn) -> None:
    repo = ExtensionOAuthRepository(db_conn)
    configuration = repo.create_configuration(**_configuration_values())
    assert configuration is not None

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM project_connections")
        before = cur.fetchone()[0]

    state = repo.create_state(**_state_values(configuration["configuration_id"]))
    assert state is not None
    assert state["existing_connection_id"] is None

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM project_connections")
        assert cur.fetchone()[0] == before


def test_foreign_scope_is_indistinguishable_from_missing(db_conn) -> None:
    repo = ExtensionOAuthRepository(db_conn)
    configuration = repo.create_configuration(**_configuration_values())
    assert configuration is not None
    configuration_id = configuration["configuration_id"]
    state = repo.create_state(**_state_values(configuration_id))
    assert state is not None

    foreign_scope = {
        "tenant_id": TENANT_B,
        "account_id": ACCOUNT_B,
        "project_id": PROJECT_B,
        "extension_install_id": INSTALL_B,
        "extension_id": "fixture_oauth_b",
        "provider_id": PROVIDER_ID,
        "configuration_id": configuration_id,
    }
    assert repo.get_configuration_for_scope(**foreign_scope) is None
    assert (
        repo.update_configuration(
            **foreign_scope,
            expected_configuration_revision=1,
            extension_release_id=RELEASE_B,
            display_name="Foreign",
            configuration_revision=2,
            authorization_revision=1,
            status="ready_for_authorization",
            non_secret_config={"client_id": "foreign"},
            secret_refs={},
            provider_security_fingerprint="security-a",
            provider_revocation_fingerprint="revocation-a",
            last_error={},
        )
        is None
    )
    assert repo.delete_configuration(**foreign_scope, expected_configuration_revision=1) is False
    assert repo.consume_state(state_hash=state["state_hash"], **foreign_scope) is None
    assert (
        repo.lock_connection(
            connection_id="missing", connection_type="fixture_mail", **foreign_scope
        )
        is None
    )
    assert repo.list_connections_for_configuration(**foreign_scope) == []

    visible = repo.get_configuration_for_scope(
        tenant_id=TENANT_A,
        account_id=ACCOUNT_A,
        project_id=PROJECT_A,
        extension_install_id=INSTALL_A,
        extension_id="fixture_oauth_a",
        provider_id=PROVIDER_ID,
        configuration_id=configuration_id,
    )
    assert visible is not None
    assert visible["display_name"] == "Primary"


def test_state_consumption_is_atomic_single_use_and_exact_scope(db_conn) -> None:
    repo = ExtensionOAuthRepository(db_conn)
    configuration = repo.create_configuration(**_configuration_values())
    assert configuration is not None
    configuration_id = configuration["configuration_id"]
    state = repo.create_state(**_state_values(configuration_id))
    scope = {
        "tenant_id": TENANT_A,
        "account_id": ACCOUNT_A,
        "project_id": PROJECT_A,
        "extension_install_id": INSTALL_A,
        "extension_id": "fixture_oauth_a",
        "provider_id": PROVIDER_ID,
        "configuration_id": configuration_id,
    }

    consumed = repo.consume_state(state_hash=state["state_hash"], **scope)
    assert consumed is not None
    assert consumed["consumed_at"] is not None
    assert repo.consume_state(state_hash=state["state_hash"], **scope) is None


def test_configuration_delete_cascades_state_but_restricts_referenced_connection(db_conn) -> None:
    repo = ExtensionOAuthRepository(db_conn)
    configuration = repo.create_configuration(**_configuration_values())
    configuration_id = configuration["configuration_id"]
    state = repo.create_state(**_state_values(configuration_id))
    scope = {
        "tenant_id": TENANT_A,
        "account_id": ACCOUNT_A,
        "project_id": PROJECT_A,
        "extension_install_id": INSTALL_A,
        "extension_id": "fixture_oauth_a",
        "provider_id": PROVIDER_ID,
        "configuration_id": configuration_id,
    }
    assert repo.delete_configuration(**scope, expected_configuration_revision=1) is True
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM extension_oauth_authorization_states WHERE state_hash = %s",
            (state["state_hash"],),
        )
        assert cur.fetchone()[0] == 0

    configuration = repo.create_configuration(**_configuration_values())
    configuration_id = configuration["configuration_id"]
    with db_conn.cursor() as cur:
        cur.execute(
            """INSERT INTO project_connections (
                   connection_id, project_id, extension_id, account_id,
                   extension_install_id, connection_type, oauth_configuration_id,
                   oauth_provider_id, oauth_extension_release_id,
                   oauth_authorization_revision, oauth_requested_scopes,
                   oauth_granted_scopes, oauth_provider_security_fingerprint,
                   oauth_provider_revocation_fingerprint
               ) VALUES (
                   'conn_oauth_a', %s, 'fixture_oauth_a', %s, %s, 'fixture_mail', %s,
                   %s, %s, 1, '["fixture.read"]'::jsonb, '["fixture.read"]'::jsonb,
                   'security-a', 'revocation-a'
               )""",
            (PROJECT_A, ACCOUNT_A, INSTALL_A, configuration_id, PROVIDER_ID, RELEASE_A),
        )

    scope["configuration_id"] = configuration_id
    with pytest.raises(ForeignKeyViolation), db_conn.transaction():
        repo.delete_configuration(**scope, expected_configuration_revision=1)

    locked = repo.lock_connection(
        connection_id="conn_oauth_a", connection_type="fixture_mail", **scope
    )
    assert locked is not None
    assert locked["oauth_configuration_id"] == configuration_id
    assert [row["connection_id"] for row in repo.list_connections_for_configuration(**scope)] == [
        "conn_oauth_a"
    ]


def test_configuration_lifecycle_mutations_are_revision_and_scope_exact(db_conn) -> None:
    repo = ExtensionOAuthRepository(db_conn)
    selected = repo.create_configuration(**_configuration_values())
    other = repo.create_configuration(**_configuration_values())
    assert selected is not None
    assert other is not None
    selected_state = repo.create_state(**_state_values(selected["configuration_id"]))
    other_state = repo.create_state(**_state_values(other["configuration_id"]))
    assert selected_state is not None
    assert other_state is not None

    with db_conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO project_connections (
                   connection_id, project_id, extension_id, account_id,
                   extension_install_id, connection_type, oauth_configuration_id,
                   oauth_provider_id, oauth_extension_release_id,
                   oauth_authorization_revision, oauth_requested_scopes,
                   oauth_granted_scopes, oauth_provider_security_fingerprint,
                   oauth_provider_revocation_fingerprint, connection_config
               ) VALUES (
                   %s, %s, 'fixture_oauth_a', %s, %s, 'fixture_mail', %s,
                   %s, %s, 1, '[]'::jsonb, '[]'::jsonb, 'security-a', 'revocation-a',
                   %s::jsonb
               )""",
            (
                (
                    "conn_selected",
                    PROJECT_A,
                    ACCOUNT_A,
                    INSTALL_A,
                    selected["configuration_id"],
                    PROVIDER_ID,
                    RELEASE_A,
                    '{"oauth":{"subject":"selected"},"other":true}',
                ),
                (
                    "conn_other",
                    PROJECT_A,
                    ACCOUNT_A,
                    INSTALL_A,
                    other["configuration_id"],
                    PROVIDER_ID,
                    RELEASE_A,
                    "{}",
                ),
            ),
        )

    scope = {
        "tenant_id": TENANT_A,
        "account_id": ACCOUNT_A,
        "project_id": PROJECT_A,
        "extension_install_id": INSTALL_A,
        "extension_id": "fixture_oauth_a",
        "provider_id": PROVIDER_ID,
        "configuration_id": selected["configuration_id"],
    }
    assert repo.invalidate_pending_states(**scope) == 1
    assert repo.mark_connections_reauthorization_required(**scope) == 1
    with db_conn.cursor() as cur:
        cur.execute(
            """SELECT connection_id,
                      connection_config->'oauth'->>'lifecycle_status',
                      connection_config->'oauth'->>'subject',
                      connection_config->>'other'
                 FROM project_connections
                ORDER BY connection_id"""
        )
        assert cur.fetchall() == [
            ("conn_other", None, None, None),
            ("conn_selected", "authorization_required", "selected", "true"),
        ]
        cur.execute(
            """SELECT state_hash, consumed_at IS NOT NULL
                 FROM extension_oauth_authorization_states
                ORDER BY state_hash"""
        )
        state_status = dict(cur.fetchall())
        assert state_status[selected_state["state_hash"]] is True
        assert state_status[other_state["state_hash"]] is False

    assert repo.delete_connections_for_configuration(**scope) == 1
    assert repo.delete_configuration(**scope, expected_configuration_revision=99) is False
    assert repo.delete_configuration(**scope, expected_configuration_revision=1) is True
    with db_conn.cursor() as cur:
        cur.execute("SELECT connection_id FROM project_connections ORDER BY connection_id")
        assert cur.fetchall() == [("conn_other",)]


def test_release_upgrade_inventory_and_apply_are_exact_and_atomic(db_conn) -> None:
    oauth = ExtensionOAuthRepository(db_conn)
    configuration = oauth.create_configuration(**_configuration_values())
    configuration_id = configuration["configuration_id"]
    affected_state = oauth.create_state(**_state_values(configuration_id))
    with db_conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO project_connections (
                   connection_id, project_id, extension_id, account_id,
                   extension_install_id, connection_type, oauth_configuration_id,
                   oauth_provider_id, oauth_extension_release_id,
                   oauth_authorization_revision, oauth_requested_scopes,
                   oauth_granted_scopes, oauth_provider_security_fingerprint,
                   oauth_provider_revocation_fingerprint, connection_config
               ) VALUES (
                   %s, %s, 'fixture_oauth_a', %s, %s, 'fixture_mail', %s,
                   %s, %s, 1, '["fixture.read"]'::jsonb, %s::jsonb,
                   'security-a', 'revocation-a', %s::jsonb
               )""",
            (
                (
                    "conn_upgrade_affected",
                    PROJECT_A,
                    ACCOUNT_A,
                    INSTALL_A,
                    configuration_id,
                    PROVIDER_ID,
                    RELEASE_A,
                    '["fixture.read"]',
                    '{"oauth":{"subject":"keep"}}',
                ),
                (
                    "conn_upgrade_unaffected",
                    PROJECT_A,
                    ACCOUNT_A,
                    INSTALL_A,
                    configuration_id,
                    PROVIDER_ID,
                    RELEASE_A,
                    '["fixture.read", "fixture.write"]',
                    "{}",
                ),
            ),
        )

    repository = PostgresOAuthReleaseUpgradeRepository(db_conn)
    configurations, connections = repository.lock_oauth_release_inventory(
        account_id=str(ACCOUNT_A),
        install_id=INSTALL_A,
        extension_id="fixture_oauth_a",
        old_release_id=RELEASE_A,
    )

    assert configurations == [
        {
            "configuration_id": configuration_id,
            "provider_id": PROVIDER_ID,
            "pending_states": [
                {
                    "state_id": affected_state["state_hash"],
                    "connection_type": "fixture_mail",
                }
            ],
        }
    ]
    assert [row["connection_id"] for row in connections] == [
        "conn_upgrade_affected",
        "conn_upgrade_unaffected",
    ]

    repository.apply_oauth_release_impact(
        account_id=str(ACCOUNT_A),
        install_id=INSTALL_A,
        extension_id="fixture_oauth_a",
        old_release_id=RELEASE_A,
        new_release_id=RELEASE_A_NEXT,
        invalidate_pending_state_ids=(affected_state["state_hash"],),
        reauthorize_connection_ids=("conn_upgrade_affected",),
        provider_fingerprints={PROVIDER_ID: ("security-next", "revocation-next")},
    )

    with db_conn.cursor() as cur:
        cur.execute(
            """SELECT extension_release_id, provider_security_fingerprint,
                      provider_revocation_fingerprint
                 FROM extension_oauth_configurations
                WHERE configuration_id = %s""",
            (configuration_id,),
        )
        assert cur.fetchone() == (RELEASE_A_NEXT, "security-next", "revocation-next")
        cur.execute(
            """SELECT connection_id,
                      connection_config->'oauth'->>'lifecycle_status',
                      connection_config->'oauth'->>'subject',
                      oauth_extension_release_id,
                      oauth_provider_security_fingerprint,
                      oauth_provider_revocation_fingerprint
                 FROM project_connections
                WHERE connection_id LIKE 'conn_upgrade_%'
                ORDER BY connection_id"""
        )
        assert cur.fetchall() == [
            (
                "conn_upgrade_affected",
                "authorization_required",
                "keep",
                RELEASE_A_NEXT,
                "security-next",
                "revocation-next",
            ),
            (
                "conn_upgrade_unaffected",
                None,
                None,
                RELEASE_A_NEXT,
                "security-next",
                "revocation-next",
            ),
        ]
        cur.execute(
            """SELECT consumed_at IS NOT NULL
                 FROM extension_oauth_authorization_states
                WHERE state_hash = %s""",
            (affected_state["state_hash"],),
        )
        assert cur.fetchone() == (True,)


def test_release_upgrade_rejects_a_locked_fresh_disconnect_without_mutation(db_conn) -> None:
    oauth = ExtensionOAuthRepository(db_conn)
    configuration = oauth.create_configuration(**_configuration_values())
    configuration_id = configuration["configuration_id"]
    connection_id = "conn_upgrade_disconnect_race"
    refresh_ref = f"project_connection:{connection_id}:_oauth_refresh_token"
    with db_conn.cursor() as cur:
        cur.execute(
            """INSERT INTO project_connections (
                   connection_id, project_id, extension_id, account_id,
                   extension_install_id, connection_type, oauth_configuration_id,
                   oauth_provider_id, oauth_extension_release_id,
                   oauth_authorization_revision, oauth_requested_scopes,
                   oauth_granted_scopes, oauth_provider_security_fingerprint,
                   oauth_provider_revocation_fingerprint, connection_config, secret_refs
               ) VALUES (
                   %s, %s, 'fixture_oauth_a', %s, %s, 'fixture_mail', %s,
                   %s, %s, 1, '["fixture.read"]'::jsonb, '["fixture.read"]'::jsonb,
                   'security-a', 'revocation-a',
                   '{"oauth":{"lifecycle_status":"connected"}}'::jsonb, %s::jsonb
               )""",
            (
                connection_id,
                PROJECT_A,
                ACCOUNT_A,
                INSTALL_A,
                configuration_id,
                PROVIDER_ID,
                RELEASE_A,
                f'{{"refs":{{"_oauth_refresh_token":"{refresh_ref}"}}}}',
            ),
        )
    old_descriptor = {
        "runtime": {
            "extension_contract_v2": {
                "oauth_providers": [
                    {
                        "provider_id": PROVIDER_ID,
                        "security_fingerprint": "security-a",
                        "revocation_fingerprint": "revocation-a",
                    }
                ],
                "connection_types": [
                    {
                        "connection_type_id": "fixture_mail",
                        "auth": {
                            "type": "oauth2",
                            "provider_id": PROVIDER_ID,
                            "scopes": {"required": ["fixture.read"], "optional": []},
                        },
                    }
                ],
            }
        }
    }
    new_descriptor = {
        "runtime": {
            "extension_contract_v2": {
                **old_descriptor["runtime"]["extension_contract_v2"],
                "oauth_providers": [
                    {
                        "provider_id": PROVIDER_ID,
                        "security_fingerprint": "security-next",
                        "revocation_fingerprint": "revocation-next",
                    }
                ],
            }
        }
    }
    release_repository = PostgresOAuthReleaseUpgradeRepository(db_conn)
    service = OAuthReleaseUpgradeService(release_repository)
    prepare_values = {
        "account_id": str(ACCOUNT_A),
        "install_id": INSTALL_A,
        "extension_id": "fixture_oauth_a",
        "old_release_id": RELEASE_A,
        "new_release_id": RELEASE_A_NEXT,
        "old_descriptor": old_descriptor,
        "new_descriptor": new_descriptor,
    }
    with pytest.raises(ServicePreconditionError) as approval_info:
        service.prepare(**prepare_values, approved_impact_hash="")
    prepared = service.prepare(
        **prepare_values,
        approved_impact_hash=approval_info.value.payload["impact_hash"],
    )
    exact_scope = {
        "tenant_id": str(TENANT_A),
        "account_id": str(ACCOUNT_A),
        "project_id": str(PROJECT_A),
        "extension_install_id": INSTALL_A,
        "extension_id": "fixture_oauth_a",
        "extension_release_id": RELEASE_A,
        "provider_id": PROVIDER_ID,
        "configuration_id": str(configuration_id),
        "connection_id": connection_id,
    }
    assert oauth.block_connection_disconnect(
        **exact_scope,
        expected_credential_revision=1,
        disconnect_operation_id="release-race-disconnect-operation",
        lease_seconds=300,
    )
    with db_conn.cursor() as cur:
        cur.execute(
            """SELECT credential_revision, enabled, connection_config, secret_refs,
                      oauth_granted_scopes, oauth_extension_release_id,
                      oauth_provider_security_fingerprint,
                      oauth_provider_revocation_fingerprint
                 FROM project_connections WHERE connection_id = %s""",
            (connection_id,),
        )
        connection_before = cur.fetchone()
        cur.execute(
            """SELECT extension_release_id, provider_security_fingerprint,
                      provider_revocation_fingerprint
                 FROM extension_oauth_configurations WHERE configuration_id = %s""",
            (configuration_id,),
        )
        configuration_before = cur.fetchone()

    with pytest.raises(ServicePreconditionError) as conflict_info:
        service.apply(prepared)

    assert conflict_info.value.payload == {
        "error": "oauth_release_disconnect_in_progress",
        "connection_ids": [connection_id],
    }
    with db_conn.cursor() as cur:
        cur.execute(
            """SELECT credential_revision, enabled, connection_config, secret_refs,
                      oauth_granted_scopes, oauth_extension_release_id,
                      oauth_provider_security_fingerprint,
                      oauth_provider_revocation_fingerprint
                 FROM project_connections WHERE connection_id = %s""",
            (connection_id,),
        )
        assert cur.fetchone() == connection_before
        cur.execute(
            """SELECT extension_release_id, provider_security_fingerprint,
                      provider_revocation_fingerprint
                 FROM extension_oauth_configurations WHERE configuration_id = %s""",
            (configuration_id,),
        )
        assert cur.fetchone() == configuration_before


@pytest.mark.parametrize(
    "recovery_metadata",
    [
        {},
        {
            "disconnect_operation_id": "malformed operation id",
            "disconnect_lease_expires_at": "not-a-timestamp",
        },
    ],
)
def test_disconnect_repairs_corrupt_recovery_metadata_without_an_unsafe_cast(
    db_conn,
    recovery_metadata: dict[str, str],
) -> None:
    repo, configuration_id, connection_id, manifest = _seed_recoverable_disconnect(
        db_conn, recovery_metadata=recovery_metadata
    )
    deleted_refs: list[str] = []
    revoked_tokens: list[str] = []
    service = _database_lifecycle_service(
        db_conn,
        manifest=manifest,
        deleted_refs=deleted_refs,
        revoked_tokens=revoked_tokens,
    )

    result = service.disconnect(
        _oauth_scope(), INSTALL_A, "example", str(configuration_id), connection_id
    )

    assert result == {
        "status": "disconnected",
        "connection_id": connection_id,
        "remote_revocation": "succeeded",
    }
    with db_conn.cursor() as cur:
        cur.execute(
            """SELECT credential_revision, enabled, oauth_granted_scopes,
                      secret_refs, connection_config->'oauth'
                 FROM project_connections WHERE connection_id = %s""",
            (connection_id,),
        )
        revision, enabled, granted_scopes, secret_refs, oauth = cur.fetchone()
    assert revision == 5
    assert enabled is True
    assert granted_scopes == []
    assert secret_refs == {}
    assert oauth == {"lifecycle_status": "disconnected"}
    assert revoked_tokens == ["refresh-canary"]
    assert deleted_refs == [connection_secret_ref_key(connection_id, "_oauth_refresh_token")]
    assert (
        repo.finalize_connection_disconnect(
            tenant_id=TENANT_A,
            account_id=ACCOUNT_A,
            project_id=PROJECT_A,
            extension_install_id=INSTALL_A,
            extension_id="fixture_oauth_a",
            provider_id="example",
            configuration_id=configuration_id,
            connection_id=connection_id,
            expected_credential_revision=4,
            expected_disconnect_operation_id=recovery_metadata.get("disconnect_operation_id", ""),
        )
        is False
    )


@pytest.mark.parametrize("mutation", ["save", "delete"])
@pytest.mark.parametrize(
    ("recovery_metadata", "recovery_kind"),
    [
        (
            {
                "disconnect_operation_id": "A" * 43,
                "disconnect_lease_expires_at": "2099-01-01T00:00:00+00:00",
            },
            "fresh",
        ),
        (
            {
                "disconnect_operation_id": "malformed operation id",
                "disconnect_lease_expires_at": "not-a-timestamp",
            },
            "recoverable",
        ),
    ],
)
def test_configuration_mutation_rejects_an_exact_in_flight_disconnect_before_writes(
    db_conn,
    mutation: str,
    recovery_metadata: dict[str, str],
    recovery_kind: str,
) -> None:
    repo, configuration_id, connection_id, manifest = _seed_recoverable_disconnect(
        db_conn, recovery_metadata=recovery_metadata
    )
    configuration_ref = configuration_secret_ref_key(str(configuration_id), "client_secret")
    secret_store = _MemoryConfigurationSecrets(
        {(str(ACCOUNT_A), configuration_ref): "client-secret-canary"}
    )
    service = OAuthConfigurationService(
        repository=repo,
        secret_store=secret_store,
        install_loader=lambda install_id, account_id: (
            {
                "extension_id": "fixture_oauth_a",
                "release_id": RELEASE_A,
                "compiled_manifest": manifest,
            }
            if install_id == INSTALL_A and account_id == str(ACCOUNT_A)
            else None
        ),
        confirmation_key=b"configuration-race-confirmation-key",
        transaction_factory=lambda: db_conn.transaction(),
    )
    with db_conn.cursor() as cur:
        cur.execute(
            """SELECT credential_revision, enabled, connection_config, secret_refs,
                      oauth_granted_scopes, oauth_extension_release_id,
                      oauth_provider_security_fingerprint,
                      oauth_provider_revocation_fingerprint
                 FROM project_connections WHERE connection_id = %s""",
            (connection_id,),
        )
        connection_before = deepcopy(cur.fetchone())
        cur.execute(
            """SELECT extension_release_id, configuration_revision,
                      authorization_revision, status, non_secret_config,
                      secret_refs, provider_security_fingerprint,
                      provider_revocation_fingerprint
                 FROM extension_oauth_configurations WHERE configuration_id = %s""",
            (configuration_id,),
        )
        configuration_before = deepcopy(cur.fetchone())
    secrets_before = deepcopy(secret_store.values)

    if mutation == "save":
        command = OAuthConfigurationSave(
            configuration_id=configuration_id,
            display_name="Primary",
            values={"client_id": "changed-client"},
        )
        impact = service.preview_configuration_change(
            _oauth_scope(),
            INSTALL_A,
            "example",
            command,
            expected_revision=1,
        )

        def invoke() -> object:
            return service.save_configuration(
                _oauth_scope(),
                INSTALL_A,
                "example",
                command,
                expected_revision=1,
                confirmation_token=impact.confirmation_token,
            )

    else:
        impact = service.preview_configuration_delete(
            _oauth_scope(), INSTALL_A, "example", str(configuration_id), expected_revision=1
        )

        def invoke() -> object:
            return service.delete_configuration(
                _oauth_scope(),
                INSTALL_A,
                "example",
                str(configuration_id),
                expected_revision=1,
                confirmation_token=impact.confirmation_token,
            )

    with pytest.raises(ServicePreconditionError) as exc_info:
        invoke()

    assert exc_info.value.payload == {
        "error": "oauth_configuration_disconnect_in_progress",
        "connection_ids": [connection_id],
    }
    with db_conn.cursor() as cur:
        cur.execute(
            """SELECT credential_revision, enabled, connection_config, secret_refs,
                      oauth_granted_scopes, oauth_extension_release_id,
                      oauth_provider_security_fingerprint,
                      oauth_provider_revocation_fingerprint
                 FROM project_connections WHERE connection_id = %s""",
            (connection_id,),
        )
        assert cur.fetchone() == connection_before
        cur.execute(
            """SELECT extension_release_id, configuration_revision,
                      authorization_revision, status, non_secret_config,
                      secret_refs, provider_security_fingerprint,
                      provider_revocation_fingerprint
                 FROM extension_oauth_configurations WHERE configuration_id = %s""",
            (configuration_id,),
        )
        assert cur.fetchone() == configuration_before
    assert secret_store.values == secrets_before
    assert secret_store.writes == []
    assert secret_store.deletes == []

    if recovery_kind == "fresh":
        assert repo.finalize_connection_disconnect(
            tenant_id=TENANT_A,
            account_id=ACCOUNT_A,
            project_id=PROJECT_A,
            extension_install_id=INSTALL_A,
            extension_id="fixture_oauth_a",
            provider_id="example",
            configuration_id=configuration_id,
            connection_id=connection_id,
            expected_credential_revision=4,
            expected_disconnect_operation_id="A" * 43,
        )
    else:
        lifecycle = _database_lifecycle_service(
            db_conn, manifest=manifest, deleted_refs=[], revoked_tokens=[]
        )
        assert (
            lifecycle.disconnect(
                _oauth_scope(), INSTALL_A, "example", str(configuration_id), connection_id
            )["status"]
            == "disconnected"
        )


def test_disconnect_and_configuration_mutation_share_configuration_first_lock_order(
    db_conn, oauth_database
) -> None:
    _repo, configuration_id, connection_id, _manifest = _seed_recoverable_disconnect(
        db_conn, recovery_metadata={}
    )
    with db_conn.cursor() as cur:
        cur.execute(
            """UPDATE project_connections
                  SET enabled = true,
                      credential_revision = 3,
                      connection_config = '{"oauth":{"lifecycle_status":"connected"}}'::jsonb
                WHERE connection_id = %s""",
            (connection_id,),
        )
    db_conn.commit()
    configuration_conn = psycopg.connect(oauth_database.dsn)
    disconnect_conn = psycopg.connect(oauth_database.dsn)
    observer_conn = psycopg.connect(oauth_database.dsn)
    outcome: queue.Queue[tuple[str, object]] = queue.Queue()
    started = threading.Event()
    scope = {
        "tenant_id": TENANT_A,
        "account_id": ACCOUNT_A,
        "project_id": PROJECT_A,
        "extension_install_id": INSTALL_A,
        "extension_id": "fixture_oauth_a",
        "extension_release_id": RELEASE_A,
        "provider_id": "example",
        "configuration_id": configuration_id,
        "connection_id": connection_id,
    }
    try:
        with disconnect_conn.cursor() as cur:
            cur.execute("SET deadlock_timeout = '100ms'")
            cur.execute("SET statement_timeout = '5s'")
            cur.execute("SELECT pg_backend_pid()")
            disconnect_pid = int(cur.fetchone()[0])
        disconnect_conn.commit()
        with configuration_conn.cursor() as cur:
            cur.execute("SET deadlock_timeout = '100ms'")
            cur.execute("SET statement_timeout = '5s'")
        configuration_conn.commit()
        configuration_repo = ExtensionOAuthRepository(configuration_conn)
        assert (
            configuration_repo.get_configuration_for_scope_for_update(
                tenant_id=TENANT_A,
                account_id=ACCOUNT_A,
                project_id=PROJECT_A,
                extension_install_id=INSTALL_A,
                extension_id="fixture_oauth_a",
                provider_id="example",
                configuration_id=configuration_id,
            )
            is not None
        )

        def lock_for_disconnect() -> None:
            started.set()
            try:
                row = ExtensionOAuthRepository(disconnect_conn).lock_connection_for_lifecycle(
                    **scope
                )
            except BaseException as exc:  # pragma: no cover - relayed to owner thread
                outcome.put(("error", exc))
            else:
                outcome.put(("row", row))

        disconnect_thread = threading.Thread(target=lock_for_disconnect, daemon=True)
        disconnect_thread.start()
        assert started.wait(timeout=1)
        deadline = time.monotonic() + 3
        waiting = False
        while time.monotonic() < deadline and not waiting:
            with observer_conn.cursor() as cur:
                cur.execute(
                    "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s",
                    (disconnect_pid,),
                )
                activity = cur.fetchone()
            waiting = bool(activity and activity[0] == "Lock")
            if not waiting:
                started.wait(timeout=0.01)
        assert waiting, "disconnect did not reach the held configuration lock"

        configuration_error: BaseException | None = None
        try:
            locked_connections = configuration_repo.list_connections_for_configuration_for_update(
                tenant_id=TENANT_A,
                account_id=ACCOUNT_A,
                project_id=PROJECT_A,
                extension_install_id=INSTALL_A,
                extension_id="fixture_oauth_a",
                provider_id="example",
                configuration_id=configuration_id,
            )
        except BaseException as exc:
            configuration_error = exc
            configuration_conn.rollback()
        else:
            configuration_conn.commit()

        disconnect_thread.join(timeout=5)
        assert not disconnect_thread.is_alive()
        disconnect_kind, disconnect_value = outcome.get(timeout=1)
        assert configuration_error is None
        assert [row["connection_id"] for row in locked_connections] == [connection_id]
        assert disconnect_kind == "row"
        assert isinstance(disconnect_value, dict)
        assert disconnect_value["connection_id"] == connection_id
        disconnect_conn.rollback()
    finally:
        configuration_conn.rollback()
        disconnect_conn.rollback()
        for connection in (observer_conn, disconnect_conn, configuration_conn):
            connection.close()
        with db_conn.cursor() as cur:
            cur.execute(
                """DELETE FROM project_connections
                    WHERE connection_id = %s
                      AND account_id = %s
                      AND project_id = %s
                      AND extension_install_id = %s
                      AND oauth_configuration_id = %s""",
                (connection_id, ACCOUNT_A, PROJECT_A, INSTALL_A, configuration_id),
            )
            cur.execute(
                """DELETE FROM extension_oauth_configurations
                    WHERE configuration_id = %s
                      AND tenant_id = %s
                      AND account_id = %s
                      AND project_id = %s
                      AND extension_install_id = %s""",
                (configuration_id, TENANT_A, ACCOUNT_A, PROJECT_A, INSTALL_A),
            )
        db_conn.commit()


def test_configuration_mutation_lock_blocks_concurrent_connection_materialization(
    db_conn, oauth_database
) -> None:
    repo = ExtensionOAuthRepository(db_conn)
    configuration = repo.create_configuration(**_configuration_values())
    assert configuration is not None
    configuration_id = configuration["configuration_id"]
    db_conn.commit()
    scope = {
        "tenant_id": TENANT_A,
        "account_id": ACCOUNT_A,
        "project_id": PROJECT_A,
        "extension_install_id": INSTALL_A,
        "extension_id": "fixture_oauth_a",
        "provider_id": PROVIDER_ID,
        "configuration_id": configuration_id,
    }

    try:
        assert repo.get_configuration_for_scope_for_update(**scope) is not None
        concurrent = psycopg.connect(oauth_database.dsn)
        try:

            def _raises_operation_1457():
                cur.execute("SET LOCAL lock_timeout = '150ms'")
                cur.execute(
                    """INSERT INTO project_connections (
                               connection_id, project_id, extension_id, account_id,
                               extension_install_id, connection_type,
                               oauth_configuration_id, oauth_provider_id,
                               oauth_extension_release_id, oauth_authorization_revision,
                               oauth_requested_scopes, oauth_granted_scopes,
                               oauth_provider_security_fingerprint,
                               oauth_provider_revocation_fingerprint
                           ) VALUES (
                               'conn_blocked_by_configuration_lock', %s,
                               'fixture_oauth_a', %s, %s, 'fixture_mail', %s,
                               %s, %s, 1, '[]'::jsonb, '[]'::jsonb,
                               'security-a', 'revocation-a'
                           )""",
                    (
                        PROJECT_A,
                        ACCOUNT_A,
                        INSTALL_A,
                        configuration_id,
                        PROVIDER_ID,
                        RELEASE_A,
                    ),
                )

            with (
                pytest.raises(LockNotAvailable),
                concurrent.transaction(),
                concurrent.cursor() as cur,
            ):
                _raises_operation_1457()
        finally:
            concurrent.close()
    finally:
        db_conn.rollback()
        with db_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM extension_oauth_configurations WHERE configuration_id = %s",
                (configuration_id,),
            )
        db_conn.commit()


def test_authorization_parent_lock_blocks_concurrent_save_until_state_exists(
    db_conn, oauth_database
) -> None:
    repo = ExtensionOAuthRepository(db_conn)
    configuration = repo.create_configuration(**_configuration_values())
    assert configuration is not None
    configuration_id = configuration["configuration_id"]
    db_conn.commit()
    scope = {
        "tenant_id": TENANT_A,
        "account_id": ACCOUNT_A,
        "project_id": PROJECT_A,
        "extension_install_id": INSTALL_A,
        "extension_id": "fixture_oauth_a",
        "provider_id": PROVIDER_ID,
        "configuration_id": configuration_id,
    }

    try:
        assert repo.get_configuration_for_scope_for_update(**scope) is not None
        concurrent = psycopg.connect(oauth_database.dsn)
        try:

            def _raises_operation_1521():
                cur.execute("SET LOCAL lock_timeout = '150ms'")
                cur.execute(
                    """UPDATE extension_oauth_configurations
                          SET authorization_revision = authorization_revision + 1
                        WHERE configuration_id = %s""",
                    (configuration_id,),
                )

            with (
                pytest.raises(LockNotAvailable),
                concurrent.transaction(),
                concurrent.cursor() as cur,
            ):
                _raises_operation_1521()
            state = repo.create_state(**_state_values(configuration_id))
            assert state is not None
            db_conn.commit()

            concurrent_repo = ExtensionOAuthRepository(concurrent)
            with concurrent.transaction():
                assert concurrent_repo.get_configuration_for_scope_for_update(**scope) is not None
                with concurrent.cursor() as cur:
                    cur.execute(
                        """UPDATE extension_oauth_configurations
                              SET authorization_revision = authorization_revision + 1
                            WHERE configuration_id = %s""",
                        (configuration_id,),
                    )
                assert concurrent_repo.invalidate_pending_states(**scope) == 1
        finally:
            concurrent.close()
    finally:
        db_conn.rollback()
        with db_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM extension_oauth_configurations WHERE configuration_id = %s",
                (configuration_id,),
            )
        db_conn.commit()


def test_oauth_callback_registration_scope_lock_and_unique_index_serialize_first_ensure(
    db_conn, oauth_database
) -> None:
    first = PlatformLoggerExtensionsAccountsCallbacksMixin()
    first.conn = db_conn
    db_conn.commit()
    try:
        first.lock_extension_callback_registration_scope(
            extension_id="fixture_oauth_a",
            callback_id="oauth_authorization",
            project_id=str(PROJECT_A),
            connection_id=None,
        )
        concurrent = psycopg.connect(oauth_database.dsn)
        second = PlatformLoggerExtensionsAccountsCallbacksMixin()
        second.conn = concurrent
        try:

            def _raises_operation_1577():
                with concurrent.cursor() as cur:
                    cur.execute("SET LOCAL lock_timeout = '150ms'")
                second.lock_extension_callback_registration_scope(
                    extension_id="fixture_oauth_a",
                    callback_id="oauth_authorization",
                    project_id=str(PROJECT_A),
                    connection_id=None,
                )

            with pytest.raises(LockNotAvailable), concurrent.transaction():
                _raises_operation_1577()
        finally:
            concurrent.close()
        db_conn.rollback()

        with db_conn.transaction(), db_conn.cursor() as cur:
            cur.execute(
                """INSERT INTO extension_callback_registrations (
                           extension_id, callback_id, callback_token_hash, project_id,
                           connection_id, auth_config_jsonb
                       ) VALUES (%s, %s, %s, %s, NULL, '{}'::jsonb)""",
                ("fixture_oauth_a", "oauth_authorization", "hash-one", PROJECT_A),
            )
        with pytest.raises(UniqueViolation), db_conn.transaction(), db_conn.cursor() as cur:
            cur.execute(
                """INSERT INTO extension_callback_registrations (
                           extension_id, callback_id, callback_token_hash, project_id,
                           connection_id, auth_config_jsonb
                       ) VALUES (%s, %s, %s, %s, NULL, '{}'::jsonb)""",
                ("fixture_oauth_a", "oauth_authorization", "hash-two", PROJECT_A),
            )
    finally:
        db_conn.rollback()
        with db_conn.cursor() as cur:
            cur.execute(
                """DELETE FROM extension_callback_registrations
                    WHERE extension_id = 'fixture_oauth_a'
                      AND callback_id = 'oauth_authorization'
                      AND project_id = %s""",
                (PROJECT_A,),
            )
        db_conn.commit()


def test_reconnect_state_fk_rejects_connection_from_another_configuration(db_conn) -> None:
    repo = ExtensionOAuthRepository(db_conn)
    first = repo.create_configuration(**_configuration_values())
    second = repo.create_configuration(**_configuration_values())
    with db_conn.cursor() as cur:
        cur.execute(
            """INSERT INTO project_connections (
                   connection_id, project_id, extension_id, account_id,
                   extension_install_id, connection_type, oauth_configuration_id,
                   oauth_provider_id, oauth_extension_release_id,
                   oauth_authorization_revision, oauth_requested_scopes,
                   oauth_granted_scopes, oauth_provider_security_fingerprint,
                   oauth_provider_revocation_fingerprint
               ) VALUES (
                   'conn_reconnect', %s, 'fixture_oauth_a', %s, %s, 'fixture_mail', %s,
                   %s, %s, 1, '[]'::jsonb, '[]'::jsonb, 'security-a', 'revocation-a'
               )""",
            (PROJECT_A, ACCOUNT_A, INSTALL_A, first["configuration_id"], PROVIDER_ID, RELEASE_A),
        )

    _raises_input_1639_1 = _state_values(
        second["configuration_id"], existing_connection_id="conn_reconnect"
    )
    with pytest.raises(ForeignKeyViolation), db_conn.transaction():
        repo.create_state(**_raises_input_1639_1)

    wrong_type = _state_values(first["configuration_id"], existing_connection_id="conn_reconnect")
    wrong_type["connection_type"] = "fixture_drive"
    with pytest.raises(ForeignKeyViolation), db_conn.transaction():
        repo.create_state(**wrong_type)


def test_install_repoint_preserves_old_grant_and_allows_current_release_reconnect(db_conn) -> None:
    repo = ExtensionOAuthRepository(db_conn)
    configuration = repo.create_configuration(**_configuration_values())
    configuration_id = configuration["configuration_id"]
    with db_conn.cursor() as cur:
        cur.execute(
            """INSERT INTO project_connections (
                   connection_id, project_id, extension_id, account_id,
                   extension_install_id, connection_type, oauth_configuration_id,
                   oauth_provider_id, oauth_extension_release_id,
                   oauth_authorization_revision, oauth_requested_scopes,
                   oauth_granted_scopes, oauth_provider_security_fingerprint,
                   oauth_provider_revocation_fingerprint
               ) VALUES (
                   'conn_upgrade', %s, 'fixture_oauth_a', %s, %s, 'fixture_mail', %s,
                   %s, %s, 1, '[]'::jsonb, '[]'::jsonb, 'security-a', 'revocation-a'
               )""",
            (PROJECT_A, ACCOUNT_A, INSTALL_A, configuration_id, PROVIDER_ID, RELEASE_A),
        )
        cur.execute(
            "UPDATE extension_installs SET release_id = %s WHERE install_id = %s",
            (RELEASE_A_NEXT, INSTALL_A),
        )
        cur.execute(
            """UPDATE extension_oauth_configurations
               SET extension_release_id = %s WHERE configuration_id = %s""",
            (RELEASE_A_NEXT, configuration_id),
        )

    reconnect = _state_values(configuration_id, existing_connection_id="conn_upgrade")
    reconnect["extension_release_id"] = RELEASE_A_NEXT
    assert repo.create_state(**reconnect) is not None
    locked = repo.lock_connection(
        connection_id="conn_upgrade",
        tenant_id=TENANT_A,
        account_id=ACCOUNT_A,
        project_id=PROJECT_A,
        extension_install_id=INSTALL_A,
        extension_id="fixture_oauth_a",
        provider_id=PROVIDER_ID,
        configuration_id=configuration_id,
        connection_type="fixture_mail",
    )
    assert locked is not None
    assert locked["oauth_extension_release_id"] == RELEASE_A

    with db_conn.cursor() as cur:
        cur.execute(
            """SELECT install.release_id, configuration.extension_release_id,
                      connection.oauth_extension_release_id
               FROM extension_installs AS install
               JOIN extension_oauth_configurations AS configuration
                 ON configuration.extension_install_id = install.install_id
               JOIN project_connections AS connection
                 ON connection.oauth_configuration_id = configuration.configuration_id
               WHERE install.install_id = %s""",
            (INSTALL_A,),
        )
        assert cur.fetchone() == (RELEASE_A_NEXT, RELEASE_A_NEXT, RELEASE_A)


def test_oauth_connection_extension_must_match_owning_install(db_conn) -> None:
    repo = ExtensionOAuthRepository(db_conn)
    configuration = repo.create_configuration(**_configuration_values())
    with pytest.raises(ForeignKeyViolation), db_conn.transaction(), db_conn.cursor() as cur:
        cur.execute(
            """INSERT INTO project_connections (
                   connection_id, project_id, extension_id, account_id,
                   extension_install_id, connection_type, oauth_configuration_id,
                   oauth_provider_id, oauth_extension_release_id,
                   oauth_authorization_revision, oauth_requested_scopes,
                   oauth_granted_scopes, oauth_provider_security_fingerprint,
                   oauth_provider_revocation_fingerprint
               ) VALUES (
                   'conn_wrong_extension', %s, 'fixture_oauth_b', %s, %s,
                   'fixture_mail', %s, %s, %s, 1, '[]'::jsonb, '[]'::jsonb,
                   'security-a', 'revocation-a'
               )""",
            (
                PROJECT_A,
                ACCOUNT_A,
                INSTALL_A,
                configuration["configuration_id"],
                PROVIDER_ID,
                RELEASE_A,
            ),
        )


def test_oauth_metadata_is_bidirectional_all_or_none_in_model_and_database(db_conn) -> None:
    _raises_input_1740_1 = str(PROJECT_A)
    _raises_input_1740_2 = str(ACCOUNT_A)
    with pytest.raises(ValueError, match="exact configuration metadata"):
        ProjectConnection(
            project_id=_raises_input_1740_1,
            extension_id="fixture_oauth_a",
            account_id=_raises_input_1740_2,
            extension_install_id=INSTALL_A,
            oauth_provider_id=PROVIDER_ID,
        )

    with pytest.raises(CheckViolation), db_conn.transaction(), db_conn.cursor() as cur:
        cur.execute(
            """INSERT INTO project_connections (
                   connection_id, project_id, extension_id, account_id,
                   extension_install_id, oauth_provider_id
               ) VALUES ('conn_partial_oauth', %s, 'fixture_oauth_a', %s, %s, %s)""",
            (PROJECT_A, ACCOUNT_A, INSTALL_A, PROVIDER_ID),
        )


def test_expired_state_is_not_consumed(db_conn) -> None:
    repo = ExtensionOAuthRepository(db_conn)
    configuration = repo.create_configuration(**_configuration_values())
    state = repo.create_state(**_state_values(configuration["configuration_id"]))
    with db_conn.cursor() as cur:
        cur.execute(
            """UPDATE extension_oauth_authorization_states
               SET created_at = '2000-01-01T00:00:00Z',
                   expires_at = '2000-01-02T00:00:00Z'
               WHERE state_hash = %s""",
            (state["state_hash"],),
        )
    scope = {
        "tenant_id": TENANT_A,
        "account_id": ACCOUNT_A,
        "project_id": PROJECT_A,
        "extension_install_id": INSTALL_A,
        "extension_id": "fixture_oauth_a",
        "provider_id": PROVIDER_ID,
        "configuration_id": configuration["configuration_id"],
    }
    assert repo.consume_state(state_hash=state["state_hash"], **scope) is None
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT consumed_at FROM extension_oauth_authorization_states WHERE state_hash = %s",
            (state["state_hash"],),
        )
        assert cur.fetchone() == (None,)


def test_multiple_configurations_in_one_scope_remain_distinct(db_conn) -> None:
    repo = ExtensionOAuthRepository(db_conn)
    first = repo.create_configuration(**_configuration_values())
    second_values = _configuration_values()
    second_values["display_name"] = "Secondary"
    second = repo.create_configuration(**second_values)
    list_scope = {
        "tenant_id": TENANT_A,
        "account_id": ACCOUNT_A,
        "project_id": PROJECT_A,
        "extension_install_id": INSTALL_A,
        "extension_id": "fixture_oauth_a",
        "provider_id": PROVIDER_ID,
    }
    assert {row["configuration_id"] for row in repo.list_configurations(**list_scope)} == {
        first["configuration_id"],
        second["configuration_id"],
    }
    first_row = repo.get_configuration_for_scope(
        **list_scope, configuration_id=first["configuration_id"]
    )
    second_row = repo.get_configuration_for_scope(
        **list_scope, configuration_id=second["configuration_id"]
    )
    assert first_row["display_name"] == "Primary"
    assert second_row["display_name"] == "Secondary"


def test_release_upgrade_also_moves_a_setup_stranded_on_an_earlier_release(db_conn) -> None:
    """A setup an earlier update did not move was skipped by every later one.

    On the demo, a hand-adopted bundle left the Shopify setup behind; the next
    catalog update moved only setups on the release it replaced, so
    authorization stayed refused until the setup was deleted. A stranded setup
    that signs in exactly as the new release does is moved; one that differs is
    left for approval.
    """
    with db_conn.cursor() as cur:
        cur.execute(
            """INSERT INTO extension_releases
                   (release_id, extension_id, release_version, descriptor_sha256)
                   VALUES ('release_oauth_a_third', 'fixture_oauth_a', '3.0.0', repeat('a', 64))
                   ON CONFLICT (release_id) DO NOTHING"""
        )
    oauth = ExtensionOAuthRepository(db_conn)
    same_sign_in = oauth.create_configuration(
        **{
            **_configuration_values(),
            "display_name": "Same sign-in",
            "provider_security_fingerprint": "security-next",
            "provider_revocation_fingerprint": "revocation-next",
        }
    )["configuration_id"]
    changed_sign_in = oauth.create_configuration(
        **{**_configuration_values(), "display_name": "Changed sign-in"}
    )["configuration_id"]
    with db_conn.cursor() as cur:
        cur.execute(
            """INSERT INTO project_connections (
                   connection_id, project_id, extension_id, account_id,
                   extension_install_id, connection_type, oauth_configuration_id,
                   oauth_provider_id, oauth_extension_release_id,
                   oauth_authorization_revision, oauth_requested_scopes,
                   oauth_granted_scopes, oauth_provider_security_fingerprint,
                   oauth_provider_revocation_fingerprint, connection_config
               ) VALUES (
                   'conn_stranded', %s, 'fixture_oauth_a', %s, %s, 'fixture_mail', %s,
                   %s, %s, 1, '["fixture.read"]'::jsonb, '["fixture.read"]'::jsonb,
                   'security-next', 'revocation-next', '{}'::jsonb
               )""",
            (PROJECT_A, ACCOUNT_A, INSTALL_A, same_sign_in, PROVIDER_ID, RELEASE_A),
        )

    PostgresOAuthReleaseUpgradeRepository(db_conn).apply_oauth_release_impact(
        account_id=str(ACCOUNT_A),
        install_id=INSTALL_A,
        extension_id="fixture_oauth_a",
        old_release_id=RELEASE_A_NEXT,
        new_release_id="release_oauth_a_third",
        invalidate_pending_state_ids=(),
        reauthorize_connection_ids=(),
        provider_fingerprints={PROVIDER_ID: ("security-next", "revocation-next")},
    )

    with db_conn.cursor() as cur:
        cur.execute(
            """SELECT configuration_id, extension_release_id
                 FROM extension_oauth_configurations
                WHERE configuration_id = ANY(%s)""",
            ([same_sign_in, changed_sign_in],),
        )
        releases = {row[0]: row[1] for row in cur.fetchall()}
        cur.execute(
            "SELECT oauth_extension_release_id FROM project_connections"
            " WHERE connection_id = 'conn_stranded'"
        )
        connection_release = cur.fetchone()[0]

    assert releases[same_sign_in] == "release_oauth_a_third"
    assert releases[changed_sign_in] == RELEASE_A
    assert connection_release == "release_oauth_a_third"


def test_bound_workflow_steps_are_found_by_either_binding_and_only_in_the_account(db_conn) -> None:
    from core.application.extensions.oauth_repository import ExtensionOAuthRepository as Repo

    with db_conn.cursor() as cur:
        cur.execute(
            """INSERT INTO registry_workflows (workflow_key, display_name, account_id)
               VALUES ('wf_bound_steps', 'Bound steps', %s), ('wf_foreign_steps', 'Foreign', %s)""",
            (ACCOUNT_A, ACCOUNT_B),
        )
        cur.execute(
            """INSERT INTO registry_workflow_groups
                   (workflow_id, group_id, kind, display_name, sort_order, loop, body_jsonb)
               VALUES ('wf_bound_steps', 'export', 'loop', 'Export', 0,
                       '{"over": "state.products"}'::jsonb,
                       '{"kind": "sequence", "entry": {"kind": "step", "id": "override_only"}}'::jsonb)
               RETURNING id"""
        )
        export_group_id = cur.fetchone()[0]
        # A root step has no group; the grouped step reports its group's name.
        cur.executemany(
            """INSERT INTO registry_workflow_steps
                   (owner_workflow_id, group_id, step_kind, step_id, display_name, sort_order,
                    connection_binding_jsonb, override)
               VALUES (%s, %s, 'connector', %s, %s, %s, %s::jsonb, %s::jsonb)""",
            (
                (
                    "wf_bound_steps",
                    None,
                    "top_level",
                    "Top level binding",
                    0,
                    '{"connection_id": "conn_bound"}',
                    "{}",
                ),
                (
                    "wf_bound_steps",
                    export_group_id,
                    "override_only",
                    "Override binding",
                    1,
                    "{}",
                    '{"connector": {"connection_binding": {"connection_id": "conn_bound"}}}',
                ),
                (
                    "wf_bound_steps",
                    None,
                    "other_connection",
                    "Other connection",
                    2,
                    '{"connection_id": "conn_other"}',
                    "{}",
                ),
                (
                    "wf_foreign_steps",
                    None,
                    "foreign",
                    "Foreign account",
                    0,
                    '{"connection_id": "conn_bound"}',
                    "{}",
                ),
            ),
        )

    rows = Repo(db_conn).list_workflow_steps_bound_to_connections(
        account_id=ACCOUNT_A, connection_ids=["conn_bound"]
    )

    assert [(row["workflow_name"], row["group_name"], row["step_name"]) for row in rows] == [
        ("Bound steps", None, "Top level binding"),
        ("Bound steps", "Export", "Override binding"),
    ]
