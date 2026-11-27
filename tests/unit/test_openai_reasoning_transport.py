import json

import httpx
import pytest
from langchain_openai import ChatOpenAI
from openai import BadRequestError

from core.application.orchestration import model_provider


@pytest.mark.parametrize("model_id", ["gpt-6.2-sol", "gpt-6.1-terra", "gpt-6-luna"])
def test_saved_openai_reasoning_model_sends_tools_to_responses(monkeypatch, model_id):
    requests = []

    def record_request(request):
        requests.append((request.url.path, json.loads(request.content)))
        return httpx.Response(400, json={"error": {"message": "end of transport test"}})

    def chat_openai(**kwargs):
        return ChatOpenAI(http_client=http_client, **kwargs)

    config = {
        "account_id": "test-account",
        "agents_config": {
            "default_provider": "custom:test-provider",
            "default_model": model_id,
            "model_provider_id": "test-provider",
            "max_retries": 0,
        },
    }
    test_credential = "fixture"
    saved = {
        "id": "test-provider",
        "provider_type": "openai",
        "default_model": model_id,
        "_decrypted_api_key": test_credential,
    }
    tool = {
        "type": "function",
        "function": {"name": "probe", "parameters": {"type": "object", "properties": {}}},
    }

    with httpx.Client(transport=httpx.MockTransport(record_request)) as http_client:
        monkeypatch.setattr(model_provider, "ChatOpenAI", chat_openai)
        model = model_provider.get_chat_model(
            config, allow_unverified_custom=True, claimed_custom_provider=saved
        )
        with pytest.raises(BadRequestError, match="end of transport test"):
            model.bind_tools([tool], tool_choice="required").invoke("Call the probe tool.")

    [(path, payload)] = requests
    assert path == "/v1/responses"
    assert payload["model"] == model_id
    assert payload["tool_choice"] == "required"
    assert payload["tools"][0]["name"] == "probe"
    assert payload["reasoning"] == {"effort": "medium"}
    assert "temperature" not in payload
