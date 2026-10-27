from __future__ import annotations

import json
from typing import Any

from core.application.lifecycle.contracts import (
    ComposeTarget,
)
from core.application.lifecycle.stack_service import (
    StackGatewayError,
)
from core.infrastructure.lifecycle import docker_runtime_support
from core.infrastructure.lifecycle.compact_runtime_scripts import (
    _COMPACT_APP_READINESS_SCRIPT,
)

from .docker_gateway_contracts import (
    _BUNDLED_STORAGE_PROBE_TIMEOUT_SECONDS,
    _DOCKER_TIMEOUT_SECONDS,
    _MAX_CANDIDATE_READINESS_OUTPUT_BYTES,
    _WORKFLOW_KNOWLEDGE_RECONCILIATION_TIMEOUT_SECONDS,
    ComposeCommandRunner,
)


def require_storage_connectivity(
    target: ComposeTarget, *, run_compose: ComposeCommandRunner
) -> None:
    """Run the disposable storage probe; its failure carries the probe's own output.

    AC17-R148: `config apply` answered only "the target configuration failed" when this
    probe failed, because the boolean form above drops the reason.
    """
    service = "compact-runtime" if target.deployment_profile == "compact" else "fs-app"
    arguments = ["run", "--rm", "-T", "--no-deps"]
    if target.deployment_profile == "compact":
        arguments.extend(("--user", "10001:10100"))
    arguments.extend(
        (
            "--entrypoint",
            "python",
            service,
            "-c",
            docker_runtime_support.storage_probe_script(target),
        )
    )
    run_compose(
        target,
        tuple(arguments),
        action="storage_readiness",
        timeout_seconds=_DOCKER_TIMEOUT_SECONDS,
    )


def compact_candidate_preflight(target: ComposeTarget, *, run_compose: ComposeCommandRunner) -> str:
    """Run product-owned database/identity checks in Compact, not management."""
    if target.deployment_profile != "compact":
        raise ValueError("Compact candidate preflight requires a Compact target")
    outcome = run_compose(
        target,
        (
            "exec",
            "-T",
            "compact-runtime",
            "python",
            "-m",
            "core.entrypoints.cli.ops.candidate_runtime_preflight",
        ),
        action="candidate_preflight",
        timeout_seconds=210.0,
    )
    return str(outcome.stdout or "")


def compact_app_readiness(
    target: ComposeTarget, *, run_compose: ComposeCommandRunner
) -> dict[str, Any]:
    """Read the candidate app readiness from inside the Compact container."""
    if target.deployment_profile != "compact":
        raise ValueError("Compact app readiness requires a Compact target")
    outcome = run_compose(
        target,
        (
            "exec",
            "-T",
            "compact-runtime",
            "python",
            "-c",
            _COMPACT_APP_READINESS_SCRIPT,
        ),
        action="candidate_readiness",
        timeout_seconds=_DOCKER_TIMEOUT_SECONDS,
    )
    raw = str(outcome.stdout or "")
    lines = raw.splitlines()
    if len(raw.encode("utf-8")) > _MAX_CANDIDATE_READINESS_OUTPUT_BYTES or len(lines) != 1:
        raise StackGatewayError(
            action="candidate_readiness",
            detail="Compact app readiness returned an invalid process result.",
        )
    try:
        payload = json.loads(lines[0])
    except (TypeError, ValueError) as exc:
        raise StackGatewayError(
            action="candidate_readiness",
            detail="Compact app readiness returned invalid JSON.",
        ) from exc
    if not isinstance(payload, dict):
        raise StackGatewayError(
            action="candidate_readiness",
            detail="Compact app readiness returned an invalid payload.",
        )
    return payload


def compact_maintenance_operation(
    target: ComposeTarget, operation: str, *, run_compose: ComposeCommandRunner
) -> dict[str, object]:
    """Delegate fixed maintenance operations to the existing runtime marker owner."""
    commands = {
        "maintenance-status": "maintenance.status",
        "maintenance-enable": "maintenance.enable",
        "maintenance-disable": "maintenance.disable",
    }
    if target.deployment_profile != "compact" or operation not in commands:
        raise ValueError("Compact maintenance requires a fixed Compact operation")
    arguments = (
        "exec",
        "-T",
        "--user",
        "10007:10007",
        "compact-runtime",
        "python",
        "-m",
        "core.entrypoints.cli.platform_runtime",
        operation,
        "--json",
    )
    if operation != "maintenance-status":
        arguments += ("--confirm",)
    outcome = run_compose(
        target,
        arguments,
        action="maintenance",
        # Observation is bounded; native cooperative job drain retains its
        # existing execution policy and can legitimately exceed 30 seconds.
        timeout_seconds=_DOCKER_TIMEOUT_SECONDS if operation == "maintenance-status" else None,
    )
    raw = str(outcome.stdout or "")
    try:
        if len(raw.encode("utf-8")) > 64 * 1024:
            raise ValueError("Oversized maintenance result")
        payload = json.loads(raw)
        if (
            not isinstance(payload, dict)
            or payload.get("schema") != "flow-steward.cli.v1"
            or payload.get("ok") is not True
            or payload.get("command") != commands[operation]
            or not isinstance(payload.get("result"), dict)
        ):
            raise ValueError("Invalid maintenance result")
    except (TypeError, ValueError) as exc:
        raise StackGatewayError(
            action="maintenance", detail="Compact maintenance returned an invalid result."
        ) from exc
    return dict(payload["result"])


