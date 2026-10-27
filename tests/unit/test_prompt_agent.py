"""
Unit tests for agents/prompt_agent.py — PromptAgent (the sole agent execution path).

Tests:
- _build_tool_config() produces correct runtime configuration
- _get_context_sections() returns correct sections per agent/skill
- _extract_verdict() parses review verdicts
- _parse_artifacts() extracts structured artifacts from LLM output
- Status mapping (review_verdict=fail → needs_rework)

Run:
    docker compose exec fs-app python -m pytest tests/unit/test_prompt_agent.py -v
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
try:
    from langchain_core.messages import AIMessage, HumanMessage
except ModuleNotFoundError as exc:
    if getattr(exc, "name", "") == "langchain_core":
        pytest.skip("langchain_core is not installed", allow_module_level=True)
    raise


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_AGENT_CONTEXT_SECTIONS = {
    "composer": {
        "default": [
            "env_snapshot",
            "reference_context",
            "schema_notes",
            "architecture",
            "graph_context",
        ],
        "apply_revision": [
            "env_snapshot",
            "architecture",
            "lint_findings",
            "check_results",
            "reference_context",
        ],
    },
    "proofreader": {
        "default": ["env_snapshot", "architecture", "lint_findings", "check_results"],
    },
    "critic": {
        "default": ["architecture", "lint_findings"],
    },
    "scout": {
        "default": ["env_snapshot", "reference_context", "kb_context"],
    },
}


def _make_manifest(
    agent_id: str = "composer",
    tools: list | None = None,
    skills: list | None = None,
    config: dict | None = None,
    tags: list | None = None,
):
    """Create a minimal mock AgentManifest."""
    m = MagicMock()
    m.agent_id = agent_id
    m.display_name = agent_id.replace("_", " ").title()
    m.tools = tools or []
    m.skills = skills or ["compose_report"]
    m.config = config or {}
    m.max_iterations = 10
    m.tags = tags or []
    m.prompts = MagicMock()
    m.prompts.system = "You are a test agent."
    m.prompts.skill_prompts = {}
    m.prompts.context_template = ""
    m.prompts.output_format = ""
    m.prompts.context_sections = _AGENT_CONTEXT_SECTIONS.get(agent_id, {})
    m.prompts.examples = {}
    return m


def _authorized_prompt_agent(manifest):
    """Give execution tests an explicit assigned-skill registry in their own scope."""
    from core.application.auth.config import DEFAULT_ACCOUNT_ID
    from core.application.orchestration.agents.prompt_agent import PromptAgent
    from core.application.registry.skill_registry import SkillRegistry

    rows = {
        skill: {
            "skill_id": skill,
            "display_name": skill,
            "enabled": True,
            "account_id": DEFAULT_ACCOUNT_ID,
            "instructions": [],
        }
        for skill in manifest.skills
    }
    store = types.SimpleNamespace(list_skills=lambda: list(rows.values()), get_skill=rows.get)
    return PromptAgent(manifest, skill_registry=SkillRegistry(store))


def _make_state(**overrides) -> dict:
    """Create a minimal state dict for testing."""
    from core.application.auth.config import DEFAULT_ACCOUNT_ID

    base = {
        "account_id": DEFAULT_ACCOUNT_ID,
        "project_name": "test-project",
        "project_root": "/target",
        "container_root": "/target",
        "project_config": {
            "git_root": "/target",
            "default_branch": "main",
            "stack": {
                "linter": "flake8",
                "test_runner": "pytest tests/ -v",
            },
        },
        "task_id": "TASK-123",
        "task_summary": "Fix the login bug",
        "branch_name": "",
        "branch_fork_of": "",
    }
    base.update(overrides)
    return base


def _implementation_contract_fields() -> set[str]:
    return {
        "workspace_state",
        "git_diff",
        "files_changed",
        "commit_sha",
        "developer_verification",
        "checks_run",
        "checks_skipped",
        "blocking_issues",
    }


def _mark_contract_driven_implementation_agent(agent):
    return agent


def _mark_read_only_change_contract_agent(agent):
    return agent


# ---------------------------------------------------------------------------
# PromptAgent instantiation
# ---------------------------------------------------------------------------


class TestPromptAgentInit:
    """Test PromptAgent can be instantiated."""

    def test_create_prompt_agent(self):
        from core.application.orchestration.agents.prompt_agent import PromptAgent

        manifest = _make_manifest()
        agent = PromptAgent(manifest)
        assert agent.manifest is manifest
        assert agent.tool_registry is None

    def test_create_with_tool_registry(self):
        from core.application.orchestration.agents.prompt_agent import PromptAgent

        manifest = _make_manifest()
        registry = MagicMock()
        agent = PromptAgent(manifest, tool_registry=registry)
        assert agent.tool_registry is registry


class TestExecutionErrorDiagnostics:
    def test_extract_execution_error_metadata_reads_provider_http_fields(self):
        from core.application.orchestration.agents.prompt_agent import PromptAgent

        class _FakeResponse:
            status_code = 502
            headers: ClassVar[dict[str, str]] = {"x-request-id": "req_123"}
            text: ClassVar[str] = '{"error":"upstream unavailable"}'

        class _FakeProviderError(Exception):
            def __init__(self):
                super().__init__("Connection error.")
                self.response = _FakeResponse()
                self.code = "upstream_unavailable"
                self.type = "APIConnectionError"
                self.retryable = True

        err = _FakeProviderError()
        details = PromptAgent._extract_execution_error_metadata(err, provider_hint="openai")

        assert details["provider_name"] == "openai"
        assert details["http_status"] == 502
        assert details["provider_error_code"] == "upstream_unavailable"
        assert details["provider_error_type"] == "APIConnectionError"
        assert details["request_id"] == "req_123"
        assert details["retryable"] is True
        assert "upstream unavailable" in str(details["response_excerpt"])

    def test_extract_execution_error_metadata_reads_chained_provider_error(self):
        from core.application.orchestration.agents.prompt_agent import PromptAgent

        class _FakeProviderCause(Exception):
            status_code = 429
            request_id = "req_chained"
            error_code = "rate_limit"
            error_type = "RateLimitError"

        err = RuntimeError("outer")
        err.__cause__ = _FakeProviderCause("provider failed")

        details = PromptAgent._extract_execution_error_metadata(err)

        assert details["http_status"] == 429
        assert details["provider_error_code"] == "rate_limit"
        assert details["provider_error_type"] == "RateLimitError"
        assert details["request_id"] == "req_chained"
        assert details["retryable"] is True

    def test_format_execution_error_includes_diagnostics_suffix(self):
        from core.application.orchestration.agents.prompt_agent import PromptAgent

        err = RuntimeError("Connection error.")
        text = PromptAgent._format_execution_error(
            err,
            diagnostics={
                "provider_name": "openai",
                "http_status": 429,
                "provider_error_code": "rate_limit",
                "provider_error_type": "RateLimitError",
                "request_id": "req_abc",
            },
        )

        assert "provider=openai" in text
        assert "http_status=429" in text
        assert "provider_code=rate_limit" in text
        assert "provider_type=RateLimitError" in text
        assert "request_id=req_abc" in text


class TestPromptAgentExecutionTimeouts:
    def test_standard_provider_path_forwards_manifest_timeout(self):
        from core.application.orchestration.agents.base import AgentInput

        manifest = _make_manifest(
            agent_id="designer",
            skills=["design_solution"],
            config={"timeout_minutes": 0.5},
        )
        manifest.timeout_minutes = 0.5
        manifest.max_iterations = 3

        agent = _authorized_prompt_agent(manifest)
        agent_input = AgentInput(
            task_context={},
            step_context={},
            agent_config={},
            skill="design_solution",
            state_ref=_make_state(),
        )

        with (
            patch(
                "core.application.orchestration.agents.prompt_agent.PromptBuilder"
            ) as builder_cls,
            patch("core.application.orchestration.agents.prompt_agent.emit_event"),
            patch(
                "core.application.orchestration.agents.prompt_agent._resolve_provider",
                return_value="openai",
            ),
            patch(
                "core.application.orchestration.agents.prompt_agent.invoke_agent",
                return_value=types.SimpleNamespace(content="done"),
            ) as invoke_mock,
            patch.object(agent, "_resolve_tools", return_value=[]),
            patch.object(agent, "_get_context_sections", return_value=[]),
            patch.object(agent, "_log_prompt_request"),
            patch.object(agent, "_log_prompt_response"),
            patch.object(agent, "_parse_artifacts", return_value={}),
            patch.object(agent, "_extract_verdict", return_value={}),
            patch.object(agent, "validate_agent_output", return_value=[]),
            patch.object(
                agent,
                "_build_phase_result_v2",
                return_value=({"status": "pass", "summary": "done", "confidence": 0.9}, []),
            ),
        ):
            builder = builder_cls.return_value
            builder.build.return_value = ("system prompt", "user prompt", "v1")
            output = agent.execute(agent_input)

        assert output.status == "pass"
        assert invoke_mock.call_args.kwargs["timeout_seconds"] == 30
        final_schema = invoke_mock.call_args.kwargs["final_response_schema"]
        assert isinstance(final_schema, dict)
        assert final_schema["title"] == "PhaseResultV2"
        assert final_schema["properties"]["schema_version"]["enum"] == ["phase_result_v2"]

    def test_execute_returns_normalized_phase_result_artifacts(self):
        from core.application.orchestration.agents.base import AgentInput

        manifest = _make_manifest(
            agent_id="inspector",
            skills=["inspect_content"],
            config={},
        )
        manifest.timeout_minutes = 1
        manifest.max_iterations = 3

        agent = _authorized_prompt_agent(manifest)
        agent_input = AgentInput(
            task_context={},
            step_context={},
            agent_config={},
            skill="inspect_content",
            state_ref=_make_state(),
        )

        response_text = (
            "```json\n"
            "{"
            '"schema_version":"phase_result_v2",'
            '"status":"pass",'
            '"summary":"security scan complete",'
            '"artifacts":{"content_verdict":"PASS: No security issues found."}'
            "}\n```"
        )

        with (
            patch(
                "core.application.orchestration.agents.prompt_agent.PromptBuilder"
            ) as builder_cls,
            patch("core.application.orchestration.agents.prompt_agent.emit_event"),
            patch(
                "core.application.orchestration.agents.prompt_agent._resolve_provider",
                return_value="openai",
            ),
            patch(
                "core.application.orchestration.agents.prompt_agent.invoke_agent",
                return_value=types.SimpleNamespace(content=response_text),
            ),
            patch.object(agent, "_resolve_tools", return_value=[]),
            patch.object(agent, "_get_context_sections", return_value=[]),
            patch.object(agent, "_log_prompt_request"),
            patch.object(agent, "_log_prompt_response"),
            patch.object(agent, "validate_agent_output", return_value=[]),
        ):
            builder = builder_cls.return_value
            builder.build.return_value = ("system prompt", "user prompt", "v1")
            output = agent.execute(agent_input)

        assert output.status == "pass"
        assert output.artifacts["content_verdict"] == "pass"

    def test_execute_flattens_responses_api_content_blocks_before_artifact_parsing(self):
        from core.application.orchestration.agents.base import AgentInput

        manifest = _make_manifest(
            agent_id="inspector",
            skills=["inspect_content"],
            config={},
        )
        agent = _authorized_prompt_agent(manifest)
        agent_input = AgentInput(
            task_context={},
            step_context={},
            agent_config={},
            skill="inspect_content",
            state_ref=_make_state(),
        )
        block_content = [
            {"type": "reasoning", "summary": []},
            {
                "type": "text",
                "text": (
                    "```json\n"
                    '{"schema_version":"phase_result_v2","status":"pass",'
                    '"summary":"security scan complete","artifacts":'
                    '{"content_verdict":"PASS: No security issues found."}}'
                    "\n```"
                ),
            },
        ]

        with (
            patch(
                "core.application.orchestration.agents.prompt_agent.PromptBuilder"
            ) as builder_cls,
            patch("core.application.orchestration.agents.prompt_agent.emit_event"),
            patch(
                "core.application.orchestration.agents.prompt_agent._resolve_provider",
                return_value="openai",
            ),
            patch(
                "core.application.orchestration.agents.prompt_agent.invoke_agent",
                return_value=types.SimpleNamespace(content=block_content),
            ),
            patch.object(agent, "_resolve_tools", return_value=[]),
            patch.object(agent, "_get_context_sections", return_value=[]),
            patch.object(agent, "_log_prompt_request"),
            patch.object(agent, "_log_prompt_response"),
            patch.object(agent, "validate_agent_output", return_value=[]),
        ):
            builder_cls.return_value.build.return_value = (
                "system prompt",
                "user prompt",
                "v1",
            )
            output = agent.execute(agent_input)

        assert output.status == "pass"
        assert output.artifacts["content_verdict"] == "pass"
        assert isinstance(output.artifacts["raw_output"], str)

    def test_execute_returns_typed_fail_without_continuing_after_provider_exception(self):
        from core.application.orchestration.agents.base import AgentInput

        agent = _authorized_prompt_agent(
            _make_manifest(agent_id="composer", skills=["compose_report"])
        )
        agent_input = AgentInput(
            task_context={},
            step_context={},
            agent_config={},
            skill="compose_report",
            state_ref=_make_state(),
        )

        with (
            patch(
                "core.application.orchestration.agents.prompt_agent.PromptBuilder"
            ) as builder_cls,
            patch(
                "core.application.orchestration.agents.prompt_agent.emit_event"
            ) as emit_event_mock,
            patch(
                "core.application.orchestration.agents.prompt_agent._resolve_provider",
                return_value="openai",
            ),
            patch(
                "core.application.orchestration.agents.prompt_agent.invoke_agent",
                side_effect=RuntimeError("provider exploded"),
            ),
            patch.object(agent, "_resolve_tools", return_value=[]),
            patch.object(agent, "_get_context_sections", return_value=[]),
            patch.object(agent, "_log_prompt_request"),
            patch.object(agent, "_log_prompt_response"),
            patch.object(agent, "_parse_artifacts") as parse_artifacts,
        ):
            builder_cls.return_value.build.return_value = (
                "system prompt",
                "user prompt",
                "v1",
            )
            output = agent.execute(agent_input)

        assert output.status == "fail"
        assert output.artifacts["_execution_error"] is True
        assert output.artifacts["error_type"] == "RuntimeError"
        parse_artifacts.assert_not_called()
        assert not any(call.args[2] == "agent_completed" for call in emit_event_mock.call_args_list)

    def test_execute_projects_structured_http_tool_failure_on_host_owned_channel(self):
        from core.application.orchestration.agents.base import AgentInput
        from core.application.orchestration.retry_policy import OperationRetrySignalError

        agent = _authorized_prompt_agent(
            _make_manifest(agent_id="composer", skills=["compose_report"])
        )
        agent_input = AgentInput(
            task_context={},
            step_context={},
            agent_config={},
            skill="compose_report",
            state_ref=_make_state(),
        )
        signal = {
            "failure_class": "provider",
            "retryable": True,
            "request_semantics": "safe_read",
            "definitely_no_external_effect": True,
            "external_effect_status": "failed",
            "http_status": 429,
        }

        with (
            patch(
                "core.application.orchestration.agents.prompt_agent.PromptBuilder"
            ) as builder_cls,
            patch("core.application.orchestration.agents.prompt_agent.emit_event"),
            patch(
                "core.application.orchestration.agents.prompt_agent._resolve_provider",
                return_value="openai",
            ),
            patch(
                "core.application.orchestration.agents.prompt_agent.invoke_agent",
                side_effect=OperationRetrySignalError(
                    signal,
                    operation_deadline_epoch_seconds=130.0,
                ),
            ),
            patch.object(agent, "_resolve_tools", return_value=[]),
            patch.object(agent, "_get_context_sections", return_value=[]),
            patch.object(agent, "_log_prompt_request"),
            patch.object(agent, "_log_prompt_response"),
        ):
            builder_cls.return_value.build.return_value = (
                "system prompt",
                "user prompt",
                "v1",
            )
            output = agent.execute(agent_input)

        assert output.status == "fail"
        assert output.operation_retry_signal == {
            "schema_version": "operation_retry_signal_v1",
            "provenance": "agent_managed_custom_http",
            "signal": signal,
            "deadline_epoch_seconds": 130.0,
        }
        assert not set(signal).intersection(output.artifacts)

    def test_execute_returns_typed_blocked_when_step_requires_unavailable_runtime_tools(self):
        from core.application.orchestration.agents.base import AgentInput

        agent = _authorized_prompt_agent(
            _make_manifest(
                agent_id="scout",
                tools=["drive_read", "sheet_read"],
                skills=["summarize_brief"],
            )
        )
        agent_input = AgentInput(
            task_context={},
            step_context={
                "_step": {"override": {"require_runtime_tools": True}},
            },
            agent_config={},
            skill="summarize_brief",
            state_ref=_make_state(),
        )

        with (
            patch(
                "core.application.orchestration.agents.prompt_agent.PromptBuilder"
            ) as builder_cls,
            patch("core.application.orchestration.agents.prompt_agent.emit_event"),
            patch.object(agent, "_resolve_tools", return_value=[]),
            patch(
                "core.application.orchestration.agents.prompt_agent.invoke_agent"
            ) as invoke_agent_mock,
        ):
            builder_cls.return_value.build.return_value = (
                "system prompt",
                "user prompt",
                "v1",
            )
            output = agent.execute(agent_input)

        assert output.status == "blocked"
        assert output.artifacts["code"] == "required_tool_unavailable"
        assert output.artifacts["required_tool_ids"] == [
            "drive_read",
            "sheet_read",
        ]
        assert "read-only runtime tools" in output.summary
        invoke_agent_mock.assert_not_called()

    def test_resolve_final_response_schema_prefers_published_block_outputs(self):
        from core.application.orchestration.agents.prompt_agent import PromptAgent

        agent = PromptAgent(_make_manifest(agent_id="designer", skills=["outline_plan"]))
        fake_db = MagicMock()
        fake_db.get_published_block_contract.return_value = {
            "block_key": "designer.outline_plan",
            "outputs": [
                {
                    "field_key": "architecture",
                    "value_type": "text",
                    "required_by_default": True,
                    "exposed_by_default": True,
                },
                {
                    "field_key": "files_to_touch",
                    "value_type": "list",
                    "required_by_default": True,
                    "exposed_by_default": True,
                },
                {
                    "field_key": "outline_doc",
                    "value_type": "json",
                    "required_by_default": False,
                    "exposed_by_default": True,
                },
            ],
        }
        fake_db.get_skill_outputs.return_value = [
            {"name": "legacy_skill_output", "type": "text", "required": True},
        ]
        fake_registry = MagicMock()
        fake_registry.db_store = fake_db

        with patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=fake_registry,
        ):
            schema = agent._resolve_final_response_schema(
                "outline_plan",
                step={"override": {"published_block_key": "designer.outline_plan"}},
            )

        artifacts = schema["properties"]["artifacts"]
        assert artifacts["properties"]["architecture"]["type"] == "string"
        assert artifacts["properties"]["files_to_touch"]["type"] == "array"
        architecture_doc_schema = artifacts["properties"]["outline_doc"]
        if "type" in architecture_doc_schema:
            assert architecture_doc_schema["type"] == "object"
        else:
            assert any(
                item.get("type") == "object"
                for item in architecture_doc_schema.get("anyOf", [])
                if isinstance(item, dict)
            )
        required_fields = set(artifacts.get("required", []))
        assert {"architecture", "files_to_touch"}.issubset(required_fields)
        assert "legacy_skill_output" not in artifacts["properties"]

    def test_resolve_final_response_schema_without_step_uses_skill_outputs(self):
        from core.application.orchestration.agents.prompt_agent import PromptAgent

        agent = PromptAgent(_make_manifest(agent_id="designer", skills=["outline_plan"]))
        fake_db = MagicMock()
        fake_db.get_skill_outputs.return_value = [
            {"name": "architecture", "type": "text", "required": True},
            {"name": "files_to_touch", "type": "list", "required": True},
        ]
        fake_registry = MagicMock()
        fake_registry.db_store = fake_db

        with patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=fake_registry,
        ):
            schema = agent._resolve_final_response_schema("outline_plan")

        artifacts = schema["properties"]["artifacts"]
        assert set(artifacts["properties"].keys()) == {"architecture", "files_to_touch"}


class _FakePrompt:
    def format_messages(self, **kwargs):
        return [HumanMessage(content="do the task")]


class _StructuredRunnable:
    def __init__(self, payload):
        self.payload = payload

    def invoke(self, messages, **kwargs):
        return self.payload


class _FakeChatModel:
    def __init__(
        self,
        *,
        draft_response: AIMessage,
        structured_payload: dict,
        provider_policy: str = "openai",
    ):
        self.draft_response = draft_response
        self.structured_payload = structured_payload
        self.metadata = {"flow_steward_provider_policy": provider_policy}
        self.structured_calls: list[dict] = []
        self.invoke_calls = 0

    def invoke(self, messages, **kwargs):
        self.invoke_calls += 1
        return self.draft_response

    def bind_tools(self, tools, **kwargs):
        return self

    def with_structured_output(self, schema=None, **kwargs):
        self.structured_calls.append({"schema": schema, "kwargs": kwargs})
        return _StructuredRunnable(self.structured_payload)


class TestInvokeAgentStructuredOutput:
    def test_registered_tool_failure_stops_llm_loop_and_preserves_retry_signal(self):
        from core.application.orchestration.agents.common import invoke_agent
        from core.application.orchestration.retry_policy import OperationRetrySignalError

        class _SequencedChatModel:
            def __init__(self):
                self.invoke_calls = 0
                self._responses = [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "fetch_docs",
                                "args": {},
                                "id": "call_http",
                            }
                        ],
                    ),
                    AIMessage(content="the model must not receive the failure as text"),
                ]

            def invoke(self, messages, **kwargs):
                _ = messages, kwargs
                self.invoke_calls += 1
                return self._responses.pop(0)

            def bind_tools(self, tools, **kwargs):
                _ = tools, kwargs
                return self

        model = _SequencedChatModel()
        failure = {
            "ok": False,
            "failure_class": "provider",
            "retryable": True,
            "request_semantics": "mutation",
            "definitely_no_external_effect": False,
            "external_effect_status": "timeout_unknown",
            "http_status": 429,
        }
        registered_tool = MagicMock()
        registered_tool.name = "fetch_docs"
        registered_tool.metadata = {}
        registered_tool.invoke.side_effect = OperationRetrySignalError(
            failure, operation_deadline_epoch_seconds=123.0
        )
        state = {
            "project_name": "demo",
            "project_config": {},
        }
        with (
            patch(
                "core.application.orchestration.agents.common.build_context_block",
                return_value="",
            ),
            patch(
                "core.application.orchestration.agents.common.build_langchain_prompt",
                return_value=_FakePrompt(),
            ),
            patch(
                "core.application.orchestration.agents.common.get_chat_model",
                return_value=model,
            ),
            patch("core.application.orchestration.agents.common.emit_event"),
            pytest.raises(OperationRetrySignalError) as raised,
        ):
            invoke_agent(
                state=state,
                persona={"name": "Developer", "system_prompt": "You are helpful."},
                platform_config={},
                task_input="Fetch docs",
                agent_name="composer",
                tools=[registered_tool],
                context_sections=[],
            )

        assert model.invoke_calls == 1
        assert raised.value.retry_signal["http_status"] == 429
        assert raised.value.operation_deadline_epoch_seconds == 123.0
        registered_tool.invoke.assert_called_once()

    def test_agent_events_redact_projected_mcp_excerpts_only(self):
        from core.application.orchestration.agents.common import invoke_agent

        class _SequencedChatModel:
            def __init__(self):
                self._responses = [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "projected_mcp_tool_a1b2c3d4",
                                "args": {"token": "mcp-argument-secret"},
                                "id": "call_mcp",
                            },
                            {
                                "name": "ordinary_tool",
                                "args": {"value": "ordinary-argument"},
                                "id": "call_ordinary",
                            },
                        ],
                    ),
                    AIMessage(content="done"),
                ]

            def invoke(self, messages, **kwargs):
                return self._responses.pop(0)

            def bind_tools(self, tools, **kwargs):
                return self

        mcp_tool = MagicMock()
        mcp_tool.name = "projected_mcp_tool_a1b2c3d4"
        mcp_tool.metadata = {
            "flow_steward_projected_mcp_v1": True,
            "flow_steward_canonical_tool_id": "fs.gmail.label_message",
        }
        mcp_tool.invoke.return_value = "mcp-result-secret"
        ordinary_tool = MagicMock()
        ordinary_tool.name = "ordinary_tool"
        ordinary_tool.metadata = {}
        ordinary_tool.invoke.return_value = "ordinary-result"

        with (
            patch(
                "core.application.orchestration.agents.common.build_context_block",
                return_value="",
            ),
            patch(
                "core.application.orchestration.agents.common.build_langchain_prompt",
                return_value=_FakePrompt(),
            ),
            patch(
                "core.application.orchestration.agents.common.get_chat_model",
                return_value=_SequencedChatModel(),
            ),
            patch("core.application.orchestration.agents.common.emit_event") as emit_event,
        ):
            response = invoke_agent(
                state={"project_config": {}, "project_name": "demo"},
                persona={"name": "Developer", "system_prompt": "You are helpful."},
                platform_config={},
                task_input="Use both tools",
                agent_name="composer",
                tools=[mcp_tool, ordinary_tool],
                context_sections=[],
            )

        assert response.content == "done"
        emitted = [call.args for call in emit_event.call_args_list]
        mcp_details = [
            args[4]
            for args in emitted
            if len(args) > 4
            and args[2] in {"tool_call", "tool_result"}
            and args[4].get("tool_name") == "projected_mcp_tool_a1b2c3d4"
        ]
        ordinary_details = [
            args[4]
            for args in emitted
            if len(args) > 4
            and args[2] in {"tool_call", "tool_result"}
            and args[4].get("tool_name") == "ordinary_tool"
        ]
        assert len(mcp_details) == 2
        assert all(
            detail["canonical_tool_id"] == "fs.gmail.label_message" for detail in mcp_details
        )
        assert all("args_excerpt" not in detail for detail in mcp_details)
        assert all("result_excerpt" not in detail for detail in mcp_details)
        assert len(ordinary_details) == 2
        assert ordinary_details[0]["args_excerpt"] == "{'value': 'ordinary-argument'}"
        assert ordinary_details[1]["result_excerpt"] == "ordinary-result"
        assert "mcp-argument-secret" not in repr(emitted)
        assert "mcp-result-secret" not in repr(emitted)

    def test_invoke_agent_reuses_exact_resolved_model_for_structured_finalization(self):
        from core.application.orchestration.agents.common import invoke_agent
        from core.application.orchestration.models import build_phase_result_json_schema

        schema = build_phase_result_json_schema(
            [
                {
                    "name": "summary_doc",
                    "type": "text",
                    "description": "Ready doc",
                    "required": True,
                }
            ]
        )
        route_a = _FakeChatModel(
            draft_response=AIMessage(content="Draft plain-text response"),
            structured_payload={
                "schema_version": "phase_result_v2",
                "status": "pass",
                "summary": "route-a",
                "artifacts": {"summary_doc": "from-route-a"},
                "evidence": [],
                "errors": [],
                "files_to_touch": [],
                "next_action": "next_phase",
                "confidence": 0.9,
            },
            provider_policy="openai_compatible",
        )
        route_b = _FakeChatModel(
            draft_response=AIMessage(content="unused"),
            structured_payload={
                "schema_version": "phase_result_v2",
                "status": "pass",
                "summary": "route-b",
                "artifacts": {"summary_doc": "from-route-b"},
                "evidence": [],
                "errors": [],
                "files_to_touch": [],
                "next_action": "next_phase",
                "confidence": 0.9,
            },
            provider_policy="openai_compatible",
        )

        with (
            patch(
                "core.application.orchestration.agents.common.build_context_block",
                return_value="",
            ),
            patch(
                "core.application.orchestration.agents.common.build_langchain_prompt",
                return_value=_FakePrompt(),
            ),
            patch(
                "core.application.orchestration.agents.common.get_chat_model",
                side_effect=[route_a, route_b],
            ) as model_factory,
            patch("core.application.orchestration.agents.common.emit_event"),
        ):
            response = invoke_agent(
                state={"project_config": {}, "project_name": "demo"},
                persona={"name": "Developer", "system_prompt": "You are helpful."},
                platform_config={},
                task_input="Ship it",
                agent_name="composer",
                tools=[],
                context_sections=[],
                final_response_schema=schema,
            )

        assert model_factory.call_count == 1
        assert json.loads(response.content)["artifacts"]["summary_doc"] == "from-route-a"
        assert len(route_a.structured_calls) == 1
        assert route_b.structured_calls == []

    def test_invoke_agent_finalizes_openai_response_with_skill_schema(self):
        from core.application.orchestration.agents.common import invoke_agent
        from core.application.orchestration.models import build_phase_result_json_schema

        schema = build_phase_result_json_schema(
            [
                {
                    "name": "summary_doc",
                    "type": "text",
                    "description": "Ready doc",
                    "required": True,
                }
            ]
        )
        payload = {
            "schema_version": "phase_result_v2",
            "status": "pass",
            "summary": "done",
            "artifacts": {"summary_doc": "Ready"},
            "evidence": [],
            "errors": [],
            "files_to_touch": [],
            "next_action": "next_phase",
            "confidence": 0.9,
        }
        fake_llm = _FakeChatModel(
            draft_response=AIMessage(content="Draft plain-text response"),
            structured_payload=payload,
        )

        with (
            patch(
                "core.application.orchestration.agents.common.build_context_block", return_value=""
            ),
            patch(
                "core.application.orchestration.agents.common.build_langchain_prompt",
                return_value=_FakePrompt(),
            ),
            patch(
                "core.application.orchestration.agents.common.get_chat_model", return_value=fake_llm
            ),
            patch(
                "core.application.orchestration.agents.common._resolve_provider",
                side_effect=AssertionError(
                    "provider policy must come from the constructed model snapshot"
                ),
            ),
            patch("core.application.orchestration.agents.common.emit_event"),
        ):
            response = invoke_agent(
                state={"project_config": {}, "project_name": "demo"},
                persona={"name": "Developer", "system_prompt": "You are helpful."},
                platform_config={},
                task_input="Ship it",
                agent_name="composer",
                tools=[],
                context_sections=[],
                final_response_schema=schema,
            )

        body = json.loads(response.content)
        assert body["schema_version"] == "phase_result_v2"
        assert body["status"] == "pass"
        assert body["artifacts"]["summary_doc"] == "Ready"
        assert len(fake_llm.structured_calls) == 1
        structured_call = fake_llm.structured_calls[0]
        assert structured_call["schema"]["title"] == "PhaseResultV2"
        assert (
            structured_call["schema"]["properties"]["artifacts"]["properties"]["summary_doc"][
                "type"
            ]
            == "string"
        )
        assert structured_call["kwargs"]["method"] == "json_schema"
        assert structured_call["kwargs"]["strict"] is True
        assert structured_call["kwargs"]["include_raw"] is False

    @pytest.mark.parametrize(
        ("provider", "expected_kwargs"),
        [
            ("anthropic", {"method": "json_schema", "include_raw": False}),
            ("google", {"method": "json_schema", "include_raw": False}),
        ],
    )
    def test_invoke_agent_finalizes_supported_non_openai_providers(self, provider, expected_kwargs):
        from core.application.orchestration.agents.common import invoke_agent
        from core.application.orchestration.models import build_phase_result_json_schema

        schema = build_phase_result_json_schema(
            [
                {
                    "name": "summary_doc",
                    "type": "text",
                    "description": "Ready doc",
                    "required": True,
                }
            ]
        )
        payload = {
            "schema_version": "phase_result_v2",
            "status": "pass",
            "summary": "done",
            "artifacts": {"summary_doc": "Ready"},
            "evidence": [],
            "errors": [],
            "files_to_touch": [],
            "next_action": "next_phase",
            "confidence": 0.9,
        }
        fake_llm = _FakeChatModel(
            draft_response=AIMessage(content="Draft plain-text response"),
            structured_payload=payload,
            provider_policy=provider,
        )

        with (
            patch(
                "core.application.orchestration.agents.common.build_context_block", return_value=""
            ),
            patch(
                "core.application.orchestration.agents.common.build_langchain_prompt",
                return_value=_FakePrompt(),
            ),
            patch(
                "core.application.orchestration.agents.common.get_chat_model", return_value=fake_llm
            ),
            patch(
                "core.application.orchestration.agents.common._resolve_provider",
                return_value=provider,
            ),
            patch("core.application.orchestration.agents.common.emit_event"),
        ):
            response = invoke_agent(
                state={"project_config": {}, "project_name": "demo"},
                persona={"name": "Developer", "system_prompt": "You are helpful."},
                platform_config={},
                task_input="Ship it",
                agent_name="composer",
                tools=[],
                context_sections=[],
                final_response_schema=schema,
            )

        body = json.loads(response.content)
        assert body["schema_version"] == "phase_result_v2"
        assert body["artifacts"]["summary_doc"] == "Ready"
        assert len(fake_llm.structured_calls) == 1
        assert fake_llm.structured_calls[0]["kwargs"] == expected_kwargs

    def test_invoke_agent_strips_raw_tool_calls_from_structured_draft(self):
        from core.application.orchestration.agents.common import invoke_agent
        from core.application.orchestration.models import build_phase_result_json_schema

        class _CapturingStructuredRunnable:
            def __init__(self, payload):
                self.payload = payload
                self.messages = []

            def invoke(self, messages, **kwargs):
                self.messages = list(messages)
                return self.payload

        class _CapturingChatModel(_FakeChatModel):
            def __init__(self, *, draft_response: AIMessage, structured_payload: dict):
                super().__init__(
                    draft_response=draft_response,
                    structured_payload=structured_payload,
                )
                self.structured_runnable = _CapturingStructuredRunnable(structured_payload)

            def with_structured_output(self, schema=None, **kwargs):
                self.structured_calls.append({"schema": schema, "kwargs": kwargs})
                return self.structured_runnable

        schema = build_phase_result_json_schema(
            [
                {
                    "name": "code_output",
                    "type": "text",
                    "description": "Implementation notes",
                    "required": True,
                }
            ]
        )
        payload = {
            "schema_version": "phase_result_v2",
            "status": "pass",
            "summary": "done",
            "artifacts": {"code_output": "Implemented safely."},
            "evidence": [],
            "errors": [],
            "files_to_touch": [],
            "next_action": "next_phase",
            "confidence": 0.9,
        }
        fake_llm = _CapturingChatModel(
            draft_response=AIMessage(
                content="Draft plain-text response",
                additional_kwargs={
                    "tool_calls": [
                        {
                            "id": "call_unanswered",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "{bad"},
                        }
                    ]
                },
            ),
            structured_payload=payload,
        )

        with (
            patch(
                "core.application.orchestration.agents.common.build_context_block", return_value=""
            ),
            patch(
                "core.application.orchestration.agents.common.build_langchain_prompt",
                return_value=_FakePrompt(),
            ),
            patch(
                "core.application.orchestration.agents.common.get_chat_model", return_value=fake_llm
            ),
            patch(
                "core.application.orchestration.agents.common._resolve_provider",
                return_value="openai",
            ),
            patch("core.application.orchestration.agents.common.emit_event"),
        ):
            response = invoke_agent(
                state={"project_config": {}, "project_name": "demo"},
                persona={"name": "Developer", "system_prompt": "You are helpful."},
                platform_config={},
                task_input="Ship it",
                agent_name="composer",
                tools=[],
                context_sections=[],
                final_response_schema=schema,
            )

        body = json.loads(response.content)
        assert body["artifacts"]["code_output"] == "Implemented safely."
        draft_messages = [
            message
            for message in fake_llm.structured_runnable.messages
            if getattr(message, "content", None) == "Draft plain-text response"
        ]
        assert draft_messages
        assert all(
            "tool_calls" not in getattr(message, "additional_kwargs", {})
            for message in draft_messages
        )

    def test_invoke_agent_uses_raw_openai_tool_call_id_when_parsed_id_is_missing(self):
        from core.application.orchestration.agents.common import invoke_agent

        class _CapturingChatModel:
            def __init__(self):
                self.invocations = []
                self.responses = [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "read_file",
                                "args": {"path": "app/notes.py"},
                                "id": "",
                            }
                        ],
                        additional_kwargs={
                            "tool_calls": [
                                {
                                    "id": "call_raw_read_file",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"path": "app/notes.py"}',
                                    },
                                }
                            ]
                        },
                    ),
                    AIMessage(content="done"),
                ]

            def invoke(self, messages, **kwargs):
                self.invocations.append(list(messages))
                return self.responses.pop(0)

            def bind_tools(self, tools, **kwargs):
                return self

        fake_llm = _CapturingChatModel()
        read_tool = MagicMock()
        read_tool.name = "read_file"
        read_tool.invoke.return_value = "def note_title_matches(): ..."

        with (
            patch(
                "core.application.orchestration.agents.common.build_context_block", return_value=""
            ),
            patch(
                "core.application.orchestration.agents.common.build_langchain_prompt",
                return_value=_FakePrompt(),
            ),
            patch(
                "core.application.orchestration.agents.common.get_chat_model",
                return_value=fake_llm,
            ),
            patch(
                "core.application.orchestration.agents.common._resolve_provider",
                return_value="openai",
            ),
            patch("core.application.orchestration.agents.common.emit_event"),
        ):
            response = invoke_agent(
                state={"project_config": {}, "project_name": "demo"},
                persona={"name": "Developer", "system_prompt": "You are helpful."},
                platform_config={},
                task_input="Ship it",
                agent_name="composer",
                tools=[read_tool],
                context_sections=[],
            )

        assert response.content == "done"
        second_messages = fake_llm.invocations[1]
        tool_messages = [
            message for message in second_messages if message.__class__.__name__ == "ToolMessage"
        ]
        assert len(tool_messages) == 1
        assert tool_messages[0].tool_call_id == "call_raw_read_file"

    def test_invoke_agent_converts_tool_timeout_to_tool_error_message(self):
        from core.application.orchestration.agents.common import invoke_agent

        class _TimeoutAwareChatModel:
            def __init__(self):
                self.invocations = []
                self.responses = [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "code_impact_check",
                                "args": {"changed_files": "app/notes.py"},
                                "id": "call_impact",
                            }
                        ],
                    ),
                    AIMessage(content="continued after tool timeout"),
                ]

            def invoke(self, messages, **kwargs):
                self.invocations.append(list(messages))
                return self.responses.pop(0)

            def bind_tools(self, tools, **kwargs):
                return self

        fake_llm = _TimeoutAwareChatModel()
        impact_tool = MagicMock()
        impact_tool.name = "code_impact_check"
        impact_tool.invoke.side_effect = TimeoutError(
            "tool 'code_impact_check' exceeded 1s execution timeout"
        )

        with (
            patch(
                "core.application.orchestration.agents.common.build_context_block", return_value=""
            ),
            patch(
                "core.application.orchestration.agents.common.build_langchain_prompt",
                return_value=_FakePrompt(),
            ),
            patch(
                "core.application.orchestration.agents.common.get_chat_model",
                return_value=fake_llm,
            ),
            patch(
                "core.application.orchestration.agents.common._resolve_provider",
                return_value="openai",
            ),
            patch(
                "core.application.orchestration.agents.common._agent_tool_call_timeout_seconds",
                return_value=1,
            ),
            patch("core.application.orchestration.agents.common.emit_event"),
        ):
            response = invoke_agent(
                state={"project_config": {}, "project_name": "demo"},
                persona={"name": "Developer", "system_prompt": "You are helpful."},
                platform_config={},
                task_input="Ship it",
                agent_name="composer",
                tools=[impact_tool],
                context_sections=[],
            )

        assert response.content == "continued after tool timeout"
        second_messages = fake_llm.invocations[1]
        tool_messages = [
            message for message in second_messages if message.__class__.__name__ == "ToolMessage"
        ]
        assert len(tool_messages) == 1
        assert tool_messages[0].tool_call_id == "call_impact"
        assert "Tool error" in str(tool_messages[0].content)
        assert "code_impact_check" in str(tool_messages[0].content)

    def test_invoke_agent_skips_native_structured_output_for_unsupported_providers(self):
        from core.application.orchestration.agents.common import invoke_agent
        from core.application.orchestration.models import build_phase_result_json_schema

        schema = build_phase_result_json_schema(
            [
                {
                    "name": "summary_doc",
                    "type": "text",
                    "description": "Ready doc",
                    "required": True,
                }
            ]
        )
        fake_llm = _FakeChatModel(
            draft_response=AIMessage(content="Draft plain-text response"),
            structured_payload={"ignored": True},
            provider_policy="unsupported_provider",
        )

        with (
            patch(
                "core.application.orchestration.agents.common.build_context_block", return_value=""
            ),
            patch(
                "core.application.orchestration.agents.common.build_langchain_prompt",
                return_value=_FakePrompt(),
            ),
            patch(
                "core.application.orchestration.agents.common.get_chat_model", return_value=fake_llm
            ),
            patch(
                "core.application.orchestration.agents.common._resolve_provider",
                return_value="unsupported_provider",
            ),
            patch("core.application.orchestration.agents.common.emit_event"),
        ):
            response = invoke_agent(
                state={"project_config": {}, "project_name": "demo"},
                persona={"name": "Developer", "system_prompt": "You are helpful."},
                platform_config={},
                task_input="Ship it",
                agent_name="composer",
                tools=[],
                context_sections=[],
                final_response_schema=schema,
            )

        assert response.content == "Draft plain-text response"
        assert fake_llm.structured_calls == []

    def test_invoke_agent_fails_closed_if_structured_output_errors(self):
        from core.application.orchestration.agents.common import invoke_agent
        from core.application.orchestration.models import build_phase_result_json_schema

        class _FailingStructuredChatModel(_FakeChatModel):
            def with_structured_output(self, schema=None, **kwargs):
                raise RuntimeError("provider rejected schema")

        schema = build_phase_result_json_schema(
            [
                {
                    "name": "summary_doc",
                    "type": "text",
                    "description": "Ready doc",
                    "required": True,
                }
            ]
        )
        fake_llm = _FailingStructuredChatModel(
            draft_response=AIMessage(content="Draft plain-text response"),
            structured_payload={"ignored": True},
        )

        with (
            patch(
                "core.application.orchestration.agents.common.build_context_block", return_value=""
            ),
            patch(
                "core.application.orchestration.agents.common.build_langchain_prompt",
                return_value=_FakePrompt(),
            ),
            patch(
                "core.application.orchestration.agents.common.get_chat_model", return_value=fake_llm
            ),
            patch(
                "core.application.orchestration.agents.common._resolve_provider",
                return_value="anthropic",
            ),
            patch("core.application.orchestration.agents.common.emit_event"),
            pytest.raises(RuntimeError, match="structured_output_unavailable"),
        ):
            invoke_agent(
                state={"project_config": {}, "project_name": "demo"},
                persona={"name": "Developer", "system_prompt": "You are helpful."},
                platform_config={},
                task_input="Ship it",
                agent_name="composer",
                tools=[],
                context_sections=[],
                final_response_schema=schema,
            )

    def test_invoke_agent_final_budget_prompt_preserves_observed_evidence(self):
        from core.application.orchestration.agents.common import invoke_agent

        class _PrimaryChatModel:
            def invoke(self, messages, **kwargs):
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "http_fetch",
                            "args": {
                                "command": ".venv/bin/pytest tests/test_notes.py::TestDeleteNote -q"
                            },
                            "id": "call_tests",
                        }
                    ],
                )

            def bind_tools(self, tools, **kwargs):
                return self

        class _FallbackChatModel:
            def __init__(self):
                self.messages = []

            def invoke(self, messages, **kwargs):
                self.messages = list(messages)
                return AIMessage(content="final verifier report")

            def bind_tools(self, tools, **kwargs):
                return self

        primary_llm = _PrimaryChatModel()
        fallback_llm = _FallbackChatModel()
        shell_tool = MagicMock()
        shell_tool.name = "http_fetch"
        shell_tool.invoke.return_value = "exit_code: 0\nstdout:\n  7 passed in 0.51s\nsuccess: True"

        with (
            patch(
                "core.application.orchestration.agents.common.build_context_block", return_value=""
            ),
            patch(
                "core.application.orchestration.agents.common.build_langchain_prompt",
                return_value=_FakePrompt(),
            ),
            patch(
                "core.application.orchestration.agents.common.get_chat_model",
                side_effect=[primary_llm, fallback_llm],
            ),
            patch(
                "core.application.orchestration.agents.common._resolve_provider",
                return_value="openai",
            ),
            patch("core.application.orchestration.agents.common.emit_event"),
        ):
            response = invoke_agent(
                state={"project_config": {}, "project_name": "demo"},
                persona={"name": "Story Verifier", "system_prompt": "You are helpful."},
                platform_config={},
                task_input="Verify the implementation",
                agent_name="auditor",
                tools=[shell_tool],
                context_sections=[],
                max_tool_rounds=1,
            )

        assert response.content == "final verifier report"
        final_prompt = "\n".join(
            str(getattr(message, "content", "") or "") for message in fallback_llm.messages
        )
        assert "Tool-budget exhaustion is not itself a blocker" in final_prompt
        assert "check output you directly observed" in final_prompt
        assert "base your final verdict on that evidence" in final_prompt

    def test_invoke_agent_final_budget_accepts_responses_api_content_blocks(self):
        """The final fallback may return LangChain's structured content list."""
        from core.application.orchestration.agents.common import invoke_agent

        class _PrimaryChatModel:
            def invoke(self, messages, **kwargs):
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "http_fetch",
                            "args": {"command": ".venv/bin/pytest tests/test_notes.py -q"},
                            "id": "call_tests",
                        }
                    ],
                )

            def bind_tools(self, tools, **kwargs):
                return self

        class _FallbackChatModel:
            def invoke(self, messages, **kwargs):
                return AIMessage(
                    content=[
                        {
                            "type": "text",
                            "text": "final response from structured content",
                        }
                    ]
                )

            def bind_tools(self, tools, **kwargs):
                return self

        shell_tool = MagicMock()
        shell_tool.name = "http_fetch"
        shell_tool.invoke.return_value = "exit_code: 0\nstdout:\n  1 passed\nsuccess: True"

        with (
            patch(
                "core.application.orchestration.agents.common.build_context_block", return_value=""
            ),
            patch(
                "core.application.orchestration.agents.common.build_langchain_prompt",
                return_value=_FakePrompt(),
            ),
            patch(
                "core.application.orchestration.agents.common.get_chat_model",
                side_effect=[_PrimaryChatModel(), _FallbackChatModel()],
            ),
            patch(
                "core.application.orchestration.agents.common._resolve_provider",
                return_value="openai",
            ),
            patch("core.application.orchestration.agents.common.emit_event"),
        ):
            response = invoke_agent(
                state={"project_config": {}, "project_name": "demo"},
                persona={"name": "Developer", "system_prompt": "You are helpful."},
                platform_config={},
                task_input="Ship it",
                agent_name="composer",
                tools=[shell_tool],
                context_sections=[],
                max_tool_rounds=1,
            )

        assert response.content == [
            {"type": "text", "text": "final response from structured content"}
        ]

    def test_invoke_agent_formats_multiline_read_file_results_before_scanning(self):
        from core.application.orchestration.agents.common import invoke_agent

        class _CapturingChatModel:
            def __init__(self, responses):
                self._responses = list(responses)
                self.invocations = []

            def invoke(self, messages, **kwargs):
                self.invocations.append(list(messages))
                if not self._responses:
                    raise AssertionError("unexpected extra invoke")
                return self._responses.pop(0)

            def bind_tools(self, tools, **kwargs):
                return self

        llm = _CapturingChatModel(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "read_file",
                            "args": {"path": "app/routers/tasks.py"},
                            "id": "call_read_file",
                        }
                    ],
                ),
                AIMessage(content="verification complete"),
            ]
        )
        read_tool = MagicMock()
        read_tool.name = "read_file"
        read_tool.invoke.return_value = {
            "success": True,
            "path": "/project/app/routers/tasks.py",
            "content": "@router.post('/tasks')\ndef create_task():\n    pass\n",
            "truncated": False,
        }

        with (
            patch(
                "core.application.orchestration.agents.common.build_context_block", return_value=""
            ),
            patch(
                "core.application.orchestration.agents.common.build_langchain_prompt",
                return_value=_FakePrompt(),
            ),
            patch(
                "core.application.orchestration.agents.common.get_chat_model",
                return_value=llm,
            ),
            patch(
                "core.application.orchestration.agents.common._resolve_provider",
                return_value="openai",
            ),
            patch("core.application.orchestration.agents.common.emit_event"),
        ):
            response = invoke_agent(
                state={"project_config": {}, "project_name": "demo"},
                persona={"name": "Task Verifier", "system_prompt": "You are helpful."},
                platform_config={},
                task_input="Verify the implementation",
                agent_name="checker",
                tools=[read_tool],
                context_sections=[],
            )

        assert response.content == "verification complete"
        read_tool.invoke.assert_called_once()
        tool_messages = [
            message for message in llm.invocations[1] if message.__class__.__name__ == "ToolMessage"
        ]
        if tool_messages:
            assert "@router.post('/tasks')" in tool_messages[0].content
            assert "[PII:email" not in tool_messages[0].content


