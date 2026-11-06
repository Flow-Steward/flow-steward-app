"""Workflow report documents: ReportDefinition and ReportSnapshot v1 (ADR 0059).

This package owns the report contract: the schemas, the authoring-time
definition validator, the pure compiler, the snapshot validator used at the
workflow boundary, and the bounded preview that is the only form of a report
allowed in events, messages and model-visible projections. It depends on no
registry, jobs, entrypoint, React or MCP code.
"""

from __future__ import annotations

from core.application.workflow_documents.report_compiler import compile_report
from core.application.workflow_documents.report_definition import validate_report_definition
from core.application.workflow_documents.report_issues import ReportError, prefix_issues
from core.application.workflow_documents.report_rules import map_strings
from core.application.workflow_documents.report_snapshot import validate_report_snapshot
from core.application.workflow_documents.report_summary import (
    REPORT_KIND,
    replace_report_output,
    report_preview,
)

__all__ = [
    "REPORT_KIND",
    "ReportError",
    "compile_report",
    "map_strings",
    "prefix_issues",
    "replace_report_output",
    "report_preview",
    "validate_report_definition",
    "validate_report_snapshot",
]
