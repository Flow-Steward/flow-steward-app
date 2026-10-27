"""LangChain chat-model adapter for LLM provider extensions.

`get_chat_model()` returns an `ExtensionChatModel` for extension provider keys
(`llmext_*`). The provider runs in its install's own runtime (an extension
subprocess, or a remote HTTPS runtime where one is configured): chat goes through
the source's `chat_action_id` (`llm.chat`) and structured output through
`structured_chat_action_id` (`llm.chat_structured`) when declared, else
`chat_action_id` with an embedded JSON schema. The adapter exposes the
`invoke()` + `with_structured_output()` surface the agent runtime
depends on. There is no silent fallback to another provider: a missing/unhealthy
install or an unsupported structured mode raises `ExtensionLLMError`.
"""

from __future__ import annotations

import copy
import json
import math
import uuid
from collections.abc import Sequence
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from pydantic import Field

_DEFAULT_MAX_TOKENS = 4096
_MAX_CONTINUATION_METADATA_BYTES = 16 * 1024
_RETRYABLE_FAILURES = frozenset({"rate_limited", "timeout", "upstream_error"})
_ROLE_BY_TYPE = {
    "human": "user",
    "ai": "assistant",
    "system": "system",
    "tool": "tool",
    "function": "tool",
}


class ExtensionLLMError(RuntimeError):
    """Raised when an extension-backed LLM call cannot be completed."""


def _normalize_tool(tool: Any) -> dict[str, Any]:
    """Convert supported host/LangChain tools to canonical function wire."""
    if isinstance(tool, dict) and "name" in tool and "input_schema" in tool:
        function: dict[str, Any] = {
            "name": str(tool.get("name") or "").strip(),
            "parameters": copy.deepcopy(tool.get("input_schema")),
        }
        if tool.get("description") is not None:
            function["description"] = str(tool.get("description") or "")
        normalized: dict[str, Any] = {"type": "function", "function": function}
    else:
        from langchain_core.utils.function_calling import convert_to_openai_tool

        normalized = copy.deepcopy(convert_to_openai_tool(tool))

    function = normalized.get("function") if isinstance(normalized, dict) else None
    if (
        normalized.get("type") != "function"
        or not isinstance(function, dict)
        or not str(function.get("name") or "").strip()
        or not isinstance(function.get("parameters"), dict)
    ):
        raise ValueError("tool must define a function name and JSON Schema parameters")
    return normalized


def _message_role(message: BaseMessage) -> str:
    return _ROLE_BY_TYPE.get(getattr(message, "type", "") or "", "user")


def _message_content(message: BaseMessage) -> Any:
    """Keep provider content blocks structured on the canonical wire."""
    return getattr(message, "content", "")


def _wire_tool_calls(message: BaseMessage) -> list[dict[str, Any]]:
    """Normalize LangChain tool calls without serializing their arguments."""
    calls = getattr(message, "tool_calls", None)
    if not isinstance(calls, list):
        calls = (getattr(message, "additional_kwargs", {}) or {}).get("tool_calls")
    if not isinstance(calls, list):
        return []
    normalized: list[dict[str, Any]] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        call_id = str(call.get("id") or "").strip()
        name = str(call.get("name") or (call.get("function") or {}).get("name") or "").strip()
        arguments = call.get("args", (call.get("function") or {}).get("arguments"))
        if not call_id or not name or arguments is None:
            continue
        normalized.append({"id": call_id, "name": name, "arguments": arguments})
    return normalized


def _bounded_continuation(value: Any) -> dict[str, Any] | None:
    """Pass opaque provider state only when it is a small JSON object."""
    if not isinstance(value, dict):
        return None
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        return None
    if len(encoded) > _MAX_CONTINUATION_METADATA_BYTES:
        return None
    return value


