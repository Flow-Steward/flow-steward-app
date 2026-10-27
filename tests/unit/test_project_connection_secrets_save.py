"""`project_connection_secrets_save`: a secret and the connection that uses it, as one write.

The platform-owned `provider_secrets_save` stored the token and stopped. The
connection that makes the token usable was created by an extension-side branch
that Contract v2 routing never reaches, so in production a valid token existed
with no connection to carry it — saved, verified against notification provider, and useless.

The two writes are one transaction. A failure must not leave a new token
without its connection, or a connection claiming a token that was never saved.
"""

from __future__ import annotations

import copy
import json
from contextlib import contextmanager

import pytest

from core.application.extensions import project_connection_secrets as module
from core.application.extensions.project_connection_secrets import (
    SecretsSaveRejected,
    save_project_connection_secrets,
)

_TOKEN = "TOKEN-MUST-NEVER-BE-ECHOED-7710"
_CREDENTIAL_KEY = "credential_key"
_OTHER_REF = "workflow:orders:input:smtp"
_REAL_TRANSACTION_FOR = module._transaction_for
_SECRET_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["credential_key"],
    "properties": {"credential_key": {"type": "string", "minLength": 1, "maxLength": 64}},
}


class _Store:
    """Behaves like the integration store for these two writes.

    Writes land in a journal and are committed to visible state only when the
    surrounding transaction exits cleanly — which is what a pinned connection
    does, and what makes an atomicity assertion mean something.
    """

    def __init__(self, connection: dict | None = None) -> None:
        self.connection = copy.deepcopy(connection)
        self.secrets: dict[tuple[str, str, str], str] = {}
        self._journal: list[tuple[str, dict]] = []
        self.conn = object()
        self.in_transaction = False
        self.saved_inside_transaction: list[str] = []

    def get_project_connection(self, *, project_id, extension_id="", connection_id="", **_kw):
        if self.connection is None:
            return None
        if connection_id and self.connection["connection_id"] != connection_id:
            return None
        if extension_id and self.connection["extension_id"] != extension_id:
            return None
        return copy.deepcopy(self.connection)

    def save_provider_secret(self, *, provider_key, secret_key, secret_value, project_id, **_kw):
        self.saved_inside_transaction.append("secret" if self.in_transaction else "secret:OUTSIDE")
        self._journal.append(
            ("secret", {"key": (project_id, provider_key, secret_key), "value": secret_value})
        )
        return True

    def upsert_project_connection(self, **fields):
        self.saved_inside_transaction.append(
            "connection" if self.in_transaction else "connection:OUTSIDE"
        )
        self._journal.append(("connection", fields))
        merged = dict(self.connection or {})
        merged.update(fields)
        merged.setdefault("connection_id", "conn_new")
        return merged

    # transaction lifecycle driven by the module's transaction helper
    def begin(self):
        self.in_transaction = True
        self._journal = []

    def commit(self):
        for kind, payload in self._journal:
            if kind == "secret":
                self.secrets[payload["key"]] = payload["value"]
            else:
                merged = dict(self.connection or {})
                merged.update(payload)
                merged.setdefault("connection_id", "conn_new")
                self.connection = merged
        self._journal = []
        self.in_transaction = False

    def rollback(self):
        self._journal = []
        self.in_transaction = False

    @contextmanager
    def provider_configuration_transaction(self):
        self.begin()
        try:
            yield self
        except BaseException:
            self.rollback()
            raise
        self.commit()


class _PortOnlyStore(_Store):
    """Operation-store double that exposes the port but no logger internals."""

    def __init__(self, connection: dict | None = None) -> None:
        super().__init__(connection)
        del self.conn


class _StoreWithoutTransactionPort(_Store):
    def __init__(self, connection: dict | None = None) -> None:
        super().__init__(connection)
        del self.conn
        self.provider_configuration_transaction = None  # type: ignore[assignment]


