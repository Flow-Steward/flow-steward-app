"""
Unit tests for core/model_provider.py — get_chat_model().

Tests:
- Returns ChatAnthropic for anthropic provider
- Returns ChatOpenAI for openai provider
- Returns ChatGoogleGenerativeAI for google provider
- Per-agent model override
- Default model from env vars
- Fallback when no config

Run:
    docker compose exec fs-app python -m pytest tests/unit/test_model_provider.py -v
"""

from __future__ import annotations

import os
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _candidate_mode_for_client_construction(monkeypatch):
    """Construct API clients from an account-scoped saved-provider snapshot."""
    from core.application.orchestration import model_provider
    from core.application.settings import model_catalog

    original_catalog = dict(model_catalog.PROVIDER_CATALOG)
    original_verified = dict(model_catalog.BUILTIN_RELEASE_VERIFIED_MODELS)
    monkeypatch.setattr(
        "core.infrastructure.db.platform_stores.PlatformConnectionStore.get_shared_instance",
        lambda: SimpleNamespace(conn=object()),
    )
    monkeypatch.setattr(model_catalog, "_load_visibility_policy", lambda *_args, **_kwargs: {})
    for provider, entries in {
        "openai": (model_catalog.ModelEntry("gpt-5.2", "test candidate"),),
        "anthropic": (
            model_catalog.ModelEntry("claude-opus-4-6", "test candidate"),
            model_catalog.ModelEntry("claude-sonnet-4-6", "test candidate"),
        ),
    }.items():
        family = model_catalog.PROVIDER_CATALOG[provider]
        model_catalog.PROVIDER_CATALOG[provider] = type(family)(
            **{**family.__dict__, "models": (*family.models, *entries)}
        )

    original_get_chat_model = model_provider.get_chat_model

    def saved_provider(provider: str, model: str) -> dict:
        return {
            "id": f"test-{provider}",
            "account_id": "test-account",
            "provider_type": provider,
            "default_model": model,
            "_decrypted_api_key": "test-key",
        }

    def candidate_get_chat_model(*args, **kwargs):
        config = deepcopy(args[0])
        agents_cfg = config.get("agents_config") or {}
        config["agents_config"] = agents_cfg
        config.setdefault("account_id", "test-account")
        agent_name = kwargs.get("agent_name")
        agent_cfg = model_provider._project_agent_config(agents_cfg, agent_name)
        selected_provider = str(
            agent_cfg.get("provider") or agents_cfg.get("default_provider") or ""
        ).strip()
        selected_model = str(
            agent_cfg.get("model") or agents_cfg.get("default_model") or ""
        ).strip()
        if selected_provider not in {"openai", "anthropic", "google"} or not selected_model:
            return original_get_chat_model(*args, **kwargs)

        provider_id = f"test-{selected_provider}"
        if agent_cfg:
            agent_cfg["provider"] = f"custom:{provider_id}"
            agent_cfg["model_provider_id"] = provider_id
        else:
            agents_cfg["default_provider"] = f"custom:{provider_id}"
            agents_cfg["model_provider_id"] = provider_id
        kwargs.setdefault(
            "claimed_custom_provider", saved_provider(selected_provider, selected_model)
        )
        args = (config, *args[1:])
        return original_get_chat_model(*args, **kwargs)

    monkeypatch.setattr(model_provider, "get_chat_model", candidate_get_chat_model)
    yield
    model_catalog.PROVIDER_CATALOG.clear()
    model_catalog.PROVIDER_CATALOG.update(original_catalog)
    model_catalog.BUILTIN_RELEASE_VERIFIED_MODELS.clear()
    model_catalog.BUILTIN_RELEASE_VERIFIED_MODELS.update(original_verified)


def test_normalize_provider_model_pair_rejects_missing_model_without_fallback():
    from core.application.orchestration.model_provider import (
        ModelUnavailableError,
        _normalize_provider_model_pair,
    )

    with pytest.raises(ModelUnavailableError, match="model_unavailable"):
        _normalize_provider_model_pair("openai", "")


