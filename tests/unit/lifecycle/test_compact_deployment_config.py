from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from core.application.installer.spec import INSTALLER_SPEC
from core.application.lifecycle.configuration_service import ConfigurationService
from core.application.lifecycle.contracts import DeploymentConfig
from core.application.management.command_specs.config import CONFIG_COMMAND_SPECS
from core.infrastructure.config.compact_runtime import (
    CompactPortMap,
    CompactRuntimeSettings,
)


def _derivation_service() -> ConfigurationService:
    service = object.__new__(ConfigurationService)
    service.store = SimpleNamespace(project_name=lambda _config: "flow-steward")
    return service


@pytest.mark.parametrize("raw", ["", "0", "-1", "abc", "1.5", "+2", " 2"])
def test_compact_worker_count_rejects_explicit_invalid_values(raw: str) -> None:
    with pytest.raises(ValueError, match="FS_WORKER_PROCESSES"):
        CompactRuntimeSettings.from_environment(
            {"FS_DEPLOYMENT_PROFILE": "compact", "FS_WORKER_PROCESSES": raw}
        )


def test_compact_worker_count_defaults_only_when_absent() -> None:
    settings = CompactRuntimeSettings.from_environment({"FS_DEPLOYMENT_PROFILE": "compact"})

    assert settings.worker_processes == 2


def test_compact_runtime_budget_defaults_are_explicit() -> None:
    settings = CompactRuntimeSettings.from_environment({"FS_DEPLOYMENT_PROFILE": "compact"})

    assert (
        settings.startup_timeout_seconds,
        settings.restart_limit,
        settings.restart_window_seconds,
        settings.restart_backoff_seconds,
        settings.restart_backoff_max_seconds,
        settings.shutdown_grace_seconds,
        settings.kill_reserve_seconds,
        settings.health_interval_seconds,
        settings.health_stale_seconds,
        settings.dependency_timeout_seconds,
    ) == (300, 3, 300, 2, 30, 180, 10, 2, 10, 300)


