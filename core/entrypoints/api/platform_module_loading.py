"""Composition of platform module loading for the HTTP app (ADR 0082)."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from core.application.settings.platform_contracts import FeatureNotEntitledError
from core.application.settings.platform_module_views import PlatformModuleViews
from core.infrastructure.runtime.app_factory import (
    RuntimeContainer,
    load_optional_private_modules,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from core.infrastructure.platform_modules.loader import PreparedPlatformModule

#: Service errors module routes raise that Core renders as service error responses.
MODULE_SERVICE_ERROR_TYPES: tuple[type[Exception], ...] = (FeatureNotEntitledError,)


def load_platform_modules(
    app: FastAPI,
    container: RuntimeContainer,
    prepared_modules: Sequence[PreparedPlatformModule] = (),
    *,
    logger: logging.Logger | None = None,
    module_names: Any = None,
) -> tuple[str, ...]:
    """Load prepared platform modules, then optional bootstrap modules, with Core's views."""
    return load_optional_private_modules(
        app,
        container,
        logger=logger,
        module_names=module_names,
        prepared_modules=prepared_modules,
        views=PlatformModuleViews(),
    )


__all__ = ["MODULE_SERVICE_ERROR_TYPES", "load_platform_modules"]
