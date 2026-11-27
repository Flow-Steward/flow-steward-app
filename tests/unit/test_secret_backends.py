"""Secret backends behind the stored-secret reader (ADR 0066 amendment 2026-11-26).

A platform module registers a backend through Core's port; the reader stays local
for every reference without a locator, reads a located one from its backend and
never falls back between the two. The fake ``acme.vault`` backend stands in for a
module.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest

from core.infrastructure.platform_modules import loader, registration_rollback
from core.infrastructure.security import secret_backends, secret_codec, stored_secrets
from core.infrastructure.security.secret_backends import (
    SecretBackendContext,
    SecretBackendHealth,
    SecretBackendPort,
    SecretBackendRefusedError,
)

ACCOUNT = "11111111-1111-1111-1111-111111111111"
REF = "catalog_secret:s1"
FEATURE = "acme.secret_backend.vault"


class AcmeVault:
    kind = "acme.vault"
    feature = FEATURE
    capabilities = frozenset({"write"})

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})
        self.reads: list[tuple[SecretBackendContext, dict[str, Any]]] = []
        self.error: Exception | None = None

    def read(self, context: SecretBackendContext, locator: Mapping[str, Any]) -> str:
        self.reads.append((context, dict(locator)))
        if self.error is not None:
            raise self.error
        name = str(locator.get("name") or "")
        if name not in self.values:
            raise stored_secrets.StoredSecretMissing(name)
        return self.values[name]

    def health(self, context: SecretBackendContext) -> SecretBackendHealth:
        return SecretBackendHealth(healthy=True)

    def write(
        self, context: SecretBackendContext, locator: Mapping[str, Any], value: str
    ) -> Mapping[str, Any]:
        self.values[str(locator["name"])] = value
        return {**locator, "version": "1"}


class _Conn:
    """Answers the locator lookup and the local value lookup from two dictionaries."""

    def __init__(
        self,
        *,
        locators: dict[str, tuple[Any, ...]] | None = None,
        ciphertexts: dict[str, str] | None = None,
        fail_lookup: bool = False,
    ) -> None:
        self.locators = dict(locators or {})
        self.ciphertexts = dict(ciphertexts or {})
        self.fail_lookup = fail_lookup
        self.statements: list[str] = []

    def cursor(self, *_args, **_kwargs):
        conn = self

        class _Cursor:
            result: Any = None

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def execute(self, query, params=()):
                text = " ".join(str(query).split())
                conn.statements.append(text)
                if "FROM secret_backend_locators" in text:
                    if conn.fail_lookup:
                        raise RuntimeError("relation at https://vault.acme.test/x failed")
                    self.result = conn.locators.get(params[1])
                elif "FROM secret_refs" in text:
                    value = conn.ciphertexts.get(params[1])
                    self.result = (value,) if value else None

            def fetchone(self):
                return self.result

        return _Cursor()


def _located(
    name: str = "db-password",
    *,
    enabled: bool = True,
    kind: str = "acme.vault",
    config: dict[str, Any] | None = None,
) -> tuple[Any, ...]:
    return (kind, "cfg-1", {"name": name}, config or {"vault": "main"}, enabled)


def _context() -> stored_secrets.SecretReadContext:
    return stored_secrets.SecretReadContext.for_account(ACCOUNT)


@pytest.fixture(autouse=True)
def _policy_allows_the_test_account(monkeypatch):
    # ADR 0066 amendment 2026-11-27: a second account uses an external backend only
    # where the deployment's policy allows it.
    monkeypatch.setenv(
        "FS_SECRET_BACKEND_POLICY",
        json.dumps(
            {
                ACCOUNT: {"acme.vault": {"allowed_values": {}}},
                "22222222-0000-0000-0000-0": {"acme.vault": {"allowed_values": {}}},
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
    backend = AcmeVault({"db-password": "s3cr3t-value"})
    port = SecretBackendPort()
    with port.platform_module_scope("acme.vault-module", ["secret_backend"]):
        port.register_secret_backend(backend)
    return backend


@pytest.fixture
def local_values(monkeypatch) -> None:
    monkeypatch.setattr(secret_codec, "decrypt_secret", lambda value: f"local:{value}")


# -- registration --------------------------------------------------------------------


def test_without_a_backend_the_reader_is_local_and_never_looks_for_a_locator(
    local_values,
) -> None:
    conn = _Conn(ciphertexts={REF: "enc"})

    reader = stored_secrets.stored_secret_reader(conn)

    assert isinstance(reader, stored_secrets.LocalStoredSecretReader)
    assert reader.read(_context(), REF) == "local:enc"
    assert not any("secret_backend_locators" in item for item in conn.statements)


def test_a_module_that_declares_the_capability_registers_a_backend(vault) -> None:
    assert secret_backends.registered_backend("acme.vault") is vault
    assert secret_backends.backend_owner("acme.vault") == "acme.vault-module"
    assert isinstance(
        stored_secrets.stored_secret_reader(_Conn()), secret_backends.RoutedStoredSecretReader
    )


@pytest.mark.parametrize(
    ("module_id", "provides"),
    [
        ("acme.other", []),
        ("acme.other", ["entitlement_source"]),
        (None, ["secret_backend"]),
    ],
)
def test_a_registration_without_the_declared_capability_is_refused(module_id, provides) -> None:
    port = SecretBackendPort()

    with (
        port.platform_module_scope(module_id, provides),
        pytest.raises(SecretBackendRefusedError),
    ):
        port.register_secret_backend(AcmeVault())
    assert secret_backends.registered_backends() == ()


def test_a_registration_outside_loading_or_after_the_seal_is_refused() -> None:
    port = SecretBackendPort()
    with pytest.raises(SecretBackendRefusedError):
        port.register_secret_backend(AcmeVault())
    port.seal()
    with (
        port.platform_module_scope("acme.vault-module", ["secret_backend"]),
        pytest.raises(SecretBackendRefusedError),
    ):
        port.register_secret_backend(AcmeVault())


def test_a_kind_is_registered_once_and_local_is_core_s_own(vault) -> None:
    port = SecretBackendPort()
    local = AcmeVault()
    local.kind = "local"
    with port.platform_module_scope("acme.second", ["secret_backend"]):
        with pytest.raises(SecretBackendRefusedError):
            port.register_secret_backend(AcmeVault())
        with pytest.raises(ValueError):
            port.register_secret_backend(local)


def test_a_capability_needs_its_method() -> None:
    class NoWrite(AcmeVault):
        kind = "acme.nowrite"
        write = None  # type: ignore[assignment]

    port = SecretBackendPort()
    with (
        port.platform_module_scope("acme.vault-module", ["secret_backend"]),
        pytest.raises(TypeError),
    ):
        port.register_secret_backend(NoWrite())


@pytest.mark.parametrize(
    "fields",
    [
        "vault",
        [{"label": "No key"}],
        [{"key": "vault"}],
        [{"key": "Vault", "label": "Upper case"}],
        [{"key": "api_key", "label": "A credential"}],
        [{"key": "client_secret", "label": "A credential"}],
        [{"key": "token", "label": "Pasted", "type": "secret"}],
        [{"key": "vault", "label": "Vault", "type": "json"}],
        [{"key": "region", "label": "Region", "type": "select"}],
        [{"key": "region", "label": "Region", "type": "select", "options": ["eu", "eu"]}],
        [{"key": "vault", "label": "Vault", "options": ["eu"]}],
        [{"key": "on", "label": "On", "type": "checkbox", "default": "yes"}],
        [{"key": "vault", "label": "Vault"}, {"key": "vault", "label": "Again"}],
        [{"key": "vault", "label": "Vault", "visible_when": {"field": "nope", "contains": "x"}}],
        [{"key": "vault", "label": "Vault", "visible_when": {"field": "vault", "contains": "x"}}],
    ],
)
def test_a_config_form_core_cannot_draw_fails_the_registration(fields) -> None:
    class Formed(AcmeVault):
        kind = "acme.formed"

        def config_fields(self):
            return fields

    port = SecretBackendPort()
    with (
        port.platform_module_scope("acme.vault-module", ["secret_backend"]),
        pytest.raises(ValueError),
    ):
        port.register_secret_backend(Formed())
    assert secret_backends.registered_backend("acme.formed") is None


def test_a_backend_without_a_form_or_label_gets_a_readable_kind(vault) -> None:
    assert secret_backends.backend_config_fields(vault) == ()
    assert secret_backends.backend_label(vault) == "Acme vault"


def test_a_backend_label_is_short_text() -> None:
    class Labelled(AcmeVault):
        kind = "acme.labelled"
        label = 42

    port = SecretBackendPort()
    with (
        port.platform_module_scope("acme.vault-module", ["secret_backend"]),
        pytest.raises(ValueError),
    ):
        port.register_secret_backend(Labelled())


def test_core_knows_the_capability_and_rolls_back_a_failed_module() -> None:
    assert "secret_backend" in loader.PLATFORM_MODULE_CAPABILITIES
    assert loader.parse_module_capabilities(["secret_backend"], where="t") == {"secret_backend"}
    assert (
        "core.infrastructure.security.secret_backends"
        in registration_rollback._CORE_GLOBAL_REGISTRY_MODULES
    )

    port = SecretBackendPort()
    container = SimpleNamespace(
        registries={}, service_factories={}, auth_helpers={}, metadata={}, secret_backends=port
    )
    module = SimpleNamespace(module_id="acme.vault-module", provides=frozenset({"secret_backend"}))
    rollback = loader.ModuleRegistrationRollback.capture(container)
    with rollback.loading_scope(module):
        port.register_secret_backend(AcmeVault())
    assert secret_backends.registered_backend("acme.vault") is not None

    rollback.restore()

    assert secret_backends.registered_backend("acme.vault") is None


def test_a_background_role_s_port_gets_the_loading_scope() -> None:
    port = SecretBackendPort()
    module = SimpleNamespace(module_id="acme.vault-module", provides=frozenset({"secret_backend"}))
    rollback = loader.ModuleRegistrationRollback.capture(services={"secret_backends": port})

    with rollback.loading_scope(module):
        port.register_secret_backend(AcmeVault())
    rollback.restore()

    assert secret_backends.registered_backends() == ()


# -- routing -------------------------------------------------------------------------


def test_a_reference_without_a_locator_is_read_locally(vault, local_values) -> None:
    conn = _Conn(ciphertexts={REF: "enc"})

    assert stored_secrets.stored_secret_reader(conn).read(_context(), REF) == "local:enc"
    assert vault.reads == []


def test_a_located_reference_is_read_from_its_backend(vault) -> None:
    conn = _Conn(locators={REF: _located()}, ciphertexts={REF: "stale-local"})

    value = stored_secrets.stored_secret_reader(conn).read(_context(), REF)

    assert value == "s3cr3t-value"
    context, locator = vault.reads[0]
    assert locator == {"name": "db-password"}
    assert context.account_id == ACCOUNT and context.config_id == "cfg-1"
    assert dict(context.config) == {"vault": "main"}
    remaining = context.remaining_seconds()
    assert remaining is not None and remaining <= secret_backends.DEFAULT_READ_SECONDS
    assert not any("FROM secret_refs" in item for item in conn.statements)


def test_a_prefetched_local_value_never_hides_a_remote_one(vault) -> None:
    conn = _Conn(locators={REF: _located()})

    reader = stored_secrets.stored_secret_reader(conn, prefetched_ciphertexts={REF: "enc"})

    assert reader.read(_context(), REF) == "s3cr3t-value"


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (stored_secrets.StoredSecretMissing("x"), stored_secrets.StoredSecretMissing),
        (stored_secrets.StoredSecretUnavailable("x"), stored_secrets.StoredSecretUnavailable),
        (
            stored_secrets.StoredSecretStorageRefused("x"),
            stored_secrets.StoredSecretStorageRefused,
        ),
        (
            RuntimeError("403 from https://vault.acme.test/v1/kv"),
            stored_secrets.StoredSecretUnavailable,
        ),
    ],
)
def test_a_failing_backend_fails_closed_without_falling_back(
    vault, local_values, raised, expected
) -> None:
    vault.error = raised
    conn = _Conn(locators={REF: _located()}, ciphertexts={REF: "enc"})

    with pytest.raises(expected) as caught:
        stored_secrets.stored_secret_reader(conn).read(_context(), REF)

    assert caught.value.ref_key == REF
    assert "vault.acme.test" not in str(caught.value)
    assert not any("FROM secret_refs" in item for item in conn.statements)


def test_a_turned_off_configuration_or_a_missing_module_refuses(vault) -> None:
    turned_off = _Conn(locators={REF: _located(enabled=False)})
    not_installed = _Conn(locators={REF: _located(kind="acme.other")})

    with pytest.raises(stored_secrets.StoredSecretStorageRefused):
        stored_secrets.stored_secret_reader(turned_off).read(_context(), REF)
    with pytest.raises(stored_secrets.StoredSecretStorageRefused):
        stored_secrets.stored_secret_reader(not_installed).read(_context(), REF)
    assert vault.reads == []


def test_a_failed_locator_lookup_is_unavailable_and_never_guesses_local(
    vault, local_values
) -> None:
    conn = _Conn(fail_lookup=True, ciphertexts={REF: "enc"})

    with pytest.raises(stored_secrets.StoredSecretUnavailable):
        stored_secrets.stored_secret_reader(conn).read(_context(), REF)


def test_a_value_that_is_not_text_is_refused(vault) -> None:
    vault.values["db-password"] = 42  # type: ignore[assignment]

    with pytest.raises(stored_secrets.StoredSecretStorageRefused):
        stored_secrets.stored_secret_reader(_Conn(locators={REF: _located()})).read(_context(), REF)


# -- the per-process cache --------------------------------------------------------------


def test_a_value_is_kept_for_the_cache_period_then_read_again(vault, monkeypatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(secret_backends.time, "monotonic", lambda: now[0])
    conn = _Conn(locators={REF: _located()})
    reader = stored_secrets.stored_secret_reader(conn)

    reader.read(_context(), REF)
    reader.read(_context(), REF)
    assert len(vault.reads) == 1

    now[0] += secret_backends.CACHE_SECONDS + 1
    reader.read(_context(), REF)
    assert len(vault.reads) == 2


def test_a_changed_locator_or_an_invalidation_is_never_a_stale_hit(vault) -> None:
    vault.values["other"] = "other-value"
    reader = stored_secrets.stored_secret_reader(_Conn(locators={REF: _located()}))
    reader.read(_context(), REF)

    moved = stored_secrets.stored_secret_reader(_Conn(locators={REF: _located("other")}))
    assert moved.read(_context(), REF) == "other-value"

    secret_backends.invalidate_cached_secret(ACCOUNT, REF)
    reader.read(_context(), REF)
    assert len(vault.reads) == 3


def test_accounts_never_share_a_cached_value(vault) -> None:
    conn = _Conn(locators={REF: _located()})
    reader = stored_secrets.stored_secret_reader(conn)
    reader.read(_context(), REF)
    reader.read(stored_secrets.SecretReadContext.for_account("22222222-0000-0000-0000-0"), REF)

    assert len(vault.reads) == 2


# -- audit ---------------------------------------------------------------------------


def test_reads_are_logged_by_reference_backend_and_outcome_only(vault, caplog) -> None:
    caplog.set_level(logging.INFO, logger=secret_backends.__name__)
    stored_secrets.stored_secret_reader(_Conn(locators={REF: _located()})).read(_context(), REF)
    vault.error = RuntimeError("denied for https://vault.acme.test with token abc")
    secret_backends.clear_cache()
    with pytest.raises(stored_secrets.StoredSecretUnavailable):
        stored_secrets.stored_secret_reader(_Conn(locators={REF: _located()})).read(_context(), REF)

    outcomes = [
        (record.secret_ref, record.secret_backend, record.outcome)
        for record in caplog.records
        if hasattr(record, "outcome")
    ]
    assert outcomes == [
        (REF, "acme.vault", "read"),
        (REF, "acme.vault", "secret_store_unavailable"),
    ]
    assert "s3cr3t-value" not in caplog.text
    assert "vault.acme.test" not in caplog.text
    assert "db-password" not in caplog.text
