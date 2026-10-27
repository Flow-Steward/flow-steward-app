"""Opt-in smoke contract for deployed reference LLM provider runtimes.

The runtime URL and upstream credentials are deliberately supplied by the
operator.  These tests never run in ordinary CI and do not accept localhost or
private endpoints through the application installation flow.
"""

from __future__ import annotations

import os

import pytest

from core.application.orchestration.extension_chat_model import ExtensionChatModel
from core.application.settings.llm_capability_verifier import run_capability_suite
from core.infrastructure.db.remote_extension_runtime import remote_query


def _live_enabled() -> bool:
    return str(os.environ.get("FS_RUN_LIVE_PROVIDER_TESTS") or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


@pytest.mark.parametrize(
    (
        "name",
        "runtime_url_env",
        "runtime_token_env",
        "upstream_key_env",
        "upstream_url_env",
        "model_env",
    ),
    [
        (
            "aggregator",
            "FS_LIVE_LLM_AGGREGATOR_RUNTIME_URL",
            "FS_LIVE_LLM_AGGREGATOR_RUNTIME_TOKEN",
            "FS_LIVE_LLM_AGGREGATOR_API_KEY",
            "",
            "FS_LIVE_LLM_AGGREGATOR_MODEL",
        ),
        (
            "endpoint",
            "FS_LIVE_LLM_ENDPOINT_RUNTIME_URL",
            "FS_LIVE_LLM_ENDPOINT_RUNTIME_TOKEN",
            "FS_LIVE_LLM_ENDPOINT_BEARER_TOKEN",
            "FS_LIVE_LLM_ENDPOINT_UPSTREAM_BASE_URL",
            "FS_LIVE_LLM_ENDPOINT_MODEL",
        ),
    ],
)
def test_live_reference_extension_passes_enablement_capability_suite(
    name: str,
    runtime_url_env: str,
    runtime_token_env: str,
    upstream_key_env: str,
    upstream_url_env: str,
    model_env: str,
) -> None:
    if not _live_enabled():
        pytest.skip("set FS_RUN_LIVE_PROVIDER_TESTS=1 to run extension capability audit")
    runtime_url = str(os.environ.get(runtime_url_env) or "").strip()
    runtime_token = str(os.environ.get(runtime_token_env) or "").strip()
    model_id = str(os.environ.get(model_env) or "").strip()
    if not runtime_url or not runtime_token or not model_id:
        pytest.skip(f"{name} runtime URL, token, and model are required")
    upstream_url = str(os.environ.get(upstream_url_env) or "").strip()
    if name == "endpoint" and not upstream_url:
        pytest.skip("FS_LIVE_LLM_ENDPOINT_UPSTREAM_BASE_URL is required")

    credentials = {"api_key": str(os.environ.get(upstream_key_env) or "").strip()}
    if name == "endpoint":
        credentials = {"bearer_token": credentials["api_key"]} if credentials["api_key"] else {}
    extension = {
        "runtime_mode": "remote_https",
        "remote_base_url": runtime_url,
        "remote_bearer_token": runtime_token,
        "connection_credentials": credentials,
        "connection_config": {"upstream_base_url": upstream_url} if upstream_url else {},
    }
    context = {"account_id": "live-provider-audit"}

    discovery = remote_query(
        extension,
        query_id="llm.list_models",
        query_payload={"cursor": "", "limit": 1},
        context=context,
        runtime_context=context,
    )
    assert discovery["status"] == "succeeded", discovery.get("detail")

    class _LiveExtensionChatModel(ExtensionChatModel):
        def _runtime_extension(self) -> dict:
            return extension

    adapter = _LiveExtensionChatModel(
        account_id="live-provider-audit",
        provider_key=f"llmext_{name}_live",
        model_id=model_id,
        chat_action_id="llm.chat",
        structured_chat_action_id="llm.chat_structured",
        request_timeout=60,
    )
    # This is the exact gate used by enablement: chat, tool call, tool-result
    # replay, strict JSON schema, and their combined flow without repair.
    assert run_capability_suite(adapter) == {"chat", "tools", "structured_output"}
