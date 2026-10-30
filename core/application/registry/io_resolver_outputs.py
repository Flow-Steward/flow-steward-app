# ruff: noqa: F405, RUF100
# mypy: ignore-errors
from core.application.orchestration.state_paths import StatePathError, parse_state_path
from core.application.registry.state_selector_shape import StateSelectorShape
from core.application.registry.workflow_contract import normalize_field_type
from core.application.workflows.workflow_step_config_contracts import (
    TABULAR_FEED_PICKER_OUTPUT_FIELDS_BY_OPERATION,
)

from .io_resolver_templates import *  # noqa: F403,F401  # NOSONAR


def _loop_variable_rows(item: object) -> list[dict]:
    """Editor rows for a For each's item and index names (none outside a body)."""
    if not isinstance(item, dict):
        return []
    for_each_id = str(item.get("for_each_id") or "").strip()
    label = str(item.get("display_name") or "").strip() or for_each_id
    rows: list[dict] = []
    item_name = str(item.get("item_name") or "").strip()
    if item_name:
        rows.append(
            {
                "name": item_name,
                "type": "json",
                "description": f"The current item of For each {label}",
                "description_key": "react.workflows.variable_description.for_each_item",
                "description_params": {"for_each": label},
                "source": "for_each",
                "for_each_id": for_each_id,
            }
        )
    index_name = str(item.get("index_name") or "").strip()
    if index_name and index_name != item_name:
        rows.append(
            {
                "name": index_name,
                "type": "number",
                "description": f"The position of the current item in For each {label}, from 0",
                "description_key": "react.workflows.variable_description.for_each_index",
                "description_params": {"for_each": label},
                "source": "for_each",
                "for_each_id": for_each_id,
            }
        )
    return rows


