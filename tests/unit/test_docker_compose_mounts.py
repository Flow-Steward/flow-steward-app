from __future__ import annotations

import re
from pathlib import Path

import yaml
from core.infrastructure.config.env_registry import registry_default_value

ROOT = Path(__file__).resolve().parents[2]
FIRST_RETIRED_NAME = "co" + "dex"
SECOND_RETIRED_NAME = "clau" + "de"
FIRST_RETIRED_HOME = "CO" + "DEX_HOME"
SECOND_RETIRED_TOKEN = "CLAU" + "DE_CODE_OAUTH_TOKEN"
FIRST_RETIRED_PACKAGE = f"@openai/{FIRST_RETIRED_NAME}"
SECOND_RETIRED_PACKAGE = f"@anthropic-ai/{SECOND_RETIRED_NAME}-code"

CLI_BASE_ENVIRONMENT = {
    "ENVIRONMENT",
    "FS_CONFIGURATION_STATE_ROOT",
    "FS_DOCKER_HOST_PROJECT_ROOT",
    "FS_INSTALLER_STATE_DIR",
    "FS_INSTALLER_USE_HOST_PYTHON",
    "FS_LOAD_REPO_DOTENV",
    "FS_STACK_ENVIRONMENT",
    "HOME",
    "PYTHONPYCACHEPREFIX",
    "TERM",
}
CLI_PRODUCTION_ENVIRONMENT = CLI_BASE_ENVIRONMENT | {
    "FS_BUILD_SHA",
    "FS_RELEASE_REGISTRIES",
    "FS_RELEASE_REPOSITORY",
    "FS_RUNTIME_ENVIRONMENT",
}


def test_production_cli_receives_release_source_overrides() -> None:
    for compose_file in ("docker-compose.prod.yaml", "docker-compose.compact.yaml"):
        service = _service_config("fs-cli", compose_file=compose_file)
        environment = service["environment"]

        assert "FS_RELEASE_REGISTRIES" in environment
        assert "FS_RELEASE_REPOSITORY" in environment


def test_cli_uses_the_full_mounted_installer_state_root() -> None:
    expected = {
        "docker-compose.yaml": {
            "state_root": "/run/flow-steward-install-state",
            "state_mount": (
                "${FS_INSTALLER_STATE_DIR:-../../.flow-steward}:/run/flow-steward-install-state:rw"
            ),
            "identity_mount": (
                "${FS_INSTALLER_STATE_DIR:-../../.flow-steward}:"
                "${FS_INSTALLER_STATE_DIR:-${FS_DOCKER_HOST_PROJECT_ROOT:-/run/flow-steward-host-project}/.flow-steward}:rw"
            ),
            "runtime_mount": (
                "${FS_INSTALLER_STATE_DIR:-../../.flow-steward}"
                "/runtime:/run/flow-steward-runtime:rw"
            ),
        },
        "docker-compose.prod.yaml": {
            "state_root": "/run/flow-steward-candidate-state",
            "state_mount": (
                "${FS_CANDIDATE_STATE_DIR:?candidate state directory is required}"
                ":/run/flow-steward-candidate-state:rw"
            ),
            "runtime_mount": (
                "${FS_INSTALLER_STATE_DIR:-../../.flow-steward}"
                "/runtime:/run/flow-steward-runtime:rw"
            ),
        },
        "docker-compose.compact.yaml": {
            "state_root": "/run/flow-steward-install-state",
            "state_mount": (
                "${FS_INSTALLER_STATE_DIR:-../../.flow-steward}:/run/flow-steward-install-state:rw"
            ),
            "runtime_mount": (
                "${FS_INSTALLER_STATE_DIR:-../../.flow-steward}"
                "/runtime:/run/flow-steward-runtime:rw"
            ),
        },
    }
    for compose_file, paths in expected.items():
        service = _service_config("fs-cli", compose_file=compose_file)
        state_root = paths["state_root"]

        assert service["environment"].get("FS_CONFIGURATION_STATE_ROOT") == state_root, (
            f"{compose_file}: fs-cli must read configuration from its mounted installer state"
        )
        if compose_file != "docker-compose.yaml":
            assert service["environment"]["FS_INSTALLER_STATE_DIR"] == state_root
        assert paths["state_mount"] in service["volumes"]
        if "identity_mount" in paths:
            assert paths["identity_mount"] in service["volumes"]
        assert paths["runtime_mount"] in service["volumes"]


def test_compact_runtime_and_cli_use_their_canonical_image_roles() -> None:
    compact_runtime = _service_config("compact-runtime", compose_file="docker-compose.compact.yaml")
    cli = _service_config("fs-cli", compose_file="docker-compose.compact.yaml")

    assert compact_runtime["image"].startswith("${FS_COMPACT_IMAGE:")
    assert cli["image"].startswith("${FS_CLI_IMAGE:")


def test_compact_cli_uses_the_canonical_host_identity_and_docker_group() -> None:
    cli = _service_config("fs-cli", compose_file="docker-compose.compact.yaml")

    assert cli["user"] == "${FS_HOST_UID:-1000}:${FS_HOST_GID:-1000}"
    assert cli["group_add"] == ["${FS_DOCKER_GID:-0}"]
    assert cli["environment"]["HOME"] == "/tmp"
    assert cli["environment"]["PYTHONPYCACHEPREFIX"] == "/tmp/flow-steward/pycache"
    assert cli["environment"]["FS_LOAD_REPO_DOTENV"] == "0"


