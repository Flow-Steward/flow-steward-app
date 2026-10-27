#!/usr/bin/env python3
# ruff: noqa: E402
# mypy: ignore-errors
"""Generate a canonical task-ingress extension scaffold."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

import yaml

EXTENSION_INVOKE_VALUE = "extension:invoke"
HEALTH_PY_FILENAME = "health.py"
HTTPS_JSON_SCHEMA_ORG_DRAFT_2020_12_SCHEMA_VALUE = "https://json-schema.org/draft/2020-12/schema"
MAIN_PY_FILENAME = "main.py"
SCHEMA_VALUE = "$schema"


PROJECT_ROOT = Path(__file__).resolve().parents[4]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.application.extensions.identity import extension_folder_name, normalize_extension_id
from core.application.extensions.remote_runtime_protocol_v2 import HOST_CONTRACT_VERSION
from core.application.extensions.scaffold import DEFAULT_EXTENSION_ICON_PATH, SCAFFOLD_ICON_FILENAME
from core.infrastructure.db.ingress_runtime import (
    TASK_INGRESS_REQUIRED_ACTION_ERROR_CODES,
    TASK_INGRESS_REQUIRED_QUERY_ERROR_CODES,
)
from core.infrastructure.extension_runtime.extension_runtime import (
    TASK_INGRESS_REQUIRED_ACTION_RESULT_FIELDS,
    TASK_INGRESS_REQUIRED_QUERY_RESULT_FIELDS,
)

DEFAULT_SUPPORTED_EVENTS: list[str] = [
    "external.task.created",
    "external.task.updated",
    "external.comment.received",
]
DEFAULT_REQUIRED_SCOPES: list[str] = [
    "event:subscribe",
    EXTENSION_INVOKE_VALUE,
    "command:enqueue_followup_job",
]


def _slug_display_name(extension_id: str) -> str:
    words = [token for token in extension_id.replace("-", "_").split("_") if token]
    if not words:
        return extension_id
    return " ".join(token.capitalize() for token in words)


def _normalize_extension_id(extension_id: str) -> str:
    return normalize_extension_id(extension_id)


def _manifest_payload(
    *, extension_id: str, display_name: str, description: str, vendor_name: str, category: str
) -> dict[str, Any]:
    return {
        "manifest_version": 2,
        "lifecycle": {"data_schema_version": 1, "data_migrations": []},
        "extension_id": extension_id,
        "kind": "task_provider",
        "version": "1.0.0",
        "runtime": {"contract_version": HOST_CONTRACT_VERSION},
        "display_name": display_name,
        "description": description,
        "vendor": {"name": vendor_name},
        "listing": {"category": category, "tags": [], "summary": description},
        "features": ["task_ingress", "task_sync", "task_normalization", "action"],
        "event_subscriptions": [
            {"event_type": event_type, "severity": "any", "conditions": {}}
            for event_type in DEFAULT_SUPPORTED_EVENTS
        ],
        "required_scopes": list(DEFAULT_REQUIRED_SCOPES),
        "schemas": {
            "config": "schemas/config.schema.yaml",
            "secrets": "schemas/secrets.schema.yaml",  # pragma: allowlist secret
            "subscriptions": "schemas/subscriptions.schema.yaml",
        },
        "entrypoint": {"mode": "subprocess", "command": ["python3", MAIN_PY_FILENAME]},
        "health": {"command": ["python3", HEALTH_PY_FILENAME], "timeout_seconds": 5},
        "ui_manifest": "ui/ui_manifest.yaml",
        "query_manifest": "ui/queries/queries.yaml",
        "action_manifest": "ui/actions/actions.yaml",
        "python_requirements": [],
    }


def _action_manifest_payload() -> dict[str, Any]:
    return {
        "actions": [
            {
                "action_id": "normalize_task_action",
                "title": "Normalize task payload",
                "description": "Normalize provider payload into canonical task ingress envelope.",
                "required_scopes": [EXTENSION_INVOKE_VALUE],
                "handler": {"mode": "extension"},
                "result_fields": list(
                    TASK_INGRESS_REQUIRED_ACTION_RESULT_FIELDS.get("normalize_task_action") or ()
                ),
                "error_codes": list(
                    TASK_INGRESS_REQUIRED_ACTION_ERROR_CODES.get("normalize_task_action") or ()
                ),
                "command_type": "enqueue_followup_job",
                "mutates_platform": True,
            },
            {
                "action_id": "fetch_attachments",
                "title": "Fetch task attachments",
                "description": "Return task attachments in canonical result.attachments shape.",
                "required_scopes": [EXTENSION_INVOKE_VALUE],
                "handler": {"mode": "extension"},
                "result_fields": list(
                    TASK_INGRESS_REQUIRED_ACTION_RESULT_FIELDS.get("fetch_attachments") or ()
                ),
                "error_codes": list(
                    TASK_INGRESS_REQUIRED_ACTION_ERROR_CODES.get("fetch_attachments") or ()
                ),
                "mutates_platform": False,
            },
            {
                "action_id": "sync_completion_status",
                "title": "Sync completion status",
                "description": "Update remote task completion state from Flow Steward status.",
                "required_scopes": [EXTENSION_INVOKE_VALUE],
                "handler": {"mode": "extension"},
                "result_fields": list(
                    TASK_INGRESS_REQUIRED_ACTION_RESULT_FIELDS.get("sync_completion_status") or ()
                ),
                "error_codes": list(
                    TASK_INGRESS_REQUIRED_ACTION_ERROR_CODES.get("sync_completion_status") or ()
                ),
                "mutates_platform": False,
            },
        ]
    }


def _query_manifest_payload() -> dict[str, Any]:
    return {
        "queries": [
            {
                "query_id": "provider_health",
                "title": "Provider health",
                "description": "Returns provider health and configuration status.",
                "required_scopes": ["read:projects"],
                "result_fields": list(
                    TASK_INGRESS_REQUIRED_QUERY_RESULT_FIELDS.get("provider_health") or ()
                ),
                "error_codes": list(
                    TASK_INGRESS_REQUIRED_QUERY_ERROR_CODES.get("provider_health") or ()
                ),
                "handler": {"mode": "extension"},
            },
            {
                "query_id": "list_remote_tasks",
                "title": "List remote tasks",
                "description": "Returns provider tasks for diagnostics and manual replays.",
                "required_scopes": ["read:projects"],
                "result_fields": ["result.items", "result.summary"],
                "error_codes": [
                    "not_configured",
                    "authentication_failed",
                    "invalid_payload",
                    "rate_limited",
                    "provider_unavailable",
                    "timeout",
                    "upstream_error",
                    "internal_error",
                ],
                "handler": {"mode": "extension"},
            },
        ]
    }


def _ui_manifest_payload() -> dict[str, Any]:
    return {
        "version": "1",
        "pages": [
            {
                "ref": "pages/overview.yaml",
            }
        ],
    }


def _overview_component_payload(*, extension_id: str) -> dict[str, Any]:
    return {
        "component_id": "overview_markdown",
        "type": "markdown",
        "title": "Task ingress overview",
        "content": (
            f"`{extension_id}` scaffold is ready. "
            "Implement provider-specific logic in task_ingress/normalize.py and task_ingress/http.py."
        ),
    }


def _overview_page_payload() -> dict[str, Any]:
    return {
        "page_id": "overview",
        "title": "Overview",
        "components": [{"ref": "../components/overview.yaml"}],
    }


def _config_schema_payload() -> dict[str, Any]:
    return {
        SCHEMA_VALUE: HTTPS_JSON_SCHEMA_ORG_DRAFT_2020_12_SCHEMA_VALUE,
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "workspace_id": {"type": "string", "minLength": 1},
            "project_id": {"type": "string", "minLength": 1},
            "default_priority": {"type": "string", "default": "medium"},
        },
    }


def _secrets_schema_payload() -> dict[str, Any]:
    return {
        SCHEMA_VALUE: HTTPS_JSON_SCHEMA_ORG_DRAFT_2020_12_SCHEMA_VALUE,
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "api_token": {"type": "string", "minLength": 1},
            "webhook_secret": {"type": "string", "minLength": 1},
        },
        "required": ["api_token"],
    }


def _subscriptions_schema_payload() -> dict[str, Any]:
    return {
        SCHEMA_VALUE: HTTPS_JSON_SCHEMA_ORG_DRAFT_2020_12_SCHEMA_VALUE,
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "webhook_events": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "default": ["task.created", "task.updated"],
            }
        },
    }


def _main_py(extension_id: str) -> str:
    return f"""#!/usr/bin/env python3