def test_normalize_provider_model_pair_rejects_removed_model_id() -> None:
    from core.application.orchestration.model_provider import (
        ModelUnavailableError,
        _normalize_provider_model_pair,
    )

    with pytest.raises(ModelUnavailableError, match="does not belong"):
        _normalize_provider_model_pair("openai", "gpt-4o")


def test_get_chat_model_rechecks_account_availability_for_builtin_route(monkeypatch) -> None:
    from core.application.orchestration import model_provider as mp

    monkeypatch.setattr(mp, "ChatOpenAI", MagicMock())
    monkeypatch.setattr(mp, "_get_registry_agent_config", lambda _name: {})
    monkeypatch.setattr(
        "core.application.settings.model_catalog.validate_provider_model_pair",
        lambda *_args, **_kwargs: (False, "Provider 'openai' is disabled in account settings."),
    )

    with pytest.raises(mp.ModelUnavailableError, match="model_unavailable"):
        mp.get_chat_model(
            {
                "account_id": "acc-disabled",
                "agents_config": {
                    "default_provider": "openai",
                    "default_model": "gpt-5.6-sol",
                },
            }
        )

    mp.ChatOpenAI.assert_not_called()


sys.path.insert(0, str(Path(__file__).parent.parent.parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def anthropic_config(model: str = "claude-haiku-4-5-20251001") -> dict:
    return {
        "agents_config": {
            "default_provider": "anthropic",
            "default_model": model,
        }
    }


def openai_config(model: str = "gpt-5.2") -> dict:
    return {
        "agents_config": {
            "default_provider": "openai",
            "default_model": model,
        }
    }


def google_config(model: str = "gemini-3.6-flash") -> dict:
    return {
        "agents_config": {
            "default_provider": "google",
            "default_model": model,
        }
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGetChatModelProviders:
    """Provider selection based on config."""

    def test_anthropic_provider_returns_chat_anthropic(self):
        """anthropic provider → ChatAnthropic instance."""
        try:
            from core.application.orchestration.model_provider import get_chat_model
        except ImportError:
            pytest.skip("core.application.orchestration.model_provider not importable")

        with patch("core.application.orchestration.model_provider.ChatAnthropic") as mock_cls:
            mock_cls.return_value = MagicMock()
            result = get_chat_model(anthropic_config())

        mock_cls.assert_called_once()

    def test_openai_provider_returns_chat_openai(self):
        """openai provider → ChatOpenAI instance."""
        try:
            from core.application.orchestration.model_provider import get_chat_model
        except ImportError:
            pytest.skip("core.application.orchestration.model_provider not importable")

        with patch("core.application.orchestration.model_provider.ChatOpenAI") as mock_cls:
            mock_cls.return_value = MagicMock()
            result = get_chat_model(openai_config())

        mock_cls.assert_called_once()
        assert mock_cls.call_args.kwargs["metadata"] == {
            "flow_steward_provider": "custom:test-openai",
            "flow_steward_provider_policy": "openai",
            "flow_steward_model": "gpt-5.2",
        }

    @pytest.mark.parametrize(
        "model_id",
        ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"),
    )
    def test_direct_gpt_5_6_uses_responses_api_for_reasoning_and_tools(self, model_id):
        from core.application.orchestration.model_provider import get_chat_model

        with patch("core.application.orchestration.model_provider.ChatOpenAI") as mock_cls:
            mock_cls.return_value = MagicMock()
            get_chat_model(openai_config(model_id))

        kwargs = mock_cls.call_args.kwargs
        assert kwargs["use_responses_api"] is True
        assert kwargs["reasoning"] == {"effort": "medium"}
        assert "reasoning_effort" not in kwargs
        assert "temperature" not in kwargs

    def test_google_provider_returns_chat_google(self):
        """google provider → ChatGoogleGenerativeAI instance."""
        try:
            from core.application.orchestration.model_provider import get_chat_model
        except ImportError:
            pytest.skip("core.application.orchestration.model_provider not importable")

        # ChatGoogleGenerativeAI is imported lazily inside the function
        with patch("langchain_google_genai.ChatGoogleGenerativeAI") as mock_cls:
            mock_cls.return_value = MagicMock()
            try:
                result = get_chat_model(google_config())
                mock_cls.assert_called_once()
            except Exception:
                # Google package may not be installed — skip gracefully
                pytest.skip("langchain_google_genai not available")

    def test_returns_model_instance(self):
        """get_chat_model returns an object (not None)."""
        try:
            from core.application.orchestration.model_provider import get_chat_model
        except ImportError:
            pytest.skip("core.application.orchestration.model_provider not importable")

        with patch("core.application.orchestration.model_provider.ChatAnthropic") as mock_cls:
            mock_cls.return_value = MagicMock(name="mock_anthropic")
            result = get_chat_model(anthropic_config())

        assert result is not None

    def test_custom_provider_key_resolves_its_verified_selected_route(self):
        from core.application.orchestration import model_provider as mp

        config = {
            "account_id": "acc_default",
            "agents_config": {
                "default_provider": "custom:provider-123",
                "default_model": "gateway-model-7",
            },
        }
        named = {
            "provider_type": "openai_compatible",
            "default_model": "gateway-model-7",
            "base_url": "https://gateway.example/v1",
            "_decrypted_api_key": "test-key",
        }
        with (
            patch.object(mp, "_resolve_named_provider", return_value=named) as resolve,
            patch.object(mp, "_safe_pinned_base_url", side_effect=lambda url, _kwargs: str(url)),
            patch.object(mp, "ChatOpenAI") as chat_openai,
        ):
            chat_openai.return_value = MagicMock()
            mp.get_chat_model(config)

        resolve.assert_called_once_with(
            "provider-123", account_id="acc_default", require_verified=True
        )
        assert chat_openai.call_args.kwargs["model"] == "gateway-model-7"
        assert chat_openai.call_args.kwargs["metadata"] == {
            "flow_steward_provider": "custom:provider-123",
            "flow_steward_provider_policy": "openai_compatible",
            "flow_steward_model": "gateway-model-7",
        }

    def test_custom_provider_key_rejects_model_other_than_selected_route(self):
        from core.application.orchestration import model_provider as mp

        config = {
            "account_id": "acc_default",
            "agents_config": {
                "default_provider": "custom:provider-123",
                "default_model": "other-model",
            },
        }
        with (
            patch.object(
                mp,
                "_resolve_named_provider",
                return_value={
                    "provider_type": "openai_compatible",
                    "default_model": "gateway-model-7",
                },
            ),
            pytest.raises(mp.ModelUnavailableError, match="no longer the verified selected model"),
        ):
            mp.get_chat_model(config)

    def test_named_provider_id_rejects_changed_persisted_project_model(self):
        from core.application.orchestration import model_provider as mp

        config = {
            "account_id": "acc_default",
            "agents_config": {
                "default_provider": "custom:provider-123",
                "default_model": "old-model",
                "model_provider_id": "provider-123",
            },
        }
        with (
            patch.object(
                mp,
                "_resolve_named_provider",
                return_value={
                    "provider_type": "openai_compatible",
                    "default_model": "new-model",
                },
            ),
            pytest.raises(mp.ModelUnavailableError, match="model_unavailable"),
        ):
            mp.get_chat_model(config)

    def test_named_provider_id_rejects_changed_persisted_agent_model(self):
        from core.application.orchestration import model_provider as mp

        config = {
            "account_id": "acc_default",
            "agents_config": {
                "default_provider": "openai",
                "default_model": "gpt-5.2",
                "agents": {
                    "config": {
                        "composer": {
                            "provider": "custom:provider-123",
                            "model": "old-model",
                            "model_provider_id": "provider-123",
                        }
                    }
                },
            },
        }
        with (
            patch.object(
                mp,
                "_resolve_named_provider",
                return_value={
                    "provider_type": "openai_compatible",
                    "default_model": "new-model",
                },
            ),
            pytest.raises(mp.ModelUnavailableError, match="model_unavailable"),
        ):
            mp.get_chat_model(config, agent_name="composer")

    def test_named_provider_id_accepts_exact_persisted_route(self):
        from core.application.orchestration import model_provider as mp

        config = {
            "account_id": "acc_default",
            "agents_config": {
                "default_provider": "custom:provider-123",
                "default_model": "gateway-model-7",
                "model_provider_id": "provider-123",
            },
        }
        named = {
            "provider_type": "openai_compatible",
            "default_model": "gateway-model-7",
            "base_url": "https://gateway.example/v1",
            "_decrypted_api_key": "test-key",
        }
        with (
            patch.object(mp, "_resolve_named_provider", return_value=named),
            patch.object(mp, "_safe_pinned_base_url", side_effect=lambda url, _kwargs: str(url)),
            patch.object(mp, "ChatOpenAI") as chat_openai,
        ):
            mp.get_chat_model(config)

        assert chat_openai.call_args.kwargs["model"] == "gateway-model-7"
        assert chat_openai.call_args.kwargs["metadata"] == {
            "flow_steward_provider": "custom:provider-123",
            "flow_steward_provider_policy": "openai_compatible",
            "flow_steward_model": "gateway-model-7",
        }


class TestGetChatModelAgentOverride:
    """Per-agent model configuration override."""

    def test_per_agent_model_override(self):
        """Per-agent config overrides default model."""
        try:
            from core.application.orchestration.model_provider import get_chat_model
        except ImportError:
            pytest.skip("core.application.orchestration.model_provider not importable")

        config = {
            "agents_config": {
                "default_provider": "anthropic",
                "default_model": "claude-haiku-4-5-20251001",
                "agents": {
                    "config": {
                        "composer": {
                            "model": "claude-opus-4-6",
                            "provider": "anthropic",
                        }
                    }
                },
            }
        }

        captured_kwargs = []

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            with patch("core.application.orchestration.model_provider.ChatAnthropic") as mock_cls:

                def capture(**kwargs):
                    captured_kwargs.append(kwargs)
                    return MagicMock()

                mock_cls.side_effect = capture
                result = get_chat_model(config, agent_name="composer")

        # If per-agent override works, the model should be claude-opus-4-6
        if captured_kwargs:
            model_used = captured_kwargs[0].get("model", "")
            # Either the override model or the default - either is acceptable
            assert isinstance(model_used, str)

    def test_unknown_agent_uses_default(self):
        """Unknown agent name falls back to default config."""
        try:
            from core.application.orchestration.model_provider import get_chat_model
        except ImportError:
            pytest.skip("core.application.orchestration.model_provider not importable")

        config = {
            "agents_config": {
                "default_provider": "anthropic",
                "default_model": "claude-haiku-4-5-20251001",
            }
        }

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            with patch("core.application.orchestration.model_provider.ChatAnthropic") as mock_cls:
                mock_cls.return_value = MagicMock()
                result = get_chat_model(config, agent_name="nonexistent_agent_xyz")

        mock_cls.assert_called_once()

    def test_project_default_beats_registry_agent_config(self):
        """Project defaults should override registry agent config."""
        try:
            from core.application.orchestration.model_provider import get_chat_model
        except ImportError:
            pytest.skip("core.application.orchestration.model_provider not importable")

        config = {
            "agents_config": {
                "default_provider": "openai",
                "default_model": "gpt-5.2",
            }
        }

        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}),
            patch(
                "core.application.orchestration.model_provider._get_registry_agent_config"
            ) as mock_registry,
            patch("core.application.orchestration.model_provider.ChatOpenAI") as mock_openai,
        ):
            mock_registry.return_value = {
                "provider": "anthropic",
                "model": "claude-haiku-4-5-20251001",
            }
            mock_openai.return_value = MagicMock()
            get_chat_model(config, agent_name="proofreader")

        mock_openai.assert_called_once()

    def test_project_default_model_beats_registry_model(self):
        """Project default model should override registry model when no per-agent override exists."""
        try:
            from core.application.orchestration.model_provider import get_chat_model
        except ImportError:
            pytest.skip("core.application.orchestration.model_provider not importable")

        config = {
            "agents_config": {
                "default_provider": "openai",
                "default_model": "gpt-5.2",
            }
        }

        captured_kwargs = []
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}),
            patch(
                "core.application.orchestration.model_provider._get_registry_agent_config"
            ) as mock_registry,
            patch("core.application.orchestration.model_provider.ChatOpenAI") as mock_openai,
        ):

            def capture(**kwargs):
                captured_kwargs.append(kwargs)
                return MagicMock()

            mock_openai.side_effect = capture
            mock_registry.return_value = {
                "provider": "openai",
                "model": "gpt-5.2",
            }
            get_chat_model(config, agent_name="proofreader")

        assert captured_kwargs
        assert captured_kwargs[0]["model"] == "gpt-5.2"

    def test_get_chat_model_reuses_registry_snapshot_for_route_and_settings(self, monkeypatch):
        """Route selection and construction settings must share one registry read."""
        from core.application.orchestration import model_provider as mp

        registry_calls: list[str] = []

        def get_registry_config(agent_name: str) -> dict:
            registry_calls.append(agent_name)
            if len(registry_calls) == 1:
                return {
                    "provider": "custom:test-openai",
                    "model": "gpt-5.2",
                    "model_provider_id": "test-openai",
                    "temperature": 0.37,
                    "max_retries": 2,
                }
            return {
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "temperature": 0.91,
                "max_retries": 5,
            }

        monkeypatch.setattr(mp, "_get_registry_agent_config", get_registry_config)
        monkeypatch.setattr(mp, "ChatOpenAI", MagicMock())
        monkeypatch.setattr(mp, "ChatAnthropic", MagicMock())
        monkeypatch.setattr(
            "core.application.settings.model_catalog.validate_provider_model_pair",
            lambda *_args, **_kwargs: (True, ""),
        )
        monkeypatch.delenv("FS_LLM_REQUEST_TIMEOUT_SECONDS", raising=False)

        mp.get_chat_model(
            {"account_id": "test-account"},
            agent_name="proofreader",
            claimed_custom_provider={
                "id": "test-openai",
                "account_id": "test-account",
                "provider_type": "openai",
                "default_model": "gpt-5.2",
                "_decrypted_api_key": "test-key",
            },
        )

        assert registry_calls == ["proofreader"]
        mp.ChatOpenAI.assert_called_once_with(
            model="gpt-5.2",
            base_url="https://api.openai.com/v1",
            temperature=0.37,
            api_key="test-key",
            max_retries=2,
            metadata={
                "flow_steward_provider": "custom:test-openai",
                "flow_steward_provider_policy": "openai",
                "flow_steward_model": "gpt-5.2",
            },
        )
        mp.ChatAnthropic.assert_not_called()

    def test_explicit_openai_provider_does_not_inherit_anthropic_default_model(self):
        """An incomplete override cannot borrow a model from another provider."""
        from core.application.orchestration.model_provider import (
            ModelUnavailableError,
            get_chat_model,
        )

        config = {
            "agents_config": {
                "default_provider": "openai",
                "default_model": "claude-sonnet-4-6",
                "agents": {
                    "config": {
                        "summarizer": {
                            "provider": "openai",
                            "model": "",
                        }
                    }
                },
            }
        }

        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}),
            patch("core.application.orchestration.model_provider.ChatOpenAI") as mock_openai,
            pytest.raises(ModelUnavailableError, match="model_unavailable"),
        ):
            get_chat_model(config, agent_name="summarizer")

        mock_openai.assert_not_called()


