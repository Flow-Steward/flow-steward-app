from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime


@dataclass(frozen=True)
class _PricingRule:
    prompt_per_1k: float
    completion_per_1k: float
    effective_from: date = date.min
    effective_until: date | None = None
    long_context_threshold: int | None = None
    long_input_multiplier: float = 1.0
    long_output_multiplier: float = 1.0

    def applies_on(self, target: date) -> bool:
        return self.effective_from <= target and (
            self.effective_until is None or target <= self.effective_until
        )


_COST_RULES: dict[tuple[str, str], tuple[_PricingRule, ...]] = {
    # Exact direct-provider IDs only. Checked against official pricing on
    # 2026-07-18. Effective dates and long-context thresholds are request rules,
    # not model-ID aliases or prefix matches.
    ("anthropic", "claude-fable-5"): (_PricingRule(0.010, 0.050),),
    ("anthropic", "claude-opus-5"): (_PricingRule(0.005, 0.025),),
    ("anthropic", "claude-opus-4-8"): (_PricingRule(0.005, 0.025),),
    ("anthropic", "claude-opus-4-7"): (_PricingRule(0.005, 0.025),),
    ("anthropic", "claude-opus-4-6"): (_PricingRule(0.005, 0.025),),
    ("anthropic", "claude-sonnet-5"): (
        _PricingRule(0.002, 0.010, effective_until=date(2026, 8, 31)),
        _PricingRule(0.003, 0.015, effective_from=date(2026, 9, 1)),
    ),
    ("anthropic", "claude-sonnet-4-6"): (_PricingRule(0.003, 0.015),),
    ("anthropic", "claude-haiku-4-5-20251001"): (_PricingRule(0.001, 0.005),),
    (
        "openai",
        "gpt-5.6-sol",
    ): (
        _PricingRule(
            0.005,
            0.030,
            long_context_threshold=272_000,
            long_input_multiplier=2.0,
            long_output_multiplier=1.5,
        ),
    ),
    ("openai", "gpt-5.6-terra"): (_PricingRule(0.0025, 0.015),),
    ("openai", "gpt-5.6-luna"): (_PricingRule(0.001, 0.006),),
}


def _pricing_date(value: date | datetime | None) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.now(UTC).date()


def estimate_llm_cost_usd(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
    *,
    provider: str = "",
    priced_at: date | datetime | None = None,
) -> float | None:
    normalized_provider = str(provider or "").strip()
    normalized_model = str(model or "").strip()
    if not normalized_provider or not normalized_model:
        return None
    rules = _COST_RULES.get((normalized_provider, normalized_model), ())
    target_date = _pricing_date(priced_at)
    pricing = next((rule for rule in rules if rule.applies_on(target_date)), None)
    if pricing is None:
        return None

    prompt_count = max(int(prompt_tokens or 0), 0)
    cache_create_count = max(int(cache_creation_tokens or 0), 0)
    cache_read_count = max(int(cache_read_tokens or 0), 0)
    completion_count = max(int(completion_tokens or 0), 0)
    total_input = prompt_count + cache_create_count + cache_read_count
    long_context = (
        pricing.long_context_threshold is not None and total_input > pricing.long_context_threshold
    )
    input_multiplier = pricing.long_input_multiplier if long_context else 1.0
    output_multiplier = pricing.long_output_multiplier if long_context else 1.0

    prompt_cost = prompt_count * pricing.prompt_per_1k * input_multiplier
    cache_create_cost = cache_create_count * pricing.prompt_per_1k * 1.25 * input_multiplier
    cache_read_cost = cache_read_count * pricing.prompt_per_1k * 0.1 * input_multiplier
    completion_cost = completion_count * pricing.completion_per_1k * output_multiplier
    return round((prompt_cost + cache_create_cost + cache_read_cost + completion_cost) / 1000, 6)
