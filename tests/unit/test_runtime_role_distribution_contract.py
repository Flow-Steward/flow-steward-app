from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from tests.support.source_index import whole_words_in

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "infra" / "docker" / "Dockerfile"
BASE_COMPOSE = ROOT / "infra" / "docker" / "docker-compose.yaml"
PRODUCTION_COMPOSE = ROOT / "infra" / "docker" / "docker-compose.prod.yaml"
COMPACT_COMPOSE = ROOT / "infra" / "docker" / "docker-compose.compact.yaml"

ROLE_TARGETS = {
    "fs-app": "app-runtime",
    "fs-frontend": "frontend-development",
    "fs-worker": "worker-runtime",
    "control-plane": "control-runtime",
}
BUILD_CACHE_IMPORT_PATHS = {
    "fs-app": "${FS_INSTALLER_STATE_DIR:-.flow-steward}/build-cache/app",
    "fs-cli": "${FS_INSTALLER_STATE_DIR:-.flow-steward}/build-cache/cli",
    "fs-frontend": "${FS_INSTALLER_STATE_DIR:-.flow-steward}/build-cache/frontend",
    "fs-test-runner": "${FS_INSTALLER_STATE_DIR:-.flow-steward}/build-cache/audit",
    "fs-audit": "${FS_INSTALLER_STATE_DIR:-.flow-steward}/build-cache/audit",
    "fs-worker": "${FS_INSTALLER_STATE_DIR:-.flow-steward}/build-cache/worker",
    "control-plane": "${FS_INSTALLER_STATE_DIR:-.flow-steward}/build-cache/control",
    "fs-mcp": "${FS_INSTALLER_STATE_DIR:-.flow-steward}/build-cache/mcp",
}
BUILD_CACHE_EXPORT_PATHS = {
    service_name: cache_path
    for service_name, cache_path in BUILD_CACHE_IMPORT_PATHS.items()
    if service_name != "fs-test-runner"
}
ROLE_SLICE_OUTPUTS = {
    "app-runtime": "/out/app",
    "worker-runtime": "/out/worker",
    "control-runtime": "/out/control",
}
PRODUCTION_ROLE_IMAGES = {
    "fs-app": "${FS_APP_IMAGE:?FS_APP_IMAGE is required for immutable production provenance}",
    "fs-worker": (
        "${FS_WORKER_IMAGE:?FS_WORKER_IMAGE is required for immutable production provenance}"
    ),
    "control-plane": (
        "${FS_CONTROL_IMAGE:?FS_CONTROL_IMAGE is required for immutable production provenance}"
    ),
}
PRODUCTION_ROLE_DIGESTS = {
    "fs-app": "FS_APP_IMAGE_DIGEST",
    "fs-worker": "FS_WORKER_IMAGE_DIGEST",
    "control-plane": "FS_CONTROL_IMAGE_DIGEST",
}
COMMON_ROLE_ENV_KEYS = {
    "FS_COMPOSE_PROJECT_NAME",
    "FS_DEPLOYMENT_PROFILE",
    "FS_INSTALLATION_ID",
    "FS_MONITORING_EXPECTATIONS",
    "FS_MONITORING_DEPENDENCIES",
    "FS_MONITORING_INTERVAL_SECONDS",
    "FS_MONITORING_STALE_SECONDS",
    "FS_MONITORING_HISTORY_ENABLED",
    "FS_MONITORING_CURRENT_RETENTION_DAYS",
    "FS_MONITORING_HISTORY_RETENTION_DAYS",
    "FS_MONITORING_CLEANUP_BATCH_SIZE",
    "FS_MONITORING_QUEUE_MAX_MESSAGES",
    "FS_MONITORING_QUEUE_MAX_BYTES",
    "FS_MONITORING_OPERATION_TIMEOUT_SECONDS",
    "ARTIFACT_LOCAL_ROOT_DIR",
    "ARTIFACT_S3_ACCESS_KEY",
    "ARTIFACT_S3_BUCKET",
    "ARTIFACT_S3_ENDPOINT_URL",
    "ARTIFACT_S3_PUBLIC_ENDPOINT_URL",
    "ARTIFACT_S3_KEY_PREFIX",
    "ARTIFACT_S3_PRESIGN_TTL_SECONDS",
    "ARTIFACT_S3_REGION",
    "ARTIFACT_S3_SECRET_KEY",
    "ARTIFACT_S3_USE_SSL",
    "ARTIFACT_STORAGE_BACKEND",
    "FS_DB_DSN",
    "FS_LOAD_REPO_DOTENV",
    "FS_LOG_FORMAT",
    "FS_LOG_LEVEL",
    "FS_OBSERVABILITY_DSN",
    "LANGGRAPH_POSTGRES_URI",
    "PYTHONPYCACHEPREFIX",
    "FS_RABBITMQ_TRANSPORT_MODE",
    "RABBITMQ_URL",
    "SECURITY_PII_SALT",
    "SENTRY_DSN",
    "SENTRY_ENVIRONMENT",
}
APP_WORKER_ENV_KEYS = {
    "ENCRYPTION_KEY",
    "FS_WORKFLOW_DISCOVERY_LEXICAL_FALLBACK_ENABLED",
    "FS_ALLOW_PRIVATE_REMOTE_URLS",
    "FS_CONTEXT_GRAPH_BACKEND",
    "FS_DEFAULT_MODEL",
    "FS_DEFAULT_PROVIDER",
    "FS_DEFAULT_TEMPERATURE",
    # The catalog URLs live in the compose x-app-worker-environment anchor, so
    # fs-app and fs-worker carry them and control-plane does not.
    "FS_EXTENSION_CATALOG_URL",
    "FS_MCP_CATALOG_URL",
    "FS_WORKFLOW_TEMPLATE_CATALOG_URL",
    "FS_EXTENSION_DEPS_FETCH",
    "FS_EXTENSION_DEPS_SITE_MAX_BYTES",
    "FS_EXTENSION_ARTIFACT_MAX_BYTES",
    "FS_BASE64_FILE_MAX_BYTES",
    "FS_WORKFLOW_TEST_FILE_MAX_BYTES",
    "FS_FILE_PREVIEW_MAX_BYTES",
    "FS_EXTENSION_OPERATION_TIMEOUT_SECONDS",
    "FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS",
    "FS_EXTENSIONS_ROOT",
    "FS_FEED_MAX_BYTES",
    "FS_FEED_MAX_ROWS",
    "FS_MEMORY_KNOWLEDGE_BACKEND",
    "FS_MEMORY_PROFILE",
    "FS_MEMORY_REPOSITORY_BACKEND",
    "FS_MEMORY_TEXT_SEARCH_BACKEND",
    "FS_MEMORY_VECTOR_BACKEND",
    "FS_LLM_REQUEST_TIMEOUT_SECONDS",
    "FS_PGVECTOR_DSN",
    "FS_SECRET_KEY",
    "FS_TABULAR_FEED_MAX_MATERIALIZE_BYTES",
    "FS_TABULAR_FEED_READ_ROWS_SCAN_MAX_BYTES",
    "FS_TABULAR_FEED_READ_ROWS_SCAN_MAX_ROWS",
    "MCP_URL",
}
APP_ENV_KEYS = (
    COMMON_ROLE_ENV_KEYS
    | APP_WORKER_ENV_KEYS
    | {
        "CONTROL_LOOPS_ENABLED",
        "ENVIRONMENT",
        "FS_AUTH_BOOTSTRAP_ALLOW_TRUSTED_PROXY",
        "FS_AUTH_BOOTSTRAP_ENABLED",
        "FS_AUTH_BOOTSTRAP_LOCALHOST_ONLY",
        "FS_AUTH_BOOTSTRAP_STATE_FILE",
        "FS_AUTH_BOOTSTRAP_TOKEN",
        "FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT",
        "FS_AUTH_COOKIE_SECURE",
        "FS_AUTH_TRUSTED_ORIGINS",
        "FS_DOCS_INTERNAL_ENABLED",
        "FS_DOCS_PUBLIC_ENABLED",
        "FS_EXTENSION_INSTALLER_PROXY_ENABLED",
        "FS_GZIP_ENABLED",
        "FS_LOCAL_AUTH_ENABLED",
        "FS_METRICS_TOKEN",
        "FS_OPENAPI_INTERNAL_ENABLED",
        "FS_OPENAPI_PUBLIC_ENABLED",
        "FS_PROCESS_METRICS_HOST",
        "FS_PUBLIC_BASE_URL",
        "FS_SERVE_SSL",
        "FS_STACK_ENVIRONMENT",
        "HOME",
        "PORT",
        "WORKERS",
    }
)
# AC17-R123: both roles that run extension steps are told the address the platform
# serves artifact bodies from, in development as well as production, because both
# composes describe deployments where storage may not be able to presign.
APP_ENV_KEYS = APP_ENV_KEYS | {"FS_APP_INTERNAL_URL"}
# External review P2: the proxies `config` recorded as trusted reach the app, whose
# start script adds them to uvicorn's --forwarded-allow-ips.
APP_ENV_KEYS = APP_ENV_KEYS | {"FS_PROXY_TRUSTED_IPS"}
# Retention defaults the settings page shows for accounts with no saved policy; the
# control plane applies them with the platform-wide windows only it reads.
RETENTION_POLICY_ENV_KEYS = {
    "FS_RETENTION_LOGS_DAYS",
    "FS_RETENTION_MESSAGES_DAYS",
    "FS_RETENTION_TRASH_DAYS",
    "FS_RETENTION_TERMINAL_JOB_CHECKPOINTS_DAYS",
    "FS_RETENTION_REVISIONS_KEEP",
}
APP_ENV_KEYS = APP_ENV_KEYS | RETENTION_POLICY_ENV_KEYS
WORKER_ENV_KEYS = (
    COMMON_ROLE_ENV_KEYS
    | APP_WORKER_ENV_KEYS
    | {
        "FS_EXTENSION_INBOUND_POLL_ENABLED",
        "FS_MCP_GROUNDING_REQUIRED",
        "FS_METRICS_TOKEN",
        "FS_PROCESS_METRICS_HOST",
        "FS_WORKER_METRICS_PORT",
        "MCP_BEARER_TOKEN",
    }
)
WORKER_ENV_KEYS = WORKER_ENV_KEYS | {"FS_APP_INTERNAL_URL"}
CONTROL_ENV_KEYS = COMMON_ROLE_ENV_KEYS | {
    "ENCRYPTION_KEY",
    "FS_CONTROL_METRICS_PORT",
    "FS_DASHBOARD_ROLLUP_BACKFILL_DAYS",
    "FS_DASHBOARD_ROLLUP_INTERVAL_SECONDS",
    "FS_DASHBOARD_ROLLUP_LOOKBACK_DAYS",
    "FS_EXTERNAL_EFFECT_MAINTENANCE_ENABLED",
    "FS_EXTERNAL_EFFECT_MAINTENANCE_INTERVAL_SECONDS",
    "FS_METRICS_TOKEN",
    "FS_PROCESS_METRICS_HOST",
    "FS_SCHEDULED_JOBS_BATCH_LIMIT",
    "FS_SCHEDULED_JOBS_INTERVAL_SECONDS",
    "FS_SECURITY_POSTURE_ENABLED",
    "FS_SECURITY_POSTURE_INTERVAL_SECONDS",
    "FS_WATCHDOG_INTERVAL_SECONDS",
    "FS_WATCHDOG_MAX_JOURNAL_ENTRIES",
    "FS_WATCHDOG_ORPHAN_GRACE_SECONDS",
    "FS_WATCHDOG_PICKUP_GRACE_SECONDS",
    "FS_WATCHDOG_STUCK_SECONDS",
    "FS_WORKFLOW_KNOWLEDGE_SYNC_CHANGELOG_LIMIT",
    "FS_WORKFLOW_KNOWLEDGE_SYNC_ENABLED",
    "FS_WORKFLOW_KNOWLEDGE_SYNC_INTERVAL_SECONDS",
}
CONTROL_ENV_KEYS = (
    CONTROL_ENV_KEYS
    | RETENTION_POLICY_ENV_KEYS
    | {
        "FS_RETENTION_ENABLED",
        "FS_RETENTION_INTERVAL_SECONDS",
        "FS_RETENTION_JOBS_DAYS",
        "FS_RETENTION_EVENTS_DAYS",
        "FS_RETENTION_RETRY_QUEUE_DAYS",
    }
)
DEVELOPMENT_ROLE_ENV_KEYS = {
    "fs-app": APP_ENV_KEYS | {"TEST_RUNNER_BASE_URL"},
    "fs-frontend": {"FS_AUTO_RELOAD", "VITE_BACKEND_TARGET"},
    "fs-worker": WORKER_ENV_KEYS | {"FS_PUBLIC_BASE_URL"},
    "control-plane": CONTROL_ENV_KEYS,
}
# HOME is production-only for the worker and the control plane, and deliberately so:
# only the production compose pins them to the operator's uid, which the image's
# /etc/passwd does not know. In development they run as the image's own user, whose
# home exists, so a HOME there would be noise rather than a rule. The app carries it in
# both, because it is pinned to the operator in both.
PRODUCTION_ROLE_ENV_KEYS = {
    "fs-app": APP_ENV_KEYS | {"FS_APP_IMAGE_DIGEST"},
    "fs-worker": WORKER_ENV_KEYS | {"FS_WORKER_IMAGE_DIGEST", "HOME", "FS_PUBLIC_BASE_URL"},
    "control-plane": CONTROL_ENV_KEYS | {"FS_CONTROL_IMAGE_DIGEST", "HOME"},
}
ROLE_MANIFESTS = {
    "fs-app": "app",
    "fs-worker": "worker",
    "control-plane": "control",
}
APT_ARCHIVE_CACHE_MOUNT = (
    "--mount=type=cache,id=flow-steward-apt-bookworm-archives,target=/var/cache/apt,sharing=locked"
)
APT_INDEX_CACHE_MOUNT = (
    "--mount=type=cache,id=flow-steward-apt-bookworm-index,target=/var/lib/apt,sharing=locked"
)
PIP_CACHE_MOUNT = (
    "--mount=type=cache,id=flow-steward-pip-python312,target=/root/.cache/pip,sharing=locked"
)
TECHNICAL_ENV_KEYS = {
    "FS_APP_IMAGE_DIGEST",
    "FS_CONTROL_IMAGE_DIGEST",
    "FS_WORKER_IMAGE_DIGEST",
    "HOME",
    "PYTHONPYCACHEPREFIX",
}
FORBIDDEN_RUNTIME_COPY_ROOTS = {
    ".",
    "backups",
    "core",
    "docs",
    "extensions",
    "logs",
    "tests",
}
FORBIDDEN_ROLE_SOURCE_TREES = {
    "core/application",
    "core/infrastructure",
}
FRONTEND_FORBIDDEN_ENV_PREFIXES = (
    "ANTHROPIC_",
    "ARTIFACT_",
    "CLAU" + "DE_",
    "CO" + "DEX_",
    "DATABASE_",
    "FS_AI_",
    "FS_AUTH_",
    "FS_DB_",
    "FS_DEFAULT_",
    "FS_EXTENSION_",
    "FS_MCP_",
    "FS_MEMORY_",
    "GOOGLE_",
    "LANGGRAPH_",
    "MCP_",
    "OPENAI_",
    "POSTGRES_",
    "RABBITMQ_",
)