def test_production_bytecode_caches_stay_inside_each_container() -> None:
    """Release images date every source 1970-01-01, so a cache kept on the host logs
    mount outlived the build and served another release's modules after an upgrade."""
    source = (ROOT / "infra" / "docker" / "docker-compose.prod.yaml").read_text(encoding="utf-8")
    services = (yaml.safe_load(source) or {})["services"]
    prefixes = {
        name: service["environment"]["PYTHONPYCACHEPREFIX"]
        for name, service in services.items()
        if isinstance(service.get("environment"), dict)
        and "PYTHONPYCACHEPREFIX" in service["environment"]
    }

    assert {"fs-app", "fs-worker", "control-plane"} <= set(prefixes)
    assert {name for name, prefix in prefixes.items() if not prefix.startswith("/tmp/")} == set()


CORE_RUNTIME_SERVICES = {
    "docker-compose.yaml": {
        "control-plane",
        "fs-app",
        "fs-cli",
        "fs-frontend",
        "fs-mcp",
        "fs-worker",
        "pg-fs",
        "rabbitmq-fs",
        "seaweedfs",
        "traefik",
    },
    "docker-compose.prod.yaml": {
        "control-plane",
        "fs-app",
        "fs-cli",
        "fs-mcp",
        "fs-worker",
        "pg-fs",
        "rabbitmq-fs",
        "seaweedfs",
        "traefik",
    },
}

ONE_SHOT_SERVICES = {
    "docker-compose.yaml": {
        "dev-dataset-fixture",
        "extension-dependencies",
        "fs-audit",
        "fs-extension-manager",
        "fs-test-runner",
        "memory-postgres-extensions",
        "pg-audit",
        "rabbitmq-audit",
        "sonar-scanner",
    },
    "docker-compose.prod.yaml": {
        "dev-dataset-fixture",
        "extension-dependencies",
        "fs-extension-manager",
        "memory-postgres-extensions",
    },
}

DEV_DATASET_FIXTURE_SERVICE = "dev-dataset-fixture"


def _split_volume(volume: str) -> tuple[str, str] | None:
    parts = volume.rsplit(":", 2)
    if len(parts) == 3 and parts[-1] in {"ro", "rw"}:
        return parts[0], parts[1]
    if len(parts) >= 2:
        return ":".join(parts[:-1]), parts[-1]
    return None


def _service_volume_targets(
    service_name: str, *, compose_file: str = "docker-compose.yaml"
) -> set[str]:
    source = (ROOT / "infra" / "docker" / compose_file).read_text(encoding="utf-8")
    data = yaml.safe_load(source) or {}
    services = data.get("services") or {}
    service = services.get(service_name) or {}
    targets: set[str] = set()
    for volume in service.get("volumes") or []:
        if not isinstance(volume, str):
            continue
        parsed = _split_volume(volume)
        if parsed is not None:
            targets.add(parsed[1])
    return targets


def _service_volume_sources_by_target(
    service_name: str, *, compose_file: str = "docker-compose.yaml"
) -> dict[str, str]:
    source = (ROOT / "infra" / "docker" / compose_file).read_text(encoding="utf-8")
    data = yaml.safe_load(source) or {}
    services = data.get("services") or {}
    service = services.get(service_name) or {}
    sources: dict[str, str] = {}
    for volume in service.get("volumes") or []:
        if not isinstance(volume, str):
            continue
        parsed = _split_volume(volume)
        if parsed is not None:
            sources[parsed[1]] = parsed[0]
    return sources


def _service_environment(
    service_name: str, *, compose_file: str = "docker-compose.yaml"
) -> dict[str, str]:
    source = (ROOT / "infra" / "docker" / compose_file).read_text(encoding="utf-8")
    data = yaml.safe_load(source) or {}
    services = data.get("services") or {}
    service = services.get(service_name) or {}
    environment = service.get("environment") or {}
    return environment if isinstance(environment, dict) else {}


def _service_config(service_name: str, *, compose_file: str = "docker-compose.yaml") -> dict:
    source = (ROOT / "infra" / "docker" / compose_file).read_text(encoding="utf-8")
    data = yaml.safe_load(source) or {}
    services = data.get("services") or {}
    service = services.get(service_name) or {}
    return service if isinstance(service, dict) else {}


def _service_depends_on(service_name: str, *, compose_file: str = "docker-compose.yaml") -> dict:
    service = _service_config(service_name, compose_file=compose_file)
    depends_on = service.get("depends_on") or {}
    return depends_on if isinstance(depends_on, dict) else {}


def _dockerfile_stage_body(path: Path, stage_name: str, next_stage_name: str) -> str:
    text = path.read_text(encoding="utf-8")

    def stage_start(marker: str) -> int:
        alias = marker.rsplit(" AS ", 1)[-1]
        match = re.search(rf"^FROM\s+.+\s+AS\s+{re.escape(alias)}\s*$", text, re.MULTILINE)
        if match is None:
            raise AssertionError(f"Dockerfile stage is missing: {alias}")
        return match.end()

    start = stage_start(stage_name)
    end = stage_start(next_stage_name)
    return text[start:end]


