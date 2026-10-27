# ruff: noqa: F403, I001
from __future__ import annotations

from .command_services_common import *  # NOSONAR

from .command_services_project_config_mixin import ProjectCommandServiceConfigMixin
from .command_services_project_core_mixin import ProjectCommandServiceCoreMixin
from .command_services_project_crud_mixin import ProjectCommandServiceCrudMixin
from .command_services_project_tasks_mixin import ProjectCommandServiceTasksMixin
from .command_services_project_run_requests_mixin import ProjectCommandServiceRunRequestsMixin
from .command_services_project_workflow_bindings_mixin import (
    ProjectCommandServiceWorkflowBindingsMixin,
)


class ProjectCommandService(
    ProjectCommandServiceCoreMixin,
    ProjectCommandServiceWorkflowBindingsMixin,
    ProjectCommandServiceConfigMixin,
    ProjectCommandServiceCrudMixin,
    ProjectCommandServiceTasksMixin,
    ProjectCommandServiceRunRequestsMixin,
):
    pass


__all__ = [name for name in globals() if not name.startswith("__")]
