"""A failed replacement must not forget the connection's original author."""

from __future__ import annotations

import copy
from contextlib import nullcontext
from typing import Any

import pytest

from core.application.extensions.oauth_callback import OAuthCallbackService
from tests.unit import test_extension_oauth_callback as callback
from tests.unit import test_extension_oauth_refresh_token_import as imported


def _bound_callback_repository() -> callback._Repository:
    repository = callback._Repository()
    repository.configuration["status"] = "verified"
    repository.state["existing_connection_id"] = "connection-existing"
    repository.state["intent_payload"] = {"connection_credential_revision": 7}
    repository.connection["oauth_authorization_revision"] = 2
    repository.connection["connection_config"]["oauth"]["subject"] = "member-A"
    return repository


def _identity(subject: str) -> dict[str, Any]:
    return {"status": "succeeded", "response": {"result": {"subject": subject, "health": "ok"}}}


def _failure(code: str) -> dict[str, Any]:
    return {"status": "failed", "response": {"ok": False, "error_code": code}}


def _retry(
    service: OAuthCallbackService, repository: callback._Repository
) -> callback.OAuthCallbackResult:
    return service.retry_post_connect(
        tenant_id="tenant-a",
        account_id="account-a",
        project_id="project-a",
        extension_install_id="install-a",
        extension_id="fixture.extension-oauth",
        provider_id="example",
        configuration_id=repository.state["configuration_id"],
        connection_id="connection-existing",
        connection_type="example_mail",
    )


@pytest.mark.parametrize(
    "failure", ["provider_unavailable", "malformed_response", "internal_failure"]
)
@pytest.mark.parametrize("retry_subject", ["member-A", "member-B"])
def test_failed_reconnect_retry_keeps_original_author(failure: str, retry_subject: str) -> None:
    repository = _bound_callback_repository()
    responses = iter([_failure(failure), _identity(retry_subject)])
    service = callback._service(repository, post_connect=lambda **_: next(responses))
    assert callback._handle(service).outcome == "needs_attention"

    result = _retry(service, repository)

    oauth = repository.connection["connection_config"]["oauth"]
    assert oauth["subject"] == "member-A"
    update = repository.post_connect_updates[-1]
    if retry_subject == "member-B":
        assert result.outcome == "needs_attention"
        assert oauth["last_error_code"] == "subject_changed"
        assert update["runtime_status"] == "unavailable"
        assert update["mark_last_seen_ok"] is False
    else:
        assert result.outcome == "connected"
        assert update["runtime_status"] == "ready"
        assert "replaced_subject" not in oauth
    assert repository.events.count("exchange") == 1


@pytest.mark.parametrize("failure", ["provider_unavailable", "authorization_required"])
def test_failed_reconnect_cannot_switch_author_on_next_authorization(failure: str) -> None:
    repository = _bound_callback_repository()
    responses = iter([_failure(failure), _identity("member-B")])
    service = callback._service(repository, post_connect=lambda **_: next(responses))
    assert callback._handle(service).outcome in {"needs_attention", "authorization_required"}
    # The shared fixture writes short sentinel refs. A second real consent
    # receives the token store's canonical, connection-owned refs instead.
    repository.connection["secret_refs"] = {
        "refs": {
            "_oauth_access_token": "project_connection:connection-existing:_oauth_access_token",
            "_oauth_refresh_token": "project_connection:connection-existing:_oauth_refresh_token",
        }
    }
    # Model a fresh consent state bound to the current credential revision.
    repository.consumed = False
    repository.state["intent_payload"] = {
        "connection_credential_revision": repository.connection["credential_revision"]
    }

    result = callback._handle(service)

    assert result.outcome == "needs_attention"
    oauth = repository.connection["connection_config"]["oauth"]
    assert oauth["subject"] == "member-A"
    assert oauth["last_error_code"] == "subject_changed"
    assert repository.post_connect_updates[-1]["runtime_status"] == "unavailable"


def test_pending_reconnect_retry_cannot_switch_author(monkeypatch: pytest.MonkeyPatch) -> None:
    repository = _bound_callback_repository()
    service = callback._service(repository, post_connect=lambda **_: _identity("member-B"))
    original_update = repository.update_oauth_post_connect
    monkeypatch.setattr(repository, "update_oauth_post_connect", lambda **_: {})
    assert callback._handle(service).outcome == "restart_required"
    monkeypatch.setattr(repository, "update_oauth_post_connect", original_update)

    result = _retry(service, repository)

    assert result.outcome == "needs_attention"
    oauth = repository.connection["connection_config"]["oauth"]
    assert oauth["subject"] == "member-A"
    assert oauth["last_error_code"] == "subject_changed"


def test_same_author_reconnect_still_connects() -> None:
    repository = _bound_callback_repository()
    result = callback._handle(
        callback._service(repository, post_connect=lambda **_: _identity("member-A"))
    )
    assert result.outcome == "connected"
    oauth = repository.connection["connection_config"]["oauth"]
    assert oauth["subject"] == "member-A"
    assert "replaced_subject" not in oauth


def test_failed_import_retry_cannot_switch_author() -> None:
    descriptor = imported._descriptor()
    repository = imported._bound_repository(descriptor)
    service, store, _ = imported._service(
        repository, descriptor, post_connect=lambda **_: _failure("provider_unavailable")
    )
    assert (
        service.import_refresh_token(
            imported._scope(), "install-a", "console", imported._replace_command()
        ).outcome
        == "needs_attention"
    )
    recovery = OAuthCallbackService(
        repository=repository,  # type: ignore[arg-type]
        token_store=store,
        install_loader=lambda *_: {
            "extension_id": "fixture.oauth-import",
            "release_id": "release-a",
            "enabled": True,
            "compiled_manifest": copy.deepcopy(descriptor),
        },
        configuration_secret_value=lambda **_: pytest.fail("retry must not exchange credentials"),
        decrypt_verifier=lambda _: pytest.fail("retry must not use callback state"),
        exchange_code=lambda **_: pytest.fail("retry must not exchange an authorization code"),
        transaction_factory=nullcontext,
        public_base_url="https://app.example.test",
        invoke_post_connect=lambda **_: _identity("account-other"),
        resolve_runtime_access_token=lambda **_: {"access_token": "committed-access-token"},
    )

    result = recovery.retry_post_connect(
        tenant_id="tenant-a",
        account_id="account-a",
        project_id="project-a",
        extension_install_id="install-a",
        extension_id="fixture.oauth-import",
        provider_id="console",
        configuration_id=imported.CONFIGURATION_ID,
        connection_id="connection-existing",
        connection_type="console_account",
    )

    assert result.outcome == "needs_attention"
    oauth = repository.connections["connection-existing"]["connection_config"]["oauth"]
    assert oauth["subject"] == "account-old"
    assert oauth["last_error_code"] == "subject_changed"
    assert repository.post_connect_updates[-1]["runtime_status"] == "unavailable"