@dataclass(frozen=True)
class DockerStage:
    parent: str
    copy_sources: tuple[str, ...]
    instructions: tuple[str, ...]


def _compose(path: Path) -> dict:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    assert isinstance(payload, dict)
    return payload


def test_removed_dead_role_environment_keys_are_not_distributed() -> None:
    dead_key = "FS_AI_BUILDER_STAGE_PIPELINE_ENABLED"
    manifest = json.loads(
        (ROOT / "core/infrastructure/config/flow_steward_env_manifest.json").read_text(
            encoding="utf-8"
        )
    )

    assert dead_key not in BASE_COMPOSE.read_text(encoding="utf-8")
    assert dead_key not in PRODUCTION_COMPOSE.read_text(encoding="utf-8")
    assert dead_key not in {entry["name"] for entry in manifest["variables"]}


def _logical_dockerfile_lines(path: Path) -> tuple[str, ...]:
    logical: list[str] = []
    pending = ""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        pending = f"{pending} {line}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        logical.append(pending)
        pending = ""
    assert not pending, "Dockerfile ends with an incomplete instruction"
    return tuple(logical)


def _copy_sources(instruction: str) -> tuple[str, ...]:
    body = instruction.partition(" ")[2].strip()
    if body.startswith("["):
        items = json.loads(body)
        return tuple(str(item) for item in items[:-1])
    tokens = shlex.split(body)
    paths = [token for token in tokens if not token.startswith("--")]
    return tuple(paths[:-1])


