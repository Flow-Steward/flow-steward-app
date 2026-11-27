"""Readiness check rows the retained lifecycle CLI reports.

Adapters only: they translate the application readiness payload, and the host
diagnostics the launcher passes on stdin, into the bounded check rows used by
``doctor`` and lifecycle readiness. They decide nothing about the stack.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from typing import Any

from core.entrypoints.cli.maintenance_adapters import _is_persisted_stuck_jobs_only

_HOST_DIAGNOSTIC_FIELDS = frozenset({"id", "category", "title", "status", "detail", "remediation"})
_MAX_HOST_DIAGNOSTICS_BYTES = 64 * 1024


def _app_health_checks(
    payload: Mapping[str, Any],
    *,
    allow_lifecycle_mutation: bool,
) -> list[dict[str, str]]:
    raw_checks = payload.get("checks")
    if not isinstance(raw_checks, Mapping):
        return [
            {
                "id": "api_ready",
                "category": "core",
                "title": "Application readiness",
                "status": "fail",
                "detail": "App readiness response did not contain checks.",
                "remediation": "Inspect the application readiness endpoint.",
            }
        ]
    checks: list[dict[str, str]] = []
    for check_id, value in raw_checks.items():
        if allow_lifecycle_mutation and check_id == "lifecycle_recovery":
            continue
        if allow_lifecycle_mutation and _is_persisted_stuck_jobs_only(check_id, value):
            continue
        if not isinstance(value, Mapping):
            checks.append(
                {
                    "id": str(check_id),
                    "category": "core",
                    "status": "fail",
                    "detail": "App readiness check returned an invalid result.",
                }
            )
            continue
        raw_status = str(value.get("status") or "").strip().lower()
        status = "pass" if value.get("ok") is True else "fail"
        if raw_status in {"pass", "warn", "fail"}:
            status = raw_status
        checks.append(
            {
                "id": str(value.get("id") or check_id),
                "category": str(value.get("category") or "core"),
                "title": str(value.get("title") or check_id),
                "status": status,
                "detail": str(value.get("detail") or value.get("error") or ""),
                "remediation": str(value.get("remediation") or ""),
            }
        )
    return checks


def _launcher_host_diagnostics() -> tuple[dict[str, object], ...] | None:
    arguments = tuple(sys.argv[1:])
    if arguments.count("doctor") != 1 or any(
        argument
        not in {
            "doctor",
            "--json",
            "--plain",
            "--no-color",
            "--verbose",
            "--non-interactive",
        }
        for argument in arguments
    ):
        return None
    if sys.stdin.isatty():
        return None
    payload = sys.stdin.read(_MAX_HOST_DIAGNOSTICS_BYTES + 1)
    if not payload:
        return None
    try:
        if len(payload.encode("utf-8")) > _MAX_HOST_DIAGNOSTICS_BYTES:
            raise ValueError("host diagnostic payload is too large")
        decoded = json.loads(payload)
        if not isinstance(decoded, list) or not 1 <= len(decoded) <= 32:
            raise ValueError("host diagnostic payload must be a bounded list")
        findings: list[dict[str, object]] = []
        for finding in decoded:
            if not isinstance(finding, dict) or set(finding) != _HOST_DIAGNOSTIC_FIELDS:
                raise ValueError("host diagnostic finding fields are invalid")
            if finding["status"] not in {"pass", "warn", "fail"}:
                raise ValueError("host diagnostic status is invalid")
            if any(
                not isinstance(finding[field], str) or len(finding[field]) > 4096
                for field in _HOST_DIAGNOSTIC_FIELDS
            ):
                raise ValueError("host diagnostic values are invalid")
            findings.append(dict(finding))
        return tuple(findings)
    except ValueError:
        return (
            {
                "id": "host_diagnostics",
                "category": "core",
                "title": "Host diagnostics",
                "status": "fail",
                "detail": "Launcher host diagnostics could not be transferred safely.",
                "remediation": "Rerun ./flow-steward doctor from the installation host.",
            },
        )
