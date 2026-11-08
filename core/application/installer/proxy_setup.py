"""Pure, nonsecret setup artifacts for an operator's existing reverse proxy."""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address

from core.application.lifecycle.proxy_renderer import ProxyRenderConfig, render_proxy_fragment


@dataclass(frozen=True, slots=True)
class ProxySetup:
    """Alternative new-site and existing-TLS configurations, never applied automatically."""

    title: str
    http_config: str
    existing_https_locations: str
    instructions: tuple[str, ...]


def render_proxy_setup(config: ProxyRenderConfig, *, public_port: int = 443) -> ProxySetup:
    """Render using validated, placement-derived backends supplied by the caller.

    Build ``config`` from ``ConfigurationService.proxy_fragment()`` and the active
    deployment profile so Docker aliases and published host ports retain one owner.
    No certificates, privileged commands or filesystem access are performed here.
    """
    if (
        isinstance(public_port, bool)
        or not isinstance(public_port, int)
        or not 1 <= public_port <= 65535
    ):
        raise ValueError("public HTTPS port must be between 1 and 65535")
    if config.kind == "apache":
        return _apache_setup(config, public_port)
    locations = _nginx_locations(config)
    host = f"[{config.public_host}]" if ":" in config.public_host else config.public_host
    ipv6_listener = "  listen [::]:80;\n" if ":" in config.public_host else ""
    return ProxySetup(
        title="Finish your Nginx setup",
        http_config=(
            f"server {{\n  listen 80;\n{ipv6_listener}  server_name {host};\n{locations}}}\n"
        ),
        existing_https_locations=locations,
        instructions=(
            "Choose one: update an existing HTTPS site, or create a new site below.",
            "Existing HTTPS: replace its proxy locations with the supplied locations; "
            f"keep your listen {public_port} and certificate settings.",
            _validation_instructions(public_port),
            "New site: "
            "Back up your proxy configuration, then install the complete HTTP server block "
            "in an enabled Nginx site file.",
            "Run nginx -t, then reload Nginx after the check succeeds.",
            _certificate_instructions(config, public_port),
            "Use the same host or Docker network as the supplied backend addresses. "
            "Open the HTTPS site and check its response before using the administrator setup URL.",
        ),
    )


def _nginx_locations(config: ProxyRenderConfig) -> str:
    mcp = config.app_backend if config.deployment_profile == "compact" else config.mcp_backend
    assert mcp is not None
    routes = (
        ("= /mcp", mcp),
        ("^~ /mcp/", mcp),
        ("^~ /.well-known/oauth-protected-resource", mcp),
        ("/", config.app_backend),
    )
    shared = (
        "    proxy_http_version 1.1;\n"
        "    proxy_set_header Host $host;\n"
        "    proxy_set_header Authorization $http_authorization;\n"
        "    proxy_set_header X-Forwarded-Proto $scheme;\n"
        "    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
        "    proxy_set_header Upgrade $http_upgrade;\n"
        '    proxy_set_header Connection "upgrade";\n'
        "    proxy_buffering off;\n"
        "    proxy_read_timeout 3600s;\n"
    )
    return "".join(
        f"  location {path} {{\n    proxy_pass http://{backend};\n{shared}  }}\n"
        for path, backend in routes
    )


def _apache_setup(config: ProxyRenderConfig, public_port: int) -> ProxySetup:
    # Keep the existing routing owner, including the scalable MCP discovery route
    # and ws:// handling supported by Apache versions predating upgrade=websocket.
    fragment = render_proxy_fragment(config)
    virtual_host, body = fragment.split("<VirtualHost ", 1)[1].split(">\n", 1)
    routes = body.rsplit("</VirtualHost>", 1)[0]
    locations = (
        "  ProxyRequests Off\n"
        '  RequestHeader set X-Forwarded-Proto "expr=%{REQUEST_SCHEME}"\n'
        "  ProxyTimeout 3600\n"
        '  ProxyPass "/.well-known/acme-challenge/" "!"\n'
        f"{routes}"
    )
    return ProxySetup(
        title="Finish your Apache setup",
        http_config=f"<VirtualHost {virtual_host}>\n{locations}</VirtualHost>\n",
        existing_https_locations=locations,
        instructions=(
            "Choose one: update an existing HTTPS virtual host, or create a new site below.",
            "Enable mod_proxy, mod_proxy_http, mod_proxy_wstunnel, mod_headers and mod_ssl. "
            "On Debian/Ubuntu: a2enmod proxy proxy_http proxy_wstunnel headers ssl.",
            "Existing HTTPS: replace its proxy directives with the supplied directives; "
            f"keep your Listen {public_port}, <VirtualHost *:{public_port}> "
            "(or the matching address-based virtual host) and certificate settings.",
            _validation_instructions(public_port),
            "New site: "
            "Back up your proxy configuration, then install the complete HTTP virtual host "
            "in an enabled Apache site file. Keep your existing ACME challenge handler.",
            "Run apachectl configtest, then apachectl graceful after the check succeeds.",
            _certificate_instructions(config, public_port),
            "Use the same host or Docker network as the supplied backend addresses. "
            "Open the HTTPS site and check its response before using the administrator setup URL.",
        ),
    )


def _validation_instructions(public_port: int) -> str:
    return (
        "For direct HTTP-01 certificate validation, point the domain's DNS at this server "
        f"and allow inbound ports 80 and {public_port}. "
        "If an existing CDN or proxy owns public HTTPS, keep its origin firewall policy "
        "and use your existing origin certificate or DNS validation instead."
    )


def _certificate_instructions(config: ProxyRenderConfig, public_port: int) -> str:
    try:
        ip_address(config.public_host)
    except ValueError:
        pass
    else:
        return (
            "IP address: configure a certificate covering this exact IP address and a TLS "
            f"listener on port {public_port}, or use a domain with a matching certificate. "
            "For IPv6 Apache, keep the address-based virtual host; IPv6 ServerName is unsupported."
        )
    if config.kind == "apache" and public_port != 443:
        return (
            "New site only: obtain a certificate using your existing certificate workflow "
            f"or DNS validation, then configure Listen {public_port} and "
            f"<VirtualHost *:{public_port}> with the issued certificate paths. "
            "Verify that the HTTPS listener and redirect retain your chosen port."
        )
    port_option = f" --https-port {public_port}" if public_port != 443 else ""
    name = "Nginx" if config.kind == "nginx" else "Apache"
    return (
        f"New site with direct HTTP-01 only: install Certbot and its {name} plugin, then run "
        f"certbot --{config.kind} -d {config.public_host} --redirect{port_option}. "
        "Certbot obtains the certificate and updates the site for HTTPS."
    )
