"""Mixed notification providers, the system email sender and delivery credentials.

On the canonical PostgreSQL schema, with a real extension process: a small
notification extension written to a temporary bundle records every request it
receives on stdin and answers from a plan. Connections, external effects,
sender revisions, OAuth configurations and settings are real rows; only the
install catalog lookup is replaced by a registry of "installed" records that
point at that bundle.
"""

from __future__ import annotations

import contextlib
import copy
import json
import logging
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.extensions import (
    notification_capability,
    operation_targets,
    system_email,
    system_email_sender,
)
from core.application.extensions.oauth_runtime import OAuthRuntimeTokenError
from core.application.management.errors import ServiceForbiddenError
from core.application.management.scope import ScopeContext
from core.application.notifications import delivery as notification_delivery
from core.application.notifications import route_connections
from core.application.notifications.dispatch import dispatch_project_notification_event

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("notification_providers")]

EXT = "fixture.notify-mixed"
BOUND_EXT = "fixture.notify-bound"
#: An install is unique per extension and account, so "another install" is
#: another extension's install with the same connection types.
OTHER_EXT = "fixture.notify-other"

_EXTENSION_MAIN = r"""
import json, pathlib, sys
root = pathlib.Path.cwd()
request = json.loads(sys.stdin.read() or "{}")
log = root / "requests.jsonl"
with log.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(request) + "\n")
count = sum(1 for _ in log.open(encoding="utf-8"))
plan_file = root / "plan.json"
plan = json.loads(plan_file.read_text(encoding="utf-8")) if plan_file.exists() else []
response = plan[count - 1] if count <= len(plan) and plan[count - 1] else None
if response is None:
    response = {"ok": True, "delivered": True, "provider_message_id": f"msg-{count}"}
print(json.dumps(response))
"""

MAIL_TYPE = {
    "connection_type_id": "example_mail",
    "notification": {"target_kind": "recipient", "ledger_channel": "email"},
}
CHAT_TYPE = {
    "connection_type_id": "example_chat",
    "notification": {"target_kind": "channel", "ledger_channel": "chat"},
}
BOUND_TYPE = {
    "connection_type_id": "example_chat_bound",
    "notification": {
        "target_kind": "channel",
        "ledger_channel": "chat",
        "channel_source": "connection",
    },
}
FILES_TYPE = {"connection_type_id": "example_files"}
#: An application connection that takes its client values from an OAuth configuration.
APP_TYPE = {
    "connection_type_id": "example_mail_app",
    "notification": {"target_kind": "recipient", "ledger_channel": "email"},
    "config_schema": {"type": "object", "properties": {"sender": {"type": "string"}}},
    "credentials_from": {
        "oauth_provider_id": "example_oauth",
        "fields": {
            "client_id": {"into": "config", "as": "client_id"},
            "client_secret": {"into": "secrets", "as": "client_secret"},
            "tenant_id": {"into": "config", "as": "tenant_id"},
        },
    },
}
OAUTH_PROVIDER = {
    "provider_id": "example_oauth",
    "credential_fields": [
        {"field_id": "client_id", "secret": False, "required": True},
        {"field_id": "client_secret", "secret": True, "required": True},
        {"field_id": "tenant_id", "secret": False, "required": True, "usage": "runtime"},
    ],
}


def _manifest(connection_types: list[dict[str, Any]]) -> dict[str, Any]:
    effect = {
        "operation_id": "deliver_notification",
        "effect_kind": "message_delivery",
        "channel": "email",
        "idempotency": "required",
        "test_mode_behavior": "suppress",
        "observability": {"target_kind": "recipient"},
    }
    operation = {
        "operation_id": "deliver_notification",
        "operation_kind": "action",
        "workflow_visible": False,
        "inputs": [
            {"name": "event", "value_type": "object", "required": True},
            {"name": "route", "value_type": "object", "required": True},
        ],
        "outputs": [
            {"name": "delivered", "value_type": "boolean"},
            {"name": "provider_message_id", "value_type": "text"},
        ],
    }
    return {
        "kind": "notification_provider",
        "connection_types": connection_types,
        "oauth_providers": [OAUTH_PROVIDER],
        "external_effects": [effect],
        "operation_manifest": [operation],
    }


@dataclass
class Installed:
    """One "installed" extension record and the bundle its process runs from."""

    extension_key: str
    install_id: str
    bundle: Path
    connection_types: list[dict[str, Any]]
    enabled: bool = True
    primary: bool = True

    def record(self) -> dict[str, Any]:
        manifest = _manifest(self.connection_types)
        return {
            "extension_id": self.extension_key,
            "extension_key": self.extension_key,
            "display_name": f"{self.extension_key} fixture",
            "extension_install_id": self.install_id,
            "install_id": self.install_id,
            "release_id": f"rel-{self.install_id}",
            "account_id": DEFAULT_ACCOUNT_ID,
            "enabled": self.enabled,
            "kind": "notification_provider",
            "bundle_root": str(self.bundle),
            "entrypoint": {"mode": "subprocess", "command": [sys.executable, "main.py"]},
            "runtime": {"contract_version": "extension_host_v2"},
            "compiled_manifest": manifest,
            "external_effects": manifest["external_effects"],
            "operation_manifest": manifest["operation_manifest"],
            "connection_types": self.connection_types,
        }

    def requests(self) -> list[dict[str, Any]]:
        log = self.bundle / "requests.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def plan(self, responses: list[dict[str, Any] | None]) -> None:
        (self.bundle / "plan.json").write_text(json.dumps(responses), encoding="utf-8")


def _store_class():
    from core.infrastructure.db.platform_stores import PlatformIntegrationStore

    class SpyStore(PlatformIntegrationStore):
        """The platform integration store; invocation and delivery logs are kept for asserts."""

        invocation_logs: list[dict[str, Any]]
        delivery_logs: list[dict[str, Any]]
        catalog: dict[str, dict[str, Any]]

        def log_extension_invocation(self, **kwargs: Any) -> None:
            self.invocation_logs.append(copy.deepcopy(kwargs))

        def log_notification_delivery(self, **kwargs: Any) -> dict[str, Any]:
            self.delivery_logs.append(copy.deepcopy(kwargs))
            return {**kwargs}

        def get_extension(self, extension_key: str) -> dict[str, Any] | None:
            return self.catalog.get(extension_key)

    return SpyStore


@contextmanager
def _dedicated_store(dsn: str) -> Iterator[Any]:
    from core.infrastructure.db import psycopg_runtime

    cls = _store_class()
    connection = psycopg_runtime.connect(dsn, autocommit=True)
    store = cls.__new__(cls)
    store.dsn = dsn
    store._direct_conn = connection
    store._pool = None
    store.conn = connection
    store._column_exists_cache = {}
    store._lifecycle_starts = {}
    store.invocation_logs = []
    store.delivery_logs = []
    store.catalog = {}
    try:
        yield store
    finally:
        connection.close()


