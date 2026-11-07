"""CLI usage fidelity without widening credential disclosure."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from core.application.management.job_public_projection import job_public_summary_projection
from core.application.management.query_models_jobs import (
    JobDetailContractView,
    JobStatusView,
    JobUsageSummaryView,
)
from core.application.security.credential_redaction import RedactionContext, redact, redact_output
from core.entrypoints.cli.platform.click_adapter import build_click_app
from core.entrypoints.cli.platform.execution import _public_result_payload
from core.entrypoints.cli.platform.service_registry import ServiceRegistry


@pytest.mark.parametrize("counts", [(0, 0, 0, 0), (11, 7, 18, 5)])
def test_jobs_status_cli_preserves_public_usage_counts(counts: tuple[int, int, int, int]) -> None:
    usage = JobUsageSummaryView(
        tokens_prompt=counts[0],
        tokens_completion=counts[1],
        tokens_total=counts[2],
        llm_calls=counts[3],
    )
    status = JobStatusView(
        id="job-usage",
        status="completed",
        task_summary="Usage fidelity",
        usage=usage,
        invocation={"token": 123456, "api_key": 123456, "tokens_total": 123456},
    )

    class Query:
        def get_job_status(self, job_id: str, *, scope: object) -> JobStatusView:
            assert job_id == "job-usage"
            assert scope.actor_id == "flow-steward-cli"
            return status

    runtime = SimpleNamespace(job_query_service=Query())
    result = CliRunner().invoke(
        build_click_app(ServiceRegistry(runtime), runtime),
        ["jobs", "status", "job-usage", "--json"],
    )
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output)
    assert envelope["ok"] is True
    assert envelope["result"]["usage"] == usage.model_dump(mode="json")
    assert envelope["result"]["usage"] == job_public_summary_projection(status)["usage"]
    assert envelope["result"]["invocation"] == {
        "token": "[REDACTED]",
        "api_key": "[REDACTED]",
        "tokens_total": "[REDACTED]",
    }


def test_job_usage_exception_cannot_reveal_explicit_secret_or_nested_secret_parent() -> None:
    class StatusWithSecretUsage(JobStatusView):
        secret_usage: JobUsageSummaryView

    status = StatusWithSecretUsage(
        id="job-usage",
        status="completed",
        task_summary="Counter boundaries",
        usage=JobUsageSummaryView(tokens_prompt=11, tokens_total=18, tokens_completion=5),
        secret_usage=JobUsageSummaryView(tokens_total=123456),
        invocation={"usage": {"tokens_total": 123456}},
    )
    result = _public_result_payload(
        status,
        context=RedactionContext(known_secrets=("11",)),
        secret_fields=frozenset({"tokens_total"}),
    )
    assert result["usage"]["tokens_prompt"] == "[REDACTED]"
    assert result["usage"]["tokens_total"] == "[REDACTED]"
    assert result["usage"]["tokens_completion"] == 5
    assert result["secret_usage"] == "[REDACTED]"
    assert result["invocation"] == {"usage": {"tokens_total": "[REDACTED]"}}


def test_generic_redaction_does_not_whitelist_usage_names() -> None:
    payload = {"usage": {"tokens_total": 123456, "tokens_completion": 654321}, "token": 987654}
    expected = {
        "usage": {"tokens_total": "[REDACTED]", "tokens_completion": "[REDACTED]"},
        "token": "[REDACTED]",
    }
    assert redact_output(payload) == expected
    assert redact(payload) == expected


@pytest.mark.parametrize("value", ["123456", True, 123456.0, -1, None, {"value": 123456}, [123456]])
def test_declared_usage_path_does_not_accept_non_counter_values(value: object) -> None:
    result = redact_output(
        {"usage": {"tokens_total": value}},
        public_usage_counter_paths=frozenset({("usage", "tokens_total")}),
    )
    assert result == {"usage": {"tokens_total": "[REDACTED]"}}


@pytest.mark.parametrize("counts", [(0, 0, 0, 0), (11, 7, 18, 5)])
@pytest.mark.parametrize("include_details", [False, True])
def test_jobs_show_cli_preserves_typed_usage_without_exposing_extra_credentials(
    counts: tuple[int, int, int, int], include_details: bool
) -> None:
    usage = JobUsageSummaryView(
        tokens_prompt=counts[0],
        tokens_completion=counts[1],
        tokens_total=counts[2],
        llm_calls=counts[3],
        provider_label="example-provider",
        provider_token=123456,
        provider_usage={"tokens_total": 654321},
    )
    status = JobStatusView(
        id="job-usage",
        status="completed",
        task_summary="Usage fidelity",
        usage=usage,
        current_agent="",
        invocation={"tokens_total": 123456},
    )

    class Query:
        def get_job_status(self, job_id: str, *, scope: object) -> JobStatusView:
            assert job_id == "job-usage"
            assert scope.actor_id == "flow-steward-cli"
            return status

        def get_job_detail_contract(
            self, job_id: str, *, scope: object, user_role: str
        ) -> JobDetailContractView:
            assert job_id == "job-usage"
            assert scope.actor_id == "flow-steward-cli"
            assert user_role == "operator"
            return JobDetailContractView.model_validate(
                {
                    **status.model_dump(mode="json"),
                    "usage_breakdown_raw": {"tokens_completion": 654321},
                    "extension_payload": {"usage": {"tokens_total": 654321}},
                }
            )

    runtime = SimpleNamespace(job_query_service=Query())
    args = ["jobs", "show", "job-usage", "--json"]
    if include_details:
        args.append("--include-details")
    result = CliRunner().invoke(build_click_app(ServiceRegistry(runtime), runtime), args)
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output)
    assert envelope["ok"] is True
    assert envelope["command"] == "jobs.show"
    payload = envelope["result"]
    assert payload["id"] == "job-usage"
    if include_details:
        assert payload["usage"]["provider_label"] == "example-provider"
        assert payload["usage"]["provider_token"] == "[REDACTED]"
        assert payload["usage"]["provider_usage"] == {"tokens_total": "[REDACTED]"}
        assert payload["invocation"] == {"tokens_total": "[REDACTED]"}
        assert payload["extension_payload"] == {"usage": {"tokens_total": "[REDACTED]"}}
        assert payload["usage_breakdown_raw"] == {"tokens_completion": "[REDACTED]"}
    else:
        assert "provider_label" not in payload["usage"]
        assert "provider_token" not in payload["usage"]
        assert "provider_usage" not in payload["usage"]
        assert "invocation" not in payload
        assert "extension_payload" not in payload
    expected = dict(
        zip(
            ("tokens_prompt", "tokens_completion", "tokens_total", "llm_calls"),
            counts,
            strict=True,
        )
    )
    assert {key: payload["usage"][key] for key in expected} == expected
    assert all(type(payload["usage"][key]) is int for key in expected)


def test_job_detail_usage_metadata_cannot_override_known_or_explicit_secrets() -> None:
    detail = JobDetailContractView.model_validate(
        {
            "id": "job-usage",
            "usage": {
                "tokens_prompt": 11,
                "tokens_total": 18,
                "tokens_completion": 5,
                "secret_payload": {"tokens_total": 123456},
            },
        }
    )
    result = _public_result_payload(
        detail,
        context=RedactionContext(known_secrets=("11",)),
        secret_fields=frozenset({"tokens_total"}),
    )
    assert result["usage"]["tokens_prompt"] == "[REDACTED]"
    assert result["usage"]["tokens_total"] == "[REDACTED]"
    assert result["usage"]["secret_payload"] == "[REDACTED]"
    assert result["usage"]["tokens_completion"] == 5


def test_jobs_show_details_does_not_invent_usage_when_summary_is_absent() -> None:
    class Query:
        def get_job_detail_contract(
            self, job_id: str, *, scope: object, user_role: str
        ) -> JobDetailContractView:
            assert job_id == "job-usage"
            assert scope.actor_id == "flow-steward-cli"
            assert user_role == "operator"
            return JobDetailContractView(
                id=job_id,
                usage_breakdown_raw={"tokens_completion": 5, "calls": 2},
            )

    runtime = SimpleNamespace(job_query_service=Query())
    result = CliRunner().invoke(
        build_click_app(ServiceRegistry(runtime), runtime),
        ["jobs", "show", "job-usage", "--include-details", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["result"]
    assert "usage" not in payload
    assert payload["usage_breakdown_raw"] == {
        "tokens_completion": "[REDACTED]",
        "calls": 2,
    }


@pytest.mark.parametrize("usage", [None, {}, {"provider_label": "example-provider"}])
def test_jobs_show_details_preserves_supplied_usage_shape(usage: object) -> None:
    class Query:
        def get_job_detail_contract(
            self, job_id: str, *, scope: object, user_role: str
        ) -> JobDetailContractView:
            assert job_id == "job-usage"
            assert scope.actor_id == "flow-steward-cli"
            assert user_role == "operator"
            return JobDetailContractView.model_validate({"id": job_id, "usage": usage})

    runtime = SimpleNamespace(job_query_service=Query())
    result = CliRunner().invoke(
        build_click_app(ServiceRegistry(runtime), runtime),
        ["jobs", "show", "job-usage", "--include-details", "--json"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["result"]["usage"] == usage


def test_optional_typed_usage_does_not_trust_dict_list_or_ambiguous_union() -> None:
    class UntrustedUsage(JobStatusView):
        optional_dict: dict[str, object] | None = None
        optional_list: list[JobUsageSummaryView] | None = None
        ambiguous: JobUsageSummaryView | dict[str, object] | None = None
        secret_usage: JobUsageSummaryView | None = None

    status = UntrustedUsage(
        id="job-usage",
        status="completed",
        task_summary="Optional boundaries",
        optional_dict={"tokens_total": 123456},
        optional_list=[JobUsageSummaryView(tokens_total=123456)],
        ambiguous={"tokens_total": 123456},
        secret_usage=JobUsageSummaryView(tokens_total=123456),
    )
    payload = _public_result_payload(status, context=RedactionContext(), secret_fields=frozenset())
    assert payload["optional_dict"]["tokens_total"] == "[REDACTED]"
    assert payload["optional_list"][0]["tokens_total"] == "[REDACTED]"
    assert payload["ambiguous"]["tokens_total"] == "[REDACTED]"
    assert payload["secret_usage"] == "[REDACTED]"
