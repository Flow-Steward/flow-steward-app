#!/usr/bin/env python3
"""Disposable real-container routing smoke for Nginx and Apache."""

from __future__ import annotations

import argparse
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from uuid import uuid4

from core.application.lifecycle.proxy_renderer import ProxyRenderConfig, render_proxy_fragment

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


def _wait(url: str, *, host: str, expected: str) -> None:
    for _ in range(50):
        try:
            request = urllib.request.Request(url, headers={"Host": host})
            with urllib.request.urlopen(request, timeout=2) as response:
                if response.read().decode() == expected:
                    return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"proxy route did not return {expected}: {url}")


def _proxy_mount(kind: str, fragment: str, root: Path) -> tuple[str, str]:
    if kind == "nginx":
        path = root / "default.conf"
        path.write_text(fragment, encoding="utf-8")
        return "nginx:1.27-alpine", f"{path}:/etc/nginx/conf.d/default.conf:ro"
    default = _run(
        "docker",
        "run",
        "--rm",
        "httpd:2.4-alpine",
        "cat",
        "/usr/local/apache2/conf/httpd.conf",
        capture=True,
    )
    default = default.replace(
        "#LoadModule proxy_module modules/mod_proxy.so",
        "LoadModule proxy_module modules/mod_proxy.so",
    ).replace(
        "#LoadModule proxy_http_module modules/mod_proxy_http.so",
        "LoadModule proxy_http_module modules/mod_proxy_http.so",
    ).replace(
        # The fragment's /ws/ rule is a ws:// proxy; it names this module as required.
        "#LoadModule proxy_wstunnel_module modules/mod_proxy_wstunnel.so",
        "LoadModule proxy_wstunnel_module modules/mod_proxy_wstunnel.so",
    )
    config = root / "httpd.conf"
    config.write_text(default + "\nInclude conf/extra/flow-steward.conf\n", encoding="utf-8")
    vhost = root / "flow-steward.conf"
    vhost.write_text(fragment, encoding="utf-8")
    return (
        "httpd:2.4-alpine",
        f"{config}:/usr/local/apache2/conf/httpd.conf:ro,{vhost}:/usr/local/apache2/conf/extra/flow-steward.conf:ro",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.parse_args()
    suffix = uuid4().hex[:10]
    network = f"fsacc-proxy-{suffix}"
    containers: list[str] = []
    _run("docker", "network", "create", "--label", _OWNER_LABEL, network)
    try:
        for role, body in (("app", "APP"), ("mcp", "MCP")):
            name = f"fsacc-proxy-{role}-{suffix}"
            containers.append(name)
            _run(
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "--network",
                network,
                "--network-alias",
                role,
                "--label",
                _OWNER_LABEL,
                "nginx:1.27-alpine",
                "sh",
                "-c",
                f"printf 'server {{ listen 80; location / {{ return 200 {body}; }} }}' "
                "> /etc/nginx/conf.d/default.conf && exec nginx -g 'daemon off;'",
            )
        with tempfile.TemporaryDirectory(prefix="fsacc-proxy-config-") as directory:
            root = Path(directory)
            for profile in ("compact", "scalable"):
                for kind in ("nginx", "apache"):
                    fragment = render_proxy_fragment(
                        ProxyRenderConfig(
                            kind=kind,  # type: ignore[arg-type]
                            deployment_profile=profile,  # type: ignore[arg-type]
                            public_host="flow.example.test",
                            app_backend="app:80",
                            mcp_backend="mcp:80" if profile == "scalable" else None,
                        )
                    )
                    case_root = root / f"{profile}-{kind}"
                    case_root.mkdir()
                    image, mounts = _proxy_mount(kind, fragment, case_root)
                    name = f"fsacc-proxy-{profile}-{kind}-{suffix}"
                    containers.append(name)
                    command = [
                        "docker",
                        "run",
                        "-d",
                        "--name",
                        name,
                        "--network",
                        network,
                        "--label",
                        _OWNER_LABEL,
                        "-p",
                        "127.0.0.1::80",
                    ]
                    for mount in mounts.split(","):
                        command.extend(("-v", mount))
                    command.append(image)
                    _run(*command)
                    port = _run("docker", "port", name, "80/tcp", capture=True).rsplit(":", 1)[1]
                    base = f"http://127.0.0.1:{port}"
                    _wait(base + "/", host="flow.example.test", expected="APP")
                    _wait(base + "/mcpx", host="flow.example.test", expected="APP")
                    _wait(
                        base + "/mcp",
                        host="flow.example.test",
                        expected="APP" if profile == "compact" else "MCP",
                    )
                    _run("docker", "rm", "-f", name)
                    containers.remove(name)
    finally:
        for name in reversed(containers):
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
    print("6 external proxy profile/kind routing cases passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
