from __future__ import annotations

import asyncio
import json
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError
from starlette.responses import Response
from starlette.routing import Match

from core.application.management.errors import ServiceValidationError
from core.entrypoints.api import approval_decision_transport
from core.infrastructure.runtime import serve_request_runtime


def test_platform_kit_static_assets_revalidate_after_rebuild() -> None:
    middleware = serve_request_runtime.build_static_asset_cache_middleware(
        static_cache_max_age_seconds=300,
    )

    async def call_next(_request):
        return Response()

    response = asyncio.run(
        middleware(
            SimpleNamespace(url=SimpleNamespace(path="/static/platform-kit/index.js")),
            call_next,
        )
    )

    assert response.headers["Cache-Control"] == "no-cache"


def test_other_static_assets_keep_configured_cache_policy() -> None:
    middleware = serve_request_runtime.build_static_asset_cache_middleware(
        static_cache_max_age_seconds=300,
    )

    async def call_next(_request):
        return Response()

    response = asyncio.run(
        middleware(
            SimpleNamespace(url=SimpleNamespace(path="/static/images/logo.svg")),
            call_next,
        )
    )

    assert response.headers["Cache-Control"] == (
        "public, max-age=300, stale-while-revalidate=86400"
    )


class _FakeRequest:
    def __init__(
        self,
        headers: dict[str, str] | None = None,
        payload=None,
        *,
        auth_source: str = "",
        query_params: dict[str, str] | None = None,
        raw_body: bytes | None = None,
        chunk_size: int = 0,
    ) -> None:
        self.headers = headers or {}
        self._payload = payload
        self.query_params = query_params or {}
        self.state = SimpleNamespace(auth_source=auth_source, auth_user=None)
        self._raw_body = raw_body if raw_body is not None else json.dumps(payload).encode("utf-8")
        self._chunk_size = max(0, int(chunk_size))

    async def json(self):
        return self._payload

    async def stream(self):
        chunk_size = self._chunk_size or len(self._raw_body) or 1
        for offset in range(0, len(self._raw_body), chunk_size):
            yield self._raw_body[offset : offset + chunk_size]


class _FakePrincipal:
    def __init__(self, *, is_active: bool, username: str = "alice") -> None:
        self.is_active = is_active
        self.username = username

    def model_dump(self) -> dict:
        return {"username": self.username, "is_active": self.is_active}


class _FakeWebSocket:
    def __init__(self, *, token: str = "", cookie_token: str = "") -> None:
        self.query_params = {"token": token} if token else {}
        self.cookies = {"session": cookie_token} if cookie_token else {}


def _error_payload(**kwargs):
    return dict(kwargs)


def _service_validation_message(exc: ValidationError) -> str:
    return str(exc)


def test_service_error_response_keeps_extra_context_and_code() -> None:
    exc = type(
        "Exc",
        (),
        {
            "status_code": 409,
            "payload": {
                "message": "Conflict happened",
                "details": {"field": "name"},
                "correlation_id": "corr-1",
                "code": "conflict",
                "extra_flag": True,
            },
        },
    )()

    response = serve_request_runtime.service_error_response(
        exc,
        None,
        error_payload=_error_payload,
        request_correlation_id=lambda _request, fallback="": fallback or "generated",
        error_code_for_status=lambda status: f"status-{status}",
        err_request_failed="Request failed",
    )

    assert response.status_code == 409
    assert b'"code":"conflict"' in response.body
    assert b'"context":{"extra_flag":true}' in response.body


def test_service_error_response_serializes_uuid_extra_context() -> None:
    trace_id = uuid.UUID("58eeaeb3-a9c1-4c51-b292-14eb86899d86")
    exc = type(
        "Exc",
        (),
        {
            "status_code": 409,
            "payload": {
                "message": "Conflict happened",
                "details": {"trace_id": trace_id},
                "correlation_id": "corr-uuid",
                "code": "conflict",
                "extra_trace_id": trace_id,
            },
        },
    )()

    response = serve_request_runtime.service_error_response(
        exc,
        None,
        error_payload=_error_payload,
        request_correlation_id=lambda _request, fallback="": fallback or "generated",
        error_code_for_status=lambda status: f"status-{status}",
        err_request_failed="Request failed",
    )

    assert response.status_code == 409
    assert str(trace_id).encode() in response.body


