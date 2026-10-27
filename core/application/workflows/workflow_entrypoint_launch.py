"""Canonical internal launch command for incoming workflow entrypoints."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.application.idempotency.service import RequestIdempotencyService
from core.application.workflows.workflow_entrypoint_models import WorkflowEntrypointLaunchResult


class WorkflowEntrypointLaunchService:
    """Build and dispatch the canonical internal command for an incoming entrypoint."""

    def __init__(
        self,
        *,
        enqueue_workflow_start: Callable[..., Any],
        now_iso: Callable[[], str],
    ) -> None:
        self._enqueue_workflow_start = enqueue_workflow_start
        self._now_iso = now_iso

    def launch(
        self,
        *,
        entry_key: str,
        task_id: str,
        project_id: str,
        run_config: dict[str, Any],
        workflow_id: str,
        request_id: str,
        idempotency_key: str,
        workflow_inputs: dict[str, Any],
        workflow_interface: dict[str, Any],
        files: list[dict[str, Any]],
        request_context: dict[str, Any],
        idempotency_identities: list[dict[str, str]],
        primary_idempotency_identity: dict[str, str],
    ) -> WorkflowEntrypointLaunchResult:
        clean_entry_key = str(entry_key or "").strip()
        inputs = dict(workflow_inputs or {})
        task_summary = str(inputs.get("title") or inputs.get("summary") or "").strip()
        if not task_summary:
            task_summary = f"Webhook entrypoint {clean_entry_key}"
        source_object_id = str(
            inputs.get("object_id") or inputs.get("source_object_id") or inputs.get("id") or ""
        ).strip()
        received_at = self._now_iso()
        from core.application.registry.workflow_secret_defaults import (
            workflow_secret_launch_snapshot,
        )

        secret_snapshot = workflow_secret_launch_snapshot(workflow_interface, inputs=inputs)
        clean_idempotency_key = str(idempotency_key or "").strip()
        idempotency_key_digest = str(
            primary_idempotency_identity.get("key_digest") or ""
        ).strip() or (
            RequestIdempotencyService.digest_key(clean_idempotency_key)
            if clean_idempotency_key
            else None
        )
        job_meta: dict[str, Any] = {
            "entry_key": clean_entry_key,
            "received_at": received_at,
            "invocation": {
                "channel": "webhook",
                "trigger_family": "incoming_webhook",
                "entry_path": "webhook",
                "source_system": "webhook",
                "request_id": str(request_id or "").strip(),
                "received_at": received_at,
                "source_ip": str(request_context.get("source_ip") or "").strip(),
                "content_type": str(request_context.get("content_type") or "").strip(),
                "idempotency_key_digest": idempotency_key_digest,
                "entry_key": clean_entry_key,
                "request_context_summary": dict(request_context.get("payload_summary") or {}),
            },
        }
        identities = [dict(identity) for identity in idempotency_identities]
        if identities:
            job_meta["request_idempotency_identities"] = identities
        if primary_idempotency_identity:
            job_meta["idempotency"] = dict(primary_idempotency_identity)

        job_id = self._enqueue_workflow_start(
            task_id=str(task_id),
            task_summary=task_summary,
            task_source="webhook",
            project_id=str(project_id),
            project_name=str(
                run_config.get("project_name") or run_config.get("name") or ""
            ).strip(),
            project_config=dict(run_config or {}),
            selected_workflow_id=str(workflow_id),
            workflow_selection_source="entry_binding",
            entry_path="webhook",
            input_contract="direct_inputs",
            source_system="webhook",
            source_event_id=str(request_id or "").strip(),
            source_object_id=source_object_id,
            idempotency_key=str(idempotency_key or "").strip(),
            test_mode=False,
            inputs=inputs,
            files=list(files or []),
            extra_state={
                "entry_key": clean_entry_key,
            },
            workflow_secret_ref_fingerprints=secret_snapshot,
            job_meta=job_meta,
        )
        return WorkflowEntrypointLaunchResult(
            job_id=str(job_id),
            task_summary=task_summary,
        )


__all__ = ["WorkflowEntrypointLaunchService"]
