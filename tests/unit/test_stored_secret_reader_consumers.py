"""Every stored-secret consumer reads through the host-selected reader (ADR 0066).

LLM provider keys, data connections and typed MCP connector headers used to decrypt
the local ciphertext themselves, so a value kept outside the database could never
reach them. They now hand the reference, and the local ciphertext their own scoped
statement already read, to ``stored_secret_reader``. With the local reader the
result is the value they decrypted before. The run's own dispatch envelope stays an
installation-key record and never goes through the reader.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from core.infrastructure.security import secret_codec, stored_secrets


class _SpyReader:
    def __init__(self, values: dict[str, str] | None = None, error: Exception | None = None):
        self.values = dict(values or {})
        self.error = error
        self.reads: list[tuple[str, str]] = []

    def read(self, context: stored_secrets.SecretReadContext, ref_key: str) -> str:
        self.reads.append((context.account_id, ref_key))
        if self.error is not None:
            raise self.error
        return self.values[ref_key]


@pytest.fixture
def host(monkeypatch):
    """Replace the host selection point and record what each consumer asked it for."""
    calls: list[dict[str, Any]] = []
    state: dict[str, Any] = {"reader": _SpyReader()}

    def select(conn, *, prefetched_ciphertexts=None):
        calls.append({"conn": conn, "prefetched": dict(prefetched_ciphertexts or {})})
        return state["reader"]

    monkeypatch.setattr(stored_secrets, "stored_secret_reader", select)
    state["calls"] = calls
    return state


# -- the local reader keeps a prefetched value a single read -------------------------


def test_a_prefetched_ciphertext_is_decrypted_without_a_second_query(monkeypatch) -> None:
    monkeypatch.setattr(secret_codec, "decrypt_secret", lambda value: f"plain:{value}")
    conn = MagicMock()
    conn.cursor.side_effect = AssertionError("the value came with the pointer")

    reader = stored_secrets.stored_secret_reader(conn, prefetched_ciphertexts={"k": "enc"})

    assert reader.read(stored_secrets.SecretReadContext.for_account("acct"), "k") == "plain:enc"


def test_a_prefetched_miss_is_missing_and_never_queried() -> None:
    reader = stored_secrets.stored_secret_reader(None, prefetched_ciphertexts={"k": None})

    with pytest.raises(stored_secrets.StoredSecretMissing):
        reader.read(stored_secrets.SecretReadContext.for_account("acct"), "k")


def test_an_unreadable_prefetched_ciphertext_is_undecryptable(monkeypatch) -> None:
    def broken(_value: str) -> str:
        raise ValueError("malformed")

    monkeypatch.setattr(secret_codec, "decrypt_secret", broken)
    reader = stored_secrets.stored_secret_reader(None, prefetched_ciphertexts={"k": "enc"})

    with pytest.raises(stored_secrets.StoredSecretUndecryptable):
        reader.read(stored_secrets.SecretReadContext.for_account("acct"), "k")


# -- LLM provider keys --------------------------------------------------------------


def _provider_store(row: dict[str, Any]) -> MagicMock:
    class _Cursor:
        def execute(self, *_args, **_kwargs) -> None:
            return None

        def fetchone(self) -> dict[str, Any]:
            return dict(row)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    store = MagicMock()
    store.conn.cursor.side_effect = lambda *_a, **_k: _Cursor()
    return store


def test_a_provider_key_is_read_through_the_reader_with_its_joined_ciphertext(host) -> None:
    import core.application.orchestration.model_provider as mp

    host["reader"] = _SpyReader({"ref-openai": "plain-key"})
    store = _provider_store(
        {
            "id": "provider-1",
            "account_id": "acc_alpha",
            "api_key_ref": "ref-openai",
            "api_key_ciphertext": "ciphertext",
        }
    )
    with patch(
        "core.infrastructure.db.platform_stores.PlatformConnectionStore.get_shared_instance",
        return_value=store,
    ):
        result = mp._resolve_named_provider("provider-1", account_id="acc_alpha")

    assert result["_decrypted_api_key"] == "plain-key"
    assert "api_key_ciphertext" not in result
    assert host["calls"] == [{"conn": store.conn, "prefetched": {"ref-openai": "ciphertext"}}]
    assert host["reader"].reads == [("acc_alpha", "ref-openai")]


def test_a_provider_key_the_reader_cannot_find_fails_closed(host) -> None:
    import core.application.orchestration.model_provider as mp

    host["reader"] = _SpyReader(error=stored_secrets.StoredSecretMissing("ref-openai"))
    store = _provider_store(
        {"id": "provider-1", "account_id": "acc_alpha", "api_key_ref": "ref-openai"}
    )
    with (
        patch(
            "core.infrastructure.db.platform_stores.PlatformConnectionStore.get_shared_instance",
            return_value=store,
        ),
        pytest.raises(mp.ModelProviderResolutionError, match="secret reference is missing"),
    ):
        mp._resolve_named_provider("provider-1", account_id="acc_alpha")


def test_an_unavailable_provider_key_never_falls_back(host) -> None:
    import core.application.orchestration.model_provider as mp

    host["reader"] = _SpyReader(error=stored_secrets.StoredSecretUnavailable("ref-openai"))
    store = _provider_store(
        {
            "id": "provider-1",
            "account_id": "acc_alpha",
            "api_key_ref": "ref-openai",
            "api_key_ciphertext": "ciphertext",
        }
    )
    with (
        patch(
            "core.infrastructure.db.platform_stores.PlatformConnectionStore.get_shared_instance",
            return_value=store,
        ),
        pytest.raises(mp.ModelProviderResolutionError, match="could not be resolved"),
    ):
        mp._resolve_named_provider("provider-1", account_id="acc_alpha")


def test_a_provider_without_a_key_asks_the_reader_nothing(host) -> None:
    import core.application.orchestration.model_provider as mp

    store = _provider_store({"id": "provider-1", "account_id": "acc_alpha", "api_key_ref": ""})
    with patch(
        "core.infrastructure.db.platform_stores.PlatformConnectionStore.get_shared_instance",
        return_value=store,
    ):
        result = mp._resolve_named_provider("provider-1", account_id="acc_alpha")

    assert "_decrypted_api_key" not in result
    assert host["calls"] == []


# -- the verification probe of a custom provider ------------------------------------


def test_the_claimed_key_is_read_through_the_reader(host) -> None:
    from core.application.management.command_services_model_provider import _claimed_api_key

    host["reader"] = _SpyReader({"provider-key": "api-key-a"})
    conn = MagicMock()

    key = _claimed_api_key(conn, account_id="acc", ref_key="provider-key", ciphertext="enc-a")

    assert key == "api-key-a"
    assert host["calls"] == [{"conn": conn, "prefetched": {"provider-key": "enc-a"}}]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (stored_secrets.StoredSecretUnavailable("k"), TimeoutError),
        (stored_secrets.StoredSecretUndecryptable("k"), ValueError),
    ],
)
def test_an_unreadable_claimed_key_maps_to_a_safe_verification_error(host, error, expected) -> None:
    from core.application.management.command_services_model_provider import _claimed_api_key
    from core.application.settings.llm_capability_verifier import normalize_verification_error

    host["reader"] = _SpyReader(error=error)

    with pytest.raises(expected) as raised:
        _claimed_api_key(MagicMock(), account_id="acc", ref_key="k", ciphertext="enc")

    category = normalize_verification_error(raised.value)
    assert category == ("timeout" if expected is TimeoutError else "invalid_response")


def test_no_pointer_or_no_value_means_no_key(host) -> None:
    from core.application.management.command_services_model_provider import _claimed_api_key

    host["reader"] = _SpyReader(error=stored_secrets.StoredSecretMissing("k"))

    assert _claimed_api_key(MagicMock(), account_id="acc", ref_key="", ciphertext="x") is None
    assert _claimed_api_key(MagicMock(), account_id="acc", ref_key="k", ciphertext="") is None
    assert [call["prefetched"] for call in host["calls"]] == [{"k": None}]


# -- data connections ---------------------------------------------------------------


class _RegistryDb:
    def __init__(self) -> None:
        self.cursor_calls = 0

    def _cursor(self):
        self.cursor_calls += 1
        return MagicMock()


def test_a_data_connection_secret_is_read_through_the_reader(host) -> None:
    from core.application.orchestration import dataset_query_step_runtime as runtime

    host["reader"] = _SpyReader({"workspace_data_connection:c1:password": "pw"})
    db = _RegistryDb()

    payload = runtime._resolved_secret_refs(
        db,
        {
            "account_id": "account-1",
            "secret_refs_jsonb": {"refs": {"password": "workspace_data_connection:c1:password"}},
        },
    )

    assert payload["values"] == {"password": "pw"}
    assert host["reader"].reads == [("account-1", "workspace_data_connection:c1:password")]
    # The reader gets the registry store's cursor as its connection.
    adapter = host["calls"][0]["conn"]
    adapter.cursor()
    assert db.cursor_calls == 1


def test_an_unreadable_data_connection_secret_is_left_out(host) -> None:
    from core.application.orchestration import dataset_query_step_runtime as runtime

    host["reader"] = _SpyReader(error=stored_secrets.StoredSecretUnavailable("ref"))

    assert runtime._resolve_secret_ref_value(_RegistryDb(), account_id="a", ref_key="ref") == ""


# -- typed MCP connector headers ----------------------------------------------------


class _HeaderConn:
    def __init__(self, ciphertext: str | None) -> None:
        self.ciphertext = ciphertext
        self.statements: list[tuple[str, tuple[Any, ...]]] = []

    def cursor(self, *_args, **_kwargs):
        conn = self

        class _Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def execute(self, query, params=()):
                conn.statements.append((" ".join(str(query).split()), tuple(params)))

            def fetchone(self):
                return (conn.ciphertext,) if conn.ciphertext else None

        return _Cursor()


def _header_row(ref_key: str) -> dict[str, Any]:
    from core.application.management import mcp_connector_header_secrets as header_secrets

    return {
        "connector_id": "mcp_1",
        "account_id": "acct",
        "project_id": "proj",
        "tenant_id": "tenant",
        header_secrets.HEADER_SECRET_REFS_FIELD: {"refs": {"X-Tenant": ref_key}},
    }


def test_a_typed_header_is_scope_checked_then_read_through_the_reader(host) -> None:
    from core.application.management import mcp_connector_header_secrets as header_secrets

    key = f"{header_secrets.header_secret_ref_prefix('mcp_1')}gen1:0"
    host["reader"] = _SpyReader({key: "tenant-a"})
    conn = _HeaderConn("enc-tenant")

    headers = header_secrets.resolve_connector_headers(conn, _header_row(key))

    assert headers == {"X-Tenant": "tenant-a"}
    assert conn.statements[0][1] == ("acct", "proj", "tenant", key)
    assert host["calls"] == [{"conn": conn, "prefetched": {key: "enc-tenant"}}]


def test_a_typed_header_the_reader_refuses_fails_closed(host) -> None:
    from core.application.management import mcp_connector_header_secrets as header_secrets
    from core.application.management.errors import ServiceUnavailableError

    key = f"{header_secrets.header_secret_ref_prefix('mcp_1')}gen1:0"
    host["reader"] = _SpyReader(error=stored_secrets.StoredSecretMissing(key))

    with pytest.raises(ServiceUnavailableError) as raised:
        header_secrets.resolve_connector_headers(_HeaderConn(None), _header_row(key))

    assert raised.value.payload["error"] == "mcp_connector_header_secret_missing"


def test_a_typed_header_outside_the_connector_prefix_is_never_read(host) -> None:
    from core.application.management import mcp_connector_header_secrets as header_secrets
    from core.application.management.errors import ServiceUnavailableError

    with pytest.raises(ServiceUnavailableError):
        header_secrets.resolve_connector_headers(
            _HeaderConn("enc"), _header_row("mcp_connector:other:header:gen1:0")
        )
    assert host["calls"] == []


# -- the run's own dispatch envelope stays local --------------------------------------


def test_the_dispatch_envelope_is_never_read_through_the_reader(host, monkeypatch) -> None:
    import json

    from core.application.secrets import run_authority

    monkeypatch.setattr(
        "core.application.auth.secrets.decrypt_secret",
        lambda _value: json.dumps({"extra_state": {"input": "x"}}),
    )
    repository = MagicMock()
    repository.return_value.get.return_value = {"encrypted_envelope": "sealed"}
    with patch(
        "core.application.jobs.dispatch_outbox_repository.DispatchOutboxRepository", repository
    ):
        extra = run_authority.dispatch_extra_state(MagicMock(), "job-1")

    assert extra == {"input": "x"}
    assert host["calls"] == []
