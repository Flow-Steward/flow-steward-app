# ruff: noqa: F403, F405, RUF022
# mypy: ignore-errors
"""Workflow admin route/helper module extracted from workflows."""

from __future__ import annotations

from core.application.extensions.projected_dataset_bridge import projection_bridge_id
from core.application.workflows.workflow_secret_default_service import (
    normalize_disabled_workflow_validation,
)
from core.entrypoints.admin.workflows_common import *  # NOSONAR


class _WorkflowValidationDBView:
    def __init__(
        self,
        db: Any,
        workflow_id: str,
        workflow: dict[str, Any],
        *,
        account_id: str = DEFAULT_ACCOUNT_ID,
    ) -> None:
        self._db = db
        self._workflow_id = _normalize_text(workflow_id)
        self._workflow = dict(workflow)
        self._account_id = _normalize_text(account_id) or DEFAULT_ACCOUNT_ID
        self._registry = None

    def _get_registry(self):
        if self._registry is None:
            from core.application.registry.registry_manager import RegistryManager

            self._registry = RegistryManager.instance()
        return self._registry

    def _projected_agent_row(self, agent_id: str) -> dict[str, Any] | None:
        from core.application.registry.workflow_lookup import get_registry_artifact_row

        canonical = get_registry_artifact_row(
            self._db,
            artifact_kind="agent",
            id_key="agent_id",
            artifact_id=agent_id,
            platform_getter="get_agent",
            account_id=self._account_id,
        )
        if canonical:
            return canonical
        projection = self._extension_projection_snapshot("agent", agent_id)
        if projection:
            return projection
        manifest = self._get_registry().agents.get_agent(agent_id)
        if manifest is None:
            return None
        return {
            "agent_id": manifest.agent_id,
            "version": manifest.version,
            "display_name": manifest.display_name,
            "description": manifest.description,
            "enabled": bool(manifest.enabled),
            "role_type": manifest.role_type,
            "_skills": [
                {
                    "skill_id": skill_id,
                    "skill_prompt": "",
                    "sort_order": index,
                }
                for index, skill_id in enumerate(list(manifest.skills or []))
                if _normalize_text(skill_id)
            ],
            "skills": [
                {
                    "skill_id": skill_id,
                    "skill_prompt": "",
                    "sort_order": index,
                }
                for index, skill_id in enumerate(list(manifest.skills or []))
                if _normalize_text(skill_id)
            ],
            "_tools": [
                {
                    "tool_id": tool_id,
                    "sort_order": index,
                }
                for index, tool_id in enumerate(list(manifest.tools or []))
                if _normalize_text(tool_id)
            ],
            "tools": [
                {
                    "tool_id": tool_id,
                    "sort_order": index,
                }
                for index, tool_id in enumerate(list(manifest.tools or []))
                if _normalize_text(tool_id)
            ],
            "_inputs": list(manifest.structured_inputs or []),
            "_outputs": list(manifest.structured_outputs or []),
            "prompts": {
                "system": getattr(manifest.prompts, "system", ""),
                "skill_prompts": getattr(manifest.prompts, "skill_prompts", {}) or {},
                "local_instructions": getattr(manifest.prompts, "local_instructions", []) or [],
                "context_template": getattr(manifest.prompts, "context_template", ""),
                "output_format": getattr(manifest.prompts, "output_format", ""),
                "context_sections": getattr(manifest.prompts, "context_sections", {}) or {},
                "examples": getattr(manifest.prompts, "examples", {}) or {},
            },
            "outputs": [row.get("name") for row in (manifest.structured_outputs or [])],
        }

    def _projected_skill_row(self, skill_id: str) -> dict[str, Any] | None:
        from core.application.registry.workflow_lookup import get_registry_artifact_row

        canonical = get_registry_artifact_row(
            self._db,
            artifact_kind="skill",
            id_key="skill_id",
            artifact_id=skill_id,
            platform_getter="get_skill",
            account_id=self._account_id,
        )
        if canonical:
            return canonical
        projection = self._extension_projection_snapshot("skill", skill_id)
        if projection:
            return projection
        manifest = self._get_registry().skills.get_skill(skill_id)
        if manifest is None:
            return None
        return {
            "skill_id": manifest.skill_id,
            "version": manifest.version,
            "display_name": manifest.display_name,
            "description": manifest.description,
            "enabled": bool(manifest.enabled),
            "category": manifest.category,
            "skill_type": manifest.skill_type,
            "instructions": list(manifest.instructions or []),
            "prompt_template": manifest.prompt_template,
            "input_schema": dict(manifest.input_schema or {}),
            "output_schema": dict(manifest.output_schema or {}),
            "examples": list(manifest.examples or []),
            "tags": list(manifest.tags or []),
            "supported_role_types": list(manifest.supported_role_types or []),
            "_inputs": list(manifest.expected_inputs or []),
            "_outputs": list(manifest.expected_outputs or []),
            "using_agents": [
                agent_id
                for agent_id in (
                    _normalize_text(agent.agent_id)
                    for agent in (self._get_registry().agents.list_agents() or [])
                    if skill_id in set(agent.skills or [])
                )
                if agent_id
            ],
            "_agents": [
                agent_id
                for agent_id in (
                    _normalize_text(agent.agent_id)
                    for agent in (self._get_registry().agents.list_agents() or [])
                    if skill_id in set(agent.skills or [])
                )
                if agent_id
            ],
            "agents": [
                agent_id
                for agent_id in (
                    _normalize_text(agent.agent_id)
                    for agent in (self._get_registry().agents.list_agents() or [])
                    if skill_id in set(agent.skills or [])
                )
                if agent_id
            ],
        }

    def _extension_projection_snapshot(
        self, artifact_kind: str, artifact_id: str
    ) -> dict[str, Any] | None:
        normalized_artifact_id = _normalize_text(artifact_id)
        if not normalized_artifact_id:
            return None
        runtime_store = _get_extension_runtime_store(self._db)
        if runtime_store is None or not hasattr(runtime_store, "list_projections"):
            return None
        try:
            rows = runtime_store.list_projections(
                account_id=self._account_id,
                artifact_kind=artifact_kind,
                only_active=False,
            )
        except Exception as exc:
            logger.debug(
                "extension projection lookup failed kind=%s id=%s: %s",
                artifact_kind,
                normalized_artifact_id,
                exc,
            )
            return None
        for row in rows or []:
            if hasattr(row, "model_dump"):
                projection = row.model_dump(mode="json")
            elif isinstance(row, dict):
                projection = dict(row)
            else:
                projection = {}
            local_artifact_id = _normalize_text(projection.get("artifact_id"))
            install_id = _normalize_text(projection.get("install_id"))
            public_artifact_id = local_artifact_id
            if install_id and local_artifact_id:
                public_artifact_id = projection_bridge_id(install_id, local_artifact_id)
            workflow_parts = self._workflow_id.split(".", 2)
            workflow_install_id = (
                workflow_parts[1]
                if len(workflow_parts) == 3 and workflow_parts[0] == "extension"
                else ""
            )
            scoped_local_reference = (
                bool(workflow_install_id)
                and install_id == workflow_install_id
                and local_artifact_id == normalized_artifact_id
            )
            if public_artifact_id != normalized_artifact_id and not scoped_local_reference:
                continue
            snapshot = projection.get("projection")
            snapshot = dict(snapshot) if isinstance(snapshot, dict) else {}
            if not snapshot:
                continue
            if artifact_kind == "agent":
                return self._normalize_projected_agent_snapshot(
                    normalized_artifact_id,
                    snapshot,
                    projection,
                )
            if artifact_kind == "skill":
                return self._normalize_projected_skill_snapshot(
                    normalized_artifact_id,
                    snapshot,
                    projection,
                )
            if artifact_kind == "tool":
                return self._normalize_projected_tool_snapshot(
                    normalized_artifact_id,
                    snapshot,
                    projection,
                )
            return snapshot
        return None

    def _normalize_projected_agent_snapshot(
        self,
        agent_id: str,
        snapshot: dict[str, Any],
        projection: dict[str, Any],
    ) -> dict[str, Any]:
        payload = dict(snapshot)
        payload["agent_id"] = agent_id
        payload["enabled"] = bool(payload.get("enabled", projection.get("is_active", True)))
        payload["source_install_id"] = _normalize_text(
            payload.get("source_install_id") or projection.get("install_id")
        )
        payload["source_release_id"] = _normalize_text(
            payload.get("source_release_id") or projection.get("release_id")
        )
        payload["projection_mode"] = _normalize_text(payload.get("projection_mode") or "derived")
        raw_skills = payload.get("_skills")
        if not isinstance(raw_skills, list):
            raw_skills = payload.get("skills")
        normalized_skills: list[dict[str, Any]] = []
        for index, item in enumerate(list(raw_skills or [])):
            if isinstance(item, dict):
                skill_id = _normalize_text(
                    item.get("skill_id") or item.get("id") or item.get("name")
                )
                if skill_id:
                    normalized_skills.append(
                        {
                            "skill_id": skill_id,
                            "skill_prompt": _normalize_text(item.get("skill_prompt")),
                            "sort_order": int(item.get("sort_order", index) or index),
                        }
                    )
            else:
                skill_id = _normalize_text(item)
                if skill_id:
                    normalized_skills.append(
                        {
                            "skill_id": skill_id,
                            "skill_prompt": "",
                            "sort_order": index,
                        }
                    )
        payload["_skills"] = normalized_skills
        payload["skills"] = normalized_skills
        raw_tools = payload.get("_tools")
        if not isinstance(raw_tools, list):
            raw_tools = payload.get("tools")
        normalized_tools: list[dict[str, Any]] = []
        for index, item in enumerate(list(raw_tools or [])):
            if isinstance(item, dict):
                tool_id = _normalize_text(item.get("tool_id") or item.get("id") or item.get("name"))
                if tool_id:
                    normalized_tools.append(
                        {
                            "tool_id": tool_id,
                            "sort_order": int(item.get("sort_order", index) or index),
                        }
                    )
            else:
                tool_id = _normalize_text(item)
                if tool_id:
                    normalized_tools.append({"tool_id": tool_id, "sort_order": index})
        payload["_tools"] = normalized_tools
        payload["tools"] = normalized_tools
        payload["_inputs"] = list(payload.get("_inputs") or payload.get("inputs") or [])
        payload["_outputs"] = list(payload.get("_outputs") or payload.get("outputs") or [])
        if not isinstance(payload.get("prompts"), dict):
            payload["prompts"] = {
                "system": _normalize_text(payload.get("system_prompt")),
                "skill_prompts": {},
                "local_instructions": [],
                "context_template": "",
                "output_format": "",
                "context_sections": {},
                "examples": {},
            }
        return payload

    def _normalize_projected_skill_snapshot(
        self,
        skill_id: str,
        snapshot: dict[str, Any],
        projection: dict[str, Any],
    ) -> dict[str, Any]:
        payload = dict(snapshot)
        payload["skill_id"] = skill_id
        payload["enabled"] = bool(payload.get("enabled", projection.get("is_active", True)))
        payload["source_install_id"] = _normalize_text(
            payload.get("source_install_id") or projection.get("install_id")
        )
        payload["source_release_id"] = _normalize_text(
            payload.get("source_release_id") or projection.get("release_id")
        )
        payload["projection_mode"] = _normalize_text(payload.get("projection_mode") or "derived")
        payload["_inputs"] = list(payload.get("_inputs") or payload.get("public_inputs") or [])
        payload["_outputs"] = list(payload.get("_outputs") or payload.get("public_outputs") or [])
        payload.setdefault("supported_role_types", [])
        payload.setdefault("instructions", [])
        payload.setdefault("examples", [])
        payload.setdefault("tags", [])
        payload.setdefault("input_schema", {})
        payload.setdefault("output_schema", {})
        return payload

    def _normalize_projected_tool_snapshot(
        self,
        tool_id: str,
        snapshot: dict[str, Any],
        projection: dict[str, Any],
    ) -> dict[str, Any]:
        payload = dict(snapshot)
        payload["tool_id"] = tool_id
        payload["enabled"] = bool(payload.get("enabled", projection.get("is_active", True)))
        payload["source_install_id"] = _normalize_text(
            payload.get("source_install_id") or projection.get("install_id")
        )
        payload["source_release_id"] = _normalize_text(
            payload.get("source_release_id") or projection.get("release_id")
        )
        payload["projection_mode"] = _normalize_text(payload.get("projection_mode") or "derived")
        payload.setdefault("parameters", [])
        payload.setdefault("returns", {})
        return payload

    def get_workflow(self, workflow_id: str):
        from core.application.registry.workflow_lookup import get_registry_artifact_row

        # B21: the workflow this view stands for is the row it was handed -- the
        # resolved projection, redacted when protected. Asking the platform table
        # first returned an installed extension's anchor row instead, whose
        # settings are empty, so the builder said "no inputs / no outputs" for a
        # workflow that declares both (and read an unredacted row).
        if _normalize_text(workflow_id) == self._workflow_id:
            return dict(self._workflow)
        canonical = get_registry_artifact_row(
            self._db,
            artifact_kind="workflow",
            id_key="workflow_id",
            artifact_id=workflow_id,
            platform_getter="get_workflow",
            account_id=self._account_id,
        )
        if canonical:
            return canonical
        return None

    def get_agent(self, agent_id: str):
        return self._db.get_agent(agent_id) or self._projected_agent_row(agent_id)

    def get_agent_skills(self, agent_id: str):
        getter = getattr(self._db, "get_agent_skills", None)
        rows = getter(agent_id) if callable(getter) else []
        if rows:
            return rows
        agent = self._projected_agent_row(agent_id)
        return list(agent.get("_skills") or []) if isinstance(agent, dict) else []

    def get_agent_tools(self, agent_id: str):
        getter = getattr(self._db, "get_agent_tools", None)
        rows = getter(agent_id) if callable(getter) else []
        if rows:
            return rows
        agent = self._projected_agent_row(agent_id)
        return list(agent.get("_tools") or []) if isinstance(agent, dict) else []

    def get_skill(self, skill_id: str):
        return self._db.get_skill(skill_id) or self._projected_skill_row(skill_id)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)