def _docker_stages(path: Path) -> dict[str, DockerStage]:
    stage_parents: dict[str, str] = {}
    stage_copies: dict[str, list[str]] = {}
    stage_instructions: dict[str, list[str]] = {}
    current_stage: str | None = None
    for instruction in _logical_dockerfile_lines(path):
        from_match = re.fullmatch(
            r"FROM\s+(?P<parent>\S+)(?:\s+AS\s+(?P<name>\S+))?",
            instruction,
            flags=re.IGNORECASE,
        )
        if from_match is not None:
            current_stage = from_match.group("name")
            if current_stage is not None:
                stage_parents[current_stage] = from_match.group("parent")
                stage_copies[current_stage] = []
                stage_instructions[current_stage] = []
            continue
        if current_stage is not None:
            stage_instructions[current_stage].append(instruction)
            if instruction.upper().startswith("COPY "):
                stage_copies[current_stage].extend(_copy_sources(instruction))
    return {
        name: DockerStage(
            parent=parent,
            copy_sources=tuple(stage_copies[name]),
            instructions=tuple(stage_instructions[name]),
        )
        for name, parent in stage_parents.items()
    }


def _inherited_copy_sources(stages: dict[str, DockerStage], target: str) -> set[str]:
    assert target in stages, f"Dockerfile is missing the explicit {target!r} target"
    sources: set[str] = set()
    visited: set[str] = set()
    current = target
    while current in stages:
        assert current not in visited, f"Dockerfile stage inheritance cycle at {current!r}"
        visited.add(current)
        stage = stages[current]
        sources.update(source.removesuffix("/") for source in stage.copy_sources)
        current = stage.parent
    return sources


def _stage_lineage(stages: dict[str, DockerStage], target: str) -> tuple[str, ...]:
    assert target in stages, f"Dockerfile is missing the explicit {target!r} target"
    lineage: list[str] = []
    current = target
    while current in stages:
        assert current not in lineage, f"Dockerfile stage inheritance cycle at {current!r}"
        lineage.append(current)
        current = stages[current].parent
    return tuple(lineage)


def test_development_services_build_explicit_role_targets() -> None:
    services = _compose(BASE_COMPOSE)["services"]

    actual = {
        service_name: services[service_name]["build"]["target"] for service_name in ROLE_TARGETS
    }

    assert actual == ROLE_TARGETS


def test_local_builds_export_and_reimport_persistent_cache_metadata() -> None:
    services = _compose(BASE_COMPOSE)["services"]

    for service_name, cache_path in BUILD_CACHE_IMPORT_PATHS.items():
        build = services[service_name]["build"]
        assert build["cache_from"] == [f"type=local,src={cache_path}"]

    for service_name, cache_path in BUILD_CACHE_EXPORT_PATHS.items():
        build = services[service_name]["build"]
        assert build["cache_to"] == [f"type=local,dest={cache_path},mode=min"]

    test_build = services["fs-test-runner"]["build"]
    audit_build = services["fs-audit"]["build"]
    assert "cache_to" not in test_build
    for key in ("dockerfile", "target", "args"):
        assert test_build[key] == audit_build[key]


def test_role_targets_do_not_inherit_broad_or_forbidden_repository_copies() -> None:
    stages = _docker_stages(DOCKERFILE)

    for target in ROLE_TARGETS.values():
        copied_sources = _inherited_copy_sources(stages, target)
        forbidden = copied_sources & FORBIDDEN_RUNTIME_COPY_ROOTS
        assert not forbidden, f"{target} inherits forbidden copy roots: {sorted(forbidden)}"


def test_retained_cli_includes_release_credential_secret_reader() -> None:
    sources = _inherited_copy_sources(_docker_stages(DOCKERFILE), "cli")
    assert "core/infrastructure/security/owner_only_secret.py" in sources


def test_compact_container_projects_cookie_override_with_secure_default() -> None:
    services = yaml.safe_load(COMPACT_COMPOSE.read_text(encoding="utf-8"))["services"]

    assert services["compact-runtime"]["environment"].get("FS_AUTH_COOKIE_SECURE") == (
        "${FS_AUTH_COOKIE_SECURE:-1}"
    )


def test_compact_cli_uses_exact_compact_image_when_no_separate_cli_is_selected() -> None:
    services = yaml.safe_load(COMPACT_COMPOSE.read_text(encoding="utf-8"))["services"]
    assert services["fs-cli"]["image"] == (
        "${FS_CLI_IMAGE:-${FS_COMPACT_IMAGE:?FS_COMPACT_IMAGE is required for immutable production provenance}}"
    )


