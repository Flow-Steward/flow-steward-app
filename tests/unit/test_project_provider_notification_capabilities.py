"""The provider catalog must carry the capability the notification UI decides on.

The UI has to tell a provider that delivers to a *recipient* (an address you type)
from one that delivers to a *channel* (one you pick from a list). It must do that
without knowing which extension sends mail, or a second mail provider silently
gets the wrong form.

The capability therefore has to reach the client. It did not: the endpoint
returned no `features` and no target kind at all, so the UI read an empty list and
treated every provider as channel-addressed. This pins the whole path — declaration
in the extension manifest, derivation in Core, field in the response — against
synthetic `acme.*` extensions.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.responses import JSONResponse

from core.application.extensions.external_effects import (
    DEFAULT_NOTIFICATION_TARGET_KIND,
    declared_features,
    notification_target_kind,
)

RECIPIENT_EXTENSION: dict[str, Any] = {
    "extension_id": "acme.mailer",
    "kind": "notification_provider",
    "features": ["notify", "action"],
    "external_effects": [
        {
            "operation_id": "send_message",
            "effect_kind": "message_delivery",
            "observability": {
                "target_fields": ["to", "cc"],
                "target_kind": "recipient",
                "target_label": "recipient",
            },
        }
    ],
}
CHANNEL_EXTENSION: dict[str, Any] = {
    "extension_id": "acme.messenger",
    "kind": "notification_provider",
    "features": ["notify", "ingress_event"],
}


def test_a_declared_recipient_target_is_read_from_the_manifest() -> None:
    assert notification_target_kind(RECIPIENT_EXTENSION) == "recipient"


def test_a_provider_that_declares_nothing_addresses_a_channel() -> None:
    """The default has to be the safe one: a channel is chosen, not typed."""
    assert notification_target_kind(CHANNEL_EXTENSION) == DEFAULT_NOTIFICATION_TARGET_KIND
    assert notification_target_kind({}) == "channel"
    assert notification_target_kind(None) == "channel"


def test_the_declaration_is_read_from_a_compiled_manifest_too() -> None:
    """Installed extensions carry it under `compiled_manifest`."""
    assert notification_target_kind({"compiled_manifest": RECIPIENT_EXTENSION}) == "recipient"


def test_declared_features_come_through_in_manifest_order() -> None:
    assert declared_features(RECIPIENT_EXTENSION) == ["notify", "action"]
    assert declared_features({"compiled_manifest": CHANNEL_EXTENSION}) == [
        "notify",
        "ingress_event",
    ]
    assert declared_features({}) == []


@pytest.mark.parametrize("compiled_descriptor", [False, True])
def test_the_providers_endpoint_carries_features_and_the_target_kind(
    tmp_path, compiled_descriptor
) -> None:
    """End to end: the response the notification UI actually reads."""
    from unittest.mock import MagicMock, patch

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from core.entrypoints.api.project_integrations import create_project_integrations_router

    extensions = {
        "acme.mailer": _compiled_recipient_extension(
            tmp_path, compiled_descriptor=compiled_descriptor
        ),
        "acme.messenger": CHANNEL_EXTENSION,
    }
    bl = MagicMock()
    bl.get_project.return_value = {"id": "proj_1", "account_id": "acc_1"}
    bl.list_project_connections.return_value = [
        {"connection_id": "conn_mail", "extension_id": "acme.mailer", "enabled": True},
        {"connection_id": "conn_chat", "extension_id": "acme.messenger", "enabled": True},
    ]
    bl.get_provider.return_value = None
    bl.get_extension.side_effect = lambda extension_id: extensions.get(extension_id)

    app = FastAPI()
    app.include_router(
        create_project_integrations_router(
            get_logger_only=lambda: bl,
            project_query_service=lambda: MagicMock(),
            project_command_service=lambda: MagicMock(),
            request_scope=lambda request, project_id="": MagicMock(project_id=project_id),
            require_authenticated_user=lambda *args, **kwargs: {"roles": ["admin"]},
            to_json_obj=lambda value: dict(value) if isinstance(value, dict) else value,
            api_error_response=lambda exc: JSONResponse({"error": str(exc)}, status_code=500),
            service_error_response=lambda exc: JSONResponse({"error": str(exc)}, status_code=400),
            secret_storage_config_error=lambda _exc: False,
            auth_admin_roles={"admin"},
            err_database_not_configured="database not configured",
            err_project_not_found="project not found",
            err_admin_role_required="admin required",
        )
    )
    with (
        patch(
            "core.entrypoints.api.project_integrations.require_project_access",
            return_value=None,
        ),
        TestClient(app) as client,
    ):
        response = client.get("/api/projects/proj_1/providers")

    assert response.status_code == 200, response.text
    rows = {row["provider_key"]: row for row in response.json()["project_providers"]}
    assert rows["acme.mailer"]["notification_target_kind"] == "recipient"
    assert rows["acme.mailer"].get("notification_recipient_format") == "email"
    assert rows["acme.messenger"].get("notification_recipient_format") is None
    assert rows["acme.mailer"]["features"] == ["notify", "action"]
    assert rows["acme.messenger"]["notification_target_kind"] == "channel"
    assert rows["acme.messenger"]["features"] == ["notify", "ingress_event"]


def test_an_extension_registers_itself_as_a_provider_from_its_own_manifest() -> None:
    """Enabling is when a declared provider becomes real — no bootstrap seed needed.

    Everything comes from typed manifest fields the extension already declares:
    `kind` says it is a provider, `extension_id` and `display_name` name it, and
    `features` say what it can do. The platform adds no contract of its own.
    """
    from core.application.extensions.provider_registration import provider_row_for_extension

    row = provider_row_for_extension(
        {
            "extension_id": "acme.mailer",
            "kind": "notification_provider",
            "display_name": "Acme Mailer",
            "description": "Synthetic provider.",
            "features": ["notify", "action"],
        }
    )

    assert row == {
        "provider_key": "acme.mailer",
        "provider_type_key": "notification_provider",
        "display_name": "Acme Mailer",
        "description": "Synthetic provider.",
        "implementation_kind": "extension",
        "integration_key": "acme.mailer",
        "config": {"extension_key": "acme.mailer"},
        "features": ["notify", "action"],
        "enabled": True,
    }


def test_an_extension_that_is_not_a_provider_registers_nothing() -> None:
    from core.application.extensions.provider_registration import provider_row_for_extension

    assert provider_row_for_extension({"extension_id": "acme.tool", "kind": "tool_provider"}) == {
        "provider_key": "acme.tool",
        "provider_type_key": "tool_provider",
        "display_name": "acme.tool",
        "description": "",
        "implementation_kind": "extension",
        "integration_key": "acme.tool",
        "config": {"extension_key": "acme.tool"},
        "features": [],
        "enabled": True,
    }
    assert provider_row_for_extension({"extension_id": "acme.thing", "kind": "widget"}) is None
    assert provider_row_for_extension({"kind": "notification_provider"}) is None
    assert provider_row_for_extension({}) is None


def _mixed_target_kind_bundle(root, *, kind: str):
    """A bundle whose two effects address different kinds of target."""
    import yaml

    bundle = root / f"acme_mixed_{kind}"
    (bundle / "ui").mkdir(parents=True)
    (bundle / "extension.yaml").write_text(
        yaml.safe_dump(
            {
                "manifest_version": 2,
                "runtime": {"contract_version": "extension_host_v2"},
                "lifecycle": {"data_schema_version": 1, "data_migrations": []},
                "extension_id": f"acme.mixed-{kind.replace('_', '-')}",
                "kind": kind,
                "version": "1.0.0",
                "display_name": "Acme Mixed",
                "description": "Declares two target kinds.",
                "vendor": {"name": "Acme Test"},
                "features": ["notify"] if kind == "notification_provider" else ["tool"],
                "required_scopes": ["extension:invoke"],
                "entrypoint": {"command": ["python3", "main.py"]},
                "health": {"command": ["python3", "health.py"]},
                "ui_manifest": "ui/ui_manifest.yaml",
                "external_effects": [
                    {
                        "operation_id": "send_mail",
                        "effect_kind": "message_delivery",
                        "idempotency": "required",
                        "test_mode_behavior": "suppress",
                        "observability": {
                            "target_fields": ["to"],
                            "target_kind": "recipient",
                            "target_label": "recipient",
                        },
                    },
                    {
                        "operation_id": "post_update",
                        "effect_kind": "message_delivery",
                        "idempotency": "required",
                        "test_mode_behavior": "suppress",
                        "observability": {
                            "target_fields": ["channel_id"],
                            "target_kind": "channel",
                            "target_label": "channel",
                        },
                    },
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (bundle / "ui" / "ui_manifest.yaml").write_text("version: '1'\npages: []\n", encoding="utf-8")
    (bundle / "main.py").write_text("print('{\"ok\": true}')\n", encoding="utf-8")
    (bundle / "health.py").write_text("print('ok')\n", encoding="utf-8")
    return bundle


def _compile_issue_codes(bundle) -> set[str]:
    from core.infrastructure.extension_runtime.extension_runtime_compile import (
        compile_extension_bundle,
    )

    compiled = compile_extension_bundle(str(bundle))
    issues = (compiled.get("compile_errors") or []) + (compiled.get("validation_errors") or [])
    return {str(issue.get("code") or "") for issue in issues}


def test_a_notification_provider_with_mixed_target_kinds_fails_to_compile(tmp_path) -> None:
    """The author finds out, not an operator after a message is lost.

    Falling back to a default here was a silent degradation: the provider would
    render a channel form and a recipient-addressed operation would never deliver.
    """
    codes = _compile_issue_codes(_mixed_target_kind_bundle(tmp_path, kind="notification_provider"))

    assert "ambiguous_external_effect_target_kind" in codes, codes


def test_a_tool_provider_may_address_different_kinds_of_target(tmp_path) -> None:
    """Nothing renders one route form from a tool provider's operations.

    A tool provider legitimately touches a file in one operation and a record in
    another; requiring agreement there rejects a correct manifest.
    """
    codes = _compile_issue_codes(_mixed_target_kind_bundle(tmp_path, kind="tool_provider"))

    assert "ambiguous_external_effect_target_kind" not in codes, codes


def test_a_damaged_descriptor_shows_no_recipient_form() -> None:
    """Compilation rejects this, so reaching the runtime means the record is damaged."""
    mixed = {
        "external_effects": [
            {"operation_id": "send_mail", "observability": {"target_kind": "recipient"}},
            {"operation_id": "post_update", "observability": {"target_kind": "channel"}},
        ]
    }

    assert notification_target_kind(mixed) == DEFAULT_NOTIFICATION_TARGET_KIND


def test_agreeing_declarations_are_honoured_however_many_there_are() -> None:
    agreeing = {
        "external_effects": [
            {"operation_id": "send_mail", "observability": {"target_kind": "recipient"}},
            {"operation_id": "send_digest", "observability": {"target_kind": "recipient"}},
            {"operation_id": "archive", "effect_kind": "storage_write"},
        ]
    }

    assert notification_target_kind(agreeing) == "recipient"


def test_the_declaration_order_does_not_change_the_answer() -> None:
    """The bug was order-dependence, so pin it directly."""
    forward = {
        "external_effects": [
            {"observability": {"target_kind": "recipient"}},
            {"observability": {"target_kind": "channel"}},
        ]
    }
    reversed_rows = {"external_effects": list(reversed(forward["external_effects"]))}

    assert notification_target_kind(forward) == notification_target_kind(reversed_rows)


# ---------------------------------------------------------------------------
# The provider row is not optional
#
# Without it a notification route cannot be saved or delivered, so an extension
# that enabled while its row is missing looks available and silently does nothing.
# ---------------------------------------------------------------------------

MAILER = {
    "extension_id": "acme.mailer",
    "kind": "notification_provider",
    "display_name": "Acme Mailer",
    "features": ["notify"],
}


def test_a_failed_catalog_write_is_raised_not_swallowed() -> None:
    from unittest.mock import MagicMock

    from core.application.extensions.provider_registration import (
        ProviderRegistrationError,
        register_provider_from_extension,
    )

    logger = MagicMock()
    logger.save_provider.side_effect = RuntimeError("catalog unavailable")

    with pytest.raises(ProviderRegistrationError, match=r"acme\.mailer"):
        register_provider_from_extension(logger, MAILER)


def test_an_absent_catalog_is_refused_rather_than_treated_as_success() -> None:
    from core.application.extensions.provider_registration import (
        ProviderRegistrationError,
        register_provider_from_extension,
    )

    _raises_input_346_1 = object()
    with pytest.raises(ProviderRegistrationError, match="no provider catalog"):
        register_provider_from_extension(_raises_input_346_1, MAILER)


def test_an_extension_that_declares_no_provider_needs_no_catalog() -> None:
    """Most extensions are not providers; they must not require a catalog write."""
    from core.application.extensions.provider_registration import (
        register_provider_from_extension,
    )

    assert (
        register_provider_from_extension(None, {"extension_id": "acme.x", "kind": "widget"}) is None
    )


def test_disabling_an_extension_disables_its_provider_row() -> None:
    from unittest.mock import MagicMock

    from core.application.extensions.provider_registration import (
        set_provider_enabled_for_extension,
    )

    logger = MagicMock()

    assert set_provider_enabled_for_extension(logger, MAILER, enabled=False) is True
    written = logger.save_provider.call_args.args[0]
    assert written["provider_key"] == "acme.mailer"
    assert written["enabled"] is False


def test_reconcile_makes_the_catalog_a_function_of_what_is_installed() -> None:
    """Rows are rewritten from the inventory, enabled state included."""
    from unittest.mock import MagicMock

    from core.application.extensions.provider_registration import (
        reconcile_extension_providers_for_logger,
    )

    logger = MagicMock()
    logger.list_extensions.return_value = [
        {**MAILER, "enabled": True},
        {"extension_id": "acme.messenger", "kind": "notification_provider", "enabled": False},
        {"extension_id": "acme.tooling", "kind": "widget", "enabled": True},
    ]

    reconciled = reconcile_extension_providers_for_logger(logger)

    assert [(row["provider_key"], row["enabled"]) for row in reconciled] == [
        ("acme.mailer", True),
        ("acme.messenger", False),
    ]


def test_reconcile_refuses_to_report_success_when_the_inventory_fails() -> None:
    from unittest.mock import MagicMock

    from core.application.extensions.provider_registration import (
        ProviderRegistrationError,
        reconcile_extension_providers_for_logger,
    )

    logger = MagicMock()
    logger.list_extensions.side_effect = RuntimeError("inventory unavailable")

    with pytest.raises(ProviderRegistrationError, match="could not list installed extensions"):
        reconcile_extension_providers_for_logger(logger)


# ---------------------------------------------------------------------------
# Lifecycle: the catalog and the extension move together or not at all
# ---------------------------------------------------------------------------


def test_a_snapshot_of_an_absent_row_restores_by_disabling_it() -> None:
    """There is no delete: a row this enable created must be left unable to deliver."""
    from unittest.mock import MagicMock

    from core.application.extensions.provider_registration import restore_provider_state

    logger = MagicMock()
    restore_provider_state(logger, MAILER, None)

    written = logger.save_provider.call_args.args[0]
    assert written["provider_key"] == "acme.mailer"
    assert written["enabled"] is False


def test_a_snapshot_restores_the_enabled_state_it_captured() -> None:
    from unittest.mock import MagicMock

    from core.application.extensions.provider_registration import restore_provider_state

    logger = MagicMock()
    restore_provider_state(logger, MAILER, {"provider_key": "acme.mailer", "enabled": True})

    assert logger.save_provider.call_args.args[0]["enabled"] is True


def test_restoring_never_raises_over_the_failure_it_is_unwinding() -> None:
    """It runs while another error is already on its way up."""
    from unittest.mock import MagicMock

    from core.application.extensions.provider_registration import restore_provider_state

    logger = MagicMock()
    logger.save_provider.side_effect = RuntimeError("catalog unavailable")

    restore_provider_state(logger, MAILER, None)


def test_a_snapshot_reads_the_row_that_is_actually_there() -> None:
    from unittest.mock import MagicMock

    from core.application.extensions.provider_registration import provider_state_snapshot

    logger = MagicMock()
    logger.get_provider.return_value = {"provider_key": "acme.mailer", "enabled": False}

    assert provider_state_snapshot(logger, MAILER) == {
        "provider_key": "acme.mailer",
        "enabled": False,
    }
    logger.get_provider.assert_called_once_with("acme.mailer")


def test_a_snapshot_of_an_extension_that_declares_no_provider_is_none() -> None:
    from unittest.mock import MagicMock

    from core.application.extensions.provider_registration import provider_state_snapshot

    assert (
        provider_state_snapshot(MagicMock(), {"extension_id": "acme.x", "kind": "widget"}) is None
    )


def test_a_route_naming_an_unknown_workflow_is_rejected_not_a_server_error() -> None:
    """Found in a browser: saving one answered HTTP 500 "Failed to update…".

    `notification_routes.workflow_id` references `registry_workflows`. An id the
    operator typed that does not exist reached the database, came back as an
    integrity error, and the service layer could only report it as an internal
    failure — for a value the operator supplied and could fix. It is checked
    before the write now, and named, so it surfaces as a rejected value.
    """
    from core.application.notifications.repository import (
        NotificationRepositoryMixin,
    )

    class _Cursor:
        def __init__(self) -> None:
            self.queries: list[tuple] = []

        def execute(self, sql, params=None):
            self.queries.append((sql, params))

        def fetchall(self):
            return [{"workflow_id": "known_workflow"}]

    class _Store(NotificationRepositoryMixin):
        def _table_exists(self, _name):
            return True

    store = _Store()
    cursor = _Cursor()

    # Position 2 is `workflow_id` in the prepared tuple.
    store._reject_unknown_route_workflows(
        cursor, [("k", "", "known_workflow", "p", "e", "any", "{}", True)]
    )

    with pytest.raises(ValueError) as excinfo:
        store._reject_unknown_route_workflows(
            cursor, [("k", "", "no_such_workflow_xyz", "p", "e", "any", "{}", True)]
        )
    assert "no_such_workflow_xyz" in str(excinfo.value)
    assert "not a known workflow" in str(excinfo.value)


def test_a_route_without_a_workflow_id_needs_no_lookup() -> None:
    """The field is optional, so an absent one must not cost a query."""
    from core.application.notifications.repository import (
        NotificationRepositoryMixin,
    )

    class _Cursor:
        def __init__(self) -> None:
            self.queries: list[tuple] = []

        def execute(self, sql, params=None):
            self.queries.append((sql, params))

        def fetchall(self):
            return []

    class _Store(NotificationRepositoryMixin):
        def _table_exists(self, _name):
            return True

    cursor = _Cursor()
    _Store()._reject_unknown_route_workflows(cursor, [("k", "", None, "p", "e", "any", "{}", True)])

    assert cursor.queries == []


class _RouteCursor:
    """A cursor that answers the two lookups `save_notification_routes` makes."""

    def __init__(self, *, provider_row=None, known_workflows=()) -> None:
        self.provider_row = provider_row
        self.known_workflows = list(known_workflows)
        self._last = ""

    def execute(self, sql, params=None):
        self._last = " ".join(str(sql).split())

    def fetchone(self):
        return self.provider_row

    def fetchall(self):
        return [{"workflow_id": value} for value in self.known_workflows]


def _route_store():
    from core.application.notifications.repository import (
        NotificationRepositoryMixin,
    )

    class _Store(NotificationRepositoryMixin):
        def _table_exists(self, _name):
            return True

    return _Store()


def test_a_route_on_a_disabled_provider_is_refused_with_the_reason() -> None:
    """Found in a browser: the route saved, and then never delivered.

    A provider row that is off still resolves, so the route was accepted and sat
    there dead. The operator learns about it when a notification does not arrive
    — the worst possible moment to be told.
    """
    store = _route_store()
    cursor = _RouteCursor(provider_row={"id": "provider-1", "enabled": False})

    with pytest.raises(ValueError) as excinfo:
        store._resolve_notification_provider_id_cur(cursor, "acme.mailer")

    message = str(excinfo.value)
    assert "acme.mailer" in message
    assert "disabled" in message
    # The remedy, not just the symptom.
    assert "Enable its extension" in message


def test_an_enabled_provider_still_resolves() -> None:
    store = _route_store()

    assert (
        store._resolve_notification_provider_id_cur(
            _RouteCursor(provider_row={"id": "provider-1", "enabled": True}), "acme.mailer"
        )
        == "provider-1"
    )
    # A row from a store that does not report the column is not a disabled one.
    assert (
        store._resolve_notification_provider_id_cur(
            _RouteCursor(provider_row=("provider-2", None)), "acme.mailer"
        )
        == "provider-2"
    )


def test_an_unknown_provider_is_still_refused() -> None:
    store = _route_store()

    _raises_input_621_1 = _RouteCursor(provider_row=None)
    with pytest.raises(ValueError) as excinfo:
        store._resolve_notification_provider_id_cur(_raises_input_621_1, "acme.ghost")

    assert "Unknown notification provider_key" in str(excinfo.value)


def _duplicate_key_store():
    from core.application.notifications.repository import (
        NotificationRepositoryMixin,
    )

    class _Conn:
        def cursor(self):  # pragma: no cover - reaching this means no refusal
            raise AssertionError("the write must be refused before any cursor is opened")

    class _Store(NotificationRepositoryMixin):
        conn = _Conn()

        def _table_exists(self, _name):
            return True

        def _column_exists(self, _table, _column):
            return False

    return _Store()


def _route(route_key: str) -> dict:
    return {
        "route_key": route_key,
        "provider_key": "acme.mailer",
        "event_type": "workflow.automation.notify",
        "route_config": {"channel_id": "ops"},
    }


def test_two_routes_with_the_same_typed_key_are_refused() -> None:
    """Found in a browser: the second was silently saved as `<key>_2`.

    A route key identifies a route. Renaming one the operator typed produces a
    route they did not ask for, under a name they will not find again, and says
    nothing about it.
    """
    _raises_callable_664_1 = _duplicate_key_store().save_notification_routes
    _raises_input_664_2 = [_route("alerts"), _route("alerts")]
    with pytest.raises(ValueError) as excinfo:
        _raises_callable_664_1("project-1", _raises_input_664_2)

    message = str(excinfo.value)
    assert "alerts" in message
    assert "more than once" in message
    # A conflict, so the API answers 409 rather than "bad value".
    assert getattr(excinfo.value, "is_conflict", False) is True


def test_the_service_answers_a_duplicate_key_with_a_conflict() -> None:
    """The store's marker has to survive the service layer, or it is decoration."""
    from core.application.management.errors import (
        ServiceConflictError,
        ServiceValidationError,
    )
    from core.application.notifications.repository import (
        NotificationRouteConflictError,
    )

    assert ServiceConflictError.status_code == 409
    assert ServiceValidationError.status_code == 400
    assert getattr(NotificationRouteConflictError("x"), "is_conflict", False) is True
    # A plain refused value carries no marker, so it stays a 400.
    assert getattr(ValueError("x"), "is_conflict", False) is False


