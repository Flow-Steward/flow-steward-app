#!/usr/bin/env python3
"""Standalone API fuzz runner for boundary and validation checks."""

from __future__ import annotations

import json
import random
import string
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from .fuzz_common import (
    CheckResult,
    csrf_headers,
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
class FuzzApiOptions:
    base_url: str
    seed: int
    max_text_size: int
    timeout: float
    report_path: str
    role: str
    username: str
    password: str
    skip_auth: bool
    strict: bool


def _random_text(rng: random.Random, size: int) -> str:
    alphabet = string.ascii_letters + string.digits + " _-:/?<>{}[]()"
    return "".join(rng.choice(alphabet) for _ in range(size))


def _run(
    name: str,
    fn,
) -> CheckResult:
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


def _check_protected_route_requires_auth(
    base_url: str, verify: bool | str, timeout: float
) -> CheckResult:
    response = requests.get(
        f"{base_url}/api/jobs",
        params={"limit": 1},
        timeout=timeout,
        verify=verify,
        allow_redirects=False,
    )
    if response.status_code >= 500:
        return CheckResult(
            name="protected_route_requires_auth",
            status="fail",
            detail="Protected route returned 5xx instead of controlled auth response",
            http_status=response.status_code,
        )
    if response.status_code in {401, 403, 302}:
        return CheckResult(
            name="protected_route_requires_auth",
            status="pass",
            detail="Protected route denied anonymous access as expected",
            http_status=response.status_code,
        )
    if response.status_code == 200:
        return CheckResult(
            name="protected_route_requires_auth",
            status="warn",
            detail="Route is accessible anonymously (may be intentional in current auth mode)",
            http_status=response.status_code,
        )
    return CheckResult(
        name="protected_route_requires_auth",
        status="warn",
        detail="Unexpected status for anonymous request",
        http_status=response.status_code,
    )


def _check_login_payload_fuzz(
    *,
    base_url: str,
    verify: bool | str,
    timeout: float,
    rng: random.Random,
    max_text_size: int,
) -> CheckResult:
    payload = {
        "username": _random_text(rng, max(256, max_text_size // 2)),
        "password": _random_text(rng, max(256, max_text_size // 2)),
    }
    response = requests.post(
        f"{base_url}/api/auth/login",
        json=payload,
        timeout=timeout,
        verify=verify,
    )
    if response.status_code >= 500:
        return CheckResult(
            name="login_payload_fuzz",
            status="fail",
            detail="Login endpoint crashed on large random payload",
            http_status=response.status_code,
        )
    if response.status_code in {400, 401, 422, 429}:
        return CheckResult(
            name="login_payload_fuzz",
            status="pass",
            detail="Login endpoint rejected random payload safely",
            http_status=response.status_code,
        )
    return CheckResult(
        name="login_payload_fuzz",
        status="warn",
        detail="Login endpoint returned uncommon status for random payload",
        http_status=response.status_code,
    )


def _check_workflow_run_malformed_json(
    *,
    session: requests.Session,
    base_url: str,
    verify: bool | str,
    timeout: float,
) -> CheckResult:
    headers = {"Content-Type": APPLICATION_JSON_VALUE, "Accept": APPLICATION_JSON_VALUE}
    headers.update(csrf_headers(session))
    response = session.post(
        f"{base_url}/api/workflow-runs",
        data='{"workflow_id": "broken json",',
        headers=headers,
        timeout=timeout,
        verify=verify,
    )
    if response.status_code >= 500:
        return CheckResult(
            name="workflow_run_malformed_json",
            status="fail",
            detail="Workflow-run endpoint returned 5xx on malformed JSON",
            http_status=response.status_code,
        )
    if response.status_code in {400, 422}:
        return CheckResult(
            name="workflow_run_malformed_json",
            status="pass",
            detail="Malformed JSON rejected with client error",
            http_status=response.status_code,
        )
    if response.status_code in {401, 403}:
        return CheckResult(
            name="workflow_run_malformed_json",
            status="pass",
            detail="Protected workflow launch denied safely before payload validation",
            http_status=response.status_code,
        )
    return CheckResult(
        name="workflow_run_malformed_json",
        status="warn",
        detail="Unexpected status for malformed JSON payload",
        http_status=response.status_code,
    )


def _check_workflow_run_type_confusion(
    *,
    session: requests.Session,
    base_url: str,
    verify: bool | str,
    timeout: float,
) -> CheckResult:
    payload = {
        "workflow_id": ["array_instead_of_string"],
        "inputs": "not an object",
        "project_id": "not-a-uuid",
        "client_context": ["not", "an", "object"],
    }
    headers = {"Content-Type": APPLICATION_JSON_VALUE, "Accept": APPLICATION_JSON_VALUE}
    headers.update(csrf_headers(session))
    response = session.post(
        f"{base_url}/api/workflow-runs",
        data=json.dumps(payload),
        headers=headers,
        timeout=timeout,
        verify=verify,
    )
    if response.status_code >= 500:
        return CheckResult(
            name="workflow_run_type_confusion",
            status="fail",
            detail="Workflow-run endpoint returned 5xx for type-confused payload",
            http_status=response.status_code,
        )
    if response.status_code in {400, 422}:
        return CheckResult(
            name="workflow_run_type_confusion",
            status="pass",
            detail="Type-confused payload rejected as expected",
            http_status=response.status_code,
        )
    if response.status_code in {401, 403}:
        return CheckResult(
            name="workflow_run_type_confusion",
            status="pass",
            detail="Protected workflow launch denied safely before payload validation",
            http_status=response.status_code,
        )
    return CheckResult(
        name="workflow_run_type_confusion",
        status="warn",
        detail="Unexpected status for type-confused task payload",
        http_status=response.status_code,
    )


def _check_search_query_fuzz(
    *,
    session: requests.Session,
    base_url: str,
    verify: bool | str,
    timeout: float,
    rng: random.Random,
    max_text_size: int,
) -> CheckResult:
    query = "<script>alert(1)</script>' OR 1=1 -- " + _random_text(rng, min(max_text_size, 2048))
    params: dict[str, str | int] = {"query": query, "category": "all", "limit": 20}
    response = session.get(
        f"{base_url}/api/search",
        params=params,
        timeout=timeout,
        verify=verify,
    )
    if response.status_code >= 500:
        return CheckResult(
            name="search_query_fuzz",
            status="fail",
            detail="Search endpoint returned 5xx for special-character query",
            http_status=response.status_code,
        )
    if response.status_code in {200, 400, 422}:
        return CheckResult(
            name="search_query_fuzz",
            status="pass",
            detail="Search endpoint handled special-character query safely",
            http_status=response.status_code,
        )
    return CheckResult(
        name="search_query_fuzz",
        status="warn",
        detail="Unexpected status for special-character search query",
        http_status=response.status_code,
    )


def _check_search_limit_bounds(
    *,
    session: requests.Session,
    base_url: str,
    verify: bool | str,
    timeout: float,
) -> CheckResult:
    statuses: list[int] = []
    for limit_value in ("-1", "999", "text"):
        response = session.get(
            f"{base_url}/api/search",
            params={"query": "fuzz", "limit": limit_value, "category": "all"},
            timeout=timeout,
            verify=verify,
        )
        statuses.append(response.status_code)
        if response.status_code >= 500:
            return CheckResult(
                name="search_limit_bounds",
                status="fail",
                detail=f"Search endpoint returned 5xx for limit={limit_value}",
                http_status=response.status_code,
            )
    if all(status in {400, 422} for status in statuses):
        return CheckResult(
            name="search_limit_bounds",
            status="pass",
            detail=f"Invalid limit values rejected ({statuses})",
        )
    return CheckResult(
        name="search_limit_bounds",
        status="warn",
        detail=f"Unexpected status mix for invalid limit values ({statuses})",
    )


def run_fuzz_api(
    *,
    base_url: str,
    seed: int,
    max_text_size: int,
    timeout: float,
    report_path: str,
    role: str,
    username: str,
    password: str,
    skip_auth: bool,
    strict: bool,
) -> int:
    """Run API fuzz checks from typed operation inputs."""
    options = FuzzApiOptions(
        base_url=base_url,
        seed=seed,
        max_text_size=max_text_size,
        timeout=timeout,
        report_path=report_path,
        role=role,
        username=username,
        password=password,
        skip_auth=skip_auth,
        strict=strict,
    )
    return run_fuzz_api_options(options)


def run_fuzz_api_options(options: FuzzApiOptions) -> int:
    rng = random.Random(options.seed)
    verify = resolve_ssl_verify()
    base_url = str(options.base_url or "").rstrip("/")
    if not base_url:
        print("Base URL is required")
        return 2

    results: list[CheckResult] = []
    results.append(
        _run(
            "protected_route_requires_auth",
            lambda: _check_protected_route_requires_auth(base_url, verify, options.timeout),
        )
    )
    results.append(
        _run(
            "login_payload_fuzz",
            lambda: _check_login_payload_fuzz(
                base_url=base_url,
                verify=verify,
                timeout=options.timeout,
                rng=rng,
                max_text_size=max(256, int(options.max_text_size)),
            ),
        )
    )

    session: requests.Session | None = None
    if options.skip_auth:
        results.append(
            CheckResult(
                name="authenticated_checks",
                status="skip",
                detail="Authenticated checks skipped by request",
            )
        )
    else:
        username, password = resolve_credentials(
            role=options.role,
            username_override=options.username,
            password_override=options.password,
        )
        try:
            session, _ = login_session(
                base_url=base_url,
                verify=verify,
                username=username,
                password=password,
                timeout=options.timeout,
            )
        except Exception as exc:
            results.append(
                CheckResult(
                    name="authenticated_checks",
                    status="warn",
                    detail=f"Could not establish authenticated session: {exc}",
                )
            )

    if session is not None:
        results.append(
            _run(
                "workflow_run_malformed_json",
                lambda: _check_workflow_run_malformed_json(
                    session=session,
                    base_url=base_url,
                    verify=verify,
                    timeout=options.timeout,
                ),
            )
        )
        results.append(
            _run(
                "workflow_run_type_confusion",
                lambda: _check_workflow_run_type_confusion(
                    session=session,
                    base_url=base_url,
                    verify=verify,
                    timeout=options.timeout,
                ),
            )
        )
        results.append(
            _run(
                "search_query_fuzz",
                lambda: _check_search_query_fuzz(
                    session=session,
                    base_url=base_url,
                    verify=verify,
                    timeout=options.timeout,
                    rng=rng,
                    max_text_size=max(512, int(options.max_text_size)),
                ),
            )
        )
        results.append(
            _run(
                "search_limit_bounds",
                lambda: _check_search_limit_bounds(
                    session=session,
                    base_url=base_url,
                    verify=verify,
                    timeout=options.timeout,
                ),
            )
        )

    report_path = (
        Path(options.report_path).expanduser()
        if options.report_path
        else default_report_path("api-fuzz")
    )
    payload = write_report(
        path=report_path,
        lane="api",
        seed=options.seed,
        base_url=base_url,
        results=results,
        extras={"strict_mode": bool(options.strict)},
    )

    print_results(results)
    summary = payload.get("summary", {})
    print(
        "\n[API FUZZ] total={total} pass={passed} warn={warn} fail={fail} skip={skip}".format(
            total=summary.get("total", 0),
            passed=summary.get("pass", 0),
            warn=summary.get("warn", 0),
            fail=summary.get("fail", 0),
            skip=summary.get("skip", 0),
        )
    )
    print(f"[API FUZZ] report={report_path}")
    return exit_code(results, fail_on_warn=bool(options.strict))


def main(argv: list[str] | None = None) -> int:
    _ = argv
    print("This module is not a public CLI. Use: flow-steward testing fuzz-api")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