def test_role_targets_do_not_copy_whole_application_or_infrastructure_packages() -> None:
    stages = _docker_stages(DOCKERFILE)

    for target in ROLE_TARGETS.values():
        copied_sources = _inherited_copy_sources(stages, target)
        forbidden = copied_sources & FORBIDDEN_ROLE_SOURCE_TREES
        assert not forbidden, f"{target} copies whole source trees: {sorted(forbidden)}"


def test_role_targets_do_not_copy_unproven_recursive_core_subpackages() -> None:
    stages = _docker_stages(DOCKERFILE)

    for target in ROLE_TARGETS.values():
        recursive_packages = {
            source
            for source in _inherited_copy_sources(stages, target)
            if source.startswith(("core/application/", "core/infrastructure/"))
            and Path(source).suffix == ""
        }
        assert not recursive_packages, (
            f"{target} recursively copies unproven core packages: {sorted(recursive_packages)}"
        )


def test_role_targets_copy_only_their_filtered_source_slice() -> None:
    stages = _docker_stages(DOCKERFILE)

    for target, output_path in ROLE_SLICE_OUTPUTS.items():
        target_copy_instructions = [
            instruction
            for instruction in stages[target].instructions
            if instruction.upper().startswith("COPY ")
        ]
        source_slice_copies = [
            instruction
            for instruction in target_copy_instructions
            if "--from=source-slices" in instruction
        ]

        assert len(source_slice_copies) == 1
        assert output_path in source_slice_copies[0]
        assert not any(
            source.removesuffix("/").startswith(("core", "db", "locales", "VERSION"))
            for source in _inherited_copy_sources(stages, target)
        )


def test_source_slice_manifests_and_helper_are_build_inputs() -> None:
    stages = _docker_stages(DOCKERFILE)
    source_slice_body = "\n".join(stages["source-slices"].instructions)

    assert "infra/docker/materialize_source_slice.py" in source_slice_body
    assert "infra/docker/source-slices/" in source_slice_body
    # Manifests and their files must be materialized from the copied build-stage
    # snapshot, not a later context bind view after a merged source addition.
    for instruction in (
        "COPY core/ /src/core/",
        "COPY db/ /src/db/",
        "COPY locales/ /src/locales/",
        "COPY VERSION /src/VERSION",
    ):
        assert instruction in stages["source-slices"].instructions
    assert "--mount=type=bind" not in source_slice_body
    assert "--source-root /src" in source_slice_body


