from __future__ import annotations

from datetime import UTC, datetime, timedelta


def test_explicit_retry_signal_is_projected_to_one_closed_top_level_contract() -> None:
    from core.application.orchestration.retry_policy import normalize_retry_signal

    signal = normalize_retry_signal(
        {
            "failure_class": "provider",
            "retryable": True,
            "retry_after_seconds": 7,
            "definitely_no_external_effect": True,
            "external_effect_status": "failed",
            "provider_error_code": "rate_limited",
            "http_status": 429,
            "headers": {"Authorization": "secret"},
            "body": "secret-body",
        },
        operation_kind="query",
        request_semantics="safe_read",
    )

    assert signal == {
        "operation_kind": "query",
        "request_semantics": "safe_read",
        "failure_class": "provider",
        "retryable": True,
        "retry_after_seconds": 7.0,
        "definitely_no_external_effect": True,
        "external_effect_status": "failed",
        "provider_error_code": "rate_limited",
        "http_status": 429,
    }
    assert "secret" not in repr(signal)


def test_default_retry_budget_means_two_retries_after_first_attempt() -> None:
    from core.application.orchestration.retry_policy import RetryPolicy, decide_retry

    policy = RetryPolicy()
    first_retry = decide_retry(
        signal={
            "request_semantics": "safe_read",
            "failure_class": "transient",
            "retryable": True,
            "external_effect_status": "failed",
        },
        retry_number=1,
        policy=policy,
        rng=lambda: 0.0,
    )
    second_retry = decide_retry(
        signal={
            "request_semantics": "safe_read",
            "failure_class": "transient",
            "retryable": True,
            "external_effect_status": "failed",
        },
        retry_number=2,
        policy=policy,
        rng=lambda: 0.0,
    )
    exhausted = decide_retry(
        signal={
            "request_semantics": "safe_read",
            "failure_class": "transient",
            "retryable": True,
            "external_effect_status": "failed",
        },
        retry_number=3,
        policy=policy,
        rng=lambda: 0.0,
    )

    assert policy.max_retries == 2
    assert first_retry.should_retry is True
    assert first_retry.base_delay_seconds == 2.0
    assert first_retry.applied_delay_seconds == 2.0
    assert second_retry.should_retry is True
    assert second_retry.base_delay_seconds == 4.0
    assert second_retry.applied_delay_seconds == 4.0
    assert exhausted.should_retry is False
    assert exhausted.reason_code == "retry_exhausted"


def test_third_retry_uses_eight_seconds_and_jitter_is_additive() -> None:
    from core.application.orchestration.retry_policy import RetryPolicy, decide_retry

    decision = decide_retry(
        signal={
            "request_semantics": "safe_read",
            "failure_class": "provider",
            "retryable": True,
            "external_effect_status": "failed",
        },
        retry_number=3,
        policy=RetryPolicy(max_retries=3),
        rng=lambda: 1.0,
    )

    assert decision.should_retry is True
    assert decision.base_delay_seconds == 8.0
    assert decision.applied_delay_seconds == 9.6


def test_retry_after_delta_and_http_date_are_minimum_delays() -> None:
    from core.application.orchestration.retry_policy import (
        RetryPolicy,
        decide_retry,
        parse_retry_after,
    )

    now = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
    assert parse_retry_after("7", now=now) == 7.0
    assert (
        parse_retry_after(
            (now + timedelta(seconds=11)).strftime("%a, %d %b %Y %H:%M:%S GMT"),
            now=now,
        )
        == 11.0
    )
    assert parse_retry_after("-1", now=now) is None
    assert (
        parse_retry_after(
            (now - timedelta(seconds=1)).strftime("%a, %d %b %Y %H:%M:%S GMT"), now=now
        )
        is None
    )

    decision = decide_retry(
        signal={
            "request_semantics": "safe_read",
            "failure_class": "provider",
            "retryable": True,
            "retry_after_seconds": 7,
            "external_effect_status": "failed",
        },
        retry_number=1,
        policy=RetryPolicy(),
        rng=lambda: 0.0,
    )
    assert decision.applied_delay_seconds == 7.0


def test_retry_delay_over_policy_or_deadline_is_terminal_without_early_retry() -> None:
    from core.application.orchestration.retry_policy import RetryPolicy, decide_retry

    signal = {
        "request_semantics": "safe_read",
        "failure_class": "provider",
        "retryable": True,
        "retry_after_seconds": 121,
        "external_effect_status": "failed",
        "provider_error_code": "rate_limited",
    }
    over_policy = decide_retry(
        signal=signal,
        retry_number=1,
        policy=RetryPolicy(max_backoff_seconds=120),
        rng=lambda: 0.0,
    )
    over_deadline = decide_retry(
        signal={**signal, "retry_after_seconds": 7},
        retry_number=1,
        policy=RetryPolicy(max_backoff_seconds=120),
        remaining_deadline_seconds=6,
        rng=lambda: 0.0,
    )

    assert over_policy.should_retry is False
    assert over_policy.reason_code == "retry_delay_exceeds_policy"
    assert over_policy.provider_error_code == "rate_limited"
    assert over_deadline.should_retry is False
    assert over_deadline.reason_code == "retry_delay_exceeds_deadline"


