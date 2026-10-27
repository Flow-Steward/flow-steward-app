# ruff: noqa: F405, RUF100
# mypy: ignore-errors
from .io_resolver_outputs import *  # noqa: F403,F401  # NOSONAR


class IOResolverValidationMixin:
    """Step I/O validation."""

    def validate_step_io(self, workflow_id: str, step_id: str) -> list[dict]:
        """Check required inputs vs outputs available upstream of the step.

        Validates:
        1. Step boundary_contract.entry: source availability + type compatibility
        2. Skill/agent/connector required inputs vs available set
        3. transform input.source and context_inject references to upstream steps

        Availability is the execution graph only: outputs of upstream steps,
        workflow inputs and the loop variables of the step's loop group.
        Issue paths are relative to the step.

        Returns list of issues: [{input_name, agent_id, skill_id, step_id,
        group_id, issue_type, path, severity, message}].
        """
        warnings: list[dict] = []
        wf, step = self._get_workflow_step(workflow_id, step_id)
        if not wf or not step:
            return warnings
        step_id = str(step.get("step_id") or "").strip()
        group_id = str(step.get("group_id") or "").strip() or None

        def _step_ref_parts(source: object) -> tuple[str, str]:
            parts = str(source or "").strip().split(".")
            if len(parts) >= 5 and parts[0] == "message" and parts[1] == "step":
                if parts[3] == "artifacts":
                    return parts[2], ".".join(parts[4:])
                return parts[2], ".".join(parts[3:])
            return "", ""

        def _normalized_explicit_source_variants(source: str) -> list[str]:
            normalized: list[str] = []
            text = str(source or "").strip()
            if not text:
                return normalized
            candidates = [text]
            if "|" in text:
                base_selector = text.split("|", 1)[0].strip()
                if base_selector:
                    candidates.append(base_selector)
                    text = base_selector
            if text.startswith("$inputs."):
                bare = text.removeprefix("$inputs.").strip()
                if bare:
                    candidates.extend([bare, f"inputs.{bare}", f"state.inputs.{bare}"])
            elif text.startswith("$state.inputs."):
                bare = text.removeprefix("$state.inputs.").strip()
                if bare:
                    candidates.extend([bare, f"inputs.{bare}", f"state.inputs.{bare}"])
            if text.startswith("${") and text.endswith("}"):
                inner = text[2:-1].strip()
                if inner:
                    candidates.extend([inner, f"inputs.{inner}", f"state.inputs.{inner}"])
            if text.startswith("{{") and text.endswith("}}"):
                inner = text[2:-2].strip()
                if inner:
                    if inner.startswith(INPUTS_PREFIX):
                        bare = inner.removeprefix(INPUTS_PREFIX).strip()
                        candidates.extend([inner, bare, f"state.inputs.{bare}"])
                    elif inner.startswith(STATE_INPUTS_PREFIX):
                        bare = inner.removeprefix(STATE_INPUTS_PREFIX).strip()
                        candidates.extend([inner, bare, f"inputs.{bare}"])
                    else:
                        candidates.extend([inner, f"inputs.{inner}", f"state.inputs.{inner}"])
            if text.startswith(INPUTS_PREFIX):
                bare = text.removeprefix(INPUTS_PREFIX).strip()
                if bare:
                    candidates.extend([bare, f"state.inputs.{bare}"])
            elif text.startswith(STATE_INPUTS_PREFIX):
                bare = text.removeprefix(STATE_INPUTS_PREFIX).strip()
                if bare:
                    candidates.extend([bare, f"inputs.{bare}"])
            elif text.startswith(STATE_PREFIX):
                bare = text.removeprefix(STATE_PREFIX).strip()
                if bare:
                    candidates.append(bare)
            for item in candidates:
                candidate = str(item or "").strip()
                if candidate and candidate not in normalized:
                    normalized.append(candidate)
            return normalized

        def _step_explicit_input_sources(step: dict[str, Any], input_name: str) -> list[str]:
            sources: list[str] = []
            step_kind = normalize_step_kind(step.get("step_kind"), default=STEP_KIND_AGENT)
            raw_mapping = self._to_object(step.get("input_mapping_jsonb"))
            if isinstance(raw_mapping, dict):
                mapped = str(raw_mapping.get(input_name) or "").strip()
                if mapped:
                    sources.append(mapped)
            override = self._to_object(step.get("override"))
            if isinstance(override, dict):
                scoped_override = self._to_object(override.get(step_kind))
                if isinstance(scoped_override, dict):
                    scoped_mapping = self._to_object(scoped_override.get("input_mapping"))
                    if isinstance(scoped_mapping, dict):
                        mapped = str(scoped_mapping.get(input_name) or "").strip()
                        if mapped:
                            sources.append(mapped)
                agent_override = self._to_object(override.get(STEP_KIND_AGENT))
                if isinstance(agent_override, dict):
                    agent_mapping = self._to_object(agent_override.get("input_mapping"))
                    if isinstance(agent_mapping, dict):
                        mapped = str(agent_mapping.get(input_name) or "").strip()
                        if mapped:
                            sources.append(mapped)
            normalized_sources: list[str] = []
            for source in sources:
                for candidate in _normalized_explicit_source_variants(source):
                    if candidate not in normalized_sources:
                        normalized_sources.append(candidate)
            return normalized_sources

        def _explicit_mapping_value_satisfied(
            value: object,
            *,
            expected_type: str,
        ) -> bool:
            def _source_satisfies_type(source: str) -> bool:
                source_type = str(type_map.get(source) or "").strip()
                if source_type and self._check_type_compat(expected_type, source_type):
                    return True
                parts = source.split(".")
                for idx in range(len(parts) - 1, 0, -1):
                    parent = ".".join(parts[:idx])
                    parent_type = str(type_map.get(parent) or "").strip().lower()
                    if parent_type in {"json", "object"}:
                        return True
                return False

            if isinstance(value, str):
                text_value = value.strip()
                if text_value.startswith(("literal:", "http://", "https://")):
                    return True
                if text_value.startswith(("{", "[")) and text_value.endswith(("}", "]")):
                    return True
                for explicit_source in _normalized_explicit_source_variants(value):
                    if _source_satisfies_type(explicit_source):
                        return True
                return False
            if isinstance(value, dict):
                nested_values = list(value.values())
            elif isinstance(value, list):
                nested_values = list(value)
            else:
                return False
            saw_nested_reference = False
            for item in nested_values:
                if isinstance(item, (dict, list)):
                    if not _explicit_mapping_value_satisfied(item, expected_type=expected_type):
                        return False
                    saw_nested_reference = True
                    continue
                if not isinstance(item, str):
                    continue
                item_text = item.strip()
                if not item_text:
                    continue
                variants = _normalized_explicit_source_variants(item_text)
                matched_source = False
                for explicit_source in variants:
                    if _source_satisfies_type(explicit_source):
                        matched_source = True
                        saw_nested_reference = True
                        break
                if matched_source:
                    continue
                if item_text.startswith(("literal:", "http://", "https://")):
                    continue
                if item_text.startswith(("{", "[")) and item_text.endswith(("}", "]")):
                    continue
                return False
            return saw_nested_reference

        def _explicit_step_input_satisfied(
            step: dict[str, Any],
            *,
            input_name: str,
            expected_type: str,
        ) -> bool:
            explicit_sources = _step_explicit_input_sources(step, input_name)
            for explicit_source in explicit_sources:
                if _explicit_mapping_value_satisfied(explicit_source, expected_type=expected_type):
                    return True
            raw_mapping = self._to_object(step.get("input_mapping_jsonb"))
            if (
                isinstance(raw_mapping, dict)
                and input_name in raw_mapping
                and _explicit_mapping_value_satisfied(
                    raw_mapping.get(input_name),
                    expected_type=expected_type,
                )
            ):
                return True
            override = self._to_object(step.get("override"))
            if isinstance(override, dict):
                step_kind = normalize_step_kind(step.get("step_kind"), default=STEP_KIND_AGENT)
                override_candidates = []
                scoped_override = self._to_object(override.get(step_kind))
                if isinstance(scoped_override, dict):
                    override_candidates.append(
                        self._to_object(scoped_override.get("input_mapping"))
                    )
                agent_override = self._to_object(override.get(STEP_KIND_AGENT))
                if isinstance(agent_override, dict):
                    override_candidates.append(self._to_object(agent_override.get("input_mapping")))
                for candidate_mapping in override_candidates:
                    if (
                        isinstance(candidate_mapping, dict)
                        and input_name in candidate_mapping
                        and _explicit_mapping_value_satisfied(
                            candidate_mapping.get(input_name),
                            expected_type=expected_type,
                        )
                    ):
                        return True
            return False

        # Build available set and type map
        type_map = self._build_available_type_map(workflow_id, step_id)
        available = set(type_map.keys())
        upstream_outputs = self.get_available_outputs_for_step(workflow_id, step_id)
        upstream_output_names = {o.get("name", "") for o in upstream_outputs if o.get("name")}
        upstream_step_ids = set(self._definition_graph(wf).upstream_step_ids(step_id))
        global_names = {v["name"] for v in GLOBAL_STATE_VARS}
        ambiguous_names = upstream_output_names & global_names
        transform_available: set[str] = set()

        def _add_transform_input_available(name: object, value_type: object = "json") -> None:
            text = str(name or "").strip()
            if not text:
                return
            normalized_type = str(value_type or "json").strip() or "json"
            candidates = _normalized_explicit_source_variants(text)
            if not text.startswith((INPUTS_PREFIX, STATE_INPUTS_PREFIX, STATE_PREFIX)):
                candidates.extend([f"{INPUTS_PREFIX}{text}", f"{STATE_INPUTS_PREFIX}{text}"])
            for candidate in candidates:
                transform_available.add(candidate)
                type_map.setdefault(candidate, normalized_type)

        settings = wf.get("settings") or {}
        if isinstance(settings, str):
            import json as _json

            try:
                settings = _json.loads(settings)
            except Exception:
                settings = {}
        callable_cfg = settings.get("callable") if isinstance(settings, dict) else {}
        if isinstance(callable_cfg, dict):
            declared_inputs = [
                inp for inp in callable_cfg.get("expected_inputs") or [] if isinstance(inp, dict)
            ]
            for inp in declared_inputs:
                _add_transform_input_available(inp.get("name"), inp.get("type") or "json")
            if declared_inputs:
                # Declared inputs also produce their container: a Transform may read them all.
                for container in (INPUTS_PREFIX.rstrip("."), STATE_INPUTS_PREFIX.rstrip(".")):
                    transform_available.add(container)
                    type_map.setdefault(container, "object")

        for output in upstream_outputs:
            output_name = str((output or {}).get("name") or "").strip()
            if not output_name:
                continue
            transform_available.add(output_name)
            transform_available.add(f"{STATE_PREFIX}{output_name}")

        # A loop group publishes its current item (and index, when named) into
        # state before each iteration of its body; the type map already holds
        # them, transform sources must see them too.
        for published_name in self._loop_published_names(wf, step_id):
            transform_available.update((published_name, f"{STATE_PREFIX}{published_name}"))

        # 1. Validate step boundary_contract.entry declared by the user
        boundary = self._to_object(step.get("boundary_contract"))
        entry_names: set[str] = set()
        raw_inputs = boundary.get("entry")
        for ei_idx, ei in enumerate(raw_inputs if isinstance(raw_inputs, list) else []):
            # Readiness reports malformed contracts with original field paths.
            # This source-resolution pass must not crash or repair stored rows.
            if not isinstance(ei, dict):
                continue
            ei_name = ei.get("name", "")
            ei_type = ei.get("type", "text")
            ei_source = ei.get("source", "")
            if not ei_name:
                continue
            entry_names.add(ei_name)
            source_step_id, source_name = _step_ref_parts(ei_source)

            if ei_source:
                # Check source exists in available set
                if ei_source not in available:
                    warnings.append(
                        {
                            "input_name": ei_name,
                            "agent_id": "",
                            "skill_id": "",
                            "step_id": step_id,
                            "group_id": group_id,
                            "issue_type": "expected_input_source_unavailable",
                            "message_key": "react.workflows.expected_input_source_unavailable",
                            "message_params": {"input_name": ei_name, "source": ei_source},
                            "source": ei_source,
                            "source_step_id": source_step_id,
                            "source_name": source_name,
                            "path": f"boundary_contract.entry[{ei_idx}].source",
                            "severity": "error",
                            "message": (
                                f"Expected input '{ei_name}' source '{ei_source}' "
                                f"not available from upstream steps or state"
                            ),
                        }
                    )
                else:
                    # Check type compatibility
                    source_type = type_map.get(ei_source, "text")
                    if not self._check_type_compat(ei_type, source_type):
                        warnings.append(
                            {
                                "input_name": ei_name,
                                "agent_id": "",
                                "skill_id": "",
                                "step_id": step_id,
                                "group_id": group_id,
                                "issue_type": "expected_input_type_mismatch",
                                "source": ei_source,
                                "source_step_id": source_step_id,
                                "source_name": source_name,
                                "path": f"boundary_contract.entry[{ei_idx}].source",
                                "severity": "error",
                                "message": (
                                    f"Type mismatch: '{ei_name}' expects {ei_type} "
                                    f"but source '{ei_source}' produces {source_type}"
                                ),
                            }
                        )
                    else:
                        _add_transform_input_available(ei_name, ei_type)
            elif ei_name in ambiguous_names:
                warnings.append(
                    {
                        "input_name": ei_name,
                        "agent_id": "",
                        "skill_id": "",
                        "step_id": step_id,
                        "group_id": group_id,
                        "issue_type": "expected_input_ambiguous",
                        "message_key": "react.workflows.expected_input_ambiguous",
                        "message_params": {"input_name": ei_name},
                        "path": f"boundary_contract.entry[{ei_idx}]",
                        "severity": "warning",
                        "message": (
                            f"Expected input '{ei_name}' is ambiguous (exists in global state and upstream step outputs). "
                            f"Set an explicit source like 'message.step.<step_id>.artifacts.{ei_name}' "
                            "to avoid loose resolution."
                        ),
                    }
                )
            # No source mapped — not an error (user just hasn't mapped it yet)

        # 2. Validate step-level inputs (skill + agent)
        # Available set is extended by entry rows (they're declared available)
        step_available = available | entry_names

        def _transform_source_available(source: str, current_available: set[str]) -> bool:
            text = str(source or "").strip()
            if not text:
                return False
            variants = _normalized_explicit_source_variants(text)
            for candidate in variants:
                if candidate in current_available:
                    return True
            state_path = text.removeprefix(STATE_PREFIX) if text.startswith(STATE_PREFIX) else text
            parts = [part for part in state_path.split(".") if part]
            for idx in range(len(parts) - 1, 0, -1):
                parent = ".".join(parts[:idx])
                parent_variants = [parent, f"{STATE_PREFIX}{parent}"]
                for parent_candidate in parent_variants:
                    if parent_candidate not in current_available:
                        continue
                    parent_type = str(type_map.get(parent_candidate) or "").strip().lower()
                    if parent_type in {"json", "object", "array", "list"}:
                        return True
            return False

        agent_id = step.get("agent_id", "")
        cap_id = step.get("skill_id", "")
        step_kind = normalize_step_kind(step.get("step_kind"), default=STEP_KIND_AGENT)

        # Check skill required inputs
        if cap_id:
            for inp in self._get_skill_inputs(cap_id):
                input_name = str(inp.get("name") or "").strip()
                if not input_name:
                    continue
                expected_type = str(inp.get("type") or "text").strip() or "text"
                if input_name not in step_available and not _explicit_step_input_satisfied(
                    step,
                    input_name=input_name,
                    expected_type=expected_type,
                ):
                    severity = "error" if inp.get("required", False) else "warning"
                    warnings.append(
                        {
                            "input_name": input_name,
                            "agent_id": agent_id,
                            "skill_id": cap_id,
                            "step_id": step_id,
                            "group_id": group_id,
                            "issue_type": "step_required_input_unavailable",
                            "path": f"inputs.{input_name}",
                            "severity": severity,
                            "message": (
                                f"{'Required' if severity == 'error' else 'Optional'} input "
                                f"'{input_name}' (from {cap_id}) not produced by upstream steps"
                            ),
                        }
                    )

        if step_kind == STEP_KIND_CONNECTOR:
            connector_id = str(step.get("connector_id") or "").strip()
            operation_id = self._connector_operation_selector(step)
            if connector_id and operation_id:
                tool = self._get_tool(
                    connector_id[len(CONNECTOR_PREFIX) :].strip()
                    if connector_id.startswith(CONNECTOR_PREFIX)
                    else connector_id
                )
                if isinstance(tool, dict):
                    for func in self._tool_interface_functions(tool):
                        func_name = str(func.get("name") or func.get("operation_id") or "").strip()
                        if func_name != operation_id:
                            continue
                        for inp in func.get("parameters", []) or []:
                            if not isinstance(inp, dict) or not bool(inp.get("required", False)):
                                continue
                            input_name = str(inp.get("name") or "").strip()
                            if not input_name:
                                continue
                            expected_type = str(inp.get("type") or "text").strip() or "text"
                            if not _explicit_step_input_satisfied(
                                step,
                                input_name=input_name,
                                expected_type=expected_type,
                            ):
                                warnings.append(
                                    {
                                        "input_name": input_name,
                                        "agent_id": "",
                                        "skill_id": "",
                                        "step_id": step_id,
                                        "group_id": group_id,
                                        "issue_type": "connector_required_input_unavailable",
                                        "path": f"inputs.{input_name}",
                                        "severity": "error",
                                        "message": (
                                            f"Required input '{input_name}' "
                                            f"(from {connector_id}::{operation_id}) "
                                            "not produced by upstream steps"
                                        ),
                                    }
                                )
                        break

        if step_kind == STEP_KIND_TRANSFORM:
            override = self._to_object(step.get("override"))
            transform_cfg = self._to_object(override.get(STEP_KIND_TRANSFORM))
            input_cfg = self._to_object(transform_cfg.get("input"))
            source = str(input_cfg.get("source") or "").strip()
            if source and not _transform_source_available(source, transform_available):
                warnings.append(
                    {
                        "input_name": "input.source",
                        "agent_id": "",
                        "skill_id": "",
                        "step_id": step_id,
                        "group_id": group_id,
                        "issue_type": "transform_input_source_unavailable",
                        "message_key": "react.workflows.transform_input_source_unavailable",
                        "message_params": {"source": source},
                        "source": source,
                        "path": "override.transform.input.source",
                        "severity": "error",
                        "message": (
                            f"transform input.source '{source}' is not produced by "
                            "workflow inputs or upstream steps"
                        ),
                    }
                )

        # Check agent required inputs
        for inp in self._get_agent_inputs(agent_id):
            input_name = str(inp.get("name") or "").strip()
            if not input_name:
                continue
            expected_type = str(inp.get("type") or "text").strip() or "text"
            if input_name not in step_available and not _explicit_step_input_satisfied(
                step,
                input_name=input_name,
                expected_type=expected_type,
            ):
                severity = "error" if inp.get("required", False) else "warning"
                warnings.append(
                    {
                        "input_name": input_name,
                        "agent_id": agent_id,
                        "skill_id": cap_id,
                        "step_id": step_id,
                        "group_id": group_id,
                        "issue_type": "agent_required_input_unavailable",
                        "path": f"inputs.{input_name}",
                        "severity": severity,
                        "message": (
                            f"{'Required' if severity == 'error' else 'Optional'} input "
                            f"'{input_name}' (from {agent_id}) not produced by upstream steps"
                        ),
                    }
                )

        # Check context_inject references: a step-artifact selector must name
        # a step that runs before this one on some path of the graph.
        inject = self._to_object(step.get("context_inject"))
        for var_name, path in inject.items():
            source = path.get("source") if isinstance(path, dict) else path
            parts = str(source).split(".")
            if len(parts) >= 5 and parts[0] == "message" and parts[1] == "step":
                ref_step_id = parts[2]
                if parts[3] != "artifacts":
                    continue
                source_name = ".".join(parts[4:])
                if ref_step_id not in upstream_step_ids:
                    warnings.append(
                        {
                            "input_name": var_name,
                            "agent_id": agent_id,
                            "skill_id": cap_id,
                            "step_id": step_id,
                            "group_id": group_id,
                            "issue_type": "context_inject_source_unavailable",
                            "source": source,
                            "source_step_id": ref_step_id,
                            "source_name": source_name,
                            "path": f"context_inject.{var_name}",
                            "severity": "error",
                            "message": (
                                f"context_inject '{var_name}' references step '{ref_step_id}' "
                                f"which does not run before step '{step_id}'"
                            ),
                        }
                    )

        return warnings


__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