def _messages_to_wire(messages: list[BaseMessage]) -> list[dict[str, Any]]:
    wire: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, BaseMessage):
            continue
        item: dict[str, Any] = {
            "role": _message_role(message),
            "content": _message_content(message),
        }
        name = str(getattr(message, "name", "") or "").strip()
        if name:
            item["name"] = name
        tool_calls = _wire_tool_calls(message)
        if tool_calls:
            item["tool_calls"] = tool_calls
        tool_call_id = str(getattr(message, "tool_call_id", "") or "").strip()
        if tool_call_id:
            item["tool_call_id"] = tool_call_id
        additional = getattr(message, "additional_kwargs", {}) or {}
        cache_control = additional.get("cache_control") if isinstance(additional, dict) else None
        if isinstance(cache_control, (dict, list)):
            item["cache_control"] = cache_control
        continuation = _bounded_continuation(
            (getattr(message, "response_metadata", {}) or {}).get("continuation")
        )
        if continuation is not None:
            item["continuation"] = continuation
        wire.append(item)
    return wire


def _normalize_schema(schema: Any, schema_name_hint: str = "") -> tuple[str, dict[str, Any], Any]:
    """Return a canonical JSON Schema plus an optional host validator."""
    model_json_schema = getattr(schema, "model_json_schema", None)
    if callable(model_json_schema):
        return (getattr(schema, "__name__", "Schema"), model_json_schema(), schema)
    # langchain TypedDict / plain dict schema
    if isinstance(schema, dict):
        name = schema_name_hint or str(schema.get("title") or "Schema")
        return (name, schema, None)
    try:
        from pydantic import TypeAdapter

        adapter = TypeAdapter(schema)
        json_schema = adapter.json_schema()
    except Exception as exc:
        raise ValueError("unsupported structured output schema") from exc
    if not isinstance(json_schema, dict) or not json_schema:
        raise ValueError("structured output schema normalized to an empty schema")
    return (
        schema_name_hint or getattr(schema, "__name__", "Schema"),
        json_schema,
        adapter,
    )


