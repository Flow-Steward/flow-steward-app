#!/usr/bin/env python3
"""Standalone UI fuzz runner with accessibility checks."""

from __future__ import annotations

import json
import random
import re
import shutil
import string
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import requests

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

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


@dataclass(frozen=True, slots=True)
class FuzzUiOptions:
    base_url: str
    seed: int
    timeout: float
    report_path: str
    role: str
    username: str
    password: str
    skip_auth: bool
    project_id: str
    routes: str
    lighthouse_routes: str
    lighthouse_threshold: float
    lighthouse_timeout: float
    skip_lighthouse: bool
    run_chrome_mcp: bool
    chrome_mcp_script: str
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


def _random_text(rng: random.Random, size: int) -> str:
    alphabet = string.ascii_letters + string.digits + "_-<>/{}[]()\"' "
    return "".join(rng.choice(alphabet) for _ in range(size))


def _parse_routes(raw: str) -> list[str]:
    routes: list[str] = []
    for chunk in str(raw or "").split(","):
        route = str(chunk or "").strip()
        if not route:
            continue
        if not route.startswith("/"):
            route = "/" + route
        routes.append(route)
    return routes


def _expand_route(route: str, project_id: str) -> str:
    if "{project_id}" not in route:
        return route
    if not project_id:
        return ""
    return route.replace("{project_id}", project_id)


def _expected_statuses_for_route(route: str, *, authenticated: bool) -> set[int]:
    if route == "/login":
        return {200, 302}
    if authenticated:
        return {200}
    return {302, 401, 403}


def _check_route_render_matrix(
    *,
    session: requests.Session,
    base_url: str,
    verify: bool | str,
    timeout: float,
    routes: list[str],
    authenticated: bool,
) -> CheckResult:
    issues: list[str] = []
    statuses: list[str] = []
    for route in routes:
        expected = _expected_statuses_for_route(route, authenticated=authenticated)
        response = session.get(
            f"{base_url}{route}",
            timeout=timeout,
            verify=verify,
            allow_redirects=False,
        )
        statuses.append(f"{route}:{response.status_code}")
        if response.status_code >= 500:
            issues.append(f"{route} returned 5xx ({response.status_code})")
            continue
        if response.status_code not in expected:
            issues.append(
                f"{route} returned {response.status_code} (expected one of {sorted(expected)})"
            )
            continue
        if response.status_code == 200 and "<html" not in response.text.lower():
            issues.append(f"{route} returned 200 but response is not HTML")

    if any("5xx" in issue for issue in issues):
        return CheckResult(
            name="ui_route_render_matrix",
            status="fail",
            detail=f"Issues: {issues}; statuses={statuses}",
        )
    if issues:
        return CheckResult(
            name="ui_route_render_matrix",
            status="warn",
            detail=f"Issues: {issues}; statuses={statuses}",
        )
    return CheckResult(
        name="ui_route_render_matrix",
        status="pass",
        detail=f"All routes behaved as expected: {statuses}",
    )


def _check_route_query_fuzz(
    *,
    session: requests.Session,
    base_url: str,
    verify: bool | str,
    timeout: float,
    routes: list[str],
    authenticated: bool,
    rng: random.Random,
) -> CheckResult:
    statuses: list[str] = []
    issues: list[str] = []
    for route in routes:
        expected = _expected_statuses_for_route(route, authenticated=authenticated)
        fuzz_query = urlencode(
            {
                "q": _random_text(rng, 128),
                "search": "<script>alert(1)</script>' OR 1=1 --",
                "page": "-1",
                "limit": "9999",
                "sort": "desc;drop table x",
            }
        )
        response = session.get(
            f"{base_url}{route}?{fuzz_query}",
            timeout=timeout,
            verify=verify,
            allow_redirects=False,
        )
        statuses.append(f"{route}:{response.status_code}")
        if response.status_code >= 500:
            issues.append(f"{route} query fuzz caused 5xx ({response.status_code})")
            continue
        accepted = expected | {400, 404, 422}
        if response.status_code not in accepted:
            issues.append(
                f"{route} query fuzz returned {response.status_code} (accepted={sorted(accepted)})"
            )

    if any("5xx" in issue for issue in issues):
        return CheckResult(
            name="ui_route_query_fuzz",
            status="fail",
            detail=f"Issues: {issues}; statuses={statuses}",
        )
    if issues:
        return CheckResult(
            name="ui_route_query_fuzz",
            status="warn",
            detail=f"Issues: {issues}; statuses={statuses}",
        )
    return CheckResult(
        name="ui_route_query_fuzz",
        status="pass",
        detail=f"All query-fuzz route checks behaved safely: {statuses}",
    )


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "route"


def _resolve_lighthouse_chrome_path() -> str:
    explicit = str(_runtime_env.get("CHROME_PATH") or "").strip()
    if explicit and Path(explicit).is_file():
        return explicit

    for candidate in (
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ):
        if Path(candidate).is_file():
            return candidate

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            resolved = str(playwright.chromium.executable_path or "").strip()
            if resolved and Path(resolved).is_file():
                return resolved
    except Exception:
        return ""
    return ""


