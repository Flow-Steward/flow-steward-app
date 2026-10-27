from __future__ import annotations

import pytest

from core.entrypoints.admin.workflows_definition import _definition_persistence_http_exception


@pytest.mark.parametrize(
    ("constraint", "status", "detail"),
    [
        ("workflow_runtime_dependencies_install_id_fkey", 404, "Connector install not found"),
        ("registry_workflow_steps_skill_id_fkey", 404, "Skill not found"),
        ("workflow_runtime_dependencies_block_key_fkey", 404, "Published block not found"),
        ("registry_workflow_steps_group_fkey", 400, "Referenced workflow dependency not found"),
    ],
)
def test_foreign_key_refusal_names_the_missing_reference(
    constraint: str, status: int, detail: str
) -> None:
    error = RuntimeError(f'insert violates foreign key constraint "{constraint}"')
    mapped = _definition_persistence_http_exception(error)
    assert mapped.status_code == status
    assert mapped.detail == detail


def test_other_database_errors_stay_server_errors() -> None:
    assert (
        _definition_persistence_http_exception(RuntimeError("deadlock detected")).status_code == 500
    )
