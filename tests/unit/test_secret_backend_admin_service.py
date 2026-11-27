"""Account administration of secret backends (ADR 0066 amendment 2026-11-26).

An account admin configures a backend a platform module registered, adopts a secret
that already exists there, and moves a catalog secret's value there and back.
Entitlement gates every new use; values already kept remotely stay readable and can
always come back; moving back never deletes the remote secret and no answer carries
a value. The fake ``acme.vault`` backend stands in for a module, an in-memory store
for the database.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import pytest

from core.application.management.errors import (
    ServiceConflictError,
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServiceUnavailableError,
    ServiceValidationError,
)
from core.application.management.scope import ScopeContext
from core.application.secrets import backend_repository, backend_service, repository
from core.application.secrets.backend_service import SecretBackendAdminService
from core.application.secrets.service import SecretCatalogService
from core.application.settings import model_provider_verification_rate_limit as rate_limit
from core.application.settings.platform_contracts import (
    FEATURE_NOT_ENTITLED_CODE,
    FeatureNotEntitledError,
)
from core.infrastructure.db import model_provider_store
from core.infrastructure.security import secret_backends, stored_secrets
from core.infrastructure.security.secret_backends import (
    SecretBackendContext,
    SecretBackendHealth,
    SecretBackendPort,
)

ACCOUNT = "11111111-1111-1111-1111-111111111111"
FEATURE = "acme.secret_backend.vault"
REMOTE_VALUE = "remote-s3cr3t"


class AcmeVault:
    kind = "acme.vault"
    feature = FEATURE
    capabilities = frozenset({"write", "delete"})

    def __init__(self) -> None:
        self.values: dict[str, str] = {"db-password": REMOTE_VALUE}
        self.deleted: list[dict[str, Any]] = []
        self.writes: list[tuple[dict[str, Any], str]] = []

    def validate_config(self, config: Mapping[str, Any]) -> Mapping[str, Any]:
        if "vault" not in config:
            raise ValueError("vault is required")
        return {**config, "vault": str(config["vault"]).lower()}

    def validate_locator(self, locator: Mapping[str, Any]) -> Mapping[str, Any]:
        if not str(locator.get("name") or ""):
            raise ValueError("name is required")
        return {"name": str(locator["name"])}

    def read(self, context: SecretBackendContext, locator: Mapping[str, Any]) -> str:
        name = str(locator["name"])
        if name not in self.values:
            raise stored_secrets.StoredSecretMissing(name)
        return self.values[name]

    def health(self, context: SecretBackendContext) -> SecretBackendHealth:
        return SecretBackendHealth(healthy=context.config.get("vault") == "main", code="")

    def write(
        self, context: SecretBackendContext, locator: Mapping[str, Any], value: str
    ) -> Mapping[str, Any]:
        self.writes.append((dict(locator), value))
        self.values[str(locator["name"])] = value
        return {**locator, "version": str(len(self.writes))}

    def delete(self, context: SecretBackendContext, locator: Mapping[str, Any]) -> None:
        self.deleted.append(dict(locator))


class ReadOnlyVault(AcmeVault):
    kind = "acme.readonly"
    feature = "acme.secret_backend.readonly"
    capabilities = frozenset()


class _Store:
    """The rows the service reads and writes, keyed like the tables."""

    def __init__(self) -> None:
        self.configs: dict[str, dict[str, Any]] = {}
        self.locators: dict[str, dict[str, Any]] = {}
        self.secrets: dict[str, dict[str, Any]] = {}
        self.local: dict[str, str] = {}
        self.audit: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        self.probes: dict[str, int] = {}


class _Entitlements:
    def __init__(self, *features: str) -> None:
        self.features = set(features)

    def is_entitled(self, account_id: str, feature: str) -> bool:
        return account_id == ACCOUNT and feature in self.features


@pytest.fixture(autouse=True)
def _policy_allows_the_test_account(monkeypatch):
    # ADR 0066 amendment 2026-11-27: a second account uses an external backend only
    # where the deployment's policy allows it.
    monkeypatch.setenv(
        "FS_SECRET_BACKEND_POLICY",
        json.dumps(
            {
                ACCOUNT: {
                    "acme.vault": {"allowed_values": {}},
                    "acme.readonly": {"allowed_values": {}},
                    "acme.labelled": {"allowed_values": {}},
                }
            }
        ),
    )


@pytest.fixture(autouse=True)
def _clean_registry():
    checkpoint = secret_backends.loading_checkpoint()
    secret_backends.restore_loading_checkpoint(None)
    yield
    secret_backends.restore_loading_checkpoint(checkpoint)


@pytest.fixture
def vault() -> AcmeVault:
    backend = AcmeVault()
    port = SecretBackendPort()
    with port.platform_module_scope("acme.vault-module", ["secret_backend"]):
        port.register_secret_backend(backend)
        port.register_secret_backend(ReadOnlyVault())
    return backend


@pytest.fixture
def store(monkeypatch) -> _Store:
    data = _Store()

    def insert_config(cur, *, account_id, backend_kind, name, non_secret_config, enabled, actor):
        if any(row["name"] == name for row in data.configs.values()):
            import psycopg

            raise psycopg.errors.UniqueViolation("duplicate")
        row = {
            "id": str(uuid.uuid4()),
            "account_id": account_id,
            "backend_kind": backend_kind,
            "name": name,
            "non_secret_config": dict(non_secret_config),
            "enabled": enabled,
            "created_by": actor,
            "updated_by": actor,
        }
        data.configs[row["id"]] = row
        return dict(row)

    def get_config(cur, *, account_id, config_id, for_update=False):
        row = data.configs.get(config_id)
        return dict(row) if row and row["account_id"] == account_id else None

    def update_config(cur, *, account_id, config_id, name, non_secret_config, enabled, actor):
        row = data.configs[config_id]
        row.update(name=name, non_secret_config=dict(non_secret_config), enabled=enabled)
        return dict(row)

    def get_locator(cur, *, account_id, ref_key):
        row = data.locators.get(ref_key)
        return dict(row) if row else None

    def insert_locator(cur, *, account_id, ref_key, config_id, backend_kind, locator, actor):
        data.locators[ref_key] = {
            "ref_key": ref_key,
            "config_id": config_id,
            "backend_kind": backend_kind,
            "locator": dict(locator),
        }

    def update_locator(cur, *, account_id, ref_key, locator):
        data.locators[ref_key]["locator"] = dict(locator)

    def locate(conn, *, account_id, ref_key):
        row = data.locators.get(ref_key)
        if not row:
            return None
        config = data.configs[row["config_id"]]
        return {
            "backend_kind": row["backend_kind"],
            "config_id": row["config_id"],
            "locator": dict(row["locator"]),
            "config": dict(config["non_secret_config"]),
            "enabled": config["enabled"],
        }

    def insert_secret(cur, *, account_id, name, description, actor, storage_ref_key=None):
        secret_id = str(uuid.uuid4())
        row = {
            "id": secret_id,
            "account_id": account_id,
            "name": name,
            "description": description,
            "storage_ref_key": storage_ref_key or f"catalog_secret:{secret_id}",
        }
        data.secrets[secret_id] = row
        return dict(row)

    def get_secret(cur, *, account_id, secret_id, for_update=False):
        row = data.secrets.get(secret_id)
        return dict(row) if row and row["account_id"] == account_id else None

    def record_audit(cur, *, account_id, command_type, actor_kind, actor_id, target, payload):
        data.audit.append((command_type, dict(target), dict(payload or {})))

    class _Local:
        def __init__(self, conn, **_kwargs):
            pass

        def read(self, context, ref_key):
            if ref_key not in data.local:
                raise stored_secrets.StoredSecretMissing(ref_key)
            return data.local[ref_key]

    def upsert(conn, *, account_id, ref_key, encrypted_value):
        data.local[ref_key] = encrypted_value

    monkeypatch.setattr(backend_repository, "insert_config", insert_config)
    monkeypatch.setattr(backend_repository, "get_config", get_config)
    monkeypatch.setattr(backend_repository, "update_config", update_config)
    monkeypatch.setattr(
        backend_repository, "list_configs", lambda cur, *, account_id: list(data.configs.values())
    )
    monkeypatch.setattr(backend_repository, "list_catalog_locators", lambda cur, **_k: [])
    monkeypatch.setattr(backend_repository, "get_locator", get_locator)
    monkeypatch.setattr(backend_repository, "insert_locator", insert_locator)
    monkeypatch.setattr(backend_repository, "update_locator", update_locator)
    monkeypatch.setattr(
        backend_repository,
        "delete_locator",
        lambda cur, *, account_id, ref_key: data.locators.pop(ref_key, None),
    )
    monkeypatch.setattr(
        backend_repository,
        "delete_local_value",
        lambda cur, *, account_id, ref_key: data.local.pop(ref_key, None),
    )

    def locator_holder(cur, *, account_id, config_id, locator, exclude_ref_key=None):
        for ref_key, row in data.locators.items():
            if (
                ref_key != exclude_ref_key
                and row["config_id"] == config_id
                and all(row["locator"].get(k) == v for k, v in locator.items())
            ):
                return ref_key
        return None

    def consume(conn, *, keys, max_attempts, window_seconds, now=None, **_kwargs):
        for key in keys:
            data.probes[key] = data.probes.get(key, 0) + 1
            if data.probes[key] > max_attempts:
                return False, int(window_seconds)
        return True, 0

    monkeypatch.setattr(backend_repository, "locator_holder", locator_holder)
    monkeypatch.setattr(rate_limit, "consume_attempt_window", consume)
    monkeypatch.setattr(secret_backends, "locate", locate)
    monkeypatch.setattr(repository, "insert_secret", insert_secret)
    monkeypatch.setattr(repository, "get_secret", get_secret)
    monkeypatch.setattr(repository, "touch_value", lambda cur, **_k: None)
    monkeypatch.setattr(repository, "record_audit", record_audit)
    monkeypatch.setattr(repository, "bindings_for_secret", lambda cur, **_k: [])
    monkeypatch.setattr(
        repository,
        "delete_secret",
        lambda cur, *, account_id, secret_id, storage_ref_key: (
            data.secrets.pop(secret_id, None),
            data.local.pop(storage_ref_key, None),
        ),
    )
    monkeypatch.setattr(backend_service, "LocalStoredSecretReader", _Local)
    monkeypatch.setattr(model_provider_store, "upsert_secret_ref", upsert)

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        yield object(), object()

    monkeypatch.setattr(SecretBackendAdminService, "_transaction", transaction)
    monkeypatch.setattr(SecretCatalogService, "_transaction", transaction)
    return data


def _admin(**fields: Any) -> ScopeContext:
    return ScopeContext(
        account_id=ACCOUNT,
        principal_type="admin",
        auth_source="local",
        actor_id="ana",
        **fields,
    )


def _service(*features: str) -> SecretBackendAdminService:
    entitlements = _Entitlements(*features)
    return SecretBackendAdminService(
        lambda: object(), entitlement_service=lambda: entitlements, encrypt=lambda v: f"enc:{v}"
    )


def _config(service: SecretBackendAdminService, **overrides: Any) -> dict[str, Any]:
    payload = {"backend": "acme.vault", "name": "Main vault", "config": {"vault": "MAIN"}}
    payload.update(overrides)
    return service.create_config(_admin(), **payload)


# -- who may configure ---------------------------------------------------------------


@pytest.mark.parametrize(
    "scope",
    [
        ScopeContext(account_id=ACCOUNT, principal_type="operator", auth_source="local"),
        ScopeContext(
            account_id=ACCOUNT,
            principal_type="admin",
            auth_source="pat",
            token_scopes=frozenset({"admin:all"}),
        ),
    ],
)
def test_only_an_interactive_admin_configures_secret_storage(vault, store, scope) -> None:
    with pytest.raises(ServiceForbiddenError):
        _service(FEATURE).create_config(scope, backend="acme.vault", name="x", config={})
    assert store.configs == {}


def test_a_project_limited_admin_cannot_reach_account_storage(vault, store) -> None:
    with pytest.raises(ServiceNotFoundError):
        _service(FEATURE).overview(_admin(allowed_project_ids=frozenset({"p1"})))


# -- configurations ------------------------------------------------------------------


def test_a_configuration_is_validated_by_its_backend_and_audited(vault, store) -> None:
    view = _config(_service(FEATURE))

    assert view["backend"] == "acme.vault" and view["installed"] is True
    assert view["config"] == {"vault": "main"}
    assert store.audit == [
        ("secret.storage_config_create", {"config_id": view["id"]}, {"backend": "acme.vault"})
    ]


def test_a_configuration_needs_the_backend_s_feature(vault, store) -> None:
    with pytest.raises(FeatureNotEntitledError) as refused:
        _config(_service())

    assert refused.value.payload["code"] == FEATURE_NOT_ENTITLED_CODE
    assert refused.value.payload["details"]["feature"] == FEATURE
    assert store.configs == {}


def test_a_backend_that_is_not_installed_cannot_be_configured(store) -> None:
    with pytest.raises(ServiceUnavailableError):
        _config(_service(FEATURE))


@pytest.mark.parametrize(
    "config",
    [{"vault": "main", "client_secret": "x"}, {"vault": "main", "auth": {"token": "x"}}],
)
def test_a_configuration_never_holds_a_credential(vault, store, config) -> None:
    with pytest.raises(ServiceValidationError, match="credentials come from the deployment"):
        _config(_service(FEATURE), config=config)


def test_a_bad_configuration_is_refused_with_the_backend_s_reason(vault, store) -> None:
    with pytest.raises(ServiceValidationError, match="vault is required"):
        _config(_service(FEATURE), config={})


def test_turning_off_or_renaming_needs_no_entitlement_but_turning_on_does(vault, store) -> None:
    config = _config(_service(FEATURE))
    lapsed = _service()

    off = lapsed.update_config(_admin(), config["id"], enabled=False, name="Old vault")
    assert off["enabled"] is False and off["name"] == "Old vault"
    with pytest.raises(FeatureNotEntitledError):
        lapsed.update_config(_admin(), config["id"], enabled=True)
    with pytest.raises(FeatureNotEntitledError):
        lapsed.update_config(_admin(), config["id"], config={"vault": "other"})

    on = _service(FEATURE).update_config(_admin(), config["id"], enabled=True)
    assert on["enabled"] is True


def test_health_reports_a_neutral_state(vault, store) -> None:
    service = _service(FEATURE)
    config = _config(service)

    assert service.config_health(_admin(), config["id"]) == {
        "config_id": config["id"],
        "healthy": True,
        "code": "",
    }
    service.update_config(_admin(), config["id"], enabled=False)
    assert service.config_health(_admin(), config["id"])["code"] == "turned_off"


def test_the_overview_lists_installed_kinds_with_their_entitlement(vault, store) -> None:
    overview = _service(FEATURE).overview(_admin())

    assert overview["backends"] == [
        {
            "kind": "acme.readonly",
            "label": "Acme readonly",
            "config_fields": [],
            "feature": "acme.secret_backend.readonly",
            "capabilities": [],
            "entitled": False,
        },
        {
            "kind": "acme.vault",
            "label": "Acme vault",
            "config_fields": [],
            "feature": FEATURE,
            "capabilities": ["delete", "write"],
            "entitled": True,
        },
    ]


class LabelledVault(AcmeVault):
    kind = "acme.labelled"
    label = "Company vault"

    def config_fields(self):
        return [
            {"key": "vault", "label": "Vault name", "required": True, "help": "As shown there."},
            {
                "key": "region",
                "type": "select",
                "label": "Region",
                "options": ["eu", {"value": "us", "label": "United States"}],
                "default": "eu",
                "advanced": True,
            },
            {
                "key": "path_prefix",
                "label": "Path prefix",
                "visible_when": {"field": "region", "contains": "us"},
            },
        ]


def test_the_overview_carries_the_backend_label_and_its_config_form(store) -> None:
    port = SecretBackendPort()
    with port.platform_module_scope("acme.vault-module", ["secret_backend"]):
        port.register_secret_backend(LabelledVault())

    service = _service(FEATURE)
    backend = service.overview(_admin())["backends"][0]

    assert backend["label"] == "Company vault"
    assert backend["config_fields"] == [
        {
            "key": "vault",
            "type": "text",
            "label": "Vault name",
            "required": True,
            "advanced": False,
            "help": "As shown there.",
        },
        {
            "key": "region",
            "type": "select",
            "label": "Region",
            "required": False,
            "advanced": True,
            "options": [
                {"value": "eu", "label": "eu"},
                {"value": "us", "label": "United States"},
            ],
            "default": "eu",
        },
        {
            "key": "path_prefix",
            "type": "text",
            "label": "Path prefix",
            "required": False,
            "advanced": False,
            "visible_when": {"field": "region", "contains": "us"},
        },
    ]
    config = service.create_config(
        _admin(), backend="acme.labelled", name="Main", config={"vault": "Main"}
    )
    assert config["backend_label"] == "Company vault"


def test_a_configuration_whose_module_is_gone_keeps_a_readable_kind(vault, store) -> None:
    service = _service(FEATURE)
    config = _config(service)
    secret_backends.restore_loading_checkpoint(None)

    listed = service.overview(_admin())["configs"]

    assert listed[0]["id"] == config["id"]
    assert listed[0]["backend_label"] == "Acme vault"
    assert listed[0]["installed"] is False


# -- adopting a remote secret ----------------------------------------------------------


def test_adopting_checks_the_remote_value_and_never_returns_it(vault, store) -> None:
    service = _service(FEATURE)
    config = _config(service)

    view = service.adopt_secret(
        _admin(), name="DB password", config_id=config["id"], locator={"name": "db-password"}
    )

    assert REMOTE_VALUE not in repr(view)
    assert view["storage"] == {"backend": "acme.vault", "config_id": config["id"]}
    ref_key = store.secrets[view["id"]]["storage_ref_key"]
    assert store.locators[ref_key]["locator"] == {"name": "db-password"}
    assert ref_key not in store.local
    assert store.audit[-1][0] == "secret.storage_adopt"
    assert REMOTE_VALUE not in repr(store.audit)


def test_adopting_a_location_that_holds_nothing_creates_nothing(vault, store) -> None:
    service = _service(FEATURE)
    config = _config(service)

    with pytest.raises(ServiceValidationError, match="No secret exists"):
        service.adopt_secret(
            _admin(), name="Nope", config_id=config["id"], locator={"name": "missing"}
        )
    assert store.secrets == {} and store.locators == {}


def test_adopting_needs_the_feature_and_a_configuration_that_is_on(vault, store) -> None:
    config = _config(_service(FEATURE))

    with pytest.raises(FeatureNotEntitledError):
        _service().adopt_secret(
            _admin(), name="DB", config_id=config["id"], locator={"name": "db-password"}
        )
    _service(FEATURE).update_config(_admin(), config["id"], enabled=False)
    with pytest.raises(ServiceConflictError):
        _service(FEATURE).adopt_secret(
            _admin(), name="DB", config_id=config["id"], locator={"name": "db-password"}
        )
    assert store.secrets == {}


# -- moving a value there and back -----------------------------------------------------


def _local_secret(store: _Store, value: str = "local-value") -> dict[str, Any]:
    secret_id = str(uuid.uuid4())
    row = {
        "id": secret_id,
        "account_id": ACCOUNT,
        "name": "API key",
        "description": "",
        "storage_ref_key": f"catalog_secret:{secret_id}",
    }
    store.secrets[secret_id] = row
    store.local[row["storage_ref_key"]] = value
    return row


def test_moving_a_value_writes_it_remotely_and_drops_the_local_copy(vault, store) -> None:
    service = _service(FEATURE)
    config = _config(service)
    secret = _local_secret(store)

    result = service.move_to_backend(
        _admin(), secret["id"], config_id=config["id"], locator={"name": "api-key"}
    )

    ref_key = secret["storage_ref_key"]
    assert result == {
        "secret_id": secret["id"],
        "storage": {"backend": "acme.vault", "config_id": config["id"]},
    }
    assert vault.values["api-key"] == "local-value"
    assert store.locators[ref_key]["locator"] == {"name": "api-key", "version": "1"}
    assert ref_key not in store.local
    assert store.audit[-1][2] == {
        "to": "acme.vault",
        "config_id": config["id"],
        "replace": False,
    }


def test_a_backend_without_writes_takes_no_moved_value(vault, store) -> None:
    service = _service(FEATURE, "acme.secret_backend.readonly")
    config = _config(service, backend="acme.readonly", name="Read only")
    secret = _local_secret(store)

    with pytest.raises(ServiceValidationError, match="adopt"):
        service.move_to_backend(
            _admin(), secret["id"], config_id=config["id"], locator={"name": "x"}
        )
    assert secret["storage_ref_key"] in store.local


def test_moving_a_value_needs_the_feature(vault, store) -> None:
    config = _config(_service(FEATURE))
    secret = _local_secret(store)

    with pytest.raises(FeatureNotEntitledError):
        _service().move_to_backend(
            _admin(), secret["id"], config_id=config["id"], locator={"name": "x"}
        )
    assert vault.writes == []


def test_moving_back_needs_no_feature_and_never_deletes_the_remote_secret(vault, store) -> None:
    service = _service(FEATURE)
    config = _config(service)
    adopted = service.adopt_secret(
        _admin(), name="DB", config_id=config["id"], locator={"name": "db-password"}
    )
    ref_key = store.secrets[adopted["id"]]["storage_ref_key"]
    # The feature ended and the configuration was turned off: the data still comes back.
    lapsed = _service()
    lapsed.update_config(_admin(), config["id"], enabled=False)

    result = lapsed.move_to_local(_admin(), adopted["id"])

    assert result == {"secret_id": adopted["id"], "storage": {"backend": "local"}}
    assert store.local[ref_key] == f"enc:{REMOTE_VALUE}"
    assert ref_key not in store.locators
    assert vault.deleted == []
    assert vault.values["db-password"] == REMOTE_VALUE
    with pytest.raises(ServiceConflictError, match="already kept locally"):
        lapsed.move_to_local(_admin(), adopted["id"])


# -- the catalog's own value replace and delete ----------------------------------------


def _catalog(*features: str) -> SecretCatalogService:
    entitlements = _Entitlements(*features)
    return SecretCatalogService(lambda: object(), entitlement_service=lambda: entitlements)


def test_replacing_a_remote_value_writes_it_to_the_backend(vault, store) -> None:
    service = _service(FEATURE)
    config = _config(service)
    secret = _local_secret(store)
    service.move_to_backend(
        _admin(), secret["id"], config_id=config["id"], locator={"name": "api-key"}
    )

    _catalog(FEATURE).replace_value(secret["id"], _admin(), value="rotated")

    assert vault.values["api-key"] == "rotated"
    assert store.locators[secret["storage_ref_key"]]["locator"]["version"] == "2"
    assert secret["storage_ref_key"] not in store.local
    with pytest.raises(FeatureNotEntitledError):
        _catalog().replace_value(secret["id"], _admin(), value="again")
    assert vault.values["api-key"] == "rotated"


def test_a_remote_value_a_backend_cannot_write_is_never_replaced_locally(vault, store) -> None:
    service = _service(FEATURE, "acme.secret_backend.readonly")
    config = _config(service, backend="acme.readonly", name="Read only")
    adopted = service.adopt_secret(
        _admin(), name="DB", config_id=config["id"], locator={"name": "db-password"}
    )

    with pytest.raises(ServiceConflictError, match="move it back"):
        _catalog(FEATURE, "acme.secret_backend.readonly").replace_value(
            adopted["id"], _admin(), value="x"
        )
    assert store.local == {}


def test_deleting_a_remote_secret_forgets_its_locator_only(vault, store) -> None:
    service = _service(FEATURE)
    config = _config(service)
    adopted = service.adopt_secret(
        _admin(), name="DB", config_id=config["id"], locator={"name": "db-password"}
    )

    _catalog().delete_secret(adopted["id"], _admin())

    assert store.locators == {}
    assert vault.deleted == []
    assert vault.values["db-password"] == REMOTE_VALUE