def _get_extension_runtime_store(db) -> Any | None:
    try:
        from core.application.extensions.service_support import (
            get_default_extension_runtime_store,
        )

        bl = _platform_logger_for_admin_catalog(db)
        return get_default_extension_runtime_store(get_logger=lambda: bl)
    except Exception as exc:
        logger.debug("extension runtime store unavailable for workflow admin surface: %s", exc)
        return None


def _extension_workflow_projection_rows(
    db,
    *,
    enabled_only: bool = False,
    account_id: str = DEFAULT_ACCOUNT_ID,
) -> list[dict[str, Any]]:
    runtime_store = _get_extension_runtime_store(db)
    if runtime_store is None or not hasattr(runtime_store, "list_projections"):
        return []
    try:
        rows = runtime_store.list_projections(
            account_id=str(account_id or DEFAULT_ACCOUNT_ID).strip() or DEFAULT_ACCOUNT_ID,
            artifact_kind="workflow",
            only_active=enabled_only,
        )
    except Exception as exc:
        logger.warning("workflow projection lookup failed: %s", exc)
        return []

    normalized: list[dict[str, Any]] = []
    for row in rows or []:
        if hasattr(row, "model_dump"):
            projection = row.model_dump(mode="json")
        elif isinstance(row, dict):
            projection = dict(row)
        else:
            projection = {}
        snapshot = projection.get("projection")
        snapshot = dict(snapshot) if isinstance(snapshot, dict) else {}
        local_workflow_id = _normalize_text(
            snapshot.get("local_workflow_id")
            or snapshot.get("workflow_id")
            or projection.get("artifact_id")
        )
        install_id = _normalize_text(projection.get("install_id"))
        if not local_workflow_id or not install_id:
            continue
        workflow_id = projection_bridge_id(install_id, local_workflow_id)
        snapshot["workflow_id"] = workflow_id
        snapshot["source_install_id"] = _normalize_text(
            snapshot.get("source_install_id") or install_id
        )
        snapshot["source_release_id"] = _normalize_text(
            snapshot.get("source_release_id") or projection.get("release_id")
        )
        snapshot["projection_mode"] = _normalize_text(snapshot.get("projection_mode") or "derived")
        snapshot["derived_from_kind"] = _normalize_text(
            snapshot.get("derived_from_kind") or "extension_projection"
        )
        snapshot["derived_from_ref"] = _normalize_text(
            snapshot.get("derived_from_ref")
            or projection.get("artifact_ref")
            or projection.get("extension_id")
        )
        snapshot["source_projection_id"] = _normalize_text(
            snapshot.get("source_projection_id")
            or f"{projection.get('install_id')}:{projection.get('artifact_kind')}:{projection.get('artifact_id')}"
        )
        snapshot["local_workflow_id"] = local_workflow_id
        snapshot["sync_status"] = _normalize_text(
            snapshot.get("sync_status") or ("active" if projection.get("is_active") else "disabled")
        )
        snapshot["enabled"] = bool(snapshot.get("enabled", projection.get("is_active", True)))
        normalized.append(snapshot)
    return normalized