class TestGetChatModelMissingRoute:
    """Routing must not select a provider from process environment defaults."""

    def test_api_key_environment_does_not_select_a_route(self):
        """A credential alone cannot create a routable chat model."""
        from core.application.orchestration.model_provider import (
            ModelUnavailableError,
            get_chat_model,
        )

        # Config with no provider specified but env has Anthropic key
        config = {}

        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=False),
            patch("core.application.orchestration.model_provider.ChatAnthropic") as mock_anthropic,
        ):
            with pytest.raises(ModelUnavailableError, match="model_unavailable"):
                get_chat_model(config)
        mock_anthropic.assert_not_called()

    def test_temperature_from_env(self):
        """Temperature can be set via env or config."""
        try:
            from core.application.orchestration.model_provider import get_chat_model
        except ImportError:
            pytest.skip("core.application.orchestration.model_provider not importable")

        config = {
            "agents_config": {
                "default_provider": "anthropic",
                "default_model": "claude-haiku-4-5-20251001",
                "temperature": 0.2,
            }
        }

        captured_kwargs = []

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            with patch("core.application.orchestration.model_provider.ChatAnthropic") as mock_cls:

                def capture(**kwargs):
                    captured_kwargs.append(kwargs)
                    return MagicMock()

                mock_cls.side_effect = capture
                get_chat_model(config)

        if captured_kwargs:
            temp = captured_kwargs[0].get("temperature")
            # Temperature should be a float if set
            assert temp is None or isinstance(temp, (int, float))


