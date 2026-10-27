"""CLI cost fidelity without widening credential disclosure."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from core.application.management.job_public_projection import job_public_summary_projection
from core.application.management.query_models_jobs import (
    JobCostSummaryView,
    JobDetailContractView,
    JobStatusView,
)
from core.application.security.credential_redaction import RedactionContext, redact, redact_output
from core.entrypoints.cli.platform.click_adapter import build_click_app
from core.entrypoints.cli.platform.execution import _public_result_payload
from core.entrypoints.cli.platform.service_registry import ServiceRegistry


@pytest.mark.parametrize("counts", [(0, 0, 0, 0), (11, 7, 18, 5)])
def test_jobs_status_cli_preserves_public_usage_counts(counts: tuple[int, int, int, int]) -> None:
    cost = JobCostSummaryView(
        tokens_prompt=counts[0],
        tokens_completion=counts[1],
        tokens_total=counts[2],
        unknown_cli_tokens=counts[3],
    )
    status = JobStatusView(
        id="job-cost",
        status="completed",
        task_summary="Cost fidelity",
        cost=cost,
        invocation={"token": 123456, "api_key": 123456, "tokens_total": 123456},
    )

    class Query:
        def get_job_status(self, job_id: str, *, scope: object) -> JobStatusView:
            assert job_id == "job-cost"
            assert scope.actor_id == "flow-steward-cli"
            return status

    runtime = SimpleNamespace(job_query_service=Query())
    result = CliRunner().invoke(
        build_click_app(ServiceRegistry(runtime), runtime),
        ["jobs", "status", "job-cost", "--json"],
    )
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output)
    assert envelope["ok"] is True
    assert envelope["result"]["cost"] == cost.model_dump(mode="json")
    assert envelope["result"]["cost"] == job_public_summary_projection(status)["cost"]
    assert envelope["result"]["invocation"] == {
        "token": "[REDACTED]",
        "api_key": "[REDACTED]",
        "tokens_total": "[REDACTED]",
    }


def test_job_usage_exception_cannot_reveal_explicit_secret_or_nested_secret_parent() -> None:
    class StatusWithSecretCost(JobStatusView):
        secret_cost: JobCostSummaryView

    status = StatusWithSecretCost(
        id="job-cost",
        status="completed",
        task_summary="Counter boundaries",
        cost=JobCostSummaryView(tokens_prompt=11, tokens_total=18, unknown_cli_tokens=5),
        secret_cost=JobCostSummaryView(tokens_total=123456),
        invocation={"cost": {"tokens_total": 123456}},
    )
    result = _public_result_payload(
        status,
        context=RedactionContext(known_secrets=("11",)),
        secret_fields=frozenset({"tokens_total"}),
    )
    assert result["cost"]["tokens_prompt"] == "[REDACTED]"
    assert result["cost"]["tokens_total"] == "[REDACTED]"
    assert result["cost"]["unknown_cli_tokens"] == 5
    assert result["secret_cost"] == "[REDACTED]"
    assert result["invocation"] == {"cost": {"tokens_total": "[REDACTED]"}}


def test_generic_redaction_does_not_whitelist_usage_names() -> None:
    payload = {"cost": {"tokens_total": 123456, "unknown_cli_tokens": 654321}, "token": 987654}
    expected = {
        "cost": {"tokens_total": "[REDACTED]", "unknown_cli_tokens": "[REDACTED]"},
        "token": "[REDACTED]",
    }
    assert redact_output(payload) == expected
    assert redact(payload) == expected


@pytest.mark.parametrize("value", ["123456", True, 123456.0, -1, None, {"value": 123456}, [123456]])
def test_declared_usage_path_does_not_accept_non_counter_values(value: object) -> None:
    result = redact_output(
        {"cost": {"tokens_total": value}},
        public_usage_counter_paths=frozenset({("cost", "tokens_total")}),
    )
    assert result == {"cost": {"tokens_total": "[REDACTED]"}}


@pytest.mark.parametrize("counts", [(0, 0, 0, 0), (11, 7, 18, 5)])
@pytest.mark.parametrize("include_details", [False, True])
def test_jobs_show_cli_preserves_typed_cost_without_exposing_extra_credentials(
    counts: tuple[int, int, int, int], include_details: bool
) -> None:
    cost = JobCostSummaryView(
        tokens_prompt=counts[0],
        tokens_completion=counts[1],
        tokens_total=counts[2],
        unknown_cli_tokens=counts[3],
        provider_label="example-provider",
        provider_token=123456,
        provider_usage={"tokens_total": 654321},
    )
    status = JobStatusView(
        id="job-cost",
        status="completed",
        task_summary="Cost fidelity",
        cost=cost,
        current_agent="",
        invocation={"tokens_total": 123456},
    )

    class Query:
        def get_job_status(self, job_id: str, *, scope: object) -> JobStatusView:
            assert job_id == "job-cost"
            assert scope.actor_id == "flow-steward-cli"
            return status

        def get_job_detail_contract(
            self, job_id: str, *, scope: object, user_role: str
        ) -> JobDetailContractView:
            assert job_id == "job-cost"
            assert scope.actor_id == "flow-steward-cli"
            assert user_role == "operator"
            return JobDetailContractView.model_validate(
                {
                    **status.model_dump(mode="json"),
                    "cost_breakdown_raw": {"unknown_cli_tokens": 654321},
                    "extension_payload": {"cost": {"tokens_total": 654321}},
                }
            )

    runtime = SimpleNamespace(job_query_service=Query())
    args = ["jobs", "show", "job-cost", "--json"]
    if include_details:
        args.append("--include-details")
    result = CliRunner().invoke(build_click_app(ServiceRegistry(runtime), runtime), args)
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output)
    assert envelope["ok"] is True
    assert envelope["command"] == "jobs.show"
    payload = envelope["result"]
    assert payload["id"] == "job-cost"
    if include_details:
        assert payload["cost"]["provider_label"] == "example-provider"
        assert payload["cost"]["provider_token"] == "[REDACTED]"
        assert payload["cost"]["provider_usage"] == {"tokens_total": "[REDACTED]"}
        assert payload["invocation"] == {"tokens_total": "[REDACTED]"}
        assert payload["extension_payload"] == {"cost": {"tokens_total": "[REDACTED]"}}
        assert payload["cost_breakdown_raw"] == {"unknown_cli_tokens": "[REDACTED]"}
    else:
        assert "provider_label" not in payload["cost"]
        assert "provider_token" not in payload["cost"]
        assert "provider_usage" not in payload["cost"]
        assert "invocation" not in payload
        assert "extension_payload" not in payload
    expected = dict(
        zip(
            ("tokens_prompt", "tokens_completion", "tokens_total", "unknown_cli_tokens"),
            counts,
            strict=True,
        )
    )
    assert {key: payload["cost"][key] for key in expected} == expected
    assert all(type(payload["cost"][key]) is int for key in expected)


def test_job_detail_cost_metadata_cannot_override_known_or_explicit_secrets() -> None:
    detail = JobDetailContractView.model_validate(
        {
            "id": "job-cost",
            "cost": {
                "tokens_prompt": 11,
                "tokens_total": 18,
                "unknown_cli_tokens": 5,
                "secret_payload": {"tokens_total": 123456},
            },
        }
    )
    result = _public_result_payload(
        detail,
        context=RedactionContext(known_secrets=("11",)),
        secret_fields=frozenset({"tokens_total"}),
    )
    assert result["cost"]["tokens_prompt"] == "[REDACTED]"
    assert result["cost"]["tokens_total"] == "[REDACTED]"
    assert result["cost"]["secret_payload"] == "[REDACTED]"
    assert result["cost"]["unknown_cli_tokens"] == 5


def test_jobs_show_details_does_not_invent_cost_when_summary_is_absent() -> None:
    class Query:
        def get_job_detail_contract(
            self, job_id: str, *, scope: object, user_role: str
        ) -> JobDetailContractView:
            assert job_id == "job-cost"
            assert scope.actor_id == "flow-steward-cli"
            assert user_role == "operator"
            return JobDetailContractView(
                id=job_id,
                cost_breakdown_raw={"unknown_cli_tokens": 5, "total_cost_usd": 0.25},
            )

    runtime = SimpleNamespace(job_query_service=Query())
    result = CliRunner().invoke(
        build_click_app(ServiceRegistry(runtime), runtime),
        ["jobs", "show", "job-cost", "--include-details", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["result"]
    assert "cost" not in payload
    assert payload["cost_breakdown_raw"] == {
        "unknown_cli_tokens": "[REDACTED]",
        "total_cost_usd": 0.25,
    }


@pytest.mark.parametrize("cost", [None, {}, {"provider_label": "example-provider"}])
def test_jobs_show_details_preserves_supplied_cost_shape(cost: object) -> None:
    class Query:
        def get_job_detail_contract(
            self, job_id: str, *, scope: object, user_role: str
        ) -> JobDetailContractView:
            assert job_id == "job-cost"
            assert scope.actor_id == "flow-steward-cli"
            assert user_role == "operator"
            return JobDetailContractView.model_validate({"id": job_id, "cost": cost})

    runtime = SimpleNamespace(job_query_service=Query())
    result = CliRunner().invoke(
        build_click_app(ServiceRegistry(runtime), runtime),
        ["jobs", "show", "job-cost", "--include-details", "--json"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["result"]["cost"] == cost


def test_optional_typed_usage_does_not_trust_dict_list_or_ambiguous_union() -> None:
    class UntrustedUsage(JobStatusView):
        optional_dict: dict[str, object] | None = None
        optional_list: list[JobCostSummaryView] | None = None
        ambiguous: JobCostSummaryView | dict[str, object] | None = None
        secret_cost: JobCostSummaryView | None = None

    status = UntrustedUsage(
        id="job-cost",
        status="completed",
        task_summary="Optional boundaries",
        optional_dict={"tokens_total": 123456},
        optional_list=[JobCostSummaryView(tokens_total=123456)],
        ambiguous={"tokens_total": 123456},
        secret_cost=JobCostSummaryView(tokens_total=123456),
    )
    payload = _public_result_payload(status, context=RedactionContext(), secret_fields=frozenset())
    assert payload["optional_dict"]["tokens_total"] == "[REDACTED]"
    assert payload["optional_list"][0]["tokens_total"] == "[REDACTED]"
    assert payload["ambiguous"]["tokens_total"] == "[REDACTED]"
    assert payload["secret_cost"] == "[REDACTED]"
