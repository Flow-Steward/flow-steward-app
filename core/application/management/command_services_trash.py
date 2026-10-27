# ruff: noqa: F403, I001
from __future__ import annotations

from .command_services_common import *  # NOSONAR

from .command_services_trash_entities_mixin import TrashLifecycleServiceEntitiesMixin
from .command_services_trash_execution_mixin import TrashLifecycleServiceExecutionMixin
from .command_services_trash_public_mixin import TrashLifecycleServicePublicMixin
from .command_services_trash_roots_mixin import TrashLifecycleServiceRootsMixin
from .command_services_trash_tree_mixin import TrashLifecycleServiceTreeMixin
from .command_services_trash_workspace_mixin import TrashLifecycleServiceWorkspaceMixin
from .command_services_trash_plans_mixin import TrashLifecycleServicePlansMixin


class TrashLifecycleService(
    TrashLifecycleServicePublicMixin,
    TrashLifecycleServicePlansMixin,
    TrashLifecycleServiceWorkspaceMixin,
    TrashLifecycleServiceRootsMixin,
    TrashLifecycleServiceTreeMixin,
    TrashLifecycleServiceExecutionMixin,
    TrashLifecycleServiceEntitiesMixin,
):
    pass


__all__ = [name for name in globals() if not name.startswith("__")]