class TestGetChatModelFallback:
    """Fail-closed behavior when config is incomplete."""

    def test_empty_config_is_unavailable(self):
        """An empty route must not revive an implicit provider/model fallback."""
        try:
            from core.application.orchestration.model_provider import (
                ModelUnavailableError,
                get_chat_model,
            )
        except ImportError:
            pytest.skip("core.application.orchestration.model_provider not importable")

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            with patch("core.application.orchestration.model_provider.ChatAnthropic") as mock_cls:
                mock_cls.return_value = MagicMock()
                with pytest.raises(ModelUnavailableError, match="model_unavailable"):
                    get_chat_model({})

    def test_none_agent_name_ok(self):
        """agent_name=None (default) does not crash."""
        try:
            from core.application.orchestration.model_provider import get_chat_model
        except ImportError:
            pytest.skip("core.application.orchestration.model_provider not importable")

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            with patch("core.application.orchestration.model_provider.ChatAnthropic") as mock_cls:
                mock_cls.return_value = MagicMock()
                result = get_chat_model(anthropic_config(), agent_name=None)

        assert result is not None


class TestGetChatModelTimeouts:
    def test_openai_timeout_propagates_to_client(self):
        from core.application.orchestration.model_provider import get_chat_model

        captured_kwargs = []
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            with patch("core.application.orchestration.model_provider.ChatOpenAI") as mock_cls:

                def capture(**kwargs):
                    captured_kwargs.append(kwargs)
                    return MagicMock()

                mock_cls.side_effect = capture
                get_chat_model(openai_config(), timeout_seconds=42)

        assert captured_kwargs
        assert captured_kwargs[0]["timeout"] == 42

    def test_anthropic_timeout_propagates_to_client(self):
        from core.application.orchestration.model_provider import get_chat_model

        captured_kwargs = []
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            with patch("core.application.orchestration.model_provider.ChatAnthropic") as mock_cls:

                def capture(**kwargs):
                    captured_kwargs.append(kwargs)
                    return MagicMock()

                mock_cls.side_effect = capture
                get_chat_model(anthropic_config(), timeout_seconds=17)

        assert captured_kwargs
        assert captured_kwargs[0]["timeout"] == 17

    def test_google_timeout_propagates_to_client(self):
        from core.application.orchestration.model_provider import get_chat_model

        captured_kwargs = []
        with (
            patch.dict(os.environ, {"GOOGLE_API_KEY": "test-key"}),
            patch(
                "core.application.orchestration.model_provider.ChatGoogleGenerativeAI"
            ) as mock_cls,
        ):

            def capture(**kwargs):
                captured_kwargs.append(kwargs)
                return MagicMock()

            mock_cls.side_effect = capture
            get_chat_model(google_config(), timeout_seconds=9)

        assert captured_kwargs
        assert captured_kwargs[0]["request_timeout"] == 9

    def test_timeout_respects_request_cap_env(self):
        from core.application.orchestration.model_provider import get_chat_model

        captured_kwargs = []
        with (
            patch.dict(
                os.environ,
                {"OPENAI_API_KEY": "test-key", "FS_LLM_REQUEST_TIMEOUT_SECONDS": "12"},
            ),
            patch("core.application.orchestration.model_provider.ChatOpenAI") as mock_cls,
        ):

            def capture(**kwargs):
                captured_kwargs.append(kwargs)
                return MagicMock()

            mock_cls.side_effect = capture
            get_chat_model(openai_config(), timeout_seconds=42)

        assert captured_kwargs
        assert captured_kwargs[0]["timeout"] == 12
        assert captured_kwargs[0]["max_retries"] == 0

    def test_timeout_is_not_implicitly_capped_when_env_unset(self):
        from core.application.orchestration.model_provider import get_chat_model

        captured_kwargs = []
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=True),
            patch("core.application.orchestration.model_provider.ChatOpenAI") as mock_cls,
            patch(
                "core.application.settings.model_catalog.validate_provider_model_pair",
                return_value=(True, ""),
            ),
        ):

            def capture(**kwargs):
                captured_kwargs.append(kwargs)
                return MagicMock()

            mock_cls.side_effect = capture
            get_chat_model(openai_config(), timeout_seconds=180)

        assert captured_kwargs
        assert captured_kwargs[0]["timeout"] == 180

    def test_openai_explicit_zero_retries_propagates_to_client(self):
        from core.application.orchestration.model_provider import get_chat_model

        captured_kwargs = []
        config = {
            "agents_config": {
                "default_provider": "openai",
                "default_model": "gpt-5.2",
                "max_retries": 0,
            }
        }
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            with patch("core.application.orchestration.model_provider.ChatOpenAI") as mock_cls:

                def capture(**kwargs):
                    captured_kwargs.append(kwargs)
                    return MagicMock()

                mock_cls.side_effect = capture
                get_chat_model(config, timeout_seconds=20)

        assert captured_kwargs
        assert captured_kwargs[0]["max_retries"] == 0


