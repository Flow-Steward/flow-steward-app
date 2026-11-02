"""The stored-secret reader contract: local adapter, substitutable provider, safe failures."""

from __future__ import annotations

import json
import random
from types import SimpleNamespace
from unittest.mock import patch

import psycopg
import pytest

from core.application.management import project_connection_secrets
from core.application.orchestration import connector_step_runtime
from core.application.orchestration.retry_policy import RetryPolicy, decide_retry
from core.application.registry.workflow_secret_defaults import secret_ref_fingerprint
from core.infrastructure.security import connection_secret_store
from core.infrastructure.security.secret_reference import (
    SecretReference,
    SecretReferenceUnauthorized,
    resolve_secret_reference,
)
from core.infrastructure.security.stored_secrets import (
    LocalStoredSecretReader,
    SecretReadContext,
    StoredSecretError,
    StoredSecretMissing,
    StoredSecretUnavailable,
    StoredSecretUndecryptable,
)

_VALUE = "stored-secret-value-9135"
_LEAKY = "postgresql://user:hunter2@db.internal/vault/path"


class _Cursor:
    def __init__(self, conn) -> None:
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        self._conn.calls.append(params)
        if self._conn.error is not None:
            raise self._conn.error

    def fetchone(self):
        return self._conn.rows.get(self._conn.calls[-1])


class _Conn:
    def __init__(self, rows=None, error=None) -> None:
        self.rows = dict(rows or {})
        self.error = error
        self.calls: list[tuple] = []

    def cursor(self):
        return _Cursor(self)


class _ExternalReader:
    """Not the local store: it returns values it never decrypted."""

    def __init__(self, values=None, error=None) -> None:
        self.values = dict(values or {})
        self.error = error
        self.reads: list[tuple[str, str]] = []

    def read(self, context, ref_key):
        self.reads.append((context.account_id, ref_key))
        if self.error is not None:
            raise self.error
        if ref_key not in self.values:
            raise StoredSecretMissing(ref_key)
        return self.values[ref_key]


# ---------------------------------------------------------------------------
# Local adapter
# ---------------------------------------------------------------------------


def test_local_reader_decrypts_the_account_scoped_value() -> None:
    conn = _Conn(rows={("acct-1", "ref-a"): ("cipher-a",)})
    with patch(
        "core.infrastructure.security.secret_codec.decrypt_secret", return_value=_VALUE
    ) as decrypt:
        value = LocalStoredSecretReader(conn).read(SecretReadContext("acct-1"), "ref-a")

    assert value == _VALUE
    decrypt.assert_called_once_with("cipher-a")
    assert conn.calls == [("acct-1", "ref-a")]


def test_local_reader_does_not_cross_accounts() -> None:
    conn = _Conn(rows={("acct-1", "ref-a"): ("cipher-a",)})

    with pytest.raises(StoredSecretMissing):
        LocalStoredSecretReader(conn).read(SecretReadContext("acct-2"), "ref-a")


def test_local_reader_returns_a_stored_empty_string_as_a_value() -> None:
    conn = _Conn(rows={("acct-1", "ref-a"): ("cipher-of-empty",)})
    with patch("core.infrastructure.security.secret_codec.decrypt_secret", return_value=""):
        assert LocalStoredSecretReader(conn).read(SecretReadContext("acct-1"), "ref-a") == ""


@pytest.mark.parametrize(
    ("conn", "decrypt_error", "expected"),
    [
        (_Conn(error=psycopg.OperationalError(_LEAKY)), None, StoredSecretUnavailable),
        (
            _Conn(rows={("acct-1", "ref-a"): ("cipher",)}),
            ValueError(_LEAKY),
            StoredSecretUndecryptable,
        ),
        (None, None, StoredSecretUnavailable),
    ],
)
def test_local_reader_failures_carry_no_cause_text(conn, decrypt_error, expected) -> None:
    with (
        patch(
            "core.infrastructure.security.secret_codec.decrypt_secret", side_effect=decrypt_error
        ),
        pytest.raises(expected) as raised,
    ):
        LocalStoredSecretReader(conn).read(SecretReadContext("acct-1"), "ref-a")

    assert _LEAKY not in str(raised.value)
    assert raised.value.__cause__ is None
    # Nothing of the driver's or codec's exception travels with it.
    assert raised.value.__context__ is None or raised.value.__suppress_context__


def test_local_reader_does_not_touch_the_store_after_the_deadline() -> None:
    conn = _Conn(rows={("acct-1", "ref-a"): ("cipher-a",)})
    expired = SecretReadContext("acct-1", deadline=0.0)

    with pytest.raises(StoredSecretUnavailable):
        LocalStoredSecretReader(conn).read(expired, "ref-a")
    assert conn.calls == []


