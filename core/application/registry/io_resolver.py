# ruff: noqa: F405, I001, RUF100
# mypy: ignore-errors
"""Compatibility facade for split registry I/O resolver modules."""

from __future__ import annotations

from .io_resolver_common import *  # noqa: F403,F401  # NOSONAR
from .io_resolver_blocks import *  # noqa: F403,F401  # NOSONAR
from .io_resolver_preview_helpers import *  # noqa: F403,F401  # NOSONAR
from .io_resolver_preview import *  # noqa: F403,F401  # NOSONAR
from .io_resolver_templates import *  # noqa: F403,F401  # NOSONAR
from .io_resolver_outputs import *  # noqa: F403,F401  # NOSONAR
from .io_resolver_validation import *  # noqa: F403,F401  # NOSONAR


class IOResolver(
    IOResolverValidationMixin,
    IOResolverOutputMixin,
    IOResolverTemplateMixin,
    IOResolverPreviewMixin,
    IOResolverPreviewHelpersMixin,
    IOResolverBlockContractMixin,
    IOResolverCommonMixin,
):
    """Computes effective I/O, template variables, and step I/O validation."""


__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
