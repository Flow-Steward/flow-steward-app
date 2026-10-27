"""Connectionless notification selection never uses catalog/latest identity."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from core.application.notifications.delivery import _resolve_notification_extension


def _install(**overrides):
    return SimpleNamespace(
        **{
            "extension_id": "fixture.notifier",
            "install_id": "install-selected",
            "release_id": "release-v1",
            "state": "active",
            "account_id": "account-event",
            "visibility": "account",
            **overrides,
        }
    )


class Store:
    conn = object()

    @staticmethod
    def cursor():
        return None

    def __init__(self, **overrides):
        self.calls = []
        self.row = {
            "extension_key": "fixture.notifier",
            "extension_install_id": "install-selected",
            "release_id": "release-v1",
            "enabled": True,
            "kind": "notification_provider",
            "version": "1.0.0",
            **overrides,
        }

    def get_extension(self, _key):
        raise AssertionError("Catalog/latest must not resolve an executable notification")

    def get_extension_by_install_id_for_runtime_gate(self, install_id, *, account_id):
        self.calls.append((install_id, account_id))
        return self.row


def _resolve(store):
    return _resolve_notification_extension(
        store,
        {"provider_config": {"extension_key": "fixture.notifier"}},
        account_id="account-event",
    )


def test_connectionless_notification_binds_selected_release_for_event_account():
    store = Store()
    installs = Mock()
    installs.list_for_account.return_value = [_install()]
    with patch(
        "core.application.extensions.install.DBExtensionInstallStore", return_value=installs
    ):
        resolved = _resolve(store)
    assert resolved["release_id"] == "release-v1"
    assert resolved["version"] == "1.0.0"
    assert store.calls == [("install-selected", "account-event")]
    installs.list_for_account.assert_called_once_with(
        "account-event", include_global=True, include_inactive_owned=False
    )


@pytest.mark.parametrize(
    "records",
    [
        [],
        [_install(), _install(install_id="install-other")],
        [_install(), _install(install_id="", release_id="")],
        [_install(account_id="foreign")],
        [_install(state="disabled")],
        [_install(release_id="")],
        [_install(install_id="")],
        [_install(extension_id="fixture.other")],
    ],
)
def test_connectionless_notification_rejects_missing_ambiguous_or_unscoped_install(records):
    store = Store()
    installs = Mock()
    installs.list_for_account.return_value = records
    with (
        patch("core.application.extensions.install.DBExtensionInstallStore", return_value=installs),
        pytest.raises(ValueError),
    ):
        _resolve(store)
    assert store.calls == []


def test_global_notification_install_preserves_event_account_lookup():
    store = Store(account_id="account-owner")
    installs = Mock()
    installs.list_for_account.return_value = [
        _install(account_id="account-owner", visibility="global")
    ]
    with patch(
        "core.application.extensions.install.DBExtensionInstallStore", return_value=installs
    ):
        resolved = _resolve(store)
    assert resolved["install_id"] == "install-selected"
    assert store.calls == [("install-selected", "account-event")]


@pytest.mark.parametrize(
    "changes",
    [
        {"release_id": "release-v2"},
        {"enabled": False},
        {"extension_install_id": "install-other"},
        {"extension_key": "fixture.other"},
    ],
)
def test_connectionless_notification_rechecks_selected_identity_and_enabled_gate(changes):
    store = Store(**changes)
    installs = Mock()
    installs.list_for_account.return_value = [_install()]
    with (
        patch("core.application.extensions.install.DBExtensionInstallStore", return_value=installs),
        pytest.raises(ValueError),
    ):
        _resolve(store)


def test_explicit_install_does_not_require_unique_connectionless_selection():
    store = Store()
    with patch("core.application.extensions.install.DBExtensionInstallStore") as installs:
        resolved = _resolve_notification_extension(
            store,
            {"provider_config": {"extension_key": "fixture.notifier"}},
            extension_install_id="install-selected",
            account_id="account-event",
        )
    installs.assert_not_called()
    assert resolved["install_id"] == "install-selected"
    assert store.calls == [("install-selected", "account-event")]


def test_connectionless_notification_rejects_install_deleted_after_selection():
    store = Store()
    store.row = None
    installs = Mock()
    installs.list_for_account.return_value = [_install()]
    with (
        patch("core.application.extensions.install.DBExtensionInstallStore", return_value=installs),
        pytest.raises(ValueError, match="not found"),
    ):
        _resolve(store)


@pytest.mark.parametrize("test_mode", [False, True])
def test_legacy_extension_classification_does_not_hide_binding_failure(test_mode):
    from core.application.notifications.delivery import _deliver_via_provider_runtime

    store = Store()
    store.get_extension = Mock(return_value={"kind": "notification_provider", "enabled": True})
    installs = Mock()
    installs.list_for_account.return_value = [_install(), _install(install_id="install-other")]
    with (
        patch("core.application.extensions.install.DBExtensionInstallStore", return_value=installs),
        patch(
            "core.application.extensions.provider_runtime.resolve_extension_runtime_config",
            return_value={},
        ),
        patch("core.application.extensions.external_effects.claim_external_effect") as claim,
        patch("core.application.notifications.delivery.run_extension_invocation") as invoke,
        pytest.raises(ValueError),
    ):
        _deliver_via_provider_runtime(
            bl=store,
            provider={"provider_key": "fixture.notifier"},
            event={"event_id": "event-1", "account_id": "account-event"},
            route={"route_id": "route-1", "route_config": {}},
            project_id=None,
            implementation_kind="builtin",
            test_mode=test_mode,
        )
    claim.assert_not_called()
    invoke.assert_not_called()


@pytest.mark.parametrize("binding", ["explicit", "implicit", "own", "global"])
def test_delivery_uses_bound_install_and_event_account_through_real_claim(binding):
    from core.application.notifications.delivery import _deliver_via_extension

    store = Store(account_id="account-owner" if binding == "global" else "account-event")
    connection_id = "connection-selected" if binding == "explicit" else ""
    connection_bound = binding in {"explicit", "implicit"}
    install_store = Mock()
    install_store.list_for_account.return_value = [
        _install(
            account_id="account-owner" if binding == "global" else "account-event",
            visibility="global" if binding == "global" else "account",
        )
    ]
    store.get_project_connection = Mock(
        return_value={
            "connection_id": "connection-selected",
            "extension_id": "fixture.notifier",
            "extension_install_id": "install-selected",
            "account_id": "account-event",
            "project_id": "project-event",
            "enabled": True,
        }
    )
    store.get_project_run_config = Mock(return_value={})
    # This existing resolver discovers an implicit connection without selecting
    # an executable artifact. The actual scoped connection/binder remain real.
    with (
        patch(
            "core.application.extensions.install.DBExtensionInstallStore",
            return_value=install_store,
        ) as installs,
        patch(
            "core.application.extensions.provider_runtime.resolve_extension_runtime_config",
            return_value={
                "connection_id": "connection-selected" if connection_bound else "",
                "account_id": "account-config",
            },
        ),
        patch(
            "core.application.extensions.external_effects.claim_external_effect",
            return_value={
                "effect": {"effect_id": "effect-1", "claim_token": "claim-1", "status": "claimed"},
                "should_spawn": True,
            },
        ) as claim,
        patch(
            "core.application.extensions.external_effects.cleanup_stale_external_effects",
            return_value={},
        ),
        patch(
            "core.application.extensions.external_effects.mark_external_effect_running",
            return_value={
                "effect_id": "effect-1",
                "status": "running",
            },
        ),
        patch(
            "core.application.extensions.external_effects.confirm_external_effect_from_invocation",
            return_value={
                "effect_id": "effect-1",
                "status": "succeeded",
                "__confirmed_replay_invocation": {
                    "status": "succeeded",
                    "response": {"ok": True, "delivered": True},
                },
            },
        ),
        patch(
            "core.application.notifications.delivery.run_extension_invocation",
            return_value={
                "status": "succeeded",
                "response": {"ok": True, "delivered": True},
            },
        ) as invoke,
    ):
        store.row["compiled_manifest"] = {
            "external_effects": [
                {
                    "operation_id": "deliver_notification",
                    "effect_kind": "message_delivery",
                    "channel": "notification",
                    "idempotency": "required",
                    "test_mode_behavior": "suppress",
                }
            ],
            "operation_manifest": [
                {"operation_id": "deliver_notification", "outputs": [{"name": "delivered"}]}
            ],
        }
        result = _deliver_via_extension(
            bl=store,
            provider={
                "provider_key": "fixture.notifier",
                "provider_config": {"account_id": "account-provider"},
            },
            event={"event_id": "event-1", "account_id": "account-event"},
            route={"route_id": "route-1", "route_config": {"connection_id": connection_id}},
            project_id="project-event",
        )
    assert result["status"] == "delivered"
    if connection_bound:
        installs.assert_not_called()
    else:
        install_store.list_for_account.assert_called_once_with(
            "account-event", include_global=True, include_inactive_owned=False
        )
    assert store.calls == [("install-selected", "account-event")]
    claim.assert_called_once()
    invoke.assert_called_once()
    assert claim.call_args.kwargs["account_id"] == "account-event"
    assert claim.call_args.kwargs["extension_install_id"] == "install-selected"
    assert invoke.call_args.kwargs["runtime_context"]["account_id"] == "account-event"
    assert invoke.call_args.kwargs["runtime_context"]["extension_install_id"] == "install-selected"
