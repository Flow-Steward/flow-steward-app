# ruff: noqa: F405, I001, RUF100
# mypy: ignore-errors
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from .prompt_agent_execute import *  # noqa: F403,F401  # NOSONAR


class PromptAgentTraceMixin:
    """Prompt tracing, redaction, and prompt guards."""

    def _log_prompt_request(
        self,
        state: dict,
        *,
        skill: str,
        prompt_version: str,
        provider: str,
        correlation_id: str,
        caused_by: list[dict],
        system_prompt: str,
        user_prompt: str,
    ) -> None:
        self._log_prompt_trace(
            state,
            action_type="prompt_request",
            skill=skill,
            provider=provider,
            correlation_id=correlation_id,
            payload={
                "prompt_version": str(prompt_version or ""),
                "system_prompt": self._truncate_text(system_prompt, 12000),
                "user_prompt": self._truncate_text(user_prompt, 24000),
                "raw_prompt_ref": self._build_raw_prompt_ref(system_prompt, user_prompt),
                "caused_by": [dict(x) for x in (caused_by or []) if isinstance(x, dict)],
            },
            result={
                "system_prompt_chars": len(system_prompt or ""),
                "user_prompt_chars": len(user_prompt or ""),
            },
        )

    def _log_prompt_response(
        self,
        state: dict,
        *,
        skill: str,
        provider: str,
        correlation_id: str,
        response_text: str,
        error: str = "",
    ) -> None:
        status = "error" if error else "done"
        self._log_prompt_trace(
            state,
            action_type="prompt_response",
            skill=skill,
            provider=provider,
            correlation_id=correlation_id,
            payload={
                "output_ref": f"job://{state.get('job_id')}/last_response_excerpt"
                if state.get("job_id")
                else "",
            },
            result={
                "excerpt": self._truncate_text(response_text, 4000),
                "content_chars": len(response_text or ""),
            },
            status=status,
            error=self._truncate_text(error, 2000),
        )

    def _log_prompt_trace(
        self,
        state: dict,
        *,
        action_type: str,
        skill: str,
        provider: str,
        correlation_id: str,
        payload: dict | None = None,
        result: dict | None = None,
        status: str = "done",
        error: str = "",
    ) -> None:
        """Persist prompt/response payloads in action log for replay/debug."""
        try:
            from core.application.orchestration.agents.common import (
                _get_event_logger,
                _resolve_observability_dsn,
            )
        except Exception:
            return
        try:
            dsn = _resolve_observability_dsn()
            if not dsn:
                return
            bl = _get_event_logger(dsn)
            if bl is None:
                return
            project_id, job_id = resolve_security_scope(state)
            if project_id is None:
                return
            task_id = str(state.get("task_id") or "")
            task_summary = str(state.get("task_summary") or "")
            meta = {
                "provider": str(provider or ""),
                "skill": str(skill or ""),
                "correlation_id": str(correlation_id or ""),
                "step_id": str(state.get("step_id") or ""),
            }
            safe_payload = self._redact_prompt_trace_payload(
                payload or {},
                project_id=project_id,
                job_id=job_id,
                context_type="prompt",
            )
            bl.log_action(
                project_id=project_id,
                task_id=task_id,
                task_summary=task_summary,
                agent=self.manifest.agent_id,
                action_type=action_type,
                status=status,
                payload=safe_payload,
                result=redact_structure_for_persistence(
                    result or {},
                    project_id=project_id,
                    job_id=job_id,
                    context_type="display",
                ),
                error=(
                    redact_text_for_persistence(
                        error,
                        project_id=project_id,
                        job_id=job_id,
                        context_type="display",
                    )
                    if error
                    else None
                ),
                meta=meta,
                job_id=job_id,
            )
        except Exception as e:
            logger.debug("Prompt trace log failed (non-fatal): %s", e)

    @staticmethod
    def _redact_prompt_trace_payload(
        payload: dict,
        *,
        project_id: int | str | None,
        job_id: int | str | None,
        context_type: str,
    ) -> dict:
        safe = redact_structure_for_persistence(
            payload,
            project_id=project_id,
            job_id=job_id,
            context_type=context_type,
        )
        if not isinstance(safe, dict):
            return {}
        raw_user_prompt = payload.get("user_prompt") if isinstance(payload, dict) else None
        if isinstance(raw_user_prompt, str) and TOOL_CONFIGURATION_HEADER in raw_user_prompt:
            safe["user_prompt"] = PromptAgent._redact_prompt_text_preserving_tool_config(
                raw_user_prompt,
                project_id=project_id,
                job_id=job_id,
                context_type=context_type,
            )
        return safe

    @staticmethod
    def _redact_prompt_text_preserving_tool_config(
        text: str,
        *,
        project_id: int | str | None,
        job_id: int | str | None,
        context_type: str,
    ) -> str:
        prefix, separator, suffix = str(text or "").rpartition(f"\n\n{TOOL_CONFIGURATION_HEADER}")
        if not separator:
            return redact_text_for_persistence(
                text,
                project_id=project_id,
                job_id=job_id,
                context_type=context_type,
            )
        redacted_prefix = redact_text_for_persistence(
            prefix,
            project_id=project_id,
            job_id=job_id,
            context_type=context_type,
        )
        safe_tool_config = PromptAgent._redact_operational_tool_config_text(
            f"{TOOL_CONFIGURATION_HEADER}{suffix}"
        )
        return f"{redacted_prefix}\n\n{safe_tool_config}"

    @staticmethod
    def _redact_operational_tool_config_text(text: str) -> str:
        lines: list[str] = []
        for line in str(text or "").splitlines():
            if PromptAgent._is_preservable_tool_config_line(line):
                lines.append(PromptAgent._redact_operational_tool_config_line(line))
            else:
                lines.append(
                    redact_text_for_persistence(
                        line,
                        context_type="prompt",
                    )
                )
        return "\n".join(lines)

    @staticmethod
    def _is_preservable_tool_config_line(line: str) -> bool:
        text = str(line or "").strip()
        if text == TOOL_CONFIGURATION_HEADER:
            return True
        if not text:
            return True
        lower = text.lower()
        allowed_prefixes = (
            "- project_name:",
            "- task_id:",
        )
        return lower.startswith(allowed_prefixes)

    @staticmethod
    def _redact_operational_tool_config_line(line: str) -> str:
        redacted = str(line or "")
        redacted = re.sub(
            r"\b(?:postgres|mysql|mongodb|redis)://[^\s\"']+",
            "[REDACTED_URI]",
            redacted,
            flags=re.IGNORECASE,
        )
        redacted = re.sub(
            r"(?i)\b(bearer\s+)[a-z0-9._~+/=-]{12,}",
            r"\1[REDACTED]",
            redacted,
        )
        return re.sub(
            r"(?i)\b(token|api[_-]?key|password|secret)\s*[:=]\s*[^\s,;]+",
            r"\1=[REDACTED]",
            redacted,
        )

    @staticmethod
    def _build_raw_prompt_ref(system_prompt: str, user_prompt: str) -> str | None:
        enabled = _runtime_env.get("PROMPT_RAW_ENCRYPTED_STORAGE_ENABLED", "0").strip().lower()
        if enabled not in {"1", "true", "yes", "on"}:
            return None
        digest = hashlib.sha256(
            f"{system_prompt or ''}\n\n{user_prompt or ''}".encode()
        ).hexdigest()[:24]
        return f"encrypted://prompt/{digest}"

    @staticmethod
    def _assert_prompt_guard(state: dict, system_prompt: str, user_prompt: str) -> None:
        project_config = PromptAgent._coerce_project_config(state)
        assert_prompt_payload_safe(
            system_prompt,
            user_prompt,
            project_config=project_config,
        )

    @staticmethod
    def _truncate_text(value: str, max_chars: int) -> str:
        text = str(value or "")
        if len(text) <= max_chars:
            return text
        return text[:max_chars]


__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