def _check_lighthouse_accessibility(
    *,
    session: requests.Session,
    base_url: str,
    routes: list[str],
    threshold: float,
    timeout: float,
    lighthouse_timeout: float,
) -> CheckResult:
    if not routes:
        return CheckResult(
            name="ui_accessibility_lighthouse",
            status="skip",
            detail="No lighthouse routes configured",
        )
    if shutil.which("npx") is None:
        return CheckResult(
            name="ui_accessibility_lighthouse",
            status="skip",
            detail="npx is not available; Lighthouse checks skipped",
        )
    chrome_path = _resolve_lighthouse_chrome_path()
    if not chrome_path:
        return CheckResult(
            name="ui_accessibility_lighthouse",
            status="skip",
            detail="Chrome executable not found; install Chromium or expose CHROME_PATH",
        )

    cookie_header = "; ".join(f"{k}={v}" for k, v in session.cookies.items())
    route_scores: list[tuple[str, float]] = []
    issues: list[str] = []
    lighthouse_env = _runtime_env.copy()
    lighthouse_env["CHROME_PATH"] = chrome_path
    effective_timeout = max(60.0, timeout, lighthouse_timeout)

    with tempfile.TemporaryDirectory(prefix="ui-fuzz-lighthouse-") as tmp:
        tmp_dir = Path(tmp)
        headers_file = tmp_dir / "headers.json"
        if cookie_header:
            headers_file.write_text(json.dumps({"Cookie": cookie_header}), encoding="utf-8")
        for route in routes:
            route_slug = _slug(route)
            report_file = tmp_dir / f"lh-{route_slug}.json"
            target_url = f"{base_url}{route}"
            cmd = [
                "npx",
                "--yes",
                "lighthouse",
                target_url,
                "--quiet",
                "--output",
                "json",
                "--output-path",
                str(report_file),
                "--only-categories",
                "accessibility",
                "--chrome-path",
                chrome_path,
                "--chrome-flags=--headless --ignore-certificate-errors --disable-dev-shm-usage --no-sandbox",
            ]
            if cookie_header:
                cmd.extend(["--extra-headers", str(headers_file)])
            try:
                completed = subprocess.run(
                    cmd,
                    env=lighthouse_env,
                    capture_output=True,
                    text=True,
                    timeout=effective_timeout,
                )
            except subprocess.TimeoutExpired:
                issues.append(
                    f"{route}: lighthouse command timed out after {effective_timeout:.1f}s"
                )
                continue
            if completed.returncode != 0:
                short = (completed.stderr or completed.stdout or "").strip().splitlines()
                issues.append(
                    f"{route}: lighthouse command failed ({short[-1] if short else 'no output'})"
                )
                continue
            if not report_file.is_file():
                issues.append(f"{route}: lighthouse did not produce JSON output")
                continue

            try:
                payload = json.loads(report_file.read_text(encoding="utf-8"))
                score = float(payload["categories"]["accessibility"]["score"])
            except Exception as exc:
                issues.append(f"{route}: could not parse lighthouse report ({exc})")
                continue

            route_scores.append((route, score))
            if score < threshold:
                issues.append(f"{route}: score={score:.2f} below threshold={threshold:.2f}")

    if not route_scores and issues:
        return CheckResult(
            name="ui_accessibility_lighthouse",
            status="warn",
            detail="; ".join(issues),
        )
    if any("below threshold" in issue for issue in issues):
        return CheckResult(
            name="ui_accessibility_lighthouse",
            status="fail",
            detail=f"scores={route_scores}; issues={issues}",
        )
    if issues:
        return CheckResult(
            name="ui_accessibility_lighthouse",
            status="warn",
            detail=f"scores={route_scores}; issues={issues}",
        )
    return CheckResult(
        name="ui_accessibility_lighthouse",
        status="pass",
        detail=f"Lighthouse accessibility scores={route_scores}, threshold={threshold:.2f}",
    )


def _check_chrome_mcp_runner(
    *,
    script_path: Path,
    timeout: float,
) -> CheckResult:
    if not script_path.is_file():
        return CheckResult(
            name="ui_chrome_mcp_fuzz_runner",
            status="skip",
            detail=f"Script not found: {script_path}",
        )
    completed = subprocess.run(
        [str(script_path)],
        capture_output=True,
        text=True,
        timeout=max(120.0, timeout),
    )
    if completed.returncode == 0:
        return CheckResult(
            name="ui_chrome_mcp_fuzz_runner",
            status="pass",
            detail="Chrome MCP fuzz script completed successfully",
        )
    output = (completed.stdout or "") + (completed.stderr or "")
    last_line = output.strip().splitlines()[-1] if output.strip() else "no output"
    return CheckResult(
        name="ui_chrome_mcp_fuzz_runner",
        status="fail",
        detail=f"Chrome MCP fuzz script failed (exit={completed.returncode}): {last_line}",
    )


