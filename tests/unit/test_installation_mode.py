"""DEV-HOOK-01: only agreeing canonical installation markers permit Development."""

from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    ("environment", "stack_environment", "expected"),
    [
        ("development", None, "development"),
        (None, "development", "development"),
        ("local_development", None, "development"),
        (None, "local_development", "development"),
        ("local_development", "development", "development"),
        (" DEVELOPMENT ", " Local_Development ", "development"),
        (None, None, "production"),
        ("production", None, "production"),
        (None, "production", "production"),
        ("production", "production", "production"),
        ("development", "production", "production"),
        ("production", "development", "production"),
        ("development", "staging", "production"),
        ("staging", "development", "production"),
        ("dev", None, "production"),
        (None, "dev", "production"),
        ("", None, "production"),
        ("development", "", "production"),
        (" ", "development", "production"),
    ],
)
def test_installation_mode_requires_explicit_agreeing_markers(
    monkeypatch, environment: str | None, stack_environment: str | None, expected: str
) -> None:
    from core.infrastructure.config.installation_mode import installation_mode

    for marker, value in (
        ("ENVIRONMENT", environment),
        ("FS_STACK_ENVIRONMENT", stack_environment),
    ):
        monkeypatch.delenv(marker, raising=False)
        if value is not None:
            monkeypatch.setenv(marker, value)

    assert installation_mode() == expected


def test_installation_mode_ignores_unregistered_and_client_build_hints(monkeypatch) -> None:
    from core.infrastructure.config.installation_mode import installation_mode

    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("FS_STACK_ENVIRONMENT", raising=False)
    for marker in ("FS_RUNTIME_ENVIRONMENT", "APP_ENV", "NODE_ENV", "SENTRY_ENVIRONMENT"):
        monkeypatch.setenv(marker, "development")
    monkeypatch.setenv("HOSTNAME", "localhost")
    monkeypatch.setenv("FS_EDITION", "development")

    assert installation_mode() == "production"


def test_installation_mode_rechecks_server_markers_after_a_mode_switch(monkeypatch) -> None:
    from core.infrastructure.config.installation_mode import installation_mode

    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("FS_STACK_ENVIRONMENT", "development")
    assert installation_mode() == "development"

    monkeypatch.setenv("FS_STACK_ENVIRONMENT", "production")
    assert installation_mode() == "production"