def test_core_runtime_services_restart_after_host_or_docker_restart() -> None:
    for compose_file, service_names in CORE_RUNTIME_SERVICES.items():
        for service_name in service_names:
            service = _service_config(service_name, compose_file=compose_file)
            assert service.get("restart") == "always", (
                f"{compose_file}:{service_name} must restart after a host or Docker restart"
            )

    for compose_file, service_names in ONE_SHOT_SERVICES.items():
        for service_name in service_names:
            service = _service_config(service_name, compose_file=compose_file)
            assert service.get("restart") == "no", (
                f"{compose_file}:{service_name} must remain an on-demand one-shot job"
            )


def test_dataset_fixture_is_enabled_only_in_development_and_removed_before_production() -> None:
    development = _service_config(DEV_DATASET_FIXTURE_SERVICE)
    production = _service_config(
        DEV_DATASET_FIXTURE_SERVICE,
        compose_file="docker-compose.prod.yaml",
    )

    assert development["environment"]["FS_STACK_ENVIRONMENT"] == "development"
    assert development["volumes"] == [
        "./postgres-dev-dataset-fixture/enable.sql:/fixture/enable.sql:ro"
    ]
    assert production["environment"]["FS_STACK_ENVIRONMENT"] == "production"
    assert production["volumes"] == [
        "./postgres-dev-dataset-fixture/disable.sql:/fixture/disable.sql:ro"
    ]
    assert all("enable.sql" not in volume for volume in production["volumes"])

    for compose_file in ("docker-compose.yaml", "docker-compose.prod.yaml"):
        fixture = _service_config(DEV_DATASET_FIXTURE_SERVICE, compose_file=compose_file)
        assert fixture["restart"] == "no"
        # AC17-R134: waits for the database to answer, not for its container to
        # exist. Which condition that has to be is owned by the readiness guard.
        assert fixture["depends_on"] == {"pg-fs": {"condition": "service_healthy"}}
        for service_name in ("fs-app", "fs-worker", "control-plane"):
            assert _service_depends_on(service_name, compose_file=compose_file)[
                DEV_DATASET_FIXTURE_SERVICE
            ] == {"condition": "service_completed_successfully"}
        assert DEV_DATASET_FIXTURE_SERVICE not in _service_depends_on(
            "fs-mcp",
            compose_file=compose_file,
        )


def test_extension_dependencies_materialize_offline_before_runtime_consumers() -> None:
    expected_images = {
        "docker-compose.yaml": "${FS_APP_IMAGE:-" + registry_default_value("FS_APP_IMAGE") + "}",
        "docker-compose.prod.yaml": (
            "${FS_APP_IMAGE:?FS_APP_IMAGE is required for immutable production provenance}"
        ),
    }
    expected_sources = {
        "docker-compose.yaml": "../../extensions",
        "docker-compose.prod.yaml": "${FS_EXTENSIONS_DIR:-../../extensions}",
    }
    command = [
        "python",
        "-m",
        "core.infrastructure.extension_runtime.bundled_dependencies",
        "/app/extensions",
    ]

    for compose_file in ("docker-compose.yaml", "docker-compose.prod.yaml"):
        service = _service_config("extension-dependencies", compose_file=compose_file)
        environment = _service_environment(
            "extension-dependencies",
            compose_file=compose_file,
        )
        sources = _service_volume_sources_by_target(
            "extension-dependencies",
            compose_file=compose_file,
        )

        assert service.get("image") == expected_images[compose_file]
        assert service.get("restart") == "no"
        assert service.get("command") == command
        assert environment.get("FS_EXTENSION_DEPS_FETCH") == "${FS_EXTENSION_DEPS_FETCH:-0}"
        assert environment.get("FS_EXTENSION_DEPS_SITE_MAX_BYTES") == (
            "${FS_EXTENSION_DEPS_SITE_MAX_BYTES:-536870912}"
        )
        assert sources["/app/extensions"] == expected_sources[compose_file]

        for consumer in ("fs-app", "fs-worker"):
            dependency = _service_depends_on(consumer, compose_file=compose_file).get(
                "extension-dependencies"
            )
            assert dependency == {"condition": "service_completed_successfully"}


def test_no_service_mounts_or_names_a_host_extension_policy() -> None:
    """Grants come from each extension's own manifest; there is no host policy file."""
    for compose_file in ("docker-compose.yaml", "docker-compose.prod.yaml"):
        for service_name in ("extension-dependencies", "fs-app", "fs-worker"):
            service = _service_config(service_name, compose_file=compose_file)
            environment = _service_environment(service_name, compose_file=compose_file)
            assert not any(
                "extension-install-policy" in str(volume) for volume in service.get("volumes") or []
            )
            assert "FS_EXTENSION_INSTALL_POLICY_PATH" not in environment

        dependency_mounts = _service_config("extension-dependencies", compose_file=compose_file)[
            "volumes"
        ]
        assert any(volume.endswith(":/app/extensions") for volume in dependency_mounts)
        assert not any(volume.endswith(":/app/extensions:ro") for volume in dependency_mounts)

        for service_name in ("fs-app", "fs-worker"):
            runtime_mounts = _service_config(service_name, compose_file=compose_file)["volumes"]
            assert any(volume.endswith(":/app/extensions:ro") for volume in runtime_mounts)

    assert not (ROOT / "infra/extension-runtime/extension-install-policy.yaml").exists()