def _annotate_extension_workflow_availability(
    db,
    workflow: dict[str, Any],
    *,
    account_id: str = DEFAULT_ACCOUNT_ID,
) -> dict[str, Any]:
    payload = dict(workflow or {})
    install_id = _normalize_text(payload.get("source_install_id"))
    if not install_id:
        payload.setdefault("availability_status", "available")
        payload.setdefault("availability_reason_code", "none")
        payload.setdefault("availability_reason_text", "")
        payload.setdefault("new_runs_blocked", not bool(payload.get("enabled", True)))
        payload.setdefault("visible_in_catalog", True)
        return payload
    try:
        from core.application.extensions.install_availability_service import (
            InstallAvailabilityService,
        )

        service = InstallAvailabilityService(
            get_logger=lambda: _platform_logger_for_admin_catalog(db)
        )
        availability = service.get_install_availability(
            install_id,
            account_id=str(account_id or DEFAULT_ACCOUNT_ID).strip() or DEFAULT_ACCOUNT_ID,
        )
    except Exception as exc:
        logger.warning("extension workflow availability lookup failed for %s: %s", install_id, exc)
        availability = {}

    from core.application.extensions.projection_dependency_availability import (
        missing_extension_describer,
        resolve_projection_dependency_availability,
    )

    normalized_account_id = str(account_id or DEFAULT_ACCOUNT_ID).strip() or DEFAULT_ACCOUNT_ID
    dependency_availability = resolve_projection_dependency_availability(
        payload,
        active_extension_ids=_active_extension_ids_for_projection(
            db,
            account_id=normalized_account_id,
        ),
        active_extension_versions=_active_extension_versions_for_projection(
            db,
            account_id=normalized_account_id,
        ),
        describe_missing=missing_extension_describer(
            _platform_logger_for_admin_catalog(db), account_id=normalized_account_id
        ),
    )
    status = _normalize_text(availability.get("availability_status")).lower()
    reason_code = _normalize_text(availability.get("availability_reason_code")).lower()
    reason_text = _normalize_text(availability.get("availability_reason_text"))
    if status and status != "available":
        payload["enabled"] = False
        payload["availability_status"] = status
        payload["availability_reason_code"] = reason_code or "workflow_disabled"
        payload["availability_reason_text"] = reason_text
        payload["new_runs_blocked"] = bool(availability.get("new_runs_blocked", True))
        payload["visible_in_catalog"] = bool(availability.get("visible_in_catalog", True))
        # The install itself is the reason here, not a missing dependency.
        payload.pop("missing_extension_details", None)
    elif dependency_availability["availability_status"] != "available":
        payload["enabled"] = False
        payload.update(dependency_availability)
    elif payload.get("new_runs_blocked") and _normalize_text(
        payload.get("availability_status")
    ).lower() not in ("", "available"):
        # The projection already found a required extension missing; an
        # installed-set lookup that could not answer here must not clear it.
        payload["enabled"] = False
    else:
        payload["availability_status"] = "available"
        payload["availability_reason_code"] = "none"
        payload["availability_reason_text"] = ""
        payload["new_runs_blocked"] = False
        payload["visible_in_catalog"] = bool(payload.get("visible_in_catalog", True))
        payload.pop("missing_extension_details", None)
    return payload


