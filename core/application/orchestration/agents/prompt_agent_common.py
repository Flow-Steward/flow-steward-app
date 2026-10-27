# ruff: noqa: F401, F821
# mypy: ignore-errors
"""
PromptAgent — the sole execution path for all platform agents.

Every agent defined in the registry runs through this class:
- Loads the AgentManifest from the registry YAML
- Uses PromptBuilder to assemble the prompt
- Injects tool configuration context (project_name, task_id)
- Delegates to invoke_agent() from core/application/orchestration/agents/common.py
  for the ReAct LLM loop
- Extracts structured artifacts and verdicts from LLM response
- Returns an AgentOutput

No custom Python per agent — everything is driven by YAML manifests.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import uuid
from collections.abc import Mapping
from typing import Any

from core.application.management.errors import ServiceScopeNotFoundError
from core.application.orchestration.agents.base import AgentInput, AgentOutput, BaseAgent
from core.application.orchestration.agents.common import (
    _resolve_provider,
    build_prompt_safe_state,
    emit_event,
    invoke_agent,
    resolve_security_scope,
)
from core.application.orchestration.agents.prompt_builder import PromptBuilder
from core.application.orchestration.model_response_text import model_response_text
from core.application.orchestration.models import (
    build_phase_result_json_schema,
    normalize_phase_status,
    phase_next_action,
    validate_phase_result_payload,
)
from core.infrastructure.security.prompt_safety import (
    assert_prompt_payload_safe,
    redact_structure_for_persistence,
    redact_text_for_persistence,
)

COMPLETED_PREFIX = "Completed."


logger = logging.getLogger(__name__)

INVALID_PHASE_RESULT_V2_PAYLOAD = "invalid phase_result_v2 payload"
TOOL_CONFIGURATION_HEADER = "## Tool Configuration"
_RESTRICTED_ALLOWED_TOOL_CATEGORIES = frozenset(
    {"analysis", "docs", "knowledge", "quality", "security", "verification"}
)
_RESTRICTED_ALLOWED_TOOL_NAMES: frozenset[str] = frozenset()


class PromptAgentCommonMixin:
    """Common PromptAgent initialization and deterministic/runtime helpers."""

    def __init__(self, manifest, tool_registry=None, skill_registry=None):
        """
        Args:
            manifest: AgentManifest instance loaded from YAML.
            tool_registry: ToolRegistry instance (optional; for tool prompt injection).
            skill_registry: Scoped SkillRegistry required when executing an assigned skill.
        """
        self.manifest = manifest
        self.tool_registry = tool_registry
        self.skill_registry = skill_registry

    @staticmethod
    def _collect_exception_chain(exc: Exception) -> list[object]:
        """Return exception + nested cause/context chain (deduplicated, bounded)."""
        chain: list[object] = []
        seen: set[int] = set()
        queue: list[object] = [exc]
        while queue and len(chain) < 8:
            current = queue.pop(0)
            marker = id(current)
            if marker in seen:
                continue
            seen.add(marker)
            chain.append(current)
            cause = getattr(current, "__cause__", None)
            context = getattr(current, "__context__", None)
            if cause is not None and hasattr(cause, "__traceback__"):
                queue.append(cause)
            if context is not None and hasattr(context, "__traceback__"):
                queue.append(context)
        return chain

    @staticmethod
    def _coerce_project_config(state: dict | None) -> dict[str, Any]:
        raw = (state or {}).get("project_config")
        if isinstance(raw, dict):
            return raw
        model_dump = getattr(raw, "model_dump", None)
        if callable(model_dump):
            try:
                dumped = model_dump()
            except Exception:
                dumped = None
            if isinstance(dumped, dict):
                return dumped
        dict_method = getattr(raw, "dict", None)
        if callable(dict_method):
            try:
                dumped = dict_method()
            except Exception:
                dumped = None
            if isinstance(dumped, dict):
                return dumped
        try:
            coerced = dict(raw or {})
        except Exception:
            return {}
        return coerced if isinstance(coerced, dict) else {}

    @staticmethod
    def _extract_execution_error_metadata(exc: Exception, *, provider_hint: str = "") -> dict:
        """Extract provider/HTTP diagnostics from runtime exception chain."""
        chain = PromptAgent._collect_exception_chain(exc)
        response_candidates = []
        for candidate in chain:
            response = getattr(candidate, "response", None)
            if response is not None:
                response_candidates.append(response)

        def _first_non_empty(*values):
            for value in values:
                if value is None:
                    continue
                if isinstance(value, str):
                    clean = value.strip()
                    if clean:
                        return clean
                    continue
                return value
            return None

        def _first_attr(attr_names: tuple[str, ...]):
            for candidate in chain:
                for attr in attr_names:
                    value = getattr(candidate, attr, None)
                    if value not in (None, ""):
                        return value
            return None

        http_status = None
        status_candidates = []
        for candidate in chain:
            status_candidates.extend(
                [
                    getattr(candidate, "status_code", None),
                    getattr(candidate, "http_status", None),
                ]
            )
        for response in response_candidates:
            status_candidates.append(getattr(response, "status_code", None))
        for value in status_candidates:
            if value in (None, ""):
                continue
            if isinstance(value, bool):
                continue
            try:
                parsed = int(str(value))
            except (TypeError, ValueError):
                continue
            if 100 <= parsed <= 599:
                http_status = parsed
                break

        request_id = _first_non_empty(
            _first_attr(("request_id", "x_request_id")),
        )
        if not request_id:
            for response in response_candidates:
                headers = getattr(response, "headers", None)
                if not headers:
                    continue
                getter = getattr(headers, "get", None)
                if not callable(getter):
                    continue
                for key in ("x-request-id", "request-id", "openai-request-id"):
                    value = getter(key)
                    if value and str(value).strip():
                        request_id = str(value).strip()
                        break
                if request_id:
                    break

        provider_name = _first_non_empty(
            _first_attr(("provider_name", "provider")),
            provider_hint,
        )
        if not provider_name:
            for candidate in chain:
                module_name = str(getattr(candidate.__class__, "__module__", "") or "").lower()
                if "openai" in module_name:
                    provider_name = "openai"
                    break
                if "anthropic" in module_name:
                    provider_name = "anthropic"
                    break
                if "google" in module_name or "gemini" in module_name:
                    provider_name = "google"
                    break

        provider_error_code = _first_non_empty(_first_attr(("code", "error_code")))
        provider_error_type = _first_non_empty(
            _first_attr(("type", "error_type")),
            exc.__class__.__name__,
        )

        response_excerpt = None
        for response in response_candidates:
            text = getattr(response, "text", None)
            if text:
                response_excerpt = PromptAgent._truncate_text(str(text), 4000)
                break
            body = getattr(response, "body", None)
            if body:
                response_excerpt = PromptAgent._truncate_text(str(body), 4000)
                break

        retryable_value = _first_attr(("retryable",))
        retryable = retryable_value if isinstance(retryable_value, bool) else None
        if retryable is None and http_status is not None:
            retryable = http_status == 429 or http_status >= 500

        return {
            "provider_name": provider_name,
            "http_status": http_status,
            "provider_error_code": provider_error_code,
            "provider_error_type": provider_error_type,
            "request_id": request_id,
            "retryable": retryable,
            "response_excerpt": response_excerpt,
        }

    @staticmethod
    def _format_execution_error(exc: Exception, diagnostics: dict | None = None) -> str:
        """Render exception text with chained cause/context and provider/http details."""
        head = str(exc).strip() or exc.__class__.__name__
        diagnostics = diagnostics or {}
        details: list[str] = []
        if diagnostics.get("provider_name"):
            details.append(f"provider={diagnostics['provider_name']}")
        if diagnostics.get("http_status") is not None:
            details.append(f"http_status={diagnostics['http_status']}")
        if diagnostics.get("provider_error_code"):
            details.append(f"provider_code={diagnostics['provider_error_code']}")
        if diagnostics.get("provider_error_type"):
            details.append(f"provider_type={diagnostics['provider_error_type']}")
        if diagnostics.get("request_id"):
            details.append(f"request_id={diagnostics['request_id']}")
        if details:
            head = f"{head} | {' '.join(details)}"

        cause = exc.__cause__ or exc.__context__
        if not cause:
            return head
        cause_text = str(cause).strip() or cause.__class__.__name__
        return f"{head} | cause={cause.__class__.__name__}: {cause_text}"


__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
