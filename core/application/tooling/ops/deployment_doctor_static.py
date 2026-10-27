#!/usr/bin/env python3
"""Static repository, env, and compose checks for deployment doctor."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

from core.application.tooling.ops.deployment_doctor_common import (
    DOCKER_COMPOSE_CONFIG_RENDERS_VALUE,
    INTERNAL_ONLY_SERVICES,
    NO_TRACKED_ENTERPRISE_PACKAGE_PATHS_VALUE,
    PLACEHOLDER_MARKERS,
    REQUIRED_REPO_FILES,
    REQUIRED_REPO_GLOBS,
    RUNTIME_IMAGES_ARE_EXPLICITLY_PINNED_VALUE,
    CheckResult,
    DoctorOptions,
    _compose_service_section,
    _docker_compose_command,
    _ports_are_localhost_only,
)
from core.infrastructure.config.env_registry import (
    StackEnvSelection,
    auth_default_expectations,
    image_pin_keys,
    redaction_policy_keys,
    required_env_keys_for_selection,
    secret_env_keys_for_selection,
)

_MESSAGE_ENV_FILE_ASSIGNS_EACH_KEY_ONCE = "Env file assigns each key once"


def _check_required_files(repo_root: Path) -> CheckResult:
    missing = [path for path in REQUIRED_REPO_FILES if not (repo_root / path).exists()]
    missing.extend(pattern for pattern in REQUIRED_REPO_GLOBS if not list(repo_root.glob(pattern)))
    if missing:
        return CheckResult(
            id="required_files",
            category="repo",
            title="Required deployment files exist",
            status="fail",
            detail=", ".join(missing),
            remediation="Restore the missing deployment, env example, schema, seed, or script files.",
        )
    return CheckResult(
        id="required_files",
        category="repo",
        title="Required deployment files exist",
        status="pass",
        detail=f"{len(REQUIRED_REPO_FILES) + len(REQUIRED_REPO_GLOBS)} required files found.",
    )


def _check_env_file(env_path: Path, *, stack_environment: str) -> CheckResult:
    label = "Local development" if stack_environment == "local_development" else "Production"
    if not env_path.exists():
        return CheckResult(
            id="env_file",
            category="env",
            title=f"{label} env file exists",
            status="fail",
            detail=str(env_path),
            remediation="Run ./install.sh to create or safely resume the selected installation.",
        )
    return CheckResult(
        id="env_file",
        category="env",
        title=f"{label} env file exists",
        status="pass",
        detail=str(env_path),
    )


def _check_env_duplicate_keys(env_path: Path) -> CheckResult:
    """Flag keys assigned more than once in the env file.

    Every reader takes the last assignment, so a file whose earlier lines say
    something different is actively misleading: an operator reading top-down sees
    a value the system does not use. That is merely confusing for APP_PORT and
    genuinely dangerous for the SECURITY_* keys, where the losing line can read
    like a hardened setting while the winning one disables it.

    Conflicting duplicates fail; repeated identical assignments only warn,
    because they are untidy rather than deceptive. Only key names and line
    numbers are reported — the file holds secrets and values must never be
    printed.
    """
    if not env_path.exists():
        return CheckResult(
            id="env_duplicate_keys",
            category="env",
            title=_MESSAGE_ENV_FILE_ASSIGNS_EACH_KEY_ONCE,
            status="pass",
            detail="No env file to inspect.",
        )
    occurrences: dict[str, list[tuple[int, str]]] = {}
    try:
        raw = env_path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return CheckResult(
            id="env_duplicate_keys",
            category="env",
            title=_MESSAGE_ENV_FILE_ASSIGNS_EACH_KEY_ONCE,
            status="fail",
            detail=f"Env file could not be read: {exc}",
            remediation="Restore read access to the env file.",
        )
    for line_number, line in enumerate(raw.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].lstrip()
        key, separator, value = stripped.partition("=")
        key = key.strip()
        if not separator or not key:
            continue
        occurrences.setdefault(key, []).append((line_number, value.strip()))

    conflicting: list[str] = []
    repeated: list[str] = []
    for key, entries in sorted(occurrences.items()):
        if len(entries) < 2:
            continue
        lines = ", ".join(str(number) for number, _ in entries)
        winner = entries[-1][0]
        rendered = f"{key} (lines {lines}; line {winner} wins)"
        if len({value for _, value in entries}) > 1:
            conflicting.append(rendered)
        else:
            repeated.append(rendered)

    if conflicting:
        return CheckResult(
            id="env_duplicate_keys",
            category="env",
            title=_MESSAGE_ENV_FILE_ASSIGNS_EACH_KEY_ONCE,
            status="fail",
            detail=(
                f"{len(conflicting)} key(s) assigned more than once with differing values: "
                + "; ".join(conflicting)
            ),
            remediation=(
                "Keep one assignment per key. Every reader uses the last one, so the earlier "
                "lines describe behaviour the deployment does not have."
            ),
        )
    if repeated:
        return CheckResult(
            id="env_duplicate_keys",
            category="env",
            title=_MESSAGE_ENV_FILE_ASSIGNS_EACH_KEY_ONCE,
            status="warn",
            detail=(
                f"{len(repeated)} key(s) assigned more than once with the same value: "
                + "; ".join(repeated)
            ),
            remediation="Remove the redundant assignments so the file has one line per key.",
        )
    return CheckResult(
        id="env_duplicate_keys",
        category="env",
        title=_MESSAGE_ENV_FILE_ASSIGNS_EACH_KEY_ONCE,
        status="pass",
        detail=f"{len(occurrences)} key(s), none assigned twice.",
    )


def _check_required_env(env: dict[str, str], selection: StackEnvSelection) -> CheckResult:
    required_keys = required_env_keys_for_selection(selection)
    missing = [key for key in required_keys if not env.get(key)]
    if missing:
        return CheckResult(
            id="required_keys",
            category="env",
            title="Required selected-stack variables are set",
            status="fail",
            detail=", ".join(missing),
            remediation="Fill every required selected-stack key in the generated env file.",
        )
    return CheckResult(
        id="required_keys",
        category="env",
        title="Required selected-stack variables are set",
        status="pass",
        detail=f"{len(required_keys)} required variables present.",
    )


def _check_secrets_changed(env: dict[str, str], selection: StackEnvSelection) -> CheckResult:
    secret_keys = secret_env_keys_for_selection(selection)
    placeholder_hits = [
        key
        for key in secret_keys
        if any(marker in str(env.get(key) or "") for marker in PLACEHOLDER_MARKERS)
    ]
    if placeholder_hits:
        return CheckResult(
            id="secret_placeholders",
            category="env",
            title="Production secrets are not placeholders",
            status="fail",
            detail=", ".join(placeholder_hits),
            remediation="Replace CHANGE_ME/example/local-dev values with deployment-specific secrets.",
        )
    return CheckResult(
        id="secret_placeholders",
        category="env",
        title="Production secrets are not placeholders",
        status="pass",
    )


def _check_llm_provider(env: dict[str, str]) -> CheckResult:
    del env
    return CheckResult(
        id="llm_provider",
        category="env",
        title="LLM providers are managed in Provider Settings",
        status="pass",
        detail="Configure a verified provider and select it in Assistant Settings after deployment.",
    )


def _bootstrap_requires_operator_secret(env: dict[str, str]) -> bool:
    """Whether first-admin bootstrap is reachable only with a setup token the operator holds.

    The two ways bootstrap_request_allowed() demands one: a token-state file (which it
    reads fail-closed -- a missing file refuses every request) or a configured token.
    """
    if str(env.get("FS_AUTH_BOOTSTRAP_STATE_FILE") or "").strip():
        return True
    token = str(env.get("FS_AUTH_BOOTSTRAP_TOKEN") or "").strip()
    return bool(token) and not any(marker in token for marker in PLACEHOLDER_MARKERS)


def _check_auth_defaults(
    env: dict[str, str],
    *,
    stack_environment: str,
    allow_initial_auth_bootstrap: bool = False,
) -> CheckResult:
    problems: list[str] = []
    expected = auth_default_expectations(stack_environment)
    for key, value in expected.items():
        # AC17-R148: the flag stays 1 on every installation after its first administrator
        # exists -- the installer writes it, Compact and Railway run with it, and a setup
        # link for a restored empty database depends on it. What closes bootstrap is the
        # endpoint's zero-user invariant and, before that, the setup token. So the flag
        # alone is not the finding; bootstrap reachable without a secret is.
        if (
            key == "FS_AUTH_BOOTSTRAP_ENABLED"
            and value == "0"
            and env.get(key) == "1"
            and (allow_initial_auth_bootstrap or _bootstrap_requires_operator_secret(env))
        ):
            continue
        if env.get(key) != value:
            problems.append(
                f"{key} should be {value}"
                + (
                    ", or bootstrap should require a setup token"
                    " (FS_AUTH_BOOTSTRAP_STATE_FILE or FS_AUTH_BOOTSTRAP_TOKEN)"
                    if key == "FS_AUTH_BOOTSTRAP_ENABLED" and value == "0"
                    else ""
                )
            )
    if problems:
        return CheckResult(
            id="auth_defaults",
            category="security",
            title="Selected-stack auth defaults are valid",
            status="fail",
            detail="; ".join(problems),
            remediation="Use the auth defaults generated by ./install.sh.",
        )
    return CheckResult(
        id="auth_defaults",
        category="security",
        title="Selected-stack auth defaults are valid",
        status="pass",
    )


def _check_redaction_policy(env: dict[str, str]) -> CheckResult:
    missing = [key for key in redaction_policy_keys() if not env.get(key)]
    if missing:
        return CheckResult(
            id="redaction_policy",
            category="security",
            title="Attachment and payload redaction policy is configured",
            status="fail",
            detail=", ".join(missing),
            remediation="Keep the redaction policy block from .env.example.prod intact.",
        )
    return CheckResult(
        id="redaction_policy",
        category="security",
        title="Attachment and payload redaction policy is configured",
        status="pass",
    )


def _check_image_pins(env: dict[str, str], *, stack_environment: str) -> CheckResult:
    if stack_environment == "local_development":
        return CheckResult(
            id="image_pins",
            category="compose",
            title=RUNTIME_IMAGES_ARE_EXPLICITLY_PINNED_VALUE,
            status="warn",
            detail="Skipped for local development source-build stack.",
        )
    image_values = {key: str(env.get(key) or "") for key in image_pin_keys()}
    missing = [key for key, value in image_values.items() if not value]
    latest = [key for key, value in image_values.items() if value.endswith(":latest")]
    if missing or latest:
        detail_parts = []
        if missing:
            detail_parts.append("missing: " + ", ".join(missing))
        if latest:
            detail_parts.append("uses latest tag: " + ", ".join(latest))
        return CheckResult(
            id="image_pins",
            category="compose",
            title=RUNTIME_IMAGES_ARE_EXPLICITLY_PINNED_VALUE,
            status="warn",
            detail="; ".join(detail_parts),
            remediation="Pin image tags or digests before production deployment.",
        )
    return CheckResult(
        id="image_pins",
        category="compose",
        title=RUNTIME_IMAGES_ARE_EXPLICITLY_PINNED_VALUE,
        status="pass",
    )


def _check_compose_services(compose_text: str, services: Sequence[str]) -> CheckResult:
    missing = [service for service in services if f"  {service}:" not in compose_text]
    if missing:
        return CheckResult(
            id="required_services",
            category="compose",
            title="Selected Compose stack includes required services",
            status="fail",
            detail=", ".join(missing),
            remediation="Restore the missing service definitions for the selected Flow Steward stack.",
        )
    return CheckResult(
        id="required_services",
        category="compose",
        title="Selected Compose stack includes required services",
        status="pass",
        detail=f"{len(tuple(services))} services found.",
    )


def _check_internal_services_not_published(
    compose_text: str, services: Sequence[str], *, stack_environment: str
) -> CheckResult:
    selected_internal_services = tuple(
        service for service in INTERNAL_ONLY_SERVICES if service in set(services)
    )
    published: list[str] = []
    localhost_published: list[str] = []
    for service in selected_internal_services:
        section = _compose_service_section(compose_text, service)
        if "ports:" not in section:
            continue
        if stack_environment == "local_development" and _ports_are_localhost_only(section):
            localhost_published.append(service)
            continue
        published.append(service)
    if published:
        return CheckResult(
            id="internal_ports",
            category="compose",
            title="Internal services are not published to the host",
            status="fail",
            detail=", ".join(published),
            remediation="Use expose-only networking for DB, queue, graph, and MCP services.",
        )
    return CheckResult(
        id="internal_ports",
        category="compose",
        title="Internal services are not published to the host",
        status="pass",
        detail=(
            "Localhost-only local development ports allowed: " + ", ".join(localhost_published)
            if localhost_published
            else ""
        ),
    )


def _check_compose_config(
    options: DoctorOptions, repo_root: Path, env_path: Path, *, run: bool
) -> CheckResult:
    if not run:
        return CheckResult(
            id="compose_config",
            category="compose",
            title=DOCKER_COMPOSE_CONFIG_RENDERS_VALUE,
            status="warn",
            detail="Skipped. Pass --run-compose-config to execute Docker Compose validation.",
        )
    command = _docker_compose_command(options, repo_root, env_path, ["config", "--quiet"])
    try:
        completed = subprocess.run(
            command,
            cwd=str(repo_root),
            check=False,
            capture_output=True,
            text=True,
            timeout=45,
        )
    except FileNotFoundError:
        return CheckResult(
            id="compose_config",
            category="compose",
            title=DOCKER_COMPOSE_CONFIG_RENDERS_VALUE,
            status="fail",
            detail="docker command not found",
            remediation="Install Docker Compose v2 on the deployment host.",
        )
    except Exception as exc:
        return CheckResult(
            id="compose_config",
            category="compose",
            title=DOCKER_COMPOSE_CONFIG_RENDERS_VALUE,
            status="fail",
            detail=str(exc),
        )
    if completed.returncode != 0:
        output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
        return CheckResult(
            id="compose_config",
            category="compose",
            title=DOCKER_COMPOSE_CONFIG_RENDERS_VALUE,
            status="fail",
            detail=output[:1000],
            remediation="Fix compose/env interpolation errors before deploying.",
        )
    return CheckResult(
        id="compose_config",
        category="compose",
        title=DOCKER_COMPOSE_CONFIG_RENDERS_VALUE,
        status="pass",
    )


def _check_oss_paths(repo_root: Path, *, skip: bool) -> CheckResult:
    if skip:
        return CheckResult(
            id="no_enterprise_paths",
            category="oss",
            title=NO_TRACKED_ENTERPRISE_PACKAGE_PATHS_VALUE,
            status="warn",
            detail="Skipped by --skip-git.",
        )
    try:
        completed = subprocess.run(
            ["git", "ls-files"],
            cwd=str(repo_root),
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except FileNotFoundError:
        return CheckResult(
            id="no_enterprise_paths",
            category="oss",
            title=NO_TRACKED_ENTERPRISE_PACKAGE_PATHS_VALUE,
            status="warn",
            detail="git command not found; skipped tracked-file guard.",
        )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        if "not a git repository" in detail.lower() or not (repo_root / ".git").exists():
            return CheckResult(
                id="no_enterprise_paths",
                category="oss",
                title=NO_TRACKED_ENTERPRISE_PACKAGE_PATHS_VALUE,
                status="warn",
                detail="Tracked-file guard skipped: no git metadata in this source/archive checkout.",
            )
        return CheckResult(
            id="no_enterprise_paths",
            category="oss",
            title=NO_TRACKED_ENTERPRISE_PACKAGE_PATHS_VALUE,
            status="warn",
            detail=detail[:500],
        )
    leaked = [
        path
        for path in completed.stdout.splitlines()
        if any(part == "enterprise" for part in path.split("/"))
    ]
    if leaked:
        return CheckResult(
            id="no_enterprise_paths",
            category="oss",
            title=NO_TRACKED_ENTERPRISE_PACKAGE_PATHS_VALUE,
            status="fail",
            detail=", ".join(leaked[:20]),
            remediation="Move enterprise-only paths out of the OSS repository.",
        )
    return CheckResult(
        id="no_enterprise_paths",
        category="oss",
        title=NO_TRACKED_ENTERPRISE_PACKAGE_PATHS_VALUE,
        status="pass",
    )