def test_approval_decision_from_request_rejects_non_object_json() -> None:
    _raises_input_172_1 = approval_decision_transport.approval_decision_from_request(
        _FakeRequest(headers={"content-type": "application/json"}, payload=["bad"])
    )
    with pytest.raises(ServiceValidationError, match="expected an object payload"):
        asyncio.run(_raises_input_172_1)


def test_approval_decision_rejects_declared_body_above_limit() -> None:
    limit = approval_decision_transport.MAX_APPROVAL_DECISION_BODY_BYTES
    request = _FakeRequest(
        headers={
            "content-type": "application/json",
            "content-length": str(limit + 1),
        },
        payload={"approval_id": "gate-1", "approved": True},
    )

    _raises_input_190_1 = approval_decision_transport.approval_decision_from_request(request)
    with pytest.raises(ServiceValidationError) as captured:
        asyncio.run(_raises_input_190_1)

    assert captured.value.status_code == 413
    assert captured.value.payload["code"] == "approval_payload_too_large"


def test_approval_decision_rejects_chunked_body_above_limit_while_streaming() -> None:
    limit = approval_decision_transport.MAX_APPROVAL_DECISION_BODY_BYTES
    request = _FakeRequest(
        headers={"content-type": "application/json"},
        payload=None,
        raw_body=b"x" * (limit + 1),
        chunk_size=4096,
    )

    _raises_input_206_1 = approval_decision_transport.approval_decision_from_request(request)
    with pytest.raises(ServiceValidationError) as captured:
        asyncio.run(_raises_input_206_1)

    assert captured.value.status_code == 413
    assert captured.value.payload["code"] == "approval_payload_too_large"


def test_token_approval_requires_an_explicit_decision_field() -> None:
    request = _FakeRequest(
        headers={"content-type": "application/json"},
        payload={"approval_id": "gate-1", "comment": "looks good"},
        auth_source="machine",
    )
    _raises_input_219_1 = approval_decision_transport.approval_decision_from_request(request)
    with pytest.raises(ServiceValidationError, match="explicit approval decision"):
        asyncio.run(_raises_input_219_1)


def test_token_approval_accepts_action_only_decision() -> None:
    request = _FakeRequest(
        headers={"content-type": "application/json"},
        payload={"approval_id": "gate-1", "action": "reject", "comment": "Not approved"},
        auth_source="machine",
    )
    decision = asyncio.run(approval_decision_transport.approval_decision_from_request(request))
    assert decision.action == "reject"
    assert decision.approved is False


def test_ws_authenticate_accepts_query_token_and_returns_principal_payload() -> None:
    payload = asyncio.run(
        serve_request_runtime.ws_authenticate(
            _FakeWebSocket(token="tok-1"),
            local_auth_enabled_fn=lambda: True,
            external_auth_enabled=False,
            auth_cookie_name="session",
            get_logger_only_fn=lambda: object(),
            auth_tables_available_fn=lambda _bl: True,
            get_user_by_session_token_fn=lambda _bl, token: {"token": token},
            normalize_live_principal_fn=lambda _raw, default_auth_source="": _FakePrincipal(
                is_active=True
            ),
            resolved_principal_type=_FakePrincipal,
            err_authentication_required="Auth required",
        )
    )

    assert payload == {"username": "alice", "is_active": True}


def test_compute_wait_reason_uses_status_specific_messages() -> None:
    assert (
        serve_request_runtime.compute_wait_reason(
            {"status": "pending", "queue_name": "q1"},
            {},
            retryable_interrupted_step_status="retryable_step",
        )
        == "Queued in q1"
    )
    assert (
        serve_request_runtime.compute_wait_reason(
            {"status": "retryable_step"},
            {},
            retryable_interrupted_step_status="retryable_step",
        )
        == "Interrupted in-flight step detected — manual Retry Step is required"
    )
    assert (
        serve_request_runtime.compute_wait_reason(
            {"status": "in_progress"},
            {"active_agent": "designer"},
            retryable_interrupted_step_status="retryable_step",
        )
        == "Agent 'designer' is executing"
    )


