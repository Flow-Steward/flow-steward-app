from __future__ import annotations

import pytest

from core.application.lifecycle.proxy_renderer import ProxyRenderConfig, render_proxy_fragment


@pytest.mark.parametrize("kind", ("nginx", "apache"))
@pytest.mark.parametrize("profile", ("compact", "scalable"))
def test_proxy_fragments_route_exact_mcp_paths_and_app_fallback(kind: str, profile: str) -> None:
    config = ProxyRenderConfig(
        kind=kind,  # type: ignore[arg-type]
        deployment_profile=profile,  # type: ignore[arg-type]
        public_host="flow.example.test",
        app_backend="flow-app:8099",
        mcp_backend="flow-mcp:8100" if profile == "scalable" else None,
    )

    rendered = render_proxy_fragment(config)

    assert "/mcp" in rendered
    assert "flow-app:8099" in rendered
    if profile == "scalable":
        assert "flow-mcp:8100" in rendered
        assert rendered.index("/mcp") < rendered.rindex("flow-app:8099")
    else:
        assert "flow-mcp:8100" not in rendered
    assert "/mcpx" not in rendered
    assert "Authorization" in rendered or kind == "apache"


def test_a_stored_caddy_choice_is_carried_forward_rather_than_rejected(
) -> None:
    """Caddy left the supported set; an installation that chose it must still load.

    Every configuration read goes through DeploymentConfig.model_validate, including
    rollback snapshots, so narrowing the literal without this would not have removed
    support -- it would have made those installations unreadable.
    """
    from core.application.lifecycle.contracts import DeploymentConfig

    config = DeploymentConfig.model_validate(
        {"ingress_mode": "external_proxy", "tls_mode": "reverse_proxy", "proxy_kind": "caddy"}
    )

    assert config.proxy_kind == "nginx"


def test_scalable_proxy_requires_distinct_mcp_backend() -> None:
    with pytest.raises(ValueError, match="MCP backend"):
        ProxyRenderConfig(
            kind="nginx",
            deployment_profile="scalable",
            public_host="flow.example.test",
            app_backend="127.0.0.1:18099",
        )


@pytest.mark.parametrize(
    "backend",
    ("http://127.0.0.1:8099", "localhost", "name:0", "name:65536", "name;evil:80"),
)
def test_proxy_backend_rejects_non_authority_input(backend: str) -> None:
    with pytest.raises(ValueError, match="backend"):
        ProxyRenderConfig(
            kind="apache",
            deployment_profile="compact",
            public_host="flow.example.test",
            app_backend=backend,
        )


def _service(**overrides):
    """A configuration service whose stored active configuration is the one given."""
    from types import SimpleNamespace

    from core.application.lifecycle.configuration_service import ConfigurationService
    from core.application.lifecycle.contracts import DeploymentConfig

    active = DeploymentConfig(
        environment="production",
        ingress_mode="external_proxy",
        tls_mode="reverse_proxy",
        public_host="flow.example.test",
        **overrides,
    )
    store = SimpleNamespace(
        read_active=lambda: active,
        read_pending=lambda: None,
        secret_statuses=lambda _config: {},
    )
    service = ConfigurationService(store=store)
    service.read_active = lambda: active  # type: ignore[method-assign]
    return service


def test_the_fragment_is_rendered_from_what_the_installation_already_recorded() -> None:
    """AC17-R113: the renderer had no way to reach it from the product at all.

    `render_proxy_fragment` was imported by two test files and nothing else, while the
    operations guide told an operator to open the source and call a Python function by
    hand -- for an ingress mode the installer offers beside the other two.
    """
    result = _service(
        proxy_kind="nginx",
        proxy_placement="host",
        deployment_profile="scalable",
        reverse_proxy_backend_host="127.0.0.1",
        reverse_proxy_backend_port=18099,
        reverse_proxy_mcp_port=18100,
    ).proxy_fragment()

    assert result.app_backend == "127.0.0.1:18099"
    assert result.mcp_backend == "127.0.0.1:18100"
    assert "server_name flow.example.test;" in result.fragment
    assert "proxy_pass http://127.0.0.1:18100;" in result.fragment


