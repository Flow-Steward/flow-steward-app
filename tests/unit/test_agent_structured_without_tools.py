"""An agent without tools answers in its response schema with one model call."""

from __future__ import annotations

import json

import pytest
from langchain_core.exceptions import OutputParserException
from langchain_core.messages import AIMessage, HumanMessage

from core.application.orchestration.agents import common
from core.application.orchestration.agents import common_runtime_helpers as helpers

PHASE_RESULT = {
    "schema_version": "phase_result_v2",
    "status": "pass",
    "summary": "Read the receipt.",
    "artifacts": {"total": 12.49},
    "evidence": [],
    "errors": [],
    "files_to_touch": [],
    "next_action": "next_phase",
    "confidence": 0.9,
}
SCHEMA = {"type": "object", "properties": {"status": {"type": "string"}}}


class _Prompt:
    def format_messages(self, **kwargs):
        return [HumanMessage(content=kwargs["task_input"])]


class _Model:
    """A routed chat model that records every call it receives."""

    def __init__(self, structured_answer=None):
        self.metadata = {"flow_steward_provider_policy": "openai"}
        self.free_form_calls: list[list] = []
        self.structured_calls: list[list] = []
        self.structured_answer = structured_answer or PHASE_RESULT

    def bind_tools(self, tools):
        return self

    def invoke(self, messages, **kwargs):
        self.free_form_calls.append(list(messages))
        return AIMessage(content="draft in prose")

    def with_structured_output(self, schema=None, **kwargs):
        model = self

        class _Structured:
            def invoke(self, messages, **kwargs):
                model.structured_calls.append(list(messages))
                answer = model.structured_answer
                if isinstance(answer, Exception):
                    raise answer
                return answer

        return _Structured()


def _run(monkeypatch, model, *, tools=None, schema=SCHEMA):
    monkeypatch.setattr(common, "build_context_block", lambda *a, **kw: "")
    monkeypatch.setattr(common, "build_langchain_prompt", lambda *a, **kw: _Prompt())
    monkeypatch.setattr(common, "get_chat_model", lambda *a, **kw: model)
    monkeypatch.setattr(common, "emit_event", lambda *a, **kw: None)
    monkeypatch.setattr(helpers, "_scan_outbound_response", lambda _state, response: response)
    return common.invoke_agent(
        state={"project_config": {}},
        persona={"system_prompt": ""},
        platform_config={},
        task_input="RECEIPT TEXT ...",
        agent_name="receipt_reader_agent",
        tools=tools,
        timeout_seconds=600,
        final_response_schema=schema,
    )


def test_an_agent_without_tools_answers_in_its_schema_with_one_call(monkeypatch):
    model = _Model()

    response = _run(monkeypatch, model)

    # One structured call over the prompt itself: no prose draft, no rewrite request.
    assert model.free_form_calls == []
    assert len(model.structured_calls) == 1
    assert [message.content for message in model.structured_calls[0]] == ["RECEIPT TEXT ..."]
    assert json.loads(response.content) == PHASE_RESULT
    assert response.additional_kwargs["structured_output"] == PHASE_RESULT


def test_an_agent_with_tools_keeps_the_draft_and_rewrite(monkeypatch):
    from langchain_core.tools import StructuredTool

    def lookup(term: str) -> str:
        """Look a term up."""
        return term

    model = _Model()

    _run(monkeypatch, model, tools=[StructuredTool.from_function(lookup)])

    # The tool loop drafts first; the schema rewrite is the second call.
    assert len(model.free_form_calls) == 1
    assert len(model.structured_calls) == 1
    assert "draft response above" in model.structured_calls[0][-1].content


def test_an_agent_without_a_schema_answers_in_prose(monkeypatch):
    model = _Model()

    response = _run(monkeypatch, model, schema=None)

    assert response.content == "draft in prose"
    assert model.structured_calls == []


def test_an_answer_outside_the_schema_fails_closed(monkeypatch):
    model = _Model(structured_answer=OutputParserException("not the schema"))

    with pytest.raises(RuntimeError, match="structured_output_unavailable: not the schema"):
        _run(monkeypatch, model)


def test_a_provider_error_reaches_the_retry_policy_unchanged(monkeypatch):
    class RateLimitError(Exception):
        pass

    model = _Model(structured_answer=RateLimitError("insufficient_quota"))
    monkeypatch.setattr(
        helpers,
        "_llm_invoke_with_retry",
        lambda llm, messages, **kwargs: llm.invoke(messages),
    )

    with pytest.raises(RateLimitError, match="insufficient_quota"):
        _run(monkeypatch, model)


def test_the_one_call_uses_the_steps_own_timeout_and_retries(monkeypatch):
    calls: list[dict] = []

    def _record(llm, messages, **kwargs):
        calls.append(kwargs)
        return llm.invoke(messages)

    monkeypatch.setattr(helpers, "_llm_invoke_with_retry", _record)
    monkeypatch.setenv("FS_STRUCTURED_OUTPUT_TIMEOUT_SECONDS", "7")

    _run(monkeypatch, _Model())

    # Not the short cap of a rewrite, and the ordinary provider retries.
    assert len(calls) == 1
    assert calls[0]["timeout_seconds"] not in (None, 7)
    assert "max_retries" not in calls[0]
