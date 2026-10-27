"""Stable installer failure contract."""

from __future__ import annotations


class InstallerFailure(RuntimeError):
    def __init__(
        self,
        *,
        code: str,
        cause: str,
        remediation: str,
        exit_code: int,
        retryable: bool = True,
        step: str = "",
        log_path: str | None = None,
    ) -> None:
        super().__init__(cause)
        self.code = code
        self.cause = cause
        self.remediation = remediation
        self.exit_code = exit_code
        self.retryable = retryable
        self.step = step
        self.log_path = log_path
