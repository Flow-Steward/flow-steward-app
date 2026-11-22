"""Background polls resolve their connection the way operations do."""

from __future__ import annotations

from typing import Any

import pytest

from core.application.extensions import background_poll_connection as resolver
from core.application.extensions import operation_targets
from core.application.extensions.oauth_runtime import OAuthRuntimeTokenError

EXT = "fixture.items-poll"
MODE_OFF = {
    "type": "object",
    "properties": {
        "sync_mode": {"type": "string", "default": "off"},
        "poll_interval_seconds": {"type": "integer", "default": 60},
        "sync_scope": {"type": "string"},
    },
}
EXTENSION: dict[str, Any] = {
    "extension_key": EXT,
    "connection_types": [
        {"connection_type_id": "items_user", "ingress_schema": MODE_OFF},
        {
            "connection_type_id": "items_app",
            "ingress_schema": MODE_OFF,
            "secret_schema": {
                "type": "object",
                "required": ["client_secret"],
                "properties": {"client_secret": {"type": "string"}},
            },
        },
    ],
}


def _row(connection_type: str, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "connection_id": f"conn-{connection_type}",
        "connection_type": connection_type,
        "account_id": "account-1",
        "project_id": "project-1",
        "extension_id": EXT,
        "extension_install_id": "install-1",
        "enabled": True,
        "connection_config": {"scope": "a"},
        "secret_refs": {"refs": {"client_secret": "ref-1"}},
        "ingress_config": {"sync_mode": "polling"},
        "state_version": 4,
        "config_generation": 7,
    }
    if connection_type == "items_user":
        row.update(
            oauth_configuration_id="cfg-1",
            oauth_provider_id="items_oauth",
            secret_refs={},
            connection_config={"oauth": {"lifecycle_status": "connected"}},
        )
    row.update(overrides)
    return row


class _Store:
    """The connection rows a worker's store returns; no direct database."""

    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row

    def cursor(self):  # pragma: no cover - resolve_connection_secrets is patched
        raise AssertionError("no direct SQL expected")

    def get_project_connection(self, **lookup: Any) -> dict[str, Any] | None:
        assert lookup["connection_id"] == self.row["connection_id"]
        return dict(self.row)


def _resolve(row: dict[str, Any]) -> resolver.PollConnectionResolution:
    return resolver.resolve_background_poll_connection(
        _Store(row),
        connection_row=row,
        extension=EXTENSION,
        extension_id=EXT,
        extension_install_id="install-1",
        account_id="account-1",
        project_id="project-1",
    )


@pytest.fixture
def connection_secrets(monkeypatch):
    from core.application.management import project_connection_secrets

    values: dict[str, str] = {"client_secret": "app-secret"}
    monkeypatch.setattr(
        project_connection_secrets,
        "resolve_connection_secrets",
        lambda _bl, **_kwargs: dict(values),
    )
    return values


def test_ingress_defaults_come_from_the_connection_type_schema() -> None:
    assert resolver.ingress_defaults(EXTENSION, "items_app") == {
        "sync_mode": "off",
        "poll_interval_seconds": 60,
    }
    assert resolver.ingress_defaults(EXTENSION, "unknown_type") == {}
    assert resolver.with_ingress_defaults(EXTENSION, "items_app", {"sync_mode": "x"}) == {
        "sync_mode": "x",
        "poll_interval_seconds": 60,
    }


def test_app_connection_gets_its_own_connection_secrets(connection_secrets) -> None:
    resolution = _resolve(_row("items_app"))
    assert resolution.ok
    assert resolution.connection["secrets"] == {"client_secret": "app-secret"}
    assert resolution.connection["config"] == {"scope": "a"}
    assert resolution.connection["state_version"] == 4
    assert resolution.connection["config_generation"] == 7
    assert resolution.connection["ingress_config"]["sync_mode"] == "polling"


def test_app_connection_without_its_secret_is_skipped(connection_secrets) -> None:
    connection_secrets.clear()
    resolution = _resolve(_row("items_app", secret_refs={}))
    assert resolution.skip_reason == resolver.SKIP_SECRETS_MISSING
    assert resolution.lasting is True


def test_delegated_connection_gets_a_refreshed_token(monkeypatch) -> None:
    refreshed: list[str] = []

    def _token(_bl, *, connection, **_kwargs):
        # The operations path: an expired access token is refreshed here.
        refreshed.append(connection["connection_id"])
        return {"access_token": "fresh-token"}

    monkeypatch.setattr(operation_targets, "_resolve_extension_owned_oauth_runtime", _token)
    resolution = _resolve(_row("items_user"))
    assert resolution.ok
    assert refreshed == ["conn-items_user"]
    assert resolution.connection["secrets"] == {"access_token": "fresh-token"}


def test_revoked_grant_stops_polling(monkeypatch) -> None:
    def _revoked(*_a, **_k):
        raise OAuthRuntimeTokenError("oauth_authorization_required")

    monkeypatch.setattr(operation_targets, "_resolve_extension_owned_oauth_runtime", _revoked)
    resolution = _resolve(_row("items_user"))
    assert resolution.skip_reason == resolver.SKIP_AUTHORIZATION_REQUIRED
    assert resolution.lasting is True

    # Once the OAuth runtime recorded the state, the connection is not even resolved.
    monkeypatch.setattr(
        operation_targets,
        "_resolve_extension_owned_oauth_runtime",
        lambda *_a, **_k: pytest.fail("a revoked connection must not be resolved"),
    )
    marked = _row(
        "items_user", connection_config={"oauth": {"lifecycle_status": "authorization_required"}}
    )
    assert _resolve(marked).skip_reason == resolver.SKIP_AUTHORIZATION_REQUIRED


def test_temporary_refresh_failure_skips_only_this_poll(monkeypatch) -> None:
    def _unavailable(*_a, **_k):
        raise OAuthRuntimeTokenError("oauth_provider_unavailable")

    monkeypatch.setattr(operation_targets, "_resolve_extension_owned_oauth_runtime", _unavailable)
    resolution = _resolve(_row("items_user"))
    assert resolution.skip_reason == resolver.SKIP_REFRESH_UNAVAILABLE
    assert resolution.lasting is False


def test_worker_temporary_skip_reason_matches_the_resolver() -> None:
    from core.infrastructure.runtime.worker import worker_inbound_polling

    assert worker_inbound_polling.SKIP_REFRESH_UNAVAILABLE == resolver.SKIP_REFRESH_UNAVAILABLE


def test_oauth_connection_creation_writes_the_declared_ingress_defaults() -> None:
    from core.application.extensions.oauth_grant_commit import OAuthGrantCommitter

    committer = OAuthGrantCommitter.__new__(OAuthGrantCommitter)
    committer._install_loader = lambda install_id, account_id: (
        {"compiled_manifest": EXTENSION} if install_id == "install-1" else None
    )
    ingress = committer._new_connection_ingress(
        {"extension_install_id": "install-1", "account_id": "a", "connection_type": "items_user"}
    )
    assert ingress["sync_mode"] == "off"
