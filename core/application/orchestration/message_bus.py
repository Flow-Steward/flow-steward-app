"""DB-backed AgentMessageV1 runtime bus."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from contextlib import suppress
from typing import Any, ClassVar

from core.application.management.scope import ScopeContext, normalize_scope, scope_from_state
from core.application.orchestration.models import AgentMessageV1, validate_agent_message_payload

logger = logging.getLogger(__name__)


_WITHHELD_SUMMARY = "Step details withheld by the security policy."


def _withheld_message(payload: dict[str, Any], decision: Any) -> dict[str, Any]:
    """The message without the content the security policy blocked.

    Dropping the whole message left Job details with "no inputs or outputs"
    and no reason. The step's identity stays, its data is replaced by a note
    naming the rules that blocked it.
    """
    rules = sorted(
        {
            str(getattr(finding, "rule_id", "") or "").strip()
            for finding in (getattr(decision, "findings", None) or [])
        }
        - {""}
    )
    artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), dict) else {}
    kept = {key: artifacts[key] for key in ("step_kind", "step_id") if key in artifacts}
    note = "The step's inputs and outputs were withheld from diagnostics by the security policy"
    kept["security_withheld"] = f"{note} ({', '.join(rules)})." if rules else f"{note}."
    return {**payload, "artifacts": kept, "summary": _WITHHELD_SUMMARY}


class MissingInputError(ValueError):
    """Raised when a required step input cannot be resolved from message stream."""


class MessageBus:
    """Thin service around platform_agent_messages persistence/query operations."""

    _instances: ClassVar[dict[tuple[str, str], MessageBus]] = {}

    def __init__(self, dsn: str, logger_client=None, scope: ScopeContext | None = None):
        self.dsn = dsn
        if logger_client is not None:
            self._logger = logger_client
        else:
            from core.infrastructure.db.platform_stores import PlatformJobStore

            self._logger = PlatformJobStore(dsn)
        self.scope = normalize_scope(scope) if scope is not None else None

    @classmethod
    def _resolve_dsn(cls, explicit_dsn: str | None = None) -> str:
        from core.infrastructure.config import CONFIG
        from core.infrastructure.db.platform_logger import resolve_observability_dsn

        return resolve_observability_dsn(
            explicit_dsn or CONFIG.get("fs_observability_dsn") or CONFIG.get("fs_db_dsn") or ""
        )

    @classmethod
    def instance(cls, dsn: str | None = None, *, scope: ScopeContext | None = None) -> MessageBus:
        resolved = cls._resolve_dsn(dsn)
        if not resolved:
            raise RuntimeError("MessageBus DSN is not configured")
        scope_key = scope.cache_key() if scope is not None else ""
        cache_key = (resolved, scope_key)
        cached = cls._instances.get(cache_key)
        if cached:
            return cached
        bus = cls(resolved, scope=scope)
        cls._instances[cache_key] = bus
        return bus

    @classmethod
    def for_state(cls, state: Mapping[str, Any] | None) -> MessageBus:
        return cls.instance(scope=scope_from_state(state))

    @staticmethod
    def _coerce_scope_id(value: Any) -> str | None:
        if value is None or isinstance(value, bool):
            return None
        normalized = str(value).strip()
        return normalized or None

    def publish(self, job_id: Any, envelope: dict, *, project_id: Any | None = None) -> bool:
        """Validate envelope and persist it for a job.

        Defense-in-depth: scans the serialised envelope through the security
        engine before persistence so that any secrets/PII missed by upstream
        hooks are caught at the publish boundary.
        """
        model, errors = validate_agent_message_payload(envelope)
        if errors or model is None:
            logger.debug("MessageBus.publish rejected invalid envelope: %s", "; ".join(errors))
            return False

        payload = model.model_dump()

        # Centralised scan gate — redact secrets/PII before DB write
        try:
            from core.infrastructure.security.integration import scan_outbound

            # Text is scanned as written: an ASCII-escaped dump turns every
            # non-Latin word into a \\uXXXX chain, which the obfuscation rule reads
            # as an attack and redacts.
            _payload_str = json.dumps(payload, default=str, ensure_ascii=False)
            resolved_project_id = self._coerce_scope_id(project_id) or self._coerce_scope_id(
                getattr(self.scope, "project_id", None)
            )
            _sec = scan_outbound(
                _payload_str,
                project_id=resolved_project_id,
                job_id=self._coerce_scope_id(job_id),
                context_type="display",
            )
            if _sec.action == "block":
                payload = _withheld_message(payload, _sec)
                withheld_check = scan_outbound(
                    json.dumps(payload, default=str, ensure_ascii=False),
                    project_id=resolved_project_id,
                    job_id=self._coerce_scope_id(job_id),
                    context_type="display",
                )
                if withheld_check.action == "block":
                    logger.warning("MessageBus.publish rejected by security policy (block)")
                    return False
                logger.warning("MessageBus.publish withheld step data blocked by security policy")
                return bool(self._logger.log_agent_message(job_id, payload))
            if _sec.redacted_text is not None and _sec.redacted_text != _payload_str:
                with suppress(Exception):
                    redacted_payload = json.loads(_sec.redacted_text)
                    from core.infrastructure.security.prompt_safety import (
                        restore_canonical_structured_registry_identifiers,
                    )

                    payload = restore_canonical_structured_registry_identifiers(
                        payload,
                        redacted_payload,
                    )
        except Exception:
            pass  # non-fatal

        payload["output_contract"] = model.model_dump().get("output_contract")
        return bool(self._logger.log_agent_message(job_id, payload))

    @staticmethod
    def _row_to_message(row: Mapping[str, Any]) -> dict | None:
        envelope = row.get("envelope")
        if isinstance(envelope, str):
            try:
                envelope = json.loads(envelope)
            except Exception:
                return None
        if not isinstance(envelope, dict):
            return None
        model, errors = validate_agent_message_payload(envelope)
        if errors or model is None:
            return None
        payload = model.model_dump()
        raw_id = row.get("id")
        if raw_id not in (None, ""):
            payload["id"] = raw_id
            payload["message_id"] = raw_id
        raw_timestamp = row.get("timestamp")
        if raw_timestamp not in (None, ""):
            payload["timestamp"] = str(raw_timestamp)
        return payload

    def list_messages(
        self,
        job_id: Any,
        *,
        step_id: str | None = None,
        producer: str | None = None,
        message_type: str | None = None,
        limit: int = 500,
    ) -> list[dict]:
        rows = self._logger.get_agent_messages(job_id, since_id=0, limit=limit)
        out: list[dict] = []
        for row in rows or []:
            if not isinstance(row, Mapping):
                continue
            msg = self._row_to_message(row)
            if not msg:
                continue
            if step_id and str(msg.get("step_id") or "") != step_id:
                continue
            if producer and str(msg.get("from_agent") or "") != producer:
                continue
            if message_type and str(msg.get("skill") or "") != message_type:
                continue
            out.append(msg)
        return out

    def latest(
        self,
        job_id: Any,
        *,
        step_id: str | None = None,
        producer: str | None = None,
        message_type: str | None = None,
    ) -> dict | None:
        messages = self.list_messages(
            job_id,
            step_id=step_id,
            producer=producer,
            message_type=message_type,
            limit=1000,
        )
        return messages[-1] if messages else None

    def inputs_for_step(self, job_id: Any, step: Any) -> dict[str, AgentMessageV1]:
        """Resolve step input messages from step.context_inject selectors.

        Supports:
        - context_inject.<name>.message_type
        - context_inject.<name>.producer (optional)
        - context_inject.<name>.step_id (optional)
        - context_inject.<name>.required (default true)
        """
        context_inject = getattr(step, "context_inject", None)
        if isinstance(step, dict):
            context_inject = step.get("context_inject")
        if not isinstance(context_inject, dict):
            return {}

        resolved: dict[str, AgentMessageV1] = {}
        for name, spec in context_inject.items():
            if not isinstance(spec, dict):
                continue
            message_type = str(spec.get("message_type") or spec.get("skill") or "").strip()
            producer = str(spec.get("producer") or spec.get("from_agent") or "").strip() or None
            step_id = str(spec.get("step_id") or "").strip() or None
            required = bool(spec.get("required", True))

            msg_dict = self.latest(
                job_id,
                step_id=step_id,
                producer=producer,
                message_type=message_type or None,
            )
            if not msg_dict:
                if required:
                    raise MissingInputError(
                        f"Missing required input '{name}'"
                        f" (message_type={message_type or '*'}, producer={producer or '*'}, step_id={step_id or '*'})"
                    )
                continue

            model, errors = validate_agent_message_payload(msg_dict)
            if errors or model is None:
                if required:
                    raise MissingInputError(
                        f"Invalid input message for '{name}': {'; '.join(errors)}"
                    )
                continue
            resolved[str(name)] = model
        return resolved