def _active_extension_ids_for_projection(
    db: Any,
    *,
    account_id: str,
) -> set[str] | None:
    from core.application.extensions.projection_dependency_availability import (
        active_extension_ids_for_account,
    )

    return active_extension_ids_for_account(
        _platform_logger_for_admin_catalog(db),
        account_id=account_id,
    )


def _active_extension_versions_for_projection(
    db: Any,
    *,
    account_id: str,
) -> dict[str, set[str]] | None:
    from core.application.extensions.projection_dependency_availability import (
        active_extension_versions_for_account,
    )

    return active_extension_versions_for_account(
        _platform_logger_for_admin_catalog(db),
        account_id=account_id,
    )


def _get_extension_workflow(
    db,
    workflow_id: str,
    *,
    account_id: str = DEFAULT_ACCOUNT_ID,
) -> dict[str, Any] | None:
    normalized_workflow_id = _normalize_text(workflow_id)
    if not normalized_workflow_id:
        return None
    for row in _extension_workflow_projection_rows(
        db,
        enabled_only=False,
        account_id=account_id,
    ):
        if _normalize_text(row.get("workflow_id")) == normalized_workflow_id:
            return _annotate_extension_workflow_availability(db, row, account_id=account_id)
    return None


def _resolve_workflow_row(
    db,
    workflow_id: str,
    *,
    account_id: str = DEFAULT_ACCOUNT_ID,
) -> dict[str, Any] | None:
    from core.application.registry.workflow_lookup import get_workflow_row

    return get_workflow_row(db, workflow_id, account_id=account_id)


