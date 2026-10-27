from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

_PROJECT_CONFIG_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|token|secret|password|passphrase|private[_-]?key|dsn)",
    re.IGNORECASE,
)


def _as_utc_datetime(value: Any, fallback: datetime | None = None) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value or "").strip()
    if text:
        try:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            parsed = datetime.fromisoformat(text)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except Exception:
            pass
    if fallback is not None:
        return fallback if fallback.tzinfo else fallback.replace(tzinfo=UTC)
    return None


def _redact_snapshot_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            key_text = str(key or "")
            if _PROJECT_CONFIG_SECRET_KEY_RE.search(key_text):
                redacted[key] = "[REDACTED]"
                continue
            redacted[key] = _redact_snapshot_secrets(item)
        return redacted
    if isinstance(value, list):
        return [_redact_snapshot_secrets(item) for item in value]
    return value


def _build_project_config_snapshot(project_config: dict | None) -> dict:
    if not isinstance(project_config, dict):
        return {}
    try:
        serialized = json.dumps(project_config, default=str)
        parsed = json.loads(serialized)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    redacted = _redact_snapshot_secrets(parsed)
    return redacted if isinstance(redacted, dict) else {}


def _json_safe(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, default=str))
    except Exception:
        return value
