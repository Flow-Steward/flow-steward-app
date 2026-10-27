"""The real Clone composition must not retry database parity as listener startup."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.application.installer import InstallConfig
from core.application.lifecycle.contracts import ComposeTarget
from core.application.maintenance.contracts import MaintenanceStatusResult
from core.application.release.contracts import ReleaseIdentity, ReleaseImageIdentity
from core.application.release.images import required_product_roles
from core.entrypoints.cli import clone_composition, lifecycle_runtime
from core.entrypoints.cli.runtime import CLIRuntime
from core.infrastructure.clone import installation_restore_effects


def _service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = InstallConfig(environment="production", deployment_profile="scalable")
    target = ComposeTarget(
        stack_environment="production",
        file_storage="local_fs",
        deployment_profile="scalable",
        env_file=".env.prod",
        compose_files=("compose.yaml",),
        required_services=("pg-fs", "fs-cli", "fs-app"),
        runtime_services=("pg-fs", "fs-cli", "fs-app"),
        project_name="clone-target",
    )
    digest = "sha256:" + "a" * 64
    release = ReleaseIdentity(
        version="1.2.3",
        tag="v1.2.3",
        commit_sha="b" * 40,
        registry="ghcr.io/example",
        profile="scalable",
        platform="linux/amd64",
        images=tuple(
            ReleaseImageIdentity(
                role=role.role,
                registry_ref=f"ghcr.io/example/{role.repository}@{digest}",
                index_digest=digest,
                manifest_digest=digest,
                config_digest=digest,
                revision="b" * 40,
                platform="linux/amd64",
            )
            for role in required_product_roles("scalable")
        ),
    )
    environment = {
        "FS_DB_DSN": "postgresql://fixture",
        "FS_DOCKER_HOST_PROJECT_ROOT": str(tmp_path),
    }
    monkeypatch.setattr(lifecycle_runtime, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(lifecycle_runtime, "_installed_runtime_environment", lambda: environment)
    (tmp_path / ".flow-steward").mkdir(mode=0o700)
    runtime = CLIRuntime()
    runtime.__dict__.update(
        configuration_service=SimpleNamespace(
            read_active=lambda: config,
            derive_target=lambda _config: target,
        ),
        maintenance_service=SimpleNamespace(
            status=lambda: MaintenanceStatusResult(status="enabled", owner_kind="manual")
        ),
        backup_service=MagicMock(),
        docker_lifecycle_gateway=MagicMock(),
        preservation_service=MagicMock(),
        release_service=SimpleNamespace(
            state=SimpleNamespace(load_installed_release=lambda: release),
            selector=SimpleNamespace(),
            require_official_installation=lambda: release,
        ),
    )
    return runtime.clone_service, release


def test_real_clone_composition_parity_timeout_is_one_shot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elapsed = [0.0]
    monkeypatch.setattr(installation_restore_effects, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(
        installation_restore_effects,
        "sleep",
        lambda duration: elapsed.__setitem__(0, elapsed[0] + duration),
    )

    class Response:
        status = 200

        def read(self, _limit: int) -> bytes:
            return json.dumps({"ready": True, "checks": {"postgresql": {"ok": True}}}).encode()

    class Connection:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            assert (host, port) == ("fs-app", 8099)
            assert timeout > 0

        def request(self, method: str, route: str) -> None:
            assert (method, route) == ("GET", "/health/ready")

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            pass

    monkeypatch.setattr(lifecycle_runtime, "HTTPConnection", Connection)
    parity_error = TimeoutError("database parity timed out")

    def parity(**_kwargs) -> bool:
        elapsed[0] += 2
        raise parity_error

    monkeypatch.setattr(clone_composition, "_verify_clone_database_parity", parity)
    service, release = _service(tmp_path, monkeypatch)
    prepared = SimpleNamespace(staged_root=str(tmp_path), exact_release=release)

    with pytest.raises(TimeoutError) as caught:
        service._restore_runtime._phase_handlers["verify_restored_target"](prepared)

    assert caught.value is parity_error
    assert elapsed[0] == 2


def test_real_clone_composition_passes_remaining_budget_to_http_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elapsed = [0.0]
    timeouts: list[float] = []
    monkeypatch.setattr(installation_restore_effects, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(
        installation_restore_effects,
        "sleep",
        lambda duration: elapsed.__setitem__(0, elapsed[0] + duration),
    )

    class Response:
        status = 200

        def read(self, _limit: int) -> bytes:
            return b'{"ready":true,"checks":{"postgresql":{"ok":true}}}'

    class Connection:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            assert (host, port) == ("fs-app", 8099)
            timeouts.append(timeout)

        def request(self, method: str, route: str) -> None:
            assert (method, route) == ("GET", "/health/ready")
            if len(timeouts) == 1:
                elapsed[0] = 119
                raise ConnectionRefusedError("listener starting")

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            pass

    monkeypatch.setattr(lifecycle_runtime, "HTTPConnection", Connection)
    parity = MagicMock(return_value=True)
    monkeypatch.setattr(clone_composition, "_verify_clone_database_parity", parity)
    service, release = _service(tmp_path, monkeypatch)
    prepared = SimpleNamespace(staged_root=str(tmp_path), exact_release=release)

    service._restore_runtime._phase_handlers["verify_restored_target"](prepared)

    # The final five-second probe is the unchanged, one-shot full readiness check.
    assert timeouts == [5, 0.5, 5]
    assert elapsed[0] == 119.5
    parity.assert_called_once()


@pytest.mark.parametrize(
    "payload",
    (
        {"ready": True},
        {"ready": True, "checks": {"postgresql": {"ok": False}}},
        {"ready": True, "checks": {"postgresql": "invalid"}},
    ),
)
def test_real_clone_composition_rejects_invalid_or_failed_canonical_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: dict
) -> None:
    requests: list[str] = []

    class Response:
        status = 200

        def read(self, _limit: int) -> bytes:
            return json.dumps(payload).encode()

    class Connection:
        def __init__(self, _host: str, _port: int, timeout: float) -> None:
            assert timeout == 5

        def request(self, method: str, route: str) -> None:
            assert method == "GET"
            requests.append(route)

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            pass

    monkeypatch.setattr(lifecycle_runtime, "HTTPConnection", Connection)
    parity = MagicMock(return_value=True)
    monkeypatch.setattr(clone_composition, "_verify_clone_database_parity", parity)
    service, release = _service(tmp_path, monkeypatch)
    prepared = SimpleNamespace(staged_root=str(tmp_path), exact_release=release)

    with pytest.raises(RuntimeError, match="readiness verification failed"):
        service._restore_runtime._phase_handlers["verify_restored_target"](prepared)

    assert requests == ["/health/ready"]
    parity.assert_not_called()
