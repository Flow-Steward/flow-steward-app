from __future__ import annotations

import importlib

import pytest


@pytest.fixture(autouse=True)
def _restore_security_config():
    """Undo the reloads below once each test ends.

    A reload rebinds the module's globals; without this the last test in the file
    decided the scan mode (and PII salt) for every later test in the process,
    which differs per xdist worker.
    """
    import core.infrastructure.security.config as security_config

    saved = dict(vars(security_config))
    yield
    vars(security_config).update(saved)


def _reload_config(
    monkeypatch, *, environment: str, salt: str | None, scan_mode: str | None = None
):
    monkeypatch.setenv("ENVIRONMENT", environment)
    monkeypatch.delenv("FS_STACK_ENVIRONMENT", raising=False)
    if salt is None:
        monkeypatch.delenv("SECURITY_PII_SALT", raising=False)
    else:
        monkeypatch.setenv("SECURITY_PII_SALT", salt)
    if scan_mode is None:
        monkeypatch.delenv("SECURITY_SCAN_MODE", raising=False)
    else:
        monkeypatch.setenv("SECURITY_SCAN_MODE", scan_mode)

    import core.infrastructure.security.config as security_config

    return importlib.reload(security_config)


def test_security_config_requires_unique_pii_salt_in_production(monkeypatch) -> None:
    import core.infrastructure.security.config as security_config

    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("FS_STACK_ENVIRONMENT", raising=False)
    monkeypatch.delenv("SECURITY_PII_SALT", raising=False)

    with pytest.raises(RuntimeError, match="SECURITY_PII_SALT"):
        importlib.reload(security_config)

    _reload_config(monkeypatch, environment="local_development", salt=None)


def test_security_config_defaults_to_enforcement_in_production(monkeypatch) -> None:
    config = _reload_config(
        monkeypatch,
        environment="production",
        salt="prod-pii-salt-32-plus-characters",
    )

    assert config.SECURITY_SCAN_MODE == "enforce_standard"
    assert config.enforcement_active() is True


def test_security_config_local_fallback_does_not_use_public_pii_salt(monkeypatch) -> None:
    config = _reload_config(monkeypatch, environment="local_development", salt=None)

    assert config.SECURITY_PII_SALT
    assert config.SECURITY_PII_SALT != "default-pii-salt-change-me"