# ---------------------------------------------------------------------------
# Workflow references through a substituted reader
# ---------------------------------------------------------------------------


def test_a_stored_reference_resolves_through_a_substituted_reader() -> None:
    reader = _ExternalReader(values={"ref-a": _VALUE})

    def no_local_decrypt(_ciphertext):
        raise AssertionError("a stored value must not be decrypted locally")

    value = resolve_secret_reference(
        SecretReference(name="login", ref_key="ref-a"),
        context=SecretReadContext("acct-1"),
        authorized_ref_keys=frozenset({"ref-a"}),
        reader=reader,
        decrypt_inline=no_local_decrypt,
    )

    assert value == _VALUE
    assert reader.reads == [("acct-1", "ref-a")]


def test_an_unauthorized_reference_never_reaches_the_reader() -> None:
    reader = _ExternalReader(values={"ref-a": _VALUE})

    with pytest.raises(SecretReferenceUnauthorized):
        resolve_secret_reference(
            SecretReference(name="login", ref_key="ref-a"),
            context=SecretReadContext("acct-1"),
            authorized_ref_keys=frozenset({"ref-b"}),
            reader=reader,
        )
    assert reader.reads == []


def test_a_forged_inline_reference_is_refused_before_decryption() -> None:
    decrypted: list[str] = []
    reader = _ExternalReader()

    with pytest.raises(SecretReferenceUnauthorized):
        resolve_secret_reference(
            SecretReference(name="login", ciphertext="cipher-from-another-run"),
            context=SecretReadContext("acct-1"),
            authorized_ref_keys=frozenset(),
            reader=reader,
            authorized_inline_ref_fingerprints=frozenset({"not-this-one"}),
            decrypt_inline=lambda ciphertext: decrypted.append(ciphertext) or _VALUE,
        )
    assert decrypted == []
    assert reader.reads == []


def _connector_state(ref_key: str) -> dict:
    return {
        "workflow_id": "orders",
        "runtime_context": {"account_id": "acct-1"},
        "inputs": {"login": SecretReference(name="login", ref_key=ref_key).to_wire()},
        "_workflow_secret_ref_fingerprints": [secret_ref_fingerprint(ref_key)],
    }


def _connector_step() -> dict:
    return {
        "step_id": "call_api",
        "connector_id": "demo.connector",
        "override": {
            "connector": {
                "function_name": "demo_call",
                "input_mapping": {"password": "state.inputs.login"},
            }
        },
    }


def test_the_connector_step_dispatches_a_value_from_a_substituted_reader() -> None:
    dispatched: dict = {}
    reader = _ExternalReader(values={"ref-a": _VALUE})

    def fake_dispatch(*, input_payload, **_kwargs):
        dispatched.update(input_payload)
        return {"handled": True, "ok": True, "result": {}}

    with patch.object(connector_step_runtime, "_dispatch_tool_runtime", fake_dispatch):
        result = connector_step_runtime.execute_connector_step(
            step=_connector_step(), state=_connector_state("ref-a"), secret_reader=reader
        )

    assert dispatched["password"] == _VALUE
    assert reader.reads == [("acct-1", "ref-a")]
    assert _VALUE not in json.dumps(result)


@pytest.mark.parametrize(
    ("error", "code", "may_retry"),
    [
        (StoredSecretUnavailable, "secret_store_unavailable", True),
        (StoredSecretUndecryptable, "secret_value_undecryptable", False),
    ],
)
def test_connector_store_failures_follow_the_retry_policy(error, code, may_retry) -> None:
    reader = _ExternalReader(error=error("ref-a"))

    with patch.object(
        connector_step_runtime,
        "_dispatch_tool_runtime",
        side_effect=AssertionError("dispatched without its credential"),
    ):
        result = connector_step_runtime.execute_connector_step(
            step=_connector_step(), state=_connector_state("ref-a"), secret_reader=reader
        )

    assert result["ok"] is False
    assert result["error"] == code
    decision = decide_retry(
        signal=result, retry_number=1, policy=RetryPolicy(), rng=random.Random(0).random
    )
    assert decision.should_retry is may_retry


def test_a_missing_value_is_not_retried() -> None:
    reader = _ExternalReader()

    result = connector_step_runtime.execute_connector_step(
        step=_connector_step(), state=_connector_state("ref-a"), secret_reader=reader
    )

    assert result["error"] == "unresolved_secret_input"
    decision = decide_retry(
        signal=result, retry_number=1, policy=RetryPolicy(), rng=random.Random(0).random
    )
    assert decision.should_retry is False


