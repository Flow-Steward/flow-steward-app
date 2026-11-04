"""An LLM provider install's secrets chosen from Registry → Secrets (ADR 0057).

The install keeps the storage key of each chosen secret, never a copy of its value,
so a value replaced in Registry → Secrets reaches the provider at its next use. Every
reader overlays the chosen values on the typed ones and fails closed when one is gone.
"""

from __future__ import annotations

import contextlib
import json
from types import SimpleNamespace

import pytest

from core.application.secrets import reference_consumers
from core.application.settings import llm_provider_catalog_refs as catalog_refs
from core.application.settings import llm_provider_connection_status as connection_status
from core.application.settings import llm_provider_extension_repository as store
from core.application.settings.llm_provider_extension_use_cases import (
    LlmProviderExtensionError,
)
from core.infrastructure.security import stored_secrets

_TYPED = "typed-key-value"  # pragma: allowlist secret - test sentinel
_STORED = "stored-key-value"  # pragma: allowlist secret - test sentinel
_KEY = "catalog_secret:sec-1"
_ADMIN = SimpleNamespace(
    account_id="acc-1", principal_type="admin", auth_source="local", actor_id="admin-1"
)


class _Cursor:
    def __init__(self, log: list) -> None:
        self.log = log
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params=None):
        self.log.append((" ".join(sql.split()), params))


class _Conn:
    def __init__(self) -> None:
        self.log: list = []

    def cursor(self, **_kwargs):
        return _Cursor(self.log)


def _saving(monkeypatch, *, install_config: dict, blob: dict | None, bound: dict | None = None):
    """save_credentials on an in-memory install; returns the use cases and what was written."""
    from tests.unit.test_llm_provider_catalog_ux import _saving as base_saving

    use_cases, stored = base_saving(monkeypatch, existing_config={})
    use_cases.conn = _Conn()
    monkeypatch.setattr(
        store,
        "lock_install_for_credential_rotation",
        lambda *_a, **_k: {"llm_provider_connection_config": {}, **install_config},
    )
    monkeypatch.setattr(
        store, "read_secret_ref_ciphertext", lambda *_a, **_k: json.dumps(blob) if blob else ""
    )
    monkeypatch.setattr("core.application.auth.secrets.encrypt_secret", lambda value: value)
    monkeypatch.setattr("core.application.auth.secrets.decrypt_secret", lambda value: value)
    stored.update({"blobs": [], "refs": [], "bound": [], "unbound": [], "audits": [], "names": []})
    monkeypatch.setattr(
        "core.infrastructure.db.model_provider_store.upsert_secret_ref",
        lambda *_a, encrypted_value, **_k: stored["blobs"].append(json.loads(encrypted_value)),
    )
    monkeypatch.setattr(
        connection_status,
        "record_saved_secrets",
        lambda *_a, names, **_k: stored["names"].append(sorted(names)),
    )
    monkeypatch.setattr(
        catalog_refs,
        "write_catalog_refs",
        lambda _cur, *, account_id, install_id, refs: stored["refs"].append(
            (account_id, install_id, dict(refs))
        ),
    )
    monkeypatch.setattr(reference_consumers, "bound_fields", lambda *_a, **_k: dict(bound or {}))

    def bind(_cur, scope, *, kind, consumer_id, field, secret_id):
        stored["bound"].append((scope.account_id, kind, consumer_id, field, secret_id))
        return f"catalog_secret:{secret_id}"

    monkeypatch.setattr(reference_consumers, "bind", bind)
    monkeypatch.setattr(
        reference_consumers,
        "unbind",
        lambda _cur, *, account_id, kind, consumer_id, field: (
            stored["unbound"].append((kind, consumer_id, field)) or True
        ),
    )
    monkeypatch.setattr(
        catalog_refs,
        "record_unbind_audit",
        lambda _cur, _scope, *, install_id, field: stored["audits"].append(field),
    )
    return use_cases, stored


def test_choosing_a_stored_secret_binds_it_and_drops_the_typed_copy(monkeypatch) -> None:
    use_cases, stored = _saving(
        monkeypatch, install_config={}, blob={"api_key": _TYPED, "other": "kept"}
    )

    result = use_cases.save_credentials(
        "pi_acme",
        "acc-1",
        secrets={},
        connection_config={},
        secret_choices={"api_key": "sec-1"},  # pragma: allowlist secret - test fixture
        scope=_ADMIN,
    )

    assert stored["bound"] == [("acc-1", "llm_provider", "pi_acme", "api_key", "sec-1")]
    assert stored["refs"] == [("acc-1", "pi_acme", {"api_key": _KEY})]
    # The blob keeps the other typed values and no copy of the chosen one.
    assert stored["blobs"] == [{"other": "kept"}]
    assert stored["names"] == [["api_key"]]
    assert stored["stale"] == ["credentials_changed"]
    assert result["verification_invalidated"] is True


