"""Control-plane API router for admin and registry surfaces."""

from __future__ import annotations

from fastapi import APIRouter


def create_control_router() -> APIRouter:
    """Create and return the combined control-plane APIRouter."""
    from core.entrypoints.admin.agents import router as agents_router
    from core.entrypoints.admin.catalog_categories import router as catalog_categories_router
    from core.entrypoints.admin.changelog import router as changelog_router
    from core.entrypoints.admin.data_contracts import router as data_contracts_router
    from core.entrypoints.admin.datasets import router as datasets_router
    from core.entrypoints.admin.fields import router as fields_router
    from core.entrypoints.admin.guardrails import router as guardrails_router
    from core.entrypoints.admin.monitoring import router as monitoring_router
    from core.entrypoints.admin.preview import router as preview_router
    from core.entrypoints.admin.published_blocks import router as published_blocks_router
    from core.entrypoints.admin.skills import router as skills_router
    from core.entrypoints.admin.tools import router as tools_router
    from core.entrypoints.admin.workflows import router as workflows_router
    from core.entrypoints.admin.workspaces import router as workspaces_router
    from core.entrypoints.api.workflow_extension_contract import (
        router as workflow_extension_contract_router,
    )

    router = APIRouter()
    router.include_router(agents_router)
    router.include_router(catalog_categories_router)
    router.include_router(tools_router)
    router.include_router(skills_router)
    router.include_router(workflows_router)
    router.include_router(workflow_extension_contract_router)
    router.include_router(workspaces_router)
    router.include_router(monitoring_router)
    router.include_router(changelog_router)
    router.include_router(preview_router)
    router.include_router(guardrails_router)
    router.include_router(data_contracts_router)
    router.include_router(datasets_router)
    router.include_router(fields_router)
    router.include_router(published_blocks_router)
    return router
