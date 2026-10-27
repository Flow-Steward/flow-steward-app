from __future__ import annotations

from typing import Any


class MemoryError(RuntimeError):
    """Base error for canonical memory operations."""


class MemoryValidationError(MemoryError):
    """Raised when a caller provides invalid memory input."""


class MemoryPolicyError(MemoryError):
    """Raised when scope or visibility rules reject a memory operation."""

    def __init__(self, message: str, *, diagnostics: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics or {}


class MemoryConflictError(MemoryError):
    """Raised when a memory write conflicts with existing canonical state."""


class MemoryNotFoundError(MemoryError):
    """Raised when a requested memory record or entity is missing."""


class MemoryUnsafeContentError(MemoryValidationError):
    """Raised when content fails memory safety checks."""
