"""Tests for core/model_provider.py — named provider resolution and ChatModel construction."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

import core.application.orchestration.model_provider as mp
from core.application.settings import model_catalog

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_lazy_handles(monkeypatch):
    """Reset lazy class handles before each test."""
    monkeypatch.setattr(mp, "ChatAnthropic", None)
    monkeypatch.setattr(mp, "ChatOpenAI", None)
    monkeypatch.setattr(mp, "ChatGoogleGenerativeAI", None)
    monkeypatch.setattr(mp, "AzureChatOpenAI", None)
    original_openai = model_catalog.PROVIDER_CATALOG["openai"]
    # These legacy IDs exercise isolated client-construction behavior only. A
    # test-local catalog entry keeps that coverage independent of the production
    # release allowlist, which intentionally contains no unverified routes.
    monkeypatch.setitem(
        model_catalog.PROVIDER_CATALOG,
        "openai",
        replace(
            original_openai,
            models=(
                *original_openai.models,
                model_catalog.ModelEntry(
                    "gpt-5.2", "test only", ("chat", "tool_use", "structured_output")
                ),
            ),
        ),
    )
    monkeypatch.setitem(
        model_catalog.BUILTIN_RELEASE_VERIFIED_MODELS, "openai", frozenset({"gpt-5.2"})
    )
    # This module exercises client construction, not the account-scoped
    # catalog boundary.  Keep those unit tests independent of database
    # availability; catalog fail-closed behavior has dedicated coverage.
    original_get_chat_model = mp.get_chat_model

    def saved_provider(provider: str, model: str) -> dict:
        return {
            "id": f"test-{provider}",
            "account_id": "test-account",
            "provider_type": provider,
            "default_model": model,
            "_decrypted_api_key": "test-key",
        }

    def get_chat_model_from_saved_provider(config: dict, *args, **kwargs):
        candidate = deepcopy(config)
        agents_cfg = candidate.get("agents_config") or {}
        candidate["agents_config"] = agents_cfg
        candidate.setdefault("account_id", "test-account")
        provider = str(agents_cfg.get("default_provider") or "").strip()
        model = str(agents_cfg.get("default_model") or "").strip()
        if provider in {"openai", "anthropic", "google"} and model:
            provider_id = f"test-{provider}"
            agents_cfg["default_provider"] = f"custom:{provider_id}"
            agents_cfg["model_provider_id"] = provider_id
            kwargs.setdefault("claimed_custom_provider", saved_provider(provider, model))
            return original_get_chat_model(candidate, *args, **kwargs)
        return original_get_chat_model(config, *args, **kwargs)

    monkeypatch.setattr(mp, "get_chat_model", get_chat_model_from_saved_provider)
    with patch.object(model_catalog, "validate_chat_provider_model_pair", return_value=(True, "")):
        yield


@pytest.fixture
def mock_config():
    """Minimal CONFIG mock."""
    return {"default_provider": "openai", "default_model": "gpt-5.2", "default_temperature": 0.1}


def _stub_class(name):
    """Return a MagicMock that records constructor kwargs."""
    cls = MagicMock(name=name)
    cls.return_value = MagicMock(name=f"{name}_instance")
    return cls


# ---------------------------------------------------------------------------
# _resolve_named_provider
# ---------------------------------------------------------------------------


class TestResolveNamedProvider:
    def test_requires_account_scope(self):
        with pytest.raises(mp.ModelProviderResolutionError, match="requires account_id"):
            mp._resolve_named_provider(999, account_id="")

    def test_raises_when_no_logger(self):
        with (
            patch(
                "core.infrastructure.db.platform_stores.PlatformConnectionStore.get_shared_instance",
                return_value=None,
            ),
            pytest.raises(mp.ModelProviderResolutionError, match="store is unavailable"),
        ):
            mp._resolve_named_provider(999, account_id="acc_alpha")

    def test_raises_on_exception(self):
        with (
            patch(
                "core.infrastructure.db.platform_stores.PlatformConnectionStore.get_shared_instance",
                side_effect=RuntimeError("db down"),
            ),
            pytest.raises(mp.ModelProviderResolutionError),
        ):
            mp._resolve_named_provider(999, account_id="acc_alpha")

    def test_queries_provider_and_secret_by_account_scope(self):
        calls = []

        class _Cursor:
            def __init__(self, rows):
                self._rows = list(rows)

            def execute(self, statement, params=None):
                calls.append((str(statement), params))

            def fetchone(self):
                return self._rows.pop(0) if self._rows else None

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        class _Conn:
            def __init__(self):
                self._cursor_rows = [
                    [
                        {
                            "id": "provider-1",
                            "account_id": "acc_alpha",
                            "provider_type": "openai",
                            "api_key_ref": "ref-openai",
                            "api_key_ciphertext": "ciphertext",
                        }
                    ],
                ]

            def cursor(self, *args, **kwargs):
                del args, kwargs
                return _Cursor(self._cursor_rows.pop(0))

        logger = MagicMock()
        logger.conn = _Conn()
        with (
            patch(
                "core.infrastructure.db.platform_stores.PlatformConnectionStore.get_shared_instance",
                return_value=logger,
            ),
            patch("core.application.auth.secrets.decrypt_secret", return_value="plain-key"),
        ):
            result = mp._resolve_named_provider("provider-1", account_id="acc_alpha")

        assert result["_decrypted_api_key"] == "plain-key"
        assert "provider.account_id = %s" in calls[0][0]
        assert "JOIN secret_refs" in calls[0][0]
        assert "provider.verified_at >= now() - interval '30 days'" in calls[0][0]
        assert "provider.verified_at <= now()" in calls[0][0]
        assert calls[0][1] == ("provider-1", "acc_alpha")
        assert len(calls) == 1

    def test_provider_miss_fails_closed(self):
        class _Cursor:
            def execute(self, statement, params=None):
                del statement, params

            def fetchone(self):
                return None

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        logger = MagicMock()
        logger.conn.cursor.return_value = _Cursor()
        with (
            patch(
                "core.infrastructure.db.platform_stores.PlatformConnectionStore.get_shared_instance",
                return_value=logger,
            ),
            pytest.raises(mp.ModelProviderResolutionError, match="not found"),
        ):
            mp._resolve_named_provider("provider-1", account_id="acc_alpha")

    def test_provider_pointer_and_ciphertext_are_read_in_one_statement(self):
        calls = []

        class _Cursor:
            def execute(self, statement, params=None):
                calls.append((str(statement), params))

            def fetchone(self):
                return {
                    "id": "provider-1",
                    "account_id": "acc_alpha",
                    "provider_type": "openai_compatible",
                    "api_key_ref": "provider-key",
                    "api_key_ciphertext": "ciphertext",
                }

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        logger = MagicMock()
        logger.conn.cursor.side_effect = [_Cursor(), AssertionError("second read is not atomic")]
        with (
            patch(
                "core.infrastructure.db.platform_stores.PlatformConnectionStore.get_shared_instance",
                return_value=logger,
            ),
            patch("core.application.auth.secrets.decrypt_secret", return_value="plain-key"),
        ):
            result = mp._resolve_named_provider("provider-1", account_id="acc_alpha")

        assert result["_decrypted_api_key"] == "plain-key"
        assert len(calls) == 1
        assert "JOIN secret_refs" in calls[0][0]


# ---------------------------------------------------------------------------
# get_chat_model — provider branches
# ---------------------------------------------------------------------------


class TestGetChatModelOpenAI:
    def test_missing_route_does_not_default_to_openai(self, mock_config, monkeypatch):
        stub = _stub_class("ChatOpenAI")
        monkeypatch.setattr(mp, "ChatOpenAI", stub)
        with (
            patch("core.infrastructure.config.runtime_config.CONFIG", mock_config),
            pytest.raises(mp.ModelUnavailableError, match="model_unavailable"),
        ):
            mp.get_chat_model({"account_id": "test-account"})
        stub.assert_not_called()

    def test_named_provider_without_account_scope_fails_before_credential_lookup(self, mock_config):
        with (
            patch("core.infrastructure.config.runtime_config.CONFIG", mock_config),
            patch(
                "core.infrastructure.db.platform_stores.PlatformConnectionStore.get_shared_instance"
            ) as provider_store,
            pytest.raises(mp.ModelProviderResolutionError, match="requires account_id"),
        ):
            mp.get_chat_model(
                {
                    "agents_config": {
                        "default_provider": "custom:provider-1",
                        "default_model": "private-model",
                        "model_provider_id": "provider-1",
                    },
                }
            )

        provider_store.assert_not_called()

    def test_openai_compatible_with_named_provider(self, mock_config, monkeypatch):
        stub = _stub_class("ChatOpenAI")
        monkeypatch.setattr(mp, "ChatOpenAI", stub)
        named = {
            "account_id": "acc_alpha",
            "provider_type": "openai_compatible",
            "base_url": "http://localhost:11434/v1",
            "_decrypted_api_key": "llm-key",
            "default_model": "llama3",
        }
        with (
            patch("core.infrastructure.config.runtime_config.CONFIG", mock_config),
            patch(
                "core.application.orchestration.model_provider._resolve_named_provider",
                return_value=named,
            ) as resolve_provider,
            # FS-DEEP-CAND-058: the use-time SSRF guard + connection pinning would
            # reject/own-resolve the localhost test base_url under default policy;
            # this test only verifies client construction, so bypass the combined
            # validate-and-pin helper and return the configured endpoint as-is.
            patch(
                "core.application.orchestration.model_provider._safe_pinned_base_url",
                side_effect=lambda url, kwargs: str(url or ""),
            ),
        ):
            mp.get_chat_model(
                {
                    "account_id": "acc_alpha",
                    "agents_config": {
                        "default_provider": "custom:1",
                        "default_model": "llama3",
                        "model_provider_id": 1,
                    },
                }
            )
        stub.assert_called_once()
        resolve_provider.assert_called_once_with("1", account_id="acc_alpha", require_verified=True)
        kwargs = stub.call_args[1]
        assert kwargs["base_url"] == "http://localhost:11434/v1"
        assert kwargs["api_key"] == "llm-key"
        assert kwargs["model"] == "llama3"

    def test_builtin_openai_explicitly_uses_official_endpoint(self, mock_config, monkeypatch):
        stub = _stub_class("ChatOpenAI")
        monkeypatch.setattr(mp, "ChatOpenAI", stub)
        with (
            patch("core.infrastructure.config.runtime_config.CONFIG", mock_config),
            patch.dict(
                "os.environ",
                {
                    "OPENAI_API_KEY": "process-key",
                    "OPENAI_BASE_URL": "https://attacker.example/v1",
                    "OPENAI_API_BASE": "https://legacy-attacker.example/v1",
                },
            ),
        ):
            mp.get_chat_model(
                {
                    "agents_config": {
                        "default_provider": "openai",
                        "default_model": "gpt-5.2",
                    }
                }
            )

        assert stub.call_args.kwargs["base_url"] == "https://api.openai.com/v1"

    def test_openai_compatible_without_stored_endpoint_never_uses_ambient_endpoint(
        self, mock_config, monkeypatch
    ):
        stub = _stub_class("ChatOpenAI")
        monkeypatch.setattr(mp, "ChatOpenAI", stub)
        named = {
            "account_id": "acc_alpha",
            "provider_type": "openai_compatible",
            "base_url": "",
            "_decrypted_api_key": "account-key",
            "default_model": "private-model",
        }
        with (
            patch.dict("os.environ", {"OPENAI_BASE_URL": "https://attacker.example/v1"}),
            patch("core.infrastructure.config.runtime_config.CONFIG", mock_config),
            patch.object(mp, "_resolve_named_provider", return_value=named),
            pytest.raises(mp.ModelProviderResolutionError, match="base_url is required"),
        ):
            mp.get_chat_model(
                {
                    "account_id": "acc_alpha",
                    "agents_config": {
                        "default_provider": "custom:provider-1",
                        "default_model": "private-model",
                        "model_provider_id": "provider-1",
                    },
                }
            )

        stub.assert_not_called()

    def test_named_provider_without_account_secret_never_uses_ambient_key(
        self, mock_config, monkeypatch
    ):
        stub = _stub_class("ChatOpenAI")
        monkeypatch.setattr(mp, "ChatOpenAI", stub)
        named = {
            "account_id": "acc_alpha",
            "provider_type": "openai_compatible",
            "base_url": "",
            "default_model": "private-model",
        }
        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "process-global-key"}),
            patch("core.infrastructure.config.runtime_config.CONFIG", mock_config),
            patch(
                "core.application.orchestration.model_provider._resolve_named_provider",
                return_value=named,
            ),
            pytest.raises(mp.ModelUnavailableError, match="authentication_error"),
        ):
            mp.get_chat_model(
                {
                    "account_id": "acc_alpha",
                    "agents_config": {
                        "default_provider": "custom:provider-1",
                        "default_model": "private-model",
                        "model_provider_id": "provider-1",
                    },
                }
            )
        stub.assert_not_called()

    def test_explicit_named_provider_resolution_error_does_not_fallback(
        self, mock_config, monkeypatch
    ):
        stub = _stub_class("ChatOpenAI")
        monkeypatch.setattr(mp, "ChatOpenAI", stub)
        with (
            patch("core.infrastructure.config.runtime_config.CONFIG", mock_config),
            patch(
                "core.application.orchestration.model_provider._resolve_named_provider",
                side_effect=mp.ModelProviderResolutionError("provider missing"),
            ),
            pytest.raises(mp.ModelProviderResolutionError, match="provider missing"),
        ):
            mp.get_chat_model(
                {
                    "account_id": "acc_alpha",
                    "agents_config": {
                        "default_provider": "custom:1",
                        "default_model": "llama3",
                        "model_provider_id": 1,
                    },
                }
            )
        stub.assert_not_called()


class TestGetChatModelAzure:
    def test_azure_openai_from_named_provider(self, mock_config, monkeypatch):
        stub = _stub_class("AzureChatOpenAI")
        monkeypatch.setattr(mp, "AzureChatOpenAI", stub)
        named = {
            "account_id": "acc_alpha",
            "provider_type": "azure_openai",
            "base_url": "https://my-resource.openai.azure.com/",
            "api_version": "2024-06-01",
            "deployment_name": "gpt-4o-deploy",
            "_decrypted_api_key": "azure-key-123",
            "default_model": "gpt-4o",
        }
        with (
            patch("core.infrastructure.config.runtime_config.CONFIG", mock_config),
            patch(
                "core.application.orchestration.model_provider._resolve_named_provider",
                return_value=named,
            ),
            # FS-DEEP-CAND-058: bypass the use-time SSRF validate-and-pin helper for
            # this construction test (the fake azure host does not resolve in CI).
            patch(
                "core.application.orchestration.model_provider._safe_pinned_base_url",
                side_effect=lambda url, kwargs: str(url or ""),
            ),
        ):
            mp.get_chat_model(
                {
                    "account_id": "acc_alpha",
                    "agents_config": {
                        "default_provider": "custom:42",
                        "default_model": "gpt-4o",
                        "model_provider_id": 42,
                    },
                }
            )
        stub.assert_called_once()
        kwargs = stub.call_args[1]
        assert kwargs["azure_endpoint"] == "https://my-resource.openai.azure.com/"
        assert kwargs["api_version"] == "2024-06-01"
        assert kwargs["azure_deployment"] == "gpt-4o-deploy"
        assert kwargs["api_key"] == "azure-key-123"

    @pytest.mark.parametrize("missing_field", ["base_url", "api_version", "deployment_name"])
    def test_azure_openai_never_inherits_missing_endpoint_config_from_environment(
        self, mock_config, missing_field, monkeypatch
    ):
        stub = _stub_class("AzureChatOpenAI")
        monkeypatch.setattr(mp, "AzureChatOpenAI", stub)
        named = {
            "account_id": "acc_alpha",
            "provider_type": "azure_openai",
            "base_url": "https://my-resource.openai.azure.com/",
            "api_version": "2024-06-01",
            "deployment_name": "gpt-4o-deploy",
            "_decrypted_api_key": "azure-key-123",
            "default_model": "gpt-4o",
        }
        named[missing_field] = ""
        with (
            patch.dict(
                "os.environ",
                {
                    "AZURE_OPENAI_ENDPOINT": "https://attacker.openai.azure.com/",
                    "OPENAI_API_VERSION": "2099-01-01",
                },
            ),
            patch("core.infrastructure.config.runtime_config.CONFIG", mock_config),
            patch.object(mp, "_resolve_named_provider", return_value=named),
            patch.object(mp, "_safe_pinned_base_url") as pin_endpoint,
            pytest.raises(mp.ModelProviderResolutionError, match=f"{missing_field} is required"),
        ):
            mp.get_chat_model(
                {
                    "account_id": "acc_alpha",
                    "agents_config": {
                        "default_provider": "custom:42",
                        "default_model": "gpt-4o",
                        "model_provider_id": 42,
                    },
                }
            )

        pin_endpoint.assert_not_called()
        stub.assert_not_called()


class TestGetChatModelAnthropic:
    def test_anthropic_with_named_provider_key(self, mock_config, monkeypatch):
        stub = _stub_class("ChatAnthropic")
        monkeypatch.setattr(mp, "ChatAnthropic", stub)
        named = {
            "account_id": "acc_alpha",
            "provider_type": "anthropic",
            "_decrypted_api_key": "sk-ant-custom",
            "default_model": "claude-sonnet-4-6",
        }
        with (
            patch("core.infrastructure.config.runtime_config.CONFIG", mock_config),
            patch(
                "core.application.orchestration.model_provider._resolve_named_provider",
                return_value=named,
            ),
        ):
            config = {
                "account_id": "acc_alpha",
                "agents_config": {
                    "default_provider": "custom:5",
                    "default_model": "claude-sonnet-4-6",
                    "model_provider_id": 5,
                },
            }
            mp.get_chat_model(config)
        stub.assert_called_once()
        kwargs = stub.call_args[1]
        assert kwargs["api_key"] == "sk-ant-custom"
        assert kwargs["base_url"] == "https://api.anthropic.com"

    def test_anthropic_named_provider_rejects_custom_endpoint(self, mock_config, monkeypatch):
        stub = _stub_class("ChatAnthropic")
        monkeypatch.setattr(mp, "ChatAnthropic", stub)
        named = {
            "account_id": "acc_alpha",
            "provider_type": "anthropic",
            "base_url": "https://gateway.example/v1",
            "_decrypted_api_key": "sk-ant-custom",
            "default_model": "claude-sonnet-4-6",
        }
        with (
            patch.dict("os.environ", {"ANTHROPIC_BASE_URL": "https://attacker.example"}),
            patch("core.infrastructure.config.runtime_config.CONFIG", mock_config),
            patch.object(mp, "_resolve_named_provider", return_value=named),
            pytest.raises(mp.ModelProviderResolutionError, match="does not accept base_url"),
        ):
            mp.get_chat_model(
                {
                    "account_id": "acc_alpha",
                    "agents_config": {
                        "default_provider": "custom:5",
                        "default_model": "claude-sonnet-4-6",
                        "model_provider_id": 5,
                    },
                }
            )

        stub.assert_not_called()

    @pytest.mark.parametrize("model", ["claude-opus-4-8", "claude-sonnet-5"])
    def test_adaptive_thinking_models_omit_temperature(self, mock_config, model, monkeypatch):
        stub = _stub_class("ChatAnthropic")
        monkeypatch.setattr(mp, "ChatAnthropic", stub)
        with (
            patch("core.infrastructure.config.runtime_config.CONFIG", mock_config),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
        ):
            mp.get_chat_model(
                {
                    "agents_config": {
                        "default_provider": "anthropic",
                        "default_model": model,
                        "default_temperature": 0,
                    }
                },
                allow_unverified_builtin=True,
            )
        assert "temperature" not in stub.call_args.kwargs


class TestGetChatModelGoogle:
    def test_google_with_named_provider_key(self, mock_config, monkeypatch):
        stub = _stub_class("ChatGoogleGenerativeAI")
        monkeypatch.setattr(mp, "ChatGoogleGenerativeAI", stub)
        named = {
            "account_id": "acc_alpha",
            "provider_type": "google",
            "_decrypted_api_key": "AIza-custom",
            "default_model": "gemini-3.5-flash",
        }
        with (
            patch("core.infrastructure.config.runtime_config.CONFIG", mock_config),
            patch(
                "core.application.orchestration.model_provider._resolve_named_provider",
                return_value=named,
            ),
        ):
            config = {
                "account_id": "acc_alpha",
                "agents_config": {
                    "default_provider": "custom:7",
                    "default_model": "gemini-3.5-flash",
                    "model_provider_id": 7,
                },
            }
            mp.get_chat_model(config)
        stub.assert_called_once()
        kwargs = stub.call_args[1]
        assert kwargs["google_api_key"] == "AIza-custom"
        assert kwargs["base_url"] == "https://generativelanguage.googleapis.com/"
        assert kwargs["vertexai"] is False

    def test_google_named_provider_rejects_custom_endpoint(self, mock_config, monkeypatch):
        stub = _stub_class("ChatGoogleGenerativeAI")
        monkeypatch.setattr(mp, "ChatGoogleGenerativeAI", stub)
        named = {
            "account_id": "acc_alpha",
            "provider_type": "google",
            "base_url": "https://gateway.example/v1",
            "_decrypted_api_key": "AIza-custom",
            "default_model": "gemini-3.5-flash",
        }
        with (
            patch.dict(
                "os.environ",
                {
                    "GOOGLE_GENAI_USE_VERTEXAI": "true",
                    "GOOGLE_GEMINI_BASE_URL": "https://attacker.example",
                },
            ),
            patch("core.infrastructure.config.runtime_config.CONFIG", mock_config),
            patch.object(mp, "_resolve_named_provider", return_value=named),
            pytest.raises(mp.ModelProviderResolutionError, match="does not accept base_url"),
        ):
            mp.get_chat_model(
                {
                    "account_id": "acc_alpha",
                    "agents_config": {
                        "default_provider": "custom:7",
                        "default_model": "gemini-3.5-flash",
                        "model_provider_id": 7,
                    },
                }
            )

        stub.assert_not_called()

    def test_google_uses_saved_provider_credential(self, mock_config, monkeypatch):
        monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
        runtime_config = dict(mock_config)

        with patch("core.infrastructure.config.runtime_config.CONFIG", runtime_config):
            model = mp.get_chat_model(
                {
                    "agents_config": {
                        "default_provider": "google",
                        "default_model": "gemini-3.6-flash",
                    }
                },
                allow_unverified_builtin=True,
            )

        try:
            assert model.client._api_client.api_key == "test-key"
            assert model.client._api_client.vertexai is False
        finally:
            model.client.close()
            asyncio.run(model.async_client.aclose())

    @pytest.mark.parametrize(
        "backend_env_name",
        ["GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_GENAI_USE_ENTERPRISE"],
    )
    def test_google_named_provider_pins_inner_sdk_backend_and_request_path(
        self, mock_config, monkeypatch, backend_env_name
    ):
        for env_name in (
            "GOOGLE_GENAI_USE_VERTEXAI",
            "GOOGLE_GENAI_USE_ENTERPRISE",
            "GOOGLE_CLOUD_PROJECT",
            "GOOGLE_CLOUD_LOCATION",
        ):
            monkeypatch.delenv(env_name, raising=False)
        monkeypatch.setenv(backend_env_name, "true")
        monkeypatch.setenv("GOOGLE_API_KEY", "process-global-key")
        named = {
            "account_id": "acc_alpha",
            "provider_type": "google",
            "_decrypted_api_key": "account-scoped-key",
            "default_model": "gemini-2.5-flash",
        }

        with (
            patch("core.infrastructure.config.runtime_config.CONFIG", mock_config),
            patch.object(mp, "_resolve_named_provider", return_value=named),
        ):
            model = mp.get_chat_model(
                {
                    "account_id": "acc_alpha",
                    "agents_config": {
                        "default_provider": "custom:7",
                        "default_model": "gemini-2.5-flash",
                        "model_provider_id": 7,
                    },
                }
            )

        api_client = model.client._api_client
        try:
            assert api_client.vertexai is False
            assert api_client.api_key == "account-scoped-key"
            assert api_client._http_options.base_url == "https://generativelanguage.googleapis.com/"
            assert model.async_client._api_client is api_client

            request = {}

            class _RequestCaptured(Exception):
                pass

            def capture_request(method, path, *args, **kwargs):
                request.update(method=method, path=path, args=args, kwargs=kwargs)
                raise _RequestCaptured

            monkeypatch.setattr(api_client, "request", capture_request)
            with pytest.raises(_RequestCaptured):
                model.client.models.generate_content(
                    model=model.model,
                    contents="hello",
                )

            assert request["method"] == "post"
            assert request["path"] == "models/gemini-2.5-flash:generateContent"
            assert "publishers/google" not in request["path"]
        finally:
            model.client.close()
            asyncio.run(model.async_client.aclose())


class TestGetChatModelTimeout:
    def test_timeout_passed_through(self, mock_config, monkeypatch):
        stub = _stub_class("ChatOpenAI")
        monkeypatch.setattr(mp, "ChatOpenAI", stub)
        with (
            patch("core.infrastructure.config.runtime_config.CONFIG", mock_config),
            patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}),
        ):
            mp.get_chat_model(
                {"agents_config": {"default_provider": "openai", "default_model": "gpt-5.2"}},
                timeout_seconds=30,
            )
        kwargs = stub.call_args[1]
        assert kwargs["timeout"] == 30.0

    def test_no_timeout_when_zero(self, mock_config, monkeypatch):
        stub = _stub_class("ChatOpenAI")
        monkeypatch.setattr(mp, "ChatOpenAI", stub)
        with (
            patch("core.infrastructure.config.runtime_config.CONFIG", mock_config),
            patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}, clear=True),
        ):
            mp.get_chat_model(
                {"agents_config": {"default_provider": "openai", "default_model": "gpt-5.2"}},
                timeout_seconds=0,
            )
        kwargs = stub.call_args[1]
        assert "timeout" not in kwargs

    def test_env_timeout_applies_when_timeout_unset(self, mock_config, monkeypatch):
        stub = _stub_class("ChatOpenAI")
        monkeypatch.setattr(mp, "ChatOpenAI", stub)
        with (
            patch("core.infrastructure.config.runtime_config.CONFIG", mock_config),
            patch.dict(
                "os.environ",
                {"FS_LLM_REQUEST_TIMEOUT_SECONDS": "12", "OPENAI_API_KEY": "test-key"},
            ),
        ):
            mp.get_chat_model(
                {"agents_config": {"default_provider": "openai", "default_model": "gpt-5.2"}}
            )
        kwargs = stub.call_args[1]
        assert kwargs["timeout"] == 12.0


class TestNormalizeTimeout:
    def test_none_returns_none(self):
        with patch.dict("os.environ", {}, clear=True):
            assert mp._normalize_timeout_seconds(None) is None

    def test_negative_returns_none(self):
        with patch.dict("os.environ", {}, clear=True):
            assert mp._normalize_timeout_seconds(-5) is None

    def test_env_cap_applies_when_timeout_unset(self):
        with patch.dict("os.environ", {"FS_LLM_REQUEST_TIMEOUT_SECONDS": "10"}):
            assert mp._normalize_timeout_seconds(None) == 10.0

    def test_env_cap_applies_when_timeout_zero(self):
        with patch.dict("os.environ", {"FS_LLM_REQUEST_TIMEOUT_SECONDS": "10"}):
            assert mp._normalize_timeout_seconds(0) == 10.0

    def test_unset_env_does_not_cap(self):
        with patch.dict("os.environ", {}, clear=True):
            assert mp._normalize_timeout_seconds(180) == 180.0

    def test_capped_by_env(self):
        with patch.dict("os.environ", {"FS_LLM_REQUEST_TIMEOUT_SECONDS": "10"}):
            assert mp._normalize_timeout_seconds(30) == 10.0

    def test_under_cap(self):
        with patch.dict("os.environ", {"FS_LLM_REQUEST_TIMEOUT_SECONDS": "60"}):
            assert mp._normalize_timeout_seconds(30) == 30.0
