"""Application extension-domain services."""

from core.application.extensions.scaffold import (
    ExtensionScaffoldConflictError,
    ExtensionScaffoldCreateInput,
    ExtensionScaffoldError,
    ExtensionScaffoldResult,
    ExtensionScaffoldService,
)

__all__ = [
    "ExtensionScaffoldConflictError",
    "ExtensionScaffoldCreateInput",
    "ExtensionScaffoldError",
    "ExtensionScaffoldResult",
    "ExtensionScaffoldService",
]
