from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.e2e.test_rbac_ui_api_mcp_matrix import (
    _assert_mcp_denied,
    _create_pat,
    _scoped_mcp_arguments,
)


class _Response:
    def __init__(self, body: dict, status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code

    def json(self) -> dict:
        return self._body


def test_create_pat_extracts_the_one_time_nested_raw_token() -> None:
    session = SimpleNamespace(
        cookies={},
        post=lambda *_args, **_kwargs: _Response(
            {"token": {"id": "pat-1", "raw_token": "fs_pat_secret"}},
            status_code=201,
        ),
    )

    assert _create_pat(session) == "fs_pat_secret"


@pytest.mark.parametrize(
    "error_payload",
    [
        {"ok": False, "error": {"code": "not_found", "message": "Project missing"}},
        {"ok": False, "error": "runtime initialization failed"},
    ],
)
def test_mcp_denial_assertion_rejects_downstream_business_errors(error_payload: dict) -> None:
    response = _Response(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "isError": True,
                "content": [{"type": "text", "text": __import__("json").dumps(error_payload)}],
            },
        }
    )

    with pytest.raises(AssertionError, match="authorization denial"):
        _assert_mcp_denied(response, tool_name="workflow_trigger")


def test_mcp_denial_assertion_accepts_only_the_forbidden_tool_envelope() -> None:
    response = _Response(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "isError": True,
                "content": [
                    {
                        "type": "text",
                        "text": '{"ok":false,"error":{"code":"forbidden","message":"Viewer role required"}}',
                    }
                ],
            },
        }
    )

    _assert_mcp_denied(response, tool_name="workflow_trigger")


@pytest.mark.parametrize(
    ("tool_name", "expected"),
    [
        ("job_cancel", {"job_id": "job-owned"}),
        (
            "job_approval_decide",
            {"thread_id": "thread-owned", "approval_id": "approval-owned"},
        ),
    ],
)
def test_mcp_mutation_arguments_use_valid_owned_job_fixtures(
    tool_name: str,
    expected: dict[str, str],
) -> None:
    arguments = _scoped_mcp_arguments(
        tool_name=tool_name,
        arguments={
            "job_id": "synthetic-job",
            "thread_id": "synthetic-thread",
            "approval_id": "synthetic-approval",
        },
        project_name="Owned project",
        owned_job_fixture={
            "job_id": "job-owned",
            "thread_id": "thread-owned",
            "approval_id": "approval-owned",
        },
    )

    assert {key: arguments[key] for key in expected} == expected
