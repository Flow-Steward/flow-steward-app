"""Regression coverage for the AUDIT-020 classifier defect.

The app log recorded a successful model response immediately followed by
``LLM classification failed (expected string or bytes-like object, got 'list')``.
`AIMessage.content` is a list of content blocks under the Responses API, and the
classifier fed it straight to `re.search`, so every structured reply was thrown
away and the default fallback silently took over.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.application.orchestration import classifier

CLASSIFICATION = {
    "effort_hint": "small",
    "confidence": 0.9,
    "risk_level": "high",
    "automation_recommendation": "human_only",
    "reasoning": "touches authentication",
}


class TestResponseText:
    def test_plain_string_content_is_returned(self):
        assert classifier.response_text(SimpleNamespace(content="hello")) == "hello"

    def test_responses_api_block_list_is_flattened(self):
        """The exact shape that produced the reported TypeError."""
        response = SimpleNamespace(content=[{"type": "text", "text": '{"effort_hint": "small"}'}])

        assert classifier.response_text(response) == '{"effort_hint": "small"}'

    def test_multiple_blocks_are_concatenated_in_order(self):
        response = SimpleNamespace(
            content=[
                {"type": "text", "text": '{"effort_hint":'},
                {"type": "text", "text": ' "major"}'},
            ]
        )

        assert classifier.response_text(response) == '{"effort_hint": "major"}'

    def test_reasoning_blocks_without_text_are_skipped(self):
        response = SimpleNamespace(
            content=[
                {"type": "reasoning", "summary": []},
                {"type": "text", "text": "payload"},
            ]
        )

        assert classifier.response_text(response) == "payload"

    def test_blocks_nesting_under_content_are_read(self):
        response = SimpleNamespace(content=[{"type": "output_text", "content": "payload"}])

        assert classifier.response_text(response) == "payload"

    def test_bare_string_list_is_flattened(self):
        assert classifier.response_text(SimpleNamespace(content=["a", "b"])) == "ab"

    def test_response_without_content_falls_back_to_str(self):
        assert classifier.response_text("raw text") == "raw text"

    def test_unknown_content_shape_does_not_raise(self):
        assert classifier.response_text(SimpleNamespace(content={"unexpected": True}))


class TestClassifyWithBlockListResponses:
    @staticmethod
    def _llm_returning(content):
        return SimpleNamespace(invoke=lambda _messages: SimpleNamespace(content=content))

    def test_block_list_response_is_classified_rather_than_falling_back(self, caplog):
        """Before the fix this logged a TypeError and returned the default classification."""
        content = [{"type": "text", "text": json.dumps(CLASSIFICATION)}]

        with (
            patch.object(classifier, "classify_task", classifier.classify_task),
            patch(
                "core.application.orchestration.model_provider.get_chat_model",
                return_value=self._llm_returning(content),
            ),
        ):
            result = classifier.classify_task({"title": "Rotate the auth signing key"})

        assert result.effort_hint == "small"
        assert result.risk_level == "high"
        assert result.automation_recommendation == "human_only"
        assert result.reasoning != "default_classification", (
            "a well-formed model reply must not be discarded into the default fallback"
        )
        assert "expected string or bytes-like object" not in caplog.text

    def test_string_response_still_works(self):
        with patch(
            "core.application.orchestration.model_provider.get_chat_model",
            return_value=self._llm_returning(json.dumps(CLASSIFICATION)),
        ):
            result = classifier.classify_task({"title": "Rotate the auth signing key"})

        assert result.effort_hint == "small"
        assert result.reasoning != "default_classification"

    def test_configured_project_route_reaches_classifier_model_resolver(self):
        project_config = {
            "account_id": "00000000-0000-0000-0000-000000000002",
            "agents_config": {
                "default_provider": "custom:provider-42",
                "default_model": "gpt-5.6-luna",
                "model_provider_id": "provider-42",
            },
        }
        model = self._llm_returning(json.dumps(CLASSIFICATION))

        with patch(
            "core.application.orchestration.model_provider.get_chat_model",
            return_value=model,
        ) as get_chat_model:
            result = classifier.classify_task(
                {"title": "Rotate the auth signing key"},
                project_config=project_config,
            )

        get_chat_model.assert_called_once_with(project_config, agent_name="classifier")
        assert result.reasoning == "touches authentication"
        assert result.reasoning != "default_classification"

    def test_unparseable_response_still_falls_back_to_the_default(self):
        with patch(
            "core.application.orchestration.model_provider.get_chat_model",
            return_value=self._llm_returning([{"type": "text", "text": "no json here"}]),
        ):
            result = classifier.classify_task({"title": "Draft the supplier reply"})

        assert result.reasoning == "default_classification"
        assert result.risk_level == "medium"
        assert result.automation_recommendation == "supervised"

    @pytest.mark.parametrize("content", [[], [{"type": "reasoning"}]])
    def test_empty_content_falls_back_without_raising(self, content):
        with patch(
            "core.application.orchestration.model_provider.get_chat_model",
            return_value=self._llm_returning(content),
        ):
            result = classifier.classify_task({"title": "Update the handbook"})

        assert result.reasoning == "default_classification"
