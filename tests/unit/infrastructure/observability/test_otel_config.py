from __future__ import annotations

import pytest

from core.infrastructure.observability.otel_config import (
    DEFAULT_BATCH_MAX_QUEUE_SIZE,
    MAX_QUEUE_SIZE_LIMIT,
    load_telemetry_settings,
)


def test_export_is_off_without_any_setting() -> None:
    settings = load_telemetry_settings({})

    assert settings.any_enabled is False
    assert settings.traces.disabled_reason == ""


def test_otlp_without_endpoint_never_falls_back_to_localhost() -> None:
    settings = load_telemetry_settings({"OTEL_TRACES_EXPORTER": "otlp"})

    assert settings.traces.enabled is False
    assert settings.traces.disabled_reason == "missing_endpoint"


def test_each_signal_is_enabled_separately() -> None:
    settings = load_telemetry_settings(
        {
            "OTEL_TRACES_EXPORTER": "otlp",
            "OTEL_METRICS_EXPORTER": "none",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318/",
        }
    )

    assert settings.traces.enabled is True
    assert settings.traces.endpoint == "http://collector:4318/v1/traces"
    assert settings.metrics.enabled is False
    assert settings.logs.enabled is False


def test_signal_endpoint_overrides_the_base_endpoint() -> None:
    settings = load_telemetry_settings(
        {
            "OTEL_LOGS_EXPORTER": "otlp",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318",
            "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "https://logs.example/ingest",
        }
    )

    assert settings.logs.endpoint == "https://logs.example/ingest"


@pytest.mark.parametrize(
    ("env", "reason"),
    [
        ({"OTEL_TRACES_EXPORTER": "zipkin"}, "unsupported_exporter"),
        (
            {"OTEL_TRACES_EXPORTER": "otlp", "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc"},
            "unsupported_protocol",
        ),
        (
            {"OTEL_TRACES_EXPORTER": "otlp", "OTEL_EXPORTER_OTLP_ENDPOINT": "collector:4318"},
            "invalid_endpoint",
        ),
        (
            {
                "OTEL_TRACES_EXPORTER": "otlp",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4318",
                "OTEL_SDK_DISABLED": "true",
            },
            "sdk_disabled",
        ),
    ],
)
def test_unusable_settings_keep_the_signal_off_with_a_reason(env: dict, reason: str) -> None:
    settings = load_telemetry_settings(env)

    assert settings.traces.enabled is False
    assert settings.traces.disabled_reason == reason


def test_timeout_follows_the_specification_milliseconds() -> None:
    assert load_telemetry_settings({"OTEL_EXPORTER_OTLP_TIMEOUT": "2500"}).timeout_seconds == 2.5
    assert load_telemetry_settings({}).timeout_seconds == 10.0


def test_buffers_stay_bounded_and_malformed_values_fall_back() -> None:
    settings = load_telemetry_settings(
        {
            "OTEL_BSP_MAX_QUEUE_SIZE": "999999999",
            "OTEL_BSP_MAX_EXPORT_BATCH_SIZE": "100000000",
            "OTEL_BLRP_MAX_QUEUE_SIZE": "not-a-number",
        }
    )

    assert settings.span_batch.max_queue_size == MAX_QUEUE_SIZE_LIMIT
    assert settings.span_batch.max_export_batch_size == MAX_QUEUE_SIZE_LIMIT
    assert settings.log_batch.max_queue_size == DEFAULT_BATCH_MAX_QUEUE_SIZE


def test_sampler_is_parent_based_and_ratio_is_clamped() -> None:
    settings = load_telemetry_settings(
        {"OTEL_TRACES_SAMPLER": "parentbased_traceidratio", "OTEL_TRACES_SAMPLER_ARG": "7"}
    )
    assert settings.sampler == "parentbased_traceidratio"
    assert settings.sampler_ratio == 1.0

    assert load_telemetry_settings({"OTEL_TRACES_SAMPLER": "always_on"}).sampler == (
        "parentbased_always_on"
    )