def _placeholder_workflow_canvas_graph(
    workflow: dict[str, Any],
    *,
    reason: str = "",
) -> dict[str, Any]:
    workflow_id = _normalize_text(workflow.get("workflow_id"))
    display_name = _normalize_text(workflow.get("display_name")) or workflow_id
    enabled = bool(workflow.get("enabled", False))
    interface_service = WorkflowInterfaceService()
    interface = interface_service.interface_from_definition(workflow)
    interface_contract = interface_service.public_contract_payload(interface)
    return {
        "workflow": {
            "id": workflow_id,
            "display_name": display_name,
            "enabled": enabled,
            "revision": workflow.get("revision"),
            "callable_by_parent": bool(interface.enabled)
            and "parent_workflow" in set(interface.trigger_channels),
        },
        "interface_contract": {
            "trigger_channels": interface_contract.get("trigger_channels") or [],
            "input_mode": interface_contract.get("input_mode") or "fields",
            "expected_inputs": interface_contract.get("expected_inputs") or [],
            "input_schema": interface_contract.get("input_schema") or {},
            "strict_inputs": bool(interface_contract.get("strict_inputs", True)),
        },
        "output_contract": {
            "output_mode": interface_contract.get("output_mode") or "none",
            "expected_outputs": interface_contract.get("expected_outputs") or [],
            "returned_files": interface_contract.get("returned_files") or [],
        },
        "entry": None,
        "groups": [],
        "steps": [],
        "edges": [],
        "validation_issues": [],
        "automation_rules": [],
        "protected_workflow": True,
        "availability_status": _normalize_text(workflow.get("availability_status") or "available"),
        "availability_reason_code": _normalize_text(workflow.get("availability_reason_code") or ""),
        "availability_reason_text": _normalize_text(
            workflow.get("availability_reason_text") or reason
        ),
    }


