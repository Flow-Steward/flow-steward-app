from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.application.observability.llm_usage import (
    DERIVED,
    REPORTED,
    TokenUsage,
    usage_from_extension_payload,
    usage_from_llm_result,
    usage_from_mapping,
    usage_from_message,
)


def _result(llm_output=None, *messages) -> SimpleNamespace:
    return SimpleNamespace(
        llm_output=llm_output,
        generations=[[SimpleNamespace(message=message) for message in messages]],
    )


def test_missing_counts_stay_unknown_and_zero_stays_zero() -> None:
    usage = usage_from_mapping({"prompt_tokens": 0, "completion_tokens": None})

    assert usage == TokenUsage(input_tokens=0, provenance={"input_tokens": REPORTED})
    assert usage.output_tokens is None
    assert usage.total_tokens is None
    assert usage.to_langchain_usage_metadata() is None


def test_no_counts_at_all_is_no_usage() -> None:
    assert usage_from_mapping({}) is None
    assert usage_from_mapping({"model": "x"}) is None
    assert usage_from_extension_payload(None) is None
    assert usage_from_llm_result(_result({}, SimpleNamespace(response_metadata={}))) is None


def test_reported_zero_is_not_replaced_by_a_later_alias() -> None:
    usage = usage_from_mapping({"prompt_tokens": 0, "input_tokens": 9, "output_tokens": 1})

    assert usage.input_tokens == 0


@pytest.mark.parametrize("value", [True, -1, "12", float("nan")])
def test_malformed_counts_are_unknown(value) -> None:
    assert usage_from_mapping({"input_tokens": value, "output_tokens": 2}).input_tokens is None


def test_count_preserves_large_integer_and_infinity_behavior() -> None:
    from core.application.observability.llm_usage import _count

    huge = 10**1000
    assert _count(huge) == huge
    with pytest.raises(OverflowError):
        _count(float("inf"))
    assert _count(float("-inf")) is None


def test_total_is_derived_only_from_known_components() -> None:
    usage = usage_from_mapping({"input_tokens": 7, "output_tokens": 3})

    assert usage.total_tokens == 10
    assert usage.provenance["total_tokens"] == DERIVED
    assert usage.provenance["input_tokens"] == REPORTED


def test_openai_cached_tokens_are_a_subset_of_the_input() -> None:
    usage = usage_from_mapping(
        {
            "prompt_tokens": 1_000,
            "completion_tokens": 10,
            "total_tokens": 1_010,
            "prompt_tokens_details": {"cached_tokens": 800},
        }
    )

    assert (usage.input_tokens, usage.cache_read_tokens, usage.total_tokens) == (1_000, 800, 1_010)
    assert usage.uncached_input_tokens == 200


def test_raw_anthropic_cache_buckets_are_added_to_the_input() -> None:
    usage = usage_from_mapping(
        {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_creation_input_tokens": 30,
            "cache_read_input_tokens": 50,
        }
    )

    assert usage.input_tokens == 180
    assert usage.provenance["input_tokens"] == DERIVED
    assert usage.total_tokens == 200
    assert usage.uncached_input_tokens == 100


def test_langchain_anthropic_message_is_not_added_twice() -> None:
    # langchain-anthropic already adds the cache buckets into input_tokens.
    message = SimpleNamespace(
        usage_metadata={
            "input_tokens": 180,
            "output_tokens": 20,
            "total_tokens": 200,
            "input_token_details": {
                "cache_read": 50,
                "cache_creation": 0,
                "ephemeral_5m_input_tokens": 30,
                "ephemeral_1h_input_tokens": 0,
            },
        },
        response_metadata={"model_provider": "anthropic"},
    )

    usage = usage_from_message(message)

    assert (usage.input_tokens, usage.total_tokens) == (180, 200)
    assert (usage.cache_read_tokens, usage.cache_write_tokens) == (50, 30)
    assert usage.uncached_input_tokens == 100


def test_chat_anthropic_result_reads_the_raw_usage_once() -> None:
    raw = {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_creation_input_tokens": 30,
        "cache_read_input_tokens": 50,
    }
    message = SimpleNamespace(
        usage_metadata={
            "input_tokens": 180,
            "output_tokens": 20,
            "total_tokens": 200,
            "input_token_details": {"cache_read": 50, "cache_creation": 30},
        },
        response_metadata={},
    )

    from_output = usage_from_llm_result(_result({"usage": raw}, message))
    from_message = usage_from_llm_result(_result({}, message))

    assert from_output.input_tokens == from_message.input_tokens == 180
    assert from_output.total_tokens == from_message.total_tokens == 200