# ---------------------------------------------------------------------------
# _build_tool_config()
# ---------------------------------------------------------------------------


class TestBuildToolConfig:
    """Test tool configuration block generation."""

    @pytest.fixture
    def writer_agent(self):
        from core.application.orchestration.agents.prompt_agent import PromptAgent

        manifest = _make_manifest(
            agent_id="writer", tools=["workflow_ops"], skills=["draft_summary"]
        )
        return PromptAgent(manifest)

    def test_includes_project_name_and_task_id(self, writer_agent):
        config = writer_agent._build_tool_config(_make_state())
        assert config == (
            "## Tool Configuration\n- project_name: test-project\n- task_id: TASK-123"
        )

    def test_does_not_expose_runtime_roots_branch_or_stack(self, writer_agent):
        state = _make_state(
            active_worktree_path="/tmp/worktrees/item-1",
            branch_name="task/existing-branch",
            git_head_baseline="base123",
        )
        config = writer_agent._build_tool_config(state)
        for leaked in (
            "/target",
            "/tmp/worktrees/item-1",
            "container_root",
            "task/existing-branch",
            "base123",
            "flake8",
            "pytest",
        ):
            assert leaked not in config

    def test_empty_state_returns_empty(self):
        from core.application.orchestration.agents.prompt_agent import PromptAgent

        agent = PromptAgent(_make_manifest(agent_id="writer", tools=[]))
        config = agent._build_tool_config({"container_root": "", "project_name": ""})
        assert config == ""

    def test_tool_resolution_warning_keeps_plain_project_scope(self, writer_agent):
        state = _make_state(execution_policy={"tool_resolution_warning": "no tools resolved"})
        config = writer_agent._build_tool_config(state)
        assert "- project_name: test-project" in config
        assert "- task_id: TASK-123" in config


