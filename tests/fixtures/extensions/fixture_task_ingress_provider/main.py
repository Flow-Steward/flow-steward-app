from __future__ import annotations

import json
import sys
from typing import Any


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _action_input(request: dict[str, Any]) -> dict[str, Any]:
    action = _as_dict(request.get("action"))
    payload = _as_dict(action.get("input"))
    nested = _as_dict(payload.get("payload"))
    return nested or payload


def _task_id(payload: dict[str, Any]) -> str:
    task = _as_dict(payload.get("task"))
    for value in (
        payload.get("task_id"),
        payload.get("id"),
        task.get("task_id"),
        task.get("id"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return "fixture-task"


def _ok(result: dict[str, Any]) -> int:
    print(json.dumps({"ok": True, "result": result}, sort_keys=True))
    return 0


def main() -> int:
    raw = sys.stdin.read().strip()
    request = json.loads(raw) if raw else {}
    mode = str(request.get("mode") or "").strip().lower()

    if mode == "query":
        query_id = str(_as_dict(request.get("query")).get("query_id") or "").strip()
        if query_id == "provider_health":
            return _ok({"status": "healthy", "configured": True})
        print(json.dumps({"ok": False, "error": f"unknown_query:{query_id}"}))
        return 2

    if mode == "action":
        action_id = str(_as_dict(request.get("action")).get("action_id") or "").strip()
        payload = _action_input(request)
        if action_id == "normalize_task_action":
            task_id = _task_id(payload)
            return _ok(
                {
                    "normalized": {
                        "task_id": task_id,
                        "source_id": task_id,
                        "title": str(payload.get("title") or "Fixture task"),
                    }
                }
            )
        if action_id == "fetch_attachments":
            return _ok({"attachments": list(payload.get("attachments") or [])})
        if action_id == "sync_completion_status":
            return _ok({"status": str(payload.get("final_status") or "completed")})
        print(json.dumps({"ok": False, "error": f"unknown_action:{action_id}"}))
        return 2

    print(json.dumps({"ok": False, "error": f"unknown_mode:{mode}"}))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
