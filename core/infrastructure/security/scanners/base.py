"""SEC-004: Scanner protocol — base interface for all scanner engines."""

from __future__ import annotations

from typing import Protocol

from core.infrastructure.security.models import ScanFinding
from core.infrastructure.security.policy import ScannerConfig


class Scanner(Protocol):
    """Protocol that all scanner engines must implement."""

    def scan(
        self,
        text: str,
        surface: str,
        config: ScannerConfig,
        **kwargs,
    ) -> list[ScanFinding]: ...

    def redact(self, text: str, findings: list[ScanFinding]) -> str: ...