def _config() -> dict:
    return {
        "provider_key": "acme.notification-provider",
        "connection_type_id": "notification_channel",
        "secret_fields": [{"input": "credential_value", "secret_key": _CREDENTIAL_KEY}],
    }


def _grant(**overrides) -> dict:
    grant = {
        "scope": "owning_extension",
        "provider_key": "acme.notification-provider",
        "secret_keys": ["credential_key"],
        "connection_type_ids": ["notification_channel"],
        "project_id": "project_1",
        "extension_id": "acme.notification-provider",
        "extension_install_id": "install_9",
    }
    grant.update(overrides)
    return grant


def _existing_connection() -> dict:
    return {
        "connection_id": "conn_existing_42",
        "project_id": "project_1",
        "extension_id": "acme.notification-provider",
        "extension_install_id": "install_9",
        "account_id": "acct-1",
        "connection_kind": "extension",
        "connection_type": "notification_channel",
        "display_name": "Ops bot",
        "enabled": True,
        "credential_revision": 3,
        "connection_config": {"parse_mode": "MarkdownV2", "channels": [{"id": "c1"}]},
        "secret_refs": {"refs": {"smtp_password": _OTHER_REF}},
        "connection_routes": [{"route": "ops"}],
        "ingress_config": {"inbound_mode": "polling", "polling_interval_seconds": 15},
        "workflow_mappings": {"wf": "map"},
        "tags": ["prod"],
    }


def _save(store: _Store, payload: dict, *, connection=None):
    return save_project_connection_secrets(
        store,
        project_id="project_1",
        extension_id="acme.notification-provider",
        extension_install_id="install_9",
        trusted_grant=_grant(),
        config=_config(),
        input_payload=payload,
        secret_schema=_SECRET_SCHEMA,
    )


# ---------------------------------------------------------------------------
# A new token establishes its connection
# ---------------------------------------------------------------------------


def test_a_new_token_creates_one_connection_and_one_secret_ref() -> None:
    store = _Store(connection=None)

    result = _save(store, {"credential_value": _TOKEN})

    assert store.secrets[("project_1", "acme.notification-provider", "credential_key")] == _TOKEN
    assert store.connection is not None
    assert store.connection["extension_id"] == "acme.notification-provider"
    assert store.connection["connection_type"] == "notification_channel"
    assert store.connection["connection_kind"] == "extension"
    assert store.connection["extension_install_id"] == "install_9"
    assert store.connection["enabled"] is True
    assert store.connection["secret_refs"] == {
        "provider_secrets": {
            "provider_key": "acme.notification-provider",
            "keys": {"credential_value": "credential_key"},
        }
    }
    assert result["saved_keys"] == ["credential_value"]


def test_secret_refs_map_manifest_input_name_to_provider_secret_key() -> None:
    store = _Store(connection=None)
    config = {
        "provider_key": "acme.notification-provider",
        "connection_type_id": "notification_channel",
        "secret_fields": [{"input": "credential_input", "secret_key": _CREDENTIAL_KEY}],
    }

    save_project_connection_secrets(
        store,
        project_id="project_1",
        extension_id="acme.notification-provider",
        extension_install_id="install_9",
        trusted_grant=_grant(),
        config=config,
        input_payload={"credential_input": _TOKEN},
    )

    assert store.connection["secret_refs"]["provider_secrets"] == {
        "provider_key": "acme.notification-provider",
        "keys": {"credential_input": "credential_key"},
    }


def test_both_writes_happen_inside_one_transaction() -> None:
    store = _Store(connection=None)

    _save(store, {"credential_value": _TOKEN})

    assert store.saved_inside_transaction == ["secret", "connection"], (
        "a write outside the transaction is one that survives the other's failure"
    )


def test_transaction_uses_the_operation_store_port_without_conn_internals() -> None:
    store = _PortOnlyStore(connection=None)

    result = _dispatch(store, {"credential_value": _TOKEN})

    assert result["status"] == "succeeded"
    assert store.secrets[("project_1", "acme.notification-provider", "credential_key")] == _TOKEN