@pytest.mark.parametrize(
    ("name", "value", "message"),
    (
        ("FS_COMPACT_STARTUP_TIMEOUT_SECONDS", "", "decimal integer"),
        ("FS_COMPACT_RESTART_LIMIT", "0", "between 1"),
        ("FS_COMPACT_RESTART_WINDOW_SECONDS", "-1", "decimal integer"),
        ("FS_COMPACT_SHUTDOWN_GRACE_SECONDS", "1.5", "decimal integer"),
    ),
)
def test_compact_runtime_budget_rejects_invalid_explicit_values(
    name: str,
    value: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        CompactRuntimeSettings.from_environment({"FS_DEPLOYMENT_PROFILE": "compact", name: value})


@pytest.mark.parametrize(
    "overrides",
    (
        {
            "FS_COMPACT_SHUTDOWN_GRACE_SECONDS": "10",
            "FS_COMPACT_KILL_RESERVE_SECONDS": "10",
        },
        {
            "FS_COMPACT_HEALTH_INTERVAL_SECONDS": "10",
            "FS_COMPACT_HEALTH_STALE_SECONDS": "10",
        },
        {
            "FS_COMPACT_RESTART_BACKOFF_SECONDS": "31",
            "FS_COMPACT_RESTART_BACKOFF_MAX_SECONDS": "30",
        },
    ),
)
def test_compact_runtime_budget_rejects_invalid_relationships(
    overrides: dict[str, str],
) -> None:
    with pytest.raises(ValueError, match="Compact runtime budget"):
        CompactRuntimeSettings.from_environment({"FS_DEPLOYMENT_PROFILE": "compact", **overrides})


def test_scalable_worker_rejects_explicit_compact_pool_setting() -> None:
    with pytest.raises(ValueError, match="Compact-only"):
        CompactRuntimeSettings.from_environment(
            {"FS_DEPLOYMENT_PROFILE": "scalable", "FS_WORKER_PROCESSES": "2"}
        )


@pytest.mark.parametrize(
    ("worker_processes", "expected_worker_ports"),
    ((1, (9200,)), (2, (9200, 9201)), (4, (9200, 9201, 9202, 9203))),
)
def test_compact_port_map_assigns_one_unique_port_per_worker(
    worker_processes: int,
    expected_worker_ports: tuple[int, ...],
) -> None:
    ports = CompactPortMap.build(public_port=8080, worker_processes=worker_processes)

    assert ports.worker_metrics_ports == expected_worker_ports
    assert len(ports.all_ports()) == len(set(ports.all_ports()))


@pytest.mark.parametrize("public_port", [8099, 8100, 8101, 9100, 9102, 9200])
def test_compact_port_map_rejects_public_port_collision(public_port: int) -> None:
    with pytest.raises(ValueError, match="port map contains a collision"):
        CompactPortMap.build(public_port=public_port, worker_processes=2)


def test_compact_port_map_rejects_worker_metrics_overflow() -> None:
    with pytest.raises(ValueError, match="worker metrics port exceeds 65535"):
        CompactPortMap.build(public_port=8080, worker_processes=56337)


def test_compact_production_defaults_to_local_fs() -> None:
    config = DeploymentConfig(environment="production", deployment_profile="compact")

    assert config.file_storage == "local_fs"


def test_compact_development_is_rejected() -> None:
    with pytest.raises(ValidationError, match=r"Compact.*production"):
        DeploymentConfig(environment="development", deployment_profile="compact")


def test_legacy_config_preserves_scalable_bundled_storage() -> None:
    config = DeploymentConfig.model_validate({"environment": "production"})

    assert (config.deployment_profile, config.file_storage) == ("scalable", "bundled")


def test_explicit_compact_storage_choice_is_preserved() -> None:
    config = DeploymentConfig(
        environment="production",
        deployment_profile="compact",
        file_storage="external_s3",
        s3_endpoint="https://objects.example.test",
    )

    assert config.file_storage == "external_s3"


def test_compact_runtime_inputs_are_strict_and_closed() -> None:
    config = DeploymentConfig(
        environment="production",
        deployment_profile="compact",
        runtime_http_port=8080,
        worker_processes=4,
    )

    assert (config.runtime_http_port, config.worker_processes) == (8080, 4)

    with pytest.raises(ValidationError):
        DeploymentConfig(
            environment="production",
            deployment_profile="compact",
            runtime_http_port=0,
        )


def test_install_and_config_specs_expose_all_deployment_selection_inputs() -> None:
    expected = {
        "deployment_profile",
        "runtime_http_port",
        "worker_processes",
        "file_storage",
        "ingress_mode",
        "tls_mode",
        "public_host",
        "public_port",
        "reverse_proxy_backend_host",
        "reverse_proxy_backend_port",
        "reverse_proxy_mcp_port",
        "proxy_placement",
        "proxy_kind",
        "proxy_trusted_ips",
        "traefik_network",
        "traefik_https_entrypoint",
        "traefik_http_entrypoint",
        "traefik_cert_resolver",
        "proxy_network",
        "acme_contact",
        "acme_directory_url",
        "extension_installer_origin",
        "certificate_path",
        "private_key_path",
    }
    config_set = next(spec for spec in CONFIG_COMMAND_SPECS if spec.command_id == "config.set")

    assert expected <= {field.key for field in INSTALLER_SPEC.fields}
    assert expected <= {field.key for field in config_set.fields}


def test_comma_separated_trusted_proxy_peers_normalize_to_closed_tuple() -> None:
    config = DeploymentConfig.model_validate(
        {
            "environment": "production",
            "proxy_trusted_ips": "10.0.0.0/8, 192.0.2.1/32",
        }
    )

    assert config.proxy_trusted_ips == ("10.0.0.0/8", "192.0.2.1/32")


def test_compact_target_and_compose_environment_retain_profile_inputs() -> None:
    config = DeploymentConfig(
        environment="production",
        deployment_profile="compact",
        worker_processes=4,
    )

    target = _derivation_service().derive_target(config)
    values = ConfigurationService._active_compose_values(config, target)

    assert target.deployment_profile == "compact"
    assert target.worker_processes == 4
    assert "seaweedfs" not in target.required_services
    assert values["FS_DEPLOYMENT_PROFILE"] == "compact"
    assert values["FS_RUNTIME_HTTP_PORT"] == "8080"
    assert values["FS_WORKER_PROCESSES"] == "4"


def test_scalable_compose_environment_does_not_project_compact_worker_count() -> None:
    config = DeploymentConfig(environment="production", worker_processes=4)

    target = _derivation_service().derive_target(config)
    values = ConfigurationService._active_compose_values(config, target)

    assert "FS_WORKER_PROCESSES" not in values


def test_legacy_reverse_proxy_maps_to_external_host_proxy() -> None:
    config = DeploymentConfig.model_validate(
        {"environment": "production", "tls_mode": "reverse_proxy"}
    )

    assert config.ingress_mode == "external_proxy"
    assert config.proxy_placement == "host"


@pytest.mark.parametrize(
    ("ingress_mode", "tls_mode"),
    (
        ("bundled_traefik", "platform_managed"),
        ("external_traefik", "reverse_proxy"),
        ("external_proxy", "provided"),
        ("platform", "local"),
    ),
)
def test_ingress_and_tls_owner_must_match(ingress_mode: str, tls_mode: str) -> None:
    # AC17-R78 changed the wording, not the rule. The message used to say only that the pair
    # was incompatible, leaving the operator to guess which of the two to change and to what,
    # while the permitted table sat on the line above the check.
    with pytest.raises(ValidationError) as raised:
        DeploymentConfig(
            environment="production",
            deployment_profile="compact",
            ingress_mode=ingress_mode,
            tls_mode=tls_mode,
            traefik_network="shared",
        )

    message = str(raised.value)
    assert ingress_mode in message
    assert tls_mode in message
    assert "permitted tls_mode values" in message


def test_platform_compact_target_uses_three_service_base_and_platform_overlay() -> None:
    config = DeploymentConfig(
        environment="production",
        deployment_profile="compact",
        ingress_mode="platform",
        tls_mode="platform_managed",
    )

    target = _derivation_service().derive_target(config)

    assert target.compose_files == (
        "infra/docker/docker-compose.compact.yaml",
        "infra/docker/compact/ingress-platform.yaml",
    )
    assert target.required_services == ("rabbitmq-fs", "pg-fs", "compact-runtime", "fs-cli")


def test_external_traefik_resolver_overlay_is_only_selected_for_resolver_mode() -> None:
    resolver = DeploymentConfig(
        environment="production",
        deployment_profile="compact",
        ingress_mode="external_traefik",
        tls_mode="external_resolver",
        traefik_network="shared-ingress",
        traefik_cert_resolver="letsencrypt",
    )
    certificate = resolver.model_copy(
        update={"tls_mode": "external_certificate", "traefik_cert_resolver": ""}
    )

    assert _derivation_service().derive_target(resolver).compose_files[-1] == (
        "infra/docker/compact/tls-external-resolver.yaml"
    )
    assert "tls-external-resolver.yaml" not in " ".join(
        _derivation_service().derive_target(certificate).compose_files
    )


def test_bundled_traefik_selects_distinct_file_and_acme_tls_state() -> None:
    provided = DeploymentConfig(
        environment="production",
        deployment_profile="compact",
        ingress_mode="bundled_traefik",
        tls_mode="provided",
    )
    acme = provided.model_copy(
        update={
            "tls_mode": "acme",
            "public_port": 443,
            "acme_contact": "ops@example.test",
            "acme_directory_url": "https://ca.example.test/directory",
        }
    )

    assert (
        _derivation_service()
        .derive_target(provided)
        .compose_files[-1]
        .endswith("tls-bundled-files.yaml")
    )
    assert (
        _derivation_service()
        .derive_target(acme)
        .compose_files[-1]
        .endswith("tls-bundled-acme.yaml")
    )


def test_compact_file_tls_mounts_certificate_in_active_dynamic_directory() -> None:
    compose = Path("infra/docker/compact/tls-bundled-files.yaml").read_text(encoding="utf-8")

    assert ":/run/traefik-dynamic/compact-certificate.yaml:ro" in compose
    assert ":/etc/traefik/dynamic/compact-certificate.yaml:ro" not in compose


@pytest.mark.parametrize(
    "dynamic_config",
    (
        "infra/docker/compact/public-certificate.yaml",
        "infra/traefik/fs.dynamic.yaml",
    ),
)
def test_bundled_traefik_serves_the_installation_certificate_for_unmatched_sni(
    dynamic_config: str,
) -> None:
    """AC17-R83: a client whose SNI matches nothing must still get the real certificate.

    `tls.certificates` alone is an SNI-matched set with no fallback of its own, so Traefik
    answered an unmatched name with its own `CN=TRAEFIK DEFAULT CERT`. Measured on two
    stands; on one of them a client sending no SNI at all got the dummy too. Ordinary
    clients -- browser, curl, webhook, monitor -- received the wrong certificate, and the
    installer's own health probe, pinned to the generated one, could never pass. The
    Compact profile shipped without the store that the Scalable dynamic config had carried
    all along, so both are pinned here together.
    """
    import yaml

    document = yaml.safe_load(Path(dynamic_config).read_text(encoding="utf-8"))
    default_certificate = document["tls"]["stores"]["default"]["defaultCertificate"]

    assert default_certificate["certFile"] == "/etc/traefik/certs/cert.pem"
    assert default_certificate["keyFile"] == "/etc/traefik/certs/key.pem"


@pytest.mark.parametrize("deployment_profile", ("compact", "scalable"))
@pytest.mark.parametrize("file_storage", ("local_fs", "bundled", "external_s3"))
@pytest.mark.parametrize(
    ("ingress_mode", "tls_mode", "extra"),
    (
        ("bundled_traefik", "local", {}),
        ("bundled_traefik", "self_signed", {}),
        ("bundled_traefik", "provided", {}),
        (
            "bundled_traefik",
            "acme",
            {
                "acme_contact": "ops@example.test",
                "acme_directory_url": "https://ca.example.test/directory",
            },
        ),
        (
            "external_traefik",
            "external_resolver",
            {"traefik_network": "shared", "traefik_cert_resolver": "staging"},
        ),
        (
            "external_traefik",
            "external_certificate",
            {"traefik_network": "shared"},
        ),
        ("external_proxy", "reverse_proxy", {}),
    ),
)
def test_static_profile_ingress_tls_storage_matrix_derives_existing_compose_files(
    deployment_profile: str,
    file_storage: str,
    ingress_mode: str,
    tls_mode: str,
    extra: dict[str, str],
) -> None:
    values: dict[str, object] = {
        "environment": "production",
        "deployment_profile": deployment_profile,
        "file_storage": file_storage,
        "ingress_mode": ingress_mode,
        "tls_mode": tls_mode,
        **extra,
    }
    if file_storage == "external_s3":
        values["s3_endpoint"] = "https://s3.example.test"
    config = DeploymentConfig.model_validate(values)

    target = _derivation_service().derive_target(config)

    assert target.deployment_profile == deployment_profile
    assert target.file_storage == file_storage
    assert all(
        Path(path).is_file() or path.startswith(".flow-steward/") for path in target.compose_files
    )
    assert ("traefik" in target.required_services) is (ingress_mode == "bundled_traefik")


@pytest.mark.parametrize("file_storage", ("local_fs", "bundled", "external_s3"))
def test_platform_ingress_matrix_is_compact_only(file_storage: str) -> None:
    values: dict[str, object] = {
        "environment": "production",
        "deployment_profile": "compact",
        "file_storage": file_storage,
        "ingress_mode": "platform",
        "tls_mode": "platform_managed",
    }
    if file_storage == "external_s3":
        values["s3_endpoint"] = "https://s3.example.test"

    target = _derivation_service().derive_target(DeploymentConfig.model_validate(values))

    assert "traefik" not in target.required_services
    # the ingress overlay; the storage overlay may follow it (item 5, step 5)
    assert "infra/docker/compact/ingress-platform.yaml" in target.compose_files


def test_scalable_external_traefik_removes_bundled_proxy_and_routes_both_backends() -> None:
    config = DeploymentConfig(
        environment="production",
        deployment_profile="scalable",
        ingress_mode="external_traefik",
        tls_mode="external_resolver",
        traefik_network="shared-ingress",
        traefik_cert_resolver="production",
    )

    target = _derivation_service().derive_target(config)

    assert target.compose_files == (
        "infra/docker/docker-compose.prod.yaml",
        "infra/docker/scalable/ingress-external_traefik.yaml",
        # the default HTTP entrypoint is "web", so the redirect router is published
        "infra/docker/scalable/ingress-external_traefik-http-redirect.yaml",
        "infra/docker/scalable/tls-external-resolver.yaml",
        "infra/docker/scalable/cli-host-directories.yaml",
    )
    assert "traefik" not in target.required_services
    assert "bundled-ingress" not in target.compose_profiles


def test_scalable_bundled_traefik_is_explicit_profile() -> None:
    config = DeploymentConfig(
        environment="production",
        deployment_profile="scalable",
        ingress_mode="bundled_traefik",
        tls_mode="provided",
    )

    target = _derivation_service().derive_target(config)

    assert "traefik" in target.required_services
    assert "bundled-ingress" in target.compose_profiles


def test_scalable_external_proxy_uses_canonical_static_overlay() -> None:
    config = DeploymentConfig(
        environment="production",
        deployment_profile="scalable",
        ingress_mode="external_proxy",
        tls_mode="reverse_proxy",
    )

    target = _derivation_service().derive_target(config)

    assert target.compose_files == (
        "infra/docker/docker-compose.prod.yaml",
        "infra/docker/scalable/ingress-external_proxy-host.yaml",
        "infra/docker/scalable/cli-host-directories.yaml",
    )
    assert "traefik" not in target.required_services


@pytest.mark.parametrize("deployment_profile", ("compact", "scalable"))
def test_docker_external_proxy_selects_shared_network_overlay(
    deployment_profile: str,
) -> None:
    config = DeploymentConfig(
        environment="production",
        deployment_profile=deployment_profile,  # type: ignore[arg-type]
        file_storage="local_fs",
        ingress_mode="external_proxy",
        tls_mode="reverse_proxy",
        proxy_placement="docker",
        proxy_network="shared-proxy",
    )

    target = _derivation_service().derive_target(config)

    ingress_overlay = f"infra/docker/{deployment_profile}/ingress-external_proxy-docker.yaml"
    assert ingress_overlay in target.compose_files
    assert "FS_PROXY_NETWORK" in Path(ingress_overlay).read_text(encoding="utf-8")


def test_compact_bundled_router_renders_configured_internal_port() -> None:
    script = Path("infra/docker/compact/render-public-router.sh").read_text(encoding="utf-8")
    template = Path("infra/docker/compact/public-router.yaml.tmpl").read_text(encoding="utf-8")

    assert "FS_RUNTIME_HTTP_PORT" in script
    assert "__FS_RUNTIME_HTTP_PORT__" in template
    assert "compact-runtime:8080" not in template


def _external_traefik_config(**overrides):
    from core.application.lifecycle.contracts import DeploymentConfig

    values = {
        "environment": "production",
        "deployment_profile": "compact",
        "file_storage": "local_fs",
        "ingress_mode": "external_traefik",
        "tls_mode": "external_resolver",
        "traefik_network": "traefik",
        "traefik_cert_resolver": "myresolver",
        "public_host": "fs.example.eu",
        "public_port": 443,
    }
    values.update(overrides)
    return DeploymentConfig(**values)


@pytest.mark.parametrize(
    ("profile", "overlay"),
    (
        ("compact", "infra/docker/compact/ingress-external_traefik-http-redirect.yaml"),
        ("scalable", "infra/docker/scalable/ingress-external_traefik-http-redirect.yaml"),
    ),
)
def test_external_traefik_publishes_an_http_redirect_router(profile: str, overlay: str) -> None:
    """AC17-R45: plain http:// returned 404 because no router existed on the HTTP entrypoint."""
    config = _external_traefik_config(deployment_profile=profile)
    target = _derivation_service().derive_target(config)

    assert overlay in target.compose_files


@pytest.mark.parametrize("profile", ("compact", "scalable"))
def test_empty_http_entrypoint_publishes_no_http_router(profile: str) -> None:
    """A router on an entrypoint the external Traefik does not define breaks its dashboard."""
    config = _external_traefik_config(deployment_profile=profile, traefik_http_entrypoint="")
    target = _derivation_service().derive_target(config)

    assert not any("http-redirect" in path for path in target.compose_files)


def test_http_redirect_overlay_names_router_and_middleware_by_project() -> None:
    """Two installations must be able to share one external Traefik."""
    from pathlib import Path

    for overlay in (
        "infra/docker/compact/ingress-external_traefik-http-redirect.yaml",
        "infra/docker/scalable/ingress-external_traefik-http-redirect.yaml",
    ):
        text = Path(overlay).read_text(encoding="utf-8")
        assert "ports:" not in text, "the redirect must publish no host port"
        assert "FS_TRAEFIK_HTTP_ENTRYPOINT" in text
        assert "redirectscheme.permanent=true" in text
        for line in text.splitlines():
            if "traefik.http.routers." in line or "traefik.http.middlewares." in line:
                assert "${COMPOSE_PROJECT_NAME:-flow-steward}" in line, line