def test_compute_wait_reason_names_a_durable_wait_without_calling_it_an_approval() -> None:
    timer_job = {
        "status": "awaiting_timer",
        "meta": '{"workflow_wait": {"kind": "timer", "due_at": "2026-10-12T10:00:00+00:00"}}',
    }

    assert (
        serve_request_runtime.compute_wait_reason(
            timer_job, {}, retryable_interrupted_step_status="retryable_step"
        )
        == "Paused on a Wait step until 2026-10-12T10:00:00+00:00"
    )
    assert (
        serve_request_runtime.compute_wait_reason(
            {"status": "awaiting_timer", "meta": "not json"},
            {},
            retryable_interrupted_step_status="retryable_step",
        )
        == "Paused on a Wait step until it is due"
    )
    assert (
        serve_request_runtime.compute_wait_reason(
            {"status": "awaiting_child"}, {}, retryable_interrupted_step_status="retryable_step"
        )
        == "Waiting for a child workflow to finish"
    )


def test_request_correlation_id_prefers_headers_then_fallback_then_generator() -> None:
    request = _FakeRequest(headers={"x-request-id": "req-1"})

    assert (
        serve_request_runtime.request_correlation_id(
            request,
            fallback="fallback",
            new_id_fn=lambda: "generated",
        )
        == "req-1"
    )
    assert (
        serve_request_runtime.request_correlation_id(
            None,
            fallback="fallback",
            new_id_fn=lambda: "generated",
        )
        == "fallback"
    )
    assert (
        serve_request_runtime.request_correlation_id(
            None,
            fallback="",
            new_id_fn=lambda: "generated",
        )
        == "generated"
    )


def test_request_correlation_id_prefers_request_state_when_available() -> None:
    request = _FakeRequest(headers={"x-request-id": "req-1"})
    request.state = SimpleNamespace(correlation_id="corr-state-1")

    assert (
        serve_request_runtime.request_correlation_id(
            request,
            fallback="fallback",
            new_id_fn=lambda: "generated",
        )
        == "corr-state-1"
    )


def test_to_json_obj_parses_json_strings_and_keeps_non_json_values() -> None:
    assert serve_request_runtime.to_json_obj('{"ok": true}') == {"ok": True}
    assert serve_request_runtime.to_json_obj("plain-text") == "plain-text"


def test_error_payload_preserves_legacy_error_field_and_extra_keys() -> None:
    payload = serve_request_runtime.error_payload(
        status_code=409,
        message="Conflict happened",
        details={"field": "name"},
        correlation_id="corr-1",
        code="conflict",
        extra={"retryable": False},
        err_request_failed="Request failed",
        default_correlation_id_fn=lambda: "generated",
    )

    assert payload["error"] == "Conflict happened"
    assert payload["code"] == "conflict"
    assert payload["retryable"] is False


def test_template_json_default_handles_common_python_types() -> None:
    assert serve_request_runtime.template_json_default(Path("/tmp/demo.txt")) == "/tmp/demo.txt"
    assert serve_request_runtime.template_json_default(Decimal("1.5")) == 1.5
    assert serve_request_runtime.template_json_default(date(2025, 1, 2)) == "2025-01-02"


def test_validation_errors_without_url_strips_url_field() -> None:
    class _ValidationExc:
        @staticmethod
        def errors():
            return [{"loc": ["body", "name"], "msg": "Required", "url": "https://example.com"}]

    assert serve_request_runtime.validation_errors_without_url(
        _ValidationExc(),
        err_validation="Validation error",
    ) == [{"loc": ["body", "name"], "msg": "Required"}]


def test_validation_errors_never_echo_the_submitted_value() -> None:
    """AC17-R94: the rejected value came back to the caller in clear text.

    pydantic reports the value it rejected in `input`, and this handler serves
    every validated route, so a password that failed a length or format rule was
    answered with the password itself. Only `url` was stripped -- but the link was
    never the leak.

    Asserted on a password because that is how it was found on a live stand, and
    on a second field to show the rule is not password-specific: a handler this
    general cannot know which field holds a secret, so it echoes none of them.
    """

    class _ValidationExc:
        @staticmethod
        def errors():
            return [
                {
                    "type": "string_too_short",
                    "loc": ["body", "password"],
                    "msg": "String should have at least 12 characters",
                    "input": "hunter2-in-the-clear",
                    "url": "https://errors.pydantic.dev/2.12/v/string_too_short",
                    "ctx": {"min_length": 12},
                },
                {
                    "type": "value_error",
                    "loc": ["body", "api_token"],
                    "msg": "invalid",
                    "input": "tok-live-abcdef",
                },
            ]

    result = serve_request_runtime.validation_errors_without_url(
        _ValidationExc(), err_validation="Validation error"
    )

    serialized = json.dumps(result)
    assert "hunter2-in-the-clear" not in serialized
    assert "tok-live-abcdef" not in serialized
    assert all("input" not in item for item in result)
    assert all("url" not in item for item in result)
    # what the caller still needs: which field, why, and the constraint
    assert result[0]["loc"] == ["body", "password"]
    assert result[0]["msg"] == "String should have at least 12 characters"
    assert result[0]["ctx"] == {"min_length": 12}


