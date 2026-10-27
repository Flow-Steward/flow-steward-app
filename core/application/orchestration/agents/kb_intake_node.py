"""
Project context intake — Phase 4 LangGraph node.

Runs before the first workflow agent.
Loads the runtime-owned project profile by trusted account and project IDs. The
persisted node and state keys keep their legacy names for checkpoint compatibility;
there is no public KB or MCP hop.

If project context is empty or unavailable, this node is a no-op unless mandatory
grounding is enabled.
"""

import logging

from core.application.memory.project_context import load_project_context
from core.application.orchestration.runtime_identity import runtime_identity_from_state
from core.application.orchestration.state import OrchestratorState
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

logger = logging.getLogger(__name__)


def kb_intake_node(state: OrchestratorState) -> dict:
    """
    LangGraph node: load project profile into kb_context.

    Fetches the persistent project profile (rules and conventions) by stable
    doc ID — fast, always included when it exists (~200 tokens).

    This context is injected into every subsequent agent's prompt via
    build_context_block() under the "## Project context" heading.
    """
    identity = runtime_identity_from_state(state)
    missing = identity.missing("account_id", "project_id")
    if missing:
        required = str(_runtime_env.get("FS_MCP_GROUNDING_REQUIRED") or "").strip().lower()
        if required in {"1", "true", "yes", "on"}:
            raise RuntimeError(
                "Mandatory project grounding requires trusted account_id and project_id identity."
            )
        return {"kb_context": None}

    profile = load_project_context(
        account_id=identity.account_id,
        project_id=identity.project_id,
    )
    if profile:
        from core.application.orchestration.agents.common import sanitize_external_input

        profile = sanitize_external_input(profile, context_type="kb", state=state)
        logger.info("kb_intake_node: project context loaded (%d chars)", len(profile))
        return {"kb_context": profile}
    logger.debug("kb_intake_node: no project context for project_id='%s'", identity.project_id)

    return {"kb_context": None}
