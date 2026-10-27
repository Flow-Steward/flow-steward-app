from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from core.application.lifecycle.contracts import (
    ComposeTarget,
)
from core.application.lifecycle.stack_service import (
    StackGatewayError,
)
from core.application.maintenance.contracts import MaintenanceState
from core.infrastructure.installer.state_store import InstallerStateStore, _atomic_write_text
from core.infrastructure.lifecycle.compact_runtime_scripts import (
    _COMPACT_RUNTIME_STATE_EXPORT_SCRIPT,
    _COMPACT_RUNTIME_STATE_STAGE_SCRIPT,
)

from .docker_gateway_contracts import (
    _DOCKER_TIMEOUT_SECONDS,
    _PATH_FLOW_STEWARD,
    ComposeCommandRunner,
)


def stage_profile_runtime_state(
    previous_target: ComposeTarget,
    target: ComposeTarget,
    *,
    source_environment: Mapping[str, str] | None = None,
    project_root: Path,
    run_compose: ComposeCommandRunner,
) -> None:
    """Copy host lifecycle state into Compact's sole persistent volume."""
    if previous_target.deployment_profile == target.deployment_profile:
        return
    if previous_target.deployment_profile == "compact":
        _export_compact_runtime_state(
            previous_target,
            target,
            source_environment=source_environment,
            project_root=project_root,
            run_compose=run_compose,
        )
        return
    if target.deployment_profile != "compact":
        return
    marker = project_root / ".flow-steward/runtime/maintenance-state.json"
    if marker.is_symlink():
        raise StackGatewayError(
            action="stage_profile_runtime_state",
            detail="Maintenance state must be a regular owner-controlled file.",
        )
    maintenance_state: dict[str, object] | None = None
    if marker.exists():
        if not marker.is_file():
            raise StackGatewayError(
                action="stage_profile_runtime_state",
                detail="Maintenance state must be a regular owner-controlled file.",
            )
        try:
            state = MaintenanceState.model_validate_json(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StackGatewayError(
                action="stage_profile_runtime_state",
                detail="Maintenance state is unreadable or invalid.",
            ) from exc
        maintenance_state = state.model_dump(mode="json")
    env_path = project_root / target.env_file
    worker_token = str(
        InstallerStateStore(project_root / _PATH_FLOW_STEWARD)
        .read_env_values(env_path)
        .get("FS_WORKER_MCP_BEARER_TOKEN")
        or ""
    ).strip()
    if not worker_token:
        raise StackGatewayError(
            action="stage_profile_runtime_state",
            detail="Worker machine token is missing from the installed environment.",
        )
    payload = json.dumps(
        {
            "maintenance_state": maintenance_state,
            "worker_token": worker_token,
        },
        sort_keys=True,
    )
    run_compose(
        target,
        (
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--entrypoint",
            "python",
            "compact-runtime",
            "-c",
            _COMPACT_RUNTIME_STATE_STAGE_SCRIPT,
        ),
        action="stage_profile_runtime_state",
        timeout_seconds=_DOCKER_TIMEOUT_SECONDS,
        input_text=payload,
    )


def _export_compact_runtime_state(
    previous_target: ComposeTarget,
    target: ComposeTarget,
    *,
    source_environment: Mapping[str, str] | None,
    project_root: Path,
    run_compose: ComposeCommandRunner,
) -> None:
    outcome = run_compose(
        previous_target,
        (
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--entrypoint",
            "python",
            "compact-runtime",
            "-c",
            _COMPACT_RUNTIME_STATE_EXPORT_SCRIPT,
        ),
        action="export_profile_runtime_state",
        timeout_seconds=_DOCKER_TIMEOUT_SECONDS,
        environment=source_environment,
        separate_stderr=True,
    )
    try:
        payload = json.loads(str(outcome.stdout or ""))
        worker_token = str(payload.get("worker_token") or "").strip()
        if not worker_token:
            raise ValueError("worker machine token is missing")
        raw_maintenance = payload.get("maintenance_state")
        maintenance = (
            None if raw_maintenance is None else MaintenanceState.model_validate(raw_maintenance)
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise StackGatewayError(
            action="export_profile_runtime_state",
            detail="Compact runtime state is unreadable or invalid.",
        ) from exc
    store = InstallerStateStore(project_root / _PATH_FLOW_STEWARD)
    store.update_env_values(
        project_root / target.env_file,
        {"FS_WORKER_MCP_BEARER_TOKEN": worker_token},
    )
    marker = project_root / ".flow-steward/runtime/maintenance-state.json"
    if maintenance is None:
        marker.unlink(missing_ok=True)
    else:
        _atomic_write_text(
            marker,
            json.dumps(maintenance.model_dump(mode="json"), sort_keys=True) + "\n",
            mode=0o600,
        )