def test_extension_dependency_init_module_is_in_app_source_slice() -> None:
    source_slice = (ROOT / "infra" / "docker" / "source-slices" / "app.json").read_text(
        encoding="utf-8"
    )

    assert '"core/infrastructure/extension_runtime/bundled_dependencies.py"' in source_slice


def test_no_service_mounts_a_source_project_workspace():
    for compose_file in ("docker-compose.yaml", "docker-compose.prod.yaml"):
        source = (ROOT / "infra" / "docker" / compose_file).read_text(encoding="utf-8")
        services = (yaml.safe_load(source) or {}).get("services") or {}
        assert {"fs-app", "fs-worker", "fs-mcp", "fs-cli"} <= set(services)  # non-vacuity
        for service_name in services:
            targets = _service_volume_targets(service_name, compose_file=compose_file)
            assert "/projects" not in targets, (compose_file, service_name)
            assert "/project/test-project" not in targets, (compose_file, service_name)


def test_fs_mcp_has_api_only_runtime_environment():
    for compose_file in ("docker-compose.yaml", "docker-compose.prod.yaml"):
        mcp_service = _service_config("fs-mcp", compose_file=compose_file)
        mcp_env = _service_environment("fs-mcp", compose_file=compose_file)

        assert "env_file" not in mcp_service
        assert _service_volume_targets("fs-mcp", compose_file=compose_file) == set()
        assert "FS_APP_INTERNAL_URL" in mcp_env
        assert "RABBITMQ_URL" not in mcp_env
        assert FIRST_RETIRED_HOME not in mcp_env


def test_fs_mcp_does_not_pin_global_container_name() -> None:
    mcp_service = _service_config("fs-mcp")
    assert "container_name" not in mcp_service


def test_only_the_roles_that_need_it_receive_the_internal_app_api_origin() -> None:
    """AC17-R123 widened this from "fs-mcp alone", and the point of the test survives.

    It was never about mcp: it is that an internal origin does not spread to services
    with no use for it. Two more have a use now -- the app and the worker run
    extension steps, and a step may need a URL for an artifact this deployment's
    storage cannot presign, which the platform then serves itself. Everything else is
    still asserted not to have it, so the set can only grow deliberately.
    """
    entitled = {"fs-mcp", "fs-app", "fs-worker"}
    for compose_file in ("docker-compose.yaml", "docker-compose.prod.yaml"):
        source = (ROOT / "infra" / "docker" / compose_file).read_text(encoding="utf-8")
        services = (yaml.safe_load(source) or {}).get("services") or {}
        told = {
            service_name
            for service_name in services
            if "FS_APP_INTERNAL_URL"
            in _service_environment(service_name, compose_file=compose_file)
        }
        assert told == entitled, f"{compose_file}: {told ^ entitled}"
        for service_name in entitled:
            assert (
                _service_environment(service_name, compose_file=compose_file)["FS_APP_INTERNAL_URL"]
                == "http://fs-app:8099"
            )


def test_base_compose_uses_extended_postgres_by_default() -> None:
    pg_service = _service_config("pg-fs")
    extensions_service = _service_config("memory-postgres-extensions")
    rabbitmq_service = _service_config("rabbitmq-fs")
    pg_env = _service_environment("pg-fs")
    rabbitmq_env = _service_environment("rabbitmq-fs")

    assert (
        pg_service.get("build", {}).get("dockerfile") == "infra/docker/postgres-extended/Dockerfile"
    )
    assert pg_service.get("image") == "${POSTGRES_IMAGE:-fs-postgres-extended:0.1.0-pgvector-0.8.5}"
    assert (
        "shared_preload_libraries=${POSTGRES_SHARED_PRELOAD_LIBRARIES:-age,pg_stat_statements}"
        in (pg_service.get("command") or [])
    )
    assert (
        extensions_service.get("image")
        == "${POSTGRES_IMAGE:-fs-postgres-extended:0.1.0-pgvector-0.8.5}"
    )
    assert pg_env.get("POSTGRES_DB") == "${POSTGRES_DB:-flow_steward}"
    assert pg_env.get("POSTGRES_USER") == "${POSTGRES_USER:-flow_steward}"
    assert pg_env.get("POSTGRES_PASSWORD") == "${POSTGRES_PASSWORD:-flow-steward-pass}"
    assert pg_service.get("ports") == ["127.0.0.1:${POSTGRES_HOST_PORT:-55432}:5432"]
    assert _service_config("fs-mcp").get("ports") == ["127.0.0.1:${MCP_HOST_PORT:-8100}:8100"]
    assert _service_config("rabbitmq-fs").get("ports") == [
        "127.0.0.1:${RABBITMQ_AMQP_HOST_PORT:-5673}:5672",
        "127.0.0.1:${RABBITMQ_MANAGEMENT_HOST_PORT:-15673}:15672",
    ]
    assert rabbitmq_service.get("image") == "${RABBITMQ_IMAGE:-rabbitmq:3.13-management}"
    assert rabbitmq_env.get("RABBITMQ_DEFAULT_USER") == "${RABBITMQ_USER:-flow_steward}"
    assert rabbitmq_env.get("RABBITMQ_DEFAULT_PASS") == "${RABBITMQ_PASS:-flow-steward-pass}"