def test_choosing_the_same_secret_again_changes_nothing(monkeypatch) -> None:
    use_cases, stored = _saving(
        monkeypatch,
        install_config={catalog_refs.CATALOG_REFS_KEY: {"api_key": _KEY}},
        blob=None,
        bound={"api_key": "sec-1"},  # pragma: allowlist secret - test fixture
    )

    result = use_cases.save_credentials(
        "pi_acme",
        "acc-1",
        secrets={},
        connection_config={},
        secret_choices={"api_key": "sec-1"},  # pragma: allowlist secret - test fixture
        scope=None,
    )

    assert stored["bound"] == [] and stored["refs"] == [] and stored["blobs"] == []
    assert stored["stale"] == []
    assert result["changed"] is False


def test_a_typed_value_stops_using_the_stored_secret(monkeypatch) -> None:
    use_cases, stored = _saving(
        monkeypatch,
        install_config={catalog_refs.CATALOG_REFS_KEY: {"api_key": _KEY}},
        blob=None,
        bound={"api_key": "sec-1"},  # pragma: allowlist secret - test fixture
    )

    use_cases.save_credentials(
        "pi_acme", "acc-1", secrets={"api_key": _TYPED}, connection_config={}, scope=_ADMIN
    )

    assert stored["unbound"] == [("llm_provider", "pi_acme", "api_key")]
    assert stored["audits"] == ["api_key"]
    assert stored["refs"] == [("acc-1", "pi_acme", {})]
    assert stored["blobs"] == [{"api_key": _TYPED}]


def test_an_empty_choice_stops_using_the_stored_secret(monkeypatch) -> None:
    use_cases, stored = _saving(
        monkeypatch,
        install_config={catalog_refs.CATALOG_REFS_KEY: {"api_key": _KEY}},
        blob=None,
        bound={"api_key": "sec-1"},  # pragma: allowlist secret - test fixture
    )

    result = use_cases.save_credentials(
        "pi_acme",
        "acc-1",
        secrets={},
        connection_config={},
        secret_choices={"api_key": ""},
        scope=_ADMIN,
    )

    assert stored["unbound"] == [("llm_provider", "pi_acme", "api_key")]
    assert stored["refs"] == [("acc-1", "pi_acme", {})]
    assert result["verification_invalidated"] is True


def test_a_typed_value_and_a_choice_for_one_secret_are_refused(monkeypatch) -> None:
    use_cases, stored = _saving(monkeypatch, install_config={}, blob=None)

    with pytest.raises(LlmProviderExtensionError) as exc:
        use_cases.save_credentials(
            "pi_acme",
            "acc-1",
            secrets={"api_key": _TYPED},
            connection_config={},
            secret_choices={"api_key": "sec-1"},  # pragma: allowlist secret - test fixture
            scope=_ADMIN,
        )

    assert exc.value.status_code == 422
    assert _TYPED not in exc.value.detail
    assert stored["bound"] == []


@pytest.mark.parametrize(
    "scope",
    [
        None,
        SimpleNamespace(account_id="acc-other", principal_type="admin", auth_source="local"),
        SimpleNamespace(account_id="acc-1", principal_type="viewer", auth_source="local"),
    ],
)
def test_binding_needs_bind_authority_in_the_install_account(monkeypatch, scope) -> None:
    use_cases, stored = _saving(monkeypatch, install_config={}, blob=None)

    with pytest.raises(LlmProviderExtensionError) as exc:
        use_cases.save_credentials(
            "pi_acme",
            "acc-1",
            secrets={},
            connection_config={},
            secret_choices={"api_key": "sec-1"},  # pragma: allowlist secret - test fixture
            scope=scope,
        )

    assert exc.value.status_code == 403
    assert stored["bound"] == [] and stored["refs"] == []


def test_a_choice_for_an_undeclared_secret_is_refused(monkeypatch) -> None:
    use_cases, stored = _saving(monkeypatch, install_config={}, blob=None)

    with pytest.raises(LlmProviderExtensionError) as exc:
        use_cases.save_credentials(
            "pi_acme",
            "acc-1",
            secrets={},
            connection_config={},
            secret_choices={"not_declared": "sec-1"},
            scope=_ADMIN,
        )

    assert exc.value.status_code == 422
    assert stored["bound"] == []


def test_a_chosen_secret_satisfies_a_required_secret(monkeypatch) -> None:
    from tests.unit.test_llm_provider_catalog_ux import _with_contract

    use_cases, stored = _saving(monkeypatch, install_config={}, blob=None)
    _with_contract(
        monkeypatch, use_cases, required_provider_secrets=["api_key"], optional_provider_secrets=[]
    )

    use_cases.save_credentials(
        "pi_acme",
        "acc-1",
        secrets={},
        connection_config={},
        secret_choices={"api_key": "sec-1"},  # pragma: allowlist secret - test fixture
        scope=_ADMIN,
    )

    assert stored["refs"] == [("acc-1", "pi_acme", {"api_key": _KEY})]


