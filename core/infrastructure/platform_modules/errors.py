"""Errors raised while activating and loading prepared platform modules (ADR 0082).

:mod:`core.infrastructure.platform_modules.loader` re-exports every class here.
"""

from __future__ import annotations


class PlatformModuleError(RuntimeError):
    """A prepared platform module cannot be activated, so it is refused or skipped."""


class PlatformModuleListError(PlatformModuleError):
    """The module list is malformed."""


class PlatformModuleIntegrityError(PlatformModuleError):
    """A prepared tree does not match the digests in the module list."""


class PlatformModuleCompatibilityError(PlatformModuleError):
    """A prepared tree was built for another interpreter or machine."""


class PlatformModuleConflictError(PlatformModuleError):
    """A prepared tree would shadow or contradict code that is already importable."""


class PlatformModuleImportError(PlatformModuleError):
    """A verified platform module cannot be imported or registered."""


class PlatformModuleDependencyError(PlatformModuleError):
    """A module requires a module that is missing, too old, failed, or in a cycle."""