@dataclass
class World:
    dsn: str
    store: Any
    project_id: str
    other_project_id: str
    tenant_id: str
    installed: dict[str, Installed]
    connections: dict[str, str] = field(default_factory=dict)

    @property
    def mixed(self) -> Installed:
        return self.installed["mixed"]

    def sql(self, query: str, params: tuple = ()) -> list[tuple]:
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            cur = conn.execute(query, params)
            return list(cur.fetchall()) if cur.description else []

    def revision(self, connection_id: str) -> int:
        [(value,)] = self.sql(
            "SELECT sender_revision FROM project_connections WHERE connection_id = %s",
            (connection_id,),
        )
        return int(value)

    def effects(self, connection_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT row_to_json(e)::jsonb FROM external_effects e WHERE project_id = ANY(%s)"
        params: tuple = ([self.project_id, self.other_project_id],)
        if connection_id:
            query += " AND connection_id = %s"
            params = (*params, connection_id)
        return [row[0] for row in self.sql(query + " ORDER BY created_at", params)]

    def dispatch(self, routes: list[dict[str, Any]], *, event_id: str, **kwargs: Any):
        return dispatch_project_notification_event(
            self.store,
            project_id=self.project_id,
            event_type=kwargs.pop("event_type", "job.failed"),
            severity="error",
            workflow_id="wf-notify",
            payload=kwargs.pop("payload", {"summary": "Invoice sync failed", "error": "Timeout"}),
            event_id=event_id,
            account_id=DEFAULT_ACCOUNT_ID,
            direct_routes=routes,
            ignore_filters=True,
            **kwargs,
        )


def _route(route_key: str, provider_key: str = EXT, **route_config: Any) -> dict[str, Any]:
    return {
        "route_id": None,
        "route_key": route_key,
        "provider_key": provider_key,
        "implementation_kind": "extension",
        "event_type": "*",
        "severity": "any",
        "enabled": True,
        "route_config": route_config,
    }


@pytest.fixture
def world(disposable_db, tmp_path, monkeypatch) -> Iterator[World]:
    from core.infrastructure.db import db_pool

    monkeypatch.setattr(db_pool, "_runtime_env", {"FS_DB_POOL_ENABLED": "0"})
    system_email_sender.FAULT_POINTS.clear()
    system_email_sender.clear_availability_cache()
    project_id, other_project_id = str(uuid4()), str(uuid4())
    installs = {
        "mixed": Installed(
            EXT,
            str(uuid4()),
            tmp_path / "mixed",
            [MAIL_TYPE, CHAT_TYPE, BOUND_TYPE, FILES_TYPE, APP_TYPE],
        ),
        "mixed_other": Installed(
            OTHER_EXT, str(uuid4()), tmp_path / "mixed_other", [MAIL_TYPE, CHAT_TYPE]
        ),
        "bound": Installed(BOUND_EXT, str(uuid4()), tmp_path / "bound", [BOUND_TYPE]),
    }
    for item in installs.values():
        item.bundle.mkdir()
        (item.bundle / "main.py").write_text(_EXTENSION_MAIN, encoding="utf-8")
    with psycopg.connect(disposable_db.dsn, autocommit=True) as conn:
        conn.execute(
            "DELETE FROM system_settings WHERE setting_key = %s", (system_email_sender.SETTING_KEY,)
        )
        # Sender candidates span every project: start each test from none.
        conn.execute(
            "DELETE FROM project_connections WHERE extension_id = ANY(%s)",
            ([EXT, BOUND_EXT, OTHER_EXT],),
        )
        for pid in (project_id, other_project_id):
            conn.execute(
                "INSERT INTO projects (id, name, account_id) VALUES (%s, %s, %s)",
                (pid, f"Notify fixture {pid}", DEFAULT_ACCOUNT_ID),
            )
        [(tenant_id,)] = conn.execute(
            "SELECT tenant_id FROM projects WHERE id = %s", (project_id,)
        ).fetchall()
        for item in installs.values():
            # One install per extension and account; every test shares it and
            # keeps to its own projects.
            conn.execute(
                "INSERT INTO extension_installs (extension_id, install_id, account_id) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (item.extension_key, item.install_id, DEFAULT_ACCOUNT_ID),
            )
            [(item.install_id,)] = conn.execute(
                "SELECT install_id FROM extension_installs WHERE extension_id = %s AND account_id = %s",
                (item.extension_key, DEFAULT_ACCOUNT_ID),
            ).fetchall()
            conn.execute(
                "INSERT INTO extension_releases (release_id, extension_id, release_version) "
                "VALUES (%s, %s, '1.0.0') ON CONFLICT DO NOTHING",
                (f"rel-{item.install_id}", item.extension_key),
            )
    with _dedicated_store(disposable_db.dsn) as store:
        built = World(
            dsn=disposable_db.dsn,
            store=store,
            project_id=project_id,
            other_project_id=other_project_id,
            tenant_id=str(tenant_id),
            installed=installs,
        )
        registry = {item.install_id: item for item in installs.values()}
        store.catalog = {
            item.extension_key: item.record() for item in installs.values() if item.primary
        }

        def resolve(_bl, provider, *, extension_install_id="", account_id=""):
            key = notification_delivery._notification_extension_key(provider)
            if extension_install_id:
                item = registry.get(extension_install_id)
                if item is None or item.extension_key != key:
                    raise ValueError("Notification extension identity mismatch")
            else:
                matches = [i for i in registry.values() if i.extension_key == key and i.primary]
                if len(matches) != 1:
                    raise ValueError("Notification provider requires one active visible install")
                item = matches[0]
            if not item.enabled:
                raise ValueError(f"Extension '{key}' is disabled")
            return item.record()

        monkeypatch.setattr(notification_delivery, "_resolve_notification_extension", resolve)
        monkeypatch.setattr(
            route_connections,
            "provider_install_id",
            lambda _bl, *, extension_key, account_id: next(
                (
                    i.install_id
                    for i in registry.values()
                    if i.extension_key == extension_key and i.primary
                ),
                "",
            ),
        )
        from core.application.extensions import connection_credentials

        monkeypatch.setattr(
            connection_credentials,
            "_extension_for_connection",
            lambda _bl, connection: (
                registry[connection["extension_install_id"]].record()
                if connection.get("extension_install_id") in registry
                else {}
            ),
        )
        monkeypatch.setattr(
            system_email_sender,
            "_extension_for_install",
            lambda _bl, install_id, _account_id: (
                registry[install_id].record() if install_id in registry else {}
            ),
        )
        yield built
    system_email_sender.FAULT_POINTS.clear()
    system_email_sender.clear_availability_cache()


def _connection(
    world: World,
    name: str,
    *,
    install: str = "mixed",
    connection_type: str,
    project_id: str | None = None,
    config: dict[str, Any] | None = None,
    oauth_configuration_id: str | None = None,
) -> str:
    item = world.installed[install]
    connection_id = f"conn_{name}_{uuid4().hex[:8]}"
    oauth = bool(oauth_configuration_id)
    world.sql(
        "INSERT INTO project_connections (connection_id, project_id, extension_id, account_id, "
        "extension_install_id, connection_kind, connection_type, display_name, connection_config, "
        "oauth_configuration_id, oauth_provider_id, oauth_extension_release_id, "
        "oauth_authorization_revision, oauth_provider_security_fingerprint, "
        "oauth_provider_revocation_fingerprint) "
        "VALUES (%s, %s, %s, %s, %s, 'extension', %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s)",
        (
            connection_id,
            project_id or world.project_id,
            item.extension_key,
            DEFAULT_ACCOUNT_ID,
            item.install_id,
            connection_type,
            f"{name} connection",
            json.dumps(config or {}),
            oauth_configuration_id,
            "example_oauth" if oauth else None,
            f"rel-{item.install_id}" if oauth else None,
            1 if oauth else None,
            "sec-1" if oauth else None,
            "rev-1" if oauth else None,
        ),
    )
    world.connections[name] = connection_id
    return connection_id


def _oauth_configuration(world: World, install: str = "mixed") -> str:
    item = world.installed[install]
    configuration_id = str(uuid4())
    world.sql(
        "INSERT INTO extension_oauth_configurations (configuration_id, tenant_id, account_id, "
        "project_id, extension_install_id, extension_id, extension_release_id, provider_id, "
        "display_name, status, non_secret_config, provider_security_fingerprint, "
        "provider_revocation_fingerprint) VALUES (%s, %s, %s, %s, %s, %s, %s, 'example_oauth', "
        "'Example sign-in', 'verified', %s::jsonb, 'sec-1', 'rev-1')",
        (
            configuration_id,
            world.tenant_id,
            DEFAULT_ACCOUNT_ID,
            world.project_id,
            item.install_id,
            item.extension_key,
            f"rel-{item.install_id}",
            json.dumps({"client_id": "client-1"}),
        ),
    )
    return configuration_id


def _scope(principal_type: str, *, token_scopes: frozenset[str] = frozenset(), project_id=None):
    return ScopeContext(
        account_id=DEFAULT_ACCOUNT_ID,
        principal_type=principal_type,
        actor_id=f"{principal_type}-user",
        project_id=project_id,
        token_scopes=token_scopes,
    )


PLATFORM_ADMIN = _scope("admin")


# --------------------------------------------------------------------------- C1


def test_routes_that_the_contract_refuses_are_not_saved(world: World) -> None:
    mail = _connection(world, "mail", connection_type="example_mail")
    chat = _connection(world, "chat", connection_type="example_chat")
    bound = _connection(
        world, "bound", connection_type="example_chat_bound", config={"channel_label": "Operations"}
    )
    files = _connection(world, "files", connection_type="example_files")
    other_project = _connection(
        world, "elsewhere", connection_type="example_mail", project_id=world.other_project_id
    )
    other_install = _connection(
        world, "other_install", install="mixed_other", connection_type="example_mail"
    )
    only_bound = _connection(
        world,
        "only_bound",
        install="bound",
        connection_type="example_chat_bound",
        config={"channel_label": "Ops"},
    )
    routes = [
        _route("ok_mail", connection_id=mail, to=["ops@example.com"]),
        _route("ok_chat", connection_id=chat, channel_id="ops"),
        _route("ok_bound", connection_id=bound),
        _route("ok_only_bound", provider_key=BOUND_EXT, connection_id=only_bound),
        _route("mixed_no_connection", to=["ops@example.com"]),
        _route("bound_no_connection", provider_key=BOUND_EXT),
        _route("wrong_kind", connection_id=chat, to=["ops@example.com"]),
        _route("other_install", connection_id=other_install, to=["ops@example.com"]),
        _route("other_project", connection_id=other_project, to=["ops@example.com"]),
        _route("undeclared_type", connection_id=files, to=["ops@example.com"]),
        _route("channel_to_bound", connection_id=bound, channel_id="ops"),
    ]

    refused = route_connections.validate_routes_for_save(
        world.store, project_id=world.project_id, account_id=DEFAULT_ACCOUNT_ID, routes=routes
    )

    assert {routes[index]["route_key"]: decision.error for index, decision in refused} == {
        "mixed_no_connection": "notification_connection_required",
        "bound_no_connection": "notification_connection_required",
        "wrong_kind": "notification_connection_incompatible",
        "other_install": "notification_connection_incompatible",
        "other_project": "notification_connection_incompatible",
        "undeclared_type": "notification_connection_incompatible",
        "channel_to_bound": "notification_connection_incompatible",
    }


def test_the_route_save_service_refuses_with_the_contract_code(world: World) -> None:
    from core.application.management.command_services_project_crud_mixin import (
        ProjectCommandServiceCrudMixin,
    )
    from core.application.management.errors import ServiceValidationError

    with pytest.raises(ServiceValidationError) as raised:
        ProjectCommandServiceCrudMixin._reject_incompatible_route_connections(
            world.store,
            world.project_id,
            [_route("mixed_no_connection", to=["a@example.com"])],
            PLATFORM_ADMIN,
        )
    assert raised.value.payload["code"] == "notification_connection_required"
    assert raised.value.payload["route_key"] == "mixed_no_connection"


@pytest.mark.parametrize(
    "case", ["no_connection", "other_project", "wrong_kind", "channel_to_bound"]
)
def test_a_refused_route_fails_delivery_without_retry_and_never_calls(
    world: World, monkeypatch, case
) -> None:
    from core.application.extensions import provider_runtime

    # The project's runtime connection is a working mail connection; a route
    # that must name its connection is still never delivered through it.
    runtime_mail = _connection(world, "runtime_mail", connection_type="example_mail")
    monkeypatch.setattr(
        provider_runtime,
        "resolve_extension_runtime_config",
        lambda *_a, **_k: {"project_connection": {"connection_id": runtime_mail}},
    )
    chat = _connection(world, "chat", connection_type="example_chat")
    bound = _connection(
        world, "bound", connection_type="example_chat_bound", config={"channel_label": "Ops"}
    )
    elsewhere = _connection(
        world, "elsewhere", connection_type="example_mail", project_id=world.other_project_id
    )
    route = {
        # A route stored before the change, without connection_id.
        "no_connection": _route("legacy", to=["ops@example.com"]),
        "other_project": _route("x", connection_id=elsewhere, to=["ops@example.com"]),
        "wrong_kind": _route("x", connection_id=chat, to=["ops@example.com"]),
        "channel_to_bound": _route("x", connection_id=bound, channel_id="ops"),
    }[case]

    result = notification_delivery._deliver_via_extension(
        bl=world.store,
        provider=route,
        event={
            "event_id": f"evt-{case}",
            "event_type": "job.failed",
            "account_id": DEFAULT_ACCOUNT_ID,
        },
        route=route,
        project_id=world.project_id,
    )

    assert result["status"] == "failed"
    assert result["retryable"] is False
    assert result["error_text"] == (
        "notification_connection_required"
        if case == "no_connection"
        else "notification_connection_incompatible"
    )
    assert world.mixed.requests() == []
    assert world.effects() == []


def test_one_event_two_routes_both_delivered_and_a_replay_delivers_nothing_new(
    world: World,
) -> None:
    mail = _connection(world, "mail", connection_type="example_mail")
    chat = _connection(world, "chat", connection_type="example_chat")
    routes = [
        _route("on_call", connection_id=mail, to=["oncall@example.com"]),
        _route("operations", connection_id=chat, channel_id="ops"),
    ]

    first = world.dispatch(routes, event_id="evt-two-routes")

    assert [row["status"] for row in first] == ["delivered", "delivered"]
    requests = world.mixed.requests()
    assert len(requests) == 2
    by_connection = {
        r["action_payload"]["target"]["connection"]["connection_id"]: r for r in requests
    }
    mail_request, chat_request = by_connection[mail], by_connection[chat]
    # The full local delivery request, as the extension-author guide shows it.
    assert set(mail_request) == {
        "action",
        "idempotency_key",
        "external_effect_id",
        "action_payload",
        "event",
        "route",
        "provider",
        "contract_version",
        "runtime_context",
    }
    assert mail_request["action"] == "deliver_notification"
    assert (
        mail_request["runtime_context"]["external_effect_id"] == mail_request["external_effect_id"]
    )
    assert set(mail_request["action_payload"]["target"]["connection"]) >= {
        "connection_id",
        "connection_type",
        "config",
        "secrets",
    }
    # Present only on a sensitive event (absent means false).
    assert "payload_sensitive" not in mail_request["event"]
    # The standard email layout is for the recipient route only.
    assert mail_request["route"]["route_config"]["subject"].startswith("❌ Workflow failed")
    assert mail_request["event"]["payload"]["message"].startswith("❌ Workflow failed")
    assert "subject" not in chat_request["route"]["route_config"]
    assert "message" not in chat_request["event"]["payload"]
    # One effect per route, each with its own key, on its type's ledger channel.
    assert mail_request["idempotency_key"] and chat_request["idempotency_key"]
    assert mail_request["idempotency_key"] != chat_request["idempotency_key"]
    assert mail_request["external_effect_id"] != chat_request["external_effect_id"]
    channels = {effect["connection_id"]: effect for effect in world.effects()}
    assert channels[mail]["channel"] == "email"
    assert channels[chat]["channel"] == "chat"
    assert channels[mail]["source_ref_jsonb"]["target_kind"] == "recipient"
    assert channels[chat]["source_ref_jsonb"]["target_kind"] == "channel"

    replay = world.dispatch(routes, event_id="evt-two-routes")

    assert [row["status"] for row in replay] == ["delivered", "delivered"]
    assert {row["provider_message_id"] for row in replay} == {
        row["provider_message_id"] for row in first
    }
    assert len(world.mixed.requests()) == 2, "a replay must not call the extension again"
    assert len(world.effects()) == 2


def test_a_provider_with_only_a_connection_bound_channel_delivers_without_a_channel(
    world: World,
) -> None:
    bound = _connection(
        world,
        "only_bound",
        install="bound",
        connection_type="example_chat_bound",
        config={"channel_label": "Operations"},
    )

    [row] = world.dispatch(
        [_route("ops", provider_key=BOUND_EXT, connection_id=bound)], event_id="evt-bound"
    )

    assert row["status"] == "delivered"
    [request] = world.installed["bound"].requests()
    assert "channel_id" not in request["route"]["route_config"]
    assert "channel" not in request["route"]["route_config"]
    assert (
        request["action_payload"]["target"]["connection"]["config"]["channel_label"] == "Operations"
    )


def test_the_listing_shows_channel_source_and_label_for_installed_connections(world: World) -> None:
    bound = _connection(
        world, "bound", connection_type="example_chat_bound", config={"channel_label": "Operations"}
    )
    row = world.store.get_project_connection(connection_id=bound)
    view = notification_capability.notification_connection_view(world.mixed.record(), row)
    assert (view["target_kind"], view["channel_source"], view["channel_label"]) == (
        "channel",
        "connection",
        "Operations",
    )
    assert view["notification_target_kind"] == "mixed"


# --------------------------------------------------------------------------- C3


@pytest.mark.parametrize("base_url", ["https://fs.example", ""])
def test_the_delivery_request_carries_the_run_url_when_the_platform_knows_it(
    world: World, monkeypatch, base_url
) -> None:
    from core.application.auth import serve_auth_runtime

    monkeypatch.setattr(serve_auth_runtime, "public_base_url", lambda: base_url)
    chat = _connection(world, "chat", connection_type="example_chat")
    job_id = str(uuid4())
    approval_job_id = str(uuid4())
    route = _route("operations", connection_id=chat, channel_id="ops")

    world.dispatch([route], event_id=f"evt-run-{base_url or 'none'}", job_id=job_id)
    world.dispatch(
        [route],
        event_id=f"evt-approval-{base_url or 'none'}",
        event_type="approval.requested",
        job_id=job_id,
        payload={"summary": "Needs approval", "approval_job_id": approval_job_id},
    )

    job_request, approval_request = world.mixed.requests()
    if base_url:
        assert job_request["event"]["run_url"] == f"https://fs.example/jobs/{job_id}"
        assert approval_request["event"]["run_url"] == f"https://fs.example/jobs/{approval_job_id}"
    else:
        assert "run_url" not in job_request["event"]
        assert "run_url" not in approval_request["event"]


def test_a_retry_after_a_confirmed_pre_effect_failure_carries_the_same_key(world: World) -> None:
    mail = _connection(world, "mail", connection_type="example_mail")
    world.mixed.plan(
        [
            {
                "ok": False,
                "delivered": False,
                "error": "rate_limited",
                "retryable": True,
                "definitely_no_external_effect": True,
            },
            None,
        ]
    )
    route = _route("on_call", connection_id=mail, to=["oncall@example.com"])

    [first] = world.dispatch([route], event_id="evt-retry")
    [second] = world.dispatch([route], event_id="evt-retry")

    assert first["status"] == "failed"
    assert second["status"] == "delivered"
    attempt_one, attempt_two = world.mixed.requests()
    assert attempt_one["idempotency_key"]
    assert attempt_one["external_effect_id"]
    assert attempt_two["idempotency_key"] == attempt_one["idempotency_key"]
    assert attempt_two["external_effect_id"] == attempt_one["external_effect_id"]


def test_an_oauth_connection_gets_a_refreshed_token_from_the_shared_resolver(
    world: World, monkeypatch
) -> None:
    configuration_id = _oauth_configuration(world)
    mail = _connection(
        world, "oauth_mail", connection_type="example_mail", oauth_configuration_id=configuration_id
    )
    refreshed: list[str] = []

    def resolve(_bl, *, connection, get_project_by_id=None):
        # The operations path: an expired access token is refreshed here.
        refreshed.append(connection["connection_id"])
        return {"access_token": "fresh-access-token"}

    monkeypatch.setattr(operation_targets, "_resolve_extension_owned_oauth_runtime", resolve)

    [row] = world.dispatch(
        [_route("on_call", connection_id=mail, to=["a@example.com"])], event_id="evt-oauth"
    )

    assert row["status"] == "delivered"
    assert refreshed == [mail]
    [request] = world.mixed.requests()
    assert request["action_payload"]["target"]["connection"]["secrets"] == {
        "access_token": "fresh-access-token"
    }
    assert "fresh-access-token" not in json.dumps(world.store.delivery_logs, default=str)
    assert "fresh-access-token" not in json.dumps(world.store.invocation_logs, default=str)


def test_a_revoked_grant_fails_with_authorization_required(world: World, monkeypatch) -> None:
    configuration_id = _oauth_configuration(world)
    mail = _connection(
        world, "oauth_mail", connection_type="example_mail", oauth_configuration_id=configuration_id
    )

    def revoked(*_a, **_k):
        raise OAuthRuntimeTokenError("oauth_authorization_required")

    monkeypatch.setattr(operation_targets, "_resolve_extension_owned_oauth_runtime", revoked)

    [row] = world.dispatch(
        [_route("on_call", connection_id=mail, to=["a@example.com"])], event_id="evt-revoked"
    )

    assert row["status"] == "failed"
    assert row["error_text"] == "authorization_required"
    assert world.mixed.requests() == []


def test_a_connection_with_its_own_secrets_still_delivers(world: World, monkeypatch) -> None:
    from core.application.management import project_connection_secrets

    mail = _connection(world, "mail", connection_type="example_mail")
    world.sql(
        "UPDATE project_connections SET secret_refs = %s::jsonb WHERE connection_id = %s",
        (json.dumps({"refs": {"api_key": "ref-api-key"}}), mail),  # pragma: allowlist secret
    )
    monkeypatch.setattr(
        project_connection_secrets,
        "resolve_connection_secrets",
        lambda _bl, **_k: {"api_key": "own-secret"},  # pragma: allowlist secret
    )

    [row] = world.dispatch(
        [_route("on_call", connection_id=mail, to=["a@example.com"])], event_id="evt-own"
    )

    assert row["status"] == "delivered"
    [request] = world.mixed.requests()
    assert request["action_payload"]["target"]["connection"]["secrets"] == {
        "api_key": "own-secret",  # pragma: allowlist secret
    }


# --------------------------------------------------------------------------- C2


def _choose(world: World, connection_id: str) -> None:
    status = system_email_sender.select_sender(world.store, PLATFORM_ADMIN, connection_id)
    assert status.available, status.reason


def _reset(world: World, token: str = "t") -> dict[str, Any]:
    return system_email.send_password_reset_email(
        recipient="anna@example.com",
        reset_url=f"https://fs.example/reset-password?token={token}",
        expires_in_minutes=30,
        username="Anna",
        bl=world.store,
    )


def _available(world: World) -> bool:
    return system_email_sender.sender_status(world.store).available


def test_the_chosen_connection_sends_and_the_choice_is_audited(world: World) -> None:
    _connection(world, "older", connection_type="example_mail")
    chosen = _connection(world, "noreply", connection_type="example_mail")

    _choose(world, chosen)
    result = _reset(world)

    assert result["ok"] is True
    [request] = world.mixed.requests()
    assert request["action_payload"]["target"]["connection"]["connection_id"] == chosen
    assert request["event"]["payload_sensitive"] is True
    audit = world.sql("SELECT action_type FROM platform_action_log WHERE task_id = 'system_email'")
    assert ("system_email_sender_selected",) in audit


def test_only_a_platform_administrator_may_choose(world: World) -> None:
    chosen = _connection(world, "noreply", connection_type="example_mail")
    for scope in (_scope("operator"), _scope("admin", token_scopes=frozenset({"projects:write"}))):
        with pytest.raises(ServiceForbiddenError):
            system_email_sender.select_sender(world.store, scope, chosen)
    _choose(world, chosen)
    token_admin = _scope("admin", token_scopes=frozenset({"admin:all"}))
    assert system_email_sender.select_sender(world.store, token_admin, chosen).available


def test_ineligible_connections_are_never_candidates(world: World) -> None:
    mail = _connection(world, "mail", connection_type="example_mail")
    _connection(world, "chat", connection_type="example_chat")
    _connection(world, "files", connection_type="example_files")
    world.installed["mixed_other"].enabled = False
    _connection(world, "disabled_install", install="mixed_other", connection_type="example_mail")

    candidates = [
        item["connection_id"] for item in system_email_sender.list_candidates(world.store)
    ]

    assert candidates == [mail]


@pytest.mark.parametrize(
    "change",
    ["disable", "delete", "retype", "other_install", "config", "secret", "reconnect_other_account"],
)
def test_every_change_to_the_sender_makes_system_email_unavailable_without_fallback(
    world: World, change
) -> None:
    _connection(world, "spare", connection_type="example_mail")
    chosen = _connection(
        world, "noreply", connection_type="example_mail", config={"address": "noreply@example.com"}
    )
    _choose(world, chosen)
    statements = {
        "disable": (
            "UPDATE project_connections SET enabled = false WHERE connection_id = %s",
            (chosen,),
        ),
        "delete": ("DELETE FROM project_connections WHERE connection_id = %s", (chosen,)),
        "retype": (
            "UPDATE project_connections SET connection_type = 'example_chat' WHERE connection_id = %s",
            (chosen,),
        ),
        "other_install": (
            "UPDATE project_connections SET extension_install_id = %s WHERE connection_id = %s",
            (world.installed["mixed_other"].install_id, chosen),
        ),
        "config": (
            'UPDATE project_connections SET connection_config = \'{"address": "other@example.com"}\'::jsonb '
            "WHERE connection_id = %s",
            (chosen,),
        ),
        "secret": (
            'UPDATE project_connections SET secret_refs = \'{"refs": {"client_secret": "ref-2"}}\'::jsonb, '  # pragma: allowlist secret
            "credential_revision = credential_revision + 1 WHERE connection_id = %s",
            (chosen,),
        ),
        "reconnect_other_account": (
            "UPDATE project_connections SET connection_config = connection_config || "
            '\'{"oauth": {"subject": "someone-else", "authorization_intent_id": "i-2"}}\'::jsonb '
            "WHERE connection_id = %s",
            (chosen,),
        ),
    }
    world.sql(*statements[change])

    assert _available(world) is False
    assert system_email.find_email_connection(world.store) is None
    assert _reset(world)["reason"] == "disabled_or_unconfigured"
    assert world.mixed.requests() == []


def test_a_project_admin_cannot_change_the_sender_through_its_connection(world: World) -> None:
    from core.application.extensions.oauth_authorization import (
        OAuthAuthorizationService,
        OAuthAuthorizationStart,
    )
    from core.application.management.command_models import ProjectConnectionUpdateInput
    from core.application.management.command_services_project_connection import (
        ProjectConnectionService,
    )

    chosen = _connection(world, "noreply", connection_type="example_mail")
    _choose(world, chosen)
    before = world.revision(chosen)
    service = ProjectConnectionService(get_logger=lambda: world.store)
    project_admin = _scope("operator", project_id=world.project_id)
    attempts: list[Callable[[], Any]] = [
        lambda: service.update_connection(
            chosen,
            ProjectConnectionUpdateInput(connection_config={"address": "other@example.com"}),
            project_id=world.project_id,
            scope=project_admin,
        ),
        lambda: service.update_connection(
            chosen,
            ProjectConnectionUpdateInput(connection_type="example_chat"),
            project_id=world.project_id,
            scope=project_admin,
        ),
        lambda: service.update_connection(
            chosen,
            ProjectConnectionUpdateInput(enabled=False),
            project_id=world.project_id,
            scope=project_admin,
        ),
        lambda: service.save_connection_secrets(
            chosen, {"client_secret": "x"}, project_id=world.project_id, scope=project_admin
        ),
        lambda: service.delete_connection(chosen, project_id=world.project_id, scope=project_admin),
    ]
    authorization = OAuthAuthorizationService.__new__(OAuthAuthorizationService)
    authorization._repository = world.store
    attempts.append(
        lambda: authorization.start_authorization(
            project_admin,
            world.mixed.install_id,
            "example_oauth",
            OAuthAuthorizationStart(
                configuration_id=str(uuid4()),
                connection_type="example_mail",
                intent_id="connect",
                display_name="noreply",
                return_page_id="mail",
                existing_connection_id=chosen,
            ),
        )
    )

    for attempt in attempts:
        with pytest.raises(ServiceForbiddenError) as raised:
            attempt()
        assert raised.value.payload["code"] == "system_sender_locked"

    assert world.revision(chosen) == before
    assert _available(world) is True
    # A display name is not who sends: a project admin may still change it.
    service.update_connection(
        chosen,
        ProjectConnectionUpdateInput(display_name="No-reply"),
        project_id=world.project_id,
        scope=project_admin,
    )
    assert _available(world) is True
    view = service.get_connection(chosen, project_id=world.project_id, scope=project_admin)
    assert view.system_email_sender is True
    assert view.system_email_sender_locked is True


def test_a_project_admin_cannot_edit_the_senders_oauth_configuration(world: World) -> None:
    from core.application.extensions.oauth_configuration import OAuthConfigurationService
    from core.application.extensions.oauth_configuration_models import OAuthConfigurationSave

    configuration_id = _oauth_configuration(world)
    chosen = _connection(
        world, "noreply", connection_type="example_mail", oauth_configuration_id=configuration_id
    )
    _choose(world, chosen)
    service = OAuthConfigurationService.__new__(OAuthConfigurationService)
    service._repository = world.store
    service._transaction_factory = contextlib.nullcontext

    with pytest.raises(ServiceForbiddenError) as raised:
        service._save_configuration(
            _scope("operator", project_id=world.project_id),
            world.mixed.install_id,
            "example_oauth",
            OAuthConfigurationSave(
                configuration_id=configuration_id, display_name="Other", values={"client_id": "x"}
            ),
            expected_revision=1,
            confirmation_token=None,
        )
    assert raised.value.payload["code"] == "system_sender_locked"


def test_a_platform_admins_change_needs_a_new_confirmation(world: World) -> None:
    chosen = _connection(
        world, "noreply", connection_type="example_mail", config={"address": "a@example.com"}
    )
    _choose(world, chosen)
    world.sql(
        'UPDATE project_connections SET connection_config = \'{"address": "b@example.com"}\'::jsonb '
        "WHERE connection_id = %s",
        (chosen,),
    )
    assert system_email_sender.sender_status(world.store).reason == "confirmation_revoked"

    system_email_sender.confirm_sender(world.store, PLATFORM_ADMIN)

    assert _available(world) is True


def test_an_oauth_configuration_edited_with_the_same_id_revokes_the_confirmation(
    world: World,
) -> None:
    configuration_id = _oauth_configuration(world)
    chosen = _connection(
        world, "noreply", connection_type="example_mail", oauth_configuration_id=configuration_id
    )
    _choose(world, chosen)

    # Even a write path that forgets to move the revision moves it.
    world.sql(
        'UPDATE extension_oauth_configurations SET non_secret_config = \'{"client_id": "client-2"}\'::jsonb '
        "WHERE configuration_id = %s",
        (configuration_id,),
    )

    assert system_email_sender.sender_status(world.store).reason == "confirmation_revoked"
    [(revision,)] = world.sql(
        "SELECT configuration_revision FROM extension_oauth_configurations WHERE configuration_id = %s",
        (configuration_id,),
    )
    assert revision == 2
    # A write that moves it itself is not moved twice.
    world.sql(
        'UPDATE extension_oauth_configurations SET non_secret_config = \'{"client_id": "client-3"}\'::jsonb, '
        "configuration_revision = configuration_revision + 1 WHERE configuration_id = %s",
        (configuration_id,),
    )
    [(revision,)] = world.sql(
        "SELECT configuration_revision FROM extension_oauth_configurations WHERE configuration_id = %s",
        (configuration_id,),
    )
    assert revision == 3


def test_disable_then_re_enable_keeps_the_confirmation_revoked(world: World) -> None:
    chosen = _connection(world, "noreply", connection_type="example_mail")
    _choose(world, chosen)
    world.sql("UPDATE project_connections SET enabled = false WHERE connection_id = %s", (chosen,))
    world.sql("UPDATE project_connections SET enabled = true WHERE connection_id = %s", (chosen,))

    assert system_email_sender.sender_status(world.store).reason == "confirmation_revoked"
    assert _reset(world)["reason"] == "disabled_or_unconfigured"


def test_a_token_refresh_leaves_system_email_available(world: World) -> None:
    configuration_id = _oauth_configuration(world)
    chosen = _connection(
        world, "noreply", connection_type="example_mail", oauth_configuration_id=configuration_id
    )
    _choose(world, chosen)

    # What update_oauth_refresh writes.
    world.sql(
        "UPDATE project_connections SET credential_revision = credential_revision + 1, "
        'secret_refs = \'{"refs": {"_oauth_access_token": "ref-new", "_oauth_refresh_token": "ref-r2"}}\'::jsonb, '
        'connection_config = connection_config || \'{"oauth": {"lifecycle_status": "connected", '
        '"expires_at": "2026-12-01T00:00:00Z"}}\'::jsonb WHERE connection_id = %s',
        (chosen,),
    )

    assert _available(world) is True


@pytest.mark.parametrize("change", ["connection", "oauth_configuration", "another_sender"])
def test_a_change_between_check_and_admission_sends_nothing(world: World, change) -> None:
    configuration_id = _oauth_configuration(world)
    chosen = _connection(
        world, "noreply", connection_type="example_mail", oauth_configuration_id=configuration_id
    )
    other = _connection(world, "other", connection_type="example_mail")
    _choose(world, chosen)

    def make_change() -> None:
        if change == "connection":
            world.sql(
                "UPDATE project_connections SET connection_config = connection_config || "
                '\'{"address": "x@example.com"}\'::jsonb WHERE connection_id = %s',
                (chosen,),
            )
        elif change == "oauth_configuration":
            world.sql(
                "UPDATE extension_oauth_configurations SET non_secret_config = "
                '\'{"client_id": "client-9"}\'::jsonb WHERE configuration_id = %s',
                (configuration_id,),
            )
        else:
            system_email_sender.select_sender(world.store, PLATFORM_ADMIN, other)

    system_email_sender.FAULT_POINTS["before_admission"] = make_change
    resolved: list[str] = []
    original = operation_targets._resolve_extension_owned_oauth_runtime

    def token(_bl, *, connection, get_project_by_id=None):
        resolved.append(connection["connection_id"])
        return {"access_token": "token-1"}

    operation_targets._resolve_extension_owned_oauth_runtime = token
    try:
        result = _reset(world)
    finally:
        operation_targets._resolve_extension_owned_oauth_runtime = original

    assert result["reason"] == "system_sender_changed"
    assert world.mixed.requests() == []
    assert world.effects() == []


def test_the_extension_receives_exactly_the_checked_snapshot(world: World, monkeypatch) -> None:
    from core.application.management import project_connection_secrets

    chosen = _connection(
        world, "noreply", connection_type="example_mail", config={"address": "noreply@example.com"}
    )
    world.sql(
        "UPDATE project_connections SET secret_refs = %s::jsonb WHERE connection_id = %s",
        (json.dumps({"refs": {"client_secret": "ref-1"}}), chosen),  # pragma: allowlist secret
    )
    _choose(world, chosen)
    secrets = iter([{"client_secret": "checked-secret"}, {"client_secret": "later-secret"}])
    monkeypatch.setattr(
        project_connection_secrets, "resolve_connection_secrets", lambda _bl, **_k: next(secrets)
    )

    def change_what_a_reload_would_see() -> None:
        # Not who sends (no revision moves), but a reload would pick it up.
        world.sql(
            "UPDATE project_connections SET display_name = 'Renamed' WHERE connection_id = %s",
            (chosen,),
        )

    system_email_sender.FAULT_POINTS["before_admission"] = change_what_a_reload_would_see

    assert _reset(world)["ok"] is True
    [request] = world.mixed.requests()
    connection = request["action_payload"]["target"]["connection"]
    assert connection["secrets"] == {"client_secret": "checked-secret"}  # pragma: allowlist secret
    assert connection["config"] == {"address": "noreply@example.com"}
    assert connection["display_name"] == "noreply connection"


def test_the_upgrade_pin_picks_what_the_old_rule_picked_once(world: World) -> None:
    # Before the upgrade one extension addressed one kind of target.
    _connection(
        world,
        "chat",
        install="bound",
        connection_type="example_chat_bound",
        config={"channel_label": "Ops"},
    )
    oldest = _connection(world, "oldest", connection_type="example_mail")
    _connection(world, "newer", connection_type="example_mail")
    world.store.catalog[BOUND_EXT] = {
        **world.installed["bound"].record(),
        "external_effects": [{"observability": {"target_kind": "channel"}}],
    }
    world.store.catalog[EXT] = {
        **world.mixed.record(),
        "external_effects": [{"observability": {"target_kind": "recipient"}}],
    }
    expected = system_email_sender.legacy_sender_choice(world.store)
    pins_before = _pins(world)

    status = system_email_sender.sender_status(world.store)

    assert expected is not None and expected["connection_id"] == oldest
    assert status.available
    assert status.setting.connection_id == oldest
    assert status.setting.source == "upgrade_pin"
    assert _pins(world) == pins_before + 1
    # Only once: asking again, or a later connection, does not move it.
    _connection(world, "latest", connection_type="example_mail")
    assert system_email_sender.sender_status(world.store).setting.connection_id == oldest
    assert _pins(world) == pins_before + 1


def _pins(world: World) -> int:
    [(count,)] = world.sql(
        "SELECT count(*) FROM platform_action_log WHERE action_type = 'system_email_sender_pinned'"
    )
    return int(count)


def test_a_fresh_install_without_a_selection_sends_no_email(world: World) -> None:
    status = system_email_sender.sender_status(world.store)
    assert status.available is False
    assert status.reason == "not_selected"
    _connection(world, "mail", connection_type="example_mail")
    # Pinned empty once; a connection added later is not adopted silently.
    assert system_email_sender.sender_status(world.store).reason == "not_selected"


def test_no_system_message_body_reaches_any_log(world: World, caplog) -> None:
    chosen = _connection(world, "noreply", connection_type="example_mail")
    _choose(world, chosen)
    world.mixed.plan(
        [
            None,
            {
                "ok": False,
                "delivered": False,
                "error": "quota exceeded",
                "definitely_no_external_effect": True,
            },
            None,
        ]
    )
    caplog.set_level(logging.DEBUG)
    reset_token = f"reset-{uuid4().hex}"
    change_token = f"change-{uuid4().hex}"

    assert _reset(world, reset_token)["ok"] is True
    system_email.send_email_change_verification_email(
        recipient="new@example.com",
        new_email="new@example.com",
        verification_url=f"https://fs.example/verify-email-change?token={change_token}",
        expires_in_minutes=15,
        bl=world.store,
    )
    assert (
        system_email.send_signup_notification_email(
            recipient="welcome@example.com", display_name="Welcome", bl=world.store
        )["ok"]
        is True
    )
    # A flagged project notification goes through the delivery rows too.
    mail = _connection(world, "mail", connection_type="example_mail")
    world.dispatch(
        [_route("on_call", connection_id=mail, to=["a@example.com"])],
        event_id="evt-sensitive",
        event_type="system.notice",
        payload={
            "message": f"Your link https://fs.example/x?token={reset_token}",
            "summary": "Link",
        },
        payload_sensitive=True,
    )

    requests = world.mixed.requests()
    assert reset_token in json.dumps(requests[0])
    assert change_token in json.dumps(requests[1])
    kept = json.dumps(
        {
            "invocations": world.store.invocation_logs,
            "deliveries": world.store.delivery_logs,
            "ledger": world.effects(),
            "logs": [record.getMessage() for record in caplog.records],
        },
        default=str,
    )
    assert reset_token not in kept
    assert change_token not in kept
    assert "<redacted length=" in kept


# --------------------------------------------------------------------------- C5


def _rotate(world: World, configuration_id: str, client_secret: str) -> None:
    """Store a new client secret; the configuration's revision moves with it."""
    from core.application.auth.secrets import encrypt_secret
    from core.application.extensions.oauth_configuration_secrets import (
        configuration_secret_ref_key,
    )
    from core.infrastructure.db import model_provider_store

    with psycopg.connect(world.dsn, autocommit=True) as conn:
        model_provider_store.upsert_secret_ref(
            conn,
            account_id=DEFAULT_ACCOUNT_ID,
            ref_key=configuration_secret_ref_key(configuration_id, "client_secret"),
            encrypted_value=encrypt_secret(client_secret),
        )
    world.sql(
        "UPDATE extension_oauth_configurations SET configuration_revision = "
        "configuration_revision + 1 WHERE configuration_id = %s",
        (configuration_id,),
    )


def _app_configuration(world: World, *, client_secret: str = "app-secret-1") -> str:
    """An OAuth configuration whose client values application connections borrow."""
    from core.application.extensions.oauth_configuration_secrets import (
        configuration_secret_ref_key,
    )

    configuration_id = _oauth_configuration(world)
    world.sql(
        "UPDATE extension_oauth_configurations SET non_secret_config = %s::jsonb, "
        "secret_refs = %s::jsonb WHERE configuration_id = %s",
        (
            json.dumps({"client_id": "app-client", "tenant_id": "directory-1"}),
            json.dumps(
                {
                    "refs": {
                        "client_secret": configuration_secret_ref_key(
                            configuration_id, "client_secret"
                        )
                    }
                }
            ),
            configuration_id,
        ),
    )
    _rotate(world, configuration_id, client_secret)
    return configuration_id


def _app_connection(world: World, name: str, configuration_id: str) -> str:
    connection_id = _connection(
        world, name, connection_type="example_mail_app", config={"sender": f"{name}@example.com"}
    )
    world.sql(
        "UPDATE project_connections SET credentials_configuration_id = %s WHERE connection_id = %s",
        (configuration_id, connection_id),
    )
    return connection_id


def _sent_connection(request: dict[str, Any]) -> dict[str, Any]:
    return request["action_payload"]["target"]["connection"]


def test_two_connections_borrow_one_configuration_and_follow_a_rotation(world: World) -> None:
    configuration_id = _app_configuration(world, client_secret="app-secret-1")
    first = _app_connection(world, "billing", configuration_id)
    second = _app_connection(world, "support", configuration_id)
    routes = [
        _route("billing", connection_id=first, to=["a@example.com"]),
        _route("support", connection_id=second, to=["b@example.com"]),
    ]

    world.dispatch(routes, event_id="evt-borrow-1")
    _rotate(world, configuration_id, "app-secret-2")
    world.dispatch(routes, event_id="evt-borrow-2")

    requests = world.mixed.requests()
    before, after = requests[:2], requests[2:]
    for request in before:
        connection = _sent_connection(request)
        assert connection["config"]["client_id"] == "app-client"
        assert connection["config"]["tenant_id"] == "directory-1"
        assert connection["config"]["sender"].endswith("@example.com")
        assert connection["secrets"] == {
            "client_secret": "app-secret-1",  # pragma: allowlist secret
        }
    # No connection was edited: the rotated secret reaches both on the next call.
    assert [_sent_connection(request)["secrets"] for request in after] == [
        {"client_secret": "app-secret-2"},  # pragma: allowlist secret
        {"client_secret": "app-secret-2"},  # pragma: allowlist secret
    ]
    kept = json.dumps(
        {"invocations": world.store.invocation_logs, "deliveries": world.store.delivery_logs},
        default=str,
    )
    assert "app-secret-1" not in kept
    assert "app-secret-2" not in kept


def test_every_call_path_receives_the_borrowed_values(world: World) -> None:
    from core.application.extensions.background_poll_connection import (
        resolve_background_poll_connection,
    )
    from core.application.extensions.operation_targets import _direct_action_connection_target
    from core.application.orchestration.connector_connection import (
        _build_runtime_connection_target,
    )

    configuration_id = _app_configuration(world)
    connection_id = _app_connection(world, "app", configuration_id)

    # Operations, resource queries and setup or test actions all build
    # target.connection through this one function.
    operation = _direct_action_connection_target(
        world.store,
        connection_id=connection_id,
        account_id=DEFAULT_ACCOUNT_ID,
        project_id=world.project_id,
        extension_id=EXT,
        extension_install_id=world.mixed.install_id,
    )
    poll = resolve_background_poll_connection(
        world.store,
        connection_row=world.store.get_project_connection(connection_id=connection_id),
        extension=world.mixed.record(),
        extension_id=EXT,
        extension_install_id=world.mixed.install_id,
        account_id=DEFAULT_ACCOUNT_ID,
        project_id=world.project_id,
    )
    connector, logged = _build_runtime_connection_target(
        world.store,
        connection_binding={"connection_id": connection_id},
        runtime_context={"project_id": world.project_id, "account_id": DEFAULT_ACCOUNT_ID},
        extension_id=EXT,
    )
    world.dispatch(
        [_route("app", connection_id=connection_id, to=["a@example.com"])], event_id="evt-paths"
    )
    [delivery] = world.mixed.requests()

    assert poll.ok, poll.skip_reason
    for target in (operation, poll.connection, connector, _sent_connection(delivery)):
        assert target["config"]["client_id"] == "app-client"
        assert target["config"]["tenant_id"] == "directory-1"
        assert target["secrets"]["client_secret"] == "app-secret-1"  # pragma: allowlist secret
    assert logged["secrets"] == {"client_secret": "<redacted>"}


def test_deleting_the_configuration_lists_and_then_strands_its_linked_connections(
    world: World,
) -> None:
    from core.application.extensions.oauth_configuration import OAuthConfigurationService
    from core.application.extensions.oauth_repository import ExtensionOAuthRepository
    from core.application.management.command_services_project_connection import (
        ProjectConnectionService,
    )

    configuration_id = _app_configuration(world)
    first = _app_connection(world, "billing", configuration_id)
    second = _app_connection(world, "support", configuration_id)
    [(revision,)] = world.sql(
        "SELECT configuration_revision FROM extension_oauth_configurations "
        "WHERE configuration_id = %s",
        (configuration_id,),
    )
    service = OAuthConfigurationService.__new__(OAuthConfigurationService)
    service._repository = ExtensionOAuthRepository(world.store.conn)
    service._confirmation_key = b"fixture-confirmation-key"
    service._resolve_extension_id = lambda *_a, **_k: EXT
    admin = ScopeContext(
        account_id=DEFAULT_ACCOUNT_ID,
        tenant_id=world.tenant_id,
        project_id=world.project_id,
        principal_type="admin",
        actor_id="admin-user",
    )

    impact = service._preview_configuration_delete(
        admin, world.mixed.install_id, "example_oauth", configuration_id, expected_revision=revision
    )

    assert {view.connection_id: view.link for view in impact.connections} == {
        first: "credentials",
        second: "credentials",
    }
    assert impact.requires_confirmation is True

    world.sql(
        "DELETE FROM extension_oauth_configurations WHERE configuration_id = %s",
        (configuration_id,),
    )
    view = ProjectConnectionService(get_logger=lambda: world.store).get_connection(
        first, project_id=world.project_id, scope=_scope("operator", project_id=world.project_id)
    )
    assert view.credentials_status == "credentials_unavailable"
    assert view.runtime_status == "not_ready"
    [row] = world.dispatch(
        [_route("billing", connection_id=first, to=["a@example.com"])], event_id="evt-stranded"
    )
    assert row["status"] == "failed"
    assert row["error_text"].startswith("credentials_unavailable")
    assert world.mixed.requests() == []


def test_no_borrowed_secret_reaches_the_connection_api_or_browser_manifest(world: World) -> None:
    from core.application.extensions.browser_manifest_projection import project_browser_manifest
    from core.application.management.command_services_project_connection import (
        ProjectConnectionService,
    )

    configuration_id = _app_configuration(world, client_secret="never-shown-secret")
    connection_id = _app_connection(world, "app", configuration_id)
    view = ProjectConnectionService(get_logger=lambda: world.store).get_connection(
        connection_id,
        project_id=world.project_id,
        scope=_scope("operator", project_id=world.project_id),
    )
    manifest = project_browser_manifest(world.mixed.record()["compiled_manifest"])

    assert view.credentials_configuration_id == configuration_id
    assert view.credentials_status == ""
    assert "never-shown-secret" not in view.model_dump_json()
    assert "never-shown-secret" not in json.dumps(manifest, default=str)


def test_a_linked_configuration_change_revokes_the_system_sender_confirmation(
    world: World,
) -> None:
    configuration_id = _app_configuration(world)
    sender = _app_connection(world, "noreply", configuration_id)
    _choose(world, sender)

    _rotate(world, configuration_id, "rotated-secret")

    assert system_email_sender.sender_status(world.store).reason == "confirmation_revoked"
    system_email_sender.confirm_sender(world.store, PLATFORM_ADMIN)
    assert _available(world) is True
    # Moving the connection to another configuration is a sender change too.
    other = _app_configuration(world)
    world.sql(
        "UPDATE project_connections SET credentials_configuration_id = %s WHERE connection_id = %s",
        (other, sender),
    )
    assert system_email_sender.sender_status(world.store).reason == "confirmation_revoked"
