"""
Unit tests for orchestrator/classifier.py — classify_task() function.

Tests both LLM path (mocked) and the neutral default fallback.

Run:
    docker compose exec fs-app python -m pytest tests/unit/test_classifier.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.application.orchestration.classifier import TaskClassification, classify_task

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_envelope(title: str, description: str = "", task_type: str = "request") -> dict:
    """Build a minimal TaskEnvelope dict for classify_task()."""
    return {
        "source": "manual",
        "source_id": "test-001",
        "task_type": task_type,
        "title": title,
        "description": description or title,
        "priority": "medium",
        "project_id": "test-project",
        "metadata": {},
        "trigger_config": {},
    }


# ---------------------------------------------------------------------------
# TaskClassification dataclass tests
# ---------------------------------------------------------------------------


class TestTaskClassificationDataclass:
    """Verify TaskClassification structure."""

    def test_has_all_required_fields(self):
        tc = TaskClassification(
            effort_hint="standard",
            confidence=0.9,
            risk_level="low",
            automation_recommendation="supervised",
            reasoning="Standard feature",
            task_type="feature",
        )
        assert tc.effort_hint == "standard"
        assert tc.confidence == 0.9
        assert tc.risk_level == "low"
        assert tc.automation_recommendation == "supervised"
        assert tc.reasoning == "Standard feature"
        assert tc.task_type == "feature"

    def test_valid_effort_hints(self):
        valid = {"micro", "small", "standard", "major"}
        for effort_hint in valid:
            tc = TaskClassification(
                effort_hint=effort_hint,
                confidence=0.8,
                risk_level="low",
                automation_recommendation="auto",
                reasoning="test",
                task_type="feature",
            )
            assert tc.effort_hint == effort_hint

    def test_valid_risk_levels(self):
        for rl in ("low", "medium", "high"):
            tc = TaskClassification(
                effort_hint="standard",
                confidence=0.8,
                risk_level=rl,
                automation_recommendation="auto",
                reasoning="test",
                task_type="feature",
            )
            assert tc.risk_level == rl

    def test_task_type_is_transport_only_freeform_text(self):
        for tt in ("feature", "incident", "ops_followup", "customer_reply", ""):
            tc = TaskClassification(
                effort_hint="standard",
                confidence=0.8,
                risk_level="low",
                automation_recommendation="auto",
                reasoning="test",
                task_type=tt,
            )
            assert tc.task_type == tt


# ---------------------------------------------------------------------------
# Default fallback tests (no LLM)
# ---------------------------------------------------------------------------

_DEFAULT_REASONING = "default_classification"


def _expected_default() -> tuple[str, float, str, str, str]:
    return ("standard", 0.6, "medium", "supervised", _DEFAULT_REASONING)


def _as_tuple(result: TaskClassification) -> tuple[str, float, str, str, str]:
    return (
        result.effort_hint,
        result.confidence,
        result.risk_level,
        result.automation_recommendation,
        result.reasoning,
    )


class TestDefaultClassification:
    """Without a model verdict every task gets the same neutral classification."""

    def _classify_without_llm(
        self, title: str, description: str = "", task_type: str = "request"
    ) -> TaskClassification:
        envelope = make_envelope(title, description, task_type)
        with patch(
            "core.application.orchestration.classifier._classify_with_llm",
            side_effect=Exception("LLM unavailable"),
        ):
            return classify_task(envelope)

    def test_fallback_returns_the_neutral_default(self):
        result = self._classify_without_llm("Prepare the weekly supplier digest")
        assert _as_tuple(result) == _expected_default()
        assert result.task_type == ""

    def test_fallback_ignores_task_wording(self):
        """No title or description keyword changes effort, risk or automation."""
        titles = [
            "Fix typo in the newsletter",
            "Crash report triage",
            "Refactor the onboarding checklist",
            "Rotate the payment credential",
            "Sync the webhook integration with the database",
            "",
        ]
        for title in titles:
            result = self._classify_without_llm(title, description=title)
            assert _as_tuple(result) == _expected_default(), title

    def test_deterministic_mode_bypasses_llm(self):
        envelope = make_envelope("Draft the customer reply", task_type="request")
        with patch("core.application.orchestration.classifier._classify_with_llm") as mock_llm:
            result = classify_task(envelope, deterministic=True)

        mock_llm.assert_not_called()
        assert isinstance(result, TaskClassification)
        assert _as_tuple(result) == _expected_default()

    def test_fallback_fails_safe_to_an_assisted_autonomy_gate(self):
        """An unavailable model must never grant automatic execution by default."""
        from core.application.orchestration.step_autonomy import classification_default_mode

        result = self._classify_without_llm("Publish the quarterly report")
        assert result.risk_level == "medium"
        assert result.automation_recommendation == "supervised"
        state = {"automation_recommendation": result.automation_recommendation}
        assert classification_default_mode(state) == "assisted"


# ---------------------------------------------------------------------------
# LLM path tests (mocked)
# ---------------------------------------------------------------------------


class TestLLMClassification:
    """Test classify_task() LLM path with mocked responses."""

    def _mock_llm_response(self, json_content: dict):
        """Create a mock that returns valid JSON classification."""
        from types import SimpleNamespace

        mock_llm = MagicMock()
        mock_llm.invoke.return_value = SimpleNamespace(content=json.dumps(json_content))
        return mock_llm

    def test_classify_with_valid_llm_response(self):
        """Mock LLM returns valid JSON → correct TaskClassification."""
        expected = {
            "effort_hint": "standard",
            "confidence": 0.85,
            "risk_level": "medium",
            "automation_recommendation": "supervised",
            "reasoning": "Standard feature requiring human review",
            "task_type": "feature",
        }
        mock_llm = self._mock_llm_response(expected)
        envelope = make_envelope("Add new dashboard widget")

        with patch("core.model_provider.get_chat_model", return_value=mock_llm):
            result = classify_task(envelope)

        # Should use LLM result if available
        assert isinstance(result, TaskClassification)
        assert 0.0 <= result.confidence <= 1.0

    def test_classify_llm_invalid_json_falls_back_to_default(self):
        """Mock LLM returns invalid JSON → falls back to the default classification."""
        from types import SimpleNamespace

        mock_llm = MagicMock()
        mock_llm.invoke.return_value = SimpleNamespace(content="not valid json {{{")

        envelope = make_envelope("Summarise the supplier feedback", task_type="request")

        with patch("core.model_provider.get_chat_model", return_value=mock_llm):
            result = classify_task(envelope)

        # Should not crash; the neutral default is returned
        assert isinstance(result, TaskClassification)
        assert result.reasoning == "default_classification"

    def test_classify_llm_exception_falls_back_to_default(self):
        """LLM raises exception → falls back to the default classification."""
        mock_llm = MagicMock()
        mock_llm.invoke.side_effect = RuntimeError("LLM service unavailable")

        envelope = make_envelope("Update user profile page")

        with patch("core.model_provider.get_chat_model", return_value=mock_llm):
            result = classify_task(envelope)

        assert isinstance(result, TaskClassification)

    def test_classify_always_returns_task_classification(self):
        """classify_task() should never raise — always returns TaskClassification."""
        envelopes = [
            make_envelope(""),
            make_envelope("Answer the vendor", task_type="request"),
            make_envelope("Publish the digest", task_type="report"),
            make_envelope("Service is down!", task_type="incident"),
        ]
        for env in envelopes:
            with patch(
                "core.application.orchestration.classifier._classify_with_llm",
                side_effect=Exception("no LLM"),
            ):
                result = classify_task(env)
                assert isinstance(result, TaskClassification), (
                    f"Expected TaskClassification for {env['title']}"
                )

    def test_custom_task_type_is_preserved_as_transport_metadata(self):
        envelope = make_envelope("Investigate billing export", task_type="ops_followup")
        with patch(
            "core.application.orchestration.classifier._classify_with_llm",
            side_effect=Exception("no LLM"),
        ):
            result = classify_task(envelope)
        assert result.task_type == ""


# ---------------------------------------------------------------------------
# classify_task() pass-through tests (default fallback)
# ---------------------------------------------------------------------------


class TestClassifyTaskPassThrough:
    """Optional context arguments never break the default fallback."""

    def test_kb_context_passed_through(self):
        envelope = make_envelope("Review the supplier contract")
        kb_context = "Previous task: reviewed the supplier contract last week."
        with patch(
            "core.application.orchestration.classifier._classify_with_llm", side_effect=Exception
        ):
            result = classify_task(envelope, kb_context=kb_context)
        assert isinstance(result, TaskClassification)

    def test_project_config_passed_through(self):
        envelope = make_envelope("Prepare the monthly report")
        project_config = {"team_size": 5}
        with patch(
            "core.application.orchestration.classifier._classify_with_llm", side_effect=Exception
        ):
            result = classify_task(envelope, project_config=project_config)
        assert isinstance(result, TaskClassification)
