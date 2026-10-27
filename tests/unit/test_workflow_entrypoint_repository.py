from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.application.workflows.workflow_entrypoint_repository import (
    resolve_project_run_config_for_workflow_project,
)


class _Cursor:
    description = [("project_id",)]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, *_args):
        return None

    def fetchone(self):
        return {"project_id": "project-1"}


def test_project_run_config_storage_failure_is_not_downgraded_to_partial_config() -> None:
    def _unavailable(_project_id: str):
        raise RuntimeError("project config database unavailable")

    bl = SimpleNamespace(
        conn=SimpleNamespace(cursor=lambda: _Cursor()),
        get_project_run_config=_unavailable,
        get_project=lambda _project_id: {"name": "unsafe fallback"},
    )

    with pytest.raises(RuntimeError, match="project config database unavailable"):
        resolve_project_run_config_for_workflow_project(bl, "workflow-1", "project-1")