class _Reader:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.reads: list[tuple[str, str]] = []

    def read(self, context, ref_key):
        self.reads.append((context.account_id, ref_key))
        if ref_key not in self.values:
            raise stored_secrets.StoredSecretMissing(ref_key)
        return self.values[ref_key]


def _reader(monkeypatch, values: dict[str, str]) -> _Reader:
    reader = _Reader(values)
    monkeypatch.setattr(stored_secrets, "stored_secret_reader", lambda _conn: reader)
    monkeypatch.setattr("core.application.auth.secrets.decrypt_secret", lambda value: value)
    return reader


def test_effective_credentials_overlay_the_current_stored_value(monkeypatch) -> None:
    reader = _reader(monkeypatch, {_KEY: _STORED})

    stale = {"runtime_bearer_token": "bearer", "api_key": "stale"}  # pragma: allowlist secret
    credentials = catalog_refs.effective_credentials(
        object(),
        account_id="acc-1",
        ciphertext=json.dumps(stale),
        catalog_refs={"api_key": _KEY},
    )

    assert credentials == {"runtime_bearer_token": "bearer", "api_key": _STORED}
    assert reader.reads == [("acc-1", _KEY)]
    # A replaced value is read again at the next use; nothing was cached.
    reader.values[_KEY] = "replaced"
    again = catalog_refs.effective_credentials(
        object(), account_id="acc-1", ciphertext="", catalog_refs={"api_key": _KEY}
    )
    assert again == {"api_key": "replaced"}  # pragma: allowlist secret - test fixture


def test_effective_credentials_fail_closed_when_a_chosen_value_is_gone(monkeypatch) -> None:
    _reader(monkeypatch, {})

    with pytest.raises(catalog_refs.StoredCredentialsUnavailableError) as exc:
        catalog_refs.effective_credentials(
            object(),
            account_id="acc-1",
            ciphertext=json.dumps({"other": _TYPED}),
            catalog_refs={"api_key": _KEY},
        )

    assert "API key" in exc.value.detail
    assert _TYPED not in exc.value.detail and _KEY not in exc.value.detail


def test_effective_credentials_fail_closed_on_an_unreadable_blob(monkeypatch) -> None:
    _reader(monkeypatch, {})
    with pytest.raises(catalog_refs.StoredCredentialsUnavailableError):
        catalog_refs.effective_credentials(
            object(), account_id="acc-1", ciphertext="[]", catalog_refs={}
        )


def test_release_removes_only_the_chosen_name_and_invalidates_checks() -> None:
    conn = _Conn()
    cur = conn.cursor()

    catalog_refs.release_catalog_ref(
        cur, account_id="acc-1", install_id="pi_acme", secret_name="api_key"
    )

    release_sql, release_params = conn.log[0]
    assert "llm_provider_catalog_refs" in release_sql
    assert "llm_provider_last_check" in release_sql
    # The typed-value blob and its pointer are left alone.
    assert "secret_refs" not in release_sql
    assert "llm_provider_credential_secret_ref" not in release_sql
    assert release_params == ("api_key", "api_key", "acc-1", "pi_acme")
    statements = [sql for sql, _params in conn.log]
    assert any("llm_provider_state_revision" in sql for sql in statements)
    assert any("UPDATE llm_provider_models" in sql for sql in statements)


def test_reference_consumers_release_reaches_the_install(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        catalog_refs, "release_catalog_ref", lambda _cur, **kwargs: calls.append(kwargs)
    )
    monkeypatch.setattr(reference_consumers, "unbind", lambda *_a, **_k: True)

    reference_consumers.release(
        object(),
        _ADMIN,
        {
            "consumer_kind": "llm_provider",
            "llm_provider_install_id": "pi_acme",
            "field_name": "api_key",
        },
    )

    assert calls == [{"account_id": "acc-1", "install_id": "pi_acme", "secret_name": "api_key"}]


