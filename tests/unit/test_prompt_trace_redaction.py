from __future__ import annotations

import pytest

try:
    from core.infrastructure.db.platform_logger import _redact_payload
except Exception as _e:  # pragma: no cover - optional dependency environments
    _redact_payload = None
    _IMPORT_ERROR = _e
else:
    _IMPORT_ERROR = None


def test_redact_payload_masks_prompt_sensitive_keys():
    if _redact_payload is None:
        pytest.skip(f"Flow Steward logger import unavailable: {_IMPORT_ERROR}")

    payload = {
        "db_dsn": "postgres://admin:secret@db/app",
        "project_connections": [{"connection_id": 1, "dsn": "postgres://secret"}],
        "project_providers": [{"provider_key": "x", "token": "abc"}],
        "watch": {"enabled": True},
        "config": {"raw": True},
        "safe_value": "ok",
    }
    redacted = _redact_payload(payload)
    assert redacted["db_dsn"] == "[REDACTED]"
    assert redacted["project_connections"] == "[REDACTED]"
    assert redacted["project_providers"] == "[REDACTED]"
    assert redacted["watch"] == "[REDACTED]"
    assert redacted["config"] == "[REDACTED]"
    assert redacted["safe_value"] == "ok"
