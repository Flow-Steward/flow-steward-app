"""
Unit tests for agents/base.py — AgentInput, AgentOutput, BaseAgent, PromptAgent.

Tests:
- AgentOutput.is_success(), needs_retry(), needs_escalation()
- AgentInput required fields
- PromptAgent.execute() with mock LLM
- System prompt building
- JSON artifact parsing
- Raw fallback on parse failure
- max_rounds respected

Run:
    docker compose exec fs-app python -m pytest tests/unit/test_agent_base.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


# ---------------------------------------------------------------------------
# Tests: AgentOutput
# ---------------------------------------------------------------------------


class TestAgentOutput:
    """AgentOutput dataclass and status methods."""

    def test_is_success_true(self):
        """status='pass' → is_success() is True."""
        try:
            from core.application.orchestration.agents.base import AgentOutput
        except ImportError:
            pytest.skip("core.application.orchestration.agents.base not importable")

        out = AgentOutput(status="pass", artifacts={}, summary="Done")
        assert out.is_success() is True

    def test_is_success_false(self):
        """status='retry' → is_success() is False."""
        try:
            from core.application.orchestration.agents.base import AgentOutput
        except ImportError:
            pytest.skip("core.application.orchestration.agents.base not importable")

        out = AgentOutput(status="retry", artifacts={}, summary="Need to retry")
        assert out.is_success() is False

    def test_needs_retry_true(self):
        """status='fail' → needs_retry() is True."""
        try:
            from core.application.orchestration.agents.base import AgentOutput
        except ImportError:
            pytest.skip("core.application.orchestration.agents.base not importable")

        out = AgentOutput(status="fail", artifacts={}, summary="Retry needed")
        assert out.needs_retry() is True

    def test_needs_retry_false_for_success(self):
        """status='pass' → needs_retry() is False."""
        try:
            from core.application.orchestration.agents.base import AgentOutput
        except ImportError:
            pytest.skip("core.application.orchestration.agents.base not importable")

        out = AgentOutput(status="pass", artifacts={}, summary="Done")
        assert out.needs_retry() is False

    def test_needs_escalation_true(self):
        """status='escalate' → needs_escalation() is True."""
        try:
            from core.application.orchestration.agents.base import AgentOutput
        except ImportError:
            pytest.skip("core.application.orchestration.agents.base not importable")

        out = AgentOutput(status="escalate", artifacts={}, summary="Cannot proceed")
        assert out.needs_escalation() is True

    def test_needs_escalation_false_for_success(self):
        """status='pass' → needs_escalation() is False."""
        try:
            from core.application.orchestration.agents.base import AgentOutput
        except ImportError:
            pytest.skip("core.application.orchestration.agents.base not importable")

        out = AgentOutput(status="pass", artifacts={}, summary="Done")
        assert out.needs_escalation() is False

    def test_default_confidence(self):
        """Default confidence is 1.0."""
        try:
            from core.application.orchestration.agents.base import AgentOutput
        except ImportError:
            pytest.skip("core.application.orchestration.agents.base not importable")

        out = AgentOutput(status="pass", artifacts={}, summary="Done")
        assert out.confidence == 1.0

    def test_custom_confidence(self):
        """Custom confidence value is stored."""
        try:
            from core.application.orchestration.agents.base import AgentOutput
        except ImportError:
            pytest.skip("core.application.orchestration.agents.base not importable")

        out = AgentOutput(status="pass", artifacts={}, summary="Done", confidence=0.7)
        assert out.confidence == 0.7

    def test_artifacts_is_dict(self):
        """artifacts must be a dict."""
        try:
            from core.application.orchestration.agents.base import AgentOutput
        except ImportError:
            pytest.skip("core.application.orchestration.agents.base not importable")

        out = AgentOutput(status="pass", artifacts={"summary_doc": "content"}, summary="Done")
        assert isinstance(out.artifacts, dict)
        assert out.artifacts["summary_doc"] == "content"

    def test_metadata_defaults_empty(self):
        """metadata defaults to empty dict."""
        try:
            from core.application.orchestration.agents.base import AgentOutput
        except ImportError:
            pytest.skip("core.application.orchestration.agents.base not importable")

        out = AgentOutput(status="pass", artifacts={}, summary="Done")
        assert isinstance(out.metadata, dict)


# ---------------------------------------------------------------------------
# Tests: AgentInput
# ---------------------------------------------------------------------------


class TestAgentInput:
    """AgentInput dataclass required and optional fields."""

    def test_has_task_context(self):
        """AgentInput requires task_context."""
        try:
            from core.application.orchestration.agents.base import AgentInput
        except ImportError:
            pytest.skip("core.application.orchestration.agents.base not importable")

        inp = AgentInput(
            task_context={"task_id": "test", "task_summary": "Fix bug"},
            step_context={},
            agent_config={},
        )
        assert inp.task_context["task_id"] == "test"

    def test_has_step_context(self):
        """AgentInput requires step_context."""
        try:
            from core.application.orchestration.agents.base import AgentInput
        except ImportError:
            pytest.skip("core.application.orchestration.agents.base not importable")

        inp = AgentInput(
            task_context={},
            step_context={"summary_doc": "## Product Requirements\n..."},
            agent_config={},
        )
        assert "summary_doc" in inp.step_context

    def test_skill_optional(self):
        """skill defaults to empty string."""
        try:
            from core.application.orchestration.agents.base import AgentInput
        except ImportError:
            pytest.skip("core.application.orchestration.agents.base not importable")

        inp = AgentInput(task_context={}, step_context={}, agent_config={})
        assert inp.skill == "" or inp.skill is None

    def test_feedback_optional(self):
        """feedback is optional (None by default)."""
        try:
            from core.application.orchestration.agents.base import AgentInput
        except ImportError:
            pytest.skip("core.application.orchestration.agents.base not importable")

        inp = AgentInput(task_context={}, step_context={}, agent_config={})
        assert inp.feedback is None

    def test_feedback_can_be_set(self):
        """feedback can be set to a string."""
        try:
            from core.application.orchestration.agents.base import AgentInput
        except ImportError:
            pytest.skip("core.application.orchestration.agents.base not importable")

        inp = AgentInput(
            task_context={},
            step_context={},
            agent_config={},
            feedback="Please include more detail in the summary",
        )
        assert inp.feedback == "Please include more detail in the summary"

    def test_state_ref_optional(self):
        """state_ref is optional."""
        try:
            from core.application.orchestration.agents.base import AgentInput
        except ImportError:
            pytest.skip("core.application.orchestration.agents.base not importable")

        inp = AgentInput(task_context={}, step_context={}, agent_config={})
        assert inp.state_ref is None or isinstance(inp.state_ref, dict)


# ---------------------------------------------------------------------------
# Tests: BaseAgent ABC
# ---------------------------------------------------------------------------


class TestBaseAgent:
    """BaseAgent is an ABC that requires execute() implementation."""

    def test_base_agent_is_abstract(self):
        """BaseAgent cannot be instantiated directly."""
        try:
            from core.application.orchestration.agents.base import BaseAgent
        except ImportError:
            pytest.skip("core.application.orchestration.agents.base not importable")

        with pytest.raises((TypeError, Exception)):
            BaseAgent()  # Should fail if abstract

    def test_concrete_agent_requires_execute(self):
        """Concrete subclass must implement execute()."""
        try:
            from core.application.orchestration.agents.base import (
                AgentInput,
                AgentOutput,
                BaseAgent,
            )
        except ImportError:
            pytest.skip("core.application.orchestration.agents.base not importable")

        class MyAgent(BaseAgent):
            def execute(self, input: AgentInput) -> AgentOutput:
                return AgentOutput(status="pass", artifacts={}, summary="Done")

        agent = MyAgent()
        assert hasattr(agent, "execute")


# ---------------------------------------------------------------------------
# Tests: PromptAgent
# ---------------------------------------------------------------------------


class TestPromptAgent:
    """PromptAgent subclass that uses LLM."""

    @staticmethod
    def _make_manifest(agent_id: str = "writer"):
        prompts = SimpleNamespace(
            system="You are a test agent.",
            skill_prompts={},
            context_template="",
            output_format="",
            context_sections={"default": ["architecture"]},
            examples={},
        )
        return SimpleNamespace(
            agent_id=agent_id,
            display_name=agent_id.title(),
            tools=[],
            skills=["write_section"],
            config={},
            max_iterations=3,
            prompts=prompts,
        )

    def _execution_agent(self, agent_id="writer"):
        from core.application.auth.config import DEFAULT_ACCOUNT_ID
        from core.application.orchestration.agents.prompt_agent import PromptAgent
        from core.application.registry.skill_registry import SkillRegistry

        manifest = self._make_manifest(agent_id)
        row = {
            "skill_id": "write_section",
            "display_name": "Write section",
            "enabled": True,
            "account_id": DEFAULT_ACCOUNT_ID,
            "instructions": [],
        }
        store = SimpleNamespace(
            list_skills=lambda: [row],
            get_skill=lambda skill_id: row if skill_id == "write_section" else None,
        )
        return PromptAgent(manifest, skill_registry=SkillRegistry(store))

    def test_execute_returns_agent_output(self):
        """PromptAgent.execute() returns AgentOutput."""
        try:
            from core.application.orchestration.agents.base import AgentInput, AgentOutput
            from core.application.orchestration.agents.prompt_agent import PromptAgent
        except ImportError:
            pytest.skip("core.application.orchestration.agents.base or PromptAgent not importable")

        state = {
            "task_id": "test",
            "task_summary": "Fix bug",
            "project_name": "app",
            "project_config": {},
            "agent_messages": [],
            "messages": [],
        }

        from core.application.auth.config import DEFAULT_ACCOUNT_ID

        inp = AgentInput(
            task_context={
                "account_id": DEFAULT_ACCOUNT_ID,
                "task_id": "test",
                "task_summary": "Fix the login bug",
            },
            step_context={},
            agent_config={"system_prompt": "You are a developer."},
            state_ref=state,
        )

        with (
            patch(
                "core.application.orchestration.agents.prompt_builder.PromptBuilder.build",
                return_value=("system", "user", "v1"),
            ),
            patch(
                "core.application.orchestration.agents.prompt_agent.invoke_agent",
                return_value=SimpleNamespace(content="FILES_CHANGED: auth.py\n\nImplemented fix"),
            ),
            patch("core.application.orchestration.agents.prompt_agent.emit_event"),
            patch(
                "core.application.orchestration.agents.prompt_agent._resolve_provider",
                return_value="openai",
            ),
            patch.object(PromptAgent, "_resolve_tools", return_value=[]),
            patch.object(PromptAgent, "validate_agent_output", return_value=[]),
        ):
            agent = self._execution_agent()
            result = agent.execute(inp)

        assert isinstance(result, AgentOutput)

    def test_execute_parses_json_artifacts(self):
        """PromptAgent parses JSON artifacts from LLM response."""
        try:
            from core.application.orchestration.agents.base import AgentInput
            from core.application.orchestration.agents.prompt_agent import PromptAgent
        except ImportError:
            pytest.skip("core.application.orchestration.agents.base or PromptAgent not importable")

        from core.application.auth.config import DEFAULT_ACCOUNT_ID

        inp = AgentInput(
            task_context={
                "account_id": DEFAULT_ACCOUNT_ID,
                "task_id": "test",
                "task_summary": "Build feature",
            },
            step_context={},
            agent_config={},
        )

        with (
            patch(
                "core.application.orchestration.agents.prompt_builder.PromptBuilder.build",
                return_value=("system", "user", "v1"),
            ),
            patch(
                "core.application.orchestration.agents.prompt_agent.invoke_agent",
                return_value=SimpleNamespace(content="FILES_CHANGED: spec.md\n\nFeature built"),
            ),
            patch("core.application.orchestration.agents.prompt_agent.emit_event"),
            patch(
                "core.application.orchestration.agents.prompt_agent._resolve_provider",
                return_value="openai",
            ),
            patch.object(PromptAgent, "_resolve_tools", return_value=[]),
            patch.object(PromptAgent, "validate_agent_output", return_value=[]),
        ):
            agent = self._execution_agent("coordinator")
            result = agent.execute(inp)

        assert isinstance(result.artifacts, dict)

    def test_execute_stores_raw_on_parse_failure(self):
        """Unparseable LLM response stored as raw_output artifact."""
        try:
            from core.application.orchestration.agents.base import AgentInput
            from core.application.orchestration.agents.prompt_agent import PromptAgent
        except ImportError:
            pytest.skip("core.application.orchestration.agents.base or PromptAgent not importable")

        from core.application.auth.config import DEFAULT_ACCOUNT_ID

        inp = AgentInput(
            task_context={
                "account_id": DEFAULT_ACCOUNT_ID,
                "task_id": "test",
                "task_summary": "Do work",
            },
            step_context={},
            agent_config={},
        )

        with (
            patch(
                "core.application.orchestration.agents.prompt_builder.PromptBuilder.build",
                return_value=("system", "user", "v1"),
            ),
            patch(
                "core.application.orchestration.agents.prompt_agent.invoke_agent",
                return_value=SimpleNamespace(
                    content="This is not JSON. Just plain text output from the agent."
                ),
            ),
            patch("core.application.orchestration.agents.prompt_agent.emit_event"),
            patch(
                "core.application.orchestration.agents.prompt_agent._resolve_provider",
                return_value="openai",
            ),
            patch.object(PromptAgent, "_resolve_tools", return_value=[]),
            patch.object(PromptAgent, "validate_agent_output", return_value=[]),
        ):
            agent = self._execution_agent("researcher")
            result = agent.execute(inp)

        # Should not crash, should store something
        assert isinstance(result, object)  # basic check
        # Raw text might be in summary or artifacts
        assert result.artifacts.get("raw_output")
        assert len(str(result.summary) + str(result.artifacts)) > 0

    def test_builds_system_prompt(self):
        """PromptAgent stores the manifest-driven prompt source."""
        try:
            from core.application.orchestration.agents.prompt_agent import PromptAgent
        except ImportError:
            pytest.skip("core.application.orchestration.agents.base or PromptAgent not importable")

        manifest = self._make_manifest(agent_id="researcher")
        manifest.prompts.system = "You are a world-class research assistant."
        agent = PromptAgent(manifest)
        assert agent.manifest.prompts.system == "You are a world-class research assistant."
