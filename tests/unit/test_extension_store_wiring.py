"""Extension callbacks and the trigger catalog read from the store that holds their data."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from core.application.extensions import callbacks
from core.application.workflows.workflow_trigger_catalog import list_workflow_trigger_catalog
from core.entrypoints.api.route_registration import _ProjectStoreWithExtensionList

TELEGRAM_ROW = {
    "extension_key": "flowsteward.telegram",
    "kind": "notification_provider",
    "compiled_manifest": {
        "workflow_triggers": [
            {
                "trigger_type_id": "message_received",
                "provider_key": "flowsteward.telegram",
                "display_name": "New Telegram message",
                "event_name": "telegram.message.received",
            }
        ]
    },
}


class _ProjectStore:
    """What the project store has: connections, and no extension list."""

    def __init__(self) -> None:
        self.conn = object()

    def get_project_connection(self, **_: Any) -> dict[str, Any]:
        return {}


class _IdentityStore:
    def list_extensions(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        return [dict(TELEGRAM_ROW)]


def test_catalog_of_the_project_store_alone_holds_only_the_builtin_webhook():
    catalog = list_workflow_trigger_catalog(_ProjectStore())
    assert [item["catalog_id"] for item in catalog] == ["incoming-webhook-post"]


def test_project_store_with_extension_list_offers_the_trigger_an_extension_declares():
    store = _ProjectStoreWithExtensionList(_ProjectStore(), _IdentityStore())
    catalog = list_workflow_trigger_catalog(store)
    assert "flowsteward.telegram:message_received" in [item["catalog_id"] for item in catalog]
    assert store.conn is not None


class _RegistrationService:
    def __init__(self, _bl: Any, **_: Any) -> None:
        pass

    def _registration_account_id(self, _registration: dict[str, Any]) -> str:
        return "acc-1"

    def _extension(self, extension_id: str, *, account_id: str) -> dict[str, Any]:
        return {"extension_key": extension_id}


def _registration() -> dict[str, Any]:
    return {
        "extension_id": "flowsteward.telegram",
        "connection_id": "conn-1",
        "project_id": "proj-1",
    }


def test_callback_dispatch_finds_the_connection_on_the_project_store(monkeypatch):
    monkeypatch.setattr(callbacks, "ExtensionCallbackRegistrationService", _RegistrationService)
    calls: list[dict[str, Any]] = []

    def get_project_connection(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "connection_id": "conn-1",
            "project_id": "proj-1",
            "account_id": "acc-1",
            "extension_id": "flowsteward.telegram",
            "extension_install_id": "inst-1",
        }

    identity = SimpleNamespace(
        get_extension_by_install_id=lambda install_id, account_id=None: {
            "install_id": install_id,
            "extension_key": "flowsteward.telegram",
            "enabled": True,
        }
    )
    projects = SimpleNamespace(get_project_connection=get_project_connection)
    service = callbacks.ExtensionCallbackDispatchService(identity, connection_store=projects)
    extension = service.extension_for_registration(_registration())
    assert extension is not None
    assert extension["extension_install_id"] == "inst-1"
    assert calls
    assert calls[0]["connection_id"] == "conn-1"


def test_callback_dispatch_without_a_connection_store_still_uses_its_own_store(monkeypatch):
    monkeypatch.setattr(callbacks, "ExtensionCallbackRegistrationService", _RegistrationService)
    identity = SimpleNamespace(
        get_extension_by_install_id=lambda *_a, **_k: {},
        get_project_connection=lambda **_k: {},
    )
    service = callbacks.ExtensionCallbackDispatchService(identity)
    assert service.connection_store is identity
    assert service.extension_for_registration(_registration()) is None


def test_callback_dispatch_derives_the_account_of_a_registration_from_the_project_store():
    """A registration row without ``account_id`` takes it from its project.

    The identity store has no ``get_project``; without the project store the account
    came out empty, never matched the connection's account and every delivery was 404.
    """
    identity = SimpleNamespace(
        get_extension=lambda extension_id, account_id=None: {"extension_key": extension_id},
        get_extension_by_install_id=lambda install_id, account_id=None: {
            "install_id": install_id,
            "extension_key": "flowsteward.telegram",
            "enabled": True,
        },
    )
    projects = SimpleNamespace(
        get_project=lambda project_id: {"id": project_id, "account_id": "acc-1"},
        get_project_connection=lambda **_k: {
            "connection_id": "conn-1",
            "project_id": "proj-1",
            "account_id": "acc-1",
            "extension_id": "flowsteward.telegram",
            "extension_install_id": "inst-1",
        },
    )
    service = callbacks.ExtensionCallbackDispatchService(identity, connection_store=projects)
    extension = service.extension_for_registration(_registration())
    assert extension is not None
    assert extension["account_id"] == "acc-1"
    assert extension["extension_install_id"] == "inst-1"


def test_store_union_answers_from_the_first_store_then_the_second():
    from core.entrypoints.api.extension_callbacks import _StoreUnion

    identity = SimpleNamespace(get_extension=lambda key: {"extension_key": key}, shared="first")
    projects = SimpleNamespace(
        get_project_connection=lambda **_k: {"connection_id": "c"}, shared="second"
    )
    union = _StoreUnion(identity, projects)

    assert union.get_extension("x") == {"extension_key": "x"}
    assert union.get_project_connection() == {"connection_id": "c"}
    assert union.shared == "first"
    assert not hasattr(union, "get_nothing")
    assert not hasattr(union, "__deepcopy__")


TOKEN_KEY = "bot_token"  # pragma: allowlist secret


class _ProviderSecretStore:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def get_provider_secret(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "saved-value" if kwargs["secret_key"] == TOKEN_KEY else ""


def test_operation_receives_the_token_the_extension_secrets_form_saved():
    """A token saved on the extension's secrets form reaches the operation.

    The form stores it in ``provider_secrets`` and the connection only names the
    key; operations used to see ``secret_refs.refs`` alone and failed with no token.
    """
    from core.application.extensions.operation_targets import provider_secrets_for_connection

    store = _ProviderSecretStore()
    refs = {
        "refs": {},
        "provider_secrets": {
            "provider_key": "flowsteward.telegram",
            "keys": {"bot_token": "bot_token", "unused": "other"},
        },
    }
    secrets = provider_secrets_for_connection(store, {"project_id": "proj-1"}, refs)

    assert secrets == {"bot_token": "saved-value"}
    assert store.calls[0] == {
        "provider_key": "flowsteward.telegram",
        "secret_key": TOKEN_KEY,
        "project_id": "proj-1",
        "include_global": False,
    }


def test_connection_without_provider_secrets_adds_nothing():
    from core.application.extensions.operation_targets import provider_secrets_for_connection

    store = _ProviderSecretStore()
    assert provider_secrets_for_connection(store, {"project_id": "p"}, {"refs": {}}) == {}
    assert store.calls == []
    assert (
        provider_secrets_for_connection(object(), {"project_id": "p"}, {"provider_secrets": {}})
        == {}
    )


def test_a_store_that_already_lists_extensions_is_passed_through_unwrapped():
    """Only a store without the extension list is wrapped; callers keep their own object."""
    from fastapi import FastAPI

    from core.entrypoints.api import route_registration

    full = SimpleNamespace(list_extensions=lambda enabled_only=False: [])
    app = FastAPI()
    original = route_registration._composed_service
    route_registration._composed_service = lambda _app, name: full
    try:
        assert route_registration._workflow_trigger_store(app) is full
    finally:
        route_registration._composed_service = original
