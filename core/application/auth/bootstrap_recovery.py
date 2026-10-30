"""Host-managed recovery of first-administrator access with compensating activation."""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import datetime
from typing import Protocol

from core.application.auth.bootstrap_tokens import (
    BootstrapTokenActivation,
    BootstrapTokenIssueResult,
    BootstrapTokenService,
)
from core.application.installer.errors import InstallerFailure
from core.application.management.errors import ServicePreconditionError


class BootstrapRecoveryPort(Protocol):
    def operation_lock(self) -> AbstractContextManager[object]: ...
    def prepare(self) -> None: ...
    def user_count(self) -> int: ...
    def capture(self) -> None: ...
    def publish(self, *, token: str, expires_at: datetime) -> str: ...
    def activate(self) -> None: ...
    def verify(self, *, restored: bool = False) -> None: ...
    def restore(self) -> None: ...
    def finish(self) -> None: ...


class BootstrapRecoveryService:
    """Keep operator locks through verification and any necessary compensation.

    Counts bracket publication; they do not serialize concurrent HTTP user writers.
    Replacing the hash temporarily invalidates an old token until compensation.
    """

    def __init__(self, *, gateway: BootstrapRecoveryPort) -> None:
        self.gateway = gateway

    def issue(self) -> BootstrapTokenIssueResult:
        with self.gateway.operation_lock():
            self.gateway.prepare()
            return BootstrapTokenService(user_count=self.gateway.user_count, runtime=self).issue()

    def activate(self, *, token: str, expires_at: datetime) -> BootstrapTokenActivation:
        count = self.gateway.user_count()
        if type(count) is not int or count != 0:
            raise ServicePreconditionError("Bootstrap recovery requires an empty user store.")
        self.gateway.capture()
        activation_attempted = False
        try:
            path = self.gateway.publish(token=token, expires_at=expires_at)
            activation_attempted = True
            self.gateway.activate()
            self.gateway.verify()
            count = self.gateway.user_count()
            if type(count) is not int or count != 0:
                raise ServicePreconditionError("A user appeared during bootstrap recovery.")
        except BaseException as original:
            # Transaction cleanup must also compensate cancellation before releasing locks.
            try:
                self.gateway.restore()
                if activation_attempted:
                    self.gateway.activate()
                    self.gateway.verify(restored=True)
                self.gateway.finish()
            except BaseException:  # NOSONAR - interrupted cleanup still reports kept recovery data
                raise InstallerFailure(
                    code="bootstrap_recovery_needed",
                    cause="Bootstrap recovery could not verify compensation. Recovery material was retained.",
                    remediation="Keep the owner-only operations/bootstrap-recovery snapshot and reconcile this installation before retrying.",
                    exit_code=8,
                    retryable=False,
                    step="bootstrap",
                ) from None
            if isinstance(original, (KeyboardInterrupt, SystemExit)):
                raise
            raise InstallerFailure(
                code="bootstrap_token_activation_failed",
                cause="Bootstrap recovery failed; previous artifacts and any affected runtime were restored.",
                remediation="Inspect this installation's application health and retry the host command.",
                exit_code=8,
                step="bootstrap",
            ) from None
        try:
            self.gateway.finish()
        except (OSError, ValueError):
            raise InstallerFailure(
                code="bootstrap_recovery_needed",
                cause="Bootstrap activation was verified, but recovery material cleanup failed.",
                remediation="Reconcile operations/bootstrap-recovery before retrying; preserve any remaining recovery material.",
                exit_code=8,
                retryable=False,
                step="bootstrap",
            ) from None
        return BootstrapTokenActivation(secret_path=path)