class TestResolveTools:
    def test_uses_agent_tools_only(self):
        from core.application.orchestration.agents.prompt_agent import PromptAgent

        manifest = _make_manifest(
            agent_id="composer",
            tools=["workflow_ops", "sheet_ops"],
            skills=["revise_draft"],
        )
        agent = PromptAgent(manifest)

        resolver = MagicMock()
        resolved_tool = MagicMock()
        resolved_tool.name = "workflow_list"
        resolver.resolve.return_value = [resolved_tool]

        fake_rm = types.SimpleNamespace(
            instance=lambda: types.SimpleNamespace(tool_resolver=resolver)
        )

        with patch.dict(
            sys.modules,
            {
                "core.application.registry.registry_manager": types.SimpleNamespace(
                    RegistryManager=fake_rm
                )
            },
        ):
            tools = agent._resolve_tools(_make_state(), skill="revise_draft")

        assert tools == [resolved_tool]
        resolver.resolve.assert_called_once_with(["workflow_ops", "sheet_ops"])

    def test_passes_runtime_scope_to_catalog_tool_resolution(self):
        from core.application.orchestration.agents.prompt_agent import PromptAgent

        manifest = _make_manifest(
            agent_id="composer",
            tools=["fs.catalog.read"],
            skills=["revise_draft"],
        )
        agent = PromptAgent(manifest)

        resolved_tool = MagicMock()
        resolved_tool.name = "fs.catalog.read"
        resolver = MagicMock()
        resolver.resolve_catalog.return_value = [resolved_tool]
        registry_tools = MagicMock()
        registry_tools.get_enabled_catalog_entry.return_value = {"tool_id": "fs.catalog.read"}

        fake_rm = types.SimpleNamespace(
            instance=lambda: types.SimpleNamespace(
                tools=registry_tools,
                tool_resolver=resolver,
            )
        )

        state = _make_state(
            account_id="acc_alpha",
            tenant_id="tenant_alpha",
            project_id="project_alpha",
            workspace_id="workspace_alpha",
            job_id=77,
            workflow_id="wf.reporting",
        )
        with patch.dict(
            sys.modules,
            {
                "core.application.registry.registry_manager": types.SimpleNamespace(
                    RegistryManager=fake_rm
                )
            },
        ):
            tools = agent._resolve_tools(state, skill="revise_draft")

        assert tools == [resolved_tool]
        resolver.resolve_catalog.assert_called_once()
        args, kwargs = resolver.resolve_catalog.call_args
        assert args == ([{"tool_id": "fs.catalog.read"}],)
        assert kwargs["runtime_context"] == {
            "source": "prompt_agent.resolve_tools",
            "account_id": "acc_alpha",
            "tenant_id": "tenant_alpha",
            "project_id": "project_alpha",
            "workspace_id": "workspace_alpha",
            "job_id": 77,
            "workflow_id": "wf.reporting",
        }

    def test_task_verifier_does_not_inject_implicit_fallback_tools(self):
        from core.application.orchestration.agents.prompt_agent import PromptAgent

        manifest = _make_manifest(
            agent_id="checker",
            tools=["doc_index", "workflow_ops"],
            skills=["check_output"],
        )
        agent = _mark_read_only_change_contract_agent(PromptAgent(manifest))

        def _named_tool(name: str):
            tool = MagicMock()
            tool.name = name
            return tool

        resolver = MagicMock()
        resolver.resolve.return_value = [
            _named_tool("doc_lookup"),
            _named_tool("read_file"),
            _named_tool("http_fetch"),
        ]

        fake_rm = types.SimpleNamespace(
            instance=lambda: types.SimpleNamespace(tool_resolver=resolver)
        )

        with patch.dict(
            sys.modules,
            {
                "core.application.registry.registry_manager": types.SimpleNamespace(
                    RegistryManager=fake_rm
                )
            },
        ):
            tools = agent._resolve_tools(_make_state(), skill="check_output")

        names = [t.name for t in tools]
        assert names == ["doc_lookup", "read_file", "http_fetch"]
        resolver.resolve.assert_called_once_with(["doc_index", "workflow_ops"])

    def test_review_skill_does_not_apply_skill_named_tool_filter(self):
        from core.application.orchestration.agents.prompt_agent import PromptAgent

        manifest = _make_manifest(
            agent_id="proofreader",
            tools=["drive_ops", "sheet_ops", "test_runner"],
            skills=["review_summary"],
        )
        agent = PromptAgent(manifest)

        def _named_tool(name: str):
            tool = MagicMock()
            tool.name = name
            return tool

        resolver = MagicMock()
        resolver.resolve.return_value = [
            _named_tool("read_file"),
            _named_tool("write_file"),
            _named_tool("git_status"),
            _named_tool("git_add"),
            _named_tool("run_tests"),
        ]

        fake_rm = types.SimpleNamespace(
            instance=lambda: types.SimpleNamespace(tool_resolver=resolver)
        )

        with patch.dict(
            sys.modules,
            {
                "core.application.registry.registry_manager": types.SimpleNamespace(
                    RegistryManager=fake_rm
                )
            },
        ):
            tools = agent._resolve_tools(_make_state(), skill="review_summary")

        names = [t.name for t in tools]
        assert names == [
            "read_file",
            "write_file",
            "git_status",
            "git_add",
            "run_tests",
        ]

    def test_create_pr_keeps_non_git_tools_without_special_case(self):
        from core.application.orchestration.agents.prompt_agent import PromptAgent

        manifest = _make_manifest(
            agent_id="dispatcher",
            tools=["delivery", "sheet_ops"],
            skills=["publish_handoff"],
        )
        agent = PromptAgent(manifest)

        def _named_tool(name: str):
            tool = MagicMock()
            tool.name = name
            return tool

        resolver = MagicMock()
        resolver.resolve.return_value = [
            _named_tool("status_sync"),
            _named_tool("git_status"),
            _named_tool("git_diff"),
        ]

        fake_rm = types.SimpleNamespace(
            instance=lambda: types.SimpleNamespace(tool_resolver=resolver)
        )

        with patch.dict(
            sys.modules,
            {
                "core.application.registry.registry_manager": types.SimpleNamespace(
                    RegistryManager=fake_rm
                )
            },
        ):
            tools = agent._resolve_tools(_make_state(), skill="publish_handoff")

        names = [t.name for t in tools]
        assert names == ["status_sync", "git_status", "git_diff"]

    def test_create_pr_does_not_expose_synthetic_fallback_helper(self):
        from core.application.orchestration.agents.prompt_agent import PromptAgent

        assert not hasattr(PromptAgent, "_build_create_pr_fallback_response")

    def test_permissive_project_tool_policy_keeps_mutating_tools(self):
        from core.application.orchestration.agents.prompt_agent import PromptAgent

        manifest = _make_manifest(
            agent_id="proofreader",
            tools=["drive_ops", "sheet_ops", "test_runner"],
            skills=["review_summary"],
        )
        agent = PromptAgent(manifest)

        def _named_tool(name: str):
            tool = MagicMock()
            tool.name = name
            return tool

        resolver = MagicMock()
        resolver.resolve.return_value = [
            _named_tool("read_file"),
            _named_tool("write_file"),
            _named_tool("git_add"),
            _named_tool("run_tests"),
        ]

        fake_rm = types.SimpleNamespace(
            instance=lambda: types.SimpleNamespace(tool_resolver=resolver)
        )

        state = _make_state(project_policy={"tool_policy_mode": "permissive"})

        with patch.dict(
            sys.modules,
            {
                "core.application.registry.registry_manager": types.SimpleNamespace(
                    RegistryManager=fake_rm
                )
            },
        ):
            tools = agent._resolve_tools(state, skill="review_summary")

        names = [t.name for t in tools]
        assert names == ["read_file", "write_file", "git_add", "run_tests"]

    def test_registry_only_project_tool_policy_keeps_registry_declared_edit_tools(self):
        from core.application.orchestration.agents.prompt_agent import PromptAgent

        def _named_tool(name: str):
            tool = MagicMock()
            tool.name = name
            return tool

        tools = PromptAgent._apply_skill_tool_policy(
            [
                _named_tool("read_file"),
                _named_tool("write_file"),
                _named_tool("git_status"),
                _named_tool("git_add"),
                _named_tool("run_tests"),
            ],
            skill="review_summary",
            state=_make_state(project_policy={"tool_policy_mode": "registry_only"}),
        )

        assert [tool.name for tool in tools] == [
            "read_file",
            "write_file",
            "git_status",
            "git_add",
            "run_tests",
        ]

    def test_restricted_project_tool_policy_allows_read_and_verification_tools_only(self):
        from core.application.orchestration.agents.prompt_agent import PromptAgent

        def _named_tool(name: str, metadata: dict | None = None):
            tool = MagicMock()
            tool.name = name
            tool.metadata = metadata or {}
            return tool

        tools = PromptAgent._apply_skill_tool_policy(
            [
                _named_tool("project_list"),
                _named_tool("job_cancel"),
                _named_tool("registry_quality_tool", {"category": "quality"}),
                _named_tool("registry_docs_tool", {"category": "docs"}),
                _named_tool("registry_verification_tool", {"category": "verification"}),
                _named_tool("registry_edit_tool", {"category": "editing"}),
                _named_tool("unknown_tool"),
            ],
            skill="draft_summary",
            state=_make_state(project_policy={"tool_policy_mode": "restricted"}),
        )

        assert [tool.name for tool in tools] == [
            "registry_quality_tool",
            "registry_docs_tool",
            "registry_verification_tool",
        ]

    def test_execution_policy_state_for_registry_only_records_resolved_tools(self):
        from core.application.orchestration.agents.prompt_agent import PromptAgent

        def _named_tool(name: str):
            tool = MagicMock()
            tool.name = name
            return tool

        policy = PromptAgent._build_execution_policy_state(
            _make_state(project_policy={"tool_policy_mode": "registry_only"}),
            tool_ids=["note_editor"],
            resolved_tools=[_named_tool("str_replace_based_edit_tool")],
        )

        assert policy["tool_policy_mode"] == "registry_only"
        assert policy["registry_tool_ids"] == ["note_editor"]
        assert policy["resolved_tool_names"] == ["str_replace_based_edit_tool"]
        assert "allow_file_mutation" not in policy

    def test_registry_only_test_runner_records_only_canonical_tool_metadata(self):
        from core.application.orchestration.agents.prompt_agent import PromptAgent

        def _named_tool(name: str):
            tool = MagicMock()
            tool.name = name
            return tool

        policy = PromptAgent._build_execution_policy_state(
            _make_state(project_policy={"tool_policy_mode": "registry_only"}),
            tool_ids=["test_runner"],
            resolved_tools=[_named_tool("run_tests")],
        )

        assert policy["tool_policy_mode"] == "registry_only"
        assert policy["registry_tool_ids"] == ["test_runner"]
        assert policy["resolved_tool_names"] == ["run_tests"]

    def test_execution_policy_state_for_restricted_disables_file_mutation(self):
        from core.application.orchestration.agents.prompt_agent import PromptAgent

        policy = PromptAgent._build_execution_policy_state(
            _make_state(project_policy={"tool_policy_mode": "restricted"}),
            tool_ids=["note_editor", "test_runner"],
            resolved_tools=[],
        )

        assert policy["resolved_tool_names"] == []

    def test_registry_only_policy_reports_empty_resolved_registry_tools(self):
        from core.application.orchestration.agents.prompt_agent import PromptAgent

        policy = PromptAgent._build_execution_policy_state(
            _make_state(project_policy={"tool_policy_mode": "registry_only"}),
            tool_ids=["doc_index", "workflow_ops"],
            resolved_tools=[],
        )

        assert policy["registry_tool_ids"] == ["doc_index", "workflow_ops"]
        assert policy["resolved_tool_names"] == []
        assert "resolved no runtime tools" in policy["tool_resolution_warning"]

    def test_registry_only_empty_resolution_strips_available_tools_claims(self):
        from core.application.orchestration.agents.prompt_agent import PromptAgent

        system_prompt = (
            "You are an scout.\n\n"
            "## Available Tools\n"
            "### Tool: Document Lookup\nUse doc_lookup for reference pages.\n\n"
            "## Guardrails\nStay scoped."
        )

        stripped = PromptAgent._strip_available_tools_section(system_prompt)

        assert "Available Tools" not in stripped
        assert "doc_lookup" not in stripped
        assert "## Guardrails" in stripped

    def test_registry_only_empty_resolution_suppresses_tool_config_claims(self):
        from core.application.orchestration.agents.prompt_agent import PromptAgent

        agent = PromptAgent(_make_manifest(tools=["doc_index", "workflow_ops"]))
        state = _make_state(
            project_policy={"tool_policy_mode": "registry_only"},
            project_name="Flow Steward App",
        )
        state["execution_policy"] = PromptAgent._build_execution_policy_state(
            state,
            tool_ids=["doc_index", "workflow_ops"],
            resolved_tools=[],
        )

        config = agent._build_tool_config(state, tool_ids=["doc_index", "workflow_ops"])

        assert "Tool Resolution Diagnostic" not in config
        assert "- project_name: Flow Steward App" in config


