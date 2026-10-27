# mypy: ignore-errors
from __future__ import annotations

import json
from collections.abc import (
    Mapping,
)
from typing import (
    Any,
)

from core.application.registry.projected_artifact_rows import _durable_projection_install_id

from .capability_contracts import (
    contract_field_names,
    tool_expected_inputs,
    tool_expected_outputs,
    tool_operations,
)
from .capability_discovery_helpers import (
    ScopedCapabilityRef,
    _build_candidate,
    _builder_match_fields,
    _builder_metadata,
    _builder_summary,
    _combined_summary,
    _dataset_column_names,
    _json_safe_catalog_value,
    _recommended_use,
    _scope_value,
    _score_match,
    _string_list,
    _tool_operation_display_name,
    _visible_capability,
    hashlib,
)
from .contract_fields import (
    _candidate_contract_field_names,
    _text,
    _workflow_contract_field_names,
)
from .models import (
    CapabilityCandidate,
)


class CapabilityDiscoveryCatalogMixin:
    def _candidate_from_capability(
        self,
        *,
        capability_id: str,
        capability_type: str,
        display_name: str,
        operation_id: str = "",
        function_name: str = "",
        operation_display_name: str = "",
        operation_summary: str = "",
        confidence: float,
        why_matched: str,
        capability_summary: str = "",
        step_kinds: list[str] | None = None,
        input_names: list[str] | None = None,
        input_contracts: list[dict[str, Any]] | None = None,
        output_names: list[str] | None = None,
        skill_names: list[str] | None = None,
    ) -> CapabilityCandidate:
        if capability_type == "tool":
            tool = self.db.get_tool(capability_id) if hasattr(self.db, "get_tool") else None
            tool_rows_source = tool if isinstance(tool, dict) else None
            resolved_operation_id = _text(operation_id or function_name)
            operation_input_names = contract_field_names(
                operation_input_contracts := tool_expected_inputs(
                    tool_rows_source,
                    operation_id=resolved_operation_id,
                    function_name=_text(function_name),
                )
            )
            operation_output_names = contract_field_names(
                tool_expected_outputs(
                    tool_rows_source,
                    operation_id=resolved_operation_id,
                    function_name=_text(function_name),
                )
            )
            if not capability_summary and operation_summary:
                capability_summary = operation_summary
            if not input_names and operation_input_names:
                input_names = operation_input_names
            if not input_contracts and operation_input_contracts:
                input_contracts = [dict(row) for row in operation_input_contracts[:8]]
            if not output_names and operation_output_names:
                output_names = operation_output_names
        elif capability_type == "workflow":
            workflow = (
                self.db.get_workflow(capability_id) if hasattr(self.db, "get_workflow") else None
            )
            workflow_source = workflow if isinstance(workflow, dict) else None
            if not input_names:
                input_names = _workflow_contract_field_names(workflow_source, kind="input")
            if not output_names:
                output_names = _workflow_contract_field_names(workflow_source, kind="output")
        return _build_candidate(
            capability_id=capability_id,
            capability_type=capability_type,
            display_name=display_name,
            operation_id=operation_id,
            function_name=function_name,
            operation_display_name=operation_display_name,
            operation_summary=operation_summary,
            why_matched=why_matched,
            recommended_use=_recommended_use(capability_type),
            confidence=confidence,
            capability_summary=capability_summary,
            step_kinds=step_kinds,
            input_names=input_names,
            input_contracts=input_contracts,
            output_names=output_names,
            skill_names=skill_names,
        )

    def _catalog_sync_digest(
        self,
        *,
        scope: Mapping[str, Any] | Any | None = None,
        project_id: str | None = None,
    ) -> str:
        payload = {
            "scope": {
                "account_id": _scope_value(scope or {}, "account_id"),
                "tenant_id": _scope_value(scope or {}, "tenant_id"),
                "project_id": _text(project_id) or _scope_value(scope or {}, "project_id"),
            },
            "workflows": self._catalog_rows(
                self._scoped_catalog.workflow_summaries(scope=scope or {}, project_id=project_id),
                "workflow_id",
            ),
            "agents": self._catalog_rows(
                self._scoped_catalog.agent_summaries(scope=scope or {}, project_id=project_id),
                "agent_id",
            ),
            "skills": self._catalog_rows(
                self._scoped_catalog.skill_summaries(scope=scope or {}, project_id=project_id),
                "skill_id",
            ),
            "tools": self._catalog_rows(
                self._scoped_catalog.tool_summaries(scope=scope or {}, project_id=project_id),
                "tool_id",
            ),
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()

    def _catalog_rows(
        self,
        rows: list[dict[str, Any]] | list[Mapping[str, Any]],
        id_key: str,
    ) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping) or not _visible_capability(dict(row)):
                continue
            row_id = _text(row.get(id_key))
            if not row_id:
                continue
            normalized.append(
                {str(key): _json_safe_catalog_value(row.get(key)) for key in sorted(row.keys())}
            )
        normalized.sort(key=lambda item: _text(item.get(id_key)))
        return normalized

    def _workflow_candidates(
        self,
        workflow_id: str,
        request_tokens: set[str],
        *,
        scope: Mapping[str, Any] | None = None,
        project_id: str | None = None,
        hydrate_limit: int = 24,
    ) -> list[CapabilityCandidate]:
        rows = self._scoped_catalog.workflow_summaries(scope=scope or {}, project_id=project_id)
        scored_rows: list[tuple[float, dict[str, Any], str, str]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if not _visible_capability(row):
                continue
            candidate_id = _text(row.get("workflow_id"))
            if not candidate_id or candidate_id == workflow_id:
                continue
            display_name = _text(row.get("display_name") or candidate_id)
            builder_fields = _builder_match_fields(row.get("builder_metadata_jsonb"))
            confidence = _score_match(
                request_tokens,
                candidate_id,
                display_name,
                row.get("description"),
                row.get("description_preview"),
                row.get("tags"),
                *builder_fields,
            )
            if confidence <= 0:
                continue
            scored_rows.append((confidence, row, candidate_id, display_name))
        scored_rows.sort(key=lambda item: (-item[0], item[3]))
        hydrated = self._scoped_catalog.hydrate_visible_capabilities(
            scope=scope or {},
            project_id=project_id,
            capability_refs=[
                ScopedCapabilityRef(capability_type="workflow", capability_id=candidate_id)
                for _, _, candidate_id, _ in scored_rows[:hydrate_limit]
            ],
        )
        candidates: list[CapabilityCandidate] = []
        for confidence, row, candidate_id, display_name in scored_rows[:hydrate_limit]:
            workflow_detail = hydrated.get(("workflow", candidate_id, ""))
            candidates.append(
                _build_candidate(
                    capability_id=candidate_id,
                    capability_type="workflow",
                    display_name=display_name,
                    composition_role=_text(
                        _builder_metadata(row.get("builder_metadata_jsonb")).get("composition_role")
                    ),
                    why_matched=f"Matched workflow keywords against '{display_name}'.",
                    recommended_use=_recommended_use("workflow"),
                    confidence=confidence,
                    capability_summary=_combined_summary(
                        _text(row.get("description")) or _text(row.get("description_preview")),
                        _builder_summary(row.get("builder_metadata_jsonb")),
                    ),
                    input_names=_workflow_contract_field_names(workflow_detail, kind="input")[:8],
                    output_names=_workflow_contract_field_names(workflow_detail, kind="output")[:8],
                    skill_names=_string_list(row.get("tags")),
                )
            )
        return candidates

    def _agent_candidates(
        self,
        request_tokens: set[str],
        *,
        scope: Mapping[str, Any] | None = None,
        project_id: str | None = None,
        hydrate_limit: int = 24,
    ) -> list[CapabilityCandidate]:
        rows = self._scoped_catalog.agent_summaries(scope=scope or {}, project_id=project_id)
        scored_rows: list[tuple[float, dict[str, Any], str, str]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            candidate_id = _text(row.get("agent_id"))
            display_name = _text(row.get("display_name") or candidate_id)
            builder_fields = _builder_match_fields(row.get("builder_metadata_jsonb"))
            confidence = _score_match(
                request_tokens,
                candidate_id,
                display_name,
                row.get("description"),
                row.get("description_preview"),
                row.get("role_type"),
                row.get("agent_type_key"),
                row.get("skill_names"),
                *builder_fields,
            )
            if confidence > 0:
                scored_rows.append((confidence, row, candidate_id, display_name))
        scored_rows.sort(key=lambda item: (-item[0], item[3]))
        hydrated = self._scoped_catalog.hydrate_visible_capabilities(
            scope=scope or {},
            project_id=project_id,
            capability_refs=[
                ScopedCapabilityRef(capability_type="agent", capability_id=candidate_id)
                for _, _, candidate_id, _ in scored_rows[:hydrate_limit]
            ],
        )
        candidates: list[CapabilityCandidate] = []
        for confidence, row, candidate_id, display_name in scored_rows[:hydrate_limit]:
            agent_detail = hydrated.get(("agent", candidate_id, ""))
            if agent_detail is None and _durable_projection_install_id(row):
                continue
            if agent_detail is None:
                skill_rows = []
            elif _durable_projection_install_id(agent_detail):
                skill_rows = agent_detail.get("_skills") or agent_detail.get("skills") or []
            else:
                skill_rows = (
                    self.db.get_agent_skills(candidate_id)
                    if hasattr(self.db, "get_agent_skills")
                    else []
                )
            skill_names = [
                _text((skill_row or {}).get("skill_id"))
                for skill_row in (skill_rows or [])
                if _text((skill_row or {}).get("skill_id"))
            ]
            input_names = _candidate_contract_field_names(agent_detail, kind="input")
            output_names = _candidate_contract_field_names(agent_detail, kind="output")
            if (not input_names or not output_names) and skill_names:
                merged_inputs = list(input_names)
                merged_outputs = list(output_names)
                for skill_id in skill_names:
                    try:
                        skill_detail = self._scoped_catalog.assert_capability_visible(
                            scope=scope or {},
                            project_id=project_id,
                            capability_ref=ScopedCapabilityRef(
                                capability_type="skill", capability_id=skill_id
                            ),
                        )
                    except ValueError:
                        continue
                    for name in _candidate_contract_field_names(skill_detail, kind="input"):
                        if name and name not in merged_inputs:
                            merged_inputs.append(name)
                    for name in _candidate_contract_field_names(skill_detail, kind="output"):
                        if name and name not in merged_outputs:
                            merged_outputs.append(name)
                input_names = merged_inputs
                output_names = merged_outputs
            candidates.append(
                _build_candidate(
                    capability_id=candidate_id,
                    capability_type="agent",
                    display_name=display_name,
                    composition_role=_text(
                        _builder_metadata(row.get("builder_metadata_jsonb")).get("composition_role")
                    ),
                    why_matched=f"Matched agent metadata for '{display_name}'.",
                    recommended_use=_recommended_use("agent"),
                    confidence=confidence,
                    capability_summary=_combined_summary(
                        _text(row.get("description")) or _text(row.get("description_preview")),
                        _builder_summary(row.get("builder_metadata_jsonb")),
                    ),
                    input_names=input_names,
                    output_names=output_names,
                    skill_names=skill_names or _string_list(row.get("skill_names")),
                    # Structured eligibility metadata (Create #10).
                    semantic_roles=_string_list(
                        _builder_metadata(row.get("builder_metadata_jsonb")).get("semantic_roles")
                    ),
                    anti_use_cases=_string_list(
                        _builder_metadata(row.get("builder_metadata_jsonb")).get("anti_use_cases")
                    ),
                )
            )
        return candidates

    def _skill_candidates(
        self,
        request_tokens: set[str],
        *,
        scope: Mapping[str, Any] | None = None,
        project_id: str | None = None,
        hydrate_limit: int = 24,
    ) -> list[CapabilityCandidate]:
        rows = self._scoped_catalog.skill_summaries(scope=scope or {}, project_id=project_id)
        scored_rows: list[tuple[float, dict[str, Any], str, str]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            candidate_id = _text(row.get("skill_id"))
            display_name = _text(row.get("display_name") or candidate_id)
            builder_fields = _builder_match_fields(row.get("builder_metadata_jsonb"))
            confidence = _score_match(
                request_tokens,
                candidate_id,
                display_name,
                row.get("description"),
                row.get("description_preview"),
                row.get("category"),
                *builder_fields,
            )
            if confidence > 0:
                scored_rows.append((confidence, row, candidate_id, display_name))
        scored_rows.sort(key=lambda item: (-item[0], item[3]))
        hydrated = self._scoped_catalog.hydrate_visible_capabilities(
            scope=scope or {},
            project_id=project_id,
            capability_refs=[
                ScopedCapabilityRef(capability_type="skill", capability_id=candidate_id)
                for _, _, candidate_id, _ in scored_rows[:hydrate_limit]
            ],
        )
        candidates: list[CapabilityCandidate] = []
        for confidence, row, candidate_id, display_name in scored_rows[:hydrate_limit]:
            skill_detail = hydrated.get(("skill", candidate_id, ""))
            if skill_detail is None and _durable_projection_install_id(row):
                continue
            candidates.append(
                _build_candidate(
                    capability_id=candidate_id,
                    capability_type="skill",
                    display_name=display_name,
                    composition_role=_text(
                        _builder_metadata(row.get("builder_metadata_jsonb")).get("composition_role")
                    ),
                    why_matched=f"Matched skill metadata for '{display_name}'.",
                    recommended_use=_recommended_use("skill"),
                    confidence=confidence,
                    capability_summary=_combined_summary(
                        _text(row.get("description")) or _text(row.get("description_preview")),
                        _builder_summary(row.get("builder_metadata_jsonb")),
                    ),
                    input_names=_candidate_contract_field_names(skill_detail, kind="input"),
                    output_names=_candidate_contract_field_names(skill_detail, kind="output"),
                    # Structured eligibility metadata (Create #10).
                    semantic_roles=_string_list(
                        _builder_metadata(row.get("builder_metadata_jsonb")).get("semantic_roles")
                    ),
                    anti_use_cases=_string_list(
                        _builder_metadata(row.get("builder_metadata_jsonb")).get("anti_use_cases")
                    ),
                )
            )
        return candidates

    def _tool_candidates(
        self,
        request_tokens: set[str],
        *,
        scope: Mapping[str, Any] | None = None,
        project_id: str | None = None,
        hydrate_limit: int = 24,
    ) -> list[CapabilityCandidate]:
        rows = self._scoped_catalog.tool_summaries(scope=scope or {}, project_id=project_id)
        scored_rows: list[tuple[float, dict[str, Any], str, str]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            candidate_id = _text(row.get("tool_id"))
            display_name = _text(row.get("display_name") or candidate_id)
            builder_fields = _builder_match_fields(row.get("builder_metadata_jsonb"))
            confidence = _score_match(
                request_tokens,
                candidate_id,
                display_name,
                row.get("description"),
                row.get("description_preview"),
                row.get("category"),
                row.get("impl_type"),
                *builder_fields,
            )
            if confidence > 0:
                scored_rows.append((confidence, row, candidate_id, display_name))
        scored_rows.sort(key=lambda item: (-item[0], item[3]))
        hydrated = self._scoped_catalog.hydrate_visible_capabilities(
            scope=scope or {},
            project_id=project_id,
            capability_refs=[
                ScopedCapabilityRef(capability_type="tool", capability_id=candidate_id)
                for _, _, candidate_id, _ in scored_rows[:hydrate_limit]
            ],
        )
        candidates: list[CapabilityCandidate] = []
        for confidence, row, candidate_id, display_name in scored_rows[:hydrate_limit]:
            tool_detail = hydrated.get(("tool", candidate_id, ""))
            tool_source = tool_detail if isinstance(tool_detail, dict) else row
            operations = tool_operations(tool_source if isinstance(tool_source, dict) else None)
            multi_operation = len(operations) > 1
            row_candidates = 0
            if operations:
                for operation in operations:
                    operation_id = _text(
                        operation.get("operation_id") or operation.get("function_name")
                    )
                    operation_display_name = _text(operation.get("display_name") or operation_id)
                    operation_description = _text(operation.get("description"))
                    operation_input_rows = (
                        operation.get("input_rows")
                        if isinstance(operation.get("input_rows"), list)
                        else []
                    )
                    operation_confidence = _score_match(
                        request_tokens,
                        candidate_id,
                        display_name,
                        operation_id,
                        operation_display_name,
                        operation_description,
                        " ".join(
                            contract_field_names(
                                operation.get("input_rows")
                                if isinstance(operation.get("input_rows"), list)
                                else []
                            )
                        ),
                        " ".join(
                            contract_field_names(
                                operation.get("output_rows")
                                if isinstance(operation.get("output_rows"), list)
                                else []
                            )
                        ),
                        row.get("description"),
                        row.get("description_preview"),
                        row.get("category"),
                        row.get("impl_type"),
                        *builder_fields,
                    )
                    if operation_confidence <= 0:
                        continue
                    candidates.append(
                        _build_candidate(
                            capability_id=candidate_id,
                            capability_type="tool",
                            display_name=_tool_operation_display_name(
                                display_name,
                                operation_display_name or operation_id,
                                multi_operation=multi_operation,
                            ),
                            composition_role=_text(
                                _builder_metadata(row.get("builder_metadata_jsonb")).get(
                                    "composition_role"
                                )
                            ),
                            operation_id=operation_id,
                            function_name=operation_id,
                            operation_display_name=operation_display_name or operation_id,
                            operation_summary=operation_description,
                            why_matched=(
                                f"Matched deterministic tool operation '{operation_display_name or operation_id}' within '{display_name}'."
                            ),
                            recommended_use=_recommended_use("tool"),
                            confidence=operation_confidence,
                            capability_summary=_combined_summary(
                                _text(row.get("description"))
                                or _text(row.get("description_preview")),
                                operation_description,
                                _builder_summary(row.get("builder_metadata_jsonb")),
                            ),
                            input_names=contract_field_names(operation_input_rows)[:8],
                            input_contracts=[
                                dict(row)
                                for row in operation_input_rows[:8]
                                if isinstance(row, dict)
                            ],
                            output_names=contract_field_names(
                                operation.get("output_rows")
                                if isinstance(operation.get("output_rows"), list)
                                else []
                            )[:8],
                        )
                    )
                    row_candidates += 1
                if row_candidates:
                    continue
            candidates.append(
                _build_candidate(
                    capability_id=candidate_id,
                    capability_type="tool",
                    display_name=display_name,
                    composition_role=_text(
                        _builder_metadata(row.get("builder_metadata_jsonb")).get("composition_role")
                    ),
                    why_matched=(
                        f"Matched deterministic tool/action metadata for '{display_name}'."
                    ),
                    recommended_use=_recommended_use("tool"),
                    confidence=confidence,
                    capability_summary=_combined_summary(
                        _text(row.get("description")) or _text(row.get("description_preview")),
                        _builder_summary(row.get("builder_metadata_jsonb")),
                    ),
                    input_names=contract_field_names(tool_expected_inputs(tool_source))[:8],
                    input_contracts=[
                        dict(row)
                        for row in tool_expected_inputs(tool_source)[:8]
                        if isinstance(row, dict)
                    ],
                    output_names=contract_field_names(tool_expected_outputs(tool_source))[:8],
                )
            )
        return candidates

    def _dataset_candidates(
        self,
        request_tokens: set[str],
        *,
        scope: Mapping[str, Any] | None = None,
        project_id: str | None = None,
        hydrate_limit: int = 24,
    ) -> list[CapabilityCandidate]:
        _ = scope
        list_datasets = getattr(self.db, "list_datasets", None)
        if not callable(list_datasets):
            return []
        try:
            rows = list_datasets(enabled_only=True, project_id=project_id)
        except TypeError:
            rows = list_datasets(enabled_only=True)
        scored_rows: list[tuple[float, dict[str, Any], str, str]] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            candidate_id = _text(row.get("dataset_id"))
            if not candidate_id:
                continue
            display_name = _text(row.get("display_name") or candidate_id)
            confidence = _score_match(
                request_tokens,
                candidate_id,
                display_name,
                row.get("description"),
                row.get("query_language"),
                row.get("connector_kind"),
                row.get("runtime_kind"),
                " ".join(_string_list(row.get("tags"))),
            )
            if confidence > 0:
                scored_rows.append((confidence, row, candidate_id, display_name))
        scored_rows.sort(key=lambda item: (-item[0], item[3]))
        candidates: list[CapabilityCandidate] = []
        for confidence, row, candidate_id, display_name in scored_rows[:hydrate_limit]:
            candidates.append(
                _build_candidate(
                    capability_id=candidate_id,
                    capability_type="dataset",
                    display_name=display_name,
                    why_matched=f"Matched dataset metadata for '{display_name}'.",
                    recommended_use="Read rows from this dataset with a dataset_query step.",
                    confidence=confidence,
                    capability_summary=_text(row.get("description")),
                    step_kinds=["dataset_query"],
                    output_names=_dataset_column_names(row.get("schema_snapshot_jsonb"))[:8],
                )
            )
        return candidates


__all__ = [name for name in globals() if not name.startswith("__")]
