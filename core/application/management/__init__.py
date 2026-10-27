"""Service-layer helpers for transport-independent reads and writes."""

from __future__ import annotations

import importlib
from typing import Any

CORE_APPLICATION_MANAGEMENT_COMMAND_SERVICES_VALUE = "core.application.management.command_services"
CORE_APPLICATION_MANAGEMENT_ERRORS_VALUE = "core.application.management.errors"
CORE_APPLICATION_MANAGEMENT_QUERY_SERVICES_VALUE = "core.application.management.query_services"


__all__ = [
    "ApprovalCommandService",
    "ApprovalQueryService",
    "EventCommandService",
    "EventQueryService",
    "ExtensionBindingService",
    "ExtensionQueryService",
    "ExternalAgentCommandService",
    "GlobalSearchService",
    "JobCommandService",
    "JobQueryService",
    "ProjectCommandService",
    "ProjectQueryService",
    "ServiceConflictError",
    "ServiceError",
    "ServiceForbiddenError",
    "ServiceNotFoundError",
    "ServiceScopeNotFoundError",
    "ServiceUnavailableError",
    "ServiceValidationError",
    "ToolCommandService",
    "ToolExecutionService",
    "ToolQueryService",
    "global_search",
]

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "ApprovalCommandService": (
        CORE_APPLICATION_MANAGEMENT_COMMAND_SERVICES_VALUE,
        "ApprovalCommandService",
    ),
    "EventCommandService": (
        CORE_APPLICATION_MANAGEMENT_COMMAND_SERVICES_VALUE,
        "EventCommandService",
    ),
    "ExtensionBindingService": (
        CORE_APPLICATION_MANAGEMENT_COMMAND_SERVICES_VALUE,
        "ExtensionBindingService",
    ),
    "ExternalAgentCommandService": (
        CORE_APPLICATION_MANAGEMENT_COMMAND_SERVICES_VALUE,
        "ExternalAgentCommandService",
    ),
    "JobCommandService": (CORE_APPLICATION_MANAGEMENT_COMMAND_SERVICES_VALUE, "JobCommandService"),
    "ProjectCommandService": (
        CORE_APPLICATION_MANAGEMENT_COMMAND_SERVICES_VALUE,
        "ProjectCommandService",
    ),
    "ToolCommandService": (
        CORE_APPLICATION_MANAGEMENT_COMMAND_SERVICES_VALUE,
        "ToolCommandService",
    ),
    "ToolExecutionService": (
        CORE_APPLICATION_MANAGEMENT_COMMAND_SERVICES_VALUE,
        "ToolExecutionService",
    ),
    "ApprovalQueryService": (
        CORE_APPLICATION_MANAGEMENT_QUERY_SERVICES_VALUE,
        "ApprovalQueryService",
    ),
    "EventQueryService": (CORE_APPLICATION_MANAGEMENT_QUERY_SERVICES_VALUE, "EventQueryService"),
    "ExtensionQueryService": (
        CORE_APPLICATION_MANAGEMENT_QUERY_SERVICES_VALUE,
        "ExtensionQueryService",
    ),
    "JobQueryService": (CORE_APPLICATION_MANAGEMENT_QUERY_SERVICES_VALUE, "JobQueryService"),
    "ProjectQueryService": (
        CORE_APPLICATION_MANAGEMENT_QUERY_SERVICES_VALUE,
        "ProjectQueryService",
    ),
    "ToolQueryService": (CORE_APPLICATION_MANAGEMENT_QUERY_SERVICES_VALUE, "ToolQueryService"),
    "ServiceConflictError": (CORE_APPLICATION_MANAGEMENT_ERRORS_VALUE, "ServiceConflictError"),
    "ServiceError": (CORE_APPLICATION_MANAGEMENT_ERRORS_VALUE, "ServiceError"),
    "ServiceForbiddenError": (CORE_APPLICATION_MANAGEMENT_ERRORS_VALUE, "ServiceForbiddenError"),
    "ServiceNotFoundError": (CORE_APPLICATION_MANAGEMENT_ERRORS_VALUE, "ServiceNotFoundError"),
    "ServiceScopeNotFoundError": (
        CORE_APPLICATION_MANAGEMENT_ERRORS_VALUE,
        "ServiceScopeNotFoundError",
    ),
    "ServiceUnavailableError": (
        CORE_APPLICATION_MANAGEMENT_ERRORS_VALUE,
        "ServiceUnavailableError",
    ),
    "ServiceValidationError": (CORE_APPLICATION_MANAGEMENT_ERRORS_VALUE, "ServiceValidationError"),
    "GlobalSearchService": ("core.application.search.global_search_service", "GlobalSearchService"),
    "global_search": ("core.application.search.global_search_service", "global_search"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY_EXPORTS.get(name)
    if target is not None:
        module_name, attr_name = target
        module = importlib.import_module(module_name)
        value = getattr(module, attr_name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