# ---------------------------------------------------------------------------
# _get_context_sections()
# ---------------------------------------------------------------------------


class TestGetContextSections:
    """Test context section selection per agent/skill."""

    def _agent_for(self, agent_id: str):
        from core.application.orchestration.agents.prompt_agent import PromptAgent

        return PromptAgent(_make_manifest(agent_id=agent_id))

    def test_developer_fresh_sections(self):
        agent = self._agent_for("composer")
        sections = agent._get_context_sections("compose_report", None)
        assert "architecture" in sections
        assert "schema_notes" in sections
        assert "graph_context" in sections

    def test_developer_rework_sections(self):
        """apply_revision skill uses its own context_sections from YAML."""
        agent = self._agent_for("composer")
        sections = agent._get_context_sections("apply_revision", "Fix the failing test")
        assert "lint_findings" in sections
        assert "check_results" in sections
        assert "attachment_context" not in sections

    def test_proofreader_sections(self):
        agent = self._agent_for("proofreader")
        sections = agent._get_context_sections("review_summary", None)
        assert "lint_findings" in sections
        assert "check_results" in sections

    def test_code_review_sections(self):
        agent = self._agent_for("critic")
        sections = agent._get_context_sections("critic", None)
        assert "architecture" in sections
        assert "lint_findings" in sections

    def test_scout_sections(self):
        agent = self._agent_for("scout")
        sections = agent._get_context_sections("summarize_brief", None)
        assert "attachment_context" not in sections
        assert "kb_context" in sections

    def test_default_sections_for_unknown_agent(self):
        agent = self._agent_for("custom_agent")
        sections = agent._get_context_sections("some_skill", None)
        assert sections == ["kb_context"]


