from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_runtime_dockerfiles_drop_to_non_root_user() -> None:
    for relative in ("infra/docker/Dockerfile", "infra/mcp/Dockerfile"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "USER fs" in text


def test_only_host_lifecycle_and_on_demand_test_runner_mount_docker_socket() -> None:
    text = (ROOT / "infra/docker/docker-compose.yaml").read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    services = data.get("services") or {}

    socket_services: set[str] = set()
    for service_name, service in services.items():
        volumes = service.get("volumes") if isinstance(service, dict) else []
        if "/var/run/docker.sock" in "\n".join(str(volume) for volume in volumes or []):
            socket_services.add(service_name)

    assert socket_services == {"fs-cli", "fs-test-runner"}


def test_traefik_config_has_tls_only_router_and_http_redirect() -> None:
    dynamic = (ROOT / "infra/traefik/fs.dynamic.yaml").read_text(encoding="utf-8")
    compose = (ROOT / "infra/docker/docker-compose.prod.yaml").read_text(encoding="utf-8")

    assert "fs-insecure" not in dynamic
    assert "tls: {}" in dynamic
    assert "--entrypoints.web.http.redirections.entrypoint.to=websecure" in compose
    assert "--entrypoints.web.http.redirections.entrypoint.scheme=https" in compose


def test_traefik_publishes_exact_authenticated_mcp_path_over_tls() -> None:
    dynamic = yaml.safe_load((ROOT / "infra/traefik/fs.dynamic.yaml").read_text())
    compose = yaml.safe_load((ROOT / "infra/docker/docker-compose.prod.yaml").read_text())

    routers = dynamic["http"]["routers"]
    services = dynamic["http"]["services"]
    assert routers["fs-mcp"] == {
        # P1-5: the protected-resource discovery document is served by the MCP server.
        "rule": (
            "Path(`/mcp`) || PathPrefix(`/mcp/`) "
            "|| PathPrefix(`/.well-known/oauth-protected-resource`)"
        ),
        "priority": 100,
        "entryPoints": ["websecure"],
        "service": "fs-mcp",
        "tls": {},
    }
    assert services["fs-mcp"]["loadBalancer"]["servers"] == [{"url": "http://fs-mcp:8100"}]
    assert compose["services"]["fs-mcp"]["environment"]["FS_MCP_AUTH_DISABLED"] == "0"
    assert "ports" not in compose["services"]["fs-mcp"]


def test_prod_compose_requires_metrics_token_for_metrics_services() -> None:
    text = (ROOT / "infra/docker/docker-compose.prod.yaml").read_text(encoding="utf-8")
    services = (yaml.safe_load(text) or {}).get("services") or {}

    for service_name in ("fs-app", "fs-worker", "control-plane"):
        value = str((services[service_name].get("environment") or {})["FS_METRICS_TOKEN"])
        assert value.startswith("${FS_METRICS_TOKEN:?")
    assert "FS_METRICS_TOKEN" not in (services["fs-mcp"].get("environment") or {})


def test_security_headers_include_hsts() -> None:
    text = (ROOT / "core/infrastructure/runtime/http_app_lifecycle.py").read_text(encoding="utf-8")

    assert "Strict-Transport-Security" in text
    assert "max-age=63072000; includeSubDomains" in text


def test_compact_runtime_receives_the_production_environment_markers() -> None:
    """AC17-R74: enforcement silently off on a production Compact install.

    `core/infrastructure/security/config.py` decides production from FS_STACK_ENVIRONMENT,
    ENVIRONMENT or APP_ENV, and `_DEFAULT_SCAN_MODE` is "enforce_standard" only when one of
    them says so. docker-compose.compact.yaml declared none of them — only SENTRY_ENVIRONMENT,
    which that code does not read — so a runtime installed with --environment production
    resolved local_development and ran in monitor mode: scanning active, blocking disabled.
    The value was in .env.prod the whole time; only the binding was missing.
    """
    compose = yaml.safe_load(
        (ROOT / "infra/docker/docker-compose.compact.yaml").read_text(encoding="utf-8")
    )
    runtime_environment = compose["x-compact-runtime-environment"]

    assert "FS_STACK_ENVIRONMENT" in runtime_environment
    assert "ENVIRONMENT" in runtime_environment
    # a marker that is absent must not silently become development
    assert "production" in str(runtime_environment["FS_STACK_ENVIRONMENT"])
    assert "production" in str(runtime_environment["ENVIRONMENT"])


def test_compact_environment_markers_reach_the_child_processes() -> None:
    """Binding it in Compose is only half a delivery: the supervisor re-filters the env."""
    from core.infrastructure.config.compact_runtime import CompactRuntimeSettings
    from core.infrastructure.runtime.compact.process_spec import build_process_specs

    settings = CompactRuntimeSettings.from_environment(
        {"FS_DEPLOYMENT_PROFILE": "compact", "FS_WORKER_PROCESSES": "1"}
    )
    specs = build_process_specs(
        settings,
        parent_environment={
            "PATH": "/usr/bin",
            "FS_WORKER_MCP_BEARER_TOKEN": "worker-token",
            "FS_STACK_ENVIRONMENT": "production",
            "ENVIRONMENT": "production",
        },
        public_port=8080,
        observation_fd=7,
    )

    for role in ("app", "worker-0", "control"):
        spec = next(item for item in specs if item.role == role)
        assert spec.environment["FS_STACK_ENVIRONMENT"] == "production", role
        assert spec.environment["ENVIRONMENT"] == "production", role
