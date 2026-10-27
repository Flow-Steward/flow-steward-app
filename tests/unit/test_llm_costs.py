from datetime import date

from core.application.observability.llm_costs import estimate_llm_cost_usd


def test_verified_openai_model_prices_are_exact() -> None:
    assert estimate_llm_cost_usd("gpt-5.6-sol", 1_000, 1_000, provider="openai") == 0.035
    assert estimate_llm_cost_usd("gpt-5.6-terra", 1_000, 1_000, provider="openai") == 0.0175
    assert estimate_llm_cost_usd("gpt-5.6-luna", 1_000, 1_000, provider="openai") == 0.007


def test_price_requires_exact_provider_and_model_identity() -> None:
    assert estimate_llm_cost_usd("gpt-5.6-sol", 1_000, 1_000, provider="custom:openai") is None
    assert estimate_llm_cost_usd("gpt-5.6-sol-preview", 1_000, 1_000, provider="openai") is None


def test_anthropic_prompt_cache_accounting_uses_exact_current_model_price() -> None:
    assert (
        estimate_llm_cost_usd(
            "claude-fable-5",
            0,
            1_000,
            cache_creation_tokens=1_000,
            cache_read_tokens=1_000,
            provider="anthropic",
        )
        == 0.0635
    )


def test_opus_5_uses_the_current_anthropic_price() -> None:
    assert (
        estimate_llm_cost_usd(
            "claude-opus-5", 1_000, 1_000, provider="anthropic"
        )
        == 0.030
    )


def test_unknown_model_does_not_inherit_price_by_prefix() -> None:
    assert estimate_llm_cost_usd("gpt-4o-unverified-route", 100, 100) is None


def test_sol_long_context_multiplier_applies_only_above_272k_input_tokens() -> None:
    assert (
        estimate_llm_cost_usd(
            "gpt-5.6-sol",
            272_000,
            1_000,
            provider="openai",
        )
        == 1.39
    )
    assert (
        estimate_llm_cost_usd(
            "gpt-5.6-sol",
            300_000,
            1_000,
            provider="openai",
        )
        == 3.045
    )


def test_sonnet_5_pricing_switches_on_documented_effective_date() -> None:
    assert (
        estimate_llm_cost_usd(
            "claude-sonnet-5",
            1_000,
            1_000,
            provider="anthropic",
            priced_at=date(2026, 8, 31),
        )
        == 0.012
    )
    assert (
        estimate_llm_cost_usd(
            "claude-sonnet-5",
            1_000,
            1_000,
            provider="anthropic",
            priced_at=date(2026, 9, 1),
        )
        == 0.018
    )