# ---------------------------------------------------------------------------
# _extract_verdict()
# ---------------------------------------------------------------------------


class TestGenericArtifactParsing:
    @pytest.fixture
    def agent(self):
        from core.application.orchestration.agents.prompt_agent import PromptAgent

        return PromptAgent(_make_manifest())

    def test_extract_verdict_is_empty_without_generic_contract(self, agent):
        assert agent._extract_verdict("VERDICT: pass", "review_summary") == {}

    def test_parse_artifacts_uses_generic_output_key_for_plain_text(self, agent):
        result = agent._parse_artifacts("Plain text response without JSON", "compose_report")
        assert result["output"] == "Plain text response without JSON"

    def test_parse_artifacts_flattens_phase_result_artifacts(self, agent):
        text = (
            "```json\n"
            "{"
            '"schema_version":"phase_result_v2",'
            '"status":"pass",'
            '"summary":"done",'
            '"artifacts":{"checks_run":{"unit_tests":{"result":"pass"}}}'
            "}\n```"
        )
        result = agent._parse_artifacts(text, "review_summary")
        assert result["checks_run"] == {"unit_tests": {"result": "pass"}}
        assert "artifacts" not in result


class TestPhaseResultEnvelope:
    """Test strict PhaseResultV2 envelope construction."""

    @pytest.fixture
    def agent(self):
        from core.application.orchestration.agents.prompt_agent import PromptAgent

        return _mark_contract_driven_implementation_agent(
            PromptAgent(_make_manifest(agent_id="composer", skills=["compose_report"]))
        )

    def test_plain_text_without_explicit_envelope_fails_closed(self, agent):
        payload, errors = agent._build_phase_result_v2(
            response_text="Implemented the feature and added tests.",
            artifacts={"code_output": "Implemented the feature and added tests."},
            status="pass",
            confidence=0.8,
            contract_errors=[],
        )

        assert errors
        assert payload["schema_version"] == "phase_result_v2"
        assert payload["status"] == "fail"
        assert payload["next_action"] == "retry_phase"
        assert payload["artifacts"]["code_output"] == "Implemented the feature and added tests."

    def test_invalid_phase_result_candidate_fails_closed(self, agent):
        payload, errors = agent._build_phase_result_v2(
            response_text='```json\n{"schema_version":"phase_result_v2","status":"bad","summary":"","artifacts":[]}\n```',
            artifacts={"code_output": "fallback"},
            status="pass",
            confidence=0.8,
            contract_errors=[],
        )

        assert payload["schema_version"] == "phase_result_v2"
        assert payload["status"] == "fail"
        assert payload["next_action"] == "retry_phase"
        assert errors

    def test_truncated_phase_result_candidate_fails_closed_without_repair(self, agent):
        response_text = (
            '{"schema_version":"phase_result_v2","status":"pass","summary":"done","artifacts":'
            '{"epics_and_stories":{"epics":[{"id":"E-1","stories":[{"id":"S-1","title":"Story"}]}]}},'
            '"evidence":[],"errors":[],"files_to_touch":["app/main.py"],"next_action":"next_phase","confidence":0.91'
        )

        payload, errors = agent._build_phase_result_v2(
            response_text=response_text,
            artifacts={},
            status="pass",
            confidence=0.8,
            contract_errors=[],
        )

        assert errors == ["invalid phase_result_v2 payload"]
        assert payload["status"] == "fail"
        assert payload["next_action"] == "retry_phase"

    def test_phase_result_rejects_invalid_markdown_escape_without_repair(self, agent):
        payload = {
            "schema_version": "phase_result_v2",
            "status": "pass",
            "summary": "added restart endpoint",
            "artifacts": {
                "code_output": "Added restart endpoint.",
                "git_diff": 'diff --git a/app/routers/tasks.py b/app/routers/tasks.py\n+@[PII:email:e277fe92132b](\n+    "/restart"',
            },
            "evidence": [],
            "errors": [],
            "files_to_touch": ["app/routers/tasks.py"],
            "next_action": "next_phase",
            "confidence": 0.9,
        }
        response_text = json.dumps(payload).replace("+@[PII:", "+\\[PII:", 1)

        with pytest.raises(json.JSONDecodeError):
            json.loads(response_text)

        parsed_artifacts = agent._parse_artifacts(response_text, "compose_report")
        phase_result, errors = agent._build_phase_result_v2(
            response_text=response_text,
            artifacts=parsed_artifacts,
            status="pass",
            confidence=0.8,
            contract_errors=[],
        )

        assert errors == ["invalid phase_result_v2 payload"]
        assert phase_result["status"] == "fail"
        assert phase_result["next_action"] == "retry_phase"

    def test_phase_result_normalizes_verdict_artifacts_to_canonical_values(self, agent):
        payload, errors = agent._build_phase_result_v2(
            response_text=(
                "```json\n"
                "{"
                '"schema_version":"phase_result_v2",'
                '"status":"pass",'
                '"summary":"security scan complete",'
                '"artifacts":{"content_verdict":"PASS: No security issues identified.","style_verdict":"No findings"}'
                "}\n```"
            ),
            artifacts={},
            status="pass",
            confidence=0.9,
            contract_errors=[],
        )

        assert errors == []
        assert payload["status"] == "pass"
        assert payload["artifacts"]["content_verdict"] == "pass"
        assert payload["artifacts"]["style_verdict"] == "pass"

    def test_phase_result_rejects_prose_verdict_artifacts_with_pass_status(self, agent):
        payload, errors = agent._build_phase_result_v2(
            response_text=(
                "```json\n"
                "{"
                '"schema_version":"phase_result_v2",'
                '"status":"pass",'
                '"summary":"review complete",'
                '"artifacts":{'
                '"style_verdict":"The changes correctly ensure archived notes are excluded from active search results.",'
                '"content_verdict":"No blocking issues except the SQL injection is still reachable.",'
                '"review_notes":"Detailed code review text remains prose."'
                "}"
                "}\n```"
            ),
            artifacts={},
            status="pass",
            confidence=0.9,
            contract_errors=[],
        )

        assert errors == []
        assert payload["status"] == "fail"
        assert payload["next_action"] == "retry_phase"
        assert payload["errors"]
        assert payload["artifacts"]["style_verdict"] == "needs_review"
        assert payload["artifacts"]["content_verdict"] == "needs_review"
        assert payload["artifacts"]["review_notes"] == "Detailed code review text remains prose."


