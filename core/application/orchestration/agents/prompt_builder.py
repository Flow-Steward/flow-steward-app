"""
PromptBuilder — assembles LLM prompt messages from an AgentManifest + StepConfig + state.

Pipeline:
1. Agent system prompt (agent.prompts.system)
2. Tool prompts for all enabled tools (tool.prompt)
3. Canonical selected skill instructions, then agent-specific skill guidance
4. Phase step override (extra_prompt from step.override)
5. Context template (agent.prompts.context_template) filled with state values
6. Canonical phase_result_v2 response contract
7. Feedback block (if retry)

Returns a (system_prompt: str, user_prompt: str) tuple ready for invoke_agent().
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from core.application.management.scope import scope_from_state
from core.application.orchestration.agent_messages import (
    MessageSelectorError,
    get_state_messages,
    resolve_message_selector,
)
from core.application.orchestration.agents.behavior_prompt_block import build_behavior_prompt_block
from core.application.orchestration.agents.common import sanitize_external_input
from core.infrastructure.security.prompt_safety import (
    REDACTED_CREDENTIAL_MARKER,
    find_forbidden_template_paths,
    find_prompt_guard_violations,
    is_sensitive_name,
    redact_sensitive_structure,
    sanitize_project_config_for_prompt,
    validate_reference_path,
)

if TYPE_CHECKING:
    from core.application.orchestration.state import OrchestratorState
    from core.application.registry.agent_manifest import AgentManifest
    from core.application.registry.skill_manifest import SkillManifest
    from core.application.registry.step_output_contract import StepOutputContract
    from core.application.registry.tool_registry import ToolRegistry
    from core.application.registry.workflow_manifest import StepConfig

logger = logging.getLogger(__name__)


class PromptBuilder:
    """Assembles prompts from manifest metadata + runtime state."""

    def build(
        self,
        agent: AgentManifest,
        skill: str,
        step: StepConfig | None,
        tool_registry: ToolRegistry | None,
        state: OrchestratorState | dict[str, Any],
        tool_ids: list[str] | None = None,
        feedback: str | None = None,
        skill_manifest: SkillManifest | None = None,
        output_contract: StepOutputContract | None = None,
    ) -> tuple[str, str, str]:
        """Build (system_prompt, user_prompt) for the agent+skill combination.

        Args:
            agent:        AgentManifest for the agent being invoked.
            skill:   The skill_id being executed this step.
            step:         StepConfig with optional overrides (may be None).
            tool_registry: Registry to look up tool prompts (may be None).
            state:        Current OrchestratorState for context template resolution.
            tool_ids:     Effective tool IDs for this run (direct + skill + step overrides).
            feedback:     Retry feedback from a review or failed attempt (if this is a re-run).
            skill_manifest: Selected assigned skill resolved under the invocation scope.

        Returns:
            (system_prompt, user_prompt, version_hash) — prompts + 16-char hex digest.
        """
        system_parts = []
        user_parts = []

        # ── 1. Agent identity (system) ────────────────────────────────────────
        system_prompt = agent.prompts.system.strip()
        if system_prompt:
            system_parts.append(system_prompt)

        # ── 2. Enabled tool prompts (system) ─────────────────────────────────
        step_override = (
            step.get("override") if isinstance(step, dict) else getattr(step, "override", None)
        ) or {}
        active_tools = self._resolve_active_tools(agent.tools, step_override, tool_ids)

        if tool_registry:
            tool_prompts = []
            for tool_id in active_tools:
                try:
                    manifest = tool_registry.get_enabled_tool(tool_id)
                    if manifest and manifest.prompt:
                        tool_prompts.append(
                            f"### Tool: {manifest.display_name}\n{manifest.prompt.strip()}"
                        )
                except Exception:
                    pass
            if tool_prompts:
                system_parts.append("## Available Tools\n" + "\n\n".join(tool_prompts))

        # ── 2b. Guardrail constraints (system — prevention layer) ──────────
        guardrail_constraints = self._build_guardrail_constraints(agent.agent_id, state)
        if guardrail_constraints:
            system_parts.append(guardrail_constraints)

        # ── 3. Skill-specific instructions (user) ───────────────────────
        mode_block = self._build_execution_mode_block(state=state, skill=skill, step=step)
        if mode_block:
            user_parts.append(mode_block)

        behavior_block = self._build_behavior_policy_block(state=state)
        if behavior_block:
            user_parts.append(behavior_block)

        if skill_manifest is not None:
            instructions = [line.strip() for line in skill_manifest.instructions if line.strip()]
            if instructions:
                user_parts.append("## Skill Instructions\n" + "\n".join(instructions))

        cap_prompt = (agent.prompts.skill_prompts or {}).get(skill, "")
        if cap_prompt:
            user_parts.append(f"## Task Instructions\n{cap_prompt.strip()}")

        # ── 3b. Few-shot examples (user) ────────────────────────────────────
        examples = (agent.prompts.examples or {}).get(skill, [])
        if examples:
            example_lines = ["## Examples"]
            for i, ex in enumerate(examples, 1):
                example_lines.append(f"\n### Example {i}")
                if ex.get("input"):
                    example_lines.append(f"**Input:** {ex['input']}")
                if ex.get("output"):
                    example_lines.append(f"**Output:**\n{ex['output']}")
            user_parts.append("\n".join(example_lines))

        # ── 4. Phase step extra_prompt override (user) ───────────────────────
        if step_override:
            extra = step_override.get("extra_prompt", "")
            if extra:
                user_parts.append(f"## Additional Instructions\n{extra.strip()}")

        # ── 5. Context template (user) ────────────────────────────────────────
        context_template = agent.prompts.context_template or ""
        if context_template:
            rendered_context = self._resolve_context(context_template, state, step)
            if rendered_context.strip():
                user_parts.append(rendered_context)
        elif fallback_context := self._build_default_task_context(state):
            user_parts.append(fallback_context)

        # The values this step's input mapping resolved. They were reachable only
        # through `{placeholder}` substitution in a skill's own text, so a run
        # whose skill did not spell one out — or whose mapping resolved to
        # nothing — reached the model with no trace of what the workflow had
        # fetched, and the agent answered from the run's metadata instead.
        mapped_inputs_block = self._build_materialized_inputs_context(state)
        if mapped_inputs_block:
            user_parts.append(mapped_inputs_block)

        task_envelope_block = self._build_task_envelope_context(state)
        if task_envelope_block:
            user_parts.append(task_envelope_block)

        approval_context_block = self._build_approval_context_block(state, skill=skill)
        if approval_context_block:
            user_parts.append(approval_context_block)

        # ── 6. Canonical output contract (user) ─────────────────────────────
        phase_envelope = self._build_phase_result_v2_requirements(
            skill, output_contract=output_contract
        )
        if phase_envelope:
            user_parts.append(phase_envelope)

        # ── 7. Retry feedback (user) ──────────────────────────────────────────
        if feedback:
            user_parts.append(
                f"## Rework Feedback\nYour previous attempt was reviewed and needs changes:\n\n{feedback.strip()}\n\n"
                "Please address all feedback points above."
            )

        system = "\n\n".join(system_parts)
        user = "\n\n".join(user_parts)
        guard_violations = find_prompt_guard_violations(
            system,
            user,
            project_config=state.get("project_config") if isinstance(state, dict) else None,
        )
        if guard_violations:
            raise ValueError(
                "prompt safety guard blocked prompt build: " + "; ".join(guard_violations)
            )
        version_hash = self._compute_prompt_version(
            agent.agent_id,
            skill,
            system,
            user,
        )
        return system, user, version_hash

    @staticmethod
    def _build_default_task_context(state: OrchestratorState | dict[str, Any]) -> str:
        """Provide a minimal task context block when no custom template exists."""
        task_summary = str(state.get("task_summary") or "").strip()
        initial_requirements = str(state.get("initial_requirements") or "").strip()
        project_name = str(state.get("project_name") or "").strip()
        lines: list[str] = []
        if task_summary:
            lines.append(f"Task Summary: {task_summary}")
        if initial_requirements and initial_requirements != task_summary:
            lines.append("Initial Requirements:")
            lines.append(initial_requirements)
        if project_name:
            lines.append(f"Project: {project_name}")
        if not lines:
            return ""
        return "## Task Context\n" + "\n".join(lines)

    @staticmethod
    def _build_task_envelope_context(state: OrchestratorState | dict[str, Any]) -> str:
        """Expose the structured task contract when workflow intake provided one."""
        envelope = state.get("task_envelope") if isinstance(state, dict) else None
        if not isinstance(envelope, Mapping) or not envelope:
            return ""

        allowed_keys = (
            "source",
            "source_id",
            "requested_workflow_id",
            "project_id",
            "title",
            "objective",
            "description",
            "task_type",
            "priority",
            "requirements",
            "acceptance_criteria",
            "constraints",
            "general_constraints",
            "general_purpose_constraints",
            "non_goals",
            "verification_commands",
            "files",
            "inputs",
            "metadata",
        )
        compact = {
            key: PromptBuilder._bounded_task_envelope_value(key, envelope[key])
            for key in allowed_keys
            if key in envelope
        }
        if not compact:
            return ""

        try:
            serialized = json.dumps(compact, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            serialized = json.dumps(
                json.loads(json.dumps(compact, default=str)),
                ensure_ascii=False,
                indent=2,
            )
        if len(serialized) > 8000:
            serialized = (
                serialized[:7800] + "\n... truncated; see task_envelope state for full payload ..."
            )
        verification_block = PromptBuilder._task_envelope_verification_block(
            envelope.get("verification_commands")
        )
        return (
            "## Structured Task Envelope\n"
            "Treat this as the authoritative task contract. Preserve acceptance criteria, "
            "constraints, exact verification commands, files, and inputs unless later "
            "workflow feedback explicitly changes them.\n"
            "```json\n"
            f"{serialized}\n"
            "```"
            f"{verification_block}"
        )

    @staticmethod
    def _bounded_task_envelope_value(key: str, value: Any) -> Any:
        """Keep high-priority task contract fields visible even with noisy payloads."""
        limit_by_key = {
            "description": 1800,
            "metadata": 900,
            "inputs": 900,
            "requirements": 2200,
            "acceptance_criteria": 2200,
            "constraints": 1600,
            "general_constraints": 1600,
            "general_purpose_constraints": 1600,
            "non_goals": 1600,
        }
        limit = limit_by_key.get(str(key or ""), 4000)
        return PromptBuilder._bounded_prompt_value(value, limit=limit)

    @staticmethod
    def _sensitive_state_keys(state: OrchestratorState | dict[str, Any]) -> frozenset[str]:
        """Keys whose value came from a secret, whatever they are now called."""
        raw = state.get("materialized_input_sensitive_keys") if isinstance(state, dict) else None
        if isinstance(raw, (list, tuple, set)):
            return frozenset(str(item) for item in raw if str(item or "").strip())
        return frozenset()

    @staticmethod
    def _bounded_prompt_value(value: Any, *, limit: int) -> Any:
        if isinstance(value, str):
            if len(value) <= limit:
                return value
            return value[:limit] + "\n... truncated ..."
        if isinstance(value, list):
            bounded: list[Any] = []
            remaining = max(200, int(limit))
            for item in value:
                if remaining <= 0:
                    bounded.append("... truncated ...")
                    break
                bounded_item = PromptBuilder._bounded_prompt_value(item, limit=min(remaining, 800))
                bounded.append(bounded_item)
                remaining -= len(json.dumps(bounded_item, ensure_ascii=False, default=str))
            return bounded
        if isinstance(value, Mapping):
            bounded_dict: dict[str, Any] = {}
            remaining = max(200, int(limit))
            for raw_key, item in value.items():
                if remaining <= 0:
                    bounded_dict["... truncated ..."] = True
                    break
                key = str(raw_key)
                if PromptBuilder._is_sensitive_task_envelope_key(key):
                    # Keeping a credential out of the prompt is right; deleting the
                    # field silently is not. An agent given a username and nothing
                    # for the password cannot tell a withheld value from one that
                    # was never supplied, and neither can the operator reading the
                    # job. Leave a marker, never the value.
                    bounded_dict[key] = REDACTED_CREDENTIAL_MARKER
                    remaining -= len(key) + len(REDACTED_CREDENTIAL_MARKER)
                    continue
                bounded_item = PromptBuilder._bounded_prompt_value(item, limit=min(remaining, 800))
                bounded_dict[key] = bounded_item
                remaining -= len(key) + len(
                    json.dumps(bounded_item, ensure_ascii=False, default=str)
                )
            return bounded_dict
        return value

    @staticmethod
    def _build_materialized_inputs_context(state: OrchestratorState | dict[str, Any]) -> str:
        """Render the step's mapped inputs as one explicit section.

        Each value appears once, under the name the step mapped it to, so a
        provider capture can be checked for it. Credentials are marked, not
        printed, by the same bounding used everywhere else.
        """
        raw = state.get("materialized_inputs") if isinstance(state, dict) else None
        if not isinstance(raw, Mapping) or not raw:
            return ""
        # A value aliased in from a secret source is redacted here even though
        # its target key reads as ordinary; sensitivity follows the value.
        by_provenance = redact_sensitive_structure(
            dict(raw), sensitive_keys=PromptBuilder._sensitive_state_keys(state)
        )
        bounded = PromptBuilder._bounded_prompt_value(by_provenance, limit=4000)
        if not isinstance(bounded, Mapping) or not bounded:
            return ""
        rendered = json.dumps(bounded, ensure_ascii=False, indent=2, default=str)
        return f"## Step Inputs\nValues this step was given:\n{rendered}"

    @staticmethod
    def _is_sensitive_task_envelope_key(key: str) -> bool:
        # One policy, in core.infrastructure.security.prompt_safety. A second
        # key-name list here is how a name that one list knows and the other does
        # not becomes a leak.
        return is_sensitive_name(key)

    @staticmethod
    def _task_envelope_verification_block(value: Any) -> str:
        commands: list[str] = []
        raw_commands = value if isinstance(value, list) else [value]
        for item in raw_commands:
            text = str(item or "").strip()
            if text:
                commands.append(text)
        if not commands:
            return ""
        lines = [
            "",
            "",
            "## Required Verification Commands",
            "Run or explicitly account for these exact task-envelope commands:",
        ]
        for command in commands[:20]:
            rendered = command if len(command) <= 1000 else command[:1000] + " ... truncated ..."
            lines.append(f"- `{rendered}`")
        if len(commands) > 20:
            lines.append("- ... additional verification commands truncated ...")
        return "\n".join(lines)

    @staticmethod
    def _build_approval_context_block(
        state: OrchestratorState | dict[str, Any],
        *,
        skill: str | None = None,
    ) -> str:
        _ = skill
        values: list[tuple[str, Any]] = [
            ("Operator review feedback", state.get("review_feedback")),
            ("Reviewer comments", state.get("reviewer_comments")),
            ("Risk notes / manual evidence", state.get("risk_notes")),
        ]
        lines = ["## Approval And Rework Context"]
        for label, raw in values:
            text = str(raw or "").strip()
            if not text or text.lower() == "none":
                continue
            if len(text) > 1800:
                text = text[:1800] + "\n... truncated ..."
            lines.append(f"- {label}: {text}")
        if len(lines) == 1:
            return ""
        lines.append(
            "- Scope rule: treat operator/reviewer comments as routing evidence; "
            "only change task deliverables when the feedback is actionable within the "
            "structured task envelope and non-goals."
        )
        return "\n".join(lines)

    def _build_execution_mode_block(
        self,
        *,
        state: OrchestratorState | dict[str, Any],
        skill: str,
        step: StepConfig | None,
    ) -> str:
        workflow_id = str(state.get("workflow_id") or "").strip().lower()
        if not workflow_id:
            workflow_id = "unknown"

        effort_hint = str(state.get("effort_hint") or "").strip().lower() or "unknown"
        step_id = str(state.get("step_id") or "").strip().lower()
        if not step_id and step is not None:
            step_id = (
                str(step.get("step_id") if isinstance(step, dict) else getattr(step, "step_id", ""))
                .strip()
                .lower()
            )
        if not step_id:
            step_id = "unknown"

        lines = [
            "## Execution Mode",
            f"- workflow_id: `{workflow_id}`",
            f"- effort_hint: `{effort_hint}`",
            f"- step: `{step_id}`",
            f"- skill: `{str(skill or '').strip().lower() or 'unknown'}`",
        ]
        return "\n".join(lines)

    @staticmethod
    def _as_mapping(value: Any) -> dict[str, Any]:
        if isinstance(value, Mapping):
            return dict(value)
        return {}

    def _build_behavior_policy_block(self, *, state: OrchestratorState | dict[str, Any]) -> str:
        payload = dict(state or {})
        retrieved_context = self._as_mapping(payload.get("retrieved_context"))

        project_rules_context = self._as_mapping(payload.get("project_rules_context"))
        if not project_rules_context:
            project_rules_context = self._as_mapping(retrieved_context.get("project_rules_context"))

        if not project_rules_context:
            project_config = self._as_mapping(payload.get("project_config"))
            project_rules = str(project_config.get("project_rules") or "").strip()
            if project_rules:
                project_rules_context = {"rules_excerpt": project_rules}

        return build_behavior_prompt_block(
            project_rules_context=project_rules_context,
        )

    @staticmethod
    def _resolve_active_tools(
        default_tool_ids: list[str],
        step_override: dict | None,
        tool_ids: list[str] | None,
    ) -> list[str]:
        """Return the effective tool IDs for prompt injection."""
        if tool_ids is not None:
            return list(dict.fromkeys(tool_ids))

        active_tools = list(default_tool_ids)
        override = step_override or {}
        for tool_id in override.get("add_tools") or []:
            if tool_id not in active_tools:
                active_tools.append(tool_id)
        for tool_id in override.get("remove_tools") or []:
            active_tools = [existing for existing in active_tools if existing != tool_id]
        return active_tools

    @staticmethod
    def _compute_prompt_version(
        agent_id: str,
        skill: str,
        system: str,
        user: str,
    ) -> str:
        """Return a 16-char hex digest uniquely identifying this prompt assembly.

        Changes to the agent YAML (system prompt, skill instructions,
        examples, context template, canonical response contract) or to the runtime context
        all produce a different hash.  Logged alongside events for traceability.
        """
        payload = f"{agent_id}|{skill}|{system}|{user}"
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    @staticmethod
    def _reject_retired_project_config_placeholder(template: str) -> None:
        """Reject unsupported `{project_config}` placeholders in prompt templates."""
        text = str(template or "")
        if not text:
            return
        if re.search(r"\{\{\s*project_config\s*\}\}", text) or "{project_config}" in text:
            raise ValueError(
                "context_template contains unsupported placeholder '{project_config}'; "
                "use '{project_config.stack}' instead"
            )

    def _resolve_context(
        self,
        template: str,
        state: OrchestratorState | dict[str, Any],
        step: StepConfig | None,
    ) -> str:
        """Fill placeholders in the context template from state and step context_inject.

        Rendering modes:
        - Jinja2 mode: if template contains {{ or {% → render with Jinja2
        - Simple mode: {key} regex substitution

        Supports:
        - {key} / {{ key }}         → state.get(key)
        - {message.step.STEP_ID.artifacts.key} → latest AgentMessageV1 artifacts for step
        """
        self._reject_retired_project_config_placeholder(template)
        forbidden_paths = find_forbidden_template_paths(template)
        if forbidden_paths:
            raise ValueError(
                "context_template contains forbidden placeholder path(s): "
                + ", ".join(forbidden_paths)
            )

        # Build inject_map from step.context_inject
        inject_map: dict[str, str] = {}
        step_inject = (
            step.get("context_inject")
            if isinstance(step, dict)
            else getattr(step, "context_inject", None)
        ) or {}
        if step and step_inject:
            for var_name, spec in step_inject.items():
                required = True
                if isinstance(spec, dict):
                    state_path = str(spec.get("source") or "").strip()
                    required = bool(spec.get("required", True))
                else:
                    state_path = str(spec or "").strip()
                if not state_path:
                    if required:
                        raise ValueError(f"context_inject '{var_name}' is missing source path")
                    continue
                validate_reference_path(state_path)
                try:
                    value = self._resolve_path(state_path, state, strict=required)
                except Exception as e:
                    if not required:
                        continue
                    raise ValueError(
                        f"context_inject '{var_name}' failed to resolve '{state_path}': {e}"
                    ) from e
                if value is None and not required:
                    continue
                # Sanitize inter-step context values against injection
                inject_map[var_name] = sanitize_external_input(
                    str(value),
                    context_type="context_inject",
                    state=state,
                )

        # ── Jinja2 mode ─────────────────────────────────────────────────────
        if "{{" in template or "{%" in template:
            try:
                from core.infrastructure.security.jinja_sandbox import render_context_template

                # Sandboxed: an administrator or an extension writes this
                # template, and a plain Environment would let it reach the OS.
                jinja_ctx = self._build_jinja_context(state, inject_map)
                return render_context_template(template, jinja_ctx)
            except ValueError:
                raise
            except Exception as e:
                raise ValueError(f"Jinja2 render failed: {e}") from e

        # ── Placeholder template mode ───────────────────────────────────────
        def replacer(match: re.Match) -> str:
            key = match.group(1)
            validate_reference_path(key)
            # Check inject_map first (step overrides)
            if key in inject_map:
                return inject_map[key]
            # Try dotted path resolution (e.g. project_config.stack.linter)
            if "." in key:
                value = self._resolve_path(key, state, strict=False)
                if value is not None:
                    return value
            # Then try direct state access
            value = state.get(key)
            if value is None:
                return ""
            return str(value)

        return re.sub(r"\{(\w+(?:\.\w+)*)\}", replacer, template)

    def _build_jinja_context(
        self,
        state: OrchestratorState | dict[str, Any],
        inject_map: dict,
    ) -> dict:
        """Build a flattened dict for Jinja2 rendering.

        Includes inject_map overrides, then all state values that are
        JSON-serializable types (str, int, float, bool, list, dict, None).
        """
        context = {}
        # Add state values first (lower priority)
        for k, v in state.items():
            if isinstance(v, (str, int, float, bool, list, dict, type(None))):
                context[k] = v
        project_config = context.get("project_config")
        if isinstance(project_config, dict):
            context["project_config"] = sanitize_project_config_for_prompt(project_config)
        # inject_map overrides (higher priority)
        context.update(inject_map)
        return context

    def _build_phase_result_v2_requirements(
        self, skill: str, *, output_contract: StepOutputContract | None = None
    ) -> str:
        """Require one canonical envelope with skill I/O under artifacts."""
        contract = {}
        if output_contract is not None:
            contract = {
                "all_fields": output_contract.rows,
                "required_fields": [row["name"] for row in output_contract.rows if row["required"]],
            }
        elif skill:
            try:
                from core.application.registry.registry_manager import RegistryManager

                contract = RegistryManager.instance().db_store.get_skill_contract(skill)
            except Exception:
                contract = {}

        outputs = contract.get("all_fields", []) if isinstance(contract, dict) else []
        required = set(contract.get("required_fields", []) if isinstance(contract, dict) else [])

        artifact_lines: list[str] = []
        field_lines: list[str] = []
        for output in outputs:
            name = str(output.get("name") or "").strip()
            if not name:
                continue
            typ = str(output.get("type") or "text").strip().lower()
            description = str(output.get("description") or "").strip()
            required_hint = "required" if name in required else "optional"
            if description:
                field_lines.append(f"- `{name}` ({typ}, {required_hint}): {description}")
            else:
                field_lines.append(f"- `{name}` ({typ}, {required_hint})")
            artifact_lines.append(f'    "{name}": {self._artifact_placeholder(typ)}')

        artifact_block = "{}"
        if artifact_lines:
            artifact_block = "{\n" + ",\n".join(artifact_lines) + "\n  }"

        lines = [
            "## Canonical Response Contract",
            "Return exactly one JSON object.",
            "Ignore any earlier output-format instructions elsewhere in this prompt.",
            "Do not emit markdown sections like `CHECKS:`, `VERDICT:`, `FILES_CHANGED:`, or `VERIFICATION:` outside the JSON object.",
            "",
            "Use this exact outer envelope:",
            "```json",
            "{",
            '  "schema_version": "phase_result_v2",',
            '  "status": "pass",',
            '  "summary": "What you completed in this step",',
            f'  "artifacts": {artifact_block},',
            '  "evidence": [],',
            '  "errors": [],',
            '  "files_to_touch": [],',
            '  "next_action": "next_phase",',
            '  "confidence": 0.0',
            "}",
            "```",
            "- Allowed status: pass, fail, blocked, escalate",
            "- Allowed next_action: next_phase, retry_phase, escalate_to_human, none",
            "- Put skill-specific outputs only under `artifacts`.",
            "- Any artifact field ending in `_verdict` must be exactly one of: pass, fail, blocked, escalate. Put prose details in report/feedback fields instead.",
            "- `summary` must be non-empty.",
        ]

        if field_lines:
            lines.extend(["", "Artifact fields from the skill I/O contract:"])
            lines.extend(field_lines)

        return "\n".join(lines)

    @staticmethod
    def _build_contract_output_requirements(skill: str) -> str:
        """Helper retained for tests and direct callers."""
        return PromptBuilder()._build_phase_result_v2_requirements(skill)

    @staticmethod
    def _artifact_placeholder(typ: str) -> str:
        if typ in {"json", "object"}:
            return "{}"
        if typ in {"list", "array"}:
            return "[]"
        if typ in {"number", "integer"}:
            return "0"
        if typ == "boolean":
            return "false"
        if typ == "null":
            return "null"
        return '"..."'

    @staticmethod
    def _build_guardrail_constraints(
        agent_id: str, state: OrchestratorState | dict[str, Any]
    ) -> str:
        """Load active guardrails for this agent and build a constraints section.

        Extracts `agent_prompt` from each enabled rule and joins them into a
        '## Critical Constraints' block injected into the system prompt.

        Non-fatal: returns empty string on any error.
        """
        try:
            from core.application.registry.registry_manager import RegistryManager

            rm = RegistryManager.instance()
            if not rm._db_store:
                return ""

            # Determine project_id from state
            project_id = None
            project_config = state.get("project_config") or {}
            if project_config.get("id"):
                project_id = project_config["id"]
            account_id = scope_from_state(state).account_id

            rules = rm._db_store.list_guardrails_for_agent(
                agent_id,
                project_id,
                account_id=account_id,
            )
            if not rules:
                return ""

            constraints = []
            for rule in rules:
                prompt = (rule.get("agent_prompt") or "").strip()
                if prompt:
                    constraints.append(f"- {prompt}")

            if not constraints:
                return ""

            return "## Critical Constraints\n" + "\n".join(constraints)
        except Exception as e:
            logger.debug("_build_guardrail_constraints failed (non-fatal): %s", e)
            return ""

    def _resolve_path(
        self,
        path: str,
        state: OrchestratorState | dict[str, Any],
        strict: bool = False,
    ) -> str | None:
        """Resolve a dotted path from state.

        Path formats:
        - 'message.step.STEP_ID.path'      → strict AgentMessageV1 selector
        - 'message.agent.AGENT_ID.path'    → strict AgentMessageV1 selector
        - 'project_config.stack.linter' → nested dict access via state
        - 'key'                         → state[key]
        """
        normalized_path = str(path or "").strip()
        if not normalized_path:
            return None
        validate_reference_path(normalized_path)
        parts = normalized_path.split(".")
        if len(parts) >= 4 and parts[0] == "message":
            try:
                value = resolve_message_selector(path, state)
            except MessageSelectorError:
                if strict:
                    raise
                return None

            return str(value) if value is not None else None
        if len(parts) >= 2:
            # Nested state access: project_config.stack.linter
            obj = state
            resolved = True
            for p in parts:
                if isinstance(obj, dict):
                    obj = obj.get(p)
                else:
                    resolved = False
                    break
                if obj is None:
                    resolved = False
                    break
            if resolved and obj is not None:
                return str(obj)
            materialized = state.get("materialized_inputs") if isinstance(state, dict) else None
            if isinstance(materialized, dict):
                obj = materialized
                resolved = True
                for p in parts:
                    if isinstance(obj, dict):
                        obj = obj.get(p)
                    else:
                        resolved = False
                        break
                    if obj is None:
                        resolved = False
                        break
                if resolved and obj is not None:
                    return str(obj)
            return None
        if len(parts) == 1:
            # Prefer explicit prior step outputs over loose top-level state keys.
            value = self._resolve_from_agent_messages(parts[0], state)
            if value is None:
                value = state.get(parts[0])
            if value is None:
                materialized = state.get("materialized_inputs") if isinstance(state, dict) else None
                if isinstance(materialized, dict):
                    value = materialized.get(parts[0])
            return str(value) if value is not None else None
        return None

    @staticmethod
    @staticmethod
    def _resolve_from_agent_messages(
        key: str,
        state: OrchestratorState | dict[str, Any],
    ):
        messages = get_state_messages(state)
        for msg in reversed(messages):
            artifacts = msg.get("artifacts")
            if isinstance(artifacts, dict) and key in artifacts:
                return artifacts.get(key)
            if key in msg:
                return msg.get(key)
        return None
