# ruff: noqa: F405, RUF100
# mypy: ignore-errors
from .boundary_contracts import boundary_entry_inspection
from .io_resolver_preview import *  # noqa: F403,F401  # NOSONAR


class IOResolverTemplateMixin:
    """Template variable and output-format helpers."""

    def get_step_input_defaults(self, workflow_id: str, step_id: str) -> dict:
        """Aggregate the step contract and its boundary entry into builder defaults.

        Returns required defaults (read-only), optional suggestions, and custom
        user-defined entry rows (``boundary_contract.entry``) that are not part
        of the standard step contract.
        """
        workflow, step = self._get_workflow_step(workflow_id, step_id)
        if not workflow or not step:
            return {
                "workflow_id": str(workflow_id or "").strip(),
                "step_id": str(step_id or "").strip(),
                "group_id": None,
                "required_inputs": [],
                "optional_inputs": [],
                "custom_inputs": [],
                "current_expected_inputs": [],
            }

        resolved_step_id = str(step.get("step_id") or step_id).strip()
        input_view = boundary_entry_inspection(workflow, step)
        expected_inputs = input_view.pop("expected_inputs")
        expected_by_name: dict[str, dict] = {}
        for item in expected_inputs:
            name = str((item or {}).get("name") or "").strip()
            if name and name not in expected_by_name:
                expected_by_name[name] = dict(item or {})

        available_type_map = self._build_available_type_map(workflow_id, resolved_step_id)
        merged: dict[str, dict] = {}

        agent_id = str(step.get("agent_id") or "").strip()
        cap_id = str(step.get("skill_id") or "").strip()
        step_block_key = self._resolve_step_block_key(step)
        block_contract = self._resolve_published_block_contract(
            agent_id=agent_id,
            skill_id=cap_id,
            block_key=step_block_key,
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
        merged_inputs = (
            self._merge_step_input_contract(
                agent_id=agent_id,
                skill_id=cap_id,
                block_key=step_block_key,
                include_runtime_io=False,
            )
            if step_block_key or agent_id or cap_id
            else []
        )
        step_ref = {
            "agent_id": agent_id,
            "skill_id": cap_id,
            "block_key": step_block_key,
        }
        for input_row in merged_inputs:
            name = str(input_row.get("name") or "").strip()
            if not name or name in merged:
                continue
            expected_type = str(input_row.get("type") or "text").strip() or "text"
            expected_cfg = expected_by_name.get(name) or {}
            explicit_source = str(expected_cfg.get("source") or "").strip()
            resolution = self._resolve_input_source(
                input_name=name,
                expected_type=expected_type,
                explicit_source=explicit_source,
                available_type_map=available_type_map,
            )
            suggested = [str(s or "").strip() for s in (resolution.get("suggested_sources") or [])]
            merged[name] = {
                "name": name,
                "type": expected_type,
                "description": str(input_row.get("description") or "").strip(),
                "required": bool(input_row.get("required", False)),
                "source": str(resolution.get("source") or "").strip(),
                "resolved": bool(resolution.get("resolved")),
                "resolution": str(resolution.get("reason") or "").strip(),
                "suggested_sources": [s for s in suggested if s],
                "step_refs": [dict(step_ref)],
            }

        standard_input_names = set(merged.keys())
        required_inputs = sorted(
            [dict(row) for row in merged.values() if bool(row.get("required", False))],
            key=lambda row: str(row.get("name") or ""),
        )
        optional_inputs = sorted(
            [dict(row) for row in merged.values() if not bool(row.get("required", False))],
            key=lambda row: str(row.get("name") or ""),
        )

        custom_inputs: list[dict] = []
        for item in expected_inputs:
            name = str((item or {}).get("name") or "").strip()
            if not name or name in standard_input_names:
                continue
            custom_inputs.append(
                {
                    "name": name,
                    "type": str((item or {}).get("type") or "text").strip() or "text",
                    "source": str((item or {}).get("source") or "").strip(),
                    "required": bool((item or {}).get("required", False)),
                }
            )

        current_expected_inputs: list[dict] = []
        for item in expected_inputs:
            name = str((item or {}).get("name") or "").strip()
            if not name:
                continue
            current_expected_inputs.append(
                {
                    **dict(item),
                    "name": name,
                    "type": str((item or {}).get("type") or "text").strip() or "text",
                    "source": str((item or {}).get("source") or "").strip(),
                    "required": bool((item or {}).get("required", False)),
                }
            )

        return {
            "workflow_id": str((workflow or {}).get("workflow_id") or workflow_id).strip(),
            "step_id": resolved_step_id,
            "group_id": str(step.get("group_id") or "").strip() or None,
            "required_inputs": required_inputs,
            "optional_inputs": optional_inputs,
            "custom_inputs": custom_inputs,
            "current_expected_inputs": current_expected_inputs,
            **input_view,
        }

    def list_template_variables(self, agent_id: str) -> list[dict]:
        """All variables available for context template, grouped by source."""
        variables: list[dict] = []

        # 1. Global state variables
        for v in GLOBAL_STATE_VARS:
            variables.append({**v, "group": "state", "source": "OrchestratorState"})

        # 2. Dotted paths
        for v in DOTTED_PATH_VARS:
            variables.append({**v, "group": "dotted_path", "source": "project_config"})

        # 3. Effective I/O inputs (from skills + tools + agent)
        effective = self.compute_effective_io(agent_id)
        for inp in effective["inputs"]:
            variables.append(
                {
                    "name": inp["name"],
                    "type": inp["type"],
                    "description": inp["description"],
                    "group": "input",
                    "source": f"{inp['source']}: {inp['source_id']}",
                }
            )

        # 4. Step context_inject patterns (from workflows referencing this agent)
        inject_vars = self._get_context_inject_vars(agent_id)
        for v in inject_vars:
            variables.append({**v, "group": "phase_context"})

        # 5. Explicitly blocked paths (for operator clarity in UI)
        for v in BLOCKED_PATH_VARS:
            variables.append(
                {
                    "name": v["name"],
                    "type": v.get("type", "blocked"),
                    "description": v.get("reason", ""),
                    "group": "blocked",
                    "source": "security_policy",
                    "blocked": True,
                }
            )

        return variables

    def _get_context_inject_vars(self, agent_id: str) -> list[dict]:
        """Find context_inject variables from workflow steps that reference this agent."""
        results = []
        seen = set()
        workflows = self._list_workflows()
        for wf in workflows:
            if not isinstance(wf, dict):
                continue
            steps = wf.get("steps")
            for step in steps if isinstance(steps, list) else []:
                if not isinstance(step, dict):
                    continue
                if step.get("agent_id") == agent_id:
                    inject = self._to_object(step.get("context_inject"))
                    for var_name, path in inject.items():
                        if var_name not in seen:
                            seen.add(var_name)
                            results.append(
                                {
                                    "name": var_name,
                                    "type": "text",
                                    "description": f"Injected from {path}",
                                    "source": f"workflow: {wf.get('workflow_id', '')}",
                                }
                            )
        return results

    def generate_output_format(self, outputs: list[dict]) -> str:
        """Generate the canonical phase_result_v2 contract preview from outputs."""
        if not outputs:
            return ""
        lines = [
            "Return exactly one `phase_result_v2` JSON object.",
            "Put the skill outputs below under the top-level `artifacts` field.",
            "Do not use extra markdown sections outside the JSON object.",
            "",
            "Artifact fields from skill I/O:",
            "",
        ]
        for i, out in enumerate(outputs, 1):
            name = out.get("name", "")
            typ = out.get("type", "text")
            desc = out.get("description", "")
            required = out.get("required", True)
            type_hint = f" ({typ})" if typ != "text" else ""
            desc_part = f": {desc}" if desc else ""
            required_hint = " [required]" if required else " [optional]"
            lines.append(f"{i}. **{name}**{type_hint}{required_hint}{desc_part}")
        lines.append("")
        lines.append("```json")
        artifact_fields = []
        for out in outputs:
            name = out.get("name", "")
            typ = out.get("type", "text")
            if typ == "json":
                artifact_fields.append(f'    "{name}": {{}}')
            elif typ == "list":
                artifact_fields.append(f'    "{name}": []')
            elif typ == "number":
                artifact_fields.append(f'    "{name}": 0')
            elif typ == "boolean":
                artifact_fields.append(f'    "{name}": false')
            else:
                artifact_fields.append(f'    "{name}": "..."')
        lines.append("{")
        lines.append('  "schema_version": "phase_result_v2",')
        lines.append('  "status": "pass",')
        lines.append('  "summary": "What you completed in this step",')
        lines.append('  "artifacts": {')
        lines.append(",\n".join(artifact_fields))
        lines.append("  },")
        lines.append('  "evidence": [],')
        lines.append('  "errors": [],')
        lines.append('  "files_to_touch": [],')
        lines.append('  "next_action": "next_phase",')
        lines.append('  "confidence": 0.0')
        lines.append("}")
        lines.append("```")
        return "\n".join(lines)


__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