def management_storage_connectivity(
    target: ComposeTarget, *, run_compose: ComposeCommandRunner
) -> bool:
    """Probe storage from the retained CLI while release services are changing."""
    return _storage_connectivity(
        target,
        arguments=(
            "exec",
            "-T",
            "fs-cli",
            "python",
            "-c",
            docker_runtime_support.storage_probe_script(target),
        ),
        run_compose=run_compose,
    )


def _storage_connectivity(
    target: ComposeTarget, *, arguments: tuple[str, ...], run_compose: ComposeCommandRunner
) -> bool:
    try:
        run_compose(
            target,
            arguments,
            action="storage_readiness",
            timeout_seconds=_DOCKER_TIMEOUT_SECONDS,
        )
    except StackGatewayError:
        return False
    return True


def prepare_bundled_storage_probe(
    target: ComposeTarget, *, run_compose: ComposeCommandRunner
) -> None:
    """Start only candidate SeaweedFS; rollback inventory owns reconciliation."""
    if target.file_storage != "bundled":
        raise ValueError("Bundled storage probe requires a bundled target")
    run_compose(
        target,
        ("up", "-d", "seaweedfs"),
        action="bundled_storage_probe_prepare",
        timeout_seconds=_DOCKER_TIMEOUT_SECONDS,
    )


def probe_bundled_storage(target: ComposeTarget, *, run_compose: ComposeCommandRunner) -> None:
    """Run a disposable write/read/delete probe on the target storage network."""
    if target.file_storage != "bundled":
        raise ValueError("Bundled storage probe requires a bundled target")
    run_compose(
        target,
        (
            "run",
            "--rm",
            "-T",
            "--no-deps",
            "--entrypoint",
            "python",
            "fs-cli",
            "-c",
            docker_runtime_support.bundled_storage_write_probe_script(),
        ),
        action="bundled_storage_write_probe",
        timeout_seconds=_BUNDLED_STORAGE_PROBE_TIMEOUT_SECONDS,
    )


def bundled_storage_inventory(
    target: ComposeTarget, *, run_compose: ComposeCommandRunner
) -> dict[str, object]:
    """Inventory bundled objects from a disposable target-network service."""
    if target.file_storage != "bundled":
        raise ValueError("Bundled storage inventory requires a bundled target")
    outcome = run_compose(
        target,
        (
            "run",
            "--rm",
            "-T",
            "--no-deps",
            "--entrypoint",
            "python",
            "fs-cli",
            "-c",
            docker_runtime_support.bundled_storage_inventory_script(),
        ),
        action="bundled_storage_inventory",
        timeout_seconds=_DOCKER_TIMEOUT_SECONDS,
    )
    value = json.loads(str(outcome.stdout or "").strip())
    if not isinstance(value, dict):
        raise ValueError("Bundled storage inventory returned an invalid shape")
    return value


def service_release(target: ComposeTarget, *, run_compose: ComposeCommandRunner) -> str:
    outcome = run_compose(
        target,
        (
            "exec",
            "-T",
            "fs-app",
            "python",
            "-c",
            "from core.infrastructure.config.version import get_app_version; "
            "print(get_app_version())",
        ),
        action="release_identity",
        timeout_seconds=_DOCKER_TIMEOUT_SECONDS,
    )
    release = str(outcome.stdout or "").strip()
    if (
        not release
        or len(release) > 128
        or "\n" in release
        or any(ord(character) < 32 for character in release)
    ):
        raise StackGatewayError(
            action="release_identity",
            detail="Target runtime returned an invalid release identity.",
        )
    return release


def reconcile_workflow_knowledge(
    target: ComposeTarget, *, run_compose: ComposeCommandRunner
) -> None:
    """Refresh derived knowledge using the runtime that is actually installed."""

    runtime_service = (
        "compact-runtime" if target.deployment_profile == "compact" else "control-plane"
    )
    run_compose(
        target,
        (
            "exec",
            "-T",
            runtime_service,
            "python",
            "-c",
            docker_runtime_support.workflow_knowledge_reconciliation_script(),
        ),
        action="workflow_knowledge_reconciliation",
        timeout_seconds=_WORKFLOW_KNOWLEDGE_RECONCILIATION_TIMEOUT_SECONDS,
    )


def reconcile_security_posture(target: ComposeTarget, *, run_compose: ComposeCommandRunner) -> None:
    """Refresh durable security-posture evidence before candidate readiness."""

    runtime_service = (
        "compact-runtime" if target.deployment_profile == "compact" else "control-plane"
    )
    run_compose(
        target,
        (
            "exec",
            "-T",
            runtime_service,
            "python",
            "-c",
            docker_runtime_support.security_posture_reconciliation_script(),
        ),
        action="security_posture_reconciliation",
        timeout_seconds=_WORKFLOW_KNOWLEDGE_RECONCILIATION_TIMEOUT_SECONDS,
    )
