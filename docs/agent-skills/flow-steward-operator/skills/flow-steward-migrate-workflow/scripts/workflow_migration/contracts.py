"""Bounded public contracts for foreign workflow conversion previews."""

from __future__ import annotations

from typing import Any, Literal, TypedDict


class ConversionIssue(TypedDict):
    code: str
    path: str
    message: str
    severity: str
    blocking: bool
    repair_guidance: str


class ConversionResult(TypedDict):
    status: Literal["converted", "blocked"]
    source_format: str
    bundle_yaml: str | None
    runtime_verified: bool
    target_validation: Literal["required"]
    issues: list[ConversionIssue]
    node_mappings: list[dict[str, Any]]
    credential_inputs: list[dict[str, Any]]


class ConversionBlocked(ValueError):
    """Fixed diagnostics only: never place untrusted source values in a message."""

    def __init__(self, code: str, path: str, message: str, guidance: str) -> None:
        super().__init__(code)
        self.issue: ConversionIssue = {
            "code": "n8n." + code,
            "path": path,
            "message": message,
            "severity": "error",
            "blocking": True,
            "repair_guidance": guidance,
        }


def require(
    condition: object,
    code: str,
    path: str,
    message: str,
    guidance: str = "Review and adapt this source construct against the live authoring schema.",
) -> None:
    """Fail closed on unrecognized semantics without echoing source content."""
    if not condition:
        raise ConversionBlocked(code, path, message, guidance)