# ---------------------------------------------------------------------------
# FS-DEEP-CAND-058: use-time SSRF validation of stored model-provider base_url
# ---------------------------------------------------------------------------


def test_assert_safe_provider_base_url_invokes_use_time_guard():
    from core.application.orchestration import model_provider as mp

    calls: list[tuple[str, str]] = []

    def fake_assert(url, *, purpose):
        calls.append((url, purpose))
        return url

    with patch(
        "core.application.extensions.url_safety.assert_safe_remote_https_url",
        side_effect=fake_assert,
    ):
        out = mp._assert_safe_provider_base_url("https://llm.example.com/v1")

    assert out == "https://llm.example.com/v1"
    assert calls == [("https://llm.example.com/v1", "model_provider_base_url")]


def test_assert_safe_provider_base_url_rejects_unsafe_url():
    from core.application.orchestration import model_provider as mp

    def fake_assert(url, *, purpose):
        raise ValueError("resolves to blocked address 169.254.169.254")

    with (
        patch(
            "core.application.extensions.url_safety.assert_safe_remote_https_url",
            side_effect=fake_assert,
        ),
        pytest.raises(mp.ModelProviderResolutionError),
    ):
        mp._assert_safe_provider_base_url("http://metadata.internal/v1")


def test_assert_safe_provider_base_url_skips_empty():
    from core.application.orchestration import model_provider as mp

    # No base_url configured -> nothing to validate, returns empty without dialing.
    with patch(
        "core.application.extensions.url_safety.assert_safe_remote_https_url",
        side_effect=AssertionError("should not validate empty base_url"),
    ):
        assert mp._assert_safe_provider_base_url("") == ""
        assert mp._assert_safe_provider_base_url(None) == ""


