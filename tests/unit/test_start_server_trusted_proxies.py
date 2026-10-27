"""External review P2: Scalable ignored the proxies the operator said to trust.

`config` records FS_PROXY_TRUSTED_IPS, and Compact hands it to its router. On
Scalable nothing delivered it to fs-app, and start_server.sh built uvicorn's
--forwarded-allow-ips from loopback plus whatever the name `traefik` resolved to. So
behind an external Nginx, Apache or a shared Traefik, the application discarded the
proxy's X-Forwarded-For and X-Forwarded-Proto: clients looked like the proxy, and
HTTPS requests looked like plain HTTP.

These tests run the real script with a stand-in for uvicorn that prints its
arguments, and read the Compose files for the delivery.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "core/entrypoints/cli/scripts/start_server.sh"


def _allowed(tmp_path: Path, trusted: str | None) -> tuple[int, str, str]:
    app_root = tmp_path / "app"
    manifest = app_root / "assets/frontend/dist/.vite/manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "uvicorn"
    fake.write_text('#!/bin/sh\nfor a in "$@"; do echo "$a"; done\n', encoding="utf-8")
    fake.chmod(0o755)
    environment = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "APP_ROOT": str(app_root),
        "PYTHON": sys.executable,
        "FS_SERVE_SSL": "0",
    }
    if trusted is not None:
        environment["FS_PROXY_TRUSTED_IPS"] = trusted
    outcome = subprocess.run(
        ("bash", str(SCRIPT)),
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    flag = next(
        (line for line in outcome.stdout.splitlines() if line.startswith("--forwarded-allow-ips=")),
        "",
    )
    return outcome.returncode, flag.removeprefix("--forwarded-allow-ips="), outcome.stderr


def test_the_configured_proxies_are_trusted(tmp_path: Path) -> None:
    code, allowed, stderr = _allowed(tmp_path, "172.18.0.0/16,10.8.0.7")

    assert code == 0, stderr
    entries = set(allowed.split(","))
    assert {"172.18.0.0/16", "10.8.0.7/32"} <= entries
    assert {"127.0.0.1", "::1"} <= entries  # the loopback it always trusted


def test_without_configuration_the_old_trust_is_unchanged(tmp_path: Path) -> None:
    code, allowed, stderr = _allowed(tmp_path, None)

    assert code == 0, stderr
    assert set(allowed.split(",")) == {"127.0.0.1", "::1"}


@pytest.mark.parametrize("bad", ["0.0.0.0/0", "::/0", "not-an-address", "10.0.0.0/8,*"])
def test_a_wildcard_or_malformed_entry_refuses_to_start(tmp_path: Path, bad: str) -> None:
    """The same rule the configuration applies: trusting everyone is never a default."""
    code, allowed, stderr = _allowed(tmp_path, bad)

    assert code != 0
    assert allowed == ""
    assert "FS_PROXY_TRUSTED_IPS" in stderr


@pytest.mark.parametrize("compose_file", ["docker-compose.yaml", "docker-compose.prod.yaml"])
def test_scalable_delivers_the_setting_to_the_application(compose_file: str) -> None:
    services = yaml.safe_load((ROOT / "infra/docker" / compose_file).read_text(encoding="utf-8"))[
        "services"
    ]

    assert "FS_PROXY_TRUSTED_IPS" in services["fs-app"]["environment"]