\"\"\"{extension_id} task-ingress extension entrypoint.\"\"\"
from __future__ import annotations

import json
import sys

from task_ingress.envelope import fail
from task_ingress.router import handle_request


def main() -> int:
    raw = sys.stdin.read().strip()
    try:
        request_payload = json.loads(raw) if raw else {{}}
    except Exception:
        print(json.dumps(fail("invalid_payload", message="Request payload must be valid JSON")))
        return 2

    if not isinstance(request_payload, dict):
        print(json.dumps(fail("invalid_payload", message="Request payload must be an object")))
        return 2

    response, code = handle_request(request_payload)
    print(json.dumps(response))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
"""


def _health_py(extension_id: str) -> str:
    module_name = extension_id.replace("-", "_")
    return f"""#!/usr/bin/env python3
\"\"\"{extension_id} extension health check.\"\"\"
from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from pathlib import Path


def _module_loadable(path: Path, module_name: str) -> tuple[bool, str]:
    if not path.exists():
        return False, f"{{path.name}} not found"
    try:
        spec = importlib.util.spec_from_file_location(module_name, str(path))
        if spec is None or spec.loader is None:
            return False, f"{{path.name}} is not loadable"
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as exc:
        return False, str(exc)[:200]
    return True, ""


def main() -> int:
    root = Path(__file__).parent
    checks: list[dict[str, str]] = []

    main_ok, main_detail = _module_loadable(root / "main.py", "{module_name}_main")
    checks.append(
        {{
            "name": "entrypoint",
            "status": "healthy" if main_ok else "failed",
            "detail": main_detail or "main.py is importable",
        }}
    )

    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        importlib.import_module("task_ingress.router")
        router_ok, router_detail = True, ""
    except Exception as exc:
        router_ok, router_detail = False, str(exc)[:200]
    checks.append(
        {{
            "name": "router",
            "status": "healthy" if router_ok else "failed",
            "detail": router_detail or "task_ingress/router.py is importable",
        }}
    )

    all_healthy = all(item["status"] == "healthy" for item in checks)
    payload = {{
        "ok": all_healthy,
        "status": "healthy" if all_healthy else "failed",
        "provider": "{extension_id}",
        "checks": checks,
    }}
    print(json.dumps(payload))
    return 0 if all_healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
