from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from core.application.settings.llm_capability_verifier import (
    CapabilityMismatchError,
    _invoke_before_deadline,
    run_capability_suite,
)


class _Structured:
    def __init__(self, result, timeouts=None):
        self._result = result
        self._timeouts = timeouts if timeouts is not None else []

    def bind(self, **kwargs):
        self._timeouts.append(kwargs["timeout"])
        return self

    def invoke(self, _messages):
        return self._result


class _ProbeModel:
    def __init__(self, *, basic_content, replay_content="probe-ok"):
        self.basic_content = basic_content
        self.replay_content = replay_content
        self.timeouts = []

    def bind(self, **kwargs):
        self.timeouts.append(kwargs["timeout"])
        return self

    def bind_tools(self, _tools, **_kwargs):
        return self

    def invoke(self, messages):
        if isinstance(messages, str):
            return AIMessage(content=self.basic_content)
        if len(messages) == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "probe-call",
                        "name": "flow_steward_capability_probe",
                        "args": {"key": "probe"},
                    }
                ],
            )
        return AIMessage(content=self.replay_content)

    def with_structured_output(self, schema, **_kwargs):
        if "tool_value" in schema["properties"]:
            return _Structured({"status": "ok", "tool_value": "probe-ok"}, self.timeouts)
        return _Structured(
            {"status": "ok", "items": [{"id": 7, "label": "verified"}]},
            self.timeouts,
        )


def test_basic_probe_rejects_non_exact_content() -> None:
    _raises_input_65_1 = _ProbeModel(basic_content="anything")
    with pytest.raises(CapabilityMismatchError, match="basic chat"):
        run_capability_suite(_raises_input_65_1)


def test_basic_probe_accepts_responses_api_text_content_block() -> None:
    capabilities = run_capability_suite(
        _ProbeModel(basic_content=[{"type": "text", "text": "basic-ok"}])
    )

    assert capabilities == {"chat", "tools", "structured_output"}


def test_tool_replay_accepts_response_containing_probe_value() -> None:
    capabilities = run_capability_suite(
        _ProbeModel(
            basic_content="basic-ok",
            replay_content="The tool returned probe-ok.",
        )
    )

    assert capabilities == {"chat", "tools", "structured_output"}


def test_capability_suite_applies_one_decreasing_deadline_to_every_probe() -> None:
    model = _ProbeModel(basic_content="basic-ok")
    ticks = iter([100.0, 101.0, 101.0, 103.0, 103.0, 106.0, 106.0, 110.0, 110.0, 115.0, 115.0])

    capabilities = run_capability_suite(
        model,
        timeout_seconds=60.0,
        monotonic=lambda: next(ticks),
    )

    assert capabilities == {"chat", "tools", "structured_output"}
    assert model.timeouts == [59.0, 57.0, 54.0, 50.0, 45.0]


def test_capability_suite_stops_before_probe_when_global_deadline_is_exhausted() -> None:
    model = _ProbeModel(basic_content="basic-ok")
    ticks = iter([100.0, 101.0, 161.0])

    def _raises_input_108_1():
        return next(ticks)

    with pytest.raises(TimeoutError, match="capability verification deadline exceeded"):
        run_capability_suite(model, timeout_seconds=60.0, monotonic=_raises_input_108_1)


def test_probe_result_returned_after_global_deadline_is_rejected() -> None:
    model = _ProbeModel(basic_content="basic-ok")
    ticks = iter([100.0, 100.6])

    def _raises_input_120_1():
        return next(ticks)

    with pytest.raises(TimeoutError, match="capability verification deadline exceeded"):
        _invoke_before_deadline(
            model, "Return exactly basic-ok.", deadline=100.5, monotonic=_raises_input_120_1
        )


class _RecordingProbeModel(_ProbeModel):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.requests = []

    def invoke(self, messages):
        self.requests.append(messages)
        return super().invoke(messages)


def test_the_tool_probe_asks_for_the_returned_value_without_naming_it() -> None:
    # gpt-oss models answered the replay turn with "The probe capability has been
    # confirmed." about half the time: the request never asked for the value.
    # It asks now, and the value itself is only in the tool's result, so the
    # check still proves the model read that result.
    model = _RecordingProbeModel(basic_content="basic-ok")

    run_capability_suite(model)

    tool_request = next(request for request in model.requests if isinstance(request, list))
    instruction = tool_request[0].content
    assert "reply with exactly the value it returns" in instruction
    assert "probe-ok" not in instruction


def test_a_summary_without_the_tool_value_still_fails_and_names_the_check() -> None:
    from core.application.settings.llm_capability_verifier import verification_failure_code

    model = _ProbeModel(basic_content="basic-ok", replay_content="The probe has been confirmed.")
    with pytest.raises(CapabilityMismatchError) as caught:
        run_capability_suite(model)

    assert caught.value.probe == "tool_result"
    assert verification_failure_code(caught.value) == "capability_mismatch_tool_result"


def test_a_typographic_hyphen_in_the_tool_value_is_the_same_value() -> None:
    capabilities = run_capability_suite(
        _ProbeModel(basic_content="basic-ok", replay_content="The result is **probe\u2011ok**.")
    )

    assert capabilities == {"chat", "tools", "structured_output"}


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        ("basic", "capability_mismatch_chat"),
        ("structured", "capability_mismatch_structured_output"),
        ("combined", "capability_mismatch_tool_result_structured"),
    ],
)
def test_each_failed_check_has_its_own_code(failure, code) -> None:
    from core.application.settings.llm_capability_verifier import verification_failure_code

    class _Failing(_ProbeModel):
        def with_structured_output(self, schema, **_kwargs):
            if failure == "combined" and "tool_value" in schema["properties"]:
                return _Structured({"status": "ok", "tool_value": "nope"})
            if failure == "structured" and "items" in schema["properties"]:
                return _Structured({"status": "ok", "items": [{"id": 8, "label": "verified"}]})
            return super().with_structured_output(schema)

    model = _Failing(basic_content="nope" if failure == "basic" else "basic-ok")
    with pytest.raises(CapabilityMismatchError) as caught:
        run_capability_suite(model)

    assert verification_failure_code(caught.value) == code


def test_a_contract_refusal_from_the_provider_is_tagged_with_its_check() -> None:
    from core.application.settings.llm_capability_verifier import verification_failure_code

    class _Refusing(_ProbeModel):
        def invoke(self, messages):
            if isinstance(messages, list) and len(messages) == 1:
                raise RuntimeError("extension llm.chat failed: capability_mismatch")
            return super().invoke(messages)

    model = _Refusing(basic_content="basic-ok")
    with pytest.raises(CapabilityMismatchError) as caught:
        run_capability_suite(model)

    assert verification_failure_code(caught.value) == "capability_mismatch_tools"


def test_other_failures_keep_their_own_category() -> None:
    from core.application.settings.llm_capability_verifier import verification_failure_code

    class _Unauthorised(_ProbeModel):
        def invoke(self, messages):
            raise RuntimeError("401 unauthorized")

    model = _Unauthorised(basic_content="basic-ok")
    with pytest.raises(RuntimeError) as caught:
        run_capability_suite(model)

    assert not isinstance(caught.value, CapabilityMismatchError)
    assert verification_failure_code(caught.value) == "authentication_error"
