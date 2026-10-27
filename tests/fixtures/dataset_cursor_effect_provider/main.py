from __future__ import annotations

import json
import os
import sys


def main() -> int:
    payload = json.loads(sys.stdin.read() or "{}")
    action = payload.get("action") if isinstance(payload, dict) else {}
    action = action if isinstance(action, dict) else {}
    inputs = action.get("input") if isinstance(action.get("input"), dict) else {}
    rows = inputs.get("page") if isinstance(inputs.get("page"), list) else []
    marker_path = str(inputs.get("marker_path") or "").strip()
    if not marker_path:
        print(json.dumps({"ok": False, "error": "marker_path_missing"}))
        return 0
    descriptor = os.open(marker_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as marker:
        marker.write(json.dumps({"accepted_count": len(rows)}, sort_keys=True) + "\n")
    print(json.dumps({"ok": True, "result": {"accepted_count": len(rows)}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