def test_extension_schema_runner_never_allocates_anonymous_postgres_storage() -> None:
    for compose_file in ("docker-compose.yaml", "docker-compose.prod.yaml"):
        service = _service_config(
            "memory-postgres-extensions",
            compose_file=compose_file,
        )

        assert "/var/lib/postgresql/data" in (service.get("tmpfs") or [])


def test_role_runtime_services_receive_explicit_database_and_broker_configuration() -> None:
    for service_name in ("fs-app", "fs-worker", "control-plane"):
        env = _service_environment(service_name)

        assert "FS_DB_DSN" in env
        assert "RABBITMQ_URL" in env
        assert "LANGGRAPH_POSTGRES_URI" in env

    mcp_env = _service_environment("fs-mcp")
    assert "FS_DB_DSN" not in mcp_env
    assert "RABBITMQ_URL" not in mcp_env
    assert "LANGGRAPH_POSTGRES_URI" not in mcp_env


def test_extension_runtime_services_receive_private_remote_url_policy() -> None:
    for compose_file in ("docker-compose.yaml", "docker-compose.prod.yaml"):
        for service_name in ("fs-app", "fs-worker"):
            env = _service_environment(service_name, compose_file=compose_file)

            assert env.get("FS_ALLOW_PRIVATE_REMOTE_URLS") == "${FS_ALLOW_PRIVATE_REMOTE_URLS:-0}"


def test_control_plane_receives_dispatch_encryption_key() -> None:
    expected = {
        "docker-compose.yaml": "${ENCRYPTION_KEY:-local-dev-encryption-key-not-for-production}",
        "docker-compose.prod.yaml": "${ENCRYPTION_KEY:-}",
    }

    for compose_file, value in expected.items():
        env = _service_environment("control-plane", compose_file=compose_file)
        assert env.get("ENCRYPTION_KEY") == value


def test_runtime_services_do_not_depend_on_memory_postgres_extensions() -> None:
    expected = {
        "docker-compose.yaml": (
            "fs-app",
            "fs-test-runner",
            "fs-audit",
            "fs-worker",
            "control-plane",
            "fs-mcp",
        ),
        "docker-compose.prod.yaml": (
            "fs-app",
            "fs-worker",
            "control-plane",
            "fs-mcp",
        ),
    }

    for compose_file, services in expected.items():
        for service_name in services:
            depends_on = _service_depends_on(service_name, compose_file=compose_file)
            assert "memory-postgres-extensions" not in depends_on


def test_cli_compose_does_not_synthesize_no_color_environment() -> None:
    for compose_file in ("docker-compose.yaml", "docker-compose.prod.yaml"):
        env = _service_environment("fs-cli", compose_file=compose_file)

        assert "NO_COLOR" not in env
        assert env.get("TERM") == "${TERM:-}"


def test_cli_compose_exports_only_lifecycle_process_environment() -> None:
    expected = {
        "docker-compose.yaml": CLI_BASE_ENVIRONMENT,
        "docker-compose.prod.yaml": CLI_PRODUCTION_ENVIRONMENT,
    }

    for compose_file, allowlist in expected.items():
        service = _service_config("fs-cli", compose_file=compose_file)
        environment = _service_environment("fs-cli", compose_file=compose_file)

        assert "env_file" not in service
        assert set(environment) == allowlist
        assert environment["FS_LOAD_REPO_DOTENV"] == "0"
        assert environment["ENVIRONMENT"] == "${ENVIRONMENT-${FS_STACK_ENVIRONMENT-production}}"
        assert (
            environment["FS_STACK_ENVIRONMENT"]
            == "${FS_STACK_ENVIRONMENT-${ENVIRONMENT-production}}"
        )


def test_extension_manager_uses_read_only_input_and_writable_managed_root() -> None:
    host_root = "${FS_DOCKER_HOST_PROJECT_ROOT:-/run/flow-steward-host-project}"
    host_bind = f"${{FS_DOCKER_HOST_PROJECT_ROOT:-../../}}:{host_root}:ro"

    for compose_file in ("docker-compose.yaml", "docker-compose.prod.yaml"):
        manager = _service_config("fs-extension-manager", compose_file=compose_file)
        environment = manager["environment"]
        volumes = manager["volumes"]

        assert manager["profiles"] == ["cli"]
        assert manager["restart"] == "no"
        assert manager["command"] == ["true"]
        assert manager["working_dir"] == host_root
        assert environment["FS_DOCKER_HOST_PROJECT_ROOT"] == ("${FS_DOCKER_HOST_PROJECT_ROOT:-}")
        assert environment["FS_EXTENSIONS_ROOT"] == "/app/extensions"
        assert environment["PYTHONPATH"] == "/app"
        assert environment["PYTHONSAFEPATH"] == "1"
        assert host_bind in volumes
        assert any(str(volume).endswith(":/app/extensions:rw") for volume in volumes)
        # A mutating command records itself before it runs, and the project root
        # above is read-only, so it needs a writable directory of its own — in
        # every deployment, not just the development one. Without it the command
        # dies before reading its archive.
        assert environment["FS_OPERATION_LOG_ROOT"] == "/run/flow-steward-logs"
        assert any(str(volume).endswith(":/run/flow-steward-logs:rw") for volume in volumes), (
            f"{compose_file}: fs-extension-manager has nowhere to write its operation log"
        )
        assert len(volumes) == 3
        assert "/var/run/docker.sock:/var/run/docker.sock" not in volumes
        assert "ports" not in manager
        assert "expose" not in manager

        app = _service_config("fs-app", compose_file=compose_file)
        worker = _service_config("fs-worker", compose_file=compose_file)
        assert any(str(volume).endswith(":/app/extensions:ro") for volume in app["volumes"])
        assert any(str(volume).endswith(":/app/extensions:ro") for volume in worker["volumes"])


