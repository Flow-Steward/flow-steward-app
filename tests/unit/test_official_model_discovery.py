from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from core.application.auth import secrets
from core.application.management.command_services_model_provider import ModelProviderCommandService
from core.application.management.errors import ServiceNotFoundError, ServiceValidationError
from core.application.management.scope import ScopeContext
from core.application.settings import official_model_discovery as discovery
from core.infrastructure.db import db_pool, model_provider_store


def _mock_upstream(monkeypatch, handler):
    monkeypatch.setattr(
        discovery, "build_pinned_httpx_transport", lambda **_: httpx.MockTransport(handler)
    )


def test_openai_models_use_current_key_and_exclude_shutdown_models(monkeypatch) -> None:
    def upstream(request):
        assert str(request.url) == "https://api.openai.com/v1/models"
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "old", "created": 1},
                    {"id": "new", "created": 3},
                    {"id": "new", "created": 2},
                    {"id": "retired", "created": 2, "shutdown_date": "2000-01-01"},
                ],
            },
        )

    _mock_upstream(monkeypatch, upstream)

    assert discovery.discover_official_models("openai", "test-key") == [
        {"model_id": "new", "label": "new"},
        {"model_id": "old", "label": "old"},
    ]


def test_anthropic_follows_page_cursors_for_active_models(monkeypatch) -> None:
    cursors = []

    def upstream(request):
        assert request.url.host == "api.anthropic.com"
        assert request.url.path == "/v1/models"
        assert request.headers["Authorization"] == "Bearer test-key"
        assert request.headers["anthropic-version"] == "2026-10-21"
        assert request.url.params["lifecycle[]"] == "active"
        cursor = request.url.params.get("after_id")
        cursors.append(cursor)
        if cursor is None:
            return httpx.Response(
                200,
                json={"data": [{"id": "new"}], "has_more": True, "last_id": "new"},
            )
        return httpx.Response(
            200,
            json={"data": [{"id": "more", "display_name": "More"}], "has_more": False},
        )

    _mock_upstream(monkeypatch, upstream)

    assert discovery.discover_official_models("anthropic", "test-key") == [
        {"model_id": "new", "label": "new"},
        {"model_id": "more", "label": "More"},
    ]
    assert cursors == [None, "new"]


@pytest.mark.parametrize(
    ("status", "error"),
    [(302, "upstream_error"), (401, "authentication_error"), (429, "rate_limited")],
)
def test_upstream_errors_do_not_follow_redirects_or_expose_details(
    monkeypatch, status: int, error: str
) -> None:
    def upstream(request):
        assert request.url.host == "api.openai.com"
        return httpx.Response(
            status,
            headers={"Location": "https://other.example/models"},
            text="private upstream response",
        )

    _mock_upstream(monkeypatch, upstream)

    with pytest.raises(discovery.ModelDiscoveryError, match=f"^{error}$"):
        discovery.discover_official_models("openai", "test-key")


@pytest.mark.parametrize("saved_type", ["openai", "anthropic"])
def test_discovery_only_uses_the_saved_key_for_the_same_provider(monkeypatch, saved_type: str):
    conn = object()
    get_provider = MagicMock(return_value={"provider_type": saved_type, "api_key_ref": "stored"})
    get_ciphertext = MagicMock(return_value="encrypted")
    decrypt = MagicMock(return_value="saved-key")
    list_models = MagicMock(return_value=[{"model_id": "new", "label": "new"}])
    monkeypatch.setattr(model_provider_store, "get_model_provider", get_provider)
    monkeypatch.setattr(model_provider_store, "get_secret_ref_ciphertext", get_ciphertext)
    monkeypatch.setattr(secrets, "decrypt_secret", decrypt)
    monkeypatch.setattr(discovery, "discover_official_models", list_models)
    service = ModelProviderCommandService(lambda: SimpleNamespace(conn=conn))
    scope = ScopeContext(account_id="account-a")

    if saved_type == "openai":
        assert service.discover_model_provider_models(
            "openai", api_key="", provider_id="provider-1", scope=scope
        ) == [{"model_id": "new", "label": "new"}]
        get_ciphertext.assert_called_once_with(conn, account_id="account-a", ref_key="stored")
        decrypt.assert_called_once_with("encrypted")
        list_models.assert_called_once_with("openai", "saved-key")
    else:
        with pytest.raises(ServiceNotFoundError):
            service.discover_model_provider_models(
                "openai", api_key="", provider_id="provider-1", scope=scope
            )
        get_ciphertext.assert_not_called()
        list_models.assert_not_called()

    get_provider.assert_called_once_with(conn, account_id="account-a", provider_id="provider-1")


def test_changing_provider_type_requires_a_new_key(monkeypatch) -> None:
    conn = object()
    update = MagicMock()
    monkeypatch.setattr(db_pool, "transaction_connection", nullcontext)
    monkeypatch.setattr(
        model_provider_store,
        "lock_model_provider_for_update",
        lambda *_args, **_kwargs: {"provider_type": "openai", "api_key_ref": "stored"},
    )
    monkeypatch.setattr(model_provider_store, "update_model_provider", update)
    service = ModelProviderCommandService(lambda: SimpleNamespace(conn=conn))

    with pytest.raises(ServiceValidationError, match="Enter a new API key"):
        service.update_model_provider(
            "provider-1",
            {"name": "Changed", "provider_type": "anthropic"},
            scope=ScopeContext(account_id="account-a"),
        )
    update.assert_not_called()
