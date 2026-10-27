from __future__ import annotations

import json
import sys


def main() -> int:
    request = json.load(sys.stdin)
    action = request.get("action") or {}
    input_payload = action.get("input") or {}
    dataset = input_payload.get("dataset") or {}
    query_template = input_payload.get("query_template") or {}
    limits = input_payload.get("limits") or {}

    if (
        action.get("action_id") != "execute_template"
        or dataset.get("resource_scope_jsonb")
        != {
            "version": 1,
            "resources": [{"resource_id": "records", "columns": ["record_id"]}],
        }
        or query_template.get("query_definition_jsonb")
        != {"sql": "SELECT record_id FROM records LIMIT 10"}
        or limits.get("max_runtime_rows") != 10
    ):
        json.dump(
            {
                "ok": False,
                "result": {},
                "commands": [],
                "events": [],
                "reload": [],
                "errors": [
                    {
                        "code": "invalid_fixture_contract",
                        "message": "Projected dataset host payload did not match the fixture contract",
                    }
                ],
            },
            sys.stdout,
        )
        return 2

    json.dump(
        {
            "ok": True,
            "result": {"rows": [{"record_id": 101}], "row_count": 1},
            "commands": [],
            "events": [],
            "reload": [],
            "errors": [],
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
