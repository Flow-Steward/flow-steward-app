"""Render deployment targets and environments from installed configuration."""

from __future__ import annotations

import re
from collections.abc import Mapping
from ipaddress import ip_address
from pathlib import Path

from core.application.lifecycle.configuration_models import (
    ConfigurationState,
)
from core.application.lifecycle.contracts import (
    ComposeTarget,
    DeploymentConfig,
    profile_owned_environment,
    public_base_url,
)
from core.application.lifecycle.errors import LifecycleError
from core.application.lifecycle.runtime_installation_context import (
    deployment_monitoring_dependencies,
    deployment_monitoring_expectations,
)

_BASE_SERVICES = (
    "traefik",
    "rabbitmq-fs",
    "pg-fs",
    "fs-mcp",
    "fs-app",
    "fs-extension-installer",
    "fs-worker",
    "control-plane",
)


_SCALABLE_HOST_TREES = {
    "FS_LOGS_DIR": "../../logs",
    "FS_EXTENSIONS_DIR": "../../extensions",
}


def _production_preserved_services(compact: bool) -> tuple[str, ...]:
    """Services whose data must survive a production stack operation."""
    return ("pg-fs", "compact-runtime") if compact else ("pg-fs",)


def _public_bind_host(public_host: str) -> str:
    normalized = public_host.strip().strip("[]").lower()
    if normalized in {"localhost", "127.0.0.1"}:
        return "127.0.0.1"
    if normalized == "::1":
        return "[::1]"
    return str(ip_address(0))


