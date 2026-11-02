from __future__ import annotations

from core.infrastructure.observability.otel_config import load_telemetry_settings
from core.infrastructure.observability.otel_environment import (
    SEMCONV_STABILITY_MODE,
    SUPPORTED_ENV_NAMES,
    apply_environment_policy,
)


def test_policy_removes_unsupported_otel_names_and_keeps_others() -> None:
    env = {
        "OTEL_SERVICE_NAME": "operator-name",
        "OTEL_EXPERIMENTAL_RESOURCE_DETECTORS": "process,host",
        "OTEL_PYTHON_LOG_CORRELATION": "true",
        "OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED": "true",
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "SPAN_AND_EVENT",
        "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST": ".*",
        "OTEL_PROPAGATORS": "b3,baggage",
        "OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED": "true",
        # The exporter loads a session factory from this entry-point name.
        "OTEL_PYTHON_EXPORTER_OTLP_HTTP_TRACES_CREDENTIAL_PROVIDER": "operator_plugin",
        "OTEL_TRACES_EXPORTER": "otlp",
        "SENTRY_DSN": "",
    }

    report = apply_environment_policy(env)

    assert "OTEL_SERVICE_NAME" in report.removed
    assert not any(name.startswith("OTEL_") and name not in SUPPORTED_ENV_NAMES for name in env)
    assert env["OTEL_TRACES_EXPORTER"] == "otlp"
    assert env["SENTRY_DSN"] == ""
    assert env["OTEL_SEMCONV_STABILITY_OPT_IN"] == SEMCONV_STABILITY_MODE


def test_policy_pins_http_conventions_against_operator_dup_mode() -> None:
    env = {"OTEL_SEMCONV_STABILITY_OPT_IN": "http/dup,database"}

    report = apply_environment_policy(env)

    assert env["OTEL_SEMCONV_STABILITY_OPT_IN"] == "http"
    assert report.rewritten == ("OTEL_SEMCONV_STABILITY_OPT_IN",)


def test_policy_keeps_only_the_deployment_environment_resource_attribute() -> None:
    env = {
        "OTEL_RESOURCE_ATTRIBUTES": (
            "service.name=evil,host.name=box,deployment.environment.name=prod%20eu,"
            "process.command_line=secret"
        )
    }

    apply_environment_policy(env)

    assert env["OTEL_RESOURCE_ATTRIBUTES"] == "deployment.environment.name=prod%20eu"
    assert load_telemetry_settings(env).resource_attributes == {
        "deployment.environment.name": "prod eu"
    }


def test_policy_drops_resource_attributes_with_nothing_allowed() -> None:
    env = {"OTEL_RESOURCE_ATTRIBUTES": "service.name=evil"}

    report = apply_environment_policy(env)

    assert "OTEL_RESOURCE_ATTRIBUTES" not in env
    assert "OTEL_RESOURCE_ATTRIBUTES" in report.removed


def test_policy_is_idempotent() -> None:
    env = {"OTEL_SERVICE_NAME": "x", "OTEL_RESOURCE_ATTRIBUTES": "deployment.environment.name=a"}
    apply_environment_policy(env)
    snapshot = dict(env)

    report = apply_environment_policy(env)

    assert env == snapshot
    assert report.removed == ()
    assert report.rewritten == ()


def test_policy_report_names_never_values() -> None:
    env = {"OTEL_EXPORTER_OTLP_TRACES_HEADERS_X": "authorization=Bearer s3cr3t"}

    report = apply_environment_policy(env)

    assert "s3cr3t" not in repr(report)