def _extract_callable_contract(settings: Any) -> tuple[bool, list[dict], list[dict]]:
    interface = WorkflowInterfaceService().interface_from_settings(settings)
    return (
        bool(interface.enabled),
        [row.dump() for row in interface.expected_inputs],
        [row.dump() for row in interface.expected_outputs],
    )


def _normalize_contract_rows(rows: Any) -> list[dict]:
    normalized: list[dict] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        normalized.append(
            {
                **{
                    key: value
                    for key, value in row.items()
                    if key
                    in {
                        "label",
                        "options",
                        "contract_id",
                    }
                    and value not in (None, "", [])
                },
                "name": name,
                "type": str(row.get("type") or "string").strip().lower() or "string",
                "required": bool(row.get("required", False)),
                "description": str(row.get("description") or "").strip(),
            }
        )
    return normalized


def _get_workflow_validation(
    db,
    workflow_id: str,
    *,
    account_id: str = DEFAULT_ACCOUNT_ID,
) -> dict:
    workflow = _resolve_workflow_row(db, workflow_id, account_id=account_id)
    if not isinstance(workflow, dict):
        return validate_workflow_readiness(db, workflow_id)
    if isinstance(db.get_workflow(workflow_id), dict):
        return validate_workflow_readiness(db, workflow_id)
    return validate_workflow_readiness(
        _WorkflowValidationDBView(
            db,
            workflow_id,
            workflow,
            account_id=account_id,
        ),
        workflow_id,
    )


