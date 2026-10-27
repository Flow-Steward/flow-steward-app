from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.infrastructure.installer import host_diagnostics


def _outcome(*, returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess((), returncode, stdout=stdout, stderr="")


@pytest.mark.parametrize(
    ("system", "machine", "status"),
    [
        ("Darwin", "arm64", "pass"),
        ("Linux", "x86_64", "pass"),
        ("Windows", "AMD64", "fail"),
        ("Linux", "riscv64", "fail"),
    ],
)
def test_platform_diagnostic_closes_supported_os_arch_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    machine: str,
    status: str,
) -> None:
    monkeypatch.setattr(host_diagnostics.platform, "system", lambda: system)
    monkeypatch.setattr(host_diagnostics.platform, "machine", lambda: machine)

    finding = host_diagnostics.check_platform(tmp_path)

    assert finding["id"] == "host_platform"
    assert finding["status"] == status
    assert system in finding["detail"]
    assert machine in finding["detail"]
    assert finding["remediation"]


def test_platform_diagnostic_returns_complete_failure_when_probe_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        host_diagnostics.platform,
        "system",
        lambda: (_ for _ in ()).throw(OSError("platform unavailable")),
    )

    finding = host_diagnostics.check_platform(tmp_path)

    assert finding["id"] == "host_platform"
    assert finding["status"] == "fail"
    assert finding["detail"]
    assert finding["remediation"]


def test_docker_cli_diagnostic_reports_missing_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(host_diagnostics.shutil, "which", lambda _command: None)

    finding = host_diagnostics.check_docker_cli(tmp_path)

    assert finding["id"] == "docker_cli"
    assert finding["status"] == "fail"
    assert "not found" in finding["detail"]
    assert "Docker Desktop or Docker Engine" in finding["remediation"]


def test_docker_cli_diagnostic_reports_client_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(host_diagnostics.shutil, "which", lambda _command: "/usr/bin/docker")
    monkeypatch.setattr(
        host_diagnostics,
        "_run_docker",
        lambda *_args: _outcome(stdout="28.5.2\n"),
    )

    finding = host_diagnostics.check_docker_cli(tmp_path)

    assert finding["status"] == "pass"
    assert "28.5.2" in finding["detail"]


def test_daemon_diagnostic_contains_actionable_failure_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        host_diagnostics,
        "_run_docker",
        lambda *_args: (_ for _ in ()).throw(subprocess.TimeoutExpired("docker", 5)),
    )

    finding = host_diagnostics.check_docker_daemon(tmp_path)

    assert finding["id"] == "docker_daemon"
    assert finding["status"] == "fail"
    assert "not reachable" in finding["detail"]
    assert "Start Docker" in finding["remediation"]


def test_context_diagnostic_reports_remote_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        host_diagnostics,
        "_run_docker",
        lambda *_args: _outcome(stdout="ssh://builder.example\n"),
    )

    finding = host_diagnostics.check_docker_context(tmp_path)

    assert finding["id"] == "docker_context"
    assert finding["status"] == "fail"
    assert "unsupported" in finding["detail"]
    assert "local Docker context" in finding["remediation"]


def test_context_diagnostic_does_not_render_remote_endpoint_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "must-never-be-rendered"
    monkeypatch.setattr(
        host_diagnostics,
        "_run_docker",
        lambda *_args: _outcome(stdout=f"ssh://operator:{secret}@builder.example\n"),
    )

    finding = host_diagnostics.check_docker_context(tmp_path)

    assert secret not in json.dumps(finding)


def test_context_diagnostic_verifies_effective_socket_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket_path = tmp_path / "docker.sock"
    socket_path.touch()
    monkeypatch.setattr(
        host_diagnostics,
        "_run_docker",
        lambda *_args: _outcome(stdout=f"unix://{socket_path}\n"),
    )

    finding = host_diagnostics.check_docker_context(tmp_path)

    assert finding["status"] == "pass"
    assert str(socket_path) in finding["detail"]


@pytest.mark.parametrize(
    ("version", "status"),
    [("v2.39.2", "pass"), ("5.2.0", "pass"), ("1.29.2", "fail")],
)
def test_compose_diagnostic_requires_plugin_major_two_or_newer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: str,
    status: str,
) -> None:
    monkeypatch.setattr(
        host_diagnostics,
        "_run_docker",
        lambda *_args: _outcome(stdout=f"{version}\n"),
    )

    finding = host_diagnostics.check_docker_compose(tmp_path)

    assert finding["id"] == "docker_compose"
    assert finding["status"] == status
    assert version in finding["detail"]
    assert finding["remediation"]