def test_status_names_a_chosen_secret_without_its_value(monkeypatch) -> None:
    from core.application.settings.llm_provider_extension_use_cases import (
        LlmProviderExtensionUseCases,
    )

    contract = SimpleNamespace(
        required_provider_secrets=["api_key"], optional_provider_secrets=["other"]
    )
    use_cases = LlmProviderExtensionUseCases(object(), get_logger_only=lambda: None)
    monkeypatch.setattr(
        use_cases, "_resolve_install", lambda *_a: ({}, contract, "pi_acme", "e", "E")
    )
    monkeypatch.setattr(
        "core.infrastructure.db.db_pool.transaction_connection",
        lambda conn: contextlib.nullcontext(conn),
    )
    monkeypatch.setattr(
        connection_status,
        "read_install_config",
        lambda *_a, **_k: {catalog_refs.CATALOG_REFS_KEY: {"api_key": _KEY}},
    )
    monkeypatch.setattr(connection_status, "saved_secret_names", lambda *_a, **_k: {"other"})
    monkeypatch.setattr(
        catalog_refs,
        "chosen_secrets",
        lambda *_a, **_k: {"api_key": {"id": "sec-1", "name": "OpenRouter key"}},
    )

    payload = use_cases.connection_status("pi_acme", "acc-1")

    assert payload["saved_secrets"] == [
        {
            "name": "api_key",
            "saved_at": None,
            "stored_secret": {"id": "sec-1", "name": "OpenRouter key"},
        },
        {"name": "other", "saved_at": None},
    ]
    assert _KEY not in json.dumps(payload)


def test_discovery_sends_the_chosen_value_to_the_provider(monkeypatch) -> None:
    reader = _reader(monkeypatch, {_KEY: _STORED})

    captured, status_code = _discovery(monkeypatch)

    # The synthetic stop after the credentials were handed over surfaces as 502.
    assert status_code == 502
    assert captured == [{"api_key": _STORED}]
    assert reader.reads == [("a", _KEY)]


def test_discovery_fails_closed_when_the_chosen_value_is_gone(monkeypatch) -> None:
    _reader(monkeypatch, {})

    captured, status_code = _discovery(monkeypatch)

    assert status_code == 409
    assert captured == []


def _discovery(monkeypatch) -> tuple[list, int]:
    from core.application.settings import llm_provider_extension_service as service
    from core.application.settings.llm_provider_extension_use_cases import (
        LlmProviderExtensionUseCases,
    )
    from tests.unit.test_llm_provider_json_secrets import _descriptor

    descriptor = _descriptor()
    record = {"runtime_mode": "subprocess", "release_id": "r1", "descriptor_sha256": "a" * 64}
    use_cases = LlmProviderExtensionUseCases(object(), get_logger_only=lambda: None)
    monkeypatch.setattr(use_cases, "_resolve_install", lambda *_: (record, None, "i", "e", "E"))
    monkeypatch.setattr(
        "core.infrastructure.db.db_pool.transaction_connection",
        lambda conn: contextlib.nullcontext(conn),
    )
    monkeypatch.setattr(
        store,
        "begin_install_discovery",
        lambda *_a, **_k: {
            "release_id": "r1",
            "descriptor_sha256": "a" * 64,
            "descriptor_jsonb": descriptor,
            "extension_id": descriptor["extension_id"],
            "llm_provider_state_revision": "s1",
            "encrypted_value": "",
            catalog_refs.CATALOG_REFS_KEY: {"api_key": _KEY},
        },
    )
    monkeypatch.setattr(use_cases, "_record_failed_check", lambda *_: None)
    captured: list = []

    def stop_before_network(*_args, credentials, **_kwargs):
        captured.append(credentials)
        raise RuntimeError("synthetic stop")

    monkeypatch.setattr(service, "fetch_models_via_remote_runtime", stop_before_network)
    with pytest.raises(LlmProviderExtensionError) as exc:
        use_cases.refresh_models("i", "a", connection_config={})
    return captured, exc.value.status_code


def test_model_runtime_reads_the_chosen_value(monkeypatch) -> None:
    from core.infrastructure.db.platform_stores import PlatformIdentityStore
    from tests.unit.test_llm_provider_extensions import _make_adapter

    logger = type("Logger", (), {"conn": object()})()
    monkeypatch.setattr(PlatformIdentityStore, "get_shared_instance", lambda: logger)
    _reader(monkeypatch, {_KEY: _STORED})
    monkeypatch.setattr(
        store,
        "get_model_runtime_snapshot",
        lambda *_args, **_kwargs: {
            "install_id": "inst1",
            "extension_id": "fixture.remote-llm-a",
            "state": "active",
            "runtime_mode": "remote_https",
            "remote_base_url": "https://runtime.example",
            "handshake_status": "ok",
            "health_status": "ok",
            "runtime_status": "compatible",
            "encrypted_value": '{"runtime_bearer_token":"runtime-b"}',
            catalog_refs.CATALOG_REFS_KEY: {"api_key": _KEY},
            "connection_config_jsonb": {},
            "descriptor_jsonb": {"extension_id": "fixture.remote-llm-a"},
        },
        raising=False,
    )

    extension = _make_adapter()._runtime_extension()

    assert extension["llm_credentials"] == {"runtime_bearer_token": "runtime-b", "api_key": _STORED}
