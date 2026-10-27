from __future__ import annotations

from unittest.mock import MagicMock

from core.infrastructure.db.ingress_runtime import (
    build_ingress_dedupe_key,
    format_task_ingress_error,
    normalize_ingress_provider_event,
    normalize_notification_ingress_response,
    normalize_task_ingress_response,
    resolve_ingress_project_id,
)


def test_build_ingress_dedupe_key_prefers_source_id_and_event_key() -> None:
    dedupe = build_ingress_dedupe_key(
        provider_key="fixture_events",
        source_id="cb-1",
        event_key="fixture_events.callback_query.approve",
        payload={"job_id": 55},
    )
    assert dedupe == "fixture_events:fixture_events.callback_query.approve:cb-1"


def test_normalize_ingress_provider_event_flattens_nested_payload() -> None:
    normalized = normalize_ingress_provider_event(
        {
            "event_type": "external.comment.received",
            "payload": {
                "payload": {
                    "job_id": 55,
                    "source_id": "cb-1",
                    "approved": True,
                },
                "summary": "Approval callback",
            },
        },
        provider_key="fixture_events",
        default_source="webhook:fixture_events",
    )

    assert normalized["event_type"] == "external.comment.received"
    assert normalized["job_id"] == "55"
    assert normalized["source"] == "webhook:fixture_events"
    assert normalized["source_id"] == "cb-1"
    assert normalized["payload"]["job_id"] == 55
    assert normalized["payload"]["approved"] is True
    assert normalized["payload"]["event_key"] == "external.comment.received"
    assert normalized["payload"]["dedupe_key"] == "fixture_events:external.comment.received:cb-1"


def test_normalize_notification_ingress_response_filters_undeclared_events() -> None:
    normalized = normalize_notification_ingress_response(
        {
            "ok": True,
            "result": {"provider": "fixture_events"},
            "events": [
                {
                    "event_type": "external.comment.received",
                    "payload": {"job_id": 55, "source_id": "cb-1"},
                },
                {"event_type": "secret.leaked", "payload": {"job_id": 55}},
            ],
        },
        provider_key="fixture_events",
        declared_event_types=["external.comment.received"],
        default_source="webhook:fixture_events",
    )

    assert normalized["ok"] is True
    assert normalized["result"]["provider"] == "fixture_events"
    assert len(normalized["events"]) == 1
    assert normalized["events"][0]["event_type"] == "external.comment.received"
    assert normalized["events"][0]["job_id"] == "55"


def test_resolve_ingress_project_id_falls_back_to_job_lookup() -> None:
    bl = MagicMock()
    bl.get_job.return_value = {"id": 55, "project_id": 11}

    project_id = resolve_ingress_project_id(
        ingress_result={"source": "fixture_events.callback_query"},
        ingress_events=[
            {
                "event_type": "external.comment.received",
                "payload": {"job_id": 55, "approved": True},
            }
        ],
        request_payload={},
        query_params={},
        bl=bl,
    )

    assert project_id == "11"
    bl.get_job.assert_called_once_with("55")


def test_normalize_task_ingress_response_maps_legacy_error_code() -> None:
    normalized = normalize_task_ingress_response(
        {
            "ok": False,
            "error": "missing_api_token",
        },
        default_error_message="task ingress failed",
    )

    assert normalized["ok"] is False
    assert normalized["errors"][0]["code"] == "missing_credentials"
    assert "missing_api_token" in normalized["errors"][0]["message"]


def test_format_task_ingress_error_wraps_canonical_code_prefix() -> None:
    text = format_task_ingress_error(
        {"code": "timeout", "message": "Provider request timed out"},
        fallback="task ingress failed",
    )
    assert text == "[timeout] Provider request timed out"


def test_sample_tasks_api_error_code_no_longer_aliased_to_upstream_error() -> None:
    """sample_tasks_api_error is a source-specific code removed from generic ingress aliases.

    Extensions returning this code must use the canonical 'upstream_error' instead.
    After the cleanup, unrecognised codes fall through to 'internal_error'.
    """
    from core.infrastructure.db.ingress_runtime import TASK_INGRESS_ERROR_CODE_ALIASES

    assert "sample_tasks_api_error" not in TASK_INGRESS_ERROR_CODE_ALIASES, (
        "sample_tasks_api_error must not appear in generic ingress error code aliases"
    )


def test_generic_api_error_alias_still_maps_to_upstream_error() -> None:
    """The generic 'api_error' alias (provider-agnostic) must remain."""
    from core.infrastructure.db.ingress_runtime import TASK_INGRESS_ERROR_CODE_ALIASES

    assert TASK_INGRESS_ERROR_CODE_ALIASES.get("api_error") == "upstream_error"