# ---------------------------------------------------------------------------
# Static connection credentials
# ---------------------------------------------------------------------------


def test_static_credentials_resolve_through_the_reader_and_skip_oauth_grants() -> None:
    reader = _ExternalReader(values={"ref-token": _VALUE, "ref-blank": ""})

    values = connection_secret_store.resolve_connection_secrets(
        None,
        account_id="acct-1",
        secret_refs={
            "refs": {
                "api_token": "ref-token",
                "optional_note": "ref-blank",
                "_oauth_refresh_token": "ref-oauth",
            }
        },
        reader=reader,
    )

    assert values == {"api_token": _VALUE, "optional_note": ""}
    assert ("acct-1", "ref-oauth") not in reader.reads


def test_a_configured_credential_without_a_value_is_an_error_not_a_dropped_field() -> None:
    reader = _ExternalReader(values={"ref-token": _VALUE})

    with pytest.raises(StoredSecretMissing):
        connection_secret_store.resolve_connection_secrets(
            None,
            account_id="acct-1",
            secret_refs={"refs": {"api_token": "ref-token", "webhook_secret": "ref-gone"}},
            reader=reader,
        )


def test_an_unconfigured_credential_is_simply_absent() -> None:
    reader = _ExternalReader(values={"ref-token": _VALUE})

    values = connection_secret_store.resolve_connection_secrets(
        None, account_id="acct-1", secret_refs={"refs": {"api_token": "ref-token"}}, reader=reader
    )

    assert values == {"api_token": _VALUE}


def test_the_oauth_lifecycle_read_stays_local_and_lenient() -> None:
    conn = _Conn(rows={("acct-1", "ref-oauth"): ("cipher-oauth",)})
    with (
        patch(
            "core.infrastructure.security.connection_secret_store.decrypt_secret",
            return_value="refresh-token",
        ),
        patch(
            "core.infrastructure.security.connection_secret_store.stored_secret_reader",
            side_effect=AssertionError("OAuth values must not reach the stored-secret reader"),
        ),
    ):
        values = project_connection_secrets.read_local_connection_secrets(
            conn,
            account_id="acct-1",
            secret_refs={"refs": {"_oauth_refresh_token": "ref-oauth", "_oauth_gone": "ref-x"}},
        )

    assert values == {"_oauth_refresh_token": "refresh-token"}


def test_the_oauth_token_store_reads_locally() -> None:
    from core.infrastructure.extension_runtime.oauth_token_store import (
        DatabaseOAuthCallbackTokenStore,
    )

    with patch(
        "core.infrastructure.extension_runtime.oauth_token_store.read_local_connection_secrets",
        return_value={"_oauth_refresh_token": "refresh-token"},
    ) as local_read:
        token = DatabaseOAuthCallbackTokenStore(object()).resolve_refresh_token(
            account_id="acct-1", secret_refs={"refs": {}}
        )

    assert token == "refresh-token"
    local_read.assert_called_once()


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (StoredSecretUnavailable("ref-a"), "secret_store_unavailable"),
        (RuntimeError(f"driver said {_LEAKY} {_VALUE}"), StoredSecretError.code),
    ],
)
def test_connection_secret_failures_never_carry_the_cause_text(exc, code) -> None:
    failure = project_connection_secrets.connection_secret_failure("conn-1", exc)

    assert failure.detail == f"connection_secret_resolution_failed:conn-1:{code}"
    assert _LEAKY not in failure.detail
    assert _VALUE not in failure.detail


def test_connector_connection_resolution_error_is_safe_and_classified() -> None:
    from core.application.orchestration.connector_connection import (
        ConnectionResolutionError,
        _build_runtime_connection_target,
    )

    connection = {
        "connection_id": "conn-1",
        "account_id": "acct-1",
        "secret_refs": {"refs": {"api_token": "ref-token"}},
    }
    bl = SimpleNamespace(cursor=lambda: None)
    with (
        patch(
            "core.application.orchestration.connector_connection._load_runtime_connection",
            return_value=connection,
        ),
        patch(
            "core.application.management.project_connection_secrets.resolve_connection_secrets",
            side_effect=StoredSecretUnavailable("ref-token"),
        ),
        pytest.raises(ConnectionResolutionError) as raised,
    ):
        _build_runtime_connection_target(
            bl,
            connection_binding={"connection_id": "conn-1"},
            runtime_context={"account_id": "acct-1"},
        )

    assert str(raised.value) == (
        "connection_secret_resolution_failed:conn-1:secret_store_unavailable"
    )
    assert raised.value.retryable is True
    assert raised.value.failure_class == "transient"
    assert raised.value.__cause__ is None