def test_missing_transaction_port_fails_closed_before_writes() -> None:
    store = _StoreWithoutTransactionPort(connection=None)

    _raises_input_255_1 = _grant()
    _raises_input_255_2 = _config()
    with pytest.raises(SecretsSaveRejected) as error:
        save_project_connection_secrets(
            store,
            project_id="project_1",
            extension_id="acme.notification-provider",
            extension_install_id="install_9",
            trusted_grant=_raises_input_255_1,
            config=_raises_input_255_2,
            input_payload={"credential_value": _TOKEN},
            transaction=_REAL_TRANSACTION_FOR,
        )

    assert error.value.code == "project_connection_storage_unavailable"
    assert store.saved_inside_transaction == []


def test_a_failed_connection_write_leaves_no_orphaned_token() -> None:
    """The split-brain the design forbids: a verified token with nothing to carry it."""
    store = _Store(connection=None)

    def failing_upsert(**_fields):
        raise RuntimeError("connection storage exploded")

    store.upsert_project_connection = failing_upsert  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        _save(store, {"credential_value": _TOKEN})

    assert store.secrets == {}, "the secret must roll back with the connection"
    assert store.connection is None


def test_a_false_secret_write_rolls_back_and_does_not_create_a_connection() -> None:
    store = _Store(connection=None)

    def rejected_secret_write(**_fields):
        return False

    store.save_provider_secret = rejected_secret_write  # type: ignore[method-assign]

    with pytest.raises(SecretsSaveRejected) as error:
        _save(store, {"credential_value": _TOKEN})

    assert error.value.code == "provider_secret_storage_failed"
    assert store.secrets == {}
    assert store.connection is None


# ---------------------------------------------------------------------------
# Replacing a token preserves everything the connection already carries
# ---------------------------------------------------------------------------


def test_replacing_a_token_preserves_the_connection_identity_and_state() -> None:
    store = _Store(connection=_existing_connection())

    _save(store, {"credential_value": "NEW-" + _TOKEN})

    saved = store.connection
    assert saved["connection_id"] == "conn_existing_42"
    assert saved["connection_config"] == {"parse_mode": "MarkdownV2", "channels": [{"id": "c1"}]}
    assert saved["connection_routes"] == [{"route": "ops"}]
    assert saved["ingress_config"] == {"inbound_mode": "polling", "polling_interval_seconds": 15}
    assert saved["workflow_mappings"] == {"wf": "map"}
    assert saved["tags"] == ["prod"]
    assert saved["display_name"] == "Ops bot"
    assert saved["account_id"] == "acct-1"


def test_replacing_a_token_merges_only_the_declared_secret_ref() -> None:
    """Unrelated references survive; the declared one is added, not the map replaced."""
    store = _Store(connection=_existing_connection())

    _save(store, {"credential_value": "NEW-" + _TOKEN})

    assert store.connection["secret_refs"] == {
        "refs": {"smtp_password": _OTHER_REF},
        "provider_secrets": {
            "provider_key": "acme.notification-provider",
            "keys": {"credential_value": "credential_key"},
        },
    }


# ---------------------------------------------------------------------------
# Rejections alter nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", [{}, {"credential_value": ""}, {"credential_value": "   "}])
def test_an_empty_required_secret_is_rejected_without_touching_state(payload: dict) -> None:
    store = _Store(connection=_existing_connection())
    before = copy.deepcopy(store.connection)

    with pytest.raises(SecretsSaveRejected):
        _save(store, payload)

    assert store.secrets == {}, "no secret row"
    assert store.connection == before, "no connection change"
    assert store.saved_inside_transaction == [], "no write was even attempted"


def test_a_missing_project_is_rejected() -> None:
    store = _Store(connection=None)

    _raises_input_360_1 = _config()
    with pytest.raises(SecretsSaveRejected):
        save_project_connection_secrets(
            store,
            project_id="",
            extension_id="acme.notification-provider",
            extension_install_id="install_9",
            config=_raises_input_360_1,
            input_payload={"credential_value": _TOKEN},
        )

    assert store.secrets == {}
    assert store.connection is None


