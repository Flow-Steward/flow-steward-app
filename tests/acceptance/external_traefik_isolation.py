#!/usr/bin/env python3
"""Disposable shared-Traefik isolation smoke for two Flow Steward installs."""

from __future__ import annotations

import subprocess
import time
import urllib.request
from uuid import uuid4

_OWNER_LABEL = "com.flow-steward.test-owner=compact-deployment-20260917"


def _run(*command: str, capture: bool = False) -> str:
    result = subprocess.run(
        command,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
    )
    return result.stdout.strip() if capture else ""


def _wait(url: str, host: str, expected: str) -> None:
    for _ in range(80):
        try:
            request = urllib.request.Request(url, headers={"Host": host})
            with urllib.request.urlopen(request, timeout=2) as response:
                if response.read().decode() == expected:
                    return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"shared Traefik route {host}{url} did not return {expected}")


def _backend(*, name: str, network: str, host: str, route: str, priority: int, body: str) -> None:
    service = name
    _run(
        "docker",
        "run",
        "-d",
        "--name",
        name,
        "--network",
        network,
        "--label",
        _OWNER_LABEL,
        "--label",
        "traefik.enable=true",
        "--label",
        f"traefik.http.routers.{name}.rule=Host(`{host}`){route}",
        "--label",
        f"traefik.http.routers.{name}.entrypoints=web",
        "--label",
        f"traefik.http.routers.{name}.priority={priority}",
        "--label",
        f"traefik.http.routers.{name}.service={service}",
        "--label",
        f"traefik.http.services.{service}.loadbalancer.server.port=80",
        "nginx:1.27-alpine",
        "sh",
        "-c",
        f"printf 'server {{ listen 80; location / {{ return 200 {body}; }} }}' "
        "> /etc/nginx/conf.d/default.conf && exec nginx -g 'daemon off;'",
    )


def main() -> int:
    suffix = uuid4().hex[:10]
    network = f"fsacc-traefik-{suffix}"
    names: list[str] = []
    _run("docker", "network", "create", "--label", _OWNER_LABEL, network)
    try:
        proxy = f"fsacc-traefik-proxy-{suffix}"
        names.append(proxy)
        _run(
            "docker",
            "run",
            "-d",
            "--name",
            proxy,
            "--network",
            network,
            "--label",
            _OWNER_LABEL,
            "-p",
            "127.0.0.1::80",
            "-v",
            "/var/run/docker.sock:/var/run/docker.sock:ro",
            "traefik:v3.5",
            "--providers.docker=true",
            "--providers.docker.exposedbydefault=false",
            f"--providers.docker.network={network}",
            "--entrypoints.web.address=:80",
        )
        for install, host in (("one", "one.example.test"), ("two", "two.example.test")):
            app = f"fsacc-{install}-app-{suffix}"
            mcp = f"fsacc-{install}-mcp-{suffix}"
            names.extend((app, mcp))
            _backend(
                name=app,
                network=network,
                host=host,
                route="",
                priority=10,
                body=f"{install.upper()}_APP",
            )
            _backend(
                name=mcp,
                network=network,
                host=host,
                route=" && (Path(`/mcp`) || PathPrefix(`/mcp/`))",
                priority=100,
                body=f"{install.upper()}_MCP",
            )
        control = f"fsacc-control-site-{suffix}"
        names.append(control)
        _backend(
            name=control,
            network=network,
            host="control.example.test",
            route="",
            priority=10,
            body="CONTROL",
        )
        port = _run("docker", "port", proxy, "80/tcp", capture=True).rsplit(":", 1)[1]
        base = f"http://127.0.0.1:{port}"
        try:
            for install in ("one", "two"):
                host = f"{install}.example.test"
                _wait(base + "/", host, f"{install.upper()}_APP")
                _wait(base + "/mcpx", host, f"{install.upper()}_APP")
                _wait(base + "/mcp", host, f"{install.upper()}_MCP")
            _wait(base + "/", "control.example.test", "CONTROL")
        except RuntimeError as exc:
            logs = _run("docker", "logs", proxy, capture=True)
            raise RuntimeError(f"{exc}\nTraefik logs:\n{logs}") from exc
    finally:
        for name in reversed(names):
            subprocess.run(
                ["docker", "rm", "-f", name],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        subprocess.run(
            ["docker", "network", "rm", network],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    print("two-install shared Traefik isolation and control-site smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