def test_cli_process_environment_excludes_product_secret_families() -> None:
    forbidden_exact = {
        "FS_DB_DSN",
        "FS_METRICS_TOKEN",
        "FS_PGVECTOR_DSN",
        "FS_SECRET_KEY",
        "FS_WORKER_MCP_BEARER_TOKEN",
        "LANGGRAPH_POSTGRES_URI",
        "MCP_URL",
        "SECURITY_PII_SALT",
    }
    forbidden_prefixes = (
        "ARTIFACT_S3_",
        "E2E_",
        "FS_AI_",
        "FS_CONTEXT_GRAPH_",
        "FS_DEFAULT_",
        "FS_MEMORY_",
        "POSTGRES_",
        "RABBITMQ_",
    )

    for compose_file in ("docker-compose.yaml", "docker-compose.prod.yaml"):
        environment = _service_environment("fs-cli", compose_file=compose_file)

        assert forbidden_exact.isdisjoint(environment)
        assert not any(key.startswith(forbidden_prefixes) for key in environment)


def test_prod_runtime_services_define_mutable_state_contract():
    expected_sources = {
        "fs-app": {
            "/app/.flow-steward/operations": "${FS_DOCKER_HOST_PROJECT_ROOT:-../../}/.flow-steward/operations",
            "/app/extensions": "${FS_EXTENSIONS_DIR:-../../extensions}",
            "/app/logs": "${FS_LOGS_DIR:-../../logs}",
            "/run/flow-steward-runtime": "${FS_INSTALLER_STATE_DIR:-../../.flow-steward}/runtime",
        },
        "fs-worker": {
            "/app/extensions": "${FS_EXTENSIONS_DIR:-../../extensions}",
            "/app/logs": "${FS_LOGS_DIR:-../../logs}",
            "/run/flow-steward-runtime": "${FS_INSTALLER_STATE_DIR:-../../.flow-steward}/runtime",
        },
        "control-plane": {
            "/app/logs": "${FS_LOGS_DIR:-../../logs}",
            "/run/flow-steward-runtime": "${FS_INSTALLER_STATE_DIR:-../../.flow-steward}/runtime",
        },
    }

    for service_name in ("fs-app", "fs-worker", "control-plane"):
        env = _service_environment(service_name, compose_file="docker-compose.prod.yaml")
        sources = _service_volume_sources_by_target(
            service_name, compose_file="docker-compose.prod.yaml"
        )

        if service_name in {"fs-app", "fs-worker"}:
            assert env.get("FS_EXTENSIONS_ROOT") == "/app/extensions"
        else:
            assert "FS_EXTENSIONS_ROOT" not in env
        assert sources == expected_sources[service_name]

    mcp_env = _service_environment("fs-mcp", compose_file="docker-compose.prod.yaml")
    mcp_sources = _service_volume_sources_by_target(
        "fs-mcp", compose_file="docker-compose.prod.yaml"
    )
    assert "FS_EXTENSIONS_ROOT" not in mcp_env
    assert mcp_sources == {}


def test_prod_compose_requires_immutable_extended_postgres_image() -> None:
    pg_service = _service_config("pg-fs", compose_file="docker-compose.prod.yaml")

    assert (
        pg_service.get("build", {}).get("dockerfile") == "infra/docker/postgres-extended/Dockerfile"
    )
    assert (
        pg_service.get("image")
        == "${POSTGRES_IMAGE:?POSTGRES_IMAGE is required for immutable production provenance}"
    )
    assert (
        "shared_preload_libraries=${POSTGRES_SHARED_PRELOAD_LIBRARIES:-age,pg_stat_statements}"
        in (pg_service.get("command") or [])
    )
    sources = _service_volume_sources_by_target("pg-fs", compose_file="docker-compose.prod.yaml")
    assert sources.get("/docker-entrypoint-initdb.d") == "${FS_DB_SCHEMA_DIR:-../../db/schema}"


def test_cli_oauth_token_is_supplied_by_env_file_not_blank_override():
    for service_name in ("fs-app", "fs-test-runner", "fs-audit", "fs-worker", "fs-mcp"):
        env = _service_environment(service_name)
        assert SECOND_RETIRED_TOKEN not in env