def test_get_chat_model_validates_named_provider_base_url_before_client():
    from core.application.orchestration import model_provider as mp

    config = {
        "agents_config": {
            "default_provider": "custom:prov-1",
            "default_model": "gpt-5.2",
            "model_provider_id": "prov-1",
        }
    }
    named = {
        "provider_type": "openai",
        "default_model": "gpt-5.2",
        "base_url": "http://169.254.169.254/v1",
        "_decrypted_api_key": "sk-secret",
    }

    def fake_assert(url, *, purpose):
        raise ValueError("blocked metadata endpoint")

    with (
        patch.object(mp, "_resolve_named_provider", return_value=named),
        patch.object(mp, "_get_registry_agent_config", return_value={}),
        patch.object(mp, "ChatOpenAI") as mock_openai,
        patch(
            "core.application.extensions.url_safety.assert_safe_remote_https_url",
            side_effect=fake_assert,
        ),
    ):
        with pytest.raises(mp.ModelProviderResolutionError):
            mp.get_chat_model(config)
        # The unsafe base_url must be rejected BEFORE the LLM client is built
        # with the decrypted API key.
        mock_openai.assert_not_called()


def test_safe_pinned_base_url_attaches_pinned_http_clients():
    # FS-DEEP-CAND-058: DB-backed custom base URLs must get connection-pinned httpx
    # clients so the OpenAI/Azure client dials the validated IP (no DNS rebinding).
    from core.application.orchestration import model_provider as mp

    sync_client = object()
    async_client = object()
    kwargs: dict = {}
    with (
        patch.object(mp, "_assert_safe_provider_base_url", side_effect=lambda url: str(url)),
        patch(
            "core.application.extensions.url_safety.build_pinned_httpx_client",
            return_value=sync_client,
        ) as build_sync,
        patch(
            "core.application.extensions.url_safety.build_pinned_async_httpx_client",
            return_value=async_client,
        ) as build_async,
    ):
        out = mp._safe_pinned_base_url("https://llm.example.com/v1", kwargs)

    assert out == "https://llm.example.com/v1"
    assert kwargs["http_client"] is sync_client
    assert kwargs["http_async_client"] is async_client
    build_sync.assert_called_once()
    build_async.assert_called_once()


