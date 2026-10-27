"""Trusted runtime context for first-party tabular feed execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _text(value: Any) -> str:
    return str(value or "").strip()


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _scope_value(
    *,
    key: str,
    trusted: dict[str, Any],
    project_config: dict[str, Any],
    state: dict[str, Any],
    allow_state_fallback: bool = False,
) -> str:
    if allow_state_fallback:
        return _text(trusted.get(key) or project_config.get(key) or state.get(key))
    return _text(trusted.get(key) or project_config.get(key))


@dataclass(frozen=True, slots=True)
class TabularFeedExecutionContext:
    account_id: str = ""
    tenant_id: str = ""
    workspace_id: str = ""
    project_id: str = ""
    workflow_id: str = ""
    job_id: str = ""
    run_id: str = ""
    step_id: str = ""
    actor_id: str = ""

    def as_runtime_context(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "account_id": self.account_id,
                "tenant_id": self.tenant_id,
                "workspace_id": self.workspace_id,
                "project_id": self.project_id,
                "workflow_id": self.workflow_id,
                "job_id": self.job_id,
                "run_id": self.run_id or self.job_id,
                "step_id": self.step_id,
                "actor_id": self.actor_id,
                "owner_scope": {
                    key: value
                    for key, value in {
                        "account_id": self.account_id,
                        "tenant_id": self.tenant_id,
                        "workspace_id": self.workspace_id,
                        "project_id": self.project_id,
                        "workflow_id": self.workflow_id,
                    }.items()
                    if value
                },
                "source": "tabular_feed_core",
            }.items()
            if value not in ("", None, {})
        }


def context_from_state_and_step(
    *,
    state: dict[str, Any],
    step: dict[str, Any],
) -> TabularFeedExecutionContext:
    trusted = _dict(state.get("trusted_runtime_context")) or _dict(
        state.get("_trusted_runtime_context")
    )
    project_config = _dict(state.get("project_config"))
    step_id = _text(step.get("step_id") or step.get("id"))
    if not step_id:
        from core.application.orchestration.active_step import active_step

        current = active_step(state)
        step_id = current.step_id if current is not None else ""
    return TabularFeedExecutionContext(
        account_id=_scope_value(
            key="account_id", trusted=trusted, project_config=project_config, state=state
        ),
        tenant_id=_scope_value(
            key="tenant_id", trusted=trusted, project_config=project_config, state=state
        ),
        workspace_id=_scope_value(
            key="workspace_id", trusted=trusted, project_config=project_config, state=state
        ),
        project_id=_scope_value(
            key="project_id", trusted=trusted, project_config=project_config, state=state
        ),
        workflow_id=_scope_value(
            key="workflow_id", trusted=trusted, project_config=project_config, state=state
        ),
        job_id=_scope_value(
            key="job_id",
            trusted=trusted,
            project_config=project_config,
            state=state,
            allow_state_fallback=True,
        ),
        run_id=_scope_value(
            key="run_id",
            trusted=trusted,
            project_config=project_config,
            state=state,
            allow_state_fallback=True,
        )
        or _scope_value(
            key="job_id",
            trusted=trusted,
            project_config=project_config,
            state=state,
            allow_state_fallback=True,
        ),
        step_id=step_id,
        actor_id=_scope_value(
            key="actor_id",
            trusted=trusted,
            project_config=project_config,
            state=state,
            allow_state_fallback=True,
        )
        or _scope_value(
            key="user_id",
            trusted=trusted,
            project_config=project_config,
            state=state,
            allow_state_fallback=True,
        ),
    )
