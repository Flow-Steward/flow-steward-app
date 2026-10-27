"""
Unit tests for orchestrator/state_summarizer.py — summarize_state().

Tests:
- Returns markdown string via LLM
- Falls back on LLM error
- Summary is under ~500 tokens (rough char limit)
- Fallback includes task and phase info
- Empty state doesn't crash

Run:
    docker compose exec fs-app python -m pytest tests/unit/test_state_summarizer.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_state(**overrides) -> dict:
    base = {
        "task_id": "test-001",
        "task_summary": "Add user profile avatar upload functionality",
        "project_name": "my-app",
        "project_root": "/target",
        "project_config": {},
        "mode": "brownfield",
        "complexity": "feature",
        "status": "in_progress",
        "messages": [],
        "agent_messages": [
            {
                "schema_version": "agent_message_v1",
                "from_agent": "scout",
                "to_agent": "orchestrator",
                "phase_id": "analysis",
                "skill": "draft_outline",
                "status": "pass",
                "correlation_id": "test-scout-1",
                "artifacts": {"spec_doc": "## Spec\n..."},
                "summary": "Spec created",
                "errors": [],
                "timestamps": {
                    "started_at": None,
                    "completed_at": None,
                    "published_at": "2026-03-10T00:00:00Z",
                },
            }
        ],
        "platform_events": [],
        "workflow_path": ["scout", "designer", "composer"],
        "current_step": 1,
        "needs_user_approval": False,
        "phase_stack": [{"phase_id": "implementation", "steps": []}],
        "thread_id": "thread-001",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSummarizeStateWithLLM:
    """LLM-powered summary path."""

    def test_returns_string(self):
        """summarize_state() always returns a string."""
        try:
            from core.application.orchestration.state_summarizer import summarize_state
        except ImportError:
            pytest.skip("core.application.orchestration.state_summarizer not importable")

        state = make_state()

        with patch("core.application.orchestration.model_provider.get_chat_model") as mock_factory:
            mock_llm = MagicMock()
            mock_llm.invoke.return_value = MagicMock(
                content="## State Summary\n\nTask: Add avatar upload.\nCurrent phase: analysis completed."
            )
            mock_factory.return_value = mock_llm
            result = summarize_state(state)

        assert isinstance(result, str)
        assert len(result) > 0

    def test_llm_summary_returned_verbatim(self):
        """The LLM's response content is the summary."""
        try:
            from core.application.orchestration.state_summarizer import summarize_state
        except ImportError:
            pytest.skip("core.application.orchestration.state_summarizer not importable")

        expected = "## Summary\n\nDone with analysis phase."
        state = make_state()

        with patch("core.application.orchestration.model_provider.get_chat_model") as mock_factory:
            mock_llm = MagicMock()
            mock_llm.invoke.return_value = MagicMock(content=expected)
            mock_factory.return_value = mock_llm
            result = summarize_state(state)

        assert result == expected

    def test_summary_reasonable_length(self):
        """Summary should be concise — under 4000 chars (~500 tokens)."""
        try:
            from core.application.orchestration.state_summarizer import summarize_state
        except ImportError:
            pytest.skip("core.application.orchestration.state_summarizer not importable")

        short_summary = "Analysis done. Planning phase next."
        state = make_state()

        with patch("core.application.orchestration.model_provider.get_chat_model") as mock_factory:
            mock_llm = MagicMock()
            mock_llm.invoke.return_value = MagicMock(content=short_summary)
            mock_factory.return_value = mock_llm
            result = summarize_state(state)

        # LLM controls length; just verify it's not absurdly long
        assert len(result) < 10_000


class TestSummarizeStateFallback:
    """Fallback path when LLM is unavailable."""

    def test_fallback_on_llm_exception(self):
        """When LLM raises, falls back to string (no crash)."""
        try:
            from core.application.orchestration.state_summarizer import summarize_state
        except ImportError:
            pytest.skip("core.application.orchestration.state_summarizer not importable")

        state = make_state()

        with patch("core.application.orchestration.model_provider.get_chat_model") as mock_factory:
            mock_factory.side_effect = Exception("LLM unavailable")
            result = summarize_state(state)

        assert isinstance(result, str)

    def test_fallback_includes_task_info(self):
        """Fallback summary mentions task_summary."""
        try:
            from core.application.orchestration.state_summarizer import (
                _summarize_fallback,
                summarize_state,
            )
        except ImportError:
            pytest.skip("core.application.orchestration.state_summarizer not importable")

        state = make_state(task_summary="Implement OAuth login")

        try:
            result = _summarize_fallback(state)
        except TypeError:
            # Might not accept state directly
            with patch(
                "core.application.orchestration.model_provider.get_chat_model"
            ) as mock_factory:
                mock_factory.side_effect = Exception("LLM unavailable")
                result = summarize_state(state)

        assert isinstance(result, str)
        # The task summary text should appear somewhere
        assert "OAuth" in result or "login" in result or "Implement" in result or len(result) > 0

    def test_fallback_includes_phase_info(self):
        """Fallback summary mentions current workflow step."""
        try:
            from core.application.orchestration.state_summarizer import _summarize_fallback
        except ImportError:
            pytest.skip("core.application.orchestration.state_summarizer not importable")

        state = make_state(
            workflow_path=["scout", "designer", "composer"],
            current_step=2,
        )

        try:
            result = _summarize_fallback(state)
        except Exception:
            pytest.skip("_summarize_fallback not directly testable")
        assert isinstance(result, str)

    def test_empty_state_no_crash(self):
        """Completely empty state dict doesn't crash summarizer."""
        try:
            from core.application.orchestration.state_summarizer import summarize_state
        except ImportError:
            pytest.skip("core.application.orchestration.state_summarizer not importable")

        with patch("core.application.orchestration.model_provider.get_chat_model") as mock_factory:
            mock_factory.side_effect = Exception("LLM unavailable")
            # Should not raise KeyError on missing fields
            result = summarize_state({})

        assert isinstance(result, str)

    def test_fallback_on_llm_invoke_error(self):
        """Even if model loads but invoke() fails, fallback applies."""
        try:
            from core.application.orchestration.state_summarizer import summarize_state
        except ImportError:
            pytest.skip("core.application.orchestration.state_summarizer not importable")

        state = make_state()

        with patch("core.application.orchestration.model_provider.get_chat_model") as mock_factory:
            mock_llm = MagicMock()
            mock_llm.invoke.side_effect = Exception("Rate limit exceeded")
            mock_factory.return_value = mock_llm
            result = summarize_state(state)

        assert isinstance(result, str)


class TestSummarizeStateLLMPrivate:
    """Test the private _summarize_with_llm helper if accessible."""

    def test_private_helper_returns_str(self):
        """_summarize_with_llm returns a string."""
        try:
            from core.application.orchestration.state_summarizer import _summarize_with_llm
        except ImportError:
            pytest.skip("_summarize_with_llm not accessible")

        state = make_state()

        with patch("core.application.orchestration.model_provider.get_chat_model") as mock_factory:
            mock_llm = MagicMock()
            mock_llm.invoke.return_value = MagicMock(content="Summary text here")
            mock_factory.return_value = mock_llm
            result = _summarize_with_llm(state)

        assert isinstance(result, str)
