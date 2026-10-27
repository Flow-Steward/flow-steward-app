"""Immediate child links retain execution ownership without exposing job metadata."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.errors import ServiceForbiddenError
from core.application.management.query_services import JobQueryService
from core.application.management.scope import ScopeContext


def _link(child_id="child", step_id="author-hook", execution_id="execution-1"):
    return {
        "child_job_id": child_id,
        "child_workflow_id": "client",
        "step_id": step_id,
        "step_execution_id": execution_id,
        "invocation_key": f"parent:{execution_id}",
        "input_fingerprint": "private-input-fingerprint",
        "step_agent": "internal-agent",
    }


def _service(links=None, child_changes=None):
    jobs = {
        "parent": {
            "id": "parent",
            "account_id": DEFAULT_ACCOUNT_ID,
            "selected_workflow": "author",
            "meta": {"invoke_children": links or {"child": _link()}},
        },
        "child": {
            "id": "child",
            "account_id": DEFAULT_ACCOUNT_ID,
            "selected_workflow": "client",
            "meta": {
                "invoke_parent_job_id": "parent",
                "invoke_parent_step_id": "author-hook",
                # This is a launch expectation, not evidence of an archived definition.
                "expected_workflow_revision": 999,
            },
            **(child_changes or {}),
        },
    }
    store = SimpleNamespace(get_job=lambda job_id: jobs.get(str(job_id)))
    service = JobQueryService(
        get_logger=lambda: store,
        serialize_attention_job_row=lambda row: row,
        serialize_awaiting_approval_job_row=lambda row: row,
        job_attention_group=lambda *_args: "completed",
        build_runtime_payload_for_api=lambda *_args: {},
    )
    return service, jobs


def test_child_invocations_expose_only_authorized_execution_identity():
    service, _jobs = _service()

    result = service.get_child_invocations(
        "parent", scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID)
    )

    assert result == {
        "invocations": [
            {
                "step_id": "author-hook",
                "step_execution_id": "execution-1",
                "invocation_key": "parent:execution-1",
                "child_job_id": "child",
                "child_workflow_id": "client",
            }
        ],
        "truncated": False,
    }
    assert "private-input-fingerprint" not in str(result)
    assert "999" not in str(result)


@pytest.mark.parametrize("foreign_kind", ["account", "workflow", "unrelated", "missing"])
def test_child_invocations_omit_foreign_inaccessible_or_unrelated_jobs(foreign_kind):
    service, jobs = _service()
    scope = ScopeContext(account_id=DEFAULT_ACCOUNT_ID)
    if foreign_kind == "account":
        jobs["child"]["account_id"] = "foreign-account"
    elif foreign_kind == "workflow":
        scope = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, allowed_workflow_ids=frozenset({"author"})
        )
    elif foreign_kind == "unrelated":
        jobs["child"]["meta"]["invoke_parent_job_id"] = "another-parent"
    else:
        del jobs["child"]

    result = service.get_child_invocations("parent", scope=scope)

    assert result == {"invocations": [], "truncated": False}


def test_child_invocations_authorize_the_parent_before_reading_its_links():
    service, _jobs = _service()
    scope = ScopeContext(account_id="foreign-account")
    with pytest.raises(ServiceForbiddenError):
        service.get_child_invocations("parent", scope=scope)


def test_child_invocations_preserve_two_hooks_that_call_the_same_client():
    links = {"child": _link(), "child-2": _link("child-2", "second-hook", "execution-2")}
    service, jobs = _service(links)
    jobs["child-2"] = {
        **jobs["child"],
        "id": "child-2",
        "meta": {"invoke_parent_job_id": "parent", "invoke_parent_step_id": "second-hook"},
    }

    result = service.get_child_invocations(
        "parent", scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID)
    )

    assert {
        (row["step_id"], row["step_execution_id"], row["child_job_id"])
        for row in result["invocations"]
    } == {
        ("author-hook", "execution-1", "child"),
        ("second-hook", "execution-2", "child-2"),
    }


def test_child_invocation_projection_is_bounded_without_reading_grandchildren():
    links = {f"child-{index}": _link(f"child-{index}") for index in range(105)}
    service, jobs = _service(links)
    for child_id in links:
        jobs[child_id] = {
            **jobs["child"],
            "id": child_id,
            "meta": {
                "invoke_parent_job_id": "parent",
                "invoke_parent_step_id": "author-hook",
                "invoke_children": {"grandchild": _link("grandchild")},
            },
        }

    result = service.get_child_invocations(
        "parent", scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID)
    )

    assert len(result["invocations"]) == 100
    assert result["truncated"] is True
    assert "grandchild" not in str(result)