def test_docker_config_diagnostic_flags_helper_without_exposing_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker_config = tmp_path / "docker-config"
    docker_config.mkdir()
    secret = "must-never-be-rendered"
    (docker_config / "config.json").write_text(
        json.dumps({"credsStore": "desktop", "auths": {"registry": {"auth": secret}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(host_diagnostics, "_docker_config_root", lambda: docker_config)

    finding = host_diagnostics.check_docker_config(tmp_path)

    rendered = json.dumps(finding)
    assert finding["id"] == "docker_config"
    assert finding["status"] == "warn"
    assert "credential helper" in finding["detail"].lower()
    assert "isolated Docker configuration" in finding["remediation"]
    assert secret not in rendered


def test_docker_config_diagnostic_fails_closed_for_invalid_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker_config = tmp_path / "invalid-docker-config"
    docker_config.mkdir()
    (docker_config / "config.json").write_text("{invalid", encoding="utf-8")
    monkeypatch.setattr(host_diagnostics, "_docker_config_root", lambda: docker_config)

    finding = host_diagnostics.check_docker_config(tmp_path)

    assert finding["status"] == "fail"
    assert "could not be read safely" in finding["detail"]
    assert finding["remediation"]


def test_repository_diagnostic_reports_read_only_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(host_diagnostics.os, "access", lambda *_args: False)

    finding = host_diagnostics.check_repository_access(tmp_path)

    assert finding["id"] == "repository_access"
    assert finding["status"] == "fail"
    assert str(tmp_path) in finding["detail"]
    assert "write access" in finding["remediation"]


@pytest.mark.parametrize(
    "checker",
    [host_diagnostics.check_repository_access, host_diagnostics.check_free_disk],
)
def test_filesystem_diagnostic_contains_path_resolution_failure(checker) -> None:
    class BrokenRoot:
        def __fspath__(self) -> str:
            raise OSError("path unavailable")

    finding = checker(BrokenRoot())

    assert finding["status"] == "fail"
    assert finding["detail"]
    assert finding["remediation"]


def test_disk_diagnostic_reports_required_free_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        host_diagnostics.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=1024**3),
    )

    finding = host_diagnostics.check_free_disk(tmp_path)

    assert finding["id"] == "free_disk"
    assert finding["status"] == "fail"
    assert "4 GiB" in finding["remediation"]


def test_default_port_diagnostic_names_each_busy_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checked: list[tuple[int, object]] = []

    def port_is_available(port: int, *, socket_type) -> bool:
        checked.append((port, socket_type))
        return port not in {5673, 55432}

    monkeypatch.setattr(
        host_diagnostics,
        "_port_is_available",
        port_is_available,
    )

    finding = host_diagnostics.check_default_ports(tmp_path)

    assert finding["id"] == "default_ports"
    assert finding["status"] == "fail"
    assert "5673/tcp" in finding["detail"]
    assert "55432/tcp" in finding["detail"]
    assert "8080/tcp" not in finding["detail"]
    assert "free the listed ports" in finding["remediation"]
    assert checked == [
        (8080, host_diagnostics.socket.SOCK_STREAM),
        (8099, host_diagnostics.socket.SOCK_STREAM),
        (8099, host_diagnostics.socket.SOCK_DGRAM),
        (5673, host_diagnostics.socket.SOCK_STREAM),
        (15673, host_diagnostics.socket.SOCK_STREAM),
        (1025, host_diagnostics.socket.SOCK_STREAM),
        (8025, host_diagnostics.socket.SOCK_STREAM),
        (55432, host_diagnostics.socket.SOCK_STREAM),
        (15173, host_diagnostics.socket.SOCK_STREAM),
        (8100, host_diagnostics.socket.SOCK_STREAM),
        (9000, host_diagnostics.socket.SOCK_STREAM),
        (17474, host_diagnostics.socket.SOCK_STREAM),
        (17687, host_diagnostics.socket.SOCK_STREAM),
        (18333, host_diagnostics.socket.SOCK_STREAM),
        (19333, host_diagnostics.socket.SOCK_STREAM),
    ]


def test_collector_preserves_independent_findings_when_root_resolution_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenRoot:
        def __fspath__(self) -> str:
            raise OSError("path unavailable")

    monkeypatch.setattr(
        host_diagnostics,
        "HOST_DIAGNOSTIC_CHECKERS",
        (host_diagnostics.check_repository_access, host_diagnostics.check_free_disk),
    )

    findings = host_diagnostics.collect_host_diagnostics(BrokenRoot())

    assert [finding["id"] for finding in findings] == ["repository_access", "free_disk"]
    assert all(finding["status"] == "fail" for finding in findings)
    assert all(
        set(finding) == {"id", "category", "title", "status", "detail", "remediation"}
        for finding in findings
    )


def test_collector_returns_one_complete_finding_per_bounded_checker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def checker(check_id: str):
        return lambda _repo_root: {
            "id": check_id,
            "category": "core",
            "title": check_id,
            "status": "pass",
            "detail": "checked",
            "remediation": "No action required.",
        }

    monkeypatch.setattr(
        host_diagnostics,
        "HOST_DIAGNOSTIC_CHECKERS",
        (checker("first"), checker("second")),
    )

    findings = host_diagnostics.collect_host_diagnostics(tmp_path)

    assert [finding["id"] for finding in findings] == ["first", "second"]
    assert all(
        set(finding) == {"id", "category", "title", "status", "detail", "remediation"}
        for finding in findings
    )