@pytest.mark.parametrize("failed_role", ("app", "worker", "control", "compact"))
def test_source_slice_stage_stops_at_the_first_materializer_failure(failed_role: str) -> None:
    stages = _docker_stages(DOCKERFILE)
    instruction = next(
        value
        for value in stages["source-slices"].instructions
        if value.startswith("RUN ") and "for role in app worker control compact" in value
    )
    command, replacements = re.subn(
        r"python /usr/local/bin/materialize_source_slice\.py materialize "
        r'.*?--output-root "/out/\$\{role\}"',
        "role_materializer",
        instruction.removeprefix("RUN "),
    )
    assert replacements == 1
    stub = (
        "role_materializer() { printf '%s\\n' \"$role\"; "
        f'if [ "$role" = {shlex.quote(failed_role)} ]; then return 7; fi; return 0; }}; '
    )
    result = subprocess.run(
        ["/bin/sh", "-c", stub + command],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 7, result.stdout + result.stderr
    roles = ["app", "worker", "control", "compact"]
    assert result.stdout.splitlines() == roles[: roles.index(failed_role) + 1]


def test_role_targets_have_honest_dependency_and_tooling_parents() -> None:
    stages = _docker_stages(DOCKERFILE)

    assert stages["app-runtime"].parent == "runtime-dependencies"
    assert stages["control-runtime"].parent == "control-dependencies"
    assert stages["worker-runtime"].parent == "worker-system-tooling"
    assert stages["frontend-development"].parent == "frontend-dependencies"

    control_lineage_body = "\n".join(
        instruction
        for stage_name in _stage_lineage(stages, "control-runtime")
        for instruction in stages[stage_name].instructions
    )
    assert "requirements-control.lock" in control_lineage_body
    assert "requirements-runtime.lock" not in control_lineage_body

    for target in ("app-runtime", "control-runtime", "frontend-development"):
        lineage_body = "\n".join(
            instruction
            for stage_name in _stage_lineage(stages, target)
            for instruction in stages[stage_name].instructions
        )
        assert "docker-ce-cli" not in lineage_body
        assert "nodejs" not in lineage_body


def test_role_dependency_and_system_tool_downloads_have_persistent_buildkit_caches() -> None:
    stages = _docker_stages(DOCKERFILE)

    assert 'APT::Keep-Downloaded-Packages "true"' in "\n".join(stages["base"].instructions)

    # runtime-dependencies installs no system package at all: the runtime images carry no git
    assert not any(
        "apt-get install" in instruction
        for instruction in stages["runtime-dependencies"].instructions
    )
    for stage_name in ("base", "cli", "worker-system-tooling"):
        apt_installs = [
            instruction
            for instruction in stages[stage_name].instructions
            if "apt-get install" in instruction
        ]
        assert apt_installs, f"{stage_name} must contain its expected apt install"
        for instruction in apt_installs:
            assert APT_ARCHIVE_CACHE_MOUNT in instruction
            assert APT_INDEX_CACHE_MOUNT in instruction
            assert "rm -rf /var/lib/apt/lists" not in instruction

    for stage_name, lock_name in (
        ("cli", "requirements-cli.lock"),
        ("runtime-dependencies", "requirements-runtime.lock"),
        ("control-dependencies", "requirements-control.lock"),
    ):
        installs = [
            instruction
            for instruction in stages[stage_name].instructions
            if f"-r {lock_name}" in instruction
        ]
        assert len(installs) == 1
        assert PIP_CACHE_MOUNT in installs[0]
        assert "--no-cache-dir" not in installs[0]


def test_role_distribution_has_no_retired_executor_build_or_runtime_inputs() -> None:
    first = "co" + "dex"
    second = "clau" + "de"
    forbidden = (
        first,
        f"{second} cli",
        f"{second} code",
        f"{second}_cli",
        f"{second}_home",
        f"@anthropic-ai/{second}-code",
    )
    surfaces = [DOCKERFILE, BASE_COMPOSE, PRODUCTION_COMPOSE]
    surfaces.extend((ROOT / "infra" / "docker" / "source-slices").glob("*.json"))
    surfaces.extend(ROOT.glob("requirements-*.lock"))
    offenders = {
        path.relative_to(ROOT).as_posix(): [token for token in forbidden if token in text]
        for path in surfaces
        if (text := path.read_text(encoding="utf-8", errors="ignore").lower())
        and any(token in text for token in forbidden)
    }

    assert not offenders


def test_worker_and_audit_keep_only_their_proven_system_tooling() -> None:
    stages = _docker_stages(DOCKERFILE)
    audit_lineage = "\n".join(
        instruction
        for stage_name in _stage_lineage(stages, "audit")
        for instruction in stages[stage_name].instructions
    )
    worker_lineage = "\n".join(
        instruction
        for stage_name in _stage_lineage(stages, "worker-runtime")
        for instruction in stages[stage_name].instructions
    )

    assert "docker-ce-cli" in audit_lineage
    assert "nodejs" in audit_lineage
    assert "docker-ce-cli" in worker_lineage
    assert "docker-compose-plugin" in worker_lineage
    assert "nodejs" not in worker_lineage


def test_worker_slice_ships_no_preflight_and_keeps_its_docker_tooling() -> None:
    stages = _docker_stages(DOCKERFILE)
    worker_manifest = json.loads(
        (ROOT / "infra/docker/source-slices/worker.json").read_text(encoding="utf-8")
    )
    worker_paths = set(worker_manifest["static_paths"])
    worker_lineage = "\n".join(
        instruction
        for stage_name in _stage_lineage(stages, "worker-runtime")
        for instruction in stages[stage_name].instructions
    )

    assert worker_paths  # non-vacuity
    assert {
        "core/application/orchestration/agents/preflight_node.py",
        "core/application/orchestration/agents/preflight_docker.py",
    }.isdisjoint(worker_paths)
    assert "docker-ce-cli" in worker_lineage
    assert "docker-compose-plugin" in worker_lineage
    assert "nodejs" not in worker_lineage


def test_frontend_target_is_node_only_and_independent_of_python_runtime_lock() -> None:
    stages = _docker_stages(DOCKERFILE)
    lineage = _stage_lineage(stages, "frontend-development")
    lineage_body = "\n".join(
        instruction for stage_name in lineage for instruction in stages[stage_name].instructions
    )

    assert "base" not in lineage
    assert "runtime-dependencies" not in lineage
    assert "requirements-runtime.lock" not in lineage_body
    assert stages[lineage[-1]].parent.startswith("node:22-bookworm-slim@sha256:")


def test_frontend_and_app_artifacts_include_the_platform_kit_runtime_assets() -> None:
    stages = _docker_stages(DOCKERFILE)
    frontend_build = "\n".join(stages["frontend-build"].instructions)
    frontend_development = "\n".join(stages["frontend-development"].instructions)
    app_runtime = "\n".join(stages["app-runtime"].instructions)

    assert "COPY assets/platform-kit/ assets/platform-kit/" in frontend_build
    assert "COPY assets/platform-kit/ assets/platform-kit/" in frontend_development
    assert (
        "COPY --chmod=u=rwX,go=rX --chown=fs:fs --from=frontend-build /app/assets/platform-kit "
        "assets/platform-kit" in app_runtime
    )


def test_frontend_development_service_has_a_closed_non_secret_environment() -> None:
    frontend = _compose(BASE_COMPOSE)["services"]["fs-frontend"]
    environment = frontend.get("environment") or {}

    assert "env_file" not in frontend
    assert set(environment) == DEVELOPMENT_ROLE_ENV_KEYS["fs-frontend"]
    assert not any(key.startswith(FRONTEND_FORBIDDEN_ENV_PREFIXES) for key in environment)


def test_development_role_processes_use_exact_environment_allowlists() -> None:
    services = _compose(BASE_COMPOSE)["services"]

    for service_name, expected in DEVELOPMENT_ROLE_ENV_KEYS.items():
        service = services[service_name]
        assert "env_file" not in service
        assert set(service.get("environment") or {}) == expected
        if service_name == "fs-app":
            assert (
                service["environment"]["ENVIRONMENT"]
                == "${ENVIRONMENT-${FS_STACK_ENVIRONMENT-production}}"
            )
            assert (
                service["environment"]["FS_STACK_ENVIRONMENT"]
                == "${FS_STACK_ENVIRONMENT-${ENVIRONMENT-production}}"
            )


def test_production_services_use_explicit_role_images_and_provenance() -> None:
    services = _compose(PRODUCTION_COMPOSE)["services"]

    for service_name, expected_image in PRODUCTION_ROLE_IMAGES.items():
        service = services[service_name]
        environment = service.get("environment") or {}
        digest_key = PRODUCTION_ROLE_DIGESTS[service_name]

        assert service["image"] == expected_image
        assert digest_key in environment
        assert "FS_IMAGE_DIGEST" not in environment


def test_production_role_processes_do_not_export_the_candidate_env_file() -> None:
    services = _compose(PRODUCTION_COMPOSE)["services"]

    for service_name, expected in PRODUCTION_ROLE_ENV_KEYS.items():
        service = services[service_name]
        assert "env_file" not in service
        assert set(service.get("environment") or {}) == expected
        if service_name == "fs-app":
            assert (
                service["environment"]["ENVIRONMENT"]
                == "${ENVIRONMENT-${FS_STACK_ENVIRONMENT-production}}"
            )
            assert (
                service["environment"]["FS_STACK_ENVIRONMENT"]
                == "${FS_STACK_ENVIRONMENT-${ENVIRONMENT-production}}"
            )


def test_no_role_receives_a_host_extension_policy_path() -> None:
    for compose_path in (BASE_COMPOSE, PRODUCTION_COMPOSE):
        services = _compose(compose_path)["services"]
        for service in services.values():
            assert "FS_EXTENSION_INSTALL_POLICY_PATH" not in (service.get("environment") or {})


def test_role_environment_allowlists_have_reachable_static_consumers() -> None:
    for service_name, role in ROLE_MANIFESTS.items():
        manifest = json.loads(
            (ROOT / "infra" / "docker" / "source-slices" / f"{role}.json").read_text(
                encoding="utf-8"
            )
        )
        paths = set(manifest["static_paths"])
        paths.update(item["path"] for item in manifest["dynamic_paths"])
        paths.update(item["path"] for item in manifest["package_data_paths"])
        consumer_paths = {path for path in paths if Path(path).suffix in {".py", ".sh"}}
        source = "\n".join(
            (ROOT / path).read_text(encoding="utf-8", errors="ignore")
            for path in sorted(consumer_paths)
        )
        expected = DEVELOPMENT_ROLE_ENV_KEYS[service_name] | PRODUCTION_ROLE_ENV_KEYS.get(
            service_name, set()
        )

        assert whole_words_in(source, expected - TECHNICAL_ENV_KEYS) == (
            expected - TECHNICAL_ENV_KEYS
        )


def test_environment_manifest_owns_explicit_role_images_without_legacy_aliases() -> None:
    manifest_path = ROOT / "core" / "infrastructure" / "config" / "flow_steward_env_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    names = {str(row.get("name") or "") for row in manifest.get("variables") or []}

    assert {
        "FS_APP_IMAGE",
        "FS_APP_IMAGE_DIGEST",
        "FS_WORKER_IMAGE",
        "FS_WORKER_IMAGE_DIGEST",
        "FS_CONTROL_IMAGE",
        "FS_CONTROL_IMAGE_DIGEST",
    }.issubset(names)
    assert {"FS_IMAGE", "FS_IMAGE_DIGEST"}.isdisjoint(names)


def test_operator_examples_document_role_provenance_without_legacy_aliases() -> None:
    combined = "\n".join(
        (ROOT / filename).read_text(encoding="utf-8")
        for filename in (".env.example.prod", ".env.example.ops")
    )

    for key in (
        "FS_APP_IMAGE",
        "FS_WORKER_IMAGE",
        "FS_CONTROL_IMAGE",
        "FS_APP_IMAGE_DIGEST",
        "FS_WORKER_IMAGE_DIGEST",
        "FS_CONTROL_IMAGE_DIGEST",
    ):
        assert re.search(rf"^{key}=", combined, re.MULTILINE)
    assert re.search(r"^FS_IMAGE=", combined, re.MULTILINE) is None
    assert re.search(r"^FS_IMAGE_DIGEST=", combined, re.MULTILINE) is None


def _stage_body(stage: str) -> str:
    """Return the Dockerfile text belonging to one build stage."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    marker = re.search(rf"^FROM\s+\S+(?:@\S+)?\s+AS\s+{re.escape(stage)}\s*$", text, re.MULTILINE)
    assert marker is not None, f"stage {stage} not found"
    following = re.search(r"^FROM\s", text[marker.end() :], re.MULTILINE)
    end = marker.end() + (following.start() if following else len(text) - marker.end())
    return text[marker.end() : end]


@pytest.mark.parametrize(
    "stage", ("runtime-dependencies", "app-runtime", "worker-runtime", "compact-runtime")
)
def test_runtime_images_carry_no_git(stage: str) -> None:
    """Nothing the app, worker, control or MCP processes run shells out to git.

    git belongs to the stages that operate on a checkout -- cli, audit and dev -- and to
    the installer; a runtime image that carries it only widens its surface.
    """
    stages = _docker_stages(DOCKERFILE)
    lineage = "\n".join(
        instruction
        for stage_name in _stage_lineage(stages, stage)
        for instruction in stages[stage_name].instructions
    )

    assert lineage  # non-vacuity
    assert not re.search(r"apt-get install[^\n]*\bgit\b", lineage), stage
    assert not re.search(r"apt-get install[^\n]*\bgit-lfs\b", lineage), stage
    assert "git config" not in lineage, stage


def test_the_checkout_stages_keep_git_and_need_no_git_lfs() -> None:
    """The green direction: cli and audit still operate on a checkout.

    They carried git-lfs while .gitattributes routed bundled extension payloads
    through the lfs filter. Core ships no extensions and routes nothing through it,
    so no stage needs the package; it comes back only with an lfs pattern.
    """
    stages = _docker_stages(DOCKERFILE)
    lfs_patterns = [
        line
        for line in (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
        if "filter=lfs" in line and not line.lstrip().startswith("#")
    ]
    for stage in ("cli", "audit-system-tooling"):
        body = "\n".join(stages[stage].instructions)
        assert re.search(r"apt-get install[^\n]*\bgit\b", body), stage
        if lfs_patterns:
            assert re.search(r"apt-get install[^\n]*\bgit-lfs\b", body), stage
        else:
            assert not re.search(r"\bgit-lfs\b|git lfs install", body), stage


def test_compact_runtime_does_not_gain_the_docker_control_tooling() -> None:
    """The Compact runtime must not widen what the profile can do.

    docker-ce-cli, buildx and compose are the other half of `worker-system-tooling`. They
    decide whether the worker may drive Docker -- a capability and attack-surface
    question, deliberately left out of this change.
    """
    body = _stage_body("compact-runtime")

    for package in ("docker-ce-cli", "docker-buildx-plugin", "docker-compose-plugin"):
        assert package not in body, f"compact-runtime unexpectedly installs {package}"


def test_compact_runtime_environment_carries_the_catalog_urls() -> None:
    """AC17-R99: the default profile was not covered by this contract at all.

    The catalogs are core functionality, so both URLs have to reach every Compact
    role. Delivery there is two-layered and the second layer is easy to miss: a
    variable present in the Compose anchor still never reaches a child process
    unless process_spec.py also lists it, because the supervisor builds each
    child's environment from explicit sets. Both halves are asserted here.

    This test reads docker-compose.compact.yaml, which nothing in this file did
    before -- the existing assertions cover only the Scalable compose files, which
    is why a variable could be added for Scalable, pass every check, and be absent
    from the profile most operators install.
    """
    from core.infrastructure.config.compact_runtime import CompactRuntimeSettings
    from core.infrastructure.runtime.compact import process_spec

    compose = yaml.safe_load(COMPACT_COMPOSE.read_text(encoding="utf-8"))
    anchor = compose["x-compact-runtime-environment"]
    catalog_keys = {
        "FS_EXTENSION_CATALOG_URL",
        "FS_MCP_CATALOG_URL",
        "FS_WORKFLOW_TEMPLATE_CATALOG_URL",
    }

    missing_in_compose = sorted(catalog_keys - set(anchor))
    assert missing_in_compose == [], (
        f"the Compact runtime environment does not publish: {missing_in_compose}"
    )

    # Second layer: the supervisor builds each child's environment from explicit
    # sets, so publishing the variable in Compose delivers nothing on its own.
    # Assert on the processes it actually produces rather than on the sets, so a
    # future reshuffle of the sets cannot pass this while starving a role.
    settings = CompactRuntimeSettings.from_environment(
        {"FS_DEPLOYMENT_PROFILE": "compact", "FS_WORKER_PROCESSES": "1"}
    )
    specs = process_spec.build_process_specs(
        settings,
        parent_environment={
            "PATH": "/usr/bin",
            "FS_WORKER_MCP_BEARER_TOKEN": "worker-token",
            "FS_STACK_ENVIRONMENT": "production",
            "ENVIRONMENT": "production",
            "FS_EXTENSION_CATALOG_URL": "https://example.invalid/extensions.json",
            "FS_MCP_CATALOG_URL": "https://example.invalid/mcps.json",
            "FS_WORKFLOW_TEMPLATE_CATALOG_URL": "https://example.invalid/workflows.json",
        },
        public_port=8080,
        observation_fd=7,
    )
    assert specs, "no Compact processes were produced"
    # `router` is Traefik and runs no product code, so it has no business with a
    # catalog URL; every role that does run product code must see both. Naming the
    # roles rather than excluding one keeps this honest if a role is added.
    product_roles = {"app", "installer", "mcp", "control"}
    produced = {spec.role for spec in specs}
    assert product_roles <= produced, f"expected roles are missing: {product_roles - produced}"
    starved = {
        spec.role: sorted(catalog_keys - set(spec.environment))
        for spec in specs
        if spec.role in product_roles and catalog_keys - set(spec.environment)
    }
    assert starved == {}, f"these Compact processes never see the catalogs: {starved}"
    router = next(spec for spec in specs if spec.role == "router")
    assert not (catalog_keys & set(router.environment)), (
        "the proxy does not run product code and should not carry catalog URLs"
    )


def test_compact_processes_with_a_broker_url_also_receive_its_transport_mode() -> None:
    """A broker URL without its transport mode is unusable, so they travel together.

    The transport check reads FS_RABBITMQ_TRANSPORT_MODE and treats an absent value
    as "tls", which rejects the plaintext compose URL. Compose published the mode on
    the Compact container, but the supervisor's broker allowlist did not carry it, so
    every worker refused amqp://...@rabbitmq-fs:5672/ and a fresh install stopped at
    "Compact runtime" readiness. Each role is checked by validating the URL it actually
    receives against the environment it actually receives.
    """
    from core.infrastructure.config.compact_runtime import CompactRuntimeSettings
    from core.infrastructure.config.rabbitmq_transport import validate_rabbitmq_endpoint
    from core.infrastructure.runtime.compact import process_spec

    compose = yaml.safe_load(COMPACT_COMPOSE.read_text(encoding="utf-8"))
    published = compose["services"]["compact-runtime"]["environment"]
    assert published.get("FS_RABBITMQ_TRANSPORT_MODE") == "compose-local"

    settings = CompactRuntimeSettings.from_environment(
        {"FS_DEPLOYMENT_PROFILE": "compact", "FS_WORKER_PROCESSES": "2"}
    )
    specs = process_spec.build_process_specs(
        settings,
        parent_environment={
            "PATH": "/usr/bin",
            "FS_WORKER_MCP_BEARER_TOKEN": "worker-token",
            "FS_STACK_ENVIRONMENT": "production",
            "ENVIRONMENT": "production",
            "RABBITMQ_URL": "amqp://flow:secret@rabbitmq-fs:5672/",  # pragma: allowlist secret
            "FS_RABBITMQ_TRANSPORT_MODE": "compose-local",
        },
        public_port=8080,
        observation_fd=7,
    )
    brokered = {spec.role: spec for spec in specs if "RABBITMQ_URL" in spec.environment}
    expected = {"app", "installer", "mcp", "control", "worker-0", "worker-1"}
    assert expected <= set(brokered), f"roles without a broker URL: {expected - set(brokered)}"

    rejected = {}
    for role, spec in brokered.items():
        try:
            validate_rabbitmq_endpoint(spec.environment["RABBITMQ_URL"], spec.environment)
        except ValueError as exc:
            rejected[role] = str(exc)
    assert rejected == {}, f"these Compact processes cannot reach the broker: {rejected}"


def test_compact_arms_the_installer_proxy_on_the_app_role_and_nowhere_else() -> None:
    """AC17-R101: three layers were ready and the switch that arms them reached no role.

    The installer route table lists POST /api/extensions/catalog/install, the proxy
    middleware exists, and availability reports the write delegated -- but app_factory
    installs that middleware only when FS_EXTENSION_INSTALLER_PROXY_ENABLED is true, and
    that key was in neither Compact layer. So the API process performed the install
    itself, was denied by a root owned by uid 10005, and the denial surfaced as
    "unavailable in this deployment". Every layer was green on its own.

    Both directions are asserted on purpose. The app must carry the switch; the installer
    must not, because an installer that proxies to itself is a loop, and a presence-only
    test would never see it.
    """
    from core.infrastructure.config.compact_runtime import CompactRuntimeSettings
    from core.infrastructure.runtime.compact import process_spec

    switch = "FS_EXTENSION_INSTALLER_PROXY_ENABLED"
    settings = CompactRuntimeSettings.from_environment(
        {"FS_DEPLOYMENT_PROFILE": "compact", "FS_WORKER_PROCESSES": "1"}
    )
    specs = process_spec.build_process_specs(
        settings,
        # the operator trying to switch delegation off must not be able to: the app
        # process cannot write /data/extensions under any environment.
        parent_environment={
            "PATH": "/usr/bin",
            "FS_WORKER_MCP_BEARER_TOKEN": "worker-token",
            "FS_STACK_ENVIRONMENT": "production",
            "ENVIRONMENT": "production",
            switch: "0",
        },
        public_port=8080,
        observation_fd=7,
    )
    by_role = {spec.role: spec for spec in specs}
    missing = sorted({"app", "installer"} - set(by_role))
    assert missing == [], f"roles not produced: {missing}; produced {sorted(by_role)}"

    app = by_role["app"]
    assert app.environment.get(switch) == "1", (
        "the Compact app process must delegate archive lifecycle to the installer; "
        f"it carries {switch}={app.environment.get(switch)!r}"
    )
    installer_port = by_role["installer"].port
    assert app.environment.get("FS_EXTENSION_INSTALLER_ORIGIN") == (
        f"http://127.0.0.1:{installer_port}"
    ), "the app must address the installer this supervisor actually started"

    carriers = sorted(role for role, spec in by_role.items() if switch in spec.environment)
    assert carriers == ["app"], (
        f"only the app process proxies to the installer; these also carry the switch: {carriers}"
    )


def test_the_compact_control_plane_receives_the_settings_its_loops_read() -> None:
    """AC17-R105: Compact runs all six control loops and delivered none of their settings.

    The control role's allowlist was the same four sets every other role gets, so every
    loop ran on its compiled-in default -- intervals, batch limits, the watchdog's grace
    and stuck thresholds -- while .env.prod said otherwise and nothing reported a
    mismatch. A setting that is accepted, recorded and ignored is worse than one that is
    rejected: there is nothing to notice.

    Nothing is listed here by hand. The expectation comes from the environment registry,
    which records where each variable is read, so a new control-plane setting is covered
    by this the day it is read rather than the day someone remembers to add it.
    """
    import json

    from core.infrastructure.config.compact_runtime import CompactRuntimeSettings
    from core.infrastructure.runtime.compact import process_spec

    registry = json.loads(
        (ROOT / "docs/configuration/flow-steward-env-audit.json").read_text(encoding="utf-8")
    )
    assert registry, "the environment registry is empty; this test would be vacuous"
    control_settings = {
        entry["canonical_name"]
        for entry in registry
        if any(
            location.startswith("core/infrastructure/runtime/control/")
            for location in entry.get("code_read_locations") or []
        )
        # a pytest marker is not a setting, and delivering it would be a defect of its own
        and not entry["canonical_name"].startswith("PYTEST_")
        # supplied by the container runtime, not by the operator
        and entry["canonical_name"] != "HOSTNAME"
    }
    assert control_settings, "no control-plane settings found; this test would be vacuous"

    settings = CompactRuntimeSettings.from_environment(
        {"FS_DEPLOYMENT_PROFILE": "compact", "FS_WORKER_PROCESSES": "1"}
    )
    parent = {
        "PATH": "/usr/bin",
        "FS_WORKER_MCP_BEARER_TOKEN": "worker-token",
        "FS_STACK_ENVIRONMENT": "production",
        "ENVIRONMENT": "production",
        **dict.fromkeys(control_settings, "set-by-the-operator"),
    }
    specs = process_spec.build_process_specs(
        settings, parent_environment=parent, public_port=8080, observation_fd=7
    )
    control = next(spec for spec in specs if spec.role == "control")

    starved = sorted(control_settings - set(control.environment))

    assert starved == [], f"the Compact control plane never sees these settings: {starved}"


def test_the_compact_runtime_environment_publishes_what_the_roles_may_read() -> None:
    """The second layer: a name no role can inherit is delivered by nobody.

    AC17-R99 established that a variable has to be in both the Compose overlay and the
    supervisor's allowlists. This asserts the Compose half for the same settings, so a
    future addition to one layer cannot quietly pass with the other missing.
    """
    import json

    compose = yaml.safe_load(COMPACT_COMPOSE.read_text(encoding="utf-8"))
    anchor = compose["x-compact-runtime-environment"]
    registry = json.loads(
        (ROOT / "docs/configuration/flow-steward-env-audit.json").read_text(encoding="utf-8")
    )
    control_settings = {
        entry["canonical_name"]
        for entry in registry
        if any(
            location.startswith("core/infrastructure/runtime/control/")
            for location in entry.get("code_read_locations") or []
        )
        and not entry["canonical_name"].startswith("PYTEST_")
        and entry["canonical_name"] != "HOSTNAME"
    }
    assert control_settings, "no control-plane settings found; this test would be vacuous"

    unpublished = sorted(control_settings - set(anchor))

    assert unpublished == [], f"the Compact runtime environment does not publish: {unpublished}"


def test_the_compact_roles_can_serve_machine_readable_metrics() -> None:
    """AC17-R105: the installer generated FS_METRICS_TOKEN and no process ever saw it.

    health.py gates the machine-readable metrics on this token, so a collector pointed
    at a Compact installation could reach /health and /health/ready and nothing else.
    The token was in .env.prod the whole time, which is what made it look configured.
    """
    from core.infrastructure.config.compact_runtime import CompactRuntimeSettings
    from core.infrastructure.runtime.compact import process_spec

    compose = yaml.safe_load(COMPACT_COMPOSE.read_text(encoding="utf-8"))
    assert "FS_METRICS_TOKEN" in compose["x-compact-runtime-environment"]

    settings = CompactRuntimeSettings.from_environment(
        {"FS_DEPLOYMENT_PROFILE": "compact", "FS_WORKER_PROCESSES": "1"}
    )
    specs = process_spec.build_process_specs(
        settings,
        parent_environment={
            "PATH": "/usr/bin",
            "FS_WORKER_MCP_BEARER_TOKEN": "worker-token",
            "FS_STACK_ENVIRONMENT": "production",
            "ENVIRONMENT": "production",
            "FS_METRICS_TOKEN": "collector-token",
        },
        public_port=8080,
        observation_fd=7,
    )
    app = next(spec for spec in specs if spec.role == "app")

    assert app.environment.get("FS_METRICS_TOKEN") == "collector-token"


def test_an_unset_published_variable_never_reaches_a_process_as_an_empty_string() -> None:
    """AC17-R105 follow-up: publishing a key without a default took the runtime down.

    Compose substitutes every key it names, so a variable the operator never set
    arrives as an empty string rather than not arriving. Downstream that is a
    different thing: control/config.py reads thirteen of these with
    `int(_runtime_env.get(name, "30"))`, which returns the default for a missing key
    and raises ValueError for an empty one. The control process crash-looped, and with
    the control plane down no role came up at all.

    Found on a real install after the tests here were green, which is the point of the
    assertion: the supervisor must not hand a child an empty inherited value.
    """
    from core.infrastructure.config.compact_runtime import CompactRuntimeSettings
    from core.infrastructure.runtime.compact import process_spec

    settings = CompactRuntimeSettings.from_environment(
        {"FS_DEPLOYMENT_PROFILE": "compact", "FS_WORKER_PROCESSES": "1"}
    )
    specs = process_spec.build_process_specs(
        settings,
        parent_environment={
            "PATH": "/usr/bin",
            "FS_WORKER_MCP_BEARER_TOKEN": "worker-token",
            "FS_STACK_ENVIRONMENT": "production",
            "ENVIRONMENT": "production",
            # exactly what Compose produces for a key the operator left alone
            "FS_WATCHDOG_INTERVAL_SECONDS": "",
            "FS_SCHEDULED_JOBS_BATCH_LIMIT": "   ",
        },
        public_port=8080,
        observation_fd=7,
    )

    # Only inherited values are at issue. A role's own explicit additions may be empty
    # by construction -- the MCP role derives FS_MCP_ALLOWED_HOSTS from the public
    # origin, which is not supplied here -- and those are the supervisor's decisions.
    inherited_but_empty = {
        f"{spec.role}:{name}"
        for spec in specs
        for name in ("FS_WATCHDOG_INTERVAL_SECONDS", "FS_SCHEDULED_JOBS_BATCH_LIMIT")
        if name in spec.environment
    }

    assert inherited_but_empty == set(), (
        f"these processes are handed an empty inherited value: {sorted(inherited_but_empty)}"
    )


def test_every_control_setting_survives_arriving_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reading must treat an empty value as unset, whoever delivered it.

    AC17-R105 follow-up, stated as behaviour rather than as a rule about YAML.
    Compose substitutes every key it names, so a setting the operator left alone
    reaches the process as an empty string; `int("")` raised, the control process
    crash-looped, and with the control plane down no role came up at all. Asserting on
    the overlay's syntax would have covered only the delivery path that happened to
    expose it, and left the next one open.

    The settings are read out of the module that parses them, so a new one is covered
    the day it is added.
    """
    from core.infrastructure.runtime.control.config import ControlConfig

    source = (ROOT / "core/infrastructure/runtime/control/config.py").read_text(encoding="utf-8")
    # Found under either shape on purpose, so reverting the reader makes this fail on
    # the defect rather than on finding nothing to check.
    numeric = sorted(
        set(re.findall(r'(?:_env_int|int\(\s*_runtime_env\.get)\(?\s*"([A-Z][A-Z0-9_]*)"', source))
    )
    assert numeric, "no numeric control settings parsed; this test would be vacuous"

    for name in numeric:
        monkeypatch.delenv(name, raising=False)
    absent = ControlConfig.from_env()

    for name in numeric:
        monkeypatch.setenv(name, "")
    empty = ControlConfig.from_env()

    assert empty == absent, (
        "a setting delivered as an empty string must read the same as one never set"
    )


def test_the_code_default_is_the_one_the_deployment_actually_uses() -> None:
    """A compiled-in default that no deployment can reach is a lie in the source.

    Compose declares these settings with `${NAME:-value}`, and `:-` sets the key
    whether or not the operator did, so the value in the Compose file is what every
    installation runs and the one beside the reader is never consulted. Ten of them
    disagreed -- FS_WATCHDOG_MAX_JOURNAL_ENTRIES said 200 in code and 10000 in
    Compose, FS_DASHBOARD_ROLLUP_BACKFILL_DAYS 180 against 7 -- so anyone reasoning
    from the source reasoned about numbers no process had ever used.

    Aligning the code to Compose rather than the reverse is deliberate: the Compose
    values are what production is running, and changing those under the guise of
    tidying would be a silent behaviour change nothing would catch.
    """
    compose_text = "\n".join(
        (ROOT / "infra/docker" / name).read_text(encoding="utf-8")
        for name in (
            "docker-compose.yaml",
            "docker-compose.prod.yaml",
            "docker-compose.compact.yaml",
        )
    )
    published: dict[str, str] = {}
    for name, default in re.findall(
        r"^\s+([A-Z][A-Z0-9_]*): \$\{\1:-([^}]*)\}\s*$", compose_text, re.M
    ):
        published.setdefault(name, default)
    source = (ROOT / "core/infrastructure/runtime/control/config.py").read_text(encoding="utf-8")
    in_code = dict(re.findall(r'_env_int\(\s*"([A-Z][A-Z0-9_]*)"\s*,\s*"([^"]*)"', source))
    assert published, "no compose defaults parsed; this test would be vacuous"
    assert in_code, "no code defaults parsed; this test would be vacuous"

    disagreements = {
        name: {"compose": published[name], "code": value}
        for name, value in in_code.items()
        if name in published and published[name] != value
    }

    assert disagreements == {}, (
        f"the default beside the reader is not the one the deployment uses: {disagreements}"
    )
