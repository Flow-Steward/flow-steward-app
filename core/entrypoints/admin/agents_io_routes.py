# mypy: ignore-errors
"""Performance and structured I/O routes for admin agents."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.query_models import (
    AgentEffectiveIOView,
    AgentOutputFormatPreviewView,
    AgentPerformanceView,
    AgentTemplateVariableView,
    RegistryContractItemView,
)
from core.application.registry.contract_validation import (
    format_validation_error_detail,
    validate_contract_items,
)
from core.entrypoints.admin._registry_scope import assert_row_in_account_scope, request_scope
from core.entrypoints.admin.agents_models import AgentInputItem, AgentOutputItem
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env


def register_agent_io_routes(
    router: APIRouter,
    *,
    get_db: Callable[[], Any],
    get_registry: Callable[[], Any],
    primary_skill_id: Callable[[dict | None], str | None],
) -> None:
    # --------------------------------------------------------------------------- #
    # Performance metrics (from platform_events)                                       #
    # --------------------------------------------------------------------------- #

    @router.get(
        "/{agent_id}/performance",
        response_model=AgentPerformanceView,
        responses={
            400: {"description": "Bad request"},
            403: {"description": "Forbidden"},
            404: {"description": "Not found"},
            409: {"description": "Conflict"},
            500: {"description": "Internal server error"},
            503: {"description": "Service unavailable"},
        },
    )
    def get_agent_performance(
        agent_id: str,
        request: Request,
        project_name: Annotated[str | None, Query()] = None,
    ):
        """Get performance metrics for an agent from the DB."""
        scope = request_scope(request)
        # Cross-scope guard: a scoped admin may only read metrics for an agent
        # that is global or owned by their account.
        agent = get_db().get_agent(agent_id)
        if agent:
            assert_row_in_account_scope(agent, scope, detail=f"Agent '{agent_id}' not found")
        scope_account_id = str(getattr(scope, "account_id", "") or "").strip() or DEFAULT_ACCOUNT_ID
        try:
            from core.infrastructure.runtime.entrypoint_db_adapters import (
                create_connection_store,
            )

            dsn = _runtime_env.get("DATABASE_URL") or _runtime_env.get("FS_DB_DSN")
            if not dsn:
                return {"agent_id": agent_id, "error": "No database configured"}
            bl = create_connection_store(dsn)
            with bl.conn.cursor() as cur:
                # Always confine job metrics to jobs owned by the caller's account
                # so cross-account cost/duration telemetry is never exposed.
                query = """
                    SELECT
                        COUNT(*) as invocations,
                        AVG(EXTRACT(EPOCH FROM (
                            SELECT MAX(created_at) - MIN(created_at)
                            FROM platform_events e2
                            WHERE e2.job_id = e.job_id AND e2.agent = e.agent
                        ))) as avg_duration_secs,
                        SUM((detail->>'cost_usd')::float) as total_cost_usd
                    FROM platform_events e
                    WHERE agent = %s
                      AND event_type IN ('agent_completed', 'phase_complete')
                      AND job_id IN (SELECT id FROM jobs WHERE account_id::text = %s)
                """
                params = [agent_id, scope_account_id]
                if project_name:
                    query += (
                        " AND job_id IN (SELECT id FROM jobs"
                        " WHERE project_name = %s AND account_id::text = %s)"
                    )
                    params.extend([project_name, scope_account_id])
                cur.execute(query, params)
                row = cur.fetchone()
                return {
                    "agent_id": agent_id,
                    "invocations": row[0] if row else 0,
                    "avg_duration_secs": float(row[1]) if row and row[1] else None,
                    "total_cost_usd": float(row[2]) if row and row[2] else None,
                }
        except Exception as e:
            return {"agent_id": agent_id, "error": str(e)}

    # --------------------------------------------------------------------------- #
    # Structured I/O                                                               #
    # --------------------------------------------------------------------------- #

    @router.get(
        "/{agent_id}/inputs",
        response_model=list[RegistryContractItemView],
        response_model_exclude_none=True,
        responses={
            400: {"description": "Bad request"},
            403: {"description": "Forbidden"},
            404: {"description": "Not found"},
            409: {"description": "Conflict"},
            500: {"description": "Internal server error"},
            503: {"description": "Service unavailable"},
        },
    )
    def get_agent_inputs(agent_id: str, request: Request):
        """Get structured inputs for an agent."""
        db = get_db()
        agent = db.get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
        assert_row_in_account_scope(
            agent, request_scope(request), detail=f"Agent '{agent_id}' not found"
        )
        skill_id = primary_skill_id(agent)
        if skill_id:
            return db.get_skill_inputs(skill_id)
        return db.get_agent_own_inputs(agent_id)

    @router.put(
        "/{agent_id}/inputs",
        response_model=list[RegistryContractItemView],
        response_model_exclude_none=True,
        responses={
            400: {"description": "Bad request"},
            403: {"description": "Forbidden"},
            404: {"description": "Not found"},
            409: {"description": "Conflict"},
            500: {"description": "Internal server error"},
            503: {"description": "Service unavailable"},
        },
    )
    def set_agent_inputs(agent_id: str, request: Request, items: list[AgentInputItem]):
        """Replace all structured inputs for an agent."""
        db = get_db()
        agent = db.get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
        assert_row_in_account_scope(
            agent, request_scope(request), detail=f"Agent '{agent_id}' not found"
        )
        skill_id = primary_skill_id(agent)
        payload = [i.model_dump() for i in items]
        _, issues = validate_contract_items(
            payload,
            path_prefix="agent.inputs",
            default_required=False,
        )
        if issues:
            validation = {"valid": False, "errors": issues, "warnings": []}
            raise HTTPException(
                status_code=400,
                detail=format_validation_error_detail(
                    entity_type="agent",
                    entity_id=agent_id,
                    action="save_inputs",
                    validation=validation,
                ),
            )
        if skill_id:
            db.set_skill_inputs(skill_id, payload)
            get_registry().reload_all()
            db.materialize_agents_for_skill(skill_id)
            return db.get_skill_inputs(skill_id)
        db.set_agent_inputs(agent_id, payload)
        db.materialize_agent_contract(agent_id)
        return db.get_agent_own_inputs(agent_id)

    @router.get(
        "/{agent_id}/outputs",
        response_model=list[RegistryContractItemView],
        response_model_exclude_none=True,
        responses={
            400: {"description": "Bad request"},
            403: {"description": "Forbidden"},
            404: {"description": "Not found"},
            409: {"description": "Conflict"},
            500: {"description": "Internal server error"},
            503: {"description": "Service unavailable"},
        },
    )
    def get_agent_outputs(agent_id: str, request: Request):
        """Get structured outputs for an agent."""
        db = get_db()
        agent = db.get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
        assert_row_in_account_scope(
            agent, request_scope(request), detail=f"Agent '{agent_id}' not found"
        )
        skill_id = primary_skill_id(agent)
        if skill_id:
            return db.get_skill_outputs(skill_id)
        return db.get_agent_own_outputs(agent_id)

    @router.put(
        "/{agent_id}/outputs",
        response_model=list[RegistryContractItemView],
        response_model_exclude_none=True,
        responses={
            400: {"description": "Bad request"},
            403: {"description": "Forbidden"},
            404: {"description": "Not found"},
            409: {"description": "Conflict"},
            500: {"description": "Internal server error"},
            503: {"description": "Service unavailable"},
        },
    )
    def set_agent_outputs(agent_id: str, request: Request, items: list[AgentOutputItem]):
        """Replace all structured outputs for an agent."""
        db = get_db()
        agent = db.get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
        assert_row_in_account_scope(
            agent, request_scope(request), detail=f"Agent '{agent_id}' not found"
        )
        skill_id = primary_skill_id(agent)
        payload = [i.model_dump() for i in items]
        _, issues = validate_contract_items(
            payload,
            path_prefix="agent.outputs",
            default_required=True,
        )
        if issues:
            validation = {"valid": False, "errors": issues, "warnings": []}
            raise HTTPException(
                status_code=400,
                detail=format_validation_error_detail(
                    entity_type="agent",
                    entity_id=agent_id,
                    action="save_outputs",
                    validation=validation,
                ),
            )
        if skill_id:
            db.set_skill_outputs(skill_id, payload)
            get_registry().reload_all()
            db.materialize_agents_for_skill(skill_id)
            return db.get_skill_outputs(skill_id)
        db.set_agent_outputs(agent_id, payload)
        db.materialize_agent_contract(agent_id)
        return db.get_agent_own_outputs(agent_id)

    @router.get(
        "/{agent_id}/effective-io",
        response_model=AgentEffectiveIOView,
        responses={
            400: {"description": "Bad request"},
            403: {"description": "Forbidden"},
            404: {"description": "Not found"},
            409: {"description": "Conflict"},
            500: {"description": "Internal server error"},
            503: {"description": "Service unavailable"},
        },
    )
    def get_effective_io(agent_id: str, request: Request):
        """Computed effective I/O merging agent + skill + tool I/O with source badges."""
        db = get_db()
        agent = db.get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
        assert_row_in_account_scope(
            agent, request_scope(request), detail=f"Agent '{agent_id}' not found"
        )
        from core.application.registry.io_resolver import IOResolver

        resolver = IOResolver(db)
        return resolver.compute_effective_io(agent_id)

    @router.get(
        "/{agent_id}/template-variables",
        response_model=list[AgentTemplateVariableView],
        responses={
            400: {"description": "Bad request"},
            403: {"description": "Forbidden"},
            404: {"description": "Not found"},
            409: {"description": "Conflict"},
            500: {"description": "Internal server error"},
            503: {"description": "Service unavailable"},
        },
    )
    def get_template_variables(agent_id: str, request: Request):
        """All variables available for context template editor."""
        db = get_db()
        agent = db.get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
        assert_row_in_account_scope(
            agent, request_scope(request), detail=f"Agent '{agent_id}' not found"
        )
        from core.application.registry.io_resolver import IOResolver

        resolver = IOResolver(db)
        return resolver.list_template_variables(agent_id)

    @router.post(
        "/{agent_id}/generate-output-format",
        response_model=AgentOutputFormatPreviewView,
        responses={
            400: {"description": "Bad request"},
            403: {"description": "Forbidden"},
            404: {"description": "Not found"},
            409: {"description": "Conflict"},
            500: {"description": "Internal server error"},
            503: {"description": "Service unavailable"},
        },
    )
    def generate_output_format(agent_id: str, request: Request):
        """Generate the canonical response-contract preview from structured outputs."""
        db = get_db()
        agent = db.get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
        assert_row_in_account_scope(
            agent, request_scope(request), detail=f"Agent '{agent_id}' not found"
        )
        skill_id = primary_skill_id(agent)
        outputs = db.get_skill_outputs(skill_id) if skill_id else db.get_agent_outputs(agent_id)
        from core.application.registry.io_resolver import IOResolver

        resolver = IOResolver(db)
        contract = resolver.generate_output_format(outputs)
        return {
            "response_contract": contract,
            "output_format": contract,
        }
