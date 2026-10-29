# ruff: noqa: F405, RUF100
# mypy: ignore-errors
from core.application.workflows.workflow_step_kinds import STEP_KIND_COLLECT, STEP_KIND_WAIT

from .boundary_contracts import boundary_entry_inspection
from .io_resolver_preview_helpers import *  # noqa: F403,F401  # NOSONAR


class IOResolverPreviewMixin:
    """Workflow step contract preview resolution."""

    def _resolve_non_agent_step_contract_preview(
        self,
        *,
        workflow_id: str,
        step_id: str,
    ) -> dict:
        workflow, step = self._get_workflow_step(workflow_id, step_id)
        runtime_step_id = str(step.get("step_id") or step.get("id") or step_id).strip()
        available_type_map = self._build_available_type_map(workflow_id, runtime_step_id)
        step_kind = normalize_step_kind(step.get("step_kind"), default=STEP_KIND_AGENT)
        override = self._to_object(step.get("override"))
        inputs: list[dict] = []
        outputs: list[dict] = []

        if step_kind == STEP_KIND_CONNECTOR:
            mapping = self._to_object(step.get("input_mapping_jsonb"))
            connector_id = str(step.get("connector_id") or "").strip()
            connector_override = self._to_object(override.get(STEP_KIND_CONNECTOR))
            connector_operation_id = str(
                connector_override.get("operation_id")
                or connector_override.get("function_name")
                or connector_override.get("action_id")
                or ""
            ).strip()
            for input_name, source in mapping.items():
                contract_param = self._connector_operation_parameter_contract(
                    connector_id=connector_id,
                    operation_id=connector_operation_id,
                    input_name=str(input_name),
                )
                inputs.append(
                    self._preview_input_row(
                        name=str(input_name),
                        input_type=str(
                            contract_param.get("type")
                            or available_type_map.get(str(source).strip())
                            or "text"
                        ),
                        description=str(
                            contract_param.get("description") or "Connector input payload field"
                        ),
                        required=bool(contract_param.get("required", True)),
                        source=str(source or "").strip(),
                        available_type_map=available_type_map,
                    )
                )
            output_mapping = self._to_object(step.get("output_mapping_jsonb"))
            for connector_output, state_target in output_mapping.items():
                target_name = self._state_target_name(state_target) or str(connector_output).strip()
                return_contract = self._connector_operation_return_contract(
                    connector_id=connector_id,
                    operation_id=connector_operation_id,
                    output_name=str(connector_output),
                    connector_install_id=str(
                        step.get("connector_install_id")
                        or connector_override.get("connector_install_id")
                        or ""
                    ).strip(),
                    account_id=str(workflow.get("account_id") or "").strip(),
                )
                outputs.append(
                    self._preview_output_row(
                        name=target_name,
                        output_type=str(
                            return_contract.get("type")
                            or return_contract.get("value_type")
                            or "json"
                        ),
                        description=f"Mapped from connector output '{connector_output}'",
                        source=str(step.get("connector_id") or "").strip(),
                        source_kind="connector/action/http result",
                    )
                )
        elif step_kind == STEP_KIND_HTTP_REQUEST:
            config = self._to_object(override.get(STEP_KIND_HTTP_REQUEST))
            for name, description in (("method", "HTTP method"), ("url", "Request URL")):
                selector_rows = self._template_selector_input_rows(
                    value=config.get(name),
                    field_prefix=name,
                    description_prefix=description,
                    available_type_map=available_type_map,
                )
                inputs.extend(
                    selector_rows
                    or [
                        self._preview_input_row(
                            name=name,
                            input_type="text",
                            description=description,
                            required=True,
                            source="literal",
                            available_type_map=available_type_map,
                            options=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
                            if name == "method"
                            else None,
                        )
                    ]
                )
            inputs.extend(
                self._template_selector_input_rows(
                    value=config.get("query_params") or {},
                    field_prefix="query_params",
                    description_prefix="HTTP query parameter",
                    available_type_map=available_type_map,
                )
            )
            inputs.extend(
                self._template_selector_input_rows(
                    value=config.get("headers") or {},
                    field_prefix="headers",
                    description_prefix="HTTP header",
                    available_type_map=available_type_map,
                )
            )
            inputs.extend(
                self._template_selector_input_rows(
                    value=config.get("body_template"),
                    field_prefix="body",
                    description_prefix="HTTP body field",
                    available_type_map=available_type_map,
                )
            )
            if str(config.get("auth_ref") or "").strip():
                inputs.append(
                    self._preview_input_row(
                        name="auth_ref",
                        input_type="secret",
                        description="Authorization credential reference",
                        required=True,
                        source="literal",
                        available_type_map=available_type_map,
                    )
                )
            outputs.append(
                self._preview_output_row(
                    name=self._state_target_name(config.get("response_target")) or "response_body",
                    output_type="artifact_handle"
                    if str(config.get("response_mode") or "inline").strip().lower() == "artifact"
                    else "json",
                    description="Response payload written by http_request",
                    source="http_request",
                    source_kind="connector/action/http result",
                )
            )
        elif step_kind == STEP_KIND_FILE_FROM_BASE64:
            config = self._to_object(override.get(STEP_KIND_FILE_FROM_BASE64))
            source = str(config.get("source") or "").strip()
            inputs.append(
                self._preview_input_row(
                    name="source",
                    input_type=str(available_type_map.get(source) or "text"),
                    description="Base64 file content, a data: URL, or an object holding one",
                    required=True,
                    source=source,
                    available_type_map=available_type_map,
                )
            )
            outputs.append(
                self._preview_output_row(
                    name=self._state_target_name(config.get("result_target")) or "file",
                    output_type="artifact_handle",
                    description="Saved file written by file_from_base64",
                    source=STEP_KIND_FILE_FROM_BASE64,
                    source_kind="file",
                )
            )
        elif step_kind == STEP_KIND_TEMPLATE:
            config = self._to_object(override.get(STEP_KIND_TEMPLATE))
            rows = config.get("inputs")
            for row in rows if isinstance(rows, list) else []:
                row = self._to_object(row)
                inputs.append(
                    self._preview_input_row(
                        name=str(row.get("name") or "").strip(),
                        input_type=str(row.get("type") or "text").strip() or "text",
                        description="Template input",
                        required=row.get("required") is not False,
                        source=str(row.get("source") or "").strip(),
                        available_type_map=available_type_map,
                    )
                )
            outputs.append(
                self._preview_output_row(
                    name=self._state_target_name(config.get("result_target")) or "text",
                    output_type="text",
                    description=f"Text rendered by the template ({config.get('format') or 'text'})",
                    source=STEP_KIND_TEMPLATE,
                    source_kind="template",
                )
            )
        elif step_kind == STEP_KIND_DATASET_QUERY:
            config = self._to_object(
                override.get(STEP_KIND_DATASET_QUERY) or step.get(STEP_KIND_DATASET_QUERY)
            )
            query_template_id = str(
                config.get("query_template_id") or step.get("query_template_id") or ""
            ).strip()
            query_template_input = self._preview_input_row(
                name="query_template_id",
                input_type="text",
                description="Saved dataset query template",
                required=True,
                source="literal",
                available_type_map=available_type_map,
            )
            if not query_template_id:
                query_template_input.update(
                    source="", source_kind="", resolved=False, resolution="unresolved"
                )
            inputs.append(query_template_input)
            params = self._to_object(
                config.get("params") or config.get("input_params") or step.get("params")
            )
            for param_name, source in params.items():
                source_text = str(source or "").strip()
                inputs.append(
                    self._preview_input_row(
                        name=str(param_name),
                        input_type=str(available_type_map.get(source_text) or "text"),
                        description="Dataset query parameter",
                        required=True,
                        source=source_text
                        if source_text.startswith((STATE_PREFIX, INPUTS_PREFIX))
                        else "literal",
                        available_type_map=available_type_map,
                    )
                )
            outputs.append(
                self._preview_output_row(
                    name=self._state_target_name(config.get("result_target"))
                    or "dataset_query_result",
                    output_type="array",
                    description="Dataset query execution result",
                    source=str(config.get("query_template_id") or "").strip(),
                    source_kind="dataset query result",
                )
            )
        elif step_kind == STEP_KIND_INVOKE_WORKFLOW:
            spec = self._to_object(override.get(STEP_KIND_INVOKE_WORKFLOW))
            child_workflow_id = str(spec.get("workflow_id") or "").strip()
            child_settings = self._to_object(
                (self._get_workflow(child_workflow_id) or {}).get("settings")
            )
            callable_cfg = self._to_object(child_settings.get("callable"))
            for item in callable_cfg.get("expected_inputs") or []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                source = str(self._to_object(spec.get("input_mapping")).get(name) or "").strip()
                inputs.append(
                    self._preview_input_row(
                        name=name,
                        input_type=str(item.get("type") or "text"),
                        description=str(item.get("description") or "").strip(),
                        required=bool(item.get("required", False)),
                        source=source,
                        available_type_map=available_type_map,
                    )
                )
            for item in callable_cfg.get("expected_outputs") or []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                target = str(self._to_object(spec.get("output_mapping")).get(name) or "").strip()
                outputs.append(
                    self._preview_output_row(
                        name=self._state_target_name(target) or name,
                        output_type=str(item.get("type") or "text"),
                        description=str(item.get("description") or "").strip()
                        or f"Child workflow '{child_workflow_id}' output",
                        source=child_workflow_id,
                        source_kind="child workflow output",
                    )
                )
        elif step_kind == STEP_KIND_WAIT:
            config = self._to_object(override.get(STEP_KIND_WAIT))
            duration = self._to_object(config.get("input_mapping")).get("duration_ms")
            source = self._state_source_name(duration) or self._state_selector_without_default(
                duration
            )
            if source:
                inputs.append(
                    self._preview_input_row(
                        name="duration_ms",
                        input_type="number",
                        description="Wait duration in milliseconds",
                        required=True,
                        source=source,
                        available_type_map=available_type_map,
                    )
                )
        elif step_kind == STEP_KIND_TRANSFORM:
            config = self._to_object(override.get(STEP_KIND_TRANSFORM))
            input_cfg = self._to_object(config.get("input"))
            source = str(input_cfg.get("source") or "").strip()
            mode = str(config.get("mode") or "object").strip().lower()
            if source:
                inputs.append(
                    self._preview_input_row(
                        name="input.source",
                        input_type=str(available_type_map.get(source) or "json"),
                        description="Source object or list transformed by transform",
                        required=True,
                        source=source,
                        available_type_map=available_type_map,
                    )
                )
            outputs.append(
                self._preview_output_row(
                    name=self._state_target_name(config.get("result_target")) or "transform_result",
                    output_type="array" if mode == "list" else "object",
                    description="Mapped JSON output written by transform",
                    source=runtime_step_id,
                    source_kind="transform",
                )
            )
        elif step_kind == STEP_KIND_TABULAR_FEED:
            config = self._to_object(override.get(STEP_KIND_TABULAR_FEED))
            input_cfg = self._to_object(config.get("input"))
            is_diff = str(config.get("operation") or "").strip() == "diff"
            fields = (
                ("baseline_dataset_handle", "dataset_handle")
                if is_diff
                else ("artifact_handle", "dataset_handle", "rows")
            )
            for field in fields:
                source = str(input_cfg.get(field) or "").strip()
                if not source and not is_diff:
                    continue
                inputs.append(
                    self._preview_input_row(
                        name=f"input.{field}",
                        input_type=str(available_type_map.get(source) or "json"),
                        description="Handle or bounded rows consumed by tabular_feed",
                        required=True,
                        source=source,
                        available_type_map=available_type_map,
                    )
                )
            outputs.append(
                self._preview_output_row(
                    name=self._state_target_name(config.get("result_target"))
                    or "tabular_feed_result",
                    output_type="object",
                    description="Bounded tabular_feed result envelope with handles and metadata",
                    source=runtime_step_id,
                    source_kind="tabular_feed",
                )
            )
        elif step_kind == STEP_KIND_IF:
            config = self._to_object(override.get(STEP_KIND_IF))
            condition = self._to_object(config.get("condition"))
            selector = str(condition.get("selector") or "").strip()
            if selector:
                inputs.append(
                    self._preview_input_row(
                        name="selector",
                        input_type=str(available_type_map.get(selector) or "text"),
                        description=f"if {condition.get('operator') or 'truthy'} selector",
                        required=True,
                        source=selector,
                        available_type_map=available_type_map,
                    )
                )
        elif step_kind == STEP_KIND_SWITCH:
            config = self._to_object(override.get(STEP_KIND_SWITCH))
            selector = str(config.get("selector") or "").strip()
            if selector:
                inputs.append(
                    self._preview_input_row(
                        name="selector",
                        input_type=str(available_type_map.get(selector) or "text"),
                        description="switch selector value",
                        required=True,
                        source=selector,
                        available_type_map=available_type_map,
                    )
                )
        elif step_kind == STEP_KIND_SET_VARIABLE:
            config = self._to_object(override.get(STEP_KIND_SET_VARIABLE))
            assignments = config.get("assignments")
            output_types = self._to_object(config.get("output_types"))
            if isinstance(assignments, dict) and assignments:
                for target, value in assignments.items():
                    declared_type = str(output_types.get(str(target).strip()) or "").strip()
                    outputs.append(
                        self._preview_output_row(
                            name=self._state_target_name(target),
                            output_type=declared_type or self._infer_state_output_type(value),
                            description="Assigned by set_variable",
                            source=runtime_step_id,
                            source_kind="set_variable",
                        )
                    )
            elif str(config.get("target") or "").strip():
                target = config.get("target")
                declared_type = str(output_types.get(str(target).strip()) or "").strip()
                outputs.append(
                    self._preview_output_row(
                        name=self._state_target_name(target),
                        output_type=declared_type
                        or self._infer_state_output_type(config.get("value")),
                        description="Assigned by set_variable",
                        source=runtime_step_id,
                        source_kind="set_variable",
                    )
                )
        elif step_kind == STEP_KIND_COLLECT:
            config = self._to_object(override.get(STEP_KIND_COLLECT))
            source = str(config.get("source") or "").strip()
            if source:
                inputs.append(
                    self._preview_input_row(
                        name="source",
                        input_type="json",
                        description="Value added by Collect",
                        required=True,
                        source=source if source.startswith(STATE_PREFIX) else f"state.{source}",
                        available_type_map=available_type_map,
                    )
                )
            target_name = self._state_target_name(config.get("target"))
            if target_name:
                outputs.append(
                    self._preview_output_row(
                        name=target_name,
                        output_type="object" if config.get("mode") == "set_key" else "array",
                        description="The collected variable",
                        source=runtime_step_id,
                        source_kind="collect",
                    )
                )
        elif step_kind == STEP_KIND_BATCH:
            config = self._to_object(override.get(STEP_KIND_BATCH))
            source_path = str(config.get("source_path") or "").strip()
            if source_path:
                inputs.append(
                    self._preview_input_row(
                        name="source_path",
                        input_type="array",
                        description="Collection chunked by batch",
                        required=True,
                        source=source_path
                        if source_path.startswith(STATE_PREFIX)
                        else f"state.{source_path}",
                        available_type_map=available_type_map,
                    )
                )
            outputs.append(
                self._preview_output_row(
                    name=self._state_target_name(config.get("result_target")) or "batch_result",
                    output_type="array",
                    description="Batched output collection",
                    source=runtime_step_id,
                    source_kind="batch",
                )
            )
        elif step_kind in {STEP_KIND_STOP_SUCCESS, STEP_KIND_STOP_FAIL}:
            outputs.append(
                self._preview_output_row(
                    name="workflow_terminal",
                    output_type="json",
                    description="Terminal workflow status",
                    source=runtime_step_id,
                    source_kind="global/runtime state",
                )
            )

        locked_required, unresolved_required, optional = self._step_contract_groups_from_rows(
            inputs
        )
        return {
            "workflow_id": str((workflow or {}).get("workflow_id") or workflow_id).strip(),
            "group_id": str(step.get("group_id") or "").strip() or None,
            "step_id": runtime_step_id,
            "step_kind": step_kind,
            "agent_id": str(step.get("agent_id") or "").strip(),
            "skill_id": str(step.get("skill_id") or "").strip(),
            "block_key": str(step.get("block_key") or "").strip(),
            "published_block": None,
            "skill": {
                "id": str(step.get("skill_id") or "").strip(),
                "label": "",
            },
            "locked_required": locked_required,
            "unresolved_required": unresolved_required,
            "optional": optional,
            "custom": [],
            "outputs": outputs,
            "available_type_map": available_type_map,
        }

    def resolve_step_contract_preview(
        self,
        *,
        workflow_id: str,
        step_id: str = "",
        agent_id: str = "",
        skill_id: str = "",
        block_key: str = "",
        block_binding_mode: str | None = None,
    ) -> dict:
        """Resolve effective step contract groups for builder preview.

        Without ``step_id`` (a step not saved yet) only workflow-level variables
        are available and there is no boundary entry to read.
        """
        if (
            block_binding_mode not in {None, "auto", "explicit"}
            or (block_binding_mode == "auto" and str(block_key or "").strip())
            or (block_binding_mode == "explicit" and not str(block_key or "").strip())
        ):
            raise ValueError("Choose automatic or explicit Agent contract preview")
        normalized_step_id = str(step_id or "").strip()
        workflow, resolved_step = self._get_workflow_step(workflow_id, normalized_step_id)
        if resolved_step:
            step_kind = normalize_step_kind(
                resolved_step.get("step_kind"),
                default=STEP_KIND_AGENT,
            )
            if step_kind != STEP_KIND_AGENT:
                return self._resolve_non_agent_step_contract_preview(
                    workflow_id=workflow_id,
                    step_id=normalized_step_id,
                )
            if not agent_id:
                agent_id = str(resolved_step.get("agent_id") or "").strip()
            if not skill_id:
                skill_id = str(resolved_step.get("skill_id") or "").strip()
            if not block_key and block_binding_mode is None:
                block_key = str(resolved_step.get("block_key") or "").strip()
        resolved_agent_id = str(agent_id or "").strip()
        resolved_skill_id = str(skill_id or "").strip()
        resolved_block = self._resolve_published_block_contract(
            agent_id=resolved_agent_id,
            skill_id=resolved_skill_id,
            block_key=block_key,
        )
        resolved_block_key = str((resolved_block or {}).get("block_key") or block_key).strip()
        if isinstance(resolved_block, dict):
            source_kind = str(resolved_block.get("source_kind") or "").strip()
            source_ref = str(resolved_block.get("source_ref") or "").strip()
            if source_kind == "agent":
                ref_agent, ref_skill = self._parse_agent_skill_ref(source_ref)
                if not resolved_agent_id and ref_agent:
                    resolved_agent_id = ref_agent
                if not resolved_skill_id and ref_skill:
                    resolved_skill_id = ref_skill
        if resolved_agent_id and not resolved_skill_id:
            resolved_skill_id = self._resolve_primary_skill_id(resolved_agent_id, resolved_skill_id)

        input_view = boundary_entry_inspection(workflow or {}, resolved_step or {})
        expected_inputs = input_view.pop("expected_inputs")
        expected_by_name: dict[str, dict] = {}
        for item in expected_inputs:
            name = str((item or {}).get("name") or "").strip()
            if name and name not in expected_by_name:
                expected_by_name[name] = dict(item or {})

        available_type_map = self._build_available_type_map(
            workflow_id, str((resolved_step or {}).get("step_id") or "").strip()
        )
        merged_inputs = self._merge_step_input_contract(
            agent_id=resolved_agent_id,
            skill_id=resolved_skill_id,
            block_key=resolved_block_key,
            include_runtime_io=False,
        )
        merged_outputs = self._merge_step_output_contract(
            agent_id=resolved_agent_id,
            skill_id=resolved_skill_id,
            block_key=resolved_block_key,
            include_runtime_io=False,
        )

        locked_required: list[dict] = []
        unresolved_required: list[dict] = []
        optional: list[dict] = []

        standard_input_names = {str(item.get("name") or "").strip() for item in merged_inputs}
        for input_row in merged_inputs:
            name = str(input_row.get("name") or "").strip()
            expected_type = str(input_row.get("type") or "text").strip() or "text"
            expected_cfg = expected_by_name.get(name) or {}
            explicit_source = str(expected_cfg.get("source") or "").strip()
            resolution = self._resolve_input_source(
                input_name=name,
                expected_type=expected_type,
                explicit_source=explicit_source,
                available_type_map=available_type_map,
            )
            payload = {
                "name": name,
                "type": expected_type,
                "description": str(input_row.get("description") or "").strip(),
                "required": bool(input_row.get("required", False)),
                "contract_source": str(input_row.get("source") or "").strip(),
                "contract_source_id": str(input_row.get("source_id") or "").strip(),
                "source": str(resolution.get("source") or "").strip(),
                "resolved": bool(resolution.get("resolved")),
                "resolution": str(resolution.get("reason") or "").strip(),
                "suggested_sources": list(resolution.get("suggested_sources") or []),
            }
            if payload["required"]:
                if payload["resolved"]:
                    payload["read_only"] = True
                    locked_required.append(payload)
                else:
                    unresolved_required.append(payload)
            else:
                payload["read_only"] = False
                optional.append(payload)

        custom: list[dict] = []
        for item in expected_inputs:
            name = str((item or {}).get("name") or "").strip()
            if not name or name in standard_input_names:
                continue
            expected_type = str((item or {}).get("type") or "text").strip() or "text"
            source = str((item or {}).get("source") or "").strip()
            source_type = str(available_type_map.get(source) or "").strip()
            source_available = bool(source and source_type)
            type_compatible = bool(
                source_available and self._check_type_compat(expected_type, source_type)
            )
            custom.append(
                {
                    "name": name,
                    "type": expected_type,
                    "source": source,
                    "required": bool((item or {}).get("required", True)),
                    "resolved": bool(source and type_compatible),
                    "source_available": source_available,
                    "source_type": source_type or "",
                }
            )

        skill = self._get_skill(resolved_skill_id) if resolved_skill_id else {}
        return {
            "workflow_id": str((workflow or {}).get("workflow_id") or workflow_id).strip(),
            "group_id": str((resolved_step or {}).get("group_id") or "").strip() or None,
            "step_id": normalized_step_id,
            "step_kind": STEP_KIND_AGENT,
            "agent_id": resolved_agent_id,
            "skill_id": resolved_skill_id,
            "block_key": resolved_block_key,
            "published_block": (
                {
                    "block_key": resolved_block_key,
                    "display_name": str((resolved_block or {}).get("display_name") or "").strip(),
                    "source_kind": str((resolved_block or {}).get("source_kind") or "").strip(),
                    "source_ref": str((resolved_block or {}).get("source_ref") or "").strip(),
                }
                if resolved_block_key
                else None
            ),
            "skill": {
                "id": str((skill or {}).get("skill_id") or resolved_skill_id).strip(),
                "label": str((skill or {}).get("display_name") or "").strip(),
            },
            "locked_required": locked_required,
            "unresolved_required": unresolved_required,
            "optional": optional,
            "custom": custom,
            **input_view,
            "outputs": merged_outputs,
            "available_type_map": available_type_map,
        }


__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