def test_docker_placement_addresses_the_aliases_the_overlay_declares() -> None:
    """The backends differ by placement, and the difference is derived, not assumed.

    infra/docker/*/ingress-external_proxy-docker.yaml joins the proxy network under
    `<project>-app-proxy` and `<project>-mcp-proxy`, where the container ports apply
    rather than the published ones. Getting this wrong would hand an operator a
    fragment that parses and routes nowhere.
    """
    result = _service(
        proxy_kind="apache",
        proxy_placement="docker",
        deployment_profile="scalable",
        proxy_network="flow-proxy",
    ).proxy_fragment()

    assert result.app_backend == "flow-steward-app-proxy:8099"
    assert result.mcp_backend == "flow-steward-mcp-proxy:8100"
    assert "flow-steward-mcp-proxy:8100" in result.fragment


def test_compact_renders_one_backend_because_it_runs_one_container() -> None:
    result = _service(
        proxy_kind="nginx",
        proxy_placement="host",
        deployment_profile="compact",
        file_storage="local_fs",
        reverse_proxy_backend_host="127.0.0.1",
        reverse_proxy_backend_port=18099,
    ).proxy_fragment()

    assert result.mcp_backend == ""
    assert result.fragment.count("127.0.0.1:18099") >= 2


def test_an_installation_without_an_external_proxy_is_told_so(
) -> None:
    """Refusing by naming the mode beats rendering a fragment for nobody."""
    from core.application.management.errors import ServicePreconditionError

    service = _service(
        proxy_kind="nginx",
        proxy_placement="host",
        deployment_profile="compact",
        file_storage="local_fs",
    )
    service.read_active().__dict__  # touch, so the override below is unambiguous
    from core.application.lifecycle.contracts import DeploymentConfig

    bundled = DeploymentConfig(
        environment="production",
        ingress_mode="bundled_traefik",
        tls_mode="self_signed",
        public_host="flow.example.test",
        deployment_profile="compact",
        file_storage="local_fs",
    )
    service.read_active = lambda: bundled  # type: ignore[method-assign]

    with pytest.raises(ServicePreconditionError) as raised:
        service.proxy_fragment()

    assert raised.value.payload["code"] == "ingress_mode_is_not_external_proxy"
    assert "bundled_traefik" in raised.value.payload["message"]


def _application_websocket_paths() -> list[str]:
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[3] / "core"
    pattern = re.compile(r"""\.websocket\(\s*["']([^"']+)["']""")
    return sorted(
        match.group(1)
        for path in root.rglob("*.py")
        for match in pattern.finditer(path.read_text(encoding="utf-8"))
    )


@pytest.mark.parametrize("profile", ("compact", "scalable"))
def test_apache_forwards_every_application_websocket_route_as_a_websocket(
    profile: str,
) -> None:
    """External review P2: mod_proxy_http dropped the Upgrade header on /ws/.

    Measured through httpd 2.4.68 in front of a WebSocket backend: 426 from the backend,
    which had received a plain GET, on both routes; 101 with the ws:// rule. The routes
    are read from the application, so a new one outside the prefix fails here.
    """
    from core.application.lifecycle import proxy_renderer

    paths = _application_websocket_paths()
    assert paths, "non-vacuity: the application declares WebSocket routes"
    assert all(path.startswith(proxy_renderer._WEBSOCKET_PREFIX) for path in paths), paths

    rendered = render_proxy_fragment(
        ProxyRenderConfig(
            kind="apache",
            deployment_profile=profile,  # type: ignore[arg-type]
            public_host="flow.example.test",
            app_backend="flow-app:8099",
            mcp_backend="flow-mcp:8100" if profile == "scalable" else None,
        )
    )

    websocket_rule = 'ProxyPass "/ws/" "ws://flow-app:8099/ws/" nocanon'
    assert websocket_rule in rendered
    # Apache takes the first matching ProxyPass, so the catch-all must come after it.
    assert rendered.index(websocket_rule) < rendered.index('ProxyPass "/" ')
    assert "mod_proxy_wstunnel" in rendered
