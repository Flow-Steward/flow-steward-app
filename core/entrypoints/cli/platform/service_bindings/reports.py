"""Report-domain CLI bindings to the shared reporting service."""

from __future__ import annotations

import asyncio
from typing import Any

from core.application.management.command_specs.reports import (
    ReportDiagnosticsCommandInput,
    ReportGenerateCommandInput,
    ReportGetCommandInput,
)
from core.entrypoints.cli.platform.service_bindings.common import (
    RuntimeServices,
    ServiceFactory,
    await_maybe,
    cli_admin_scope,
    cli_operator_scope,
)


async def reports_generate(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = (
        value
        if isinstance(value, ReportGenerateCommandInput)
        else ReportGenerateCommandInput.model_validate(value)
    )
    return await await_maybe(
        runtime.reporting_service.report_generate(
            command.report_type,
            dict(command.params or {}),
            scope=cli_operator_scope(),
            persist=command.persist,
        )
    )


async def reports_get(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = (
        value
        if isinstance(value, ReportGetCommandInput)
        else ReportGetCommandInput.model_validate(value)
    )
    return await await_maybe(
        runtime.reporting_service.report_get(command.report_id, scope=cli_operator_scope())
    )


async def reports_diagnostics(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    from core.application.reports.system_diagnostic_report import (
        collect_system_diagnostic_report,
    )

    command = (
        value
        if isinstance(value, ReportDiagnosticsCommandInput)
        else ReportDiagnosticsCommandInput.model_validate(value)
    )
    host_facts: dict[str, Any] = {
        "collected": command.host is not None,
        "facts": command.host,
        "doctor": command.doctor,
    }
    if command.host is None:
        host_facts["reason"] = "Run ./flow-steward reports diagnostics on the installation host."

    report = await asyncio.to_thread(
        collect_system_diagnostic_report,
        source="cli",
        store=runtime.get_worker_store(),
        list_extensions=lambda: runtime.extension_query_service.list_extensions(
            scope=cli_admin_scope()
        ),
        host=lambda: host_facts,
    )
    return {"report": report}


REPORT_SERVICE_FACTORIES: dict[str, ServiceFactory] = {
    "reports.generate": reports_generate,
    "reports.get": reports_get,
    "reports.diagnostics": reports_diagnostics,
}