def test_manifest_cannot_write_secrets_into_another_provider_namespace() -> None:
    store = _Store(connection=None)
    hostile_config = {
        **_config(),
        "provider_key": "victim.provider",
    }

    _raises_input_381_1 = _grant()
    with pytest.raises(SecretsSaveRejected) as error:
        save_project_connection_secrets(
            store,
            project_id="project_1",
            extension_id="acme.notification-provider",
            extension_install_id="install_9",
            trusted_grant=_raises_input_381_1,
            config=hostile_config,
            input_payload={"credential_value": _TOKEN},
        )

    assert error.value.code == "provider_secret_namespace_forbidden"
    assert store.secrets == {}
    assert store.connection is None


@pytest.mark.parametrize(
    ("config_patch", "code"),
    [
        (
            {"secret_fields": [{"input": "credential_value", "secret_key": "other"}]},
            "provider_secret_key_forbidden",
        ),
        ({"connection_type_id": "foreign_type"}, "project_connection_type_forbidden"),
    ],
)
def test_host_grant_rejects_undeclared_secret_keys_and_connection_types(
    config_patch: dict, code: str
) -> None:
    store = _Store(connection=None)

    _raises_input_412_1 = _grant()
    _raises_input_412_2 = {**_config(), **config_patch}
    with pytest.raises(SecretsSaveRejected) as error:
        save_project_connection_secrets(
            store,
            project_id="project_1",
            extension_id="acme.notification-provider",
            extension_install_id="install_9",
            trusted_grant=_raises_input_412_1,
            config=_raises_input_412_2,
            input_payload={"credential_value": _TOKEN},
        )

    assert error.value.code == code
    assert store.secrets == {}
    assert store.connection is None


@pytest.mark.parametrize(
    ("grant_patch", "call_patch"),
    [
        ({"project_id": "project_other"}, {}),
        ({"extension_id": "foreign.extension"}, {}),
        ({"extension_install_id": "install_other"}, {}),
    ],
)
def test_host_grant_rejects_foreign_project_extension_or_install_context(
    grant_patch: dict, call_patch: dict
) -> None:
    store = _Store(connection=None)
    call = {
        "project_id": "project_1",
        "extension_id": "acme.notification-provider",
        "extension_install_id": "install_9",
        **call_patch,
    }

    _raises_input_447_1 = _grant(**grant_patch)
    _raises_input_447_2 = _config()
    with pytest.raises(SecretsSaveRejected) as error:
        save_project_connection_secrets(
            store,
            **call,
            trusted_grant=_raises_input_447_1,
            config=_raises_input_447_2,
            input_payload={"credential_value": _TOKEN},
        )

    assert error.value.code == "host_mutation_context_mismatch"
    assert store.secrets == {}
    assert store.connection is None


# ---------------------------------------------------------------------------
# The value never leaves
# ---------------------------------------------------------------------------


def test_the_result_carries_keys_and_readiness_but_never_the_value() -> None:
    store = _Store(connection=None)

    result = _save(store, {"credential_value": _TOKEN})

    rendered = json.dumps(result)
    assert _TOKEN not in rendered
    assert result["saved_keys"] == ["credential_value"]
    assert result["connection_id"] == store.connection["connection_id"]
    assert result["connection_enabled"] is True


def test_the_stored_connection_never_carries_the_value() -> None:
    store = _Store(connection=None)

    _save(store, {"credential_value": _TOKEN})

    assert _TOKEN not in json.dumps(store.connection)


# ---------------------------------------------------------------------------
# Reachable through the compiled action manifest
#
# The contract is only real if a manifest can route to it. These go through the
# same dispatcher a notification provider `save_secrets` action does, with the calling
# convention the existing platform-operation tests use.
# ---------------------------------------------------------------------------


