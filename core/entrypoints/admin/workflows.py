# ruff: noqa: F403, I001
"""Admin API routes — Workflows CRUD with DB backend + phase/step management."""

from __future__ import annotations

from core.entrypoints.admin.workflows_common import *  # NOSONAR
from core.entrypoints.admin.workflows_projection import *  # NOSONAR
from core.entrypoints.admin.workflows_scope import *  # NOSONAR
from core.entrypoints.admin.workflows_connector_catalog import *  # NOSONAR
from core.entrypoints.admin.workflows_models import *  # NOSONAR
from core.entrypoints.admin.workflows_definition_router import *  # NOSONAR
from core.entrypoints.admin.workflows_routes_listing_import import *  # NOSONAR
from core.entrypoints.admin.workflows_routes_catalog import *  # NOSONAR
from core.entrypoints.admin.workflows_routes_read import *  # NOSONAR
from core.entrypoints.admin.workflows_routes_write import *  # NOSONAR
from core.entrypoints.admin.workflows_annotation_routes import *  # NOSONAR
from core.entrypoints.admin.workflows_kept_test_output_routes import *  # NOSONAR
from core.entrypoints.admin.workflows_group_extraction_routes import *  # NOSONAR
from core.entrypoints.admin.workflows_routes_history import *  # NOSONAR
from core.entrypoints.admin.workflows_routes_edit_session import *  # NOSONAR
from core.entrypoints.admin.workflows_routes_extension import *  # NOSONAR


__all__ = [name for name in globals() if not name.startswith("__")]