def test_jittered_delay_never_exceeds_policy_cap() -> None:
    from core.application.orchestration.retry_policy import RetryPolicy, decide_retry

    decision = decide_retry(
        signal={
            "request_semantics": "safe_read",
            "failure_class": "provider",
            "retryable": True,
            "external_effect_status": "failed",
        },
        retry_number=2,
        policy=RetryPolicy(
            max_retries=2,
            initial_backoff_seconds=120,
            max_backoff_seconds=120,
        ),
        rng=lambda: 1.0,
    )

    assert decision.should_retry is True
    assert decision.base_delay_seconds == 120.0
    assert decision.applied_delay_seconds == 120.0


def test_failed_mutation_retries_only_with_definitive_no_effect_proof() -> None:
    from core.application.orchestration.retry_policy import RetryPolicy, decide_retry

    base = {
        "failure_class": "transient",
        "retryable": True,
        "external_effect_status": "failed",
    }
    read = decide_retry(
        signal={**base, "request_semantics": "safe_read"},
        retry_number=1,
        policy=RetryPolicy(),
        rng=lambda: 0.0,
    )
    unsafe_mutation = decide_retry(
        signal={**base, "request_semantics": "mutation"},
        retry_number=1,
        policy=RetryPolicy(),
        rng=lambda: 0.0,
    )
    safe_mutation = decide_retry(
        signal={
            **base,
            "request_semantics": "mutation",
            "definitely_no_external_effect": True,
        },
        retry_number=1,
        policy=RetryPolicy(),
        rng=lambda: 0.0,
    )
    ambiguous = decide_retry(
        signal={
            **base,
            "request_semantics": "mutation",
            "definitely_no_external_effect": True,
            "external_effect_status": "timeout_unknown",
        },
        retry_number=1,
        policy=RetryPolicy(),
        rng=lambda: 0.0,
    )
    missing_status = decide_retry(
        signal={
            "failure_class": "transient",
            "retryable": True,
            "request_semantics": "mutation",
            "definitely_no_external_effect": True,
        },
        retry_number=1,
        policy=RetryPolicy(),
        rng=lambda: 0.0,
    )
    unproven_reconciliation = decide_retry(
        signal={**base, "request_semantics": "reconciled"},
        retry_number=1,
        policy=RetryPolicy(),
        rng=lambda: 0.0,
    )

    assert read.should_retry is True
    assert unsafe_mutation.should_retry is False
    assert unsafe_mutation.reason_code == "mutation_retry_requires_reconciliation"
    assert safe_mutation.should_retry is True
    assert safe_mutation.reason_code == "retry_scheduled"
    assert ambiguous.should_retry is False
    assert ambiguous.reason_code == "external_effect_ambiguous"
    assert missing_status.should_retry is False
    assert missing_status.reason_code == "mutation_effect_not_definitively_failed"
    assert unproven_reconciliation.should_retry is False
    assert unproven_reconciliation.reason_code == "reconciled_retry_requires_durable_provenance"


def test_provider_retryable_false_wins_and_flat_strategy_does_not_multiply() -> None:
    from core.application.orchestration.retry_policy import RetryPolicy, decide_retry

    denied = decide_retry(
        signal={
            "request_semantics": "safe_read",
            "failure_class": "provider",
            "retryable": False,
            "external_effect_status": "failed",
        },
        retry_number=1,
        policy=RetryPolicy(),
        rng=lambda: 0.0,
    )
    flat = decide_retry(
        signal={
            "request_semantics": "safe_read",
            "failure_class": "provider",
            "retryable": True,
            "external_effect_status": "failed",
        },
        retry_number=3,
        policy=RetryPolicy(max_retries=3, strategy="flat"),
        rng=lambda: 0.0,
    )

    assert denied.should_retry is False
    assert denied.reason_code == "not_retryable"
    assert flat.base_delay_seconds == 2.0


def test_retry_telemetry_contains_only_closed_safe_fields() -> None:
    from core.application.orchestration.retry_policy import RetryPolicy, decide_retry

    decision = decide_retry(
        signal={
            "request_semantics": "safe_read",
            "failure_class": "provider",
            "retryable": True,
            "retry_after_seconds": 7,
            "external_effect_status": "failed",
            "provider_error_code": "rate_limited",
            "http_status": 429,
            "headers": {"Authorization": "secret"},
            "url": "https://secret.example/path?token=secret",
        },
        retry_number=1,
        policy=RetryPolicy(),
        rng=lambda: 0.0,
    )

    assert decision.telemetry() == {
        "attempt_number": 2,
        "max_retries": 2,
        "base_delay_seconds": 2.0,
        "applied_delay_seconds": 7.0,
        "retry_after_seconds": 7.0,
        "failure_class": "provider",
        "reason_code": "retry_scheduled",
        "provider_error_code": "rate_limited",
        "http_status": 429,
    }
    assert "secret" not in repr(decision.telemetry())
