"""Application query service for durable workflow test-run state."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from core.infrastructure.security.prompt_safety import (
    redact_sensitive_structure,
    redact_text_for_persistence,
)


class WorkflowTestRunJobRepository(Protocol):
    def find_by_thread_id(self, thread_id: str) -> dict[str, Any] | None: ...


def _redact_public_text(value: Any) -> str | None:
    """Return a public text field with any credential-shaped content removed."""
    text = str(value or "").strip()
    if not text:
        return None
    return redact_text_for_persistence(text)


def _as_object(value: Any) -> dict[str, Any]:
    """Return a JSON object, whether it arrives as a mapping or as stored text."""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            loaded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(loaded) if isinstance(loaded, dict) else {}
    return {}


@dataclass(frozen=True)
class WorkflowTestRunQueryService:
    repository: WorkflowTestRunJobRepository

    def get_durable_job_state(self, thread_id: str) -> dict[str, Any]:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return {}
        row = self.repository.find_by_thread_id(normalized_thread_id)
        return dict(row) if isinstance(row, dict) else {}

    @staticmethod
    def _sensitive_state_keys(row: dict[str, Any], meta: dict[str, Any]) -> frozenset[str]:
        """Keys whose value came from a secret, whatever they are now called.

        Recorded when a step's inputs are materialised, so a secret aliased into
        an innocuous name is still hidden here.
        """
        for source in (meta, row):
            raw = source.get("materialized_input_sensitive_keys")
            if isinstance(raw, (list, tuple, set)):
                return frozenset(str(item) for item in raw if str(item or "").strip())
        return frozenset()

    @staticmethod
    def _durable_outputs(
        row: dict[str, Any], meta: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Read the named outputs and artifacts a finished run actually produced.

        A completed Test Run persists them, but this projection never returned
        them, so every polling GET reported empty results for a run that had
        produced them — the UI only looked right because the initial POST
        response carried them in a mock. Read the durable record instead, in the
        shapes runs are known to persist.
        """
        workflow_outputs = _as_object(meta.get("workflow_outputs")) or _as_object(
            row.get("workflow_outputs")
        )
        outputs = (
            _as_object(workflow_outputs.get("outputs"))
            or _as_object(meta.get("outputs"))
            or _as_object(row.get("outputs"))
        )
        artifacts = (
            _as_object(workflow_outputs.get("artifacts"))
            or _as_object(meta.get("artifacts"))
            or _as_object(row.get("artifacts"))
        )
        return workflow_outputs, outputs, artifacts

    def get_public_state(self, thread_id: str) -> dict[str, Any]:
        """Project queued/terminal job state without importing execution runtime."""

        normalized_thread_id = str(thread_id or "").strip()
        row = self.get_durable_job_state(normalized_thread_id)
        if not row:
            return {}
        raw_meta = row.get("meta")
        if isinstance(raw_meta, dict):
            meta = dict(raw_meta)
        elif isinstance(raw_meta, str) and raw_meta.strip():
            try:
                loaded = json.loads(raw_meta)
            except (TypeError, ValueError):
                loaded = {}
            meta = dict(loaded) if isinstance(loaded, dict) else {}
        else:
            meta = {}
        return self._public_payload(row, meta, thread_id=normalized_thread_id)

    @classmethod
    def _public_payload(
        cls,
        row: dict[str, Any],
        meta: dict[str, Any],
        *,
        thread_id: str = "",
    ) -> dict[str, Any]:
        """Build the public projection from a durable row and its metadata.

        Separated from the read so the redaction it applies can be exercised
        directly: the leak this closes was in the projection, not the query.
        """
        normalized_thread_id = str(thread_id or "").strip()
        raw_status = str(row.get("status") or "queued").strip().lower()
        status = "queued" if raw_status == "pending" else raw_status
        envelope = meta.get("task_envelope")
        task_envelope = dict(envelope) if isinstance(envelope, dict) else {}
        workflow_outputs, outputs, artifacts = cls._durable_outputs(row, meta)
        # Durable state is not a safe public projection. It carries whatever the
        # run produced, including nested credential-bearing values and anything a
        # secret was aliased into, so it is redacted through the one canonical
        # policy before it leaves the service.
        sensitive_keys = cls._sensitive_state_keys(row, meta)
        workflow_outputs = redact_sensitive_structure(
            workflow_outputs, sensitive_keys=sensitive_keys
        )
        outputs = redact_sensitive_structure(outputs, sensitive_keys=sensitive_keys)
        artifacts = redact_sensitive_structure(artifacts, sensitive_keys=sensitive_keys)
        # The envelope carries the materialized step inputs, so a secret aliased
        # into an innocuous key travelled out here while the fields beside it
        # were being carefully cleaned.
        task_envelope = redact_sensitive_structure(task_envelope, sensitive_keys=sensitive_keys)
        return {
            "status": status,
            "final_status": status,
            "job_id": str(row.get("id") or "").strip() or None,
            "thread_id": normalized_thread_id,
            "execution_id": str(meta.get("execution_id") or "").strip() or None,
            "workflow_id": str(
                row.get("selected_workflow")
                or row.get("selected_workflow_key")
                or meta.get("selected_workflow_id")
                or meta.get("workflow_id")
                or ""
            ).strip()
            or None,
            "project_id": str(row.get("project_id") or meta.get("project_id") or "").strip()
            or None,
            "project_name": str(row.get("project_name") or "").strip() or None,
            "task_id": str(row.get("task_id") or "").strip() or None,
            "task_summary": _redact_public_text(row.get("task_summary")),
            "task_envelope": task_envelope,
            "mode": str(row.get("mode") or "").strip() or None,
            "test_mode": True,
            "needs_user_approval": status == "awaiting_approval",
            "approval_id": (
                str(meta.get("state_approval_id") or "").strip() or None
                if status == "awaiting_approval"
                else None
            ),
            "approval_context": (
                _redact_public_text(meta.get("approval_context"))
                if status == "awaiting_approval"
                else None
            ),
            "final_report": _redact_public_text(row.get("final_report")),
            "error": _redact_public_text(row.get("error")),
            "step_id": str(meta.get("step_id") or "").strip() or None,
            "step_kind": str(meta.get("step_kind") or "").strip() or None,
            "outputs": outputs,
            "artifacts": artifacts,
            "workflow_outputs": workflow_outputs,
        }
