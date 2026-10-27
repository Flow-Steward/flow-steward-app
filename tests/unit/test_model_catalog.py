"""Tests for core.application.settings.model_catalog — catalog, validation, source detection."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from unittest import mock

import pytest

from core.application.settings.llm_verification_policy import (
    model_verification_is_fresh,
)
from core.application.settings.model_catalog import (
    BUILTIN_RELEASE_VERIFIED_MODELS,
    PROVIDER_CATALOG,
    validate_provider_model_pair_catalog_only,
)


def test_custom_provider_verification_rejects_future_timestamp():
    now = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    assert not model_verification_is_fresh(
        {
            "verification_status": "passed",
            "verified_at": now + timedelta(seconds=1),
        },
        now=now,
    )


# ---------------------------------------------------------------------------
# Catalog structure
# ---------------------------------------------------------------------------


class TestCatalogStructure:
    def test_retired_executor_families_are_absent_from_the_real_catalog(self):
        retired = {"co" + "dex_cli", "clau" + "de_cli"}

        assert retired.isdisjoint(PROVIDER_CATALOG)
        assert {"openai", "anthropic"} <= set(PROVIDER_CATALOG)

    def test_release_allowlist_contains_only_live_verified_openai_routes(self):
        expected_routes = {"openai": frozenset({"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"})}
        actual_routes = BUILTIN_RELEASE_VERIFIED_MODELS
        assert actual_routes == expected_routes

    def test_provider_families_registered(self):
        assert len(PROVIDER_CATALOG) == 3
        expected = {
            "anthropic",
            "openai",
            "google",
        }
        assert set(PROVIDER_CATALOG.keys()) == expected

    def test_google_candidates_match_current_ga_replacements(self):
        assert [model.model_id for model in PROVIDER_CATALOG["google"].models] == [
            "gemini-3.6-flash",
            "gemini-3.5-flash-lite",
        ]

    def test_each_family_has_models(self):
        for fid, family in PROVIDER_CATALOG.items():
            assert len(family.models) > 0, f"{fid} has no models"

    def test_unverified_direct_compatibility_families_are_removed(self):
        assert {"qwen", "deepseek", "xai"}.isdisjoint(PROVIDER_CATALOG)

    def test_anthropic_catalog_contains_opus_5(self):
        assert "claude-opus-5" in {model.model_id for model in PROVIDER_CATALOG["anthropic"].models}


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# validate_provider_model_pair_catalog_only — P1 fix
# ---------------------------------------------------------------------------


class TestCatalogOnlyValidation:
    def test_both_empty_is_valid(self):
        valid, err = validate_provider_model_pair_catalog_only("", "")
        assert valid is True
        assert err == ""

    def test_model_without_provider_is_invalid(self):
        """P1 fix: model set but provider empty must be rejected."""
        valid, err = validate_provider_model_pair_catalog_only("", "claude-sonnet-4-6")
        assert valid is False
        assert "without a provider" in err

    def test_provider_without_model_is_invalid(self):
        """P1 fix: provider set but model empty must be rejected."""
        valid, err = validate_provider_model_pair_catalog_only("anthropic", "")
        assert valid is False
        assert "without a model" in err

    def test_unreleased_candidate_pair_is_rejected(self):
        valid, err = validate_provider_model_pair_catalog_only("anthropic", "claude-sonnet-5")
        assert valid is False
        assert "release capability suite" in err

    def test_mismatched_pair(self):
        """Anthropic + gpt-5 must be rejected."""
        valid, err = validate_provider_model_pair_catalog_only("anthropic", "gpt-5")
        assert valid is False
        assert "not available for provider" in err or "does not belong" in err

    def test_google_with_openai_model_rejected(self):
        valid, _err = validate_provider_model_pair_catalog_only("google", "gpt-4o")
        assert valid is False

    def test_unknown_provider_is_rejected(self):
        """Legacy IDs must not be accepted without an account-scoped route."""
        valid, err = validate_provider_model_pair_catalog_only("custom_provider", "some-model")
        assert valid is False
        assert "Unknown or unavailable" in err


# ---------------------------------------------------------------------------
# Full validation (with source/visibility checks)
# ---------------------------------------------------------------------------


class TestFullValidation:
    def test_environment_key_does_not_trigger_catalog_queries(self):
        from core.application.settings import model_catalog

        with (
            mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}),
            mock.patch.object(
                model_catalog.model_provider_catalog_repository,
                "list_routable_model_providers",
                return_value=[],
            ),
        ):
            valid, _error = model_catalog.validate_provider_model_pair(
                "openai", "gpt-5.6-sol", conn=object()
            )

        assert valid is False

    def test_provider_without_sources_rejected(self):
        from core.application.settings.model_catalog import validate_provider_model_pair

        # Google without GOOGLE_API_KEY env var and no DB conn
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GOOGLE_API_KEY", None)
            valid, err = validate_provider_model_pair("google", "gemini-3.6-flash", conn=None)
            assert valid is False
            assert "release capability suite" in err

    def test_provider_with_env_source_remains_unpublished_without_release_evidence(self):
        from core.application.settings.model_catalog import validate_provider_model_pair

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            valid, err = validate_provider_model_pair("anthropic", "claude-sonnet-5", conn=None)
            assert valid is False
            assert "release capability suite" in err

    def test_empty_provider_with_model_rejected_full(self):
        """P1 fix: full validation also rejects orphan model."""
        from core.application.settings.model_catalog import validate_provider_model_pair

        valid, err = validate_provider_model_pair("", "gpt-5", conn=None)
        assert valid is False
        assert "without a provider" in err


# ---------------------------------------------------------------------------
# Source detection
# ---------------------------------------------------------------------------


class TestSourceDetection:
    def test_llm_api_key_environment_variables_never_create_a_provider_source(self):
        from core.application.settings.model_catalog import detect_provider_sources

        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            sources = detect_provider_sources(conn=None)
            assert "openai" not in sources

    def test_no_env_no_source(self):
        from core.application.settings.model_catalog import detect_provider_sources

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GOOGLE_API_KEY", None)
            sources = detect_provider_sources(conn=None)
            # Google should not be in sources (no env, no CLI, no DB)
            assert "google" not in sources


# ---------------------------------------------------------------------------
# Effective options
# ---------------------------------------------------------------------------


class TestEffectiveOptions:
    def test_catalog_projection_propagates_provider_store_failure(self):
        from core.application.settings import model_catalog

        with (
            mock.patch.object(
                model_catalog.model_provider_catalog_repository,
                "list_routable_model_providers",
                side_effect=RuntimeError("database unavailable"),
            ),
            pytest.raises(model_catalog.ModelCatalogUnavailableError),
        ):
            model_catalog.get_effective_options_structured(conn=object())

    def test_only_available_providers_returned(self):
        from core.application.settings.model_catalog import (
            ModelCatalogUnavailableError,
            get_effective_options,
        )

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}, clear=False):
            os.environ.pop("OPENAI_API_KEY", None)
            os.environ.pop("GOOGLE_API_KEY", None)
            os.environ.pop("DASHSCOPE_API_KEY", None)
            with pytest.raises(ModelCatalogUnavailableError):
                get_effective_options(conn=None)

    def test_structured_output(self):
        from core.application.settings.model_catalog import (
            ModelCatalogUnavailableError,
            get_effective_options_structured,
        )

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}, clear=False):
            os.environ.pop("OPENAI_API_KEY", None)
            with pytest.raises(ModelCatalogUnavailableError):
                get_effective_options_structured(conn=None)

    def test_verified_custom_provider_projects_only_its_selected_model(self):
        """A custom credential must never open a static provider family."""
        from datetime import UTC, datetime

        from core.application.settings import model_catalog

        provider = {
            "id": "provider-123",
            "name": "Account OpenAI-compatible gateway",
            "provider_type": "openai_compatible",
            "default_model": "gateway-model-7",
            "enabled": True,
            "verification_status": "passed",
            "verified_capabilities_jsonb": ["chat", "tools", "structured_output"],
            "verified_at": datetime.now(UTC),
            "lifecycle": "active",
        }
        with (
            mock.patch.object(model_catalog, "_load_visibility_policy", return_value={}),
            mock.patch.object(model_catalog, "_extension_provider_groups", return_value=[]),
            mock.patch.object(
                model_catalog.model_provider_catalog_repository,
                "list_routable_model_providers",
                return_value=[provider],
            ),
        ):
            result = model_catalog.get_effective_options_structured(conn=object())

        provider_id = "custom:provider-123"
        assert result["provider_models"][provider_id] == [
            {
                "model_id": "gateway-model-7",
                "label": "gateway-model-7",
                "capabilities": ["chat", "structured_output", "tools"],
            }
        ]
        assert result["provider_options"][-1]["label"] == "Account OpenAI-compatible gateway"
        assert all(item["model_id"] != "gpt-5.2" for item in result["provider_models"][provider_id])

    def test_unverified_custom_provider_is_not_routable(self):
        from core.application.settings import model_catalog

        provider = {
            "id": "provider-123",
            "name": "Unverified gateway",
            "provider_type": "openai_compatible",
            "default_model": "gateway-model-7",
            "enabled": True,
            "verification_status": "stale",
            "verified_capabilities_jsonb": [],
            "lifecycle": "active",
        }
        with (
            mock.patch.object(model_catalog, "_load_visibility_policy", return_value={}),
            mock.patch.object(model_catalog, "_extension_provider_groups", return_value=[]),
            mock.patch.object(
                model_catalog.model_provider_catalog_repository,
                "list_routable_model_providers",
                return_value=[provider],
            ),
        ):
            result = model_catalog.get_effective_options_structured(conn=object())

        assert "custom:provider-123" not in result["provider_models"]

    def test_custom_provider_projection_requests_database_fresh_routes_only(self):
        from core.application.settings import model_catalog

        with (
            mock.patch.object(model_catalog, "_load_visibility_policy", return_value={}),
            mock.patch.object(model_catalog, "_extension_provider_groups", return_value=[]),
            mock.patch.object(
                model_catalog.model_provider_catalog_repository,
                "list_routable_model_providers",
                return_value=[],
            ) as list_providers,
        ):
            result = model_catalog.get_effective_options_structured(conn=object())

        assert "custom:provider-123" not in result["provider_models"]
        assert list_providers.call_count >= 1
        assert all(
            call.kwargs == {"account_id": "acc_default"} for call in list_providers.call_args_list
        )

    def test_extension_model_catalog_uses_one_batched_model_query(self):
        from core.application.settings import (
            llm_provider_extension_repository as extension_store,
        )
        from core.application.settings import model_catalog

        sources = [
            {"id": "source-1", "provider_key": "llmext_one", "display_label": "One"},
            {"id": "source-2", "provider_key": "llmext_two", "display_label": "Two"},
        ]
        models = [
            {
                "provider_source_id": "source-1",
                "model_id": "model-1",
                "display_label": "Model 1",
                "capabilities_jsonb": ["chat", "tools", "structured_output"],
            },
            {
                "provider_source_id": "source-2",
                "model_id": "model-2",
                "display_label": "Model 2",
                "capabilities_jsonb": ["chat", "tools", "structured_output"],
            },
        ]
        with (
            mock.patch.object(extension_store, "list_enabled_sources", return_value=sources),
            mock.patch.object(
                extension_store,
                "list_enabled_structured_models_for_sources",
                return_value=models,
                create=True,
            ) as batch,
            mock.patch.object(
                extension_store,
                "list_enabled_structured_models",
                side_effect=AssertionError("per-source N+1 query must not run"),
            ),
        ):
            groups = model_catalog._extension_provider_groups("acc-1", object())

        assert [group["provider_key"] for group in groups] == ["llmext_one", "llmext_two"]
        batch.assert_called_once_with(
            mock.ANY,
            account_id="acc-1",
            provider_source_ids=["source-1", "source-2"],
        )

    def test_extension_model_catalog_separates_declared_and_verified_capabilities(self):
        from core.application.settings import (
            llm_provider_extension_repository as extension_store,
        )
        from core.application.settings import model_catalog

        source = {"id": "source-1", "provider_key": "llmext_one", "display_label": "One"}
        model = {
            "provider_source_id": "source-1",
            "model_id": "model-1",
            "display_label": "Model One",
            "declared_capabilities_jsonb": [
                "chat",
                "tools",
                "structured_output",
                "prompt_caching",
                "vision",
            ],
            "verified_capabilities_jsonb": ["chat", "tools", "structured_output"],
        }
        with (
            mock.patch.object(extension_store, "list_enabled_sources", return_value=[source]),
            mock.patch.object(
                extension_store,
                "list_enabled_structured_models_for_sources",
                return_value=[model],
            ),
        ):
            groups = model_catalog._extension_provider_groups("acc-1", object())

        projected = groups[0]["models"][0]
        assert projected["declared_capabilities"] == [
            "chat",
            "prompt_caching",
            "structured_output",
            "tools",
            "vision",
        ]
        assert projected["verified_capabilities"] == [
            "chat",
            "structured_output",
            "tools",
        ]
        assert projected["capabilities"] == [
            "chat",
            "prompt_caching",
            "structured_output",
            "tools",
        ]


# ---------------------------------------------------------------------------
# resolve_model_selection_context (integration with serve_workflow_helpers)
# ---------------------------------------------------------------------------


class TestResolveModelSelectionContext:
    def test_returns_all_expected_keys(self):
        from core.application.settings.model_catalog import resolve_model_selection_context

        with (
            mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}),
            mock.patch(
                "core.application.settings.model_catalog.get_effective_options_for_selection_context",
                return_value=([("openai", "OpenAI")], ["gpt-5.6-sol"]),
            ),
            mock.patch(
                "core.application.settings.model_catalog.get_effective_options_structured",
                return_value={"provider_options": [], "provider_models": {}, "all_models": []},
            ),
        ):
            ctx = resolve_model_selection_context("gpt-4.1-nano", conn=object())
            assert "default_agent_models" in ctx
            assert "available_providers" in ctx
            assert "available_models" in ctx
            assert "providers_with_models" in ctx

    def test_resolver_failure_does_not_fall_back_to_static_catalog(self):
        from core.application.settings import model_catalog

        with (
            mock.patch.object(
                model_catalog,
                "get_effective_options_for_selection_context",
                side_effect=model_catalog.ModelCatalogUnavailableError("store unavailable"),
            ),
            pytest.raises(model_catalog.ModelCatalogUnavailableError),
        ):
            model_catalog.resolve_model_selection_context("gpt-4.1-nano", conn=object())

    def test_providers_with_models_has_correct_structure(self):
        from core.application.settings.model_catalog import resolve_model_selection_context

        with (
            mock.patch(
                "core.application.settings.model_catalog.get_effective_options_for_selection_context",
                return_value=([], []),
            ),
            mock.patch(
                "core.application.settings.model_catalog.get_effective_options_structured",
                return_value={"provider_options": [], "provider_models": {}, "all_models": []},
            ),
        ):
            ctx = resolve_model_selection_context("gpt-4.1-nano", conn=object())
            pwm = ctx["providers_with_models"]
            assert "provider_models" in pwm
            assert "all_models" in pwm