class IOResolverOutputMixin:
    """Step output collection and type-map helpers."""

    def get_step_outputs(self, workflow_id: str, step_id: str) -> list[dict]:
        """Canonical outputs step X itself produces (skill contracts + mappings)."""
        workflow, step = self._get_workflow_step(workflow_id, step_id)
        if not step:
            return []
        return self.get_step_declared_outputs(
            step,
            workflow_account_id=str(workflow.get("account_id") or "").strip(),
        )

    def get_step_declared_outputs(self, step: dict, *, workflow_account_id: str = "") -> list[dict]:
        """Canonical producer contracts from the supplied saved or draft step."""
        return self._collect_step_outputs(
            step, include_runtime_io=False, workflow_account_id=workflow_account_id
        )

    def get_step_state_outputs(self, workflow_id: str, step_id: str) -> list[dict]:
        """Only writes to state, using the same target and type discovery as the UI."""
        workflow, step = self._get_workflow_step(workflow_id, step_id)
        if not step:
            return []
        return self._collect_step_outputs(
            step,
            include_runtime_io=False,
            state_only=True,
            workflow_account_id=str(workflow.get("account_id") or "").strip(),
        )

    def get_step_state_writes(self, step: dict, *, workflow_account_id: str = "") -> list[dict]:
        """Exact assignments, preserving occurrences before suggestion-name dedup.

        Shares the declared-output collector's target/type discovery; message
        contracts and synthetic ancestor/member suggestions are not assignments.
        """
        return self._collect_step_outputs(
            step,
            include_runtime_io=False,
            state_only=True,
            exact_state_writes=True,
            workflow_account_id=workflow_account_id,
        )

    def get_available_outputs_for_step(self, workflow_id: str, step_id: str) -> list[dict]:
        """Outputs of every step upstream of X in the execution graph.

        Each upstream step keeps its own rows (deduplicated per step only), so
        two producers of one name both stay addressable by their step selector.
        """
        workflow, step = self._get_workflow_step(workflow_id, step_id)
        if not step:
            return []
        account_id = str(workflow.get("account_id") or "").strip()
        graph = self._definition_graph(workflow)
        all_outputs: list[dict] = []
        for upstream in graph.upstream_steps(str(step.get("step_id") or "").strip()):
            all_outputs.extend(
                self._collect_step_outputs(
                    upstream,
                    include_runtime_io=False,
                    workflow_account_id=account_id,
                )
            )
        return all_outputs

    def get_available_outputs_after_step(self, workflow_id: str, step_id: str) -> list[dict]:
        """What a step placed right after *step_id* can read.

        Everything upstream of *step_id* plus that step's own outputs: a new step
        connected after it runs once it has finished.
        """
        workflow, step = self._get_workflow_step(workflow_id, step_id)
        if not step:
            return []
        own = self._collect_step_outputs(
            step,
            include_runtime_io=False,
            workflow_account_id=str(workflow.get("account_id") or "").strip(),
        )
        return [*self.get_available_outputs_for_step(workflow_id, step_id), *own]

    def get_all_step_outputs(self, workflow_id: str) -> list[dict]:
        """Outputs of every saved step, for a step whose place in the graph is not known yet."""
        workflow = self._get_workflow(workflow_id) or {}
        account_id = str(workflow.get("account_id") or "").strip()
        outputs: list[dict] = []
        steps = workflow.get("steps")
        for step in steps if isinstance(steps, list) else []:
            if isinstance(step, dict):
                outputs.extend(
                    self._collect_step_outputs(
                        step, include_runtime_io=False, workflow_account_id=account_id
                    )
                )
        return outputs

    def get_loop_variables(
        self, workflow_id: str, *, step_id: str = "", for_each_id: str = ""
    ) -> list[dict]:
        """The names a For each publishes to its body: the current item and its index.

        Asked for a step, the For each whose body holds that step; asked for a
        For each, that one. Outside a For each body there are none.
        """
        graph = self._definition_graph(self._get_workflow(workflow_id) or {})
        wanted = str(for_each_id or "").strip()
        if step_id:
            item = graph.for_each_of(str(step_id).strip())
        else:
            item = next(
                (row for row in graph.for_each if str(row.get("for_each_id") or "") == wanted),
                None,
            )
        return _loop_variable_rows(item)

    def get_available_variables(
        self,
        workflow_id: str,
        *,
        step_id: str = "",
        position: str = "before",
        for_each_id: str = "",
    ) -> dict:
        """The variable sources one editor field offers, grouped as the editor shows them.

        For a step: what runs before it, or with ``position="after"`` what a step
        placed right after it reads (its own outputs too), plus the item names of
        the For each it sits in. Without a step the position is unknown: every
        saved step's outputs, plus the item names of ``for_each_id`` when given.
        The global and dotted-path variables never depend on the position.
        """
        if step_id:
            after = position == "after"
            prior = (
                self.get_available_outputs_after_step(workflow_id, step_id)
                if after
                else self.get_available_outputs_for_step(workflow_id, step_id)
            )
            loop = self.get_loop_variables(workflow_id, step_id=step_id)
            resolved = "after" if after else "before"
        else:
            prior = self.get_all_step_outputs(workflow_id)
            loop = (
                self.get_loop_variables(workflow_id, for_each_id=for_each_id) if for_each_id else []
            )
            resolved = "unknown"
        return {
            "state": GLOBAL_STATE_VARS,
            "dotted_path": DOTTED_PATH_VARS,
            "prior_outputs": prior,
            "loop": loop,
            "position": resolved,
        }

    def _collect_step_outputs(
        self,
        step: dict,
        *,
        include_runtime_io: bool = True,
        state_only: bool = False,
        exact_state_writes: bool = False,
        workflow_account_id: str = "",
    ) -> list[dict]:
        """Collect all outputs one step produces.

        Canonical mode (include_runtime_io=False) uses resolved output authority
        while still including explicit state writers and invocation mappings.
        Every row names its producing ``step_id`` and that step's ``group_id``
        (null for root steps) so message selectors stay step-scoped.
        """
        by_name: dict[str, dict] = {}
        state_writes: list[dict] = []

        def _add_output(item: dict) -> bool:
            name = str((item or {}).get("name") or "").strip()
            if not name:
                return False
            existing = by_name.get(name)
            if not existing:
                by_name[name] = item
                return True
            existing["required"] = bool(
                existing.get("required", False) or item.get("required", False)
            )
            if not existing.get("description") and item.get("description"):
                existing["description"] = item["description"]
                if item.get("description_key"):
                    existing["description_key"] = item["description_key"]
                    existing["description_params"] = item.get("description_params", {})
            if (
                str(existing.get("source") or "").strip() == "agent"
                and str(item.get("source") or "").strip() == "skill"
            ):
                existing["type"] = item.get("type", existing.get("type", "text"))
                existing["source"] = "skill"
                existing["source_id"] = item.get("source_id", existing.get("source_id", ""))
            return True

        def _state_target_name(target: object) -> str:
            target_name = str(target or "").strip()
            if target_name.startswith(STATE_PREFIX):
                target_name = target_name[len(STATE_PREFIX) :]
            return target_name

        def _add_state_target_output(
            target: object,
            *,
            source: str,
            source_id: str,
            output_type: str = "json",
            assignment: bool = True,
        ) -> None:
            target_name = _state_target_name(target)
            if not target_name:
                return
            if assignment and exact_state_writes:
                state_writes.append(
                    {
                        "name": target_name,
                        "type": output_type,
                        "source": source,
                        "source_id": source_id,
                        **origin,
                    }
                )
            if "." in target_name:
                parent_name = target_name.split(".", 1)[0]
                _add_output(
                    {
                        "name": parent_name,
                        "type": "object",
                        "description": f"State object written by {source}",
                        "description_key": "react.workflows.variable_description.state_object",
                        "description_params": {"source": source},
                        "source": source,
                        "source_id": source_id,
                        **origin,
                        "required": False,
                    }
                )
            _add_output(
                {
                    "name": target_name,
                    "type": output_type,
                    "description": f"State output written by {source}",
                    "description_key": "react.workflows.variable_description.state_output",
                    "description_params": {"source": source},
                    "source": source,
                    "source_id": source_id,
                    **origin,
                    "required": False,
                }
            )

        def _is_self_passthrough_assignment(target: object, value: object) -> bool:
            target_name = _state_target_name(target)
            if not target_name:
                return False
            return _state_target_name(self._state_source_name(value)) == target_name

        if not isinstance(step, dict):
            return []
        origin = {
            "step_id": str(step.get("step_id") or "").strip(),
            "group_id": str(step.get("group_id") or "").strip() or None,
        }
        agent_id = str(step.get("agent_id") or step.get("agent") or "").strip()
        cap_id = str(step.get("skill_id") or step.get("skill") or "").strip()
        step_block_key = self._resolve_step_block_key(step)
        # Canonical authority resolves once below with strict lookup errors. The
        # best-effort preview lookup must not swallow a failed authority read.
        block_contract = (
            self._resolve_published_block_contract(
                agent_id=agent_id,
                skill_id=cap_id,
                block_key=step_block_key,
                account_id=workflow_account_id,
            )
            if include_runtime_io
            else None
        )
        if isinstance(block_contract, dict):
            step_block_key = str(block_contract.get("block_key") or step_block_key).strip()
            source_kind = str(block_contract.get("source_kind") or "").strip()
            source_ref = str(block_contract.get("source_ref") or "").strip()
            if source_kind == "agent":
                ref_agent, ref_skill = self._parse_agent_skill_ref(source_ref)
                if not agent_id and ref_agent:
                    agent_id = ref_agent
                if not cap_id and ref_skill:
                    cap_id = ref_skill
        if agent_id and not cap_id:
            cap_id = self._resolve_primary_skill_id(agent_id, cap_id)

        contract_outputs = (
            self._merge_step_output_contract(
                agent_id=agent_id,
                skill_id=cap_id,
                block_key=step_block_key,
                account_id=workflow_account_id,
                include_runtime_io=include_runtime_io,
            )
            if not state_only
            else []
        )
        for out in contract_outputs:
            _add_output(
                {
                    "name": str(out.get("name") or "").strip(),
                    "type": out.get("type", "text"),
                    "description": out.get("description", ""),
                    "source": out.get("source", "skill"),
                    "source_id": out.get("source_id", cap_id),
                    **origin,
                    "required": bool(out.get("required", False)),
                }
            )
        # invoke_workflow output_mapping: child outputs mapped to parent targets
        override = step.get("override") or {}
        if isinstance(override, str):
            import json as _json

            try:
                override = _json.loads(override)
            except Exception:
                override = {}
        invoke_spec = override.get("invoke_workflow") if isinstance(override, dict) else None
        if isinstance(invoke_spec, dict):
            output_mapping = invoke_spec.get("output_mapping") or {}
            child_wf_id = invoke_spec.get("workflow_id", "")
            # Get child callable output types for accurate type propagation
            child_output_types = self._get_callable_output_types(child_wf_id)
            for child_output, parent_target in output_mapping.items():
                target_name = _state_target_name(parent_target)
                if target_name:
                    out_type = child_output_types.get(str(child_output).strip(), "text")
                    if state_only:
                        if str(parent_target).strip().startswith(STATE_PREFIX):
                            _add_state_target_output(
                                parent_target,
                                source="invoke_workflow",
                                source_id=child_wf_id,
                                output_type=out_type,
                            )
                        continue
                    _add_output(
                        {
                            "name": target_name,
                            "type": out_type,
                            "description": f"Mapped from child workflow '{child_wf_id}' output '{child_output}'",
                            "source": "invoke_workflow",
                            "source_id": child_wf_id,
                            **origin,
                            "required": False,
                        }
                    )
        output_mapping = step.get("output_mapping_jsonb") or {}
        if isinstance(output_mapping, str):
            import json as _json

            try:
                output_mapping = _json.loads(output_mapping)
            except Exception:
                output_mapping = {}
        connector_id = str(step.get("connector_id") or "").strip()
        connector_override = override.get(STEP_KIND_CONNECTOR) if isinstance(override, dict) else {}
        connector_override = connector_override if isinstance(connector_override, dict) else {}
        connector_operation_id = str(
            connector_override.get("operation_id")
            or connector_override.get("function_name")
            or connector_override.get("action_id")
            or ""
        ).strip()
        if connector_id and isinstance(output_mapping, dict):
            for connector_output, state_target in output_mapping.items():
                return_contract = self._connector_operation_return_contract(
                    connector_id=connector_id,
                    operation_id=connector_operation_id,
                    output_name=str(connector_output),
                    connector_install_id=str(
                        step.get("connector_install_id")
                        or connector_override.get("connector_install_id")
                        or ""
                    ).strip(),
                    account_id=workflow_account_id,
                )
                _add_state_target_output(
                    state_target,
                    source="connector",
                    source_id=f"{connector_id}:{str(connector_output or '').strip()}",
                    output_type=str(
                        return_contract.get("type") or return_contract.get("value_type") or "json"
                    ),
                )
        elif (
            isinstance(output_mapping, dict)
            and output_mapping
            and str(step.get("step_kind") or "agent").strip().lower() != "set_variable"
        ):
            mapped_step_kind = str(step.get("step_kind") or "agent").strip().lower()
            mapped_step_id = (
                str(step.get("step_id") or mapped_step_kind).strip() or mapped_step_kind
            )
            declared_output_types = {
                str(item.get("name") or "").strip(): str(item.get("type") or "json").strip()
                for item in self._merge_step_output_contract(
                    agent_id=agent_id,
                    skill_id=cap_id,
                    block_key=step_block_key,
                    account_id=workflow_account_id,
                    include_runtime_io=include_runtime_io,
                )
                if isinstance(item, dict) and str(item.get("name") or "").strip()
            }
            for mapped_output_name, state_target in output_mapping.items():
                mapped_name = str(mapped_output_name or "").strip()
                _add_state_target_output(
                    state_target,
                    source=mapped_step_kind or "step",
                    source_id=mapped_step_id,
                    output_type=declared_output_types.get(mapped_name, "json"),
                )
        step_kind = str(step.get("step_kind") or "agent").strip().lower()
        step_id = str(step.get("step_id") or step_kind).strip() or step_kind
        if step_kind == "set_variable":
            spec = override.get("set_variable") if isinstance(override, dict) else {}
            spec = spec if isinstance(spec, dict) else {}
            assignments = spec.get("assignments")
            output_types = self._to_object(spec.get("output_types"))
            if isinstance(assignments, dict) and assignments:
                for target, value in assignments.items():
                    if _is_self_passthrough_assignment(target, value):
                        continue
                    declared_type = str(output_types.get(str(target).strip()) or "").strip()
                    _add_state_target_output(
                        target,
                        source=step_kind,
                        source_id=step_id,
                        output_type=declared_type or self._infer_state_output_type(value),
                    )
            else:
                target = spec.get("target")
                if not _is_self_passthrough_assignment(target, spec.get("value")):
                    declared_type = str(output_types.get(str(target).strip()) or "").strip()
                    _add_state_target_output(
                        target,
                        source=step_kind,
                        source_id=step_id,
                        output_type=declared_type
                        or self._infer_state_output_type(spec.get("value")),
                    )
        elif step_kind == "batch":
            spec = override.get("batch") if isinstance(override, dict) else {}
            spec = spec if isinstance(spec, dict) else {}
            _add_state_target_output(
                spec.get("result_target"),
                source=step_kind,
                source_id=step_id,
                output_type="array",
            )
        elif step_kind == "collect":
            # Collect keeps the variable and adds to it; the whole target is written.
            spec = override.get("collect") if isinstance(override, dict) else {}
            spec = spec if isinstance(spec, dict) else {}
            _add_state_target_output(
                spec.get("target"),
                source=step_kind,
                source_id=step_id,
                output_type="object" if spec.get("mode") == "set_key" else "array",
            )
        elif step_kind == "dataset_query":
            spec = override.get("dataset_query") if isinstance(override, dict) else {}
            spec = spec if isinstance(spec, dict) else {}
            _add_state_target_output(
                spec.get("result_target"),
                source=step_kind,
                source_id=step_id,
                output_type="json",
            )
        elif step_kind == "http_request":
            spec = override.get("http_request") if isinstance(override, dict) else {}
            spec = spec if isinstance(spec, dict) else {}
            _add_state_target_output(
                spec.get("response_target"),
                source=step_kind,
                source_id=step_id,
            )
        elif step_kind == "file_from_base64":
            spec = override.get("file_from_base64") if isinstance(override, dict) else {}
            spec = spec if isinstance(spec, dict) else {}
            _add_state_target_output(
                spec.get("result_target"),
                source=step_kind,
                source_id=step_id,
                output_type="artifact_handle",
            )
        elif step_kind == "template":
            spec = override.get("template") if isinstance(override, dict) else {}
            spec = spec if isinstance(spec, dict) else {}
            _add_state_target_output(
                spec.get("result_target"),
                source=step_kind,
                source_id=step_id,
                output_type="text",
            )
        elif step_kind == "transform":
            spec = override.get("transform") if isinstance(override, dict) else {}
            spec = spec if isinstance(spec, dict) else {}
            output_type = (
                "array" if str(spec.get("mode") or "object").strip().lower() == "list" else "object"
            )
            _add_state_target_output(
                spec.get("result_target"),
                source=step_kind,
                source_id=step_id,
                output_type=output_type,
            )
        elif step_kind == "tabular_feed":
            spec = override.get("tabular_feed") if isinstance(override, dict) else {}
            spec = spec if isinstance(spec, dict) else {}
            _add_state_target_output(
                spec.get("result_target"),
                source=step_kind,
                source_id=step_id,
                output_type="object",
            )
            target = _state_target_name(spec.get("result_target"))
            if target:
                operation = str(spec.get("operation") or "").strip().lower()
                for member, member_type in TABULAR_FEED_PICKER_OUTPUT_FIELDS_BY_OPERATION.get(
                    operation, ()
                ):
                    _add_state_target_output(
                        f"state.{target}.{member}",
                        source=step_kind,
                        source_id=step_id,
                        output_type=member_type,
                        assignment=False,
                    )
        return state_writes if exact_state_writes else list(by_name.values())

    def _get_callable_output_types(self, workflow_id: str) -> dict[str, str]:
        """Get {name: type} map from a callable workflow's expected_outputs."""
        if not workflow_id:
            return {}
        wf = self._get_workflow(workflow_id)
        if not wf:
            return {}
        settings = wf.get("settings") or {}
        if isinstance(settings, str):
            import json as _json

            try:
                settings = _json.loads(settings)
            except Exception:
                return {}
        callable_cfg = settings.get("callable") or {}
        if not isinstance(callable_cfg, dict):
            return {}
        expected_outputs = callable_cfg.get("expected_outputs") or []
        return {
            str(item.get("name", "")).strip(): normalize_field_type(item.get("type", "text"))
            for item in expected_outputs
            if isinstance(item, dict) and str(item.get("name", "")).strip()
        }

    @staticmethod
    def _check_type_compat(expected_type: str, source_type: str) -> bool:
        """Check if source_type is compatible with expected_type.

        Rules:
        - Exact canonical type matches always pass.
        - Generic producer `json` may hold any JSON value, including a scalar;
          runtime contract validation still checks the actual value.
        - Precise `object` / `array` producers do not become scalar-compatible.
        - Public workflow fields are normalized before reaching this helper.
        - Canonical text-like expectations accept older `text` / `string`.
        """
        expected = str(expected_type or "").strip().lower()
        source = str(source_type or "").strip().lower()
        if expected == source:
            return True
        if expected == "json" or source == "json":
            return (source if expected == "json" else expected) in {
                "json",
                "object",
                "array",
                "list",
                "text",
                "string",
                "paragraph",
                "select",
                "number",
                "integer",
                "boolean",
            }
        if expected in {"paragraph", "string", "select"}:
            return source in {"text", "string", "paragraph", "select"}
        if expected == "integer":
            return source in {"integer", "number"}
        if expected == "number":
            return source in {"number", "integer"}
        if expected == "boolean":
            return source == "boolean"
        if expected == "object":
            return source in {"object", "json"}
        if expected == "array":
            return source in {"array", "list", "json"}
        if expected == "list":
            return source in {"list", "array", "json"}
        if expected == "text":
            return source in {"text", "string", "paragraph", "select"}
        return False

    def _loop_published_names(self, workflow: dict, step_id: str) -> list[str]:
        """State names a For each publishes before each item of its body.

        Only a step of that For each's body sees them: the current item under
        ``item_name`` and, when named, its position under ``index_name``.
        """
        item = self._definition_graph(workflow).for_each_of(str(step_id or "").strip())
        if not isinstance(item, dict):
            return []
        names: list[str] = []
        for published in (item.get("item_name"), item.get("index_name")):
            published_name = str(published or "").strip()
            if published_name and published_name not in names:
                names.append(published_name)
        return names

    def _build_available_type_map(self, workflow_id: str, step_id: str) -> dict[str, str]:
        """Build a name→type map for all variables available to a step.

        Includes global state vars, dotted path vars, upstream step outputs,
        callable expected_inputs (for callable child workflows, these are
        injected into state at invocation time and available to every step),
        and the loop variables of the loop group the step sits in.
        """
        type_map: dict[str, str] = {}
        # Global state vars
        for v in GLOBAL_STATE_VARS:
            name = str(v.get("name", "")).strip()
            if not name:
                continue
            value_type = v.get("type", "text")
            type_map[name] = value_type
            type_map[f"state.{name}"] = value_type
        # Dotted path vars
        for v in DOTTED_PATH_VARS:
            name = str(v.get("name", "")).strip()
            if not name:
                continue
            value_type = v.get("type", "text")
            type_map[name] = value_type
            type_map[f"state.{name}"] = value_type
        # Callable expected_inputs: injected into child workflow state at invocation
        wf = self._get_workflow(workflow_id)
        if wf:
            settings = wf.get("settings") or {}
            if isinstance(settings, str):
                import json as _json

                try:
                    settings = _json.loads(settings)
                except Exception:
                    settings = {}
            callable_cfg = settings.get("callable") or {}
            if isinstance(callable_cfg, dict):
                for inp in callable_cfg.get("expected_inputs") or []:
                    if isinstance(inp, dict):
                        name = str(inp.get("name", "")).strip()
                        if name:
                            value_type = normalize_field_type(inp.get("type", "text"))
                            type_map[name] = value_type
                            type_map[f"inputs.{name}"] = value_type
                            type_map[f"state.inputs.{name}"] = value_type
        graph = self._definition_graph(wf or {})
        loop = graph.for_each_of(step_id)
        published_paths = []
        for row in self.get_loop_variables(workflow_id, step_id=step_id):
            try:
                published_paths.append(tuple(parse_state_path(row["name"])))
            except StatePathError:
                continue
        # Publication replaces earlier state bindings, including callable aliases.
        replaced = []
        for name in type_map:
            try:
                path = tuple(parse_state_path(name))
            except StatePathError:
                continue
            if any(root and path[: len(root)] == root for root in published_paths):
                replaced.append(name)
        for name in replaced:
            del type_map[name]
        # Upstream step outputs override loose global keys with the same name.
        # Also expose strict message selectors for explicit step-artifact bindings.
        for o in self.get_available_outputs_for_step(workflow_id, step_id):
            output_name = str(o.get("name") or "").strip()
            output_type = o.get("type", "text")
            step_ref = o.get("step_id", "")
            producer = graph.step(str(step_ref)) or {}
            try:
                output_path = tuple(parse_state_path(output_name))
            except StatePathError:
                output_path = ()
            shadowed = (
                bool(loop)
                and producer.get("for_each_id") != loop["for_each_id"]
                and any(root and output_path[: len(root)] == root for root in published_paths)
            )
            if output_name and not shadowed:
                type_map[output_name] = output_type
                type_map[f"state.{output_name}"] = output_type
            if step_ref and output_name:
                type_map[f"message.step.{step_ref}.artifacts.{output_name}"] = output_type
        if wf and loop:
            shape = StateSelectorShape(self, wf)
            for row in self.get_loop_variables(workflow_id, step_id=step_id):
                published_name = row["name"]
                current_type = shape.source_type(step_id, f"state.{published_name}")
                type_map[published_name] = current_type
                type_map[f"state.{published_name}"] = current_type
        return type_map

    def get_available_type_map_for_step(self, workflow_id: str, step_id: str) -> dict[str, str]:
        """Public wrapper used by save-time contract validation."""
        return dict(self._build_available_type_map(workflow_id, step_id))


__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
