"""The installer names a Docker daemon that cannot resolve host-gateway.

Found by the auditor on the owner's server after a daemon.json change without a
restart: `docker run --add-host host.docker.internal:host-gateway` wrote

    invalid IP	host.docker.internal

into the container's /etc/hosts. The installer then failed on name resolution and said
this "usually means the installer was started another way" -- it had been started the
right way. The mapping is there to read, so the failure now says what it shows.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from core.application.installer.errors import InstallerFailure
from core.infrastructure.installer import gateway as gateway_module
from core.infrastructure.installer import gateway_host_preflight


def _hosts(tmp_path: Path, mapping_line: str | None) -> Path:
    lines = ["127.0.0.1\tlocalhost", "::1\tlocalhost ip6-localhost"]
    if mapping_line is not None:
        lines.append(mapping_line)
    path = tmp_path / "hosts"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _unresolvable(*_args, **_kwargs):
    raise socket.gaierror(-2, "Name does not resolve")


def _probe_failure(monkeypatch: pytest.MonkeyPatch, hosts: Path) -> InstallerFailure:
    monkeypatch.setattr(gateway_host_preflight, "_HOSTS_FILE", hosts, raising=False)
    monkeypatch.setattr(gateway_module.socket.socket, "connect", _unresolvable)
    with pytest.raises(InstallerFailure) as raised:
        gateway_module.DockerInstallerGateway._host_route_has_listener(8595)
    return raised.value


def test_an_invalid_host_gateway_mapping_is_named_as_the_daemons(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    failure = _probe_failure(monkeypatch, _hosts(tmp_path, "invalid IP\thost.docker.internal"))

    assert failure.code == "host_port_route_probe_failed"
    assert "started another way" not in failure.cause
    assert "invalid IP" in failure.cause
    assert "host-gateway" in failure.cause
    assert "dockerd" in failure.remediation


def test_a_missing_mapping_still_points_at_how_the_installer_was_started(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    failure = _probe_failure(monkeypatch, _hosts(tmp_path, None))

    assert "started another way" in failure.cause


def test_a_valid_mapping_that_still_fails_is_not_blamed_on_the_daemon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    failure = _probe_failure(monkeypatch, _hosts(tmp_path, "172.17.0.1\thost.docker.internal"))

    assert "invalid" not in failure.cause.lower()
    assert "dockerd" not in failure.remediation