def test_fs_mcp_image_excludes_cli_backed_providers():
    dockerfile = (ROOT / "infra" / "mcp" / "Dockerfile").read_text(encoding="utf-8")

    assert FIRST_RETIRED_PACKAGE not in dockerfile
    assert SECOND_RETIRED_PACKAGE not in dockerfile
    assert "useradd -m -s /bin/bash fs" in dockerfile


def test_local_runtime_services_build_from_explicit_role_targets() -> None:
    expected_targets = {
        "fs-app": "app-runtime",
        "fs-frontend": "frontend-development",
        "fs-worker": "worker-runtime",
        "control-plane": "control-runtime",
    }
    for service_name, expected_target in expected_targets.items():
        service = _service_config(service_name)
        assert service.get("build", {}).get("target") == expected_target

    mcp_service = _service_config("fs-mcp")
    assert mcp_service.get("build", {}).get("target") == "local-runtime"
    assert _service_config("fs-test-runner").get("build", {}).get("target") == "audit"
    assert _service_config("fs-audit").get("build", {}).get("target") == "audit"


def test_local_optional_images_use_pinned_non_latest_refs() -> None:
    services = {
        "mailpit": _service_config("mailpit"),
        "sonar-scanner": _service_config("sonar-scanner"),
        "seaweedfs": _service_config("seaweedfs"),
    }

    assert services["mailpit"].get("image") == (
        "axllent/mailpit@sha256:757f22b56c1da03570afdb3d259effe5091018008a81bbedc8158cee7e16fdbc"
    )
    assert services["sonar-scanner"].get("image") == (
        "${SONAR_SCANNER_IMAGE:-sonarsource/sonar-scanner-cli@sha256:23ca0f137965d9dff2198074043fd48d386280bc5d0ccac8c8349cea4cf096a9}"
    )
    assert services["seaweedfs"].get("image") == (
        "chrislusf/seaweedfs@sha256:854479eebcbc0060d803edb27b3bd88a0552e23fde08a26a6482e59aff887a77"
    )


def test_local_mailpit_has_a_readiness_healthcheck() -> None:
    healthcheck = _service_config("mailpit").get("healthcheck") or {}

    assert healthcheck.get("test") == [
        "CMD",
        "wget",
        "--quiet",
        "--spider",
        "http://localhost:8025/readyz",
    ]
    assert healthcheck.get("interval") == "5s"
    assert healthcheck.get("timeout") == "3s"
    assert healthcheck.get("retries") == 12


def test_prod_runtime_stage_stays_free_of_local_cli_and_audit_tooling() -> None:
    dockerfile = ROOT / "infra" / "docker" / "Dockerfile"
    base_body = _dockerfile_stage_body(
        dockerfile,
        "FROM python:3.12-slim AS base",
        "FROM base AS source-slices",
    )
    app_runtime_body = _dockerfile_stage_body(
        dockerfile,
        "FROM runtime-dependencies AS app-runtime",
        "FROM worker-tooling AS worker-runtime",
    )

    assert " gcc " not in f" {base_body} "
    assert " g++ " not in f" {base_body} "
    assert "libpq-dev" not in base_body
    assert "gnupg" not in base_body
    assert "requirements-audit.txt" not in app_runtime_body
    assert "docker-ce-cli" not in app_runtime_body
    assert "playwright install" not in app_runtime_body
    assert SECOND_RETIRED_PACKAGE not in app_runtime_body
    assert FIRST_RETIRED_PACKAGE not in app_runtime_body


def test_worker_runtime_stage_stays_free_of_audit_dependencies() -> None:
    dockerfile = ROOT / "infra" / "docker" / "Dockerfile"
    worker_body = _dockerfile_stage_body(
        dockerfile,
        "FROM worker-tooling AS worker-runtime",
        "FROM control-dependencies AS control-runtime",
    )
    audit_tooling_body = _dockerfile_stage_body(
        dockerfile,
        "FROM worker-tooling AS audit-tooling",
        "FROM runtime-dependencies AS app-runtime",
    )

    assert "requirements-audit.txt" not in worker_body
    assert "playwright install" not in worker_body
    assert "requirements-audit.txt" in audit_tooling_body
    assert "playwright install chromium" in audit_tooling_body


def test_frontend_dependencies_are_installed_before_frontend_source_copy() -> None:
    dockerfile = ROOT / "infra" / "docker" / "Dockerfile"
    dependency_body = _dockerfile_stage_body(
        dockerfile,
        "FROM frontend-base AS frontend-dependencies",
        "FROM frontend-dependencies AS frontend-build",
    )
    development_body = _dockerfile_stage_body(
        dockerfile,
        "FROM frontend-dependencies AS frontend-development",
        "FROM runtime-dependencies AS worker-system-tooling",
    )

    assert "COPY assets/frontend/package.json assets/frontend/package-lock.json" in dependency_body
    assert "npm ci" in dependency_body
    assert "COPY assets/frontend/ assets/frontend/" in development_body
    assert "pip install" not in development_body
    assert "docker-ce-cli" not in development_body


def test_audit_stage_shares_installed_playwright_browsers_with_runtime_user() -> None:
    dockerfile = ROOT / "infra" / "docker" / "Dockerfile"
    audit_body = _dockerfile_stage_body(
        dockerfile,
        "FROM worker-tooling AS audit-tooling",
        "FROM runtime-dependencies AS app-runtime",
    )

    assert "ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright" in audit_body
    assert "chmod -R a+rX /ms-playwright" in audit_body