def test_extension_cache_counts_are_a_subset_of_the_input() -> None:
    usage = usage_from_extension_payload(
        {
            "input_tokens": 100,
            "output_tokens": 5,
            "cache_creation_input_tokens": 10,
            "cache_read_input_tokens": 60,
        }
    )

    assert (usage.input_tokens, usage.total_tokens) == (100, 105)
    assert usage.uncached_input_tokens == 30


def test_extension_partial_usage_keeps_unknown_counts_through_the_result() -> None:
    usage = usage_from_extension_payload({"input_tokens": 12})
    assert usage.to_langchain_usage_metadata() is None

    restored = usage_from_llm_result(_result({"flow_steward_usage": usage.as_dict()}))

    assert restored == usage
    assert restored.output_tokens is None


def test_langchain_metadata_carries_only_known_cache_buckets() -> None:
    usage = usage_from_extension_payload(
        {"input_tokens": 4, "output_tokens": 1, "cache_read_input_tokens": 0}
    )

    assert usage.to_langchain_usage_metadata() == {
        "input_tokens": 4,
        "output_tokens": 1,
        "total_tokens": 5,
        "input_token_details": {"cache_read": 0},
    }


def _callback():
    from core.infrastructure.db.platform_logger import PlatformLoggerCallback

    fake_logger = MagicMock()
    return PlatformLoggerCallback(fake_logger, project_id=1, job_id=7), fake_logger


def _openai_result(prompt: int, completion: int) -> SimpleNamespace:
    return SimpleNamespace(
        llm_output={
            "model_name": "gpt-5.6-sol",
            "token_usage": {"prompt_tokens": prompt, "completion_tokens": completion},
        },
        generations=[],
    )


def test_a_repeated_callback_for_one_run_is_recorded_once() -> None:
    callback, fake_logger = _callback()

    callback.on_llm_end(response=_openai_result(10, 2), run_id="run-1")
    callback.on_llm_end(response=_openai_result(10, 2), run_id="run-1")

    assert fake_logger.log_action.call_count == 1


def test_a_retry_is_a_new_run_and_is_recorded_separately() -> None:
    callback, fake_logger = _callback()

    callback.on_llm_end(response=_openai_result(10, 2), run_id="run-1")
    callback.on_llm_end(response=_openai_result(11, 3), run_id="run-2")

    assert [c.kwargs["tokens_prompt"] for c in fake_logger.log_action.call_args_list] == [10, 11]


def test_callback_records_unknown_usage_as_unknown_with_no_estimated_cost() -> None:
    callback, fake_logger = _callback()
    callback.on_chat_model_start(
        {}, [[]], run_id="run-u", metadata={"flow_steward_provider": "openai"}
    )

    callback.on_llm_end(
        response=SimpleNamespace(
            llm_output={"model_name": "gpt-5.6-sol", "token_usage": {"prompt_tokens": 40}},
            generations=[],
        ),
        run_id="run-u",
    )

    logged = fake_logger.log_action.call_args.kwargs
    assert logged["tokens_prompt"] == 40
    assert logged["tokens_completion"] is None
    assert logged["tokens_total"] is None
    assert logged["cost_usd"] is None


def test_callback_keeps_a_reported_zero() -> None:
    callback, fake_logger = _callback()

    callback.on_llm_end(response=_openai_result(5, 0), run_id="run-z")

    logged = fake_logger.log_action.call_args.kwargs
    assert logged["tokens_completion"] == 0
    assert logged["tokens_total"] == 5


def test_callback_prices_extension_cache_as_a_subset_of_the_input() -> None:
    callback, fake_logger = _callback()
    callback.on_chat_model_start(
        {},
        [[]],
        run_id="run-x",
        metadata={
            "flow_steward_provider_policy": "anthropic",
            "flow_steward_model": "claude-opus-5",
        },
    )
    usage = usage_from_extension_payload(
        {"input_tokens": 1_000, "output_tokens": 0, "cache_read_input_tokens": 1_000}
    )

    callback.on_llm_end(
        response=SimpleNamespace(
            llm_output={"provider_key": "llmext_x", "flow_steward_usage": usage.as_dict()},
            generations=[],
        ),
        run_id="run-x",
    )

    logged = fake_logger.log_action.call_args.kwargs
    assert logged["tokens_prompt"] == 1_000
    assert logged["tokens_total"] == 1_000
    # 1000 cache reads at 0.1x of $0.005/1k; the old additive path billed 2000 tokens.
    assert logged["cost_usd"] == 0.0005
