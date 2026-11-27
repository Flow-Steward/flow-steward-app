"""Secret backends on the disposable audit database (ADR 0066 amendment 2026-11-26).

An account admin adopts a secret an external backend keeps, the host reader serves it
from there, moves it back and forth, and the remote secret is never deleted. When the
backend's feature ends the value stays readable while a new adoption is refused. The
fake ``acme.vault`` backend stands in for a platform module. Every test runs in a
transaction that is rolled back.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest

from core.application.management.scope import ScopeContext
from core.application.secrets.backend_service import SecretBackendAdminService
from core.application.secrets.service import SecretCatalogService
from core.application.settings.platform_contracts import FeatureNotEntitledError
from core.infrastructure.security import secret_backends, stored_secrets
from core.infrastructure.security.secret_backends import (
    SecretBackendContext,
    SecretBackendHealth,
    SecretBackendPort,
)

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "db/migrations/20261126200000_secret_backends.sql"
TENANT_ID = "00000000-0000-0000-0000-000000000001"
FEATURE = "acme.secret_backend.vault"


class AcmeVault:
    kind = "acme.vault"
    feature = FEATURE
    capabilities = frozenset({"write", "delete"})

    def __init__(self) -> None:
        self.values = {"db-password": "remote-s3cr3t"}
        self.deleted: list[Any] = []

    def read(self, context: SecretBackendContext, locator: Mapping[str, Any]) -> str:
        if locator.get("name") not in self.values:
            raise stored_secrets.StoredSecretMissing("")
        return self.values[str(locator["name"])]

    def health(self, context: SecretBackendContext) -> SecretBackendHealth:
        return SecretBackendHealth(healthy=True)

    def write(
        self, context: SecretBackendContext, locator: Mapping[str, Any], value: str
    ) -> Mapping[str, Any]:
        self.values[str(locator["name"])] = value
        return dict(locator)

    def delete(self, context: SecretBackendContext, locator: Mapping[str, Any]) -> None:
        self.deleted.append(dict(locator))


class _Entitlements:
    def __init__(self) -> None:
        self.features = {FEATURE}

    def is_entitled(self, account_id: str, feature: str) -> bool:
        return feature in self.features


def _id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def db(disposable_db_conn, monkeypatch) -> Iterator[dict[str, Any]]:
    monkeypatch.setenv("ENCRYPTION_KEY", "unit-test-encryption-key")
    conn = disposable_db_conn
    checkpoint = secret_backends.loading_checkpoint()
    secret_backends.restore_loading_checkpoint(None)
    vault = AcmeVault()
    port = SecretBackendPort()
    with port.platform_module_scope("acme.vault-module", ["secret_backend"]):
        port.register_secret_backend(vault)

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        with conn.transaction(), conn.cursor() as cur:
            yield conn, cur

    monkeypatch.setattr(SecretBackendAdminService, "_transaction", transaction)
    monkeypatch.setattr(SecretCatalogService, "_transaction", transaction)
    monkeypatch.setattr(SecretBackendAdminService, "_consume_probe", lambda *_a, **_k: None)
    entitlements = _Entitlements()
    try:
        with conn.transaction(force_rollback=True):
            account_id = _id()
            # ADR 0066 amendment 2026-11-27: a second account uses an external
            # backend only where the deployment's policy allows it.
            monkeypatch.setenv(
                "FS_SECRET_BACKEND_POLICY",
                json.dumps({account_id: {AcmeVault.kind: {"allowed_values": {}}}}),
            )
            suffix = uuid.uuid4().hex[:8]
            conn.execute(
                """INSERT INTO accounts (id, tenant_id, account_key, display_name, is_active)
                   VALUES (%s, %s, %s, %s, true)""",
                (account_id, TENANT_ID, f"vault-{suffix}", f"Vault {suffix}"),
            )
            yield {
                "conn": conn,
                "account_id": account_id,
                "vault": vault,
                "entitlements": entitlements,
                "admin": ScopeContext(
                    account_id=account_id,
                    principal_type="admin",
                    auth_source="local",
                    actor_id="ana",
                ),
                "service": SecretBackendAdminService(
                    lambda: object(), entitlement_service=lambda: entitlements
                ),
                "catalog": SecretCatalogService(
                    lambda: object(), entitlement_service=lambda: entitlements
                ),
            }
    finally:
        secret_backends.restore_loading_checkpoint(checkpoint)


def _read(db: dict[str, Any], ref_key: str) -> str:
    secret_backends.clear_cache()
    return stored_secrets.stored_secret_reader(db["conn"]).read(
        stored_secrets.SecretReadContext.for_account(db["account_id"]), ref_key
    )


def _scalar(conn: Any, sql: str, params: tuple[Any, ...]) -> Any:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return row[0] if row else None


def test_the_migration_is_idempotent(db) -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    db["conn"].execute(sql)
    db["conn"].execute(sql)


def test_an_adopted_secret_is_read_from_its_backend_and_survives_the_feature(db) -> None:
    service, admin = db["service"], db["admin"]
    config = service.create_config(admin, backend="acme.vault", name="Main", config={"v": 1})
    adopted = service.adopt_secret(
        admin, name="DB password", config_id=config["id"], locator={"name": "db-password"}
    )
    ref_key = _scalar(
        db["conn"], "SELECT storage_ref_key FROM catalog_secrets WHERE id = %s", (adopted["id"],)
    )

    assert "remote-s3cr3t" not in repr(adopted)
    assert (
        _scalar(db["conn"], "SELECT count(*) FROM secret_refs WHERE ref_key = %s", (ref_key,)) == 0
    )
    assert _read(db, ref_key) == "remote-s3cr3t"

    # The feature ends: the value stays readable, a new adoption is refused.
    db["entitlements"].features.clear()
    assert _read(db, ref_key) == "remote-s3cr3t"
    with pytest.raises(FeatureNotEntitledError):
        service.adopt_secret(
            admin, name="Another", config_id=config["id"], locator={"name": "db-password"}
        )

    # Turned off: fail closed, never a local fallback.
    service.update_config(admin, config["id"], enabled=False)
    with pytest.raises(stored_secrets.StoredSecretStorageRefused):
        _read(db, ref_key)


def test_a_value_moves_back_and_forth_and_the_remote_secret_is_never_deleted(db) -> None:
    service, admin, vault = db["service"], db["admin"], db["vault"]
    config = service.create_config(admin, backend="acme.vault", name="Main", config={})
    adopted = service.adopt_secret(
        admin, name="DB password", config_id=config["id"], locator={"name": "db-password"}
    )
    ref_key = _scalar(
        db["conn"], "SELECT storage_ref_key FROM catalog_secrets WHERE id = %s", (adopted["id"],)
    )

    service.move_to_local(admin, adopted["id"])

    assert (
        _scalar(
            db["conn"],
            "SELECT count(*) FROM secret_backend_locators WHERE ref_key = %s",
            (ref_key,),
        )
        == 0
    )
    assert _read(db, ref_key) == "remote-s3cr3t"
    assert vault.deleted == [] and vault.values["db-password"] == "remote-s3cr3t"

    db["catalog"].replace_value(adopted["id"], admin, value="rotated-locally")
    service.move_to_backend(admin, adopted["id"], config_id=config["id"], locator={"name": "moved"})

    assert vault.values["moved"] == "rotated-locally"
    assert (
        _scalar(db["conn"], "SELECT count(*) FROM secret_refs WHERE ref_key = %s", (ref_key,)) == 0
    )
    assert _read(db, ref_key) == "rotated-locally"

    db["catalog"].delete_secret(adopted["id"], admin)

    assert (
        _scalar(
            db["conn"],
            "SELECT count(*) FROM secret_backend_locators WHERE ref_key = %s",
            (ref_key,),
        )
        == 0
    )
    assert vault.deleted == [] and vault.values["moved"] == "rotated-locally"


def test_a_locator_names_a_configuration_of_its_own_kind(db) -> None:
    service, admin, conn = db["service"], db["admin"], db["conn"]
    config = service.create_config(admin, backend="acme.vault", name="Main", config={})

    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        conn.execute(
            """INSERT INTO secret_backend_locators
                   (account_id, ref_key, backend_config_id, backend_kind, locator)
               VALUES (%s, 'catalog_secret:x', %s, 'acme.other', '{}'::jsonb)""",
            (db["account_id"], config["id"]),
        )
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute(
            """INSERT INTO secret_backend_configs (account_id, backend_kind, name)
               VALUES (%s, 'local', 'Local')""",
            (db["account_id"],),
        )
