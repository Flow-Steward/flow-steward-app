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


@pytest.fixture(autouse=True)
def clear_openai_chat_catalog():
    discovery._openai_chat_catalog.cache_clear()
    yield
    discovery._openai_chat_catalog.cache_clear()


def _mock_upstream(monkeypatch, handler):
    monkeypatch.setattr(
        discovery, "build_pinned_httpx_transport", lambda **_: httpx.MockTransport(handler)
    )


def _openai_model_page(model_id, inputs, outputs, endpoint, ids=""):
    return (
        f"Model ID: `{model_id}`\n\n## Model details\n\n"
        f"- Input modalities: {inputs}\n- Output modalities: {outputs}\n\n"
        "## Endpoints\n\n| Endpoint | Route | Support |\n| --- | --- | --- |\n"
        f"| Example | `{endpoint}` | Supported |\n" + ids
    )


def test_openai_suggests_only_current_documented_chat_models_available_to_the_key(
    monkeypatch,
) -> None:
    shutdown_chat = "older-chat-2020-01-01"
    available_snapshot = "new-chat-2026-10-01"
    index = """# All models

## Flagship models
- [New](/api/docs/models/new-chat.md)
- [Older](/api/docs/models/older-chat.md)
- [Responses](/api/docs/models/responses-chat.md)
- [Outside the account](/api/docs/models/unavailable.md)

## Specialty models
- [Transcription](/api/docs/models/voice-notes.md)
- [Speech](/api/docs/models/narrator.md)
- [Image](/api/docs/models/illustrator.md)
- [Untrusted](https://other.example/api/docs/models/undocumented.md)

## Deprecated models
- [Deprecated](/api/docs/models/deprecated-chat.md)
"""
    pages = {
        "new-chat": _openai_model_page(
            "new-chat",
            "text, image",
            "text",
            "v1/chat/completions",
            "\n## Model IDs\n\n- `new-chat`\n- `new-chat-2026-10-01`\n",
        ),
        "older-chat": _openai_model_page(
            "older-chat",
            "text",
            "text",
            "v1/chat/completions",
            "\n## Snapshots\n\n- `older-chat`\n- `older-chat-2020-01-01`\n",
        ),
        "responses-chat": _openai_model_page("responses-chat", "text", "text", "v1/responses"),
        "voice-notes": _openai_model_page(
            "voice-notes", "audio", "text", "v1/audio/transcriptions"
        ),
        "narrator": _openai_model_page("narrator", "text", "audio", "v1/audio/speech"),
        "illustrator": _openai_model_page(
            "illustrator", "text, image", "image", "v1/images/generations"
        ),
        "unavailable": _openai_model_page("unavailable", "text", "text", "v1/responses"),
        "deprecated-chat": _openai_model_page("deprecated-chat", "text", "text", "v1/responses"),
    }

    def upstream(request):
        if request.url.host == "api.openai.com":
            assert str(request.url) == "https://api.openai.com/v1/models"
            assert request.headers["Authorization"] == "Bearer test-key"
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "older-chat", "created": 1},
                        {"id": "new-chat", "created": 4},
                        {"id": "new-chat", "created": 3},
                        {"id": available_snapshot, "created": 3},
                        {"id": "responses-chat", "created": 2},
                        {"id": shutdown_chat, "created": 6, "shutdown_date": "2000-01-01"},
                        {"id": "voice-notes", "created": 7},
                        {"id": "narrator", "created": 8},
                        {"id": "illustrator", "created": 9},
                        {"id": "deprecated-chat", "created": 10},
                        {"id": "undocumented", "created": 11},
                    ],
                },
            )
        assert request.url.host == "developers.openai.com"
        assert "Authorization" not in request.headers
        if request.url.path == "/api/docs/models/all.md":
            return httpx.Response(200, text=index)
        model_id = request.url.path.rsplit("/", 1)[-1].removesuffix(".md")
        return httpx.Response(200, text=pages[model_id])

    _mock_upstream(monkeypatch, upstream)

    assert discovery.discover_official_models("openai", "test-key") == [
        {"model_id": "new-chat", "label": "new-chat"},
        {"model_id": available_snapshot, "label": available_snapshot},
        {"model_id": "responses-chat", "label": "responses-chat"},
        {"model_id": "older-chat", "label": "older-chat"},
    ]


def test_openai_does_not_return_unfiltered_models_when_documentation_is_unavailable(
    monkeypatch,
) -> None:
    def upstream(request):
        if request.url.host == "api.openai.com":
            return httpx.Response(200, json={"data": [{"id": "unchecked"}]})
        return httpx.Response(503, text="private upstream error")

    _mock_upstream(monkeypatch, upstream)

    with pytest.raises(discovery.ModelDiscoveryError, match=r"^upstream_error$"):
        discovery.discover_official_models("openai", "test-key")


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
                json={
                    "data": [
                        {"id": "new", "type": "model", "lifecycle": "active"},
                        {"id": "old", "type": "model", "lifecycle": "deprecated"},
                    ],
                    "has_more": True,
                    "last_id": "old",
                },
            )
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "more", "type": "model", "lifecycle": "active", "display_name": "More"},
                    {"id": "special", "type": "embedding", "lifecycle": "active"},
                ],
                "has_more": False,
            },
        )

    _mock_upstream(monkeypatch, upstream)

    assert discovery.discover_official_models("anthropic", "test-key") == [
        {"model_id": "new", "label": "new"},
        {"model_id": "more", "label": "More"},
    ]
    assert cursors == [None, "old"]


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