class ExtensionChatModel(BaseChatModel):
    """Chat model backed by an LLM provider extension."""

    account_id: str
    provider_key: str
    model_id: str
    chat_action_id: str = "llm.chat"
    structured_chat_action_id: str | None = None
    extension_install_id: str = ""
    extension_id: str = ""
    verification_generation: str = ""
    temperature: float | None = None
    max_tokens: int | None = None
    request_timeout: float | None = None
    max_retries: int | None = None
    supports_prompt_caching: bool = False
    bound_tools: list[dict[str, Any]] = Field(default_factory=list)
    bound_tool_choice: Any = None
    bound_parallel_tool_calls: bool | None = None
    bound_structured_schema: dict[str, Any] | None = None
    bound_structured_schema_name: str = ""
    bound_structured_strict: bool = True
    bound_structured_metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def _llm_type(self) -> str:
        return "extension_llm"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"provider_key": self.provider_key, "model_id": self.model_id}

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Any],
        *,
        tool_choice: Any = None,
        **kwargs: Any,
    ) -> ExtensionChatModel:
        """Return a model copy that carries canonical tool schemas to the runtime."""
        normalized = [_normalize_tool(tool) for tool in tools]
        parallel = kwargs.get("parallel_tool_calls")
        return self.model_copy(
            update={
                "bound_tools": normalized,
                "bound_tool_choice": tool_choice,
                "bound_parallel_tool_calls": bool(parallel) if parallel is not None else None,
            }
        )

    # ----- chat ----------------------------------------------------------- #

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        payload = {
            "model_id": self.model_id,
            "messages": _messages_to_wire(messages),
            "max_tokens": int(self.max_tokens or _DEFAULT_MAX_TOKENS),
            "metadata": {
                **self.bound_structured_metadata,
                **dict(kwargs.get("metadata") or {}),
            },
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.bound_tools:
            payload["tools"] = self.bound_tools
        if self.bound_tool_choice is not None:
            payload["tool_choice"] = self.bound_tool_choice
        if self.bound_parallel_tool_calls is not None:
            payload["parallel_tool_calls"] = self.bound_parallel_tool_calls
        action_id = self.chat_action_id
        if self.bound_structured_schema is not None:
            payload["schema"] = self.bound_structured_schema
            payload["schema_name"] = self.bound_structured_schema_name
            payload["strict"] = self.bound_structured_strict
            action_id = self.structured_chat_action_id or self.chat_action_id
        result = self._invoke_operation(
            op_kind="action",
            op_id=action_id,
            payload=payload,
            timeout_seconds=kwargs.get("timeout"),
        )
        if self.bound_structured_schema is not None and (
            not isinstance(result, dict) or "structured_output" not in result
        ):
            raise ExtensionLLMError(
                f"extension provider '{self.provider_key}' did not return structured_output "
                f"for model '{self.model_id}'"
            )
        message = result.get("message") if isinstance(result, dict) else None
        message = message if isinstance(message, dict) else {}
        response_metadata = {
            "finish_reason": str((result or {}).get("finish_reason") or ""),
            "model_name": self.model_id,
            "provider_key": self.provider_key,
        }
        continuation = _bounded_continuation(message.get("continuation"))
        if continuation is not None:
            response_metadata["continuation"] = continuation
        ai_message = AIMessage(
            content=message.get("content") if message.get("content") is not None else "",
            tool_calls=_response_tool_calls(message.get("tool_calls")),
            additional_kwargs=(
                {"_flow_steward_structured_output": result.get("structured_output")}
                if self.bound_structured_schema is not None and isinstance(result, dict)
                else {}
            ),
            response_metadata=response_metadata,
            usage_metadata=_usage_metadata((result or {}).get("usage")),
        )
        llm_output: dict[str, Any] = {
            "model_name": self.model_id,
            "provider_key": self.provider_key,
        }
        reported_cost = _reported_cost_usd((result or {}).get("usage"))
        if reported_cost is not None:
            llm_output["cost_usd"] = reported_cost
        return ChatResult(
            generations=[ChatGeneration(message=ai_message)],
            llm_output=llm_output,
        )

    # ----- structured output --------------------------------------------- #

    def with_structured_output(
        self,
        schema: Any,
        *,
        include_raw: bool = False,
        **kwargs: Any,
    ) -> Runnable:
        schema_name, json_schema, pydantic_cls = _normalize_schema(
            schema, str(kwargs.get("schema_name") or "")
        )
        structured_model = self.model_copy(
            update={
                "bound_structured_schema": json_schema,
                "bound_structured_schema_name": schema_name,
                "bound_structured_strict": bool(kwargs.get("strict", True)),
                "bound_structured_metadata": dict(kwargs.get("metadata") or {}),
            }
        )

        def _parse_message(raw: AIMessage) -> Any:
            try:
                parsed = _parse_structured(
                    raw.additional_kwargs.get("_flow_steward_structured_output"),
                    pydantic_cls,
                    json_schema,
                )
            except Exception as exc:
                if include_raw:
                    return {"raw": raw, "parsed": None, "parsing_error": exc}
                raise
            if include_raw:
                return {"raw": raw, "parsed": parsed, "parsing_error": None}
            return parsed

        return structured_model | RunnableLambda(_parse_message)

    # ----- extension runtime ---------------------------------------------- #

    def _runtime_extension(self) -> dict[str, Any]:
        from core.infrastructure.db.platform_stores import PlatformIdentityStore

        get_shared_instance = getattr(PlatformIdentityStore, "get_shared_instance", None)

        def _get_logger_only():
            return get_shared_instance() if callable(get_shared_instance) else None

        lookup_id = self.extension_install_id or self.extension_id
        bl = _get_logger_only()
        if bl is None or not hasattr(bl, "conn"):
            raise ExtensionLLMError("model_unavailable: extension registry is unavailable")
        from core.application.settings import llm_provider_extension_repository as store

        snapshot = store.get_model_runtime_snapshot(
            bl.conn,
            account_id=self.account_id,
            extension_install_id=lookup_id,
            provider_key=self.provider_key,
            model_id=self.model_id,
            verification_generation=self.verification_generation,
        )
        if not snapshot:
            raise ExtensionLLMError(
                f"model_unavailable: extension install '{lookup_id}' is unavailable for account"
            )
        if (
            str(snapshot.get("state") or "").lower() != "active"
            or str(snapshot.get("handshake_status") or "").lower() == "failed"
            or str(snapshot.get("health_status") or "").lower() == "failed"
            or str(snapshot.get("runtime_status") or "").lower() == "incompatible"
        ):
            raise ExtensionLLMError(
                f"model_unavailable: extension provider '{self.provider_key}' is unhealthy"
            )
        descriptor = snapshot.get("descriptor_jsonb")
        descriptor = dict(descriptor) if isinstance(descriptor, dict) else {}
        if (
            str(descriptor.get("extension_id") or "").strip()
            != str(snapshot.get("extension_id") or "").strip()
        ):
            raise ExtensionLLMError("model_unavailable: extension release identity is invalid")
        extension = {
            **descriptor,
            "extension_key": str(snapshot.get("extension_id") or "").strip(),
            "extension_id": str(snapshot.get("extension_id") or "").strip(),
            "install_id": str(snapshot.get("install_id") or "").strip(),
            "release_id": str(snapshot.get("release_id") or "").strip(),
            "runtime_mode": str(snapshot.get("runtime_mode") or "").strip(),
            "remote_base_url": str(snapshot.get("remote_base_url") or "").strip(),
            "handshake_status": str(snapshot.get("handshake_status") or "").strip(),
            "health_status": str(snapshot.get("health_status") or "").strip(),
            "runtime_status": str(snapshot.get("runtime_status") or "").strip(),
            "manifest": descriptor,
            "compiled_manifest": descriptor,
            "entrypoint": descriptor.get("entrypoint"),
            "action_definitions": descriptor.get("action_definitions"),
            "query_definitions": descriptor.get("query_definitions"),
        }
        credentials: dict[str, Any] = {}
        ciphertext = str(snapshot.get("encrypted_value") or "")
        if ciphertext:
            from core.application.auth.secrets import decrypt_secret

            try:
                parsed = json.loads(decrypt_secret(ciphertext))
            except Exception as exc:
                raise ExtensionLLMError(
                    f"authentication_error: invalid credentials for extension provider "
                    f"'{self.provider_key}'"
                ) from exc
            if not isinstance(parsed, dict):
                raise ExtensionLLMError(
                    f"authentication_error: invalid credentials for extension provider "
                    f"'{self.provider_key}'"
                )
            credentials = parsed
        # The invocation layer separates a remote runtime's own bearer from the
        # provider's secrets; both travel here in one encrypted payload.
        connection_config = snapshot.get("connection_config_jsonb")
        return {
            **extension,
            "llm_credentials": credentials,
            "llm_connection_config": (
                dict(connection_config) if isinstance(connection_config, dict) else {}
            ),
        }

    def _invoke_operation(
        self,
        *,
        op_kind: str,
        op_id: str,
        payload: dict[str, Any],
        timeout_seconds: Any = None,
    ) -> dict[str, Any]:
        from core.application.settings.llm_provider_extension_service import (
            invoke_llm_provider_operation,
        )
        from core.infrastructure.db.platform_stores import PlatformIdentityStore

        extension = self._runtime_extension()
        credentials = extension.pop("llm_credentials", {})
        connection_config = extension.pop("llm_connection_config", {})
        get_logger_only = getattr(PlatformIdentityStore, "get_shared_instance", None)
        try:
            request_timeout = float(timeout_seconds)
        except (TypeError, ValueError):
            request_timeout = float(self.request_timeout or 0)
        effective_timeout = request_timeout if request_timeout > 0 else None
        attempts = max(1, int(self.max_retries or 0) + 1)
        idempotency_key = f"llm-{uuid.uuid4().hex}" if op_kind == "action" else ""
        last_detail = ""
        for attempt in range(attempts):
            invocation = invoke_llm_provider_operation(
                extension,
                op_kind=op_kind,
                op_id=op_id,
                payload=payload,
                account_id=self.account_id,
                credentials=credentials,
                connection_config=connection_config,
                timeout_seconds=effective_timeout,
                idempotency_key=idempotency_key,
                get_logger_only=get_logger_only if callable(get_logger_only) else None,
            )
            if invocation.get("status") == "succeeded":
                response = invocation.get("response")
                if not isinstance(response, dict):
                    raise ExtensionLLMError(
                        f"invalid_response: extension provider '{self.provider_key}' returned "
                        "a malformed response"
                    )
                result = response.get("result")
                if not isinstance(result, dict):
                    raise ExtensionLLMError(
                        f"invalid_response: extension provider '{self.provider_key}' returned "
                        "a malformed result"
                    )
                return result
            last_detail = str(invocation.get("detail") or "extension LLM call failed")
            if last_detail not in _RETRYABLE_FAILURES or attempt + 1 >= attempts:
                break
        raise ExtensionLLMError(
            f"extension provider '{self.provider_key}' call to '{op_id}' failed: {last_detail}"
        )


def _usage_metadata(usage: Any) -> dict[str, Any] | None:
    if not isinstance(usage, dict):
        return None
    try:
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or (input_tokens + output_tokens))
    except (TypeError, ValueError):
        return None
    cache_creation = usage.get("cache_creation_input_tokens", usage.get("cache_creation_tokens", 0))
    cache_read = usage.get("cache_read_input_tokens", usage.get("cache_read_tokens", 0))
    try:
        cache_creation = int(cache_creation or 0)
        cache_read = int(cache_read or 0)
    except (TypeError, ValueError):
        cache_creation = cache_read = 0
    metadata: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }
    if cache_creation or cache_read:
        metadata["cache_creation_input_tokens"] = cache_creation
        metadata["cache_read_input_tokens"] = cache_read
        metadata["input_token_details"] = {
            "cache_creation": cache_creation,
            "cache_read": cache_read,
        }
    return metadata