def run_fuzz_ui(
    *,
    base_url: str,
    seed: int,
    timeout: float,
    report_path: str,
    role: str,
    username: str,
    password: str,
    skip_auth: bool,
    project_id: str,
    routes: str,
    lighthouse_routes: str,
    lighthouse_threshold: float,
    lighthouse_timeout: float,
    skip_lighthouse: bool,
    run_chrome_mcp: bool,
    chrome_mcp_script: str,
    strict: bool,
) -> int:
    """Run UI fuzz checks from typed operation inputs."""
    options = FuzzUiOptions(
        base_url=base_url,
        seed=seed,
        timeout=timeout,
        report_path=report_path,
        role=role,
        username=username,
        password=password,
        skip_auth=skip_auth,
        project_id=project_id,
        routes=routes,
        lighthouse_routes=lighthouse_routes,
        lighthouse_threshold=lighthouse_threshold,
        lighthouse_timeout=lighthouse_timeout,
        skip_lighthouse=skip_lighthouse,
        run_chrome_mcp=run_chrome_mcp,
        chrome_mcp_script=chrome_mcp_script,
        strict=strict,
    )
    return run_fuzz_ui_options(options)


def run_fuzz_ui_options(options: FuzzUiOptions) -> int:
    rng = random.Random(options.seed)
    verify = resolve_ssl_verify()
    base_url = str(options.base_url or "").rstrip("/")
    if not base_url:
        print("Base URL is required")
        return 2

    parsed_routes = [
        expanded
        for expanded in (
            _expand_route(route, str(options.project_id or "").strip())
            for route in _parse_routes(options.routes)
        )
        if expanded
    ]
    lighthouse_routes = [
        expanded
        for expanded in (
            _expand_route(route, str(options.project_id or "").strip())
            for route in _parse_routes(options.lighthouse_routes)
        )
        if expanded
    ]

    session = requests.Session()
    authenticated = False
    auth_context = "anonymous-only mode"

    if not options.skip_auth:
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
            authenticated = True
            auth_context = "authenticated session established"
        except Exception as exc:
            auth_context = f"authentication unavailable: {exc}"

    results: list[CheckResult] = []
    results.append(
        _run(
            "ui_route_render_matrix",
            lambda: _check_route_render_matrix(
                session=session,
                base_url=base_url,
                verify=verify,
                timeout=options.timeout,
                routes=parsed_routes,
                authenticated=authenticated,
            ),
        )
    )
    results.append(
        _run(
            "ui_route_query_fuzz",
            lambda: _check_route_query_fuzz(
                session=session,
                base_url=base_url,
                verify=verify,
                timeout=options.timeout,
                routes=parsed_routes,
                authenticated=authenticated,
                rng=rng,
            ),
        )
    )

    if options.skip_lighthouse:
        results.append(
            CheckResult(
                name="ui_accessibility_lighthouse",
                status="skip",
                detail="Lighthouse checks skipped by --skip-lighthouse",
            )
        )
    else:
        results.append(
            _run(
                "ui_accessibility_lighthouse",
                lambda: _check_lighthouse_accessibility(
                    session=session,
                    base_url=base_url,
                    routes=lighthouse_routes,
                    threshold=float(options.lighthouse_threshold),
                    timeout=options.timeout,
                    lighthouse_timeout=float(options.lighthouse_timeout),
                ),
            )
        )

    if options.run_chrome_mcp:
        results.append(
            _run(
                "ui_chrome_mcp_fuzz_runner",
                lambda: _check_chrome_mcp_runner(
                    script_path=Path(options.chrome_mcp_script).expanduser().resolve(),
                    timeout=max(options.timeout, 120.0),
                ),
            )
        )
    else:
        results.append(
            CheckResult(
                name="ui_chrome_mcp_fuzz_runner",
                status="skip",
                detail="Chrome MCP script not requested (use --run-chrome-mcp)",
            )
        )

    report_path = (
        Path(options.report_path).expanduser()
        if options.report_path
        else default_report_path("ui-fuzz")
    )
    payload = write_report(
        path=report_path,
        lane="ui",
        seed=options.seed,
        base_url=base_url,
        results=results,
        extras={
            "strict_mode": bool(options.strict),
            "auth_context": auth_context,
            "authenticated": authenticated,
            "routes_checked": parsed_routes,
            "lighthouse_routes": lighthouse_routes,
        },
    )

    print(f"[UI FUZZ] auth={auth_context}")
    print_results(results)
    summary = payload.get("summary", {})
    print(
        "\n[UI FUZZ] total={total} pass={passed} warn={warn} fail={fail} skip={skip}".format(
            total=summary.get("total", 0),
            passed=summary.get("pass", 0),
            warn=summary.get("warn", 0),
            fail=summary.get("fail", 0),
            skip=summary.get("skip", 0),
        )
    )
    print(f"[UI FUZZ] report={report_path}")
    return exit_code(results, fail_on_warn=bool(options.strict))


def main(argv: list[str] | None = None) -> int:
    _ = argv
    print("This module is not a public CLI. Use: flow-steward testing fuzz-ui")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
