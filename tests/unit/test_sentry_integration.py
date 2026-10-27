from __future__ import annotations

import importlib
import sys
from types import ModuleType


def _reload_module():
    import core.infrastructure.observability.sentry as sentry_mod

    return importlib.reload(sentry_mod)


def test_init_sentry_no_dsn_returns_false(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.delenv("SENTRY_KEY", raising=False)

    sentry_mod = _reload_module()
    assert sentry_mod.init_sentry(config={}, component="test") is False


def test_init_sentry_does_not_use_sentry_key_alias(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.setenv("SENTRY_KEY", "https://public@example.ingest.sentry.io/1")

    sentry_mod = _reload_module()
    assert sentry_mod.init_sentry(config={}, component="test") is False


def test_init_sentry_with_dsn_initializes(monkeypatch):
    calls: dict = {}

    class LoggingIntegration:
        def __init__(self, level, event_level):
            calls["logging_level"] = level
            calls["logging_event_level"] = event_level

    class FastApiIntegration:
        pass

    class StarletteIntegration:
        pass

    fake_sentry_sdk = ModuleType("sentry_sdk")

    def fake_init(**kwargs):
        calls["init_kwargs"] = kwargs

    def fake_set_tag(key, value):
        calls.setdefault("tags", []).append((key, value))

    fake_sentry_sdk.init = fake_init
    fake_sentry_sdk.set_tag = fake_set_tag

    fake_logging_mod = ModuleType("sentry_sdk.integrations.logging")
    fake_logging_mod.LoggingIntegration = LoggingIntegration

    fake_fastapi_mod = ModuleType("sentry_sdk.integrations.fastapi")
    fake_fastapi_mod.FastApiIntegration = FastApiIntegration

    fake_starlette_mod = ModuleType("sentry_sdk.integrations.starlette")
    fake_starlette_mod.StarletteIntegration = StarletteIntegration

    monkeypatch.setenv("SENTRY_DSN", "https://public@example.ingest.sentry.io/1")
    monkeypatch.setenv("SENTRY_TRACES_SAMPLE_RATE", "0.0")
    monkeypatch.setenv("SENTRY_PROFILES_SAMPLE_RATE", "0.0")

    monkeypatch.setitem(sys.modules, "sentry_sdk", fake_sentry_sdk)
    monkeypatch.setitem(sys.modules, "sentry_sdk.integrations.logging", fake_logging_mod)
    monkeypatch.setitem(sys.modules, "sentry_sdk.integrations.fastapi", fake_fastapi_mod)
    monkeypatch.setitem(sys.modules, "sentry_sdk.integrations.starlette", fake_starlette_mod)

    sentry_mod = _reload_module()
    assert sentry_mod.init_sentry(config={}, component="serve", framework="fastapi") is True

    assert calls["init_kwargs"]["dsn"] == "https://public@example.ingest.sentry.io/1"
    assert calls["init_kwargs"]["auto_enabling_integrations"] is False
    assert ("component", "serve") in calls["tags"]


def test_init_sentry_missing_sdk_parts_is_non_fatal(monkeypatch):
    monkeypatch.setenv("SENTRY_DSN", "https://public@example.ingest.sentry.io/1")

    fake_sentry_sdk = ModuleType("sentry_sdk")
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake_sentry_sdk)
    monkeypatch.delitem(sys.modules, "sentry_sdk.integrations.logging", raising=False)

    sentry_mod = _reload_module()
    assert sentry_mod.init_sentry(config={}, component="test") is False
