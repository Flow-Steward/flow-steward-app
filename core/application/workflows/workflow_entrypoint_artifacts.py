"""Artifact persistence lifecycle for workflow-entrypoint submissions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.application.management.staged_attachment_service import persist_staged_attachments
from core.application.workflows.workflow_entrypoint_models import WorkflowEntrypointLeaseLostError


class WorkflowEntrypointArtifactLifecycle:
    """Own lease fencing, artifact journaling, and rollback for one launch."""

    def __init__(
        self,
        *,
        logger: Any,
        artifact_plane: Any,
        entry_key: str,
        request_id: str,
        request_fingerprint: str,
        idempotency_keys: list[str],
        renew_reservation: Callable[..., bool],
        journal_reservation: Callable[..., bool],
        run_id: str | None = None,
        persist_attachments: Callable[..., list[dict[str, Any]]] = persist_staged_attachments,
    ) -> None:
        self._logger = logger
        self._artifact_plane = artifact_plane
        self._entry_key = str(entry_key or "").strip()
        self._request_id = str(request_id or "").strip()
        self._run_id = str(run_id or request_id or "").strip()
        self._request_fingerprint = str(request_fingerprint or "").strip()
        self._idempotency_keys = list(
            dict.fromkeys(str(key or "").strip() for key in idempotency_keys if str(key).strip())
        )
        self._renew_reservation = renew_reservation
        self._journal_reservation = journal_reservation
        self._persist_attachments = persist_attachments
        self._pending_artifact_ids: list[str] = []
        self._pending_artifacts: list[dict[str, str]] = []

    def heartbeat(self) -> None:
        for idempotency_key in self._idempotency_keys:
            if not self._renew_reservation(
                self._logger,
                entry_key=self._entry_key,
                idempotency_key=idempotency_key,
                request_id=self._request_id,
                request_fingerprint=self._request_fingerprint,
            ):
                raise WorkflowEntrypointLeaseLostError(
                    "Idempotency lease was lost while reading the request payload"
                )

    def reserve(self, artifact: dict[str, Any]) -> None:
        artifact_id = str(artifact.get("artifact_id") or "").strip()
        object_key = str(artifact.get("object_key") or "").strip()
        if artifact_id and artifact_id not in self._pending_artifact_ids:
            self._pending_artifact_ids.append(artifact_id)
        self._pending_artifacts = [
            item for item in self._pending_artifacts if item.get("artifact_id") != artifact_id
        ]
        if artifact_id:
            self._pending_artifacts.append({"artifact_id": artifact_id, "object_key": object_key})
        journal = {
            "pending_artifact_ids": list(self._pending_artifact_ids),
            "pending_artifacts": [dict(item) for item in self._pending_artifacts],
        }
        for idempotency_key in self._idempotency_keys:
            if not self._journal_reservation(
                self._logger,
                entry_key=self._entry_key,
                idempotency_key=idempotency_key,
                request_id=self._request_id,
                request_fingerprint=self._request_fingerprint,
                journal=journal,
            ):
                raise WorkflowEntrypointLeaseLostError(
                    "Idempotency lease was lost before artifact persistence"
                )

    def persist(self, staged_files: list[Any]) -> list[dict[str, Any]]:
        """Persist staged transport files inside the application-owned lifecycle."""
        return self._persist_attachments(
            staged_files=staged_files,
            artifact_plane=self._artifact_plane,
            owner_scope={
                "entry_path": "webhook",
                "entry_key": self._entry_key,
            },
            run_id=self._run_id,
            step_id="workflow_entrypoint",
            source_ref_prefix="multipart",
            heartbeat=self.heartbeat,
            on_artifact_reserved=self.reserve,
        )

    def rollback(self, file_refs: list[dict[str, Any]]) -> None:
        delete_artifact = getattr(self._artifact_plane, "delete_artifact", None)
        if not callable(delete_artifact):
            return
        for file_ref in file_refs:
            artifact_id = str(file_ref.get("artifact_id") or "").strip()
            if not artifact_id:
                continue
            try:
                delete_artifact(artifact_id)
            except Exception:
                continue


__all__ = ["WorkflowEntrypointArtifactLifecycle"]