def _reported_cost_usd(usage: Any) -> float | None:
    if not isinstance(usage, dict):
        return None
    value = usage.get("cost_usd")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    normalized = float(value)
    return normalized if math.isfinite(normalized) and normalized >= 0 else None


def _response_tool_calls(value: Any) -> list[dict[str, Any]]:
    """Validate and normalize runtime tool calls for LangChain's AIMessage."""
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    for call in value:
        if not isinstance(call, dict):
            raise ExtensionLLMError("extension provider returned an invalid tool call")
        call_id = str(call.get("id") or "").strip()
        name = str(call.get("name") or (call.get("function") or {}).get("name") or "").strip()
        arguments = call.get("arguments", (call.get("function") or {}).get("arguments"))
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise ExtensionLLMError(
                    "extension provider returned invalid tool arguments"
                ) from exc
        if not call_id or not name or not isinstance(arguments, dict):
            raise ExtensionLLMError("extension provider returned an incomplete tool call")
        normalized.append({"id": call_id, "name": name, "args": arguments})
    return normalized


def _parse_structured(value: Any, schema_validator: Any, json_schema: dict[str, Any]) -> Any:
    """Parse then validate against the exact canonical schema without repair."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception as exc:
            raise ExtensionLLMError(f"structured_output is not valid JSON: {exc}") from exc
    try:
        from jsonschema import Draft202012Validator

        Draft202012Validator(json_schema).validate(value)
    except ExtensionLLMError:
        raise
    except Exception as exc:
        raise ExtensionLLMError(
            f"structured_output did not match canonical JSON Schema: {exc}"
        ) from exc
    if schema_validator is not None:
        try:
            model_validate = getattr(schema_validator, "model_validate", None)
            if callable(model_validate):
                return model_validate(value)
            validate_python = getattr(schema_validator, "validate_python", None)
            if callable(validate_python):
                return validate_python(value)
            if isinstance(value, dict):
                return schema_validator(**value)
        except Exception as exc:
            raise ExtensionLLMError(
                "structured_output did not match host schema validator: " + str(exc)
            ) from exc
    return value