class ConfigurationRenderingMixin:
    """Shared rendering methods using the owning service's store and host root.

    This state-free base preserves method overrides on ConfigurationService;
    construction, publication and runtime effects remain with that service.
    """

    store: ConfigurationState
    docker_host_project_root: Path | None

    def derive_target(
        self,
        config: DeploymentConfig,
        *,
        identity_source: DeploymentConfig | None = None,
        project_name_override: str | None = None,
    ) -> ComposeTarget:
        development = config.environment == "development"
        compact = config.deployment_profile == "compact"
        if development:
            base_compose_file = "infra/docker/docker-compose.yaml"
        elif compact:
            base_compose_file = "infra/docker/docker-compose.compact.yaml"
        else:
            base_compose_file = "infra/docker/docker-compose.prod.yaml"
        compose_files = [base_compose_file]
        if compact:
            ingress_file = f"infra/docker/compact/ingress-{config.ingress_mode}.yaml"
            if config.ingress_mode == "external_proxy":
                ingress_file = (
                    f"infra/docker/compact/ingress-external_proxy-{config.proxy_placement}.yaml"
                )
            compose_files.append(ingress_file)
            if config.ingress_mode == "external_traefik" and config.traefik_http_entrypoint.strip():
                compose_files.append(
                    "infra/docker/compact/ingress-external_traefik-http-redirect.yaml"
                )
            if config.tls_mode == "external_resolver":
                compose_files.append("infra/docker/compact/tls-external-resolver.yaml")
            elif config.ingress_mode == "bundled_traefik":
                compose_files.append(
                    "infra/docker/compact/tls-bundled-acme.yaml"
                    if config.tls_mode == "acme"
                    else "infra/docker/compact/tls-bundled-files.yaml"
                )
        elif not development:
            if config.ingress_mode == "external_proxy":
                compose_files.append(
                    f"infra/docker/scalable/ingress-external_proxy-{config.proxy_placement}.yaml"
                )
            elif config.ingress_mode == "external_traefik":
                compose_files.append("infra/docker/scalable/ingress-external_traefik.yaml")
                if config.traefik_http_entrypoint.strip():
                    compose_files.append(
                        "infra/docker/scalable/ingress-external_traefik-http-redirect.yaml"
                    )
                if config.tls_mode == "external_resolver":
                    compose_files.append("infra/docker/scalable/tls-external-resolver.yaml")
            elif config.ingress_mode == "bundled_traefik" and config.tls_mode == "acme":
                compose_files.append("infra/docker/scalable/tls-bundled-acme.yaml")
        if compact and config.file_storage == "bundled":
            # Item 5, step 5: SeaweedFS beside the Compact runtime, on its own volumes.
            compose_files.append("infra/docker/compact/storage-bundled.yaml")
        if compact and config.data_placement == "host_directories":
            # A Compact runtime on the trees a Scalable installation created (item 5).
            compose_files.append("infra/docker/compact/data-host_directories.yaml")
            if config.file_storage == "local_fs":  # AC17-R169: otherwise not a host tree
                compose_files.append("infra/docker/compact/data-host_directories-artifacts.yaml")
        elif not compact and not development and config.data_placement == "compact_volume":
            # A Scalable installation on the trees a Compact one created (item 5).
            compose_files.append("infra/docker/scalable/data-compact_volume.yaml")
        if not compact and config.data_placement == "host_directories":
            compose_files.append("infra/docker/scalable/cli-host-directories.yaml")
            if config.file_storage == "local_fs":
                compose_files.append("infra/docker/scalable/cli-host-directories-artifacts.yaml")
        if development and config.ingress_mode == "external_proxy":
            compose_files.append(
                ".flow-steward/installer.compose.override.yaml"
                if config.proxy_placement == "host"
                else "infra/docker/scalable/ingress-external_proxy-docker.yaml"
            )
        profiles: list[str] = []
        services = (
            ["rabbitmq-fs", "pg-fs", "compact-runtime"]
            if compact
            else [
                service
                for service in _BASE_SERVICES
                if service != "traefik" or config.ingress_mode == "bundled_traefik"
            ]
        )
        if compact and config.ingress_mode == "bundled_traefik":
            services.append("traefik")
        if not compact and not development and config.ingress_mode == "bundled_traefik":
            profiles.append("bundled-ingress")
        if config.file_storage == "bundled":
            profiles.append("storage-seaweedfs")
            services.append("seaweedfs")
        profiles.append("cli")
        services.append("fs-cli")
        if development:
            profiles.extend(("development", "test", "audit", "quality"))
            services.extend(
                (
                    "fs-frontend",
                    "mailpit",
                    "fs-test-runner",
                    "fs-audit",
                    "pg-audit",
                    "rabbitmq-audit",
                    "sonarqube",
                    "sonar-scanner",
                )
            )
        runtime_services = tuple(
            service
            for service in services
            if service not in {"mailpit", "sonarqube", "sonar-scanner"}
            and not (service == "traefik" and config.tls_mode == "reverse_proxy")
        )
        rebuild_services = (
            tuple(
                service
                for service in services
                if service
                in {
                    "pg-fs",
                    "fs-mcp",
                    "fs-app",
                    "fs-extension-installer",
                    "fs-worker",
                    "control-plane",
                    "fs-test-runner",
                    "fs-audit",
                }
            )
            if development
            else _production_preserved_services(compact)
        )
        try:
            project_name = str(project_name_override or "").strip() or (
                self.store.transition_project_name(identity_source)
                if identity_source is not None
                else self.store.project_name(config)
            )
        except ValueError as exc:
            raise LifecycleError.stable_failure(
                code="configuration_identity_invalid",
                cause="The installed Compose project identity is inconsistent.",
                remediation="Repair the installed environment and compose identity before applying configuration.",
                step="configuration",
            ) from exc
        return ComposeTarget(
            stack_environment=config.environment,
            file_storage=config.file_storage,
            deployment_profile=config.deployment_profile,
            runtime_http_port=config.runtime_http_port,
            worker_processes=config.worker_processes,
            env_file=".env" if development else ".env.prod",
            compose_files=tuple(compose_files),
            compose_profiles=tuple(profiles),
            required_services=tuple(services),
            runtime_services=runtime_services,
            rebuild_services=rebuild_services,
            project_name=project_name,
        )

    def compose_environment(
        self,
        config: DeploymentConfig,
        target: ComposeTarget,
    ) -> dict[str, str]:
        """Return the canonical non-secret Compose control-plane environment."""
        values = self._active_compose_values(config, target)
        values.update(
            {
                "FS_TLS_CERTIFICATE_PATH": str(
                    self.store.resolve_project_path(config.certificate_path).resolve()
                ),
                "FS_TLS_PRIVATE_KEY_PATH": str(
                    self.store.resolve_project_path(config.private_key_path).resolve()
                ),
            }
        )
        return values

    @staticmethod
    def _active_compose_values(
        config: DeploymentConfig,
        target: ComposeTarget,
    ) -> dict[str, str]:
        values = {
            "FS_STACK_ENVIRONMENT": config.environment,
            "FS_DEPLOYMENT_PROFILE": config.deployment_profile,
            "FS_INGRESS_MODE": config.ingress_mode,
            "FS_PUBLIC_HOST": config.public_host,
            "FS_FILE_STORAGE": config.file_storage,
            "FS_RUNTIME_HTTP_PORT": str(config.runtime_http_port),
            # AC17-R92: Compose reads the redirect entrypoint from HTTP_PORT; it is an
            # answer now, not a variable the operator has to know to export.
            "HTTP_PORT": str(config.redirect_http_port),
            "FS_PROXY_PLACEMENT": config.proxy_placement,
            "FS_PROXY_KIND": config.proxy_kind,
            "FS_PROXY_TRUSTED_IPS": ",".join(config.proxy_trusted_ips),
            "FS_TRAEFIK_NETWORK": config.traefik_network,
            "FS_TRAEFIK_HTTPS_ENTRYPOINT": config.traefik_https_entrypoint,
            "FS_TRAEFIK_HTTP_ENTRYPOINT": config.traefik_http_entrypoint,
            "FS_TRAEFIK_CERT_RESOLVER": config.traefik_cert_resolver,
            "FS_PROXY_NETWORK": config.proxy_network,
            "FS_REVERSE_PROXY_BACKEND_PORT": str(config.reverse_proxy_backend_port),
            "FS_REVERSE_PROXY_MCP_PORT": str(config.reverse_proxy_mcp_port),
            "FS_ACME_CONTACT": config.acme_contact,
            "FS_ACME_DIRECTORY_URL": config.acme_directory_url,
            "FS_TLS_CERTIFICATE_PATH": config.certificate_path,
            "FS_TLS_PRIVATE_KEY_PATH": config.private_key_path,
            "FS_EXTENSION_INSTALLER_ORIGIN": config.extension_installer_origin,
            "FS_ENV_FILE": target.env_file,
            "FS_COMPOSE_FILES": " ".join(target.compose_files),
            "FS_COMPOSE_PROFILES": " ".join(target.compose_profiles),
            "FS_COMPOSE_SERVICES": " ".join(target.required_services),
            "FS_REBUILD_SERVICES": " ".join(target.rebuild_services),
        }
        if config.deployment_profile == "compact":
            values["FS_WORKER_PROCESSES"] = str(config.worker_processes)
        else:
            values["FS_WORKER_REPLICAS"] = str(config.worker_replicas)
        return values

    def _prepared_environment(
        self,
        config: DeploymentConfig,
        *,
        source: DeploymentConfig | None = None,
        environment_overlay: Mapping[str, str] | None = None,
    ) -> tuple[ComposeTarget, Path, dict[str, str]]:
        target = self.derive_target(config, identity_source=source)
        path = self.store.env_path(config)
        source_config = source or config
        durable_reader = getattr(self.store, "read_durable_environment", None)
        existing = (
            durable_reader(source_config)
            if callable(durable_reader)
            else self.store.read_env_values(self.store.env_path(source_config))
        )
        if environment_overlay is not None:
            existing.update({str(name): str(value) for name, value in environment_overlay.items()})
        try:
            managed = self.store.build_environment(config, existing)
        except ValueError as exc:
            raise LifecycleError.stable_failure(
                code="configuration_durable_secrets_unavailable",
                cause="The installed durable secret configuration is incomplete.",
                remediation="Run ./install.sh to provision the missing durable secrets, then retry.",
                step="configuration",
            ) from exc
        public_http_port = str(existing.get("HTTP_PORT") or "").strip()
        if public_http_port:
            # re.ASCII is load-bearing: a bare \d matches every Unicode decimal digit for
            # str patterns, and int() parses those too, so "8\u066099" would pass the
            # pattern and then be read as 8099. The flag keeps this exactly equivalent to
            # the [0-9] class it replaces (Sonar python:S6353).
            if not re.fullmatch(r"[1-9]\d{0,4}", public_http_port, re.ASCII) or not (
                1 <= int(public_http_port) <= 65535
            ):
                raise LifecycleError.stable_failure(
                    code="configuration_public_http_port_invalid",
                    cause="The installed public HTTP port is invalid.",
                    remediation="Repair HTTP_PORT in the installed environment, then retry.",
                    step="configuration",
                    details={"field": "HTTP_PORT"},
                )
            managed["HTTP_PORT"] = public_http_port
        base_url = public_base_url(config)
        managed.update(
            {
                "FS_PUBLIC_BASE_URL": base_url,
                "FS_PUBLIC_HOST": config.public_host,
                "FS_PUBLIC_BIND_HOST": _public_bind_host(config.public_host),
                "FS_DEPLOYMENT_PROFILE": config.deployment_profile,
                "FS_INGRESS_MODE": config.ingress_mode,
                "APP_PORT": str(config.public_port),
                "FS_AUTH_TRUSTED_ORIGINS": base_url,
                "FS_PROXY_PLACEMENT": config.proxy_placement,
                "FS_PROXY_KIND": config.proxy_kind,
                "FS_PROXY_TRUSTED_IPS": ",".join(config.proxy_trusted_ips),
                "FS_TRAEFIK_NETWORK": config.traefik_network,
                "FS_TRAEFIK_HTTPS_ENTRYPOINT": config.traefik_https_entrypoint,
                "FS_TRAEFIK_HTTP_ENTRYPOINT": config.traefik_http_entrypoint,
                "FS_TRAEFIK_CERT_RESOLVER": config.traefik_cert_resolver,
                "FS_PROXY_NETWORK": config.proxy_network,
                "FS_REVERSE_PROXY_BACKEND_PORT": str(config.reverse_proxy_backend_port),
                "FS_REVERSE_PROXY_MCP_PORT": str(config.reverse_proxy_mcp_port),
                "FS_ACME_CONTACT": config.acme_contact,
                "FS_ACME_DIRECTORY_URL": config.acme_directory_url,
                "FS_TLS_CERTIFICATE_PATH": str(
                    self.store.resolve_project_path(config.certificate_path).resolve()
                ),
                "FS_TLS_PRIVATE_KEY_PATH": str(
                    self.store.resolve_project_path(config.private_key_path).resolve()
                ),
                "FS_EXTENSION_INSTALLER_ORIGIN": config.extension_installer_origin,
            }
        )
        # AC17-R167: never the source profile's, which `existing` holds on a switch.
        managed.update(profile_owned_environment(config.deployment_profile))
        managed["FS_MONITORING_EXPECTATIONS"] = deployment_monitoring_expectations(config)
        managed["FS_MONITORING_DEPENDENCIES"] = deployment_monitoring_dependencies(config)
        if config.deployment_profile == "compact" and config.data_placement == "host_directories":
            # AC17-R174: the Compact overlay binds Scalable's host trees and requires where
            # they are. The apply renders from these values alone (--env-file /dev/null).
            # The installer recorded them in the installed file; else Scalable's own
            # defaults.
            for name, default in _SCALABLE_HOST_TREES.items():
                managed[name] = str(existing.get(name) or managed.get(name) or default)
        if config.deployment_profile == "compact":
            managed.update(
                {
                    "FS_RUNTIME_HTTP_PORT": str(config.runtime_http_port),
                    "FS_WORKER_PROCESSES": str(config.worker_processes),
                }
            )
        else:
            # Review P2: read by fs-worker's `deploy.replicas`, so every `compose up` --
            # apply, restore, installer update -- keeps the configured worker count.
            managed["FS_WORKER_REPLICAS"] = str(config.worker_replicas)
        if config.environment == "production":
            candidate_state_dir = self.store.pending_path.parent.resolve()
            candidate_prod_env = path.resolve()
            if self.docker_host_project_root is not None:
                candidate_state_dir = (self.docker_host_project_root / ".flow-steward").resolve()
                candidate_prod_env = (self.docker_host_project_root / path.name).resolve()
            managed.update(
                {
                    "FS_CANDIDATE_STATE_DIR": str(candidate_state_dir),
                    "FS_CANDIDATE_PROD_ENV": str(candidate_prod_env),
                }
            )
        if config.file_storage == "external_s3":
            managed.update(
                {
                    "ARTIFACT_S3_ENDPOINT_URL": config.s3_endpoint,
                    "ARTIFACT_S3_BUCKET": config.s3_bucket,
                    "ARTIFACT_S3_REGION": config.s3_region,
                    "ARTIFACT_S3_USE_SSL": (
                        "true" if config.s3_endpoint.lower().startswith("https://") else "false"
                    ),
                }
            )
        return target, path, managed
