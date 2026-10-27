"""Clone must wait for its restarted HTTP listener without masking parity failures."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from core.application.clone.planning import PreparedCloneRestore
from core.application.lifecycle.errors import LifecycleError
from core.infrastructure.clone import installation_restore_effects
from core.infrastructure.clone.installation_restore_effects import InstalledCloneRestoreEffects


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds > 0
        self.now += seconds


class _Services:
    def __init__(self) -> None:
        self.running = False

    def start(self, names: tuple[str, ...]) -> None:
        assert names == ("fs-app",)
        self.running = True

    def service_states(self, names: tuple[str, ...]) -> dict[str, bool]:
        return dict.fromkeys(names, self.running)


def _effects(
    tmp_path: Path,
    verify: Callable[[PreparedCloneRestore], None],
    services: _Services,
    *,
    readiness: bool = True,
) -> InstalledCloneRestoreEffects:
    return InstalledCloneRestoreEffects(
        operation_root=tmp_path,
        services=services,
        service_names=("fs-app",),
        volumes=None,
        target_roots={},
        backup=None,
        import_secrets=lambda _value: None,
        regenerate_configuration=lambda _value: None,
        activate_release=lambda _value: None,
        verify_database=lambda _value: True,
        secrets_match=lambda _value: True,
        configuration_matches=lambda _value: True,
        release_activated=lambda _value: True,
        verify_target=(lambda _prepared: None) if readiness else verify,
        readiness_probe=(lambda prepared, _remaining: verify(prepared)) if readiness else None,
    )


def _clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    clock = _Clock()
    # Patch only the time dependency; both phase dispatch and verification stay real.
    monkeypatch.setattr(installation_restore_effects, "monotonic", clock.monotonic)
    monkeypatch.setattr(installation_restore_effects, "sleep", clock.sleep)
    return clock


@pytest.mark.parametrize("phase", ("handler", "postcondition"))
def test_clone_restart_waits_until_listener_is_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    clock = _clock(monkeypatch)
    services = _Services()
    prepared = cast(PreparedCloneRestore, object())
    verified = False

    def verify(_prepared: PreparedCloneRestore) -> None:
        nonlocal verified
        assert services.running
        if clock.now < 7:
            raise ConnectionRefusedError(111, "listener still starting")
        verified = True

    effects = _effects(tmp_path, verify, services)
    effects.handlers()["start_maintenance"](prepared)
    assert effects.postconditions()["start_maintenance"](prepared)
    if phase == "handler":
        effects.handlers()["verify_restored_target"](prepared)
    else:
        assert effects.postconditions()["verify_restored_target"](prepared)

    assert verified
    assert clock.now == 7


@pytest.mark.parametrize("error_type", (ConnectionRefusedError, ConnectionResetError, TimeoutError))
def test_clone_unavailable_listener_stops_at_deadline_with_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_type: type[OSError]
) -> None:
    clock = _clock(monkeypatch)
    error = error_type("listener unavailable")

    def verify(_prepared: PreparedCloneRestore) -> None:
        raise error

    effects = _effects(tmp_path, verify, _Services())
    verify_restored_target = effects.handlers()["verify_restored_target"]
    prepared = cast(PreparedCloneRestore, object())
    with pytest.raises(error_type) as caught:
        verify_restored_target(prepared)

    assert caught.value is error
    assert clock.now == 120


@pytest.mark.parametrize(
    "error",
    (
        RuntimeError("Clone restore database parity verification failed"),
        ValueError("invalid readiness response"),
        TimeoutError("database parity timed out"),
        LifecycleError.stable_failure(
            code="clone_manual_maintenance_required",
            cause="Manual maintenance is no longer enabled",
            remediation="Restore manual maintenance before retrying",
            step="verify_restored_target",
        ),
    ),
)
def test_clone_permanent_verification_failure_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    clock = _clock(monkeypatch)

    def verify(_prepared: PreparedCloneRestore) -> None:
        raise error

    effects = _effects(tmp_path, verify, _Services(), readiness=False)
    verify_restored_target = effects.handlers()["verify_restored_target"]
    prepared = cast(PreparedCloneRestore, object())
    with pytest.raises(type(error)) as caught:
        verify_restored_target(prepared)

    assert caught.value is error
    assert clock.now == 0


def test_clone_rejects_success_after_slow_probe_exhausts_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _clock(monkeypatch)

    def verify(_prepared: PreparedCloneRestore) -> None:
        clock.now += 121

    effects = _effects(tmp_path, verify, _Services())
    verify_restored_target = effects.handlers()["verify_restored_target"]
    prepared = cast(PreparedCloneRestore, object())
    with pytest.raises(TimeoutError, match="deadline"):
        verify_restored_target(prepared)


def test_clone_slow_failed_probe_stops_with_its_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _clock(monkeypatch)
    refusal = ConnectionRefusedError("listener unavailable")
    calls = 0

    def verify(_prepared: PreparedCloneRestore) -> None:
        nonlocal calls
        calls += 1
        clock.now += 121
        raise refusal

    effects = _effects(tmp_path, verify, _Services())
    verify_restored_target = effects.handlers()["verify_restored_target"]
    prepared = cast(PreparedCloneRestore, object())
    with pytest.raises(ConnectionRefusedError) as caught:
        verify_restored_target(prepared)

    assert caught.value is refusal
    assert calls == 1
    assert clock.now == 121


def test_clone_does_not_probe_again_after_final_sleep_exhausts_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _clock(monkeypatch)
    refusal = ConnectionRefusedError(111, "listener still starting")

    def verify(_prepared: PreparedCloneRestore) -> None:
        if clock.now == 0:
            clock.now = 119.5
            raise refusal
        raise AssertionError("No new probe may start once its deadline is exhausted")

    effects = _effects(tmp_path, verify, _Services())
    verify_restored_target = effects.handlers()["verify_restored_target"]
    prepared = cast(PreparedCloneRestore, object())
    with pytest.raises(ConnectionRefusedError) as caught:
        verify_restored_target(prepared)

    assert caught.value is refusal
    assert clock.now == 120