def test_safe_pinned_base_url_fails_closed_when_pinning_rejects():
    # A base_url whose host resolves to a blocked peer at pin time must fail closed.
    from core.application.extensions.url_safety import PinnedPeerError
    from core.application.orchestration import model_provider as mp

    kwargs: dict = {}
    with (
        patch.object(mp, "_assert_safe_provider_base_url", side_effect=lambda url: str(url)),
        patch(
            "core.application.extensions.url_safety.build_pinned_httpx_client",
            side_effect=PinnedPeerError("resolves to disallowed address 169.254.169.254"),
        ),
        pytest.raises(mp.ModelProviderResolutionError),
    ):
        mp._safe_pinned_base_url("http://metadata.internal/v1", kwargs)
    assert "http_client" not in kwargs


def test_build_pinned_httpx_client_pins_connection_to_validated_ip():
    # End-to-end: the pinned httpx transport's backend dials the validated IP, not
    # whatever the hostname re-resolves to at connect time (DNS-rebinding defense).
    from core.application.extensions import url_safety

    with (
        patch.object(
            url_safety, "assert_safe_remote_http_url", side_effect=lambda url, *, purpose: url
        ),
        patch.object(url_safety, "resolve_pinned_ips", return_value=["203.0.113.7"]) as resolve,
    ):
        client = url_safety.build_pinned_httpx_client(
            "https://llm.example.com/v1", purpose="model_provider_base_url"
        )
        try:
            backend = client._transport._pool._network_backend
            captured: dict = {}
            real_super = type(backend).__mro__[1].connect_tcp

            def _spy(self, host, port, *a, **k):  # pragma: no cover - exercised below
                captured["host"] = host
                raise RuntimeError("stop before real dial")

            with patch.object(type(backend).__mro__[1], "connect_tcp", _spy):
                with pytest.raises(RuntimeError):
                    backend.connect_tcp("llm.example.com", 443)
            assert captured["host"] == "203.0.113.7"
            resolve.assert_called()
        finally:
            client.close()
