#!/usr/bin/env python3
"""Standalone MCP fuzz runner for JSON-RPC boundary behavior."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from .fuzz_common import (
    CheckResult,
    default_report_path,
    exit_code,
    login_session,
    print_results,
    resolve_credentials,
    resolve_ssl_verify,
    write_report,
)

APPLICATION_JSON_VALUE = "application/json"


@dataclass(frozen=True, slots=True)
class FuzzMcpOptions:
    app_base_url: str
    mcp_base_url: str
    seed: int
    timeout: float
    burst_size: int
    report_path: str
    role: str
    username: str
    password: str
    bearer_token: str
    skip_auth: bool
    strict: bool


def _run(name: str, fn) -> CheckResult:
    started = time.perf_counter()
    try:
        result = fn()
    except Exception as exc:
        elapsed = int((time.perf_counter() - started) * 1000)
        return CheckResult(
            name=name, status="fail", detail=f"Unhandled exception: {exc}", elapsed_ms=elapsed
        )
    result.elapsed_ms = int((time.perf_counter() - started) * 1000)
    return result


def _safe_json(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _rpc_url(mcp_base_url: str) -> str:
    normalized = str(mcp_base_url or "").rstrip("/")
    if normalized.endswith("/mcp"):
        return normalized
    return f"{normalized}/mcp"


def _rpc_post(
    *,
    session: requests.Session,
    mcp_base_url: str,
    payload: dict[str, Any] | None = None,
    timeout: float,
    raw_body: str | None = None,
) -> requests.Response:
    url = _rpc_url(mcp_base_url)
    if raw_body is not None:
        return session.post(
            url,
            data=raw_body,
            headers={"Content-Type": APPLICATION_JSON_VALUE, "Accept": APPLICATION_JSON_VALUE},
            timeout=timeout,
        )
    return session.post(
        url,
        json=payload or {},
        headers={"Content-Type": APPLICATION_JSON_VALUE, "Accept": APPLICATION_JSON_VALUE},
        timeout=timeout,
    )


def _check_tools_list_baseline(
    *,
    session: requests.Session,
    mcp_base_url: str,
    timeout: float,
    has_auth: bool,
) -> CheckResult:
    response = _rpc_post(
        session=session,
        mcp_base_url=mcp_base_url,
        payload={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        timeout=timeout,
    )
    if response.status_code >= 500:
        return CheckResult(
            name="mcp_tools_list_baseline",
            status="fail",
            detail="tools/list returned 5xx",
            http_status=response.status_code,
        )
    payload = _safe_json(response)
    if has_auth:
        if response.status_code == 200 and "result" in payload:
            return CheckResult(
                name="mcp_tools_list_baseline",
                status="pass",
                detail="Authenticated tools/list succeeded",
                http_status=response.status_code,
            )
        return CheckResult(
            name="mcp_tools_list_baseline",
            status="warn",
            detail="Authenticated tools/list did not return expected result payload",
            http_status=response.status_code,
        )
    if response.status_code in {401, 403}:
        return CheckResult(
            name="mcp_tools_list_baseline",
            status="pass",
            detail="Anonymous access denied as expected",
            http_status=response.status_code,
        )
    if response.status_code == 200 and (
        "error" in payload or payload.get("result", {}).get("isError")
    ):
        return CheckResult(
            name="mcp_tools_list_baseline",
            status="pass",
            detail="Anonymous request rejected via JSON-RPC error contract",
            http_status=response.status_code,
        )
    return CheckResult(
        name="mcp_tools_list_baseline",
        status="warn",
        detail="Unexpected anonymous MCP behavior",
        http_status=response.status_code,
    )


def _check_unknown_tool_call(
    *,
    session: requests.Session,
    mcp_base_url: str,
    timeout: float,
    suffix: str,
) -> CheckResult:
    response = _rpc_post(
        session=session,
        mcp_base_url=mcp_base_url,
        payload={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": f"tool_does_not_exist_{suffix}",
                "arguments": {},
            },
        },
        timeout=timeout,
    )
    if response.status_code >= 500:
        return CheckResult(
            name="mcp_unknown_tool_call",
            status="fail",
            detail="Unknown tool call caused 5xx",
            http_status=response.status_code,
        )
    payload = _safe_json(response)
    if response.status_code == 200 and (
        "error" in payload or payload.get("result", {}).get("isError")
    ):
        return CheckResult(
            name="mcp_unknown_tool_call",
            status="pass",
            detail="Unknown tool handled through JSON-RPC error contract",
            http_status=response.status_code,
        )
    if response.status_code in {400, 404}:
        return CheckResult(
            name="mcp_unknown_tool_call",
            status="pass",
            detail="Unknown tool rejected at HTTP boundary",
            http_status=response.status_code,
        )
    return CheckResult(
        name="mcp_unknown_tool_call",
        status="warn",
        detail="Unknown tool response was not deterministic",
        http_status=response.status_code,
    )


def _check_invalid_arguments_shape(
    *,
    session: requests.Session,
    mcp_base_url: str,
    timeout: float,
) -> CheckResult:
    response = _rpc_post(
        session=session,
        mcp_base_url=mcp_base_url,
        payload={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "project_list",
                "arguments": "must-be-an-object",
            },
        },
        timeout=timeout,
    )
    if response.status_code >= 500:
        return CheckResult(
            name="mcp_invalid_arguments_shape",
            status="fail",
            detail="Invalid arguments shape caused 5xx",
            http_status=response.status_code,
        )
    payload = _safe_json(response)
    if response.status_code == 200 and (
        "error" in payload or payload.get("result", {}).get("isError")
    ):
        return CheckResult(
            name="mcp_invalid_arguments_shape",
            status="pass",
            detail="Invalid arguments rejected via JSON-RPC contract",
            http_status=response.status_code,
        )
    if response.status_code in {400, 422}:
        return CheckResult(
            name="mcp_invalid_arguments_shape",
            status="pass",
            detail="Invalid arguments rejected at HTTP boundary",
            http_status=response.status_code,
        )
    return CheckResult(
        name="mcp_invalid_arguments_shape",
        status="warn",
        detail="Invalid arguments response was not deterministic",
        http_status=response.status_code,
    )


def _check_malformed_rpc_envelope(
    *,
    session: requests.Session,
    mcp_base_url: str,
    timeout: float,
) -> CheckResult:
    response = _rpc_post(
        session=session,
        mcp_base_url=mcp_base_url,
        payload={"id": 4, "params": {"name": "project_list"}},
        timeout=timeout,
    )
    if response.status_code >= 500:
        return CheckResult(
            name="mcp_malformed_rpc_envelope",
            status="fail",
            detail="Malformed JSON-RPC envelope caused 5xx",
            http_status=response.status_code,
        )
    if response.status_code in {200, 400, 422}:
        return CheckResult(
            name="mcp_malformed_rpc_envelope",
            status="pass",
            detail="Malformed JSON-RPC envelope handled safely",
            http_status=response.status_code,
        )
    return CheckResult(
        name="mcp_malformed_rpc_envelope",
        status="warn",
        detail="Unexpected status for malformed JSON-RPC envelope",
        http_status=response.status_code,
    )


def _check_non_json_body(
    *,
    session: requests.Session,
    mcp_base_url: str,
    timeout: float,
) -> CheckResult:
    response = _rpc_post(
        session=session,
        mcp_base_url=mcp_base_url,
        timeout=timeout,
        raw_body="{not-json",
    )
    if response.status_code >= 500:
        return CheckResult(
            name="mcp_non_json_body",
            status="fail",
            detail="Non-JSON body caused 5xx",
            http_status=response.status_code,
        )
    if response.status_code in {200, 400, 415, 422}:
        return CheckResult(
            name="mcp_non_json_body",
            status="pass",
            detail="Non-JSON body handled safely",
            http_status=response.status_code,
        )
    return CheckResult(
        name="mcp_non_json_body",
        status="warn",
        detail="Unexpected status for non-JSON body",
        http_status=response.status_code,
    )


def _check_burst_stability(
    *,
    session: requests.Session,
    mcp_base_url: str,
    timeout: float,
    burst_size: int,
) -> CheckResult:
    statuses: dict[int, int] = {}
    errors = 0
    for idx in range(max(1, burst_size)):
        try:
            response = _rpc_post(
                session=session,
                mcp_base_url=mcp_base_url,
                payload={"jsonrpc": "2.0", "id": idx + 100, "method": "tools/list", "params": {}},
                timeout=timeout,
            )
        except Exception:
            errors += 1
            continue
        statuses[response.status_code] = statuses.get(response.status_code, 0) + 1

    if errors > 0:
        return CheckResult(
            name="mcp_burst_stability",
            status="warn",
            detail=f"Burst completed with transport errors={errors}, statuses={statuses}",
        )
    if any(status >= 500 for status in statuses):
        return CheckResult(
            name="mcp_burst_stability",
            status="fail",
            detail=f"Burst produced 5xx statuses: {statuses}",
        )
    return CheckResult(
        name="mcp_burst_stability",
        status="pass",
        detail=f"Burst completed without transport/5xx failures: {statuses}",
    )


def run_fuzz_mcp(
    *,
    app_base_url: str,
    mcp_base_url: str,
    seed: int,
    timeout: float,
    burst_size: int,
    report_path: str,
    role: str,
    username: str,
    password: str,
    bearer_token: str,
    skip_auth: bool,
    strict: bool,
) -> int:
    """Run MCP fuzz checks from typed operation inputs."""
    options = FuzzMcpOptions(
        app_base_url=app_base_url,
        mcp_base_url=mcp_base_url,
        seed=seed,
        timeout=timeout,
        burst_size=burst_size,
        report_path=report_path,
        role=role,
        username=username,
        password=password,
        bearer_token=bearer_token,
        skip_auth=skip_auth,
        strict=strict,
    )
    return run_fuzz_mcp_options(options)


def run_fuzz_mcp_options(options: FuzzMcpOptions) -> int:
    random_gen = random.Random(options.seed)
    verify = resolve_ssl_verify()
    app_base_url = str(options.app_base_url or "").rstrip("/")
    mcp_base_url = str(options.mcp_base_url or "").rstrip("/")
    if not app_base_url or not mcp_base_url:
        print("Both --app-base-url and --mcp-base-url are required")
        return 2

    session = requests.Session()
    bearer = str(options.bearer_token or "").strip()
    auth_note = "provided bearer token"

    if not bearer and not options.skip_auth:
        username, password = resolve_credentials(
            role=options.role,
            username_override=options.username,
            password_override=options.password,
        )
        try:
            _, bearer = login_session(
                base_url=app_base_url,
                verify=verify,
                username=username,
                password=password,
                timeout=options.timeout,
            )
            auth_note = "derived bearer token from authenticated app session"
        except Exception as exc:
            auth_note = f"auth bootstrap failed: {exc}"
    elif options.skip_auth:
        auth_note = "auth bootstrap skipped by request"

    if bearer:
        session.headers.update({"Authorization": f"Bearer {bearer}"})

    has_auth = bool(bearer)
    suffix = f"{random_gen.randint(100000, 999999)}"
    results: list[CheckResult] = []
    results.append(
        _run(
            "mcp_tools_list_baseline",
            lambda: _check_tools_list_baseline(
                session=session,
                mcp_base_url=mcp_base_url,
                timeout=options.timeout,
                has_auth=has_auth,
            ),
        )
    )
    results.append(
        _run(
            "mcp_unknown_tool_call",
            lambda: _check_unknown_tool_call(
                session=session,
                mcp_base_url=mcp_base_url,
                timeout=options.timeout,
                suffix=suffix,
            ),
        )
    )
    results.append(
        _run(
            "mcp_invalid_arguments_shape",
            lambda: _check_invalid_arguments_shape(
                session=session,
                mcp_base_url=mcp_base_url,
                timeout=options.timeout,
            ),
        )
    )
    results.append(
        _run(
            "mcp_malformed_rpc_envelope",
            lambda: _check_malformed_rpc_envelope(
                session=session,
                mcp_base_url=mcp_base_url,
                timeout=options.timeout,
            ),
        )
    )
    results.append(
        _run(
            "mcp_non_json_body",
            lambda: _check_non_json_body(
                session=session,
                mcp_base_url=mcp_base_url,
                timeout=options.timeout,
            ),
        )
    )
    results.append(
        _run(
            "mcp_burst_stability",
            lambda: _check_burst_stability(
                session=session,
                mcp_base_url=mcp_base_url,
                timeout=options.timeout,
                burst_size=max(1, int(options.burst_size)),
            ),
        )
    )

    report_path = (
        Path(options.report_path).expanduser()
        if options.report_path
        else default_report_path("mcp-fuzz")
    )
    payload = write_report(
        path=report_path,
        lane="mcp",
        seed=options.seed,
        base_url=mcp_base_url,
        results=results,
        extras={
            "strict_mode": bool(options.strict),
            "has_auth": has_auth,
            "auth_context": auth_note,
        },
    )

    print(f"[MCP FUZZ] auth={auth_note}")
    print_results(results)
    summary = payload.get("summary", {})
    print(
        "\n[MCP FUZZ] total={total} pass={passed} warn={warn} fail={fail} skip={skip}".format(
            total=summary.get("total", 0),
            passed=summary.get("pass", 0),
            warn=summary.get("warn", 0),
            fail=summary.get("fail", 0),
            skip=summary.get("skip", 0),
        )
    )
    print(f"[MCP FUZZ] report={report_path}")
    return exit_code(results, fail_on_warn=bool(options.strict))


def main(argv: list[str] | None = None) -> int:
    _ = argv
    print("This module is not a public CLI. Use: flow-steward testing fuzz-mcp")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
