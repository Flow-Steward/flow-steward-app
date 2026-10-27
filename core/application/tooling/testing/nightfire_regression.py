#!/usr/bin/env python3
# ruff: noqa: E402
"""Nightfire overnight regression extension runner.

Runs a curated unattended regression extension:
- auth/bootstrap smoke
- authenticated page smoke
- Playwright UI regression subset
- custom Nightfire browser roundtrips

Usage:
  docker compose exec -T fs-app \
    flow-steward testing nightfire-regression --project-id 1
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import requests
import urllib3

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

CORE_ENTRYPOINTS_CLI_VALUE = "core.entrypoints.cli"


ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.application.tooling.testing.runtime_env import (
    apply_test_lane_defaults,
    resolve_base_url,
    resolve_test_env,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DEFAULT_APP_BASE_URL = "http://localhost:8099"
CODE_BLOCK_TEXT_FENCE = "```text"
NO_OUTPUT_PLACEHOLDER = "<no output>"


@dataclass(slots=True)
class NightfireRegressionRunOptions:
    lane: str
    project_id: str
    base_url: str
    verify_ssl: bool
    auth_role: str
    auth_username: str
    auth_password: str
    auth_display_name: str
    source_workflow_id: str
    report_path: str
    with_cutover_smoke: bool
    with_full_ui: bool
    fail_fast: bool


def _resolve_mcp_base_url(env: dict[str, str]) -> str:
    explicit = (
        str(env.get("MCP_BASE_URL") or "").strip()
        or str(_runtime_env.get("MCP_BASE_URL") or "").strip()
    )
    if explicit:
        return explicit
    if Path("/.dockerenv").exists():
        return "http://fs-mcp:8100"
    return "http://localhost:8100"


def _bootstrap_headers() -> dict[str, str]:
    token = str(_runtime_env.get("FS_AUTH_BOOTSTRAP_TOKEN", "") or "").strip()
    if not token:
        return {}
    return {"x-platform-setup-token": token}


def _request_with_retries(
    session: requests.Session,
    method: str,
    url: str,
    *,
    attempts: int = 3,
    retry_backoff_seconds: float = 1.0,
    retry_on_statuses: tuple[int, ...] = (502, 503, 504),
    **kwargs,
) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            last_error = exc
            if attempt == attempts:
                raise
            time.sleep(retry_backoff_seconds * attempt)
            continue

        if response.status_code in retry_on_statuses and attempt < attempts:
            time.sleep(retry_backoff_seconds * attempt)
            continue

        return response

    if last_error is not None:
        raise last_error
    raise RuntimeError(f"{method} {url} failed without a response")


def _login_with_account_selection(
    session: requests.Session,
    *,
    base_url: str,
    username: str,
    password: str,
    verify_ssl: bool,
) -> tuple[requests.Response, bool]:
    login_payload = {"email": username, "password": password}
    response = _request_with_retries(
        session,
        "POST",
        f"{base_url}/api/auth/login",
        json=login_payload,
        verify=verify_ssl,
        timeout=20,
    )
    if response.status_code != 409:
        return response, False

    try:
        payload = response.json()
    except (TypeError, ValueError):
        return response, False
    options = payload.get("account_options") if isinstance(payload, dict) else None
    active = [
        row
        for row in options or []
        if isinstance(row, dict)
        and bool(row.get("is_active", True))
        and str(row.get("account_id") or "").strip()
    ]
    defaults = [row for row in active if bool(row.get("is_default"))]
    selected = None
    if len(defaults) == 1:
        selected = defaults[0]
    elif len(active) == 1:
        selected = active[0]
    if not payload.get("requires_account_selection") or selected is None:
        return response, False

    selected_response = _request_with_retries(
        session,
        "POST",
        f"{base_url}/api/auth/login",
        json={**login_payload, "account_id": str(selected["account_id"]).strip()},
        verify=verify_ssl,
        timeout=20,
    )
    return selected_response, True


def _probe_command(cmd: list[str], env: dict[str, str], *, timeout: float = 15.0) -> bool:
    try:
        completed = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return completed.returncode == 0


def _resolve_compose_project_name(env: dict[str, str]) -> str:
    explicit = (
        str(env.get("COMPOSE_PROJECT_NAME") or "").strip()
        or str(_runtime_env.get("COMPOSE_PROJECT_NAME") or "").strip()
    )
    if explicit:
        return explicit

    hostname = str(_runtime_env.get("HOSTNAME") or "").strip()
    if not hostname:
        return ""
    try:
        completed = subprocess.run(
            [
                "docker",
                "inspect",
                hostname,
                "--format",
                '{{ index .Config.Labels "com.docker.compose.project" }}',
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=10.0,
        )
    except (subprocess.SubprocessError, OSError):
        return ""
    if completed.returncode != 0:
        return ""
    return str(completed.stdout or "").strip()


def _resolve_pytest_command(env: dict[str, str]) -> list[str]:
    direct_pytest = [sys.executable, "-m", "pytest"]
    if _probe_command([*direct_pytest, "--version"], env):
        return direct_pytest

    project_name = _resolve_compose_project_name(env)
    if project_name:
        docker_compose_exec = [
            "docker",
            "compose",
            "-p",
            project_name,
            "exec",
            "-T",
            "fs-test-runner",
            "python",
            "-m",
            "pytest",
        ]
        if _probe_command([*docker_compose_exec, "--version"], env, timeout=30.0):
            return docker_compose_exec

    return direct_pytest


def _run_auth_smoke(env: dict[str, str]) -> tuple[int, str]:
    base_url = str(env.get("APP_BASE_URL") or DEFAULT_APP_BASE_URL).rstrip("/")
    verify_ssl = str(env.get("E2E_VERIFY_SSL", "0") or "").strip() == "1"
    resolved = resolve_test_env(
        env,
        lane=str(env.get("NIGHTFIRE_LANE") or "ui"),
        role=str(env.get("NIGHTFIRE_AUTH_ROLE") or "").strip() or None,
        include_dotenv=False,
    )
    creds = {
        "username": resolved.username,
        "password": resolved.password,
        "display_name": resolved.display_name,
    }

    lines: list[str] = []
    session = requests.Session()
    try:
        landing = _request_with_retries(
            session,
            "GET",
            f"{base_url}/",
            verify=verify_ssl,
            timeout=15,
            allow_redirects=False,
        )
        lines.append(f"GET / -> {landing.status_code}")

        login_resp, selected_account = _login_with_account_selection(
            session,
            base_url=base_url,
            username=creds["username"],
            password=creds["password"],
            verify_ssl=verify_ssl,
        )
        if selected_account:
            lines.append("POST /api/auth/login -> 409")
            lines.append(f"POST /api/auth/login (selected account) -> {login_resp.status_code}")
        else:
            lines.append(f"POST /api/auth/login -> {login_resp.status_code}")
        if login_resp.status_code == 400 and "local auth is disabled" in login_resp.text.lower():
            lines.append(
                "Local auth disabled; treating auth bootstrap/login smoke as not applicable"
            )
            return 0, "\n".join(lines)

        if login_resp.status_code != 200:
            return 1, "\n".join(
                [
                    *lines,
                    "Seeded UI lane credentials are required for Nightfire; automatic bootstrap is disabled.",
                    login_resp.text[:400],
                ]
            )

        me_resp = _request_with_retries(
            session,
            "GET",
            f"{base_url}/api/auth/me",
            verify=verify_ssl,
            timeout=20,
        )
        lines.append(f"GET /api/auth/me -> {me_resp.status_code}")
        if me_resp.status_code != 200:
            return 1, "\n".join([*lines, me_resp.text[:400]])

        csrf_token = str(session.cookies.get("fs_csrf") or "")
        logout_headers = {"x-csrf-token": csrf_token} if csrf_token else {}
        logout_resp = _request_with_retries(
            session,
            "POST",
            f"{base_url}/api/auth/logout",
            headers=logout_headers,
            verify=verify_ssl,
            timeout=20,
        )
        lines.append(f"POST /api/auth/logout -> {logout_resp.status_code}")
        if logout_resp.status_code != 200:
            return 1, "\n".join([*lines, logout_resp.text[:400]])
        return 0, "\n".join(lines)
    except Exception as e:
        return 1, "\n".join([*lines, f"EXCEPTION: {e}"])


def _ensure_authenticated_session(env: dict[str, str]) -> tuple[requests.Session, bool]:
    base_url = str(env.get("APP_BASE_URL") or DEFAULT_APP_BASE_URL).rstrip("/")
    verify_ssl = str(env.get("E2E_VERIFY_SSL", "0") or "").strip() == "1"
    resolved = resolve_test_env(
        env,
        lane=str(env.get("NIGHTFIRE_LANE") or "ui"),
        role=str(env.get("NIGHTFIRE_AUTH_ROLE") or "").strip() or None,
        include_dotenv=False,
    )
    creds = {
        "username": resolved.username,
        "password": resolved.password,
        "display_name": resolved.display_name,
    }
    session = requests.Session()
    login_resp, _selected_account = _login_with_account_selection(
        session,
        base_url=base_url,
        username=creds["username"],
        password=creds["password"],
        verify_ssl=verify_ssl,
    )
    if login_resp.status_code != 200:
        raise RuntimeError(
            "Nightfire requires pre-seeded local auth credentials for the selected role; "
            f"login failed with {login_resp.status_code}: {login_resp.text[:300]}"
        )
    return session, verify_ssl


def _run_authenticated_page_smoke(env: dict[str, str]) -> tuple[int, str]:
    base_url = str(env.get("APP_BASE_URL") or DEFAULT_APP_BASE_URL).rstrip("/")
    resolved = resolve_test_env(
        env,
        lane=str(env.get("NIGHTFIRE_LANE") or "ui"),
        role=str(env.get("NIGHTFIRE_AUTH_ROLE") or "").strip() or None,
        include_dotenv=False,
    )
    project_id = str(resolved.project_id or "").strip()
    lines: list[str] = []
    checks = [
        ("/dashboard", []),
        ("/projects", []),
        ("/jobs", []),
        ("/monitoring?tab=processes", []),
        ("/settings", []),
        ("/registry", []),
    ]
    if project_id:
        checks.append((f"/projects/{project_id}", []))
    try:
        session, verify_ssl = _ensure_authenticated_session(env)
        for path, markers in checks:
            resp = session.get(f"{base_url}{path}", verify=verify_ssl, timeout=20)
            lines.append(f"GET {path} -> {resp.status_code}")
            if resp.status_code != 200:
                return 1, "\n".join([*lines, resp.text[:400]])
            lowered = resp.text.lower()
            if "<html" not in lowered:
                return 1, "\n".join([*lines, f"Missing HTML document for {path}"])
            missing = [marker for marker in markers if marker.lower() not in lowered]
            if missing:
                return 1, "\n".join([*lines, f"Missing markers for {path}: {', '.join(missing)}"])
        return 0, "\n".join(lines)
    except Exception as e:
        return 1, "\n".join([*lines, f"EXCEPTION: {e}"])


def _stage(
    label: str,
    cmd: list[str],
    env: dict[str, str],
    *,
    input_text: str | None = None,
) -> tuple[int, str]:
    print(f"\n{'=' * 80}\n[NIGHTFIRE] {label}\n$ {' '.join(cmd)}\n", flush=True)
    completed = subprocess.run(
        cmd,
        env=env,
        text=True,
        capture_output=True,
        input=input_text,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    if output.strip():
        print(output, flush=True)
    print(f"[NIGHTFIRE] stage={label!r} exit_code={completed.returncode}", flush=True)
    return completed.returncode, output


def _auth_secret_args(*, username: str, password: str) -> tuple[list[str], str | None]:
    args: list[str] = []
    if username:
        args.extend(["--username", username])
    if password:
        args.append("--password-stdin")
        return args, password
    return args, None


def _stage_environment(env: dict[str, str]) -> dict[str, str]:
    """Avoid relying on sensitive environment inheritance for nested public CLI calls."""

    return {
        key: value
        for key, value in env.items()
        if not (key.startswith("E2E_") and key.endswith("_PASSWORD"))
    }


def _report_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path("logs/nightfire") / f"nightfire-{stamp}.md"


def _append_stage_report(
    report_rows: list[str],
    *,
    label: str,
    code: int,
    output: str,
    artifact_path: Path | None = None,
) -> None:
    report_rows.append(f"### {label}")
    report_rows.append("")
    report_rows.append(f"- Exit code: `{code}`")
    if artifact_path is not None:
        report_rows.append(f"- Artifact: `{artifact_path}`")
        if artifact_path.is_file():
            try:
                payload = json.loads(artifact_path.read_text(encoding="utf-8"))
                summary = payload.get("summary") if isinstance(payload, dict) else None
                if isinstance(summary, dict):
                    report_rows.append(
                        "- Artifact summary: "
                        f"total={summary.get('total', 0)}, "
                        f"pass={summary.get('pass', 0)}, "
                        f"warn={summary.get('warn', 0)}, "
                        f"fail={summary.get('fail', 0)}, "
                        f"skip={summary.get('skip', 0)}"
                    )
            except Exception:
                report_rows.append("- Artifact summary: `<unavailable>`")
        else:
            report_rows.append("- Artifact status: `missing`")
    report_rows.append("")
    report_rows.append(CODE_BLOCK_TEXT_FENCE)
    report_rows.append(output.rstrip() or NO_OUTPUT_PLACEHOLDER)
    report_rows.append("```")
    report_rows.append("")


def run_nightfire_regression(
    *,
    lane: str,
    project_id: str,
    base_url: str,
    verify_ssl: bool,
    auth_role: str,
    auth_username: str,
    auth_password: str,
    auth_display_name: str,
    source_workflow_id: str,
    report_path: str,
    with_cutover_smoke: bool,
    with_full_ui: bool,
    fail_fast: bool,
) -> int:
    """Run Nightfire regression from typed operation inputs."""
    args = NightfireRegressionRunOptions(
        lane=lane,
        project_id=project_id,
        base_url=base_url,
        verify_ssl=verify_ssl,
        auth_role=auth_role,
        auth_username=auth_username,
        auth_password=auth_password,
        auth_display_name=auth_display_name,
        source_workflow_id=source_workflow_id,
        report_path=report_path,
        with_cutover_smoke=with_cutover_smoke,
        with_full_ui=with_full_ui,
        fail_fast=fail_fast,
    )
    return _run_nightfire_regression_args(args)


def _configuration_error(message: str) -> int:
    print(f"[NIGHTFIRE] ERROR: {message}", file=sys.stderr)
    return 2


def _run_nightfire_regression_args(
    args: NightfireRegressionRunOptions,
) -> int:
    report_path = _report_path(args.report_path or None)
    run_stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    env = _runtime_env.copy()
    env["APP_BASE_URL"] = str(args.base_url)
    env["E2E_VERIFY_SSL"] = "1" if args.verify_ssl else "0"
    env["NIGHTFIRE_LANE"] = str(args.lane)
    if args.auth_role:
        env["NIGHTFIRE_AUTH_ROLE"] = str(args.auth_role)
    if args.project_id:
        lane_project_key = {
            "ui": "TEST_PROJECT_UI_ID",
            "backend": "TEST_PROJECT_BACKEND_ID",
        }[str(args.lane)]
        env[lane_project_key] = str(args.project_id)
    resolved = apply_test_lane_defaults(
        env, lane=args.lane, role=args.auth_role or None, include_dotenv=True
    )
    normalized_base_url = resolve_base_url(env)
    env["APP_BASE_URL"] = normalized_base_url
    args.base_url = normalized_base_url
    role_prefix = f"E2E_{resolved.role.upper()}"
    effective_username = str(args.auth_username or resolved.username or "").strip()
    effective_password = str(args.auth_password or resolved.password or "")
    effective_display_name = str(args.auth_display_name or resolved.display_name or "").strip()
    if effective_username:
        env[f"{role_prefix}_USERNAME"] = effective_username
    if effective_password:
        env[f"{role_prefix}_PASSWORD"] = effective_password
    if effective_display_name:
        env[f"{role_prefix}_DISPLAY_NAME"] = effective_display_name
    if args.source_workflow_id:
        env["NIGHTFIRE_SOURCE_WORKFLOW_ID"] = str(args.source_workflow_id)
    project_id = str(resolved.project_id or "").strip()
    if not project_id:
        return _configuration_error(
            f"Could not resolve a test project for lane={resolved.lane}. "
            "Set the dedicated lane project env vars or pass --project-id.",
        )
    with_full_ui = bool(
        args.with_full_ui
        or str(env.get("NIGHTFIRE_FULL_UI", "0") or "").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    run_ui_pytest = resolved.lane == "ui" or with_full_ui
    mcp_base_url = _resolve_mcp_base_url(env)
    pytest_cmd = _resolve_pytest_command(env) if run_ui_pytest else []
    fuzz_report_dir = report_path.parent / "fuzz"
    fuzz_artifacts = {
        "api": fuzz_report_dir / f"nightfire-fuzz-api-{run_stamp}.json",
        "ui": fuzz_report_dir / f"nightfire-fuzz-ui-{run_stamp}.json",
        "mcp": fuzz_report_dir / f"nightfire-fuzz-mcp-{run_stamp}.json",
    }
    auth_args, auth_input = _auth_secret_args(
        username=effective_username,
        password=effective_password,
    )

    stages: list[tuple[str, list[str], Path | None, str | None]] = [
        (
            "Fuzz API contracts",
            [
                sys.executable,
                "-m",
                CORE_ENTRYPOINTS_CLI_VALUE,
                "testing",
                "fuzz-api",
                "--base-url",
                str(args.base_url),
                "--role",
                str(resolved.role),
                *auth_args,
                "--report-path",
                str(fuzz_artifacts["api"]),
                "--strict",
                "--confirm",
            ],
            fuzz_artifacts["api"],
            auth_input,
        ),
        (
            "Fuzz UI routes and accessibility",
            [
                sys.executable,
                "-m",
                CORE_ENTRYPOINTS_CLI_VALUE,
                "testing",
                "fuzz-ui",
                "--base-url",
                str(args.base_url),
                "--role",
                str(resolved.role),
                *auth_args,
                "--report-path",
                str(fuzz_artifacts["ui"]),
                "--strict",
                "--confirm",
            ],
            fuzz_artifacts["ui"],
            auth_input,
        ),
        (
            "Fuzz MCP contracts",
            [
                sys.executable,
                "-m",
                CORE_ENTRYPOINTS_CLI_VALUE,
                "testing",
                "fuzz-mcp",
                "--app-base-url",
                str(args.base_url),
                "--mcp-base-url",
                mcp_base_url,
                "--role",
                str(resolved.role),
                *auth_args,
                "--report-path",
                str(fuzz_artifacts["mcp"]),
                "--strict",
                "--confirm",
            ],
            fuzz_artifacts["mcp"],
            auth_input,
        ),
        (
            "Playwright UI smoke: projects page",
            [
                *pytest_cmd,
                "-q",
                "tests/ui/test_projects_page.py",
            ],
            None,
            None,
        ),
        (
            "Playwright UI smoke: task submission",
            [
                *pytest_cmd,
                "-q",
                "tests/ui/test_task_submission.py",
            ],
            None,
            None,
        ),
        (
            "Playwright UI smoke: registry dashboard",
            [
                *pytest_cmd,
                "-q",
                "tests/ui/test_registry_dashboard.py",
            ],
            None,
            None,
        ),
        (
            "Playwright UI smoke: registry agents",
            [
                *pytest_cmd,
                "-q",
                "tests/ui/test_registry_agents.py",
            ],
            None,
            None,
        ),
        (
            "Playwright UI smoke: registry skills",
            [
                *pytest_cmd,
                "-q",
                "tests/ui/test_registry_skills.py",
            ],
            None,
            None,
        ),
        (
            "Playwright UI smoke: registry tools",
            [
                *pytest_cmd,
                "-q",
                "tests/ui/test_registry_tools.py",
            ],
            None,
            None,
        ),
        (
            "Playwright UI smoke: registry workflows",
            [
                *pytest_cmd,
                "-q",
                "tests/ui/test_registry_workflows.py",
            ],
            None,
            None,
        ),
        (
            "Playwright UI smoke: job detail",
            [
                *pytest_cmd,
                "-q",
                "tests/ui/test_job_detail_page.py",
            ],
            None,
            None,
        ),
        (
            "Playwright UI smoke: custom nightfire roundtrips",
            [
                *pytest_cmd,
                "-q",
                "tests/ui/test_nightfire_regression.py",
            ],
            None,
            None,
        ),
    ]
    if not run_ui_pytest:
        stages = [stage for stage in stages if not stage[0].startswith("Playwright UI")]

    if args.with_cutover_smoke:
        stages.extend(
            [
                (
                    "Cutover preflight",
                    [
                        sys.executable,
                        "-m",
                        CORE_ENTRYPOINTS_CLI_VALUE,
                        "testing",
                        "cutover-preflight",
                        "--project-id",
                        project_id,
                        "--base-url",
                        str(args.base_url),
                    ],
                    None,
                    None,
                ),
                (
                    "Cutover smoke",
                    [
                        sys.executable,
                        "-m",
                        CORE_ENTRYPOINTS_CLI_VALUE,
                        "testing",
                        "cutover-smoke",
                        "--project-id",
                        project_id,
                        "--base-url",
                        str(args.base_url),
                        "--confirm",
                    ],
                    None,
                    None,
                ),
            ]
        )
    if with_full_ui:
        workflow_builder_groups = [
            (
                "core",
                "open_workflow_page_makes_no_mutation_requests or "
                "unresolved_required_input_contract_is_preserved_without_builder_repair_path or "
                "builder_defaults_do_not_change_after_plain_page_reload or "
                "workflow_settings_opens_drawer_surface or "
                "click_step_opens_step_drawer or "
                "step_editor_uses_agent_only_when_executor_is_auto_locked or "
                "step_drawer_uses_drawer_native_shell_without_legacy_modal_header",
            ),
            (
                "advanced recovery",
                "test_step_parameter_save_sends_one_update_step_config_change or "
                "test_step_advanced_contract_preserves_edit_context",
            ),
            (
                "advanced mode toggle",
                "advanced_mode_toggle_controls_drawer_advanced_tabs",
            ),
            (
                "advanced expert tabs",
                "workflow_and_step_advanced_tabs_show_real_expert_controls",
            ),
            (
                "advanced step expert",
                "step_expert_controls_are_in_advanced_tab_not_smart_setup",
            ),
            (
                "advanced group",
                "group_editor_opens_from_window_hook_and_canvas or "
                "step_row_primary_label_comes_from_published_block_when_available",
            ),
            (
                "change set",
                "new_group_stays_local_until_discarded or "
                "new_group_applies_as_one_change_set or "
                "stale_revision_keeps_local_changes_and_offers_reload",
            ),
            (
                "advanced ai automation",
                "workflow_automation_can_be_opened_and_created_from_drawer or "
                "automation_editor_presets_and_actions_are_scope_specific",
            ),
            (
                "handoff",
                "step_test_run_tab_has_dedicated_surface or "
                "workflow_test_run_surface_shows_summary_and_effect_warning",
            ),
        ]
        stages.extend(
            [
                (
                    "Playwright UI deep: sidebar page and subpage coverage",
                    [
                        *pytest_cmd,
                        "-q",
                        "tests/ui/test_sidebar_navigation.py",
                    ],
                    None,
                    None,
                ),
                (
                    "Playwright UI deep: admin grid wiring",
                    [
                        *pytest_cmd,
                        "-q",
                        "tests/ui/test_admin_grid_wiring.py",
                    ],
                    None,
                    None,
                ),
                (
                    "Playwright UI deep: nightfire CRUD surfaces",
                    [
                        *pytest_cmd,
                        "-q",
                        "tests/ui/test_nightfire_full_crud.py",
                    ],
                    None,
                    None,
                ),
            ]
        )
        for suffix, expression in workflow_builder_groups:
            stages.append(
                (
                    f"Playwright UI deep: workflow builder contracts ({suffix})",
                    [
                        *pytest_cmd,
                        "-q",
                        "tests/ui/test_workflow_builder_contracts.py",
                        "-k",
                        expression,
                    ],
                    None,
                    None,
                )
            )

    report_rows: list[str] = [
        "# Nightfire Regression Extension",
        "",
        f"- Base URL: `{args.base_url}`",
        f"- MCP base URL: `{mcp_base_url}`",
        f"- Lane: `{resolved.lane}`",
        f"- Test project id: `{project_id}`",
        f"- Test project name: `{resolved.project_name}`",
        f"- Auth role: `{resolved.role}`",
        f"- Auth user: `{resolved.username}`",
        f"- Verify SSL: `{bool(args.verify_ssl)}`",
        f"- Cutover smoke enabled: `{bool(args.with_cutover_smoke)}`",
        f"- Full UI deep coverage enabled: `{with_full_ui}`",
        "- Fuzz artifacts:",
        f"  - API: `{fuzz_artifacts['api']}`",
        f"  - UI: `{fuzz_artifacts['ui']}`",
        f"  - MCP: `{fuzz_artifacts['mcp']}`",
        "",
        "## Stage Results",
        "",
    ]
    failures = 0

    auth_code, auth_output = _run_auth_smoke(env)
    print(f"\n{'=' * 80}\n[NIGHTFIRE] Local auth bootstrap/login smoke\n", flush=True)
    if auth_output.strip():
        print(auth_output, flush=True)
    print(f"[NIGHTFIRE] stage='Local auth bootstrap/login smoke' exit_code={auth_code}", flush=True)
    _append_stage_report(
        report_rows,
        label="Local auth bootstrap/login smoke",
        code=auth_code,
        output=auth_output,
    )
    if auth_code != 0:
        failures += 1
        if args.fail_fast:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text("\n".join(report_rows) + "\n", encoding="utf-8")
            print(f"\n[NIGHTFIRE] report written to {report_path}", flush=True)
            print(f"[NIGHTFIRE] FAIL: {failures} stage(s) failed", flush=True)
            return 1

    page_smoke_code, page_smoke_output = _run_authenticated_page_smoke(env)
    print(f"\n{'=' * 80}\n[NIGHTFIRE] Authenticated page smoke\n", flush=True)
    if page_smoke_output.strip():
        print(page_smoke_output, flush=True)
    print(f"[NIGHTFIRE] stage='Authenticated page smoke' exit_code={page_smoke_code}", flush=True)
    _append_stage_report(
        report_rows,
        label="Authenticated page smoke",
        code=page_smoke_code,
        output=page_smoke_output,
    )
    if page_smoke_code != 0:
        failures += 1
        if args.fail_fast:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text("\n".join(report_rows) + "\n", encoding="utf-8")
            print(f"\n[NIGHTFIRE] report written to {report_path}", flush=True)
            print(f"[NIGHTFIRE] FAIL: {failures} stage(s) failed", flush=True)
            return 1

    stage_env = _stage_environment(env)
    for label, cmd, artifact_path, input_text in stages:
        code, output = _stage(label, cmd, stage_env, input_text=input_text)
        _append_stage_report(
            report_rows,
            label=label,
            code=code,
            output=output,
            artifact_path=artifact_path,
        )
        if code != 0:
            failures += 1
            if args.fail_fast:
                break

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(report_rows) + "\n", encoding="utf-8")
    print(f"\n[NIGHTFIRE] report written to {report_path}", flush=True)

    if failures:
        print(f"[NIGHTFIRE] FAIL: {failures} stage(s) failed", flush=True)
        return 1

    print("[NIGHTFIRE] PASS", flush=True)
    return 0


if __name__ == "__main__":
    print("Use the canonical command: flow-steward testing nightfire-regression", file=sys.stderr)
    raise SystemExit(2)