def _extension_with_platform_save() -> dict:
    return {
        "extension_key": "acme.notification-provider",
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_id": "acme.notification-provider",
        "install_metadata": {
            "runtime_grants": {"mutations": {"project_connection_secrets_save": _grant()}}
        },
        "connection_types": [
            {
                "connection_type_id": "notification_channel",
                "secret_schema": _SECRET_SCHEMA,
            }
        ],
        "action_definitions": [
            {
                "action_id": "save_secrets",
                "handler": {
                    "mode": "platform",
                    "operation": "project_connection_secrets_save",
                    "config": _config(),
                },
            }
        ],
    }


def _dispatch(store: _Store, payload: dict) -> dict:
    from core.application.extensions.platform_connection_operations import (
        install_platform_connection_operation,
    )
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_action

    runtime_context = {"logger": store, "extension_install_id": "install_9"}
    install_platform_connection_operation(runtime_context)
    return run_extension_action(
        _extension_with_platform_save(),
        action_id="save_secrets",
        context={"project_id": "project_1"},
        input_payload=payload,
        runtime_context=runtime_context,
    )


def test_the_operation_is_reachable_from_a_platform_routed_action() -> None:
    store = _Store(connection=None)

    result = _dispatch(store, {"credential_value": _TOKEN})

    assert result["status"] == "succeeded", result
    assert store.connection is not None
    assert store.connection["extension_install_id"] == "install_9"
    assert store.secrets[("project_1", "acme.notification-provider", "credential_key")] == _TOKEN


def test_platform_secret_save_without_exact_host_grant_fails_before_writes() -> None:
    from core.application.extensions.platform_connection_operations import (
        install_platform_connection_operation,
    )
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_action

    store = _Store(connection=None)
    extension = _extension_with_platform_save()
    extension.pop("install_metadata")
    runtime_context = {"logger": store, "extension_install_id": "install_9"}
    install_platform_connection_operation(runtime_context)

    result = run_extension_action(
        extension,
        action_id="save_secrets",
        context={"project_id": "project_1"},
        input_payload={"credential_value": _TOKEN},
        runtime_context=runtime_context,
    )

    assert result["status"] == "failed"
    assert result["detail"] == "host_mutation_not_granted"
    assert store.secrets == {}
    assert store.connection is None


def test_the_dispatcher_result_never_echoes_the_token() -> None:
    store = _Store(connection=None)

    result = _dispatch(store, {"credential_value": _TOKEN})

    assert _TOKEN not in json.dumps(result)


@pytest.mark.parametrize("invalid_value", ["SENSITIVE-" + "x" * 80, 12345])
def test_secret_schema_rejection_happens_before_writes_without_echo(invalid_value) -> None:
    store = _Store(connection=None)

    result = _dispatch(store, {"credential_value": invalid_value})

    assert result["status"] == "failed"
    assert result["detail"] == "invalid_payload"
    assert store.secrets == {}
    assert store.connection is None
    assert str(invalid_value) not in json.dumps(result)


def test_a_rejection_surfaces_as_a_typed_failure_not_a_traceback() -> None:
    store = _Store(connection=_existing_connection())
    before = copy.deepcopy(store.connection)

    result = _dispatch(store, {"credential_value": ""})

    assert result["status"] == "failed"
    assert result["detail"] == "provider_secret_value_required"
    assert store.connection == before
    assert store.secrets == {}


def test_a_store_without_the_capability_fails_closed_before_any_write() -> None:
    """The failure the whole design started from, now typed and pre-write."""

    class _IdentityShaped:
        conn = object()

        def save_provider_secret(self, **_kw):
            raise AssertionError("must not be reached")

    from core.infrastructure.extension_runtime.extension_runtime import run_extension_action

    result = run_extension_action(
        _extension_with_platform_save(),
        action_id="save_secrets",
        context={"project_id": "project_1"},
        input_payload={"credential_value": _TOKEN},
        runtime_context={"logger": _IdentityShaped()},
    )

    assert result["status"] == "failed"
    assert result["detail"] == "project_connection_storage_unavailable"