# ---------------------------------------------------------------------------
# Prompt builder integration
# ---------------------------------------------------------------------------


class TestPromptBuilderIntegration:
    """Test PromptBuilder nested state resolution."""

    def test_resolve_nested_state_path(self):
        from core.application.orchestration.agents.prompt_builder import PromptBuilder

        builder = PromptBuilder()

        state = {
            "project_config": {
                "conventions": {
                    "tone": "concise",
                    "language": "en",
                },
                "stack": {"linter": "flake8"},
            },
        }
        result = builder._resolve_path("project_config.conventions.tone", state)
        assert result == "concise"
        with pytest.raises(ValueError, match="prompt-safe allowlist"):
            builder._resolve_path("project_config.stack.linter", state)

    def test_resolve_agent_message_path(self):
        from core.application.orchestration.agents.prompt_builder import PromptBuilder

        builder = PromptBuilder()

        state = {
            "agent_messages": [
                {
                    "schema_version": "agent_message_v1",
                    "from_agent": "scout",
                    "to_agent": "orchestrator",
                    "step_id": "analysis",
                    "skill": "summarize_brief",
                    "status": "pass",
                    "correlation_id": "corr-1",
                    "artifacts": {"summary_doc": "Requirements content here"},
                    "summary": "done",
                    "errors": [],
                    "timestamps": {
                        "started_at": None,
                        "completed_at": None,
                        "published_at": "2026-03-10T00:00:00Z",
                    },
                }
            ],
        }
        result = builder._resolve_path("message.agent.scout.artifacts.summary_doc", state)
        assert result == "Requirements content here"

    def test_resolve_step_path_from_step_id_mapping(self):
        from core.application.orchestration.agents.prompt_builder import PromptBuilder

        builder = PromptBuilder()

        state = {
            "agent_messages": [
                {
                    "schema_version": "agent_message_v1",
                    "from_agent": "scout",
                    "to_agent": "orchestrator",
                    "step_id": "analysis",
                    "skill": "summarize_brief",
                    "status": "pass",
                    "correlation_id": "corr-2",
                    "artifacts": {"summary_doc": "Requirements content here"},
                    "summary": "done",
                    "errors": [],
                    "timestamps": {
                        "started_at": None,
                        "completed_at": None,
                        "published_at": "2026-03-10T00:00:00Z",
                    },
                }
            ],
            "workflow_id": "weekly_report",
        }
        result = builder._resolve_path("message.step.analysis.artifacts.summary_doc", state)
        assert result == "Requirements content here"

    def test_resolve_flat_key(self):
        from core.application.orchestration.agents.prompt_builder import PromptBuilder

        builder = PromptBuilder()

        state = {"task_summary": "Fix the bug"}
        result = builder._resolve_path("task_summary", state)
        assert result == "Fix the bug"

    def test_resolve_flat_key_prefers_step_artifacts(self):
        from core.application.orchestration.agents.prompt_builder import PromptBuilder

        builder = PromptBuilder()

        state = {
            "task_summary": "Global task summary",
            "agent_messages": [
                {
                    "schema_version": "agent_message_v1",
                    "from_agent": "scout",
                    "to_agent": "orchestrator",
                    "step_id": "analysis",
                    "skill": "summarize_brief",
                    "status": "pass",
                    "correlation_id": "corr-3",
                    "artifacts": {"task_summary": "Phase summary"},
                    "summary": "done",
                    "errors": [],
                    "timestamps": {
                        "started_at": None,
                        "completed_at": None,
                        "published_at": "2026-03-10T00:00:00Z",
                    },
                }
            ],
        }
        result = builder._resolve_path("task_summary", state)
        assert result == "Phase summary"

    def test_build_includes_skill_tool_prompts(self):
        from core.application.orchestration.agents.prompt_builder import PromptBuilder

        builder = PromptBuilder()
        agent = _make_manifest(agent_id="composer", tools=["sheet_ops"], skills=["revise_draft"])
        tool_registry = MagicMock()
        doc_index_tool = types.SimpleNamespace(
            display_name="Document Lookup",
            prompt="Use doc_lookup before revising the draft.",
        )
        tool_registry.get_enabled_tool.side_effect = lambda tool_id: (
            doc_index_tool if tool_id == "doc_index" else None
        )

        system_prompt, user_prompt, _ = builder.build(
            agent=agent,
            skill="revise_draft",
            step=None,
            tool_registry=tool_registry,
            state=_make_state(),
            tool_ids=["sheet_ops", "doc_index"],
        )

        assert "Document Lookup" in system_prompt
        assert "Use doc_lookup before revising the draft." in system_prompt
        assert isinstance(user_prompt, str)

    def test_build_includes_default_task_context_when_context_template_empty(self):
        from core.application.orchestration.agents.prompt_builder import PromptBuilder

        builder = PromptBuilder()
        agent = _make_manifest(agent_id="scout", skills=["summarize_brief"])
        state = _make_state(
            project_name="workflow-isolated-smoke",
            mode="brownfield",
            task_summary="Add a build_version field to /health.",
            initial_requirements=(
                "Add build_version to the health response and cover string/null edge cases "
                "with focused tests."
            ),
        )

        _system_prompt, user_prompt, _ = builder.build(
            agent=agent,
            skill="summarize_brief",
            step=None,
            tool_registry=None,
            state=state,
        )

        assert "## Task Context" in user_prompt
        assert "Task Summary: Add a build_version field to /health." in user_prompt
        assert "Add build_version to the health response" in user_prompt
        assert "Project: workflow-isolated-smoke" in user_prompt
        assert "Mode:" not in user_prompt

    def test_build_default_task_context_does_not_duplicate_identical_initial_requirements(self):
        from core.application.orchestration.agents.prompt_builder import PromptBuilder

        builder = PromptBuilder()
        agent = _make_manifest(agent_id="scout", skills=["summarize_brief"])
        state = _make_state(
            task_summary="Fix login validation edge cases.",
            initial_requirements="Fix login validation edge cases.",
        )

        _system_prompt, user_prompt, _ = builder.build(
            agent=agent,
            skill="summarize_brief",
            step=None,
            tool_registry=None,
            state=state,
        )

        assert user_prompt.count("Fix login validation edge cases.") == 1
        assert "Initial Requirements:" not in user_prompt

    def test_build_renders_analysis_requirements_for_quick_fix_development(self):
        from core.application.orchestration.agents.prompt_builder import PromptBuilder

        builder = PromptBuilder()
        agent = _make_manifest(agent_id="composer", skills=["compose_report"])
        agent.prompts.context_template = "## Requirements\n{summary_doc}"
        state = _make_state(
            workflow_id="weekly_digest",
            agent_messages=[
                {
                    "schema_version": "agent_message_v1",
                    "from_agent": "scout",
                    "to_agent": "orchestrator",
                    "step_id": "analysis",
                    "skill": "summarize_brief",
                    "status": "pass",
                    "correlation_id": "corr-analysis",
                    "artifacts": {
                        "summary_doc": "Confirm affected DAG schedule logic before editing."
                    },
                    "summary": "done",
                    "errors": [],
                    "timestamps": {
                        "started_at": None,
                        "completed_at": None,
                        "published_at": "2026-03-10T00:00:00Z",
                    },
                }
            ],
        )
        step = {"context_inject": {"summary_doc": "message.step.analysis.artifacts.summary_doc"}}

        _system_prompt, user_prompt, _ = builder.build(
            agent=agent,
            skill="compose_report",
            step=step,
            tool_registry=None,
            state=state,
        )

        assert "Confirm affected DAG schedule logic before editing." in user_prompt

    def test_prompt_agent_runtime_preserves_task_envelope_from_step_context(self):
        from core.application.orchestration.agents.base import AgentInput
        from core.application.orchestration.graph.nodes_step_context import (
            extract_step_context,
            extract_task_context,
        )
        from tests._step_run_state import step_run_state

        verification_command = (
            "python -m pytest tests/unit/test_workflow_ai_builder_service.py "
            '-k "create_recommend_new_agent_returns_capability_gap_question" -q'
        )
        target_test_name = (
            "test_ai_builder_create_recommend_new_agent_returns_capability_gap_question"
        )
        manifest = _make_manifest(agent_id="composer", skills=["compose_report"])
        manifest.prompts.context_template = (
            "## Task Context\nTask Summary: {task_summary}\n## Requirements\n{summary_doc}"
        )
        runtime_state = _make_state(
            **step_run_state(
                {
                    "step_id": "development",
                    "display_name": "Development",
                    "agent": "composer",
                    "skill": "compose_report",
                    "context_inject": {
                        "summary_doc": "initial_requirements",
                    },
                }
            ),
            task_summary="Preserve workflow runtime task envelope.",
            initial_requirements=("Analysis requires confirming the generic runtime prompt path."),
            workflow_id="weekly_digest",
            active_worktree_path="/target/.worktrees/runtime-envelope",
            task_envelope={
                "title": "Runtime prompt envelope propagation",
                "requirements": [
                    "Every real agent prompt must include the structured task envelope.",
                    f"Target regression: {target_test_name}",
                ],
                "acceptance_criteria": [
                    "PromptAgent runtime prompt includes exact verification commands."
                ],
                "verification_commands": [verification_command],
                "metadata": {
                    "api_key": "sk-test-secret-must-not-render",
                    "operator_note": "bounded metadata stays sanitized by PromptBuilder",
                },
            },
        )
        agent = _authorized_prompt_agent(manifest)
        captured = {"prompt": ""}

        def _capture_invoke_agent(**kwargs):
            captured["prompt"] = kwargs["task_input"]
            return types.SimpleNamespace(content='{"status":"pass","summary":"done"}')

        state_ref_without_envelope = {
            key: value for key, value in runtime_state.items() if key != "task_envelope"
        }

        with (
            patch("core.application.orchestration.agents.prompt_agent.emit_event"),
            patch(
                "core.application.orchestration.agents.prompt_agent._resolve_provider",
                return_value="openai",
            ),
            patch(
                "core.application.orchestration.agents.prompt_agent.invoke_agent",
                side_effect=_capture_invoke_agent,
            ),
            patch.object(agent, "_resolve_tools", return_value=[]),
            patch.object(agent, "_log_prompt_request"),
            patch.object(agent, "_log_prompt_response"),
            patch.object(agent, "_parse_artifacts", return_value={}),
            patch.object(agent, "_extract_verdict", return_value={}),
            patch.object(agent, "validate_agent_output", return_value=[]),
            patch.object(agent, "_resolve_final_response_schema", return_value=None),
            patch.object(
                agent,
                "_build_phase_result_v2",
                return_value=(
                    {"status": "pass", "summary": "Runtime prompt captured."},
                    [],
                ),
            ),
        ):
            output = agent.execute(
                AgentInput(
                    task_context=extract_task_context(runtime_state),
                    step_context=extract_step_context(runtime_state),
                    agent_config={},
                    skill="compose_report",
                    state_ref=state_ref_without_envelope,
                )
            )

        user_prompt = captured["prompt"]
        assert output.status == "pass"
        assert "## Task Context" in user_prompt
        assert "Analysis requires confirming the generic runtime prompt path." in user_prompt
        assert "## Structured Task Envelope" in user_prompt
        assert "## Required Verification Commands" in user_prompt
        assert verification_command in user_prompt
        assert "Every real agent prompt must include the structured task envelope." in user_prompt
        assert target_test_name in user_prompt
        assert "sk-test-secret-must-not-render" not in user_prompt

    def test_build_includes_mode_block_for_quick_fix_bugfix(self):
        from core.application.orchestration.agents.prompt_builder import PromptBuilder

        builder = PromptBuilder()
        agent = _make_manifest(agent_id="composer", skills=["compose_report"])
        state = _make_state(
            workflow_id="weekly_digest",
            task_type="bugfix",
            effort_hint="small",
            step_id="development",
        )

        _system_prompt, user_prompt, _ = builder.build(
            agent=agent,
            skill="compose_report",
            step={"context_inject": {}},
            tool_registry=None,
            state=state,
        )

        assert "## Execution Mode" in user_prompt
        assert "workflow_id: `weekly_digest`" in user_prompt
        assert "effort_hint: `small`" in user_prompt
        assert "### Mode Guidance" not in user_prompt

    def test_story_cycle_mode_uses_authoritative_workflow_id(self):
        from core.application.orchestration.agents.prompt_builder import PromptBuilder

        builder = PromptBuilder()
        agent = _make_manifest(agent_id="composer", skills=["compose_report"])
        state = _make_state(
            workflow_id="monthly_rollup",
            parent_workflow_id="maintenance",
            task_type="maintenance",
            effort_hint="standard",
            step_id="development",
        )

        _system_prompt, user_prompt, _ = builder.build(
            agent=agent,
            skill="compose_report",
            step={"context_inject": {}},
            tool_registry=None,
            state=state,
        )

        assert "workflow_id: `monthly_rollup`" in user_prompt
        assert "workflow_id: `maintenance`" not in user_prompt

    def test_implementation_mode_block_is_generic(self):
        from core.application.orchestration.agents.prompt_builder import PromptBuilder

        builder = PromptBuilder()
        agent = _make_manifest(agent_id="composer", skills=["compose_report"])
        state = _make_state(
            workflow_id="weekly_report",
            task_type="feature",
            effort_hint="standard",
            step_id="development",
        )

        _system_prompt, user_prompt, _ = builder.build(
            agent=agent,
            skill="compose_report",
            step={"context_inject": {}},
            tool_registry=None,
            state=state,
        )

        assert "workflow_id: `weekly_report`" in user_prompt
        assert (
            "Any artifact field ending in `_verdict` must be exactly one of: "
            "pass, fail, blocked, escalate"
        ) in user_prompt
        assert "### Mode Guidance" not in user_prompt

    def test_maintenance_planning_mode_block_is_generic(self):
        from core.application.orchestration.agents.prompt_builder import PromptBuilder

        builder = PromptBuilder()
        agent = _make_manifest(agent_id="coordinator", skills=["split_items"])
        state = _make_state(
            workflow_id="maintenance",
            task_type="maintenance",
            effort_hint="standard",
            step_id="planning",
        )

        _system_prompt, user_prompt, _ = builder.build(
            agent=agent,
            skill="split_items",
            step={"context_inject": {}},
            tool_registry=None,
            state=state,
        )

        assert "workflow_id: `maintenance`" in user_prompt
        assert "### Mode Guidance" not in user_prompt

    def test_planning_mode_block_relies_on_authored_prompts_only(self):
        from core.application.orchestration.agents.prompt_builder import PromptBuilder

        builder = PromptBuilder()
        agent = _make_manifest(agent_id="coordinator", skills=["split_items"])
        state = _make_state(
            workflow_id="weekly_report",
            task_type="feature",
            effort_hint="standard",
            step_id="planning",
        )

        _system_prompt, user_prompt, _ = builder.build(
            agent=agent,
            skill="split_items",
            step={"context_inject": {}},
            tool_registry=None,
            state=state,
        )

        assert "workflow_id: `weekly_report`" in user_prompt
        assert "### Mode Guidance" not in user_prompt

    def test_mode_block_falls_back_to_the_step_id_when_state_has_none(self):
        from core.application.orchestration.agents.prompt_builder import PromptBuilder

        builder = PromptBuilder()
        agent = _make_manifest(agent_id="composer", skills=["compose_report"])
        state = _make_state(
            workflow_id="weekly_digest",
            task_type="bugfix",
            effort_hint="small",
        )

        _system_prompt, user_prompt, _ = builder.build(
            agent=agent,
            skill="compose_report",
            step={"step_id": "development", "context_inject": {}},
            tool_registry=None,
            state=state,
        )

        assert "step: `development`" in user_prompt
        assert "phase:" not in user_prompt
        assert "### Mode Guidance" not in user_prompt

    def test_prompt_includes_structured_task_envelope_constraints(self):
        from core.application.orchestration.agents.prompt_builder import PromptBuilder

        builder = PromptBuilder()
        agent = _make_manifest(agent_id="composer", skills=["compose_report"])
        _system_prompt, user_prompt, _ = builder.build(
            agent=agent,
            skill="compose_report",
            step=None,
            tool_registry=None,
            state=_make_state(
                task_summary="Fix workflow runtime feedback",
                task_envelope={
                    "title": "Repair workflow runtime",
                    "objective": "Make workflow runtime carry the full task contract.",
                    "description": "Reject feedback must route to the actionable phase.",
                    "requirements": ["R1: Preserve task envelope fields."],
                    "acceptance_criteria": [
                        "Rejecting the quality gate reruns implementation with feedback."
                    ],
                    "verification_commands": ["python -m pytest tests/unit/test_supervisor.py -q"],
                    "constraints": ["Do not hardcode AI Builder workflow names."],
                    "general_constraints": ["Keep the workflow runtime generic."],
                    "general_purpose_constraints": ["Do not hardcode project-specific IDs."],
                    "non_goals": ["Do not change AI Builder business logic."],
                    "inputs": {"job_id": "1547"},
                },
            ),
        )

        assert "## Structured Task Envelope" in user_prompt
        assert "Repair workflow runtime" in user_prompt
        assert "Rejecting the quality gate reruns implementation" in user_prompt
        assert "python -m pytest tests/unit/test_supervisor.py -q" in user_prompt
        assert "Do not hardcode AI Builder workflow names." in user_prompt
        assert "Make workflow runtime carry the full task contract." in user_prompt
        assert "R1: Preserve task envelope fields." in user_prompt
        assert "Keep the workflow runtime generic." in user_prompt
        assert "Do not hardcode project-specific IDs." in user_prompt
        assert "Do not change AI Builder business logic." in user_prompt

    def test_task_envelope_long_description_does_not_hide_verification_commands(self):
        from core.application.orchestration.agents.prompt_builder import PromptBuilder

        builder = PromptBuilder()
        agent = _make_manifest(agent_id="proofreader", skills=["review_summary"])
        _system_prompt, user_prompt, _ = builder.build(
            agent=agent,
            skill="review_summary",
            step=None,
            tool_registry=None,
            state=_make_state(
                task_envelope={
                    "title": "Verify runtime repair",
                    "description": "very long context " * 800,
                    "verification_commands": ["python -m pytest exact/slice.py -q"],
                    "acceptance_criteria": ["Exact verification commands remain visible."],
                },
            ),
        )

        assert "python -m pytest exact/slice.py -q" in user_prompt
        assert "Exact verification commands remain visible." in user_prompt

    def test_task_envelope_large_requirements_do_not_hide_verification_commands(self):
        from core.application.orchestration.agents.prompt_builder import PromptBuilder

        builder = PromptBuilder()
        agent = _make_manifest(agent_id="proofreader", skills=["review_summary"])

        large_list_state = _make_state(
            task_envelope={
                "title": "Verify large list brief",
                "requirements": [f"R{i}: " + ("detail " * 100) for i in range(80)],
                "verification_commands": ["python -m pytest exact/list_slice.py -q"],
            },
        )
        large_dict_state = _make_state(
            task_envelope={
                "title": "Verify large dict brief",
                "acceptance_criteria": {f"AC{i}": "criterion detail " * 100 for i in range(80)},
                "verification_commands": ["python -m pytest exact/dict_slice.py -q"],
            },
        )

        _system_prompt, list_prompt, _ = builder.build(
            agent=agent,
            skill="review_summary",
            step=None,
            tool_registry=None,
            state=large_list_state,
        )
        _system_prompt, dict_prompt, _ = builder.build(
            agent=agent,
            skill="review_summary",
            step=None,
            tool_registry=None,
            state=large_dict_state,
        )

        assert "python -m pytest exact/list_slice.py -q" in list_prompt
        assert "## Required Verification Commands" in list_prompt
        assert "python -m pytest exact/dict_slice.py -q" in dict_prompt
        assert "## Required Verification Commands" in dict_prompt

    def test_rework_prompt_includes_operator_context_and_task_scope(self):
        from core.application.orchestration.agents.prompt_builder import PromptBuilder

        builder = PromptBuilder()
        agent = _make_manifest(agent_id="reviser", skills=["apply_revision"])
        _system_prompt, user_prompt, _ = builder.build(
            agent=agent,
            skill="apply_revision",
            step=None,
            tool_registry=None,
            state=_make_state(
                task_envelope={
                    "title": "Repair task output",
                    "non_goals": ["Do not repair review sandbox infrastructure."],
                    "verification_commands": ["python -m pytest targeted.py -q"],
                },
                feedback="Please fix the validation regression.",
                review_feedback="Please fix the validation regression.",
                reviewer_comments="Operator rejected with actionable task feedback.",
                risk_notes="Manual review evidence: sandbox failed, targeted checks identified defect.",
            ),
            feedback="Please fix the validation regression.",
        )

        assert "## Structured Task Envelope" in user_prompt
        assert "Do not repair review sandbox infrastructure." in user_prompt
        assert "python -m pytest targeted.py -q" in user_prompt
        assert "## Approval And Rework Context" in user_prompt
        assert "Operator rejected with actionable task feedback." in user_prompt
        assert "Manual review evidence" in user_prompt
        assert "Please fix the validation regression." in user_prompt

    def test_optional_context_inject_source_does_not_raise(self):
        from core.application.orchestration.agents.prompt_builder import PromptBuilder

        builder = PromptBuilder()
        agent = _make_manifest(agent_id="checker", skills=["check_output"])
        state = _make_state(
            workflow_id="maintenance",
            step_id="task_verification",
        )

        system_prompt, user_prompt, _ = builder.build(
            agent=agent,
            skill="check_output",
            step={
                "context_inject": {
                    "outline_doc": {
                        "source": "message.step.design.artifacts.outline_doc",
                        "required": False,
                    }
                }
            },
            tool_registry=None,
            state=state,
        )

        assert "## Execution Mode" in user_prompt
        assert "fall back to read-only file/shell inspection" not in user_prompt
        assert "Verification Tool Exception" not in system_prompt

    def test_story_verifier_mode_block_is_generic(self):
        from core.application.orchestration.agents.prompt_builder import PromptBuilder

        builder = PromptBuilder()
        agent = _make_manifest(agent_id="auditor", skills=["check_item"])
        state = _make_state(
            workflow_id="weekly_report",
            step_id="story_verification",
        )

        _system_prompt, user_prompt, _ = builder.build(
            agent=agent,
            skill="check_item",
            step={"context_inject": {}},
            tool_registry=None,
            state=state,
        )

        assert "workflow_id: `weekly_report`" in user_prompt
        assert "### Mode Guidance" not in user_prompt

    def test_review_mode_block_is_generic(self):
        from core.application.orchestration.agents.prompt_builder import PromptBuilder

        builder = PromptBuilder()
        agent = _make_manifest(agent_id="proofreader", skills=["review_summary"])
        state = _make_state(
            workflow_id="weekly_report",
            step_id="quality",
        )

        _system_prompt, user_prompt, _ = builder.build(
            agent=agent,
            skill="review_summary",
            step={"context_inject": {}},
            tool_registry=None,
            state=state,
        )

        assert "workflow_id: `weekly_report`" in user_prompt
        assert "### Mode Guidance" not in user_prompt

    def test_code_review_mode_block_is_generic(self):
        from core.application.orchestration.agents.prompt_builder import PromptBuilder

        builder = PromptBuilder()
        agent = _make_manifest(agent_id="critic", skills=["critic"])
        state = _make_state(
            workflow_id="weekly_report",
            step_id="review",
        )

        _system_prompt, user_prompt, _ = builder.build(
            agent=agent,
            skill="critic",
            step={"context_inject": {}},
            tool_registry=None,
            state=state,
        )

        assert "workflow_id: `weekly_report`" in user_prompt
        assert "### Mode Guidance" not in user_prompt

    def test_unresolved_legacy_placeholders_render_as_empty_strings(self):
        from core.application.orchestration.agents.prompt_builder import PromptBuilder

        builder = PromptBuilder()
        agent = _make_manifest(agent_id="coordinator", skills=["split_items"])
        agent.prompts.context_template = (
            "## Inputs\n"
            "Requirements: {summary_doc}\n"
            "Architecture: {outline_doc}\n"
            "Existing stories: {existing_stories}\n"
            "Feedback: {feedback}\n"
        )
        state = _make_state(
            workflow_id="maintenance",
            step_id="planning",
            agent_messages=[
                {
                    "schema_version": "agent_message_v1",
                    "from_agent": "scout",
                    "to_agent": "orchestrator",
                    "step_id": "analysis",
                    "skill": "summarize_brief",
                    "status": "pass",
                    "correlation_id": "corr-analysis",
                    "artifacts": {"summary_doc": "Refactor retry helper safely."},
                    "summary": "done",
                    "errors": [],
                    "timestamps": {
                        "started_at": None,
                        "completed_at": None,
                        "published_at": "2026-03-10T00:00:00Z",
                    },
                }
            ],
        )

        _system_prompt, user_prompt, _ = builder.build(
            agent=agent,
            skill="split_items",
            step={"context_inject": {"summary_doc": "message.step.analysis.artifacts.summary_doc"}},
            tool_registry=None,
            state=state,
        )

        assert "Requirements: Refactor retry helper safely." in user_prompt
        assert "{outline_doc}" not in user_prompt
        assert "{existing_stories}" not in user_prompt
        assert "{feedback}" not in user_prompt

    def test_resolve_missing_key_returns_none(self):
        from core.application.orchestration.agents.prompt_builder import PromptBuilder

        builder = PromptBuilder()

        state = {}
        result = builder._resolve_path("nonexistent.path.key", state)
        assert result is None

    def test_contract_output_requirements_include_fix_bug_blocks(self):
        from core.application.orchestration.agents.prompt_builder import PromptBuilder

        fake_db = MagicMock()
        fake_db.get_skill_contract.return_value = {
            "required_fields": [
                "root_cause",
                "fix_summary",
                "test_added",
                "code_output",
                "git_diff",
                "files_changed",
                "workspace_state",
            ],
            "all_fields": [
                {"name": "root_cause", "description": "Explanation of why the bug occurred"},
                {"name": "fix_summary", "description": "What was changed and why"},
                {"name": "test_added", "description": "The regression test that captures the bug"},
                {"name": "code_output", "description": "Code/implementation summary from bugfix"},
                {"name": "git_diff", "description": "Git diff"},
                {"name": "files_changed", "description": "List of files modified"},
                {"name": "workspace_state", "description": "clean or dirty"},
            ],
        }
        fake_rm = MagicMock()
        fake_rm.db_store = fake_db

        with patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=fake_rm,
        ):
            text = PromptBuilder()._build_phase_result_v2_requirements("revise_draft")

        assert '"schema_version": "phase_result_v2"' in text
        assert '"root_cause": "..."' in text
        assert '"fix_summary": "..."' in text
        assert '"test_added": "..."' in text
        assert '"files_changed": "..."' in text
        assert '"workspace_state": "..."' in text

    def test_contract_output_requirements_include_analysis_blocks(self):
        from core.application.orchestration.agents.prompt_builder import PromptBuilder

        fake_db = MagicMock()
        fake_db.get_skill_contract.return_value = {
            "required_fields": [
                "summary_doc",
                "complexity_estimate",
                "research_brief",
            ],
            "all_fields": [
                {"name": "summary_doc", "description": "Detailed requirements document"},
                {"name": "complexity_estimate", "description": "Estimated complexity level"},
                {"name": "research_brief", "description": "Concise brief for downstream agents"},
            ],
        }
        fake_rm = MagicMock()
        fake_rm.db_store = fake_db

        with patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=fake_rm,
        ):
            text = PromptBuilder()._build_phase_result_v2_requirements("summarize_brief")

        assert '"summary_doc": "..."' in text
        assert '"complexity_estimate": "..."' in text
        assert '"research_brief": "..."' in text

    def test_prompt_builder_no_longer_exposes_skill_specific_mode_guidance_helper(self):
        from core.application.orchestration.agents.prompt_builder import PromptBuilder

        assert not hasattr(PromptBuilder(), "_mode_guidance_lines")


