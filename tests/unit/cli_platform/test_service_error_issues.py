"""A refused command lists what to fix: blocked import dependencies and hook binding issues."""

from __future__ import annotations

from core.application.management.errors import ServiceError
from core.entrypoints.cli.platform.errors import map_exception


def test_a_blocked_import_lists_each_dependency_with_its_resolutions() -> None:
    exc = ServiceError(
        "Workflow import has unresolved blocking dependencies.",
        status_code=400,
        payload={
            "error": "workflow_import_preflight_blocked",
            "errors": [
                {
                    "dependency_key": "workflow:cli_paid_emails",
                    "status": "conflict",
                    "blocking": True,
                    "message": "Workflow id already exists. Choose a new target workflow id.",
                    "resolution_actions": ["rename_workflow", "abort"],
                }
            ],
        },
    )

    issues = map_exception(exc).field_issues

    assert [(issue.field, issue.code) for issue in issues] == [
        ("workflow:cli_paid_emails", "conflict")
    ]
    assert issues[0].message.endswith("Resolutions: rename_workflow, abort.")


def test_a_refused_binding_lists_its_issues() -> None:
    exc = ServiceError(
        "binding refused",
        status_code=400,
        payload={
            "issues": [
                {
                    "code": "workflow.hook.binding_output_type_mismatch",
                    "message": "Child output 'email' is string, but hook field 'vip' takes boolean",
                    "path": "output_mapping.email",
                }
            ]
        },
    )

    [issue] = map_exception(exc).field_issues

    assert (issue.field, issue.code) == (
        "output_mapping.email",
        "workflow.hook.binding_output_type_mismatch",
    )


def test_a_plain_service_error_has_no_issues() -> None:
    assert map_exception(ServiceError("Something failed")).field_issues == ()