def _get_step_test_run_validation(
    db,
    workflow_id: str,
    step_id: str,
    *,
    account_id: str = DEFAULT_ACCOUNT_ID,
) -> dict:
    from core.application.registry.active_workflow_dependencies import (
        workflow_registry_tool_issues,
    )
    from core.application.workflows.platform_step_test_run_helpers import (
        scope_workflow_validation_for_step,
    )

    workflow = _resolve_workflow_row(db, workflow_id, account_id=account_id)
    if not isinstance(workflow, dict):
        return _get_workflow_validation(db, workflow_id, account_id=account_id)
    target_step_id = _normalize_text(step_id)
    raw_steps = workflow.get("steps")
    step = next(
        (
            item
            for item in (raw_steps if isinstance(raw_steps, list) else [])
            if isinstance(item, dict) and _normalize_text(item.get("step_id")) == target_step_id
        ),
        None,
    )
    if step is None:
        return {
            "valid": False,
            "errors": [
                {
                    "code": "workflow.step_not_found",
                    "message": f"Step '{step_id}' not found",
                    "path": "workflow.steps",
                }
            ],
            "warnings": [],
        }
    selected_workflow = {**workflow, "steps": [dict(step)], "groups": []}
    canonical_workflow = db.get_workflow(workflow_id)
    validation_db = (
        db
        if isinstance(canonical_workflow, dict)
        else _WorkflowValidationDBView(db, workflow_id, workflow, account_id=account_id)
    )
    return scope_workflow_validation_for_step(
        _get_workflow_validation(db, workflow_id, account_id=account_id),
        step_id=target_step_id,
        selected_registry_tool_issues=workflow_registry_tool_issues(
            validation_db,
            selected_workflow,
            account_id=account_id,
        ),
    )


def _workflow_validation_payload_for_admin(
    db,
    workflow_id: str,
    *,
    account_id: str = DEFAULT_ACCOUNT_ID,
) -> dict[str, Any]:
    workflow = _resolve_workflow_row(db, workflow_id, account_id=account_id)
    try:
        validation = _get_workflow_validation(db, workflow_id, account_id=account_id)
    except DATABASE_OPERATION_ERRORS as exc:
        logger.warning(
            "Workflow contract validation temporarily unavailable for '%s': %s",
            workflow_id,
            exc,
        )
        validation = {
            "valid": False,
            "errors": [
                {
                    "code": "workflow.validation_temporarily_unavailable",
                    "path": "workflow",
                    "message": (
                        "Contract validation is temporarily unavailable due to high load. "
                        "Retry in a few seconds."
                    ),
                }
            ],
            "warnings": [],
        }
    service = _workflow_operator_service(db=db)
    issues = service._workflow_validation_issues(workflow, validation)
    return {
        "workflow_id": str(workflow_id or "").strip(),
        "validation": validation,
        "issues": issues,
    }


def _workflow_suggestions_payload_for_admin(
    db,
    workflow_id: str,
    *,
    account_id: str = DEFAULT_ACCOUNT_ID,
) -> dict[str, Any]:
    validation_payload = _workflow_validation_payload_for_admin(
        db,
        workflow_id,
        account_id=account_id,
    )
    service = _workflow_operator_service(db=db)
    return {
        "workflow_id": str(workflow_id or "").strip(),
        "suggestions": service._workflow_repair_suggestions(
            workflow_id,
            list(validation_payload.get("issues") or []),
        ),
    }


def _normalize_disabled_workflow_validation(
    validation: dict | None, *, final_enabled: bool
) -> dict:
    return normalize_disabled_workflow_validation(validation, final_enabled=final_enabled)


__all__ = [
    "_WorkflowValidationDBView",
    "_get_extension_runtime_store",
    "_extension_workflow_projection_rows",
    "_annotate_extension_workflow_availability",
    "_get_extension_workflow",
    "_resolve_workflow_row",
    "_placeholder_workflow_canvas_graph",
    "_extract_callable_contract",
    "_normalize_contract_rows",
    "_get_workflow_validation",
    "_get_step_test_run_validation",
    "_workflow_validation_payload_for_admin",
    "_workflow_suggestions_payload_for_admin",
    "_normalize_disabled_workflow_validation",
]
