"""Existing dependency and persisted reference visibility checks for draft preparation."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from core.application.management.errors import ServiceNotFoundError
from core.application.management.scope import ScopeContext
from core.application.workflows.authoring.catalog import WorkflowAuthoringCatalog
from core.application.workflows.workflow_portability import (
    WorkflowPortabilityError,
    WorkflowPortabilityService,
    _connection_refs,
)
from core.application.workflows.workflow_step_config_contracts import STEP_CONFIG_ALLOWED_KEYS

ALLOWED_RESOLUTION_ACTIONS = frozenset({"use_existing", "map_existing"})
_RESOLUTION_TYPES = frozenset(
    {
        "agent",
        "skill",
        "tool",
        "child_workflow",
        "published_block",
        "extension",
        "project_connection",
    }
)


class DraftReferences:
    """Share reference policy checks between validation and disabled creation."""

    db: Any
    catalog: WorkflowAuthoringCatalog
    connection_store: Any

    @staticmethod
    def _check_definition_shapes(workflow: dict[str, Any]) -> None:
        def check_steps(rows: Any, path: str) -> None:
            if not isinstance(rows, list):
                fail(path, "list")
            for index, step in enumerate(rows):
                if not isinstance(step, dict):
                    fail(f"{path}[{index}]")
                step_path = f"{path}[{index}]"
                override = step.get("override")
                if override is None:
                    continue
                if not isinstance(override, dict):
                    fail(f"{step_path}.override")
                for kind in STEP_CONFIG_ALLOWED_KEYS:
                    if kind not in override:
                        continue
                    config = override[kind]
                    if config is None:
                        continue
                    config_path = f"{step_path}.override.{kind}"
                    if not isinstance(config, dict):
                        fail(config_path)
                    if kind == "batch" and "steps" in config:
                        if not isinstance(config["steps"], list):
                            fail(f"{config_path}.steps", "list")
                        check_steps(config["steps"], f"{config_path}.steps")

        def fail(path: str, expected: str = "object") -> None:
            raise WorkflowPortabilityError(
                f"Step configuration must be an {expected}",
                code="authoring.step_config_shape_invalid",
                errors=[
                    {
                        "code": "authoring.step_config_shape_invalid",
                        "path": path,
                        "message": f"Provide an {expected} at this configuration field.",
                    }
                ],
            )

        for field, expected_type in (
            ("settings", dict),
            ("trigger", dict),
            ("participants", list),
            ("tags", list),
            ("steps", list),
            ("groups", list),
            ("for_each", list),
            ("execution_graph", dict),
        ):
            if field in workflow and not isinstance(workflow[field], expected_type):
                fail(f"workflows[0].{field}", "object" if expected_type is dict else "list")
        for field in ("phases", "_phases"):
            if field in workflow:
                fail(f"workflows[0].{field}", "steps and groups list (phases are not supported)")
        for index, participant in enumerate(workflow.get("participants") or []):
            if not isinstance(participant, dict):
                fail(f"workflows[0].participants[{index}]")
        if "steps" in workflow:
            check_steps(workflow["steps"], "workflows[0].steps")
        for collection in ("groups", "for_each"):
            for index, item in enumerate(workflow.get(collection) or []):
                if not isinstance(item, dict):
                    fail(f"workflows[0].{collection}[{index}]")

    @staticmethod
    def _check_resolutions(resolutions: list[dict[str, Any]]) -> None:
        seen = set()
        for index, row in enumerate(resolutions):
            if not isinstance(row, dict) or row.get("action") not in ALLOWED_RESOLUTION_ACTIONS:
                raise WorkflowPortabilityError(
                    "Only use_existing and map_existing resolutions are supported",
                    code="authoring.resolution_action_unsupported",
                    errors=[
                        {
                            "path": f"resolutions[{index}].action",
                            "code": "authoring.resolution_action_unsupported",
                            "message": "Use use_existing or map_existing for a permitted installed target.",
                        }
                    ],
                )
            key = str(row.get("dependency_key") or "")
            kind, _, ref = (
                key.partition(":")
                if ":" in key
                else (
                    str(row.get("dependency_type") or row.get("type") or ""),
                    "",
                    str(row.get("ref") or row.get("dependency_ref") or ""),
                )
            )
            if kind not in _RESOLUTION_TYPES or not ref or (kind, ref) in seen:
                raise WorkflowPortabilityError(
                    "Resolution must identify one supported dependency",
                    code="authoring.resolution_invalid",
                    errors=[
                        {
                            "path": f"resolutions[{index}]",
                            "code": "authoring.resolution_invalid",
                            "message": "Identify a supported dependency exactly once.",
                        }
                    ],
                )
            seen.add((kind, ref))

    def _dependency_issues(
        self,
        portability: WorkflowPortabilityService,
        scope: ScopeContext,
        project_id: str,
        preflight: dict[str, Any],
        resolutions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        issues = []
        for row in preflight["dependencies"]:
            if row["blocking"]:
                metadata = row.get("metadata") or {}
                issues.append(
                    {
                        "code": metadata.get("code") or f"authoring.dependency_{row['status']}",
                        "path": (row.get("sources") or ["dependencies"])[0],
                        "message": row.get("message")
                        or "Resolve this dependency to a permitted installed target.",
                        "severity": "error",
                        "blocking": True,
                        "repair_guidance": "Use a permitted existing target or repair the source definition.",
                    }
                )
            elif row["dependency_type"] not in {"workflow", "bundle"}:
                resolved = row.get("resolved_by") or {}
                target = str(
                    resolved.get("target_ref")
                    or resolved.get("target_id")
                    or resolved.get("target_connection_id")
                    or resolved.get("target_install_id")
                    or resolved.get("target_workflow_id")
                    or row["ref"]
                )
                issues.extend(
                    self._reference_issues(
                        portability,
                        scope,
                        project_id,
                        row["dependency_type"],
                        target,
                        (row.get("sources") or ["dependencies"])[0],
                    )
                )
        # Even unused resolution targets must not bypass visibility policy.
        for (kind, ref), resolution in portability._resolution_index(resolutions).items():
            target = str(
                resolution.get("target_ref")
                or resolution.get("target_id")
                or resolution.get("target_connection_id")
                or resolution.get("target_install_id")
                or resolution.get("target_workflow_id")
                or ref
            )
            issues.extend(
                self._reference_issues(portability, scope, project_id, kind, target, "resolutions")
            )
        return issues

    def _reference_issues(
        self,
        portability: WorkflowPortabilityService,
        scope: ScopeContext,
        project_id: str,
        kind: str,
        ref: str,
        path: str,
    ) -> list[dict[str, Any]]:
        try:
            if kind in {"agent", "skill", "tool", "child_workflow", "dataset"}:
                self.catalog.visible_row(
                    scope, project_id, "workflow" if kind == "child_workflow" else kind, ref
                )
            elif kind == "extension":
                if not portability._existing_install_in_account(ref):
                    raise ServiceNotFoundError("Install not permitted")
            elif kind == "project_connection":
                getter = getattr(self.connection_store, "get_project_connection", None)
                row = getter(connection_id=ref, project_id=project_id) if callable(getter) else None
                if (
                    not isinstance(row, dict)
                    or str(row.get("project_id") or "") != project_id
                    or (row.get("account_id") and row["account_id"] != scope.account_id)
                ):
                    raise ServiceNotFoundError("Connection not permitted")
            elif kind == "published_block":
                getter = getattr(self.db, "get_published_block_contract", None)
                row = getter(ref, account_id=scope.account_id) if callable(getter) else None
                if not isinstance(row, dict) or not self.catalog.visibility._row_visible(
                    {**row, "id": ref}, capability_type=kind, scope=scope, project_id=project_id
                ):
                    raise ServiceNotFoundError("Block not permitted")
                workflow_id = str(row.get("workflow_id") or "")
                if workflow_id:
                    self.catalog.visible_row(scope, project_id, "workflow", workflow_id)
        except ServiceNotFoundError:
            return [
                {
                    "code": "authoring.reference_not_permitted",
                    "path": path,
                    "message": "Reference is not available in the selected account/project scope.",
                    "severity": "error",
                    "blocking": True,
                    "repair_guidance": "Search for a permitted existing component in this project.",
                }
            ]
        return []

    def _step_scope_issues(
        self,
        portability: WorkflowPortabilityService,
        scope: ScopeContext,
        project_id: str,
        payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        issues = []
        for participant in payload["participants"]:
            agent_id = str(participant.get("agent_id") or participant.get("agent") or "")
            if agent_id:
                issues.extend(
                    self._reference_issues(
                        portability, scope, project_id, "agent", agent_id, "workflow.participants"
                    )
                )
        for step in self._steps(payload.get("steps") or []):
            for field, kind in (
                ("agent_id", "agent"),
                ("skill_id", "skill"),
                ("connector_id", "tool"),
                ("workflow_id", "child_workflow"),
                ("block_key", "published_block"),
            ):
                ref = str(step.get(field) or "")
                if ref:
                    issues.extend(
                        self._reference_issues(
                            portability,
                            scope,
                            project_id,
                            kind,
                            ref,
                            f"workflow.steps.{step.get('step_id', '')}.{field}",
                        )
                    )
            override = step.get("override") or {}
            connector = override.get("connector") or {}
            for location, binding in (
                ("step.connection_binding_jsonb", step.get("connection_binding_jsonb")),
                ("step.connection_binding", step.get("connection_binding")),
                ("override.connection_binding", override.get("connection_binding")),
                ("override.connector.connection_binding", connector.get("connection_binding")),
                (
                    "override.connector.connection_binding_jsonb",
                    connector.get("connection_binding_jsonb"),
                ),
            ):
                if binding:
                    for connection_id in _connection_refs(binding):
                        issues.extend(
                            self._reference_issues(
                                portability,
                                scope,
                                project_id,
                                "project_connection",
                                connection_id,
                                f"workflow.steps.{step.get('step_id', '')}.{location}",
                            )
                        )
            invoke = override.get("invoke_workflow") or {}
            if invoke.get("workflow_id"):
                issues.extend(
                    self._reference_issues(
                        portability,
                        scope,
                        project_id,
                        "child_workflow",
                        invoke["workflow_id"],
                        "workflow.steps.override.invoke_workflow.workflow_id",
                    )
                )
            dataset = override.get("dataset_query") or {}
            if dataset.get("dataset_id"):
                issues.extend(
                    self._reference_issues(
                        portability,
                        scope,
                        project_id,
                        "dataset",
                        dataset["dataset_id"],
                        "workflow.steps.override.dataset_query.dataset_id",
                    )
                )
        return issues

    @staticmethod
    def _steps(rows: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
        for row in rows:
            if not isinstance(row, dict):
                continue
            yield row
            override = row.get("override") or {}
            config = override.get("batch") or {}
            yield from DraftReferences._steps(config.get("steps") or [])
