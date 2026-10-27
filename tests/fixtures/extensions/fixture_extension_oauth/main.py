"""Offline neutral OAuth fixture entrypoint."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from typing import Any


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _response(payload: dict[str, Any], *, success: bool) -> int:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0 if success else 2


def main() -> int:
    try:
        request = json.loads(sys.stdin.read() or "{}")
    except (TypeError, ValueError):
        request = {}
    action = _mapping(_mapping(request).get("action"))
    action_id = action.get("action_id")
    if _mapping(request).get("mode") != "action" or action_id not in {
        "test_runtime_connection",
        "verify_identity",
    }:
        return _response(
            {"ok": False, "error_code": "invalid_action", "result": {}},
            success=False,
        )
    connection = _mapping(_mapping(action.get("target")).get("connection"))
    secrets = _mapping(connection.get("secrets"))
    access_token = secrets.get("access_token")
    if set(secrets) != {"access_token"} or not isinstance(access_token, str) or not access_token:
        return _response(
            {"ok": False, "error_code": "invalid_runtime_target", "result": {}},
            success=False,
        )
    if "post_connect_failure" in access_token:
        return _response(
            {"ok": False, "error_code": "provider_unavailable", "result": {}},
            success=False,
        )
    if action_id == "test_runtime_connection":
        return _response(
            {"ok": True, "result": {"runtime_revalidated": True}},
            success=True,
        )
    return _response(
        {
            "ok": True,
            "result": {
                "subject": "neutral-subject",
                "verified_display_name": "Neutral Verified Account",
                "verified_email": "verified@example.test",
                "safe_metadata": {"fixture": "neutral"},
                "health": "ok",
            },
        },
        success=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