def test_audit_stage_uses_compatible_semgrep_and_verifies_the_cli() -> None:
    audit_requirements = (ROOT / "requirements-audit.txt").read_text(encoding="utf-8")
    semgrep_requirements = (ROOT / "requirements-semgrep.txt").read_text(encoding="utf-8")
    dockerfile = ROOT / "infra" / "docker" / "Dockerfile"
    audit_body = _dockerfile_stage_body(
        dockerfile,
        "FROM worker-tooling AS audit-tooling",
        "FROM runtime-dependencies AS app-runtime",
    )

    assert "semgrep==" not in audit_requirements
    assert "setuptools==84.1.0" in semgrep_requirements
    assert "semgrep==1.179.0" in semgrep_requirements
    assert "COPY requirements-cli.lock ." in audit_body
    assert "pip install --no-cache-dir --require-hashes -r requirements-cli.lock" in audit_body
    assert "pip install --no-cache-dir -r requirements-audit.txt" in audit_body
    assert "python -m pip check" in audit_body
    assert "python -m venv /opt/semgrep" in audit_body
    assert "/opt/semgrep/bin/pip install --no-cache-dir -r requirements-semgrep.txt" in audit_body
    assert "/opt/semgrep/bin/pip check" in audit_body
    assert "/opt/semgrep/bin/semgrep --version" in audit_body
    assert "ln -s /opt/semgrep/bin/semgrep /usr/local/bin/semgrep" not in audit_body
    assert 'exec /opt/semgrep/bin/semgrep "$@"' in audit_body
    assert 'ENV PATH="/opt/semgrep/bin:${PATH}"' not in audit_body


def test_audit_toolchain_is_cached_before_revision_dependent_source_copy() -> None:
    dockerfile = ROOT / "infra" / "docker" / "Dockerfile"
    tooling_body = _dockerfile_stage_body(
        dockerfile,
        "FROM worker-tooling AS audit-tooling",
        "FROM runtime-dependencies AS app-runtime",
    )
    audit_body = _dockerfile_stage_body(
        dockerfile,
        "FROM audit-tooling AS audit",
        "FROM audit AS dev",
    )

    assert "COPY . ." not in tooling_body
    assert "pip install --no-cache-dir -r requirements-audit.txt" in tooling_body
    assert "playwright install chromium" in tooling_body
    assert "COPY . ." in audit_body
    assert "playwright install chromium" not in audit_body


def test_role_dependency_layers_precede_revision_dependent_source_slices() -> None:
    dockerfile = (ROOT / "infra" / "docker" / "Dockerfile").read_text(encoding="utf-8")

    runtime_lock = dockerfile.index("COPY requirements-runtime.lock .")
    control_lock = dockerfile.index("COPY requirements-control.lock .")
    frontend_lock = dockerfile.index(
        "COPY assets/frontend/package.json assets/frontend/package-lock.json assets/frontend/"
    )
    app_slice = dockerfile.index("--from=source-slices /out/app/ /app/")
    worker_slice = dockerfile.index("--from=source-slices /out/worker/ /app/")
    control_slice = dockerfile.index("--from=source-slices /out/control/ /app/")
    frontend_source = dockerfile.index("COPY assets/frontend/ assets/frontend/")

    assert runtime_lock < app_slice
    assert runtime_lock < worker_slice
    assert control_lock < control_slice
    assert frontend_lock < frontend_source


def test_pip_audit_checks_isolated_semgrep_requirements_separately() -> None:
    lint_script = (ROOT / "core/entrypoints/cli/scripts/lint.sh").read_text(encoding="utf-8")

    assert "FS_PIP_AUDIT_REQUIREMENTS_SEMGREP" in lint_script
    assert "requirements-semgrep.txt" in lint_script
    assert "runtime+audit" in lint_script
    assert "requirements-cli.lock" in lint_script
    assert '"cli:${cli_report}"' in lint_script
    assert "semgrep" in lint_script


def test_prod_mcp_runtime_stage_stays_free_of_local_cli_tooling() -> None:
    dockerfile = ROOT / "infra" / "mcp" / "Dockerfile"
    base_body = _dockerfile_stage_body(
        dockerfile,
        "FROM python:3.12-slim AS base",
        "FROM base AS runtime",
    )
    runtime_body = _dockerfile_stage_body(
        dockerfile,
        "FROM base AS runtime",
        "FROM runtime AS local-runtime",
    )

    assert " gcc " not in f" {base_body} "
    assert " g++ " not in f" {base_body} "
    assert "libpq-dev" not in base_body
    assert "gnupg" not in base_body
    assert SECOND_RETIRED_PACKAGE not in runtime_body
    assert FIRST_RETIRED_PACKAGE not in runtime_body


def test_dockerignore_excludes_all_non_example_env_files_from_build_context() -> None:
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert "**/.env*" in dockerignore
    assert "!.env.prod" not in dockerignore
    assert "!.env.local" not in dockerignore
    assert "!.env.*.local" not in dockerignore
    assert "!.env.example" in dockerignore
