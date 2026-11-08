"""Show proxy setup before the first-login link, without changing the host proxy."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import click

from core.application.installer.contracts import InstallRunResult
from core.application.installer.proxy_setup import ProxySetup, render_proxy_setup
from core.application.lifecycle.configuration_service import ConfigurationService
from core.application.lifecycle.proxy_renderer import ProxyRenderConfig
from core.infrastructure.config.installer_config import installer_state_root
from core.infrastructure.lifecycle.configuration_store import LifecycleConfigurationStore


def proxy_setup_for(root: Path) -> ProxySetup | None:
    service = ConfigurationService(
        store=LifecycleConfigurationStore(root, state_root=installer_state_root(root))
    )
    active = service.read_active(required=False)
    if active is None or active.ingress_mode != "external_proxy":
        return None
    fragment = service.proxy_fragment()
    return render_proxy_setup(
        ProxyRenderConfig(
            kind=fragment.proxy_kind,
            deployment_profile=active.deployment_profile,
            public_host=fragment.public_host,
            app_backend=fragment.app_backend,
            mcp_backend=fragment.mcp_backend or None,
        ),
        public_port=active.public_port,
    )


def announce_proxy_setup(setup: ProxySetup | None) -> None:
    if setup is None:
        return
    click.echo(f"\nNEXT STEP: {setup.title}")
    click.echo(
        "The application is running locally. Configure your web server before opening the public URL."
    )
    click.echo("\nNEW WEBSITE — complete HTTP configuration:")
    click.echo(setup.http_config)
    for instruction in setup.instructions:
        click.echo(instruction)
    click.echo(
        "\nEXISTING HTTPS WEBSITE — keep its certificate and replace the proxy directives with:"
    )
    click.echo(setup.existing_https_locations)
    click.echo("\nCurrent backend addresses: ./flow-steward proxy-fragment --json")


def public_installation_result(result: InstallRunResult, setup: ProxySetup | None) -> dict:
    """Keep proxy help out of the closed private cross-version handoff envelope."""
    public = result.model_dump(mode="json")
    if setup is not None:
        public["proxy_setup"] = asdict(setup)
    return public


def prepare_completion(
    result: InstallRunResult, root: Path
) -> tuple[InstallRunResult, ProxySetup | None]:
    """An optional help artifact cannot suppress a committed install or its login link."""
    try:
        return result, proxy_setup_for(root)
    except Exception as exc:
        warning = (
            f"Proxy setup instructions could not be generated ({type(exc).__name__}). "
            "The installation result is preserved. Read the saved backend settings with "
            "./flow-steward proxy-fragment --json."
        )
        return result.model_copy(update={"warnings": (*result.warnings, warning)}), None
