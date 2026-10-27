#!/usr/bin/env python3
"""One-command OSS deployment readiness checks."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from core.application.tooling.ops import deployment_doctor_common as _common
from core.application.tooling.ops import deployment_doctor_live as _live
from core.application.tooling.ops import deployment_doctor_static as _static
from core.application.tooling.ops.deployment_doctor_common import (
    DEFAULT_COMPOSE_PROBE_SERVICE,
    LIVE_PROBES_RUN_FROM_EXPECTED_BOUNDARY_VALUE,
    REQUIRED_COMPOSE_SERVICES,
    REQUIRED_DB_TABLES,
    CheckResult,
    DoctorOptions,
)
from core.infrastructure.config.env_registry import selection_from_services


def run_doctor(options: DoctorOptions) -> dict[str, Any]:
    repo_root = options.repo_root.resolve()
    env_path = _resolve_path(options.env_file, repo_root)
    env = _load_env(env_path, from_process=options.live_only)
    compose_files = _resolve_compose_files(options, repo_root)
    compose_text = "\n".join(
        path.read_text(encoding="utf-8") for path in compose_files if path.exists()
    )
    required_services = options.required_compose_services or REQUIRED_COMPOSE_SERVICES
    stack_environment = _normalize_stack_environment(options.stack_environment)
    stack_selection = selection_from_services(
        stack_environment=stack_environment,
        required_services=required_services,
        runtime_services=options.runtime_compose_services,
        file_storage=env.get("FS_FILE_STORAGE") or env.get("ARTIFACT_STORAGE_BACKEND"),
    )

    checks: list[CheckResult] = []
    if not options.live_only:
        checks.extend(
            [
                _check_required_files(repo_root),
                _check_env_file(env_path, stack_environment=stack_environment),
                _check_env_duplicate_keys(env_path),
                _check_required_env(env, stack_selection),
                _check_secrets_changed(env, stack_selection),
                _check_llm_provider(env),
                _check_auth_defaults(
                    env,
                    stack_environment=stack_environment,
                    allow_initial_auth_bootstrap=options.allow_initial_auth_bootstrap,
                ),
                _check_redaction_policy(env),
                _check_image_pins(env, stack_environment=stack_environment),
                _check_compose_services(compose_text, required_services),
                _check_internal_services_not_published(
                    compose_text,
                    required_services,
                    stack_environment=stack_environment,
                ),
                _check_compose_config(options, repo_root, env_path, run=options.run_compose_config),
                _check_oss_paths(repo_root, skip=options.skip_git),
            ]
        )
    if options.static_only:
        checks.append(
            CheckResult(
                id="live_runtime",
                category="runtime",
                title="Running deployment probes",
                status="warn",
                detail="Skipped by --static-only.",
                remediation="Run without --static-only after containers are started.",
            )
        )
    else:
        checks.extend(
            _run_live_checks(options=options, repo_root=repo_root, env_path=env_path, env=env)
        )
    if options.strict:
        checks = [
            CheckResult(
                id=check.id,
                category=check.category,
                title=check.title,
                status="fail" if check.status == "warn" else check.status,
                detail=check.detail,
                remediation=check.remediation,
            )
            for check in checks
        ]
    summary = _summary(checks)
    return {
        "ok": summary["fail"] == 0,
        "env_file": str(env_path),
        "repo_root": str(repo_root),
        "summary": summary,
        "checks": [check.dump() for check in checks],
    }


def _run_live_checks(
    *,
    options: DoctorOptions,
    repo_root: Path,
    env_path: Path,
    env: dict[str, str],
) -> list[CheckResult]:
    probe_mode = str(options.probe_mode or "compose").strip().lower()
    if probe_mode == "compose" and not options.live_only:
        checks = [_check_compose_runtime_services(options, repo_root, env_path)]
        checks.extend(
            _run_compose_live_probe(
                options=options, repo_root=repo_root, env_path=env_path, env=env
            )
        )
        return checks
    return _run_current_process_live_checks(
        options=options,
        repo_root=repo_root,
        env_path=env_path,
        env=env,
        include_compose_runtime=probe_mode == "host" and not options.live_only,
        boundary="container" if _running_in_container() else "host",
        stack_environment=_normalize_stack_environment(options.stack_environment),
    )


def _run_current_process_live_checks(
    *,
    options: DoctorOptions,
    repo_root: Path,
    env_path: Path,
    env: dict[str, str],
    include_compose_runtime: bool,
    boundary: str,
    stack_environment: str,
) -> list[CheckResult]:
    deployment_profile = str(env.get("FS_DEPLOYMENT_PROFILE") or "scalable").strip().lower()
    base_url = _resolve_base_url(env, options.base_url)
    mcp_url = _resolve_mcp_url(env, options.mcp_url)
    timeout = max(1.0, float(options.timeout_seconds or 0))
    checks = [
        CheckResult(
            id="live_probe_boundary",
            category="runtime",
            title=LIVE_PROBES_RUN_FROM_EXPECTED_BOUNDARY_VALUE,
            status="pass",
            detail=f"{boundary} process",
        ),
        _check_api_readiness(
            base_url,
            deployment_profile=deployment_profile,
            timeout=timeout,
            allow_lifecycle_mutation=options.allow_lifecycle_mutation,
            allow_release_transition=options.allow_release_transition,
        ),
        _check_db_connectivity(env, timeout=timeout),
        _check_queue_connectivity(env, timeout=timeout),
        _check_queue_worker_health(
            base_url,
            timeout=timeout,
            allow_lifecycle_mutation=options.allow_lifecycle_mutation,
            allow_release_transition=options.allow_release_transition,
        ),
        _check_mcp_auth_readiness(
            mcp_url,
            deployment_profile=deployment_profile,
            bearer_token=_resolve_bearer_token(env, options.bearer_token),
            auth_disabled=_env_flag(env, "FS_MCP_AUTH_DISABLED"),
            stack_environment=stack_environment,
            timeout=timeout,
        ),
    ]
    if include_compose_runtime:
        checks.insert(5, _check_compose_runtime_services(options, repo_root, env_path))
    return checks


def _check_required_files(repo_root: Path) -> CheckResult:
    return _static._check_required_files(repo_root)


def _check_env_file(env_path: Path, *, stack_environment: str) -> CheckResult:
    return _static._check_env_file(env_path, stack_environment=stack_environment)


def _check_env_duplicate_keys(env_path: Path) -> CheckResult:
    return _static._check_env_duplicate_keys(env_path)


def _check_required_env(env: dict[str, str], selection: Any) -> CheckResult:
    return _static._check_required_env(env, selection)


def _check_secrets_changed(env: dict[str, str], selection: Any) -> CheckResult:
    return _static._check_secrets_changed(env, selection)


def _check_llm_provider(env: dict[str, str]) -> CheckResult:
    return _static._check_llm_provider(env)


def _check_auth_defaults(
    env: dict[str, str],
    *,
    stack_environment: str,
    allow_initial_auth_bootstrap: bool = False,
) -> CheckResult:
    return _static._check_auth_defaults(
        env,
        stack_environment=stack_environment,
        allow_initial_auth_bootstrap=allow_initial_auth_bootstrap,
    )


def _check_redaction_policy(env: dict[str, str]) -> CheckResult:
    return _static._check_redaction_policy(env)


def _check_image_pins(env: dict[str, str], *, stack_environment: str) -> CheckResult:
    return _static._check_image_pins(env, stack_environment=stack_environment)


def _check_compose_services(
    compose_text: str, services: tuple[str, ...] | list[str]
) -> CheckResult:
    return _static._check_compose_services(compose_text, services)


def _check_internal_services_not_published(
    compose_text: str, services: tuple[str, ...] | list[str], *, stack_environment: str
) -> CheckResult:
    return _static._check_internal_services_not_published(
        compose_text, services, stack_environment=stack_environment
    )


def _check_compose_config(
    options: DoctorOptions, repo_root: Path, env_path: Path, *, run: bool
) -> CheckResult:
    return _static._check_compose_config(options, repo_root, env_path, run=run)


def _check_oss_paths(repo_root: Path, *, skip: bool) -> CheckResult:
    return _static._check_oss_paths(repo_root, skip=skip)


def _run_compose_live_probe(
    *,
    options: DoctorOptions,
    repo_root: Path,
    env_path: Path,
    env: dict[str, str],
) -> list[CheckResult]:
    return _live._run_compose_live_probe(
        options=options, repo_root=repo_root, env_path=env_path, env=env
    )


def _check_api_readiness(
    base_url: str,
    *,
    deployment_profile: str = "scalable",
    timeout: float,
    allow_lifecycle_mutation: bool = False,
    allow_release_transition: bool = False,
) -> CheckResult:
    return _live._check_api_readiness(
        base_url,
        deployment_profile=deployment_profile,
        timeout=timeout,
        allow_lifecycle_mutation=allow_lifecycle_mutation,
        allow_release_transition=allow_release_transition,
    )


def _check_queue_worker_health(
    base_url: str,
    *,
    timeout: float,
    allow_lifecycle_mutation: bool = False,
    allow_release_transition: bool = False,
) -> CheckResult:
    return _live._check_queue_worker_health(
        base_url,
        timeout=timeout,
        allow_lifecycle_mutation=allow_lifecycle_mutation,
        allow_release_transition=allow_release_transition,
    )


def _check_db_connectivity(env: dict[str, str], *, timeout: float) -> CheckResult:
    return _live._check_db_connectivity(env, timeout=timeout)


def _check_queue_connectivity(env: dict[str, str], *, timeout: float) -> CheckResult:
    return _live._check_queue_connectivity(env, timeout=timeout)


def _check_mcp_auth_readiness(
    mcp_url: str,
    *,
    deployment_profile: str = "scalable",
    bearer_token: str,
    auth_disabled: bool,
    stack_environment: str,
    timeout: float,
) -> CheckResult:
    return _live._check_mcp_auth_readiness(
        mcp_url,
        deployment_profile=deployment_profile,
        bearer_token=bearer_token,
        auth_disabled=auth_disabled,
        stack_environment=stack_environment,
        timeout=timeout,
    )


def _check_compose_runtime_services(
    options: DoctorOptions, repo_root: Path, env_path: Path
) -> CheckResult:
    return _live._check_compose_runtime_services(options, repo_root, env_path)


def _compose_service_name(row: dict[str, Any]) -> Any:
    return _common._compose_service_name(row)


def _compose_service_section(compose_text: str, service: str) -> str:
    return _common._compose_service_section(compose_text, service)


def _docker_compose_command(
    options: DoctorOptions, repo_root: Path, env_path: Path, args: tuple[str, ...] | list[str]
) -> list[str]:
    return _common._docker_compose_command(options, repo_root, env_path, args)


def _env_flag(env: dict[str, str], key: str) -> bool:
    return _common._env_flag(env, key)


def _first_env(env: dict[str, str], *keys: str) -> str:
    return _common._first_env(env, *keys)


def _http_json(
    url: str, *, timeout: float, headers: dict[str, str] | None = None, body: bytes | None = None
) -> tuple[int, Any, str]:
    return _common._http_json(url, timeout=timeout, headers=headers, body=body)


def _is_real_secret_value(value: Any) -> Any:
    return _common._is_real_secret_value(value)


def _join_url(base_url: str, path: str) -> str:
    return _common._join_url(base_url, path)


def _load_env(env_path: Path, *, from_process: bool) -> dict[str, str]:
    return _common._load_env(env_path, from_process=from_process)


def _normalize_stack_environment(value: Any) -> Any:
    return _common._normalize_stack_environment(value)


def _parse_compose_ps_json(raw: str) -> list[dict[str, Any]]:
    return _common._parse_compose_ps_json(raw)


def _parse_env_file(path: Path) -> dict[str, str]:
    return _common._parse_env_file(path)


def _parse_json(value: Any) -> Any:
    return _common._parse_json(value)


def _resolve_base_url(env: dict[str, str], explicit: str) -> str:
    return _common._resolve_base_url(env, explicit)


def _resolve_bearer_token(env: dict[str, str], explicit: str) -> str:
    return _common._resolve_bearer_token(env, explicit)


def _resolve_compose_files(options: DoctorOptions, repo_root: Path) -> tuple[Path, ...]:
    return _common._resolve_compose_files(options, repo_root)


def _resolve_container_base_url(env: dict[str, str], explicit: str) -> str:
    return _common._resolve_container_base_url(env, explicit)


def _resolve_mcp_url(env: dict[str, str], explicit: str) -> str:
    return _common._resolve_mcp_url(env, explicit)


def _resolve_path(path: Path, repo_root: Path) -> Path:
    return _common._resolve_path(path, repo_root)


def _runtime_check_detail(value: Any) -> Any:
    return _common._runtime_check_detail(value)


def _runtime_meta_detail(value: Any) -> Any:
    return _common._runtime_meta_detail(value)


def _running_in_container() -> bool:
    return _common._running_in_container()


def _section_declares_env_value(section: str, key: str, expected: str) -> bool:
    return _common._section_declares_env_value(section, key, expected)


def _strip_quotes(value: Any) -> Any:
    return _common._strip_quotes(value)


def _summary(checks: list[CheckResult]) -> dict[str, int]:
    return _common._summary(checks)


def _truncate_detail(value: str, *, limit: int = 500) -> str:
    return _common._truncate_detail(value, limit=limit)


__all__ = [
    "DEFAULT_COMPOSE_PROBE_SERVICE",
    "REQUIRED_DB_TABLES",
    "CheckResult",
    "DoctorOptions",
    "run_doctor",
    "shutil",
    "subprocess",
]
