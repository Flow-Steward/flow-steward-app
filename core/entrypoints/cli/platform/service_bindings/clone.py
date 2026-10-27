"""Thin CLI bindings for installation Clone operations."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import cast

from core.application.clone.operation import CloneRestoreConfirmation
from core.application.clone.service import CloneCreateRequest, CloneRestoreRequest
from core.application.lifecycle.ports import ProgressSink, TechnicalSink
from core.application.management.command_specs.clone import (
    CloneCreateCommandInput,
    CloneRestoreCommandInput,
    CloneRestoreExecutionInput,
)
from core.entrypoints.cli.platform.service_bindings.common import (
    PreparedCommand,
    RuntimeServices,
    ServiceFactory,
    await_maybe,
    current_cancellation_token,
)
from core.entrypoints.cli.secret_input import read_hidden_secret


def _passphrase_source(from_stdin: bool) -> Callable[[], bytes]:
    cached: list[bytes] = []

    def read() -> bytes:
        if not cached:
            if from_stdin:
                stream = getattr(sys.stdin, "buffer", sys.stdin)
                raw = stream.readline()
                value = raw.encode() if isinstance(raw, str) else raw
                value = value.rstrip(b"\r\n")
            else:
                value = read_hidden_secret("Clone passphrase: ")
            if not value:
                raise ValueError("Clone passphrase must not be empty")
            cached.append(value)
        return cached[0]

    if from_stdin:
        # Capture subprocesses inherit stdin before archive assembly reads the secret.
        read()
    return read


async def clone_create(runtime: RuntimeServices, value: object, **kwargs: object) -> object:
    command = CloneCreateCommandInput.model_validate(value)
    return await await_maybe(
        runtime.clone_service.create(
            CloneCreateRequest(output_directory=command.output_directory),
            passphrase_source=_passphrase_source(command.passphrase_stdin),
            progress_sink=cast(ProgressSink | None, kwargs.get("progress_sink")),
            technical_sink=cast(TechnicalSink | None, kwargs.get("technical_sink")),
            cancellation=current_cancellation_token(),
        )
    )


async def clone_restore_preview(runtime: RuntimeServices, value: object) -> PreparedCommand:
    command = CloneRestoreCommandInput.model_validate(value)
    prepared = await await_maybe(
        runtime.clone_service.prepare_restore(
            CloneRestoreRequest(
                archive_path=command.archive_path,
                operation_id=command.operation_id,
            ),
            passphrase_source=_passphrase_source(command.passphrase_stdin),
        )
    )
    plan = prepared.plan
    execution = CloneRestoreExecutionInput(
        prepared=prepared,
        target_installation_id=plan.target_installation_id,
        target_profile=plan.target_profile,
        public_host=plan.retained_shell.configuration.public_host,
        plan_digest=plan.plan_digest,
    )
    review = await await_maybe(runtime.clone_service.review_restore(prepared))
    return PreparedCommand(
        execution_input=execution,
        review=review,
        cleanup=lambda: runtime.clone_service.discard_restore(prepared),
    )


async def clone_restore(runtime: RuntimeServices, value: object, **kwargs: object) -> object:
    command = CloneRestoreExecutionInput.model_validate(value)
    confirmation = CloneRestoreConfirmation(
        target_installation_id=command.target_installation_id,
        target_profile=command.target_profile,
        public_host=command.public_host,
        plan_digest=command.plan_digest,
    )
    return await await_maybe(
        runtime.clone_service.restore(
            command.prepared,
            confirmation=confirmation,
            progress_sink=cast(ProgressSink | None, kwargs.get("progress_sink")),
            technical_sink=cast(TechnicalSink | None, kwargs.get("technical_sink")),
            cancellation=current_cancellation_token(),
        )
    )


CLONE_SERVICE_FACTORIES: dict[str, ServiceFactory] = {
    "clone.create": clone_create,
    "clone.restore": clone_restore,
}
CLONE_PLAN_BUILDERS = {"clone.restore.preview": clone_restore_preview}

__all__ = ["CLONE_PLAN_BUILDERS", "CLONE_SERVICE_FACTORIES"]
