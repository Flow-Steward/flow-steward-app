"""One read answers which projects link a workflow.

The builder used to list every project and then read each project's bindings
(one request per project) to find the project that links the open workflow.
``ProjectQueryService.list_workflow_linked_projects`` answers it in one call,
with each project's own bindings as the authority.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.errors import ServiceNotFoundError
from core.application.management.query_services import ProjectQueryService


def _service(
    projects: list[dict], bindings: dict[str, list[dict]]
) -> tuple[ProjectQueryService, MagicMock]:
    """A logger whose list rows and bindings are the given ones (list order = given order)."""
    bl = MagicMock()

    def list_projects(*, limit, offset, **_filters):
        return [dict(row) for row in projects[offset : offset + limit]]

    def get_project(project_id):
        if str(project_id) not in bindings:
            return None
        return {"account_id": DEFAULT_ACCOUNT_ID, "id": project_id, "name": project_id}

    bl.list_projects.side_effect = list_projects
    bl.count_projects.side_effect = lambda **_filters: len(projects)
    bl.get_project.side_effect = get_project
    bl.get_project_workflows.side_effect = lambda project_id, **_kwargs: [
        dict(row) for row in bindings.get(str(project_id), [])
    ]
    service = ProjectQueryService(
        get_logger=lambda: bl,
        serialize_projects_grid_row=lambda row: row,
        get_queue_depth=lambda _project_id: 0,
        get_ready_queue_depth=lambda: 0,
    )
    return service, bl


def _row(project_id: str, *workflow_ids: str, default: str = "") -> dict:
    return {
        "id": project_id,
        "name": project_id.title(),
        "workflow_ids": list(workflow_ids),
        "operator_default_workflow_id": default,
    }


def _binding(workflow_id: str, *, enabled: bool = True, default: bool = False) -> dict:
    return {"workflow_id": workflow_id, "enabled": enabled, "is_default": default}


def test_linked_projects_come_in_list_order_and_only_linked_projects_are_asked() -> None:
    service, bl = _service(
        [
            _row("alpha", "orders"),
            _row("beta", "other"),
            _row("gamma", default="orders"),
            _row("delta"),
        ],
        {
            "alpha": [_binding("orders")],
            "beta": [_binding("other")],
            "gamma": [_binding("orders", default=True)],
            "delta": [],
        },
    )

    payload = service.list_workflow_linked_projects("orders").dump()

    assert payload["workflow_id"] == "orders"
    assert [row["id"] for row in payload["projects"]] == ["alpha", "gamma"]
    assert payload["projects"][0]["name"] == "Alpha"
    asked = [call.args[0] for call in bl.get_project_workflows.call_args_list]
    assert asked == ["alpha", "gamma"]


def test_the_project_bindings_decide_over_the_list_row() -> None:
    # The list row still names the workflow, but the project's bindings no
    # longer select it (disabled): the bindings are the authority.
    service, _bl = _service(
        [_row("alpha", "orders"), _row("beta", "orders")],
        {"alpha": [_binding("orders", enabled=False)], "beta": [_binding("orders")]},
    )

    payload = service.list_workflow_linked_projects("orders").dump()

    assert [row["id"] for row in payload["projects"]] == ["beta"]


def test_every_page_of_projects_is_searched() -> None:
    projects = [_row(f"p{index:03d}") for index in range(250)]
    projects[230] = _row("p230", "orders")
    bindings = {row["id"]: [] for row in projects}
    bindings["p230"] = [_binding("orders")]
    service, bl = _service(projects, bindings)

    payload = service.list_workflow_linked_projects("orders").dump()

    assert [row["id"] for row in payload["projects"]] == ["p230"]
    assert [call.kwargs["offset"] for call in bl.list_projects.call_args_list] == [0, 200]


def test_a_failed_bindings_read_fails_the_answer_instead_of_dropping_a_link() -> None:
    # The list names a project whose bindings cannot be read.
    service, _bl = _service([_row("alpha", "orders")], {})

    with pytest.raises(ServiceNotFoundError):
        service.list_workflow_linked_projects("orders")


def test_an_empty_workflow_id_links_nothing_and_reads_nothing() -> None:
    service, bl = _service([_row("alpha", "orders")], {"alpha": [_binding("orders")]})

    payload = service.list_workflow_linked_projects("  ").dump()

    assert payload == {"workflow_id": "", "projects": []}
    bl.list_projects.assert_not_called()
