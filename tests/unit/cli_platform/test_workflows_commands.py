"""Contract tests for schema-driven workflow commands."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from click.testing import CliRunner

from core.application.management.scope import ScopeContext
from core.entrypoints.cli.platform.service_registry import ServiceRegistry


class _ProjectQuery:
    async def list_projects(self, *, page: int, per_page: int) -> dict[str, object]:
        self.page = page
        self.per_page = per_page
        return {"projects": [{"id": "proj_1", "name": "Project One"}]}


class _WorkflowOperator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def list_project_workflows(self, project_id: str, **kwargs: object) -> dict[str, object]:
        self.calls.append(("list_project_workflows", (project_id,), kwargs))
        return {"project_id": project_id, "workflows": [{"workflow_id": "wf.release"}]}

    def list_project_entrypoints(self, project_id: str, **kwargs: object) -> dict[str, object]:
        self.calls.append(("list_project_entrypoints", (project_id,), kwargs))
        return {"project_id": project_id, "entrypoints": [{"entry_key": "release"}]}

    def project_workflow_validate(
        self, project_id: str, workflow_id: str, **kwargs: object
    ) -> dict[str, object]:
        self.calls.append(("project_workflow_validate", (project_id, workflow_id), kwargs))
        return {"workflow_id": workflow_id, "validation": {"valid": True}, "issues": []}

    def project_workflow_contract(
        self, project_id: str, workflow_id: str, **kwargs: object
    ) -> dict[str, object]:
        self.calls.append(("project_workflow_contract", (project_id, workflow_id), kwargs))
        return {"workflow_id": workflow_id, "interface": {"input": {}}}

    def project_workflow_suggestions(
        self, project_id: str, workflow_id: str, **kwargs: object
    ) -> dict[str, object]:
        self.calls.append(("project_workflow_suggestions", (project_id, workflow_id), kwargs))
        return {"workflow_id": workflow_id, "suggestions": []}


@dataclass
class _Runtime:
    workflow_operator_service: _WorkflowOperator
    project_query_service: _ProjectQuery
    project_command_service: object = object()
    user_query_service: object = object()
    user_command_service: object = object()
    extension_scaffold_service: object = object()


def _app() -> tuple[object, _WorkflowOperator]:
    from core.entrypoints.cli.platform.click_adapter import build_click_app

    operator = _WorkflowOperator()
    runtime = _Runtime(workflow_operator_service=operator, project_query_service=_ProjectQuery())
    return build_click_app(ServiceRegistry(runtime), SimpleNamespace()), operator


def _assert_cli_scope(
    value: object, *, project_id: str, workflow_id: str | None = None
) -> ScopeContext:
    assert isinstance(value, ScopeContext)
    assert value.actor_id == "flow-steward-cli"
    assert value.principal_type == "system_actor"
    assert value.project_id == project_id
    assert value.allowed_project_ids == frozenset({project_id})
    if workflow_id:
        assert value.workflow_id == workflow_id
        assert value.allowed_workflow_ids == frozenset({workflow_id})
    return value


def test_workflow_phase2_leaf_commands_delegate_to_shared_operator_service() -> None:
    app, operator = _app()
    runner = CliRunner()

    cases = [
        (
            ["workflows", "list", "--project-id", "proj_1", "--include-disabled"],
            (
                "list_project_workflows",
                ("proj_1",),
                {"enabled_only": False, "include_validation": False},
            ),
        ),
        (
            [
                "workflows",
                "list",
                "--project-id",
                "proj_1",
                "--include-validation",
            ],
            (
                "list_project_workflows",
                ("proj_1",),
                {"enabled_only": True, "include_validation": True},
            ),
        ),
        (
            ["workflows", "entrypoints", "--project-id", "proj_1", "--include-disabled"],
            ("list_project_entrypoints", ("proj_1",), {"enabled_only": False}),
        ),
        (
            ["workflows", "validate", "--project-id", "proj_1", "wf.release"],
            ("project_workflow_validate", ("proj_1", "wf.release"), {}),
        ),
        (
            ["workflows", "contract", "--project-id", "proj_1", "wf.release"],
            ("project_workflow_contract", ("proj_1", "wf.release"), {}),
        ),
        (
            ["workflows", "suggestions", "--project-id", "proj_1", "wf.release"],
            ("project_workflow_suggestions", ("proj_1", "wf.release"), {}),
        ),
    ]

    for args, expected in cases:
        result = runner.invoke(app, args)
        assert result.exit_code == 0, (args, result.output)
        method, call_args, kwargs = operator.calls[-1]
        scope = kwargs.pop("scope")
        expected_method, expected_args, expected_kwargs = expected
        assert method == expected_method
        assert call_args == expected_args
        assert kwargs == expected_kwargs
        _assert_cli_scope(
            scope,
            project_id="proj_1",
            workflow_id="wf.release" if len(expected_args) == 2 else None,
        )


def test_workflow_validate_requires_workflow_id_before_service_call() -> None:
    app, operator = _app()

    result = CliRunner().invoke(
        app,
        ["workflows", "validate", "--json", "--project-id", "proj_1"],
    )

    assert result.exit_code == 3
    assert operator.calls == []