class TestPromptSafetyGuard:
    def test_execute_blocks_llm_invoke_when_prompt_guard_detects_sensitive_project_value(self):
        from core.application.orchestration.agents.base import AgentInput

        manifest = _make_manifest(agent_id="designer", skills=["design_solution"])
        agent = _authorized_prompt_agent(manifest)
        state = _make_state(
            project_config={
                "credentials": {"primary_dsn": "postgres://admin:secret@db/app"},
                "default_branch": "main",
                "stack": {"language": "python"},
            },
        )
        agent_input = AgentInput(
            task_context={},
            step_context={},
            agent_config={},
            skill="design_solution",
            state_ref=state,
        )

        with (
            patch(
                "core.application.orchestration.agents.prompt_agent.PromptBuilder"
            ) as builder_cls,
            patch(
                "core.application.orchestration.agents.prompt_agent._resolve_provider",
                return_value="openai",
            ),
            patch("core.application.orchestration.agents.prompt_agent.invoke_agent") as invoke_mock,
            patch.object(agent, "_resolve_tools", return_value=[]),
            patch.object(agent, "_get_context_sections", return_value=[]),
            patch.object(agent, "_log_prompt_request"),
            patch.object(agent, "_log_prompt_response"),
        ):
            builder = builder_cls.return_value
            builder.build.return_value = (
                "system",
                "Use DSN postgres://admin:secret@db/app for connection setup.",
                "v1",
            )
            output = agent.execute(agent_input)

        assert output.status == "fail"
        assert "sensitive value" in str(output.artifacts.get("error", "")).lower()
        invoke_mock.assert_not_called()

    def test_log_prompt_trace_redacts_payload_result_and_error(self):
        from core.application.orchestration.agents.prompt_agent import PromptAgent

        agent = PromptAgent(_make_manifest(agent_id="designer", skills=["design_solution"]))
        fake_logger = MagicMock()
        state = _make_state(
            job_id=41,
            project_config={"project_id": 9, "git_root": "/target", "default_branch": "main"},
        )

        with (
            patch(
                "core.application.orchestration.agents.common._resolve_observability_dsn",
                return_value="postgres://dsn",
            ),
            patch(
                "core.application.orchestration.agents.common._get_event_logger",
                return_value=fake_logger,
            ),
            patch(
                "core.infrastructure.security.integration.scan_outbound",
                return_value=types.SimpleNamespace(action="redact", redacted_text="[REDACTED]"),
            ),
        ):
            agent._log_prompt_trace(
                state,
                action_type="prompt_request",
                skill="design_solution",
                provider="openai",
                correlation_id="corr-1",
                payload={"system_prompt": "postgres://admin:secret@db/app"},
                result={"excerpt": "AKIAIOSFODNN7EXAMPLE"},
                status="error",
                error="token=secret",
            )

        kwargs = fake_logger.log_action.call_args.kwargs
        assert kwargs["project_id"] == "9"
        assert kwargs["payload"]["system_prompt"] == "[REDACTED]"
        assert kwargs["result"]["excerpt"] == "[REDACTED]"
        assert kwargs["error"] == "[REDACTED]"

    def test_prompt_trace_preserves_tool_config_project_name_when_pii_scanner_redacts(self):
        from core.application.orchestration.agents.prompt_agent import PromptAgent

        payload = {
            "user_prompt": (
                "Please help Alice.\n\n"
                "## Tool Configuration\n"
                "- project_name: Flow Steward App   ← runtime project scope\n"
                "- branch_name: main\n"
                "- token: super-secret-token-value"
            )
        }

        with patch(
            "core.infrastructure.security.integration.scan_outbound",
            return_value=types.SimpleNamespace(action="redact", redacted_text="[REDACTED]"),
        ):
            safe = PromptAgent._redact_prompt_trace_payload(
                payload,
                project_id=9,
                job_id=41,
                context_type="prompt",
            )

        assert "Please help Alice" not in safe["user_prompt"]
        assert "- project_name: Flow Steward App" in safe["user_prompt"]
        assert "runtime project scope" in safe["user_prompt"]
        assert "super-secret-token-value" not in safe["user_prompt"]
        assert safe["user_prompt"].count("[REDACTED]") >= 2

    def test_prompt_trace_redacts_untrusted_tool_config_lines(self):
        from core.application.orchestration.agents.prompt_agent import PromptAgent

        payload = {
            "user_prompt": (
                "User supplied section:\n\n"
                "## Tool Configuration\n"
                "- customer_email: alice@example.com\n\n"
                "## Tool Configuration\n"
                "- project_name: Flow Steward App   ← runtime project scope\n"
                "- branch_name: main"
            )
        }

        with patch(
            "core.infrastructure.security.integration.scan_outbound",
            return_value=types.SimpleNamespace(action="redact", redacted_text="[REDACTED]"),
        ):
            safe = PromptAgent._redact_prompt_trace_payload(
                payload,
                project_id=9,
                job_id=41,
                context_type="prompt",
            )

        assert "alice@example.com" not in safe["user_prompt"]
        assert "- project_name: Flow Steward App" in safe["user_prompt"]

    def test_execute_outbound_scan_uses_project_config_project_id(self):
        from core.application.orchestration.agents.base import AgentInput

        manifest = _make_manifest(agent_id="designer", skills=["design_solution"])
        agent = _authorized_prompt_agent(manifest)
        state = _make_state(
            job_id=41,
            project_config={
                "project_id": 77,
                "git_root": "/target",
                "default_branch": "main",
                "stack": {"language": "python"},
            },
        )
        agent_input = AgentInput(
            task_context={},
            step_context={},
            agent_config={},
            skill="design_solution",
            state_ref=state,
        )

        with (
            patch(
                "core.application.orchestration.agents.prompt_agent.PromptBuilder"
            ) as builder_cls,
            patch("core.application.orchestration.agents.prompt_agent.emit_event"),
            patch(
                "core.application.orchestration.agents.prompt_agent._resolve_provider",
                return_value="openai",
            ),
            patch(
                "core.application.orchestration.agents.prompt_agent.invoke_agent",
                return_value=types.SimpleNamespace(content="done"),
            ),
            patch(
                "core.infrastructure.security.integration.scan_outbound",
                return_value=types.SimpleNamespace(action="allow", redacted_text="done"),
            ) as scan_mock,
            patch.object(agent, "_resolve_tools", return_value=[]),
            patch.object(agent, "_get_context_sections", return_value=[]),
            patch.object(agent, "_log_prompt_request"),
            patch.object(agent, "_log_prompt_response"),
            patch.object(agent, "_parse_artifacts", return_value={}),
            patch.object(agent, "_extract_verdict", return_value={}),
            patch.object(agent, "validate_agent_output", return_value=[]),
            patch.object(
                agent,
                "_build_phase_result_v2",
                return_value=({"status": "pass", "summary": "done", "confidence": 0.9}, []),
            ),
        ):
            builder = builder_cls.return_value
            builder.build.return_value = ("system prompt", "user prompt", "v1")
            output = agent.execute(agent_input)

        assert output.status == "pass"
        kwargs = scan_mock.call_args.kwargs
        assert kwargs["project_id"] == "77"
        assert kwargs["job_id"] == "41"


