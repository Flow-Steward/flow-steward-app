"""Atomic owner-only filesystem gateway for platform maintenance state."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from core.infrastructure.installer.state_store import _atomic_write_text


class FilesystemMaintenanceGateway:
    """Persist the independent maintenance marker under the shared runtime dir."""

    def __init__(
        self,
        *,
        project_root: Path,
        state_model: Any,
        lifecycle_error: Any,
        runtime_root: Path | None = None,
        marker_mode: int = 0o600,
    ) -> None:
        self._project_root = Path(project_root).resolve()
        self._state_model = state_model
        self._lifecycle_error = lifecycle_error
        shared_runtime = (
            Path(runtime_root).resolve()
            if runtime_root is not None
            else self._project_root / ".flow-steward/runtime"
        )
        self._marker = shared_runtime / "maintenance-state.json"
        self._marker_mode = marker_mode

    def read(self) -> Any | None:
        if self._marker.is_symlink():
            raise self._invalid_state(
                "The maintenance state is not a regular owner-controlled file."
            )
        if not self._marker.exists():
            return None
        if not self._marker.is_file():
            raise self._invalid_state(
                "The maintenance state is not a regular owner-controlled file."
            )
        try:
            payload = json.loads(self._marker.read_text(encoding="utf-8"))
            return self._state_model.model_validate(payload)
        except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as exc:
            raise self._invalid_state("The maintenance state is unreadable or invalid.") from exc

    def begin(self, *, owner_kind: Any, owner_id: str) -> Any:
        proposed = self._state_model(
            phase="draining",
            owner_kind=owner_kind,
            owner_id=owner_id,
            activated_at=datetime.now(UTC),
        )
        current = self.read()
        if current is not None:
            if current.owner_id != proposed.owner_id or current.owner_kind != proposed.owner_kind:
                raise self._lifecycle_error.conflict(
                    operation_id=current.owner_id,
                    cause="Maintenance is already owned by another owner.",
                )
            return current
        self._write(proposed)
        return proposed

    def mark_enabled(self, *, owner_id: str) -> bool:
        validated_owner = self._validated_owner_id(owner_id)
        current = self.read()
        if current is None or current.owner_id != validated_owner:
            return False
        if current.phase == "enabled":
            return True
        self._write(current.model_copy(update={"phase": "enabled"}))
        return True

    def clear(self, *, expected_owner_id: str) -> bool:
        validated_owner = self._validated_owner_id(expected_owner_id)
        current = self.read()
        if current is None or current.owner_id != validated_owner:
            return False
        try:
            self._marker.unlink()
        except FileNotFoundError:
            return False
        return True

    def _write(self, state: Any) -> None:
        _atomic_write_text(
            self._marker,
            json.dumps(state.model_dump(mode="json"), sort_keys=True) + "\n",
            mode=self._marker_mode,
        )

    def _validated_owner_id(self, owner_id: str) -> str:
        return self._state_model(
            phase="draining",
            owner_kind="manual",
            owner_id=owner_id,
            activated_at=datetime.now(UTC),
        ).owner_id

    def _invalid_state(self, cause: str) -> Any:
        return self._lifecycle_error.recovery_required(
            code="maintenance_state_invalid",
            cause=cause,
            remediation="Repair the owner-only maintenance state before retrying.",
            step="maintenance_state",
            recovery_command="flow-steward maintenance status",
        )


__all__ = ["FilesystemMaintenanceGateway"]
