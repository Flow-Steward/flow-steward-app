# ruff: noqa: I001, RUF100
"""Compatibility facade for the PostgreSQL registry store."""

from __future__ import annotations

from .db_store_common import *  # noqa: F403,F401  # NOSONAR
from .db_store_connection_mixin import RegistryDBStoreConnectionMixin
from .db_store_contracts_mixin import RegistryDBStoreContractsMixin
from .db_store_datasets_mixin import RegistryDBStoreDatasetsMixin
from .db_store_dataset_catalog_mixin import RegistryDBStoreDatasetCatalogMixin
from .db_store_blocks_mixin import RegistryDBStoreBlocksMixin
from .db_store_agents_mixin import RegistryDBStoreAgentsMixin
from .db_store_tools_mixin import RegistryDBStoreToolsMixin
from .db_store_skills_mixin import RegistryDBStoreSkillsMixin
from .db_store_projected_skills_mixin import RegistryDBStoreProjectedSkillsMixin
from .db_store_workflows_mixin import RegistryDBStoreWorkflowsMixin
from .db_store_guardrails_mixin import RegistryDBStoreGuardrailsMixin
from .db_store_revisions_mixin import RegistryDBStoreRevisionsMixin


class RegistryDBStore(
    RegistryDBStoreConnectionMixin,
    RegistryDBStoreContractsMixin,
    RegistryDBStoreDatasetsMixin,
    RegistryDBStoreDatasetCatalogMixin,
    RegistryDBStoreBlocksMixin,
    RegistryDBStoreAgentsMixin,
    RegistryDBStoreToolsMixin,
    RegistryDBStoreProjectedSkillsMixin,
    RegistryDBStoreSkillsMixin,
    RegistryDBStoreWorkflowsMixin,
    RegistryDBStoreGuardrailsMixin,
    RegistryDBStoreRevisionsMixin,
):
    pass


__all__ = [
    *globals().get("__all__", []),
    "RegistryDBStore",
]
