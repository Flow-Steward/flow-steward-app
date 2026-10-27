"""Compact can wait for provider-linked dependencies before touching its volume."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.entrypoints.cli import compact_runtime


def test_dependency_wait_requires_both_protocols_within_one_budget() -> None:
    now = 0.0
    attempts: list[tuple[str, float]] = []

    def sleep(seconds: float) -> None:
        nonlocal now
        now += seconds

    def postgres_ready(*_args: object) -> bool:
        attempts.append(("postgres", now))
        return now >= 2.0

    def rabbitmq_ready(*_args: object) -> bool:
        attempts.append(("rabbitmq", now))
        return now >= 4.0

    compact_runtime._wait_for_bootstrap_dependencies(
        postgres=("postgres.internal", 5432),
        rabbitmq=compact_runtime.RabbitMQProbeTarget(("rabbit.internal", 5672)),
        postgres_user="flow_steward",
        timeout_seconds=10,
        postgres_ready=postgres_ready,
        rabbitmq_ready=rabbitmq_ready,
        clock=lambda: now,
        sleep=sleep,
    )

    assert now == 4.0
    assert {name for name, _attempted_at in attempts} == {"postgres", "rabbitmq"}
    assert max(attempted_at for _name, attempted_at in attempts) <= 4.0


def test_dependency_wait_names_every_unavailable_service_at_its_deadline() -> None:
    now = 0.0

    def sleep(seconds: float) -> None:
        nonlocal now
        now += seconds

    rabbitmq = compact_runtime.RabbitMQProbeTarget(("rabbit.internal", 5672))
    with pytest.raises(RuntimeError, match=r"PostgreSQL.*RabbitMQ"):
        compact_runtime._wait_for_bootstrap_dependencies(
            postgres=("postgres.internal", 5432),
            rabbitmq=rabbitmq,
            postgres_user="flow_steward",
            timeout_seconds=3,
            postgres_ready=lambda *_args: False,
            rabbitmq_ready=lambda *_args: False,
            clock=lambda: now,
            sleep=sleep,
        )

    assert now == 3.0


def test_opt_in_wait_uses_canonical_urls_before_volume_lock_and_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    observed: dict[str, object] = {}
    monkeypatch.setenv("FS_DEPLOYMENT_PROFILE", "compact")
    monkeypatch.setenv("FS_COMPACT_WAIT_FOR_DEPENDENCIES", "1")
    monkeypatch.setenv("FS_COMPACT_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv(
        "FS_DB_DSN",
        # pragma: allowlist nextline secret -- synthetic credential fixture
        "postgresql://db_user:secret@canonical-postgres.internal:5544/flow_steward",
    )
    monkeypatch.setenv(
        "RABBITMQ_URL",
        # pragma: allowlist nextline secret -- synthetic credential fixture
        "amqps://broker_user:secret@canonical-rabbit.internal:5671/flow",
    )
    monkeypatch.setenv("POSTGRES_HOST", "wrong-postgres.internal")
    monkeypatch.setenv("POSTGRES_PORT", "1111")
    monkeypatch.setenv("RABBITMQ_HOST", "wrong-rabbit.internal")
    monkeypatch.setenv("RABBITMQ_PORT", "2222")
    monkeypatch.setattr(compact_runtime, "verify_compact_environment_identity", lambda _env: None)
    monkeypatch.setattr(compact_runtime, "_HELD_VOLUME_LOCKS", [])

    def wait(**kwargs: object) -> None:
        observed.update(kwargs)
        events.append("wait")

    monkeypatch.setattr(compact_runtime, "_wait_for_bootstrap_dependencies", wait)
    monkeypatch.setattr(
        compact_runtime,
        "acquire_compact_volume_lock",
        lambda _root: events.append("lock") or object(),
    )

    def stop_before_role_start(_root: Path) -> None:
        events.append("bootstrap")
        raise RuntimeError("test stop")

    monkeypatch.setattr(compact_runtime, "_bootstrap", stop_before_role_start)

    assert compact_runtime.main() == 78
    assert events == ["wait", "lock", "bootstrap"]
    assert observed["postgres"] == ("canonical-postgres.internal", 5544)
    assert observed["postgres_user"] == "db_user"
    rabbitmq_target = observed["rabbitmq"]
    assert isinstance(rabbitmq_target, compact_runtime.RabbitMQProbeTarget)
    assert rabbitmq_target.address == ("canonical-rabbit.internal", 5671)
    assert rabbitmq_target.ssl_context is not None
    assert rabbitmq_target.server_hostname == "canonical-rabbit.internal"
    assert observed["timeout_seconds"] == 300


@pytest.mark.parametrize("backend", ["disk_typo", "local-fs", "s33"])
@pytest.mark.parametrize("wait", ["0", "1"])
def test_invalid_artifact_backend_fails_before_startup_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    backend: str,
    wait: str,
) -> None:
    events: list[str] = []
    monkeypatch.setenv("FS_DEPLOYMENT_PROFILE", "compact")
    monkeypatch.setenv("ARTIFACT_STORAGE_BACKEND", backend)
    monkeypatch.setenv("FS_COMPACT_WAIT_FOR_DEPENDENCIES", wait)
    monkeypatch.setenv("FS_COMPACT_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("FS_DB_DSN", "postgresql://operator@localhost:5432/doc04")
    monkeypatch.setenv("RABBITMQ_URL", "amqp://operator@localhost:5672/doc04")
    monkeypatch.setattr(compact_runtime, "verify_compact_environment_identity", lambda _env: None)
    monkeypatch.setattr(compact_runtime, "_HELD_VOLUME_LOCKS", [])

    def unexpected_effect(name: str):
        def effect(*_args, **_kwargs):
            events.append(name)
            raise RuntimeError("unexpected startup effect")

        return effect

    monkeypatch.setattr(
        compact_runtime, "_wait_for_bootstrap_dependencies", unexpected_effect("wait")
    )
    monkeypatch.setattr(compact_runtime, "acquire_compact_volume_lock", unexpected_effect("lock"))
    monkeypatch.setattr(compact_runtime, "_bootstrap", unexpected_effect("bootstrap"))

    assert compact_runtime.main() == 78
    assert "ARTIFACT_STORAGE_BACKEND" in capsys.readouterr().err
    assert events == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("backend", ["local_fs", "s3", "memory", " LOCAL_FS ", " S3 ", " MEMORY "])
def test_supported_artifact_backend_preserves_startup_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
) -> None:
    events: list[str] = []
    monkeypatch.setenv("FS_DEPLOYMENT_PROFILE", "compact")
    monkeypatch.setenv("ARTIFACT_STORAGE_BACKEND", backend)
    monkeypatch.setenv("FS_COMPACT_WAIT_FOR_DEPENDENCIES", "1")
    monkeypatch.setenv("FS_COMPACT_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("FS_DB_DSN", "postgresql://operator@localhost:5432/doc04")
    monkeypatch.setenv("FS_RABBITMQ_TRANSPORT_MODE", "loopback-local")
    monkeypatch.setenv("RABBITMQ_URL", "amqp://127.0.0.1:5672/doc04")
    monkeypatch.setattr(compact_runtime, "verify_compact_environment_identity", lambda _env: None)
    monkeypatch.setattr(compact_runtime, "_HELD_VOLUME_LOCKS", [])
    monkeypatch.setattr(
        compact_runtime, "_wait_for_bootstrap_dependencies", lambda **_kw: events.append("wait")
    )
    monkeypatch.setattr(
        compact_runtime,
        "acquire_compact_volume_lock",
        lambda _root: events.append("lock") or object(),
    )

    def stop(_root: Path):
        events.append("bootstrap")
        raise RuntimeError("stop before role creation")

    monkeypatch.setattr(compact_runtime, "_bootstrap", stop)
    assert compact_runtime.main() == 78
    assert events == ["wait", "lock", "bootstrap"]