"""


def _readme_md(extension_id: str, display_name: str, description: str) -> str:
    return f"""# {display_name}

{description}

This extension was generated by the Flow Steward extension scaffold.

## Authoring Format

Extension authoring contract files are YAML-only. Keep these files in YAML:

- `extension.yaml`
- `schemas/*.schema.yaml`
- `ui/ui_manifest.yaml`
- `ui/actions/actions.yaml`
- `ui/queries/queries.yaml`
- `ui/pages/*.yaml`
- `ui/components/*.yaml`

Do not add `extension.json`, `manifest.json`, JSON UI manifests, JSON action/query
manifests, JSON component files, or `schemas/*.schema.json`.

Python code may still use JSON for subprocess request/response envelopes, and
the Flow Steward CLI may still emit JSON with `--json`.

## Validate

```bash
flow-steward extensions validate {extension_id}
```

## Smoke Check

```bash
python scripts/smoke_check.py
```
"""


def _smoke_check_py(extension_id: str) -> str:
    return f"""#!/usr/bin/env python3
\"\"\"Smoke check for the generated {extension_id} extension.\"\"\"
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run(args: list[str], payload: dict | None = None) -> dict:
    proc = subprocess.run(
        [sys.executable, *args],
        cwd=str(ROOT),
        input=json.dumps(payload) if payload is not None else None,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{{args}} failed: {{proc.stderr or proc.stdout}}")
    output = proc.stdout.strip()
    return json.loads(output) if output else {{}}


def main() -> int:
    health = _run(["health.py"])
    if health.get("status") != "healthy":
        raise RuntimeError(f"health failed: {{health}}")

    response = _run(
        ["main.py"],
        {{
            "mode": "query",
            "query": {{
                "query_id": "provider_health",
                "params": {{"workspace_id": "local-smoke"}},
            }},
        }},
    )
    if not response.get("ok"):
        raise RuntimeError(f"provider_health query failed: {{response}}")

    print(json.dumps({{"ok": True, "extension_id": "{extension_id}"}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""


def _pytest_py(extension_id: str) -> str:
    return f"""from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


BUNDLE_ROOT = Path(__file__).resolve().parents[1]


def _invoke(payload: dict) -> dict:
    proc = subprocess.run(
        [sys.executable, str(BUNDLE_ROOT / "main.py")],
        cwd=str(BUNDLE_ROOT),
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout)


def test_health_check_passes() -> None:
    proc = subprocess.run(
        [sys.executable, str(BUNDLE_ROOT / "health.py")],
        cwd=str(BUNDLE_ROOT),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["status"] == "healthy"
    assert payload["provider"] == "{extension_id}"


def test_provider_health_query_passes() -> None:
    payload = _invoke(
        {{
            "mode": "query",
            "query": {{
                "query_id": "provider_health",
                "params": {{"workspace_id": "pytest"}},
            }},
        }}
    )
    assert payload["ok"] is True
    assert payload["result"]["provider"] == "{extension_id}"
"""


def _task_ingress_init_py() -> str:
    return """\"\"\"Task ingress package for generated extension.\"\"\"\n"""


def _task_ingress_common_py() -> str:
    return """\"\"\"Common coercion helpers for task-ingress handlers.\"\"\"
from __future__ import annotations

from typing import Any


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def as_str(value: Any) -> str:
    return str(value or "").strip()
"""


def _task_ingress_envelope_py() -> str:
    return """\"\"\"Canonical response envelope helpers (v1.1).\"\"\"
from __future__ import annotations

from typing import Any

from .common import as_dict, as_list, as_str


def _dict_list(value: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in as_list(value):
        payload = as_dict(item)
        if payload:
            rows.append(payload)
    return rows


def ok(
    *,
    result: dict[str, Any] | None = None,
    commands: list[dict[str, Any]] | None = None,
    events: list[dict[str, Any]] | None = None,
    reload: list[str] | None = None,
    errors: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "ok": True,
        "result": as_dict(result),
        "commands": _dict_list(commands),
        "events": _dict_list(events),
        "reload": [as_str(item) for item in as_list(reload) if as_str(item)],
        "errors": _dict_list(errors),
    }
    response.update(extra)
    return response


def fail(
    code: str,
    *,
    message: str = "",
    errors: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    error_code = as_str(code) or "internal_error"
    error_message = as_str(message) or error_code
    normalized_errors = _dict_list(errors)
    normalized_errors = [
        {"code": error_code, "message": error_message},
        *normalized_errors[1:],
    ]

    response: dict[str, Any] = {
        "ok": False,
        "result": {},
        "commands": [],
        "events": [],
        "reload": [],
    }
    response.update(extra)
    response.update(
        {
            "ok": False,
            "error_code": error_code,
            "error": error_message,
            "errors": normalized_errors,
        }
    )
    return response
"""


def _task_ingress_normalize_py(extension_id: str) -> str:
    return f"""\"\"\"Provider payload normalization helpers.\"\"\"
from __future__ import annotations

from typing import Any

from .common import as_dict, as_str


def normalize_task(input_payload: dict[str, Any]) -> dict[str, Any]:
    payload = as_dict(input_payload.get("payload")) or as_dict(input_payload)
    task = as_dict(payload.get("task")) or payload
    task_id = as_str(task.get("task_id") or task.get("id") or payload.get("task_id"))
    title = as_str(task.get("title") or task.get("name") or "Untitled task")
    description = as_str(task.get("description") or title)
    task_type = as_str(task.get("task_type") or payload.get("task_type")).lower()
    priority = as_str(task.get("priority") or payload.get("priority") or "medium").lower()
    return {{
        "source": as_str(payload.get("source") or "{extension_id}"),
        "source_id": task_id,
        "task_id": task_id,
        "task_type": task_type,
        "title": title,
        "description": description,
        "priority": priority,
        "project_id": as_str(payload.get("project_id")),
        "metadata": as_dict(payload.get("metadata")),
        "trigger_config": as_dict(payload.get("trigger_config")),
        "attachments": payload.get("attachments") if isinstance(payload.get("attachments"), list) else [],
    }}
"""


def _task_ingress_http_py() -> str:
    return """\"\"\"Shared HTTP helpers for provider API calls.\"\"\"
from __future__ import annotations

import json
from typing import Any
from urllib import error, request


def http_json_request(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout_seconds: int = 15,
) -> dict[str, Any]:
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        url,
        data=body,
        method=method.upper(),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            **(headers or {}),
        },
    )
    try:
        with request.urlopen(req, timeout=max(1, int(timeout_seconds))) as response:
            raw = response.read().decode("utf-8")
            data = json.loads(raw) if raw else {}
            return data if isinstance(data, dict) else {"value": data}
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"http_error:{exc.code}:{detail[:200]}") from exc
"""


def _task_ingress_actions_py() -> str:
    return """\"\"\"Action handlers for generated task-ingress extension.\"\"\"
from __future__ import annotations

from typing import Any

from .common import as_dict, as_list, as_str
from .envelope import fail, ok
from .normalize import normalize_task


def _normalize_task_action(action_payload: dict[str, Any]) -> dict[str, Any]:
    input_payload = as_dict(action_payload.get("input"))
    normalized = normalize_task(input_payload)
    if not as_str(normalized.get("task_id")):
        return fail("invalid_payload", message="Task payload is missing task_id")
    command = {
        "command_type": "enqueue_followup_job",
        "target": {"project_id": normalized.get("project_id")},
        "payload": {"task": normalized},
    }
    return ok(result={"normalized": normalized}, commands=[command])


def _fetch_attachments(action_payload: dict[str, Any]) -> dict[str, Any]:
    input_payload = as_dict(action_payload.get("input"))
    attachments = [as_dict(item) for item in as_list(input_payload.get("attachments"))]
    return ok(result={"attachments": attachments})


def _sync_completion_status(action_payload: dict[str, Any]) -> dict[str, Any]:
    input_payload = as_dict(action_payload.get("input"))
    task_id = as_str(input_payload.get("task_id") or input_payload.get("source_id"))
    final_status = as_str(input_payload.get("final_status") or input_payload.get("status")).lower()
    if not task_id:
        return fail("invalid_payload", message="task_id is required")
    if final_status != "completed":
        return ok(
            result={
                "status": "skipped",
                "reason": "final_status_not_supported",
                "task_id": task_id,
                "final_status": final_status,
            }
        )
    return ok(result={"status": "updated", "task_id": task_id, "final_status": final_status})


def handle_action(action_id: str, action_payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    if action_id == "normalize_task_action":
        response = _normalize_task_action(action_payload)
        return response, 0 if bool(response.get("ok")) else 2
    if action_id == "fetch_attachments":
        response = _fetch_attachments(action_payload)
        return response, 0 if bool(response.get("ok")) else 2
    if action_id == "sync_completion_status":
        response = _sync_completion_status(action_payload)
        return response, 0 if bool(response.get("ok")) else 2
    return fail("invalid_payload", message=f"unknown_action:{action_id}"), 2
"""


def _task_ingress_queries_py(extension_id: str) -> str:
    return f"""\"\"\"Query handlers for generated task-ingress extension.\"\"\"
from __future__ import annotations

from typing import Any

from .common import as_dict, as_list, as_str
from .envelope import fail, ok


def _provider_health(query_payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    params = as_dict(query_payload.get("params"))
    context = as_dict(query_payload.get("context"))
    configured = bool(as_str(params.get("workspace_id") or context.get("workspace_id")))
    status = "healthy" if configured else "degraded"
    result = {{
        "status": status,
        "provider": "{extension_id}",
        "configured": configured,
    }}
    return ok(result=result), 0


def _list_remote_tasks(query_payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    params = as_dict(query_payload.get("params"))
    rows = [as_dict(item) for item in as_list(params.get("items"))]
    summary = {{
        "provider": "{extension_id}",
        "total_returned": len(rows),
    }}
    return ok(result={{"items": rows, "summary": summary}}), 0


def handle_query(query_id: str, query_payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    if query_id == "provider_health":
        return _provider_health(query_payload)
    if query_id == "list_remote_tasks":
        return _list_remote_tasks(query_payload)
    return fail("invalid_payload", message=f"unknown_query:{{query_id}}"), 2
"""


def _task_ingress_router_py() -> str:
    return """\"\"\"Unified request router for task-ingress extension operations.\"\"\"
from __future__ import annotations

from typing import Any

from .actions import handle_action
from .common import as_dict, as_str
from .envelope import fail


def handle_request(request_payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    mode = as_str(request_payload.get("mode")).lower()

    if mode == "query":
        from .queries import handle_query

        query = as_dict(request_payload.get("query"))
        query_id = as_str(query.get("query_id"))
        return handle_query(query_id, query)

    if mode == "action":
        action = as_dict(request_payload.get("action"))
        action_id = as_str(action.get("action_id"))
        return handle_action(action_id, action)

    return fail("invalid_payload", message=f"unsupported_request:{mode}"), 2
"""


def _dump_yaml(payload: dict[str, Any]) -> str:
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _scaffold_files(
    *, extension_id: str, display_name: str, description: str, vendor_name: str, category: str
) -> dict[str, str]:
    return {
        "extension.yaml": _dump_yaml(
            _manifest_payload(
                extension_id=extension_id,
                display_name=display_name,
                description=description,
                vendor_name=vendor_name,
                category=category,
            )
        ),
        "README.md": _readme_md(extension_id, display_name, description),
        MAIN_PY_FILENAME: _main_py(extension_id),
        HEALTH_PY_FILENAME: _health_py(extension_id),
        "scripts/smoke_check.py": _smoke_check_py(extension_id),
        f"tests/test_{extension_id}.py": _pytest_py(extension_id),
        "task_ingress/__init__.py": _task_ingress_init_py(),
        "task_ingress/common.py": _task_ingress_common_py(),
        "task_ingress/envelope.py": _task_ingress_envelope_py(),
        "task_ingress/normalize.py": _task_ingress_normalize_py(extension_id),
        "task_ingress/http.py": _task_ingress_http_py(),
        "task_ingress/actions.py": _task_ingress_actions_py(),
        "task_ingress/queries.py": _task_ingress_queries_py(extension_id),
        "task_ingress/router.py": _task_ingress_router_py(),
        "ui/actions/actions.yaml": _dump_yaml(_action_manifest_payload()),
        "ui/queries/queries.yaml": _dump_yaml(_query_manifest_payload()),
        "ui/ui_manifest.yaml": _dump_yaml(_ui_manifest_payload()),
        "ui/pages/overview.yaml": _dump_yaml(_overview_page_payload()),
        "ui/components/overview.yaml": _dump_yaml(
            _overview_component_payload(extension_id=extension_id)
        ),
        "schemas/config.schema.yaml": _dump_yaml(_config_schema_payload()),
        "schemas/secrets.schema.yaml": _dump_yaml(_secrets_schema_payload()),
        "schemas/subscriptions.schema.yaml": _dump_yaml(_subscriptions_schema_payload()),
    }


def create_task_ingress_extension(
    *,
    extension_id: str,
    output_root: Path,
    display_name: str = "",
    description: str = "",
    vendor_name: str = "Your Organization",
    category: str = "task-management",
    force: bool = False,
) -> Path:
    normalized_extension_id = _normalize_extension_id(extension_id)
    normalized_display_name = str(display_name or "").strip() or _slug_display_name(
        normalized_extension_id
    )
    normalized_description = str(description or "").strip() or (
        f"{normalized_display_name} task-ingress scaffold for the canonical Flow Steward extension runtime."
    )
    normalized_vendor_name = str(vendor_name or "").strip() or "Your Organization"
    normalized_category = str(category or "").strip().lower() or "task-management"
    target_dir = output_root / extension_folder_name(normalized_extension_id)

    if target_dir.exists():
        if not force:
            raise ValueError(f"Target directory already exists: {target_dir}")
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    files = _scaffold_files(
        extension_id=normalized_extension_id,
        display_name=normalized_display_name,
        description=normalized_description,
        vendor_name=normalized_vendor_name,
        category=normalized_category,
    )
    for rel_path, content in files.items():
        _write_text(target_dir / rel_path, content)
        if rel_path in {MAIN_PY_FILENAME, HEALTH_PY_FILENAME, "scripts/smoke_check.py"}:
            (target_dir / rel_path).chmod(0o755)
    # Every bundle ships its logo; this one starts with the scaffold's default.
    (target_dir / SCAFFOLD_ICON_FILENAME).write_bytes(DEFAULT_EXTENSION_ICON_PATH.read_bytes())
    return target_dir