def test_api_error_response_logs_and_returns_sanitized_payload() -> None:
    logger = MagicMock()

    response = serve_request_runtime.api_error_response(
        RuntimeError("boom"),
        request=None,
        status_code=500,
        fallback_message="Internal server error",
        code="internal_error",
        logger=logger,
        error_payload=_error_payload,
        request_correlation_id=lambda _request: "corr-1",
    )

    assert response.status_code == 500
    assert b'"message":"Internal server error"' in response.body
    logger.error.assert_called_once()


def test_view_payload_dict_accepts_mapping_and_model_dump_objects() -> None:
    class _Dumpable:
        @staticmethod
        def model_dump() -> dict[str, str]:
            return {"value": "ok"}

    assert serve_request_runtime.view_payload_dict({"a": 1}) == {"a": 1}
    assert serve_request_runtime.view_payload_dict(_Dumpable()) == {"value": "ok"}


def test_service_validation_message_formats_locations() -> None:
    message = serve_request_runtime.service_validation_message(
        object(),
        validation_errors_without_url_fn=lambda _exc: [
            {"loc": ["body", "name"], "msg": "Field required"},
            {"msg": "Other issue"},
        ],
    )

    assert message == "body.name: Field required; Other issue"


def test_route_endpoint_returns_matching_handler() -> None:
    handler = object()
    router = type(
        "Router",
        (),
        {
            "routes": [
                type(
                    "Route", (), {"path": "/api/auth/me", "methods": {"GET"}, "endpoint": handler}
                )()
            ]
        },
    )()

    assert serve_request_runtime.route_endpoint(router, "/api/auth/me", "get") is handler


def test_route_registered_checks_router_paths_and_methods() -> None:
    router = type(
        "Router",
        (),
        {
            "routes": [
                type("Route", (), {"path": "/api/auth/me", "methods": {"GET"}})(),
            ]
        },
    )()

    assert serve_request_runtime.route_registered(router, "/api/auth/me", "GET") is True
    assert serve_request_runtime.route_registered(router, "/api/auth/me", "POST") is False


def test_route_registered_matches_dynamic_route_paths() -> None:
    class _DynamicRoute:
        path: ClassVar[str] = "/api/auth/users/{user_id}"
        methods: ClassVar[set[str]] = {"PUT"}

        def matches(self, scope):
            if scope["method"] == "PUT" and scope["path"].startswith("/api/auth/users/"):
                return Match.FULL, dict(scope)
            return Match.NONE, dict(scope)

    router = type("Router", (), {"routes": [_DynamicRoute()]})()

    assert serve_request_runtime.route_registered(router, "/api/auth/users/7", "PUT") is True
    assert serve_request_runtime.route_registered(router, "/api/auth/users/7", "GET") is False


def test_secret_storage_config_error_detects_encryption_bootstrap_messages() -> None:
    assert (
        serve_request_runtime.secret_storage_config_error(
            RuntimeError("Encrypted secret storage requires FS_SECRET_KEY / cryptography")
        )
        is True
    )
    assert serve_request_runtime.secret_storage_config_error(RuntimeError("other")) is False


def test_build_runtime_payload_for_api_returns_fallback_on_exception() -> None:
    logger = MagicMock()

    payload = asyncio.run(
        serve_request_runtime.build_runtime_payload_for_api(
            {"id": 42},
            object(),
            build_runtime_payload=lambda _job, _bl: (_ for _ in ()).throw(RuntimeError("broken")),
            build_runtime_payload_fallback=lambda job: {"fallback_job_id": job["id"]},
            timeout_seconds=0.01,
            logger=logger,
        )
    )

    assert payload == {"fallback_job_id": 42}
    logger.warning.assert_called_once()
