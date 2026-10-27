#!/usr/bin/env python3
"""Semantic assertions for retained read-only release-smoke checks."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from typing import Any


def _rows(payload: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def _walk(payload: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(payload, dict):
        for key, value in payload.items():
            yield str(key), value
            yield from _walk(value)
    elif isinstance(payload, list):
        for value in payload:
            yield from _walk(value)


def _first(payload: Any, *keys: str) -> Any:
    wanted = set(keys)
    for key, value in _walk(payload):
        if key in wanted:
            return value
    return None


def _assert_ready(payload: Any, *, require_worker: bool) -> str:
    if not isinstance(payload, dict) or payload.get("ready") is not True:
        return "backend did not report ready=true"
    checks = payload.get("checks")
    rows = list(checks.values()) if isinstance(checks, dict) else checks
    if not isinstance(rows, list) or not rows:
        return "readiness returned no concrete checks"
    failed = [
        row
        for row in rows
        if isinstance(row, dict)
        and (row.get("required") is True or "required" not in row)
        and row.get("ok") is not True
        and str(row.get("status") or row.get("state") or "").lower()
        not in {"ok", "pass", "passing", "ready", "healthy", "available"}
    ]
    if failed:
        return "one or more required readiness checks did not pass"
    if require_worker:
        worker_count = _first(payload, "worker_count", "ready_worker_count")
        worker_id = _first(payload, "worker_id", "service_account_id", "machine_token_id")
        if worker_count != 1 and not str(worker_id or "").strip():
            return "no unique worker identity was projected"
    return ""


def semantic_assertion(check_id: str, payload: Any) -> str:
    if check_id in {"A05", "E13"}:
        return _assert_ready(payload, require_worker=True)
    if check_id == "A07":
        return _assert_ready(payload, require_worker=False)
    if check_id == "A06":
        # Whatever the candidate holds, not one named bundle, and possibly none: a
        # release ships no extensions, so an empty stand is the clean case.
        rows = _rows(payload, "extensions", "items", "results")
        unhealthy = [
            str(row.get("extension_id") or row.get("name") or "?")
            for row in rows
            if str(row.get("state") or "").lower() != "active"
            and not (
                row.get("enabled") is True
                and str(row.get("status") or row.get("health") or "healthy").lower()
                in {"active", "enabled", "healthy", "installed"}
            )
            and str(row.get("state") or "").lower() not in {"disabled", "placeholder"}
        ]
        if unhealthy:
            return (
                f"packaged extensions are not enabled and healthy: {', '.join(sorted(unhealthy))}"
            )
        return ""
    if check_id == "A13":
        workflow_id = str(payload.get("workflow_id") or "") if isinstance(payload, dict) else ""
        interface = payload.get("interface") if isinstance(payload, dict) else None
        schema = interface.get("input_schema") if isinstance(interface, dict) else None
        properties = schema.get("properties") if isinstance(schema, dict) else None
        if not workflow_id or not isinstance(properties, dict) or not properties:
            return "workflow contract has no named input schema"
        return ""
    if check_id == "A14":
        rows = _rows(payload, "service_accounts", "items", "results")
        if not rows:
            return "no service account lifecycle fixture is visible"
        if any(
            not str(row.get("id") or row.get("service_account_id") or "").strip() for row in rows
        ):
            return "service account projection contains a blank identity"
        return ""
    if check_id == "A18":
        if not isinstance(payload, dict) or not isinstance(payload.get("hero_kpis"), dict):
            return "dashboard hero KPI projection is unavailable"
        spend = payload["hero_kpis"].get("spend")
        if not isinstance(spend, dict):
            return "dashboard spend projection is unavailable"
        availability = str(spend.get("availability") or "").lower()
        if availability not in {"measured", "partial", "unavailable"}:
            return "dashboard spend omits explicit availability semantics"
        if spend.get("value_usd") is None and availability not in {"unavailable"}:
            return "unknown spend was not marked unavailable"
        unknown_jobs = int(spend.get("unknown_jobs") or 0)
        if availability == "unavailable" and spend.get("value_usd") is not None:
            return "unavailable spend exposed a measured numeric value"
        if availability == "partial" and (spend.get("value_usd") is None or unknown_jobs < 1):
            return "partial spend omitted its known subtotal or unknown count"
        if availability == "measured" and unknown_jobs:
            return "measured spend includes unknown jobs"
        return ""
    if check_id == "E06":
        rows = _rows(payload, "tools", "items", "results")
        if not rows:
            return "tool search returned no candidate rows"
        if any(not str(row.get("tool_id") or row.get("id") or "").strip() for row in rows):
            return "tool search emitted a blank canonical identity"
        return ""
    return f"no semantic assertion is registered for {check_id}"


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: semantic_assertions.py CHECK_ID", file=sys.stderr)
        return 2
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"response is not valid JSON: {exc}", file=sys.stderr)
        return 1
    error = semantic_assertion(sys.argv[1], payload)
    if error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
