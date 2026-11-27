import json

import httpx
import pytest
from langchain_anthropic import chat_models

from core.application.orchestration import model_provider
from core.application.settings.llm_capability_verifier import run_capability_suite


def _saved_claude(model_id):
    config = {
        "account_id": "test-account",
        "agents_config": {
            "default_provider": "custom:test-provider",
            "default_model": model_id,
            "model_provider_id": "test-provider",
            "max_retries": 0,
            "default_temperature": 0.2,
        },
    }
    test_credential = "fixture"
    saved = {
        "id": "test-provider",
        "provider_type": "anthropic",
        "default_model": model_id,
        "_decrypted_api_key": test_credential,
    }
    return model_provider.get_chat_model(
        config, allow_unverified_custom=True, claimed_custom_provider=saved
    )


def _message(model_id, content, *, stop_reason="end_turn"):
    return httpx.Response(
        200,
        json={
            "id": "msg_fixture",
            "type": "message",
            "role": "assistant",
            "model": model_id,
            "content": content,
            "stop_reason": stop_reason,
            "usage": {"input_tokens": 8, "output_tokens": 3},
        },
    )


@pytest.mark.parametrize(
    ("model_id", "supports_temperature"),
    [
        pytest.param("claude-haiku-5-5", False, id="haiku-5-5"),
        pytest.param("claude-sonnet-5-5", False, id="sonnet-5-5"),
        pytest.param("claude-opus-4-7", False, id="opus-4-7"),
        pytest.param("claude-mythos-preview", False, id="mythos-preview"),
        pytest.param("claude-sonnet-4-6", True, id="sonnet-4-6"),
        pytest.param("claude-haiku-4-5-20251001", True, id="haiku-4-5-snapshot"),
    ],
)
def test_anthropic_sends_temperature_only_to_models_that_support_it(
    monkeypatch, model_id, supports_temperature
):
    payloads = []

    def record_request(request):
        payloads.append(json.loads(request.content))
        return _message(model_id, [{"type": "text", "text": "basic-ok"}])

    with httpx.Client(transport=httpx.MockTransport(record_request)) as http_client:
        monkeypatch.setattr(chat_models, "_get_default_httpx_client", lambda **_kwargs: http_client)
        model = _saved_claude(model_id)
        assert model.invoke("Return exactly basic-ok.").content == "basic-ok"

    [payload] = payloads
    assert payload["model"] == model_id
    if supports_temperature:
        assert payload["temperature"] == 0.2
    else:
        assert "temperature" not in payload


@pytest.mark.parametrize("model_id", ["claude-haiku-5-5", "claude-sonnet-5-5"])
def test_anthropic_capability_suite_uses_native_tools_and_json_schema(monkeypatch, model_id):
    tool_value = {"status": "ok", "tool_value": "probe-ok"}
    thinking = {"type": "thinking", "thinking": "", "signature": "fixture-signature"}
    replies = iter(
        [
            [thinking, {"type": "text", "text": "basic-ok"}],
            [
                thinking,
                {
                    "type": "tool_use",
                    "id": "toolu_fixture",
                    "name": "flow_steward_capability_probe",
                    "input": {"key": "probe"},
                },
            ],
            [{"type": "text", "text": "probe-ok"}],
            [{"type": "text", "text": '{"status":"ok","items":[{"id":7,"label":"verified"}]}'}],
            [thinking, {"type": "text", "text": json.dumps(tool_value)}],
        ]
    )
    payloads = []

    def record_request(request):
        assert request.url.path == "/v1/messages"
        payload = json.loads(request.content)
        assert "temperature" not in payload
        payloads.append(payload)
        stop_reason = "tool_use" if len(payloads) == 2 else "end_turn"
        return _message(model_id, next(replies), stop_reason=stop_reason)

    with httpx.Client(transport=httpx.MockTransport(record_request)) as http_client:
        monkeypatch.setattr(chat_models, "_get_default_httpx_client", lambda **_kwargs: http_client)
        assert run_capability_suite(_saved_claude(model_id), provider_type="anthropic") == {
            "chat",
            "tools",
            "structured_output",
        }

    _, tool_request, replay, structured, combined = payloads
    assert tool_request["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert tool_request["tools"][0]["strict"] is True
    assert replay["tools"] == combined["tools"] == tool_request["tools"]
    assert replay["tool_choice"] == combined["tool_choice"] == {"type": "none"}
    for request in (replay, combined):
        assert thinking in request["messages"][1]["content"]
        assert request["messages"][2]["content"][0]["type"] == "tool_result"
    for request in (structured, combined):
        assert request["output_config"]["format"]["type"] == "json_schema"