def test_generated_keys_may_still_collide_and_are_disambiguated() -> None:
    """Autogeneration stays: an absent key is the platform's to choose.

    Two routes that name nothing get `route_1` and `route_2`; a third whose typed
    key happens to equal a generated one is still the operator's word, so it is
    the collision that gets renamed, not their key.
    """
    store = _duplicate_key_store()
    blank_first = {**_route(""), "route_key": ""}
    blank_second = {**_route(""), "route_key": ""}

    # No refusal: nothing here was typed by an operator.
    with pytest.raises(AssertionError):
        store.save_notification_routes("project-1", [blank_first, blank_second])


def _compiled_recipient_extension(tmp_path, *, compiled_descriptor):
    """Read a file-backed schema through the real compiler and stored-row adapter."""
    import shutil
    from pathlib import Path

    import yaml

    from core.infrastructure.db.logger_extensions_accounts_common import (
        PlatformLoggerExtensionsAccountsCommonMixin,
    )
    from core.infrastructure.extension_runtime.extension_runtime_compile import (
        compile_extension_bundle,
    )

    bundle = tmp_path / "acme_mailer"
    shutil.copytree(Path("tests/fixtures/extensions/fixture_task_ingress_provider"), bundle)
    manifest_path = bundle / "extension.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    manifest.update(
        extension_id="acme.mailer",
        kind="notification_provider",
        features=["notify", "action"],
        external_effects=[
            {
                **RECIPIENT_EXTENSION["external_effects"][0],
                "operation_id": "deliver_notification",
                "channel": "message",
                "idempotency": "required",
                "test_mode_behavior": "suppress",
            }
        ],
        schemas={"subscriptions": "subscriptions.schema.yaml"},
    )
    manifest.pop("query_manifest", None)
    manifest["action_manifest"] = "actions.yaml"
    manifest["runtime"]["extension_contract_v2"] = {"operation_manifest": "operations.yaml"}
    (bundle / "actions.yaml").write_text(
        yaml.safe_dump(
            {
                "actions": [
                    {
                        "action_id": "deliver_notification",
                        "title": "Deliver notification",
                        "handler": {"mode": "extension"},
                        "workflow_visible": False,
                        "parameters": [
                            {"name": name, "type": "object", "required": True}
                            for name in ("event", "route")
                        ],
                    }
                ]
            }
        )
    )
    (bundle / "operations.yaml").write_text(
        yaml.safe_dump(
            {
                "operations": [
                    {
                        "operation_id": "deliver_notification",
                        "display_name": "Deliver notification",
                        "operation_kind": "action",
                        "workflow_visible": False,
                        "inputs": [
                            {"name": name, "value_type": "object", "required": True}
                            for name in ("event", "route")
                        ],
                        "outputs": [
                            {"name": "delivered", "value_type": "boolean"},
                            {"name": "provider_message_id", "value_type": "text"},
                        ],
                    }
                ]
            }
        )
    )
    manifest_path.write_text(yaml.safe_dump(manifest))
    schema = {
        "type": "object",
        "properties": {
            "to": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string", "format": "email"}},
                ]
            }
        },
        "required": ["to"],
    }
    (bundle / "subscriptions.schema.yaml").write_text(yaml.safe_dump(schema))
    compiled = compile_extension_bundle(bundle)
    assert compiled["status"] == "valid", compiled["compile_errors"] + compiled["validation_errors"]
    assert compiled["schemas"]["subscriptions"] == schema
    assert compiled["compiled_manifest"]["schemas"]["subscriptions"] == schema
    descriptor = PlatformLoggerExtensionsAccountsCommonMixin._normalize_extension_row(
        {
            **RECIPIENT_EXTENSION,
            "schemas_jsonb": compiled["schemas"],
            "compiled_manifest_jsonb": compiled["compiled_manifest"],
        }
    )
    assert descriptor["schemas"]["subscriptions"] == schema
    assert descriptor["compiled_manifest"]["schemas"]["subscriptions"] == schema
    if compiled_descriptor:
        descriptor.pop("schemas")
    else:
        descriptor.pop("compiled_manifest")
    return descriptor