def test_invoke_agent_ignores_legacy_http_tools_and_executes_ordinary_and_provider_tools():
    from langchain_core.tools import tool

    from core.application.orchestration.agents.common import invoke_agent

    ordinary_calls: list[str] = []

    @tool("ordinary_tool")
    def ordinary_tool(value: str) -> str:
        """Echo a value through an ordinary registered tool."""
        ordinary_calls.append(value)
        return f"ordinary:{value}"

    class _MixedToolModel:
        def __init__(self):
            self.bound_names: list[str] = []
            self.responses = [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "ordinary_tool", "args": {"value": "kept"}, "id": "ordinary-call"},
                        {
                            "name": "echo_text",
                            "args": {"arguments_json": json.dumps({"text": "provider-kept"})},
                            "id": "provider-call",
                        },
                        # A stale model call must not find an executable HTTP tool.
                        {"name": "retired_http", "args": {}, "id": "retired-call"},
                    ],
                ),
                AIMessage(content="done"),
            ]

        def bind_tools(self, tools, **_kwargs):
            self.bound_names = [item.name for item in tools]
            return self

        def invoke(self, _messages, **_kwargs):
            return self.responses.pop(0)

    model = _MixedToolModel()
    state = {
        "project_name": "Demo",
        "project_config": {
            "extra_tools": [
                {
                    "name": "retired_http",
                    "description": "Retired inline HTTP tool",
                    "type": "http",
                    "url": "https://retired.example.invalid/hook",
                    "method": "POST",
                }
            ],
            "project_providers": [
                {
                    "provider_key": "example_tool_provider",
                    "provider_type_key": "tool_provider",
                    "enabled": True,
                    "provider_enabled": True,
                    "implementation_kind": "extension",
                    "provider_config": {"extension_key": "example_tool_provider"},
                    "binding_config": {
                        "tools": [
                            {
                                "name": "echo_text",
                                "description": "Echo through an enabled ToolProvider",
                                "tool_name": "echo_text",
                            }
                        ]
                    },
                }
            ],
        },
    }
    with (
        patch("core.application.orchestration.agents.common.build_context_block", return_value=""),
        patch(
            "core.application.orchestration.agents.common.build_langchain_prompt",
            return_value=_FakePrompt(),
        ),
        patch("core.application.orchestration.agents.common.get_chat_model", return_value=model),
        patch("core.application.orchestration.agents.common.emit_event"),
        patch(
            "core.application.orchestration.tools.tool_resolver._resolve_extension_record",
            return_value=(
                {
                    "extension_key": "example_tool_provider",
                    "enabled": True,
                    "kind": "tool_provider",
                },
                MagicMock(),
            ),
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_invocation",
            return_value={
                "status": "succeeded",
                "response": {"ok": True, "result": {"text": "provider-kept"}},
            },
        ) as provider_invocation,
        patch(
            "core.application.orchestration.http_request_step_runtime.execute_http_request_step",
            return_value={"ok": True, "body": {"text": "retired"}},
        ) as inline_http,
    ):
        response = invoke_agent(
            state=state,
            persona={"name": "Developer", "system_prompt": "You are helpful."},
            platform_config={},
            task_input="Use available tools",
            agent_name="composer",
            tools=[ordinary_tool],
            context_sections=[],
        )

    assert set(model.bound_names) == {"ordinary_tool", "echo_text"}
    assert ordinary_calls == ["kept"]
    provider_invocation.assert_called_once()
    assert provider_invocation.call_args.kwargs["request_payload"]["arguments"] == {
        "text": "provider-kept",
    }
    inline_http.assert_not_called()
    assert response.content == "done"
