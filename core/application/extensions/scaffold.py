"""Application service for generating canonical YAML-only extension scaffolds."""

from __future__ import annotations

import errno
import shutil
import stat
import uuid
from pathlib import Path
from typing import Any

import yaml

from core.application.extensions.default_icon import DEFAULT_EXTENSION_ICON_PATH
from core.application.extensions.identity import extension_folder_name, normalize_extension_id
from core.application.extensions.remote_runtime_protocol_v2 import HOST_CONTRACT_VERSION
from core.application.extensions.scaffold_contracts import (
    ExtensionScaffoldConflictError,
    ExtensionScaffoldCreateInput,
    ExtensionScaffoldError,
    ExtensionScaffoldResult,
)
from core.infrastructure.extension_runtime.atomic_publish import (
    rename_no_replace as _rename_no_replace,
)

_MESSAGE_YOUR_ORGANIZATION = "Your Organization"

HEALTH_PY_FILENAME = "health.py"
HTTPS_JSON_SCHEMA_ORG_DRAFT_2020_12_SCHEMA_VALUE = "https://json-schema.org/draft/2020-12/schema"

MAIN_PY_FILENAME = "main.py"
READ_PROJECTS_VALUE = "read:projects"
SCHEMA_VALUE = "$schema"

#: The logo a new extension starts with: the grey package glyph. The bundle's
#: ``icon.png`` is required, so the scaffold writes one, and the developer
#: replaces that file with their own logo.
SCAFFOLD_ICON_FILENAME = "icon.png"

SCAFFOLD_MANIFEST_HEADER = """# Flow Steward extension scaffold TODO:
# This manifest is a minimal valid placeholder so the bundle passes validation.
# Before publishing, choose the real extension kind and replace placeholder
# features, actions, queries, schemas, callbacks, and event_subscriptions.
# For example, notification providers should not keep tool_provider/run_tool
# semantics unless they are also intentionally exposing a tool action.
"""


def _slug_display_name(extension_id: str) -> str:
    words = [token for token in extension_id.replace("-", "_").split("_") if token]
    if not words:
        return extension_id
    return " ".join(token.capitalize() for token in words)


def _manifest_payload(
    *,
    extension_id: str,
    display_name: str,
    description: str,
    vendor_name: str,
    category: str,
) -> dict[str, Any]:
    return {
        "manifest_version": 2,
        "lifecycle": {"data_schema_version": 1, "data_migrations": []},
        "extension_id": extension_id,
        "kind": "tool_provider",
        "version": "1.0.0",
        "runtime": {"contract_version": HOST_CONTRACT_VERSION},
        "display_name": display_name,
        "description": description,
        "vendor": {"name": vendor_name},
        "listing": {
            "category": category,
            "tags": [],
            "summary": description,
        },
        "features": ["tool", "action"],
        "event_subscriptions": [],
        "required_scopes": ["extension:invoke", READ_PROJECTS_VALUE],
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
                "action_id": "save_settings",
                "title": "Save settings",
                "description": "Persist sample form values (scaffold action).",
                "required_scopes": ["extension:invoke"],
                "handler": {"mode": "extension"},
                "result_fields": ["result.status", "result.saved"],
                "error_codes": ["invalid_payload", "internal_error"],
                "mutates_platform": False,
                "workflow_visible": False,
                "parameters": [
                    {"name": "name", "type": "string", "required": False},
                    {"name": "mode", "type": "string", "required": False},
                    {"name": "enabled", "type": "boolean", "required": False},
                    {"name": "notes", "type": "string", "required": False},
                ],
            }
        ]
    }


def _query_manifest_payload() -> dict[str, Any]:
    return {
        "queries": [
            {
                "query_id": "provider_health",
                "title": "Provider health",
                "description": "Returns extension health and configuration status.",
                "required_scopes": [READ_PROJECTS_VALUE],
                "result_fields": ["result.status", "result.configured"],
                "error_codes": ["not_configured", "internal_error"],
                "handler": {"mode": "extension"},
            },
            {
                "query_id": "load_settings",
                "title": "Load settings",
                "description": "Return sample settings payload for the scaffold form.",
                "required_scopes": [READ_PROJECTS_VALUE],
                "result_fields": ["result.settings"],
                "error_codes": ["internal_error"],
                "handler": {"mode": "extension"},
            },
        ]
    }


def _ui_manifest_payload() -> dict[str, Any]:
    return {
        "version": "1",
        "pages": [
            {"ref": "pages/overview.yaml"},
            {"ref": "pages/form.yaml"},
        ],
    }


def _overview_component_payload(*, extension_id: str) -> dict[str, Any]:
    return {
        "component_id": "overview_markdown",
        "type": "markdown",
        "title": "Overview",
        "content": (
            f"`{extension_id}` scaffold is ready. "
            "Add provider-specific configuration, actions, queries, and callbacks as needed."
        ),
    }


def _overview_page_payload() -> dict[str, Any]:
    return {
        "page_id": "overview",
        "title": "Overview",
        "components": [{"ref": "../components/overview.yaml"}],
    }


def _form_page_payload() -> dict[str, Any]:
    return {
        "page_id": "form",
        "title": "Sample form",
        "components": [{"ref": "../components/sample_form.yaml"}],
    }


def _sample_form_component_payload() -> dict[str, Any]:
    return {
        "component_id": "sample_form",
        "type": "form",
        "title": "Sample settings form",
        "description": "Example of typed fields and save action in the extension UI.",
        "query_id": "load_settings",
        "ui_schema": {
            "fields": [
                {
                    "field_id": "name",
                    "path": "name",
                    "label": "Name",
                    "widget": "text",
                    "placeholder": "Acme extension",
                },
                {
                    "field_id": "mode",
                    "path": "mode",
                    "label": "Mode",
                    "widget": "select",
                    "props": {
                        "options": [
                            {"value": "dev", "label": "Development"},
                            {"value": "prod", "label": "Production"},
                        ]
                    },
                },
                {
                    "field_id": "enabled",
                    "path": "enabled",
                    "label": "Enabled",
                    "widget": "boolean",
                },
                {
                    "field_id": "notes",
                    "path": "notes",
                    "label": "Notes",
                    "widget": "textarea",
                    "placeholder": "Optional notes",
                },
            ],
            "actions": [
                {
                    "action_id": "save_settings",
                    "label": "Save settings",
                    "kind": "action",
                }
            ],
        },
    }


def _config_schema_payload() -> dict[str, Any]:
    return {
        SCHEMA_VALUE: HTTPS_JSON_SCHEMA_ORG_DRAFT_2020_12_SCHEMA_VALUE,
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "project_id": {"type": "string", "minLength": 1},
            "enabled": {"type": "boolean", "default": True},
        },
    }


def _secrets_schema_payload() -> dict[str, Any]:
    return {
        SCHEMA_VALUE: HTTPS_JSON_SCHEMA_ORG_DRAFT_2020_12_SCHEMA_VALUE,
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "api_token": {
                "type": "string",
                "minLength": 1,
                "description": "Optional provider API token.",
            },
        },
    }


def _subscriptions_schema_payload() -> dict[str, Any]:
    return {
        SCHEMA_VALUE: HTTPS_JSON_SCHEMA_ORG_DRAFT_2020_12_SCHEMA_VALUE,
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "events": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "default": [],
            }
        },
    }


def _main_py(extension_id: str) -> str:
    return f"""#!/usr/bin/env python3
\"\"\"{extension_id} extension entrypoint.\"\"\"
from __future__ import annotations

import json
import sys

from flowsteward_extension_sdk import runtime_context_from_payload
from handlers.common import fail
from handlers.router import handle_request


def main() -> int:
    raw = sys.stdin.read().strip()
    try:
        request_payload = json.loads(raw) if raw else {{}}
    except Exception:
        print(json.dumps(fail("invalid_payload", "Request payload must be valid JSON")))
        return 2

    if not isinstance(request_payload, dict):
        print(json.dumps(fail("invalid_payload", "Request payload must be an object")))
        return 2

    # The host's envelope is checked before any work: another contract, a
    # runtime context without the four scope keys, or one naming both a Group
    # and a For each as the step's owner is refused with no external effect.
    try:
        runtime_context_from_payload(request_payload)
    except ValueError as exc:
        response = fail("invalid_host_envelope", str(exc))
        response["definitely_no_external_effect"] = True
        print(json.dumps(response))
        return 2

    response = handle_request(request_payload)
    print(json.dumps(response))
    return 0 if bool(response.get("ok")) else 2


if __name__ == "__main__":
    raise SystemExit(main())
"""


def _health_py(extension_id: str) -> str:
    module_name = extension_id.replace("-", "_")
    return f"""#!/usr/bin/env python3
\"\"\"{extension_id} extension health check.\"\"\"
from __future__ import annotations

import importlib.util
import json
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
    main_ok, main_detail = _module_loadable(root / "main.py", "{module_name}_main")
    checks = [
        {{
            "name": "entrypoint",
            "status": "healthy" if main_ok else "failed",
            "detail": main_detail or "main.py is importable",
        }}
    ]
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


def _handlers_init_py() -> str:
    return """\"\"\"Extension handler package.\"\"\"\n"""


def _handlers_common_py() -> str:
    return """\"\"\"Shared helpers for extension query/action handlers.\"\"\"
from __future__ import annotations

from typing import Any


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def ok(result: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": True,
        "result": result or {},
        "events": [],
        "commands": [],
        "reload": [],
        "errors": [],
    }
    payload.update(extra)
    return payload


def fail(code: str, message: str = "") -> dict[str, Any]:
    normalized_code = str(code or "internal_error")
    normalized_message = str(message or normalized_code)
    return {
        "ok": False,
        "error_code": normalized_code,
        "error": normalized_message,
        "result": {},
        "events": [],
        "commands": [],
        "reload": [],
        "errors": [{"code": normalized_code, "message": normalized_message}],
    }
"""


def _handlers_queries_py(extension_id: str) -> str:
    return f"""\"\"\"Query handlers for {extension_id}.\"\"\"
from __future__ import annotations

from handlers.common import as_dict, ok


def provider_health(payload: dict) -> dict:
    params = as_dict(payload.get("params"))
    context = as_dict(payload.get("context"))
    configured = bool(params.get("project_id") or context.get("project_id"))
    return ok(
        {{
            "status": "healthy" if configured else "degraded",
            "provider": "{extension_id}",
            "configured": configured,
        }}
    )


def load_settings(payload: dict) -> dict:
    _ = payload
    return ok(
        {{
            "settings": {{
                "name": "Example extension",
                "mode": "dev",
                "enabled": True,
                "notes": "",
            }}
        }}
    )


QUERY_HANDLERS = {{
    "provider_health": provider_health,
    "load_settings": load_settings,
}}
"""


def _handlers_actions_py() -> str:
    return """\"\"\"Action handlers for scaffold extension.\"\"\"\nfrom __future__ import annotations\n\nfrom handlers.common import as_dict, ok\n\n\ndef save_settings(payload: dict) -> dict:\n    values = as_dict(payload.get(\"values\"))\n    return ok({\"status\": \"saved\", \"saved\": values})\n\n\nACTION_HANDLERS = {\n    \"save_settings\": save_settings,\n}\n"""


def _handlers_router_py() -> str:
    return """\"\"\"Request router for scaffold extension.\"\"\"\nfrom __future__ import annotations\n\nfrom handlers.actions import ACTION_HANDLERS\nfrom handlers.common import as_dict, fail\nfrom handlers.queries import QUERY_HANDLERS\n\n\ndef _resolve_mode(request_payload: dict) -> str:\n    mode = str(request_payload.get(\"mode\") or \"\").strip().lower()\n    if mode:\n        return mode\n    if isinstance(request_payload.get(\"query\"), dict):\n        return \"query\"\n    if isinstance(request_payload.get(\"action\"), dict):\n        return \"action\"\n    return \"\"\n\n\ndef handle_request(request_payload: dict) -> dict:\n    mode = _resolve_mode(request_payload)\n\n    if mode == \"query\":\n        query = as_dict(request_payload.get(\"query\"))\n        query_id = str(query.get(\"query_id\") or request_payload.get(\"query_id\") or \"\").strip()\n        handler = QUERY_HANDLERS.get(query_id)\n        if handler is None:\n            return fail(\"invalid_payload\", f\"unknown_query:{query_id}\")\n        return handler(query)\n\n    if mode == \"action\":\n        action = as_dict(request_payload.get(\"action\"))\n        action_id = str(action.get(\"action_id\") or request_payload.get(\"action_id\") or \"\").strip()\n        handler = ACTION_HANDLERS.get(action_id)\n        if handler is None:\n            return fail(\"invalid_payload\", f\"unknown_action:{action_id}\")\n        return handler(action)\n\n    return fail(\"invalid_payload\", f\"unsupported_request:{mode}\")\n"""


def _readme_md(extension_id: str, display_name: str, description: str) -> str:
    return f"""# {display_name}

{description}

This extension was generated by the Flow Steward extension scaffold.

## Logo

`icon.png` next to `extension.yaml` is the extension's logo. Flow Steward shows
it in the extension list, the catalog and the workflow builder. The scaffold
writes a default grey package glyph; replace that file with your own logo,
keeping the name. Validation, `extensions bundle` and install refuse a bundle
whose `icon.png` is missing or breaks the rules:

- PNG only (no SVG);
- square, 32-512 px on a side, 128x128 recommended;
- at most 100 KB.

## Host contract

The extension declares `runtime.contract_version: extension_host_v2` and
uses `flowsteward-extension-sdk` 0.4.0. `main.py` checks every payload with
`runtime_context_from_payload` before any work: an envelope of another
contract, or a `runtime_context` missing one of the four scope keys
(`group_id`, `for_each_id`, `step_id`, `step_execution_id`) or naming both a
Group and a For each as the step's owner (at most one owner), is refused with
`invalid_host_envelope` and `definitely_no_external_effect: true`.

## Scaffold Placeholders

The generated manifest is intentionally a minimal valid starter, not a
recommendation that every extension should be a tool provider. These fields are
placeholders:

- `kind: tool_provider`
- `features: [tool, action]`
- `ui/actions/actions.yaml` with `save_settings`
- `ui/queries/queries.yaml` with `provider_health`

Before publishing, update `kind`, `features`, actions, queries, schemas,
callbacks, and supported events to match the real provider type. For example,
notification providers, task providers, callback consumers, and dataset-facing
extensions should replace the default tool action unless they intentionally
expose one.

## Authoring Format

Extension authoring contract files are YAML-only. Keep these files in YAML:

- `extension.yaml`
- `schemas/config.schema.yaml`
- `schemas/secrets.schema.yaml`
- `schemas/subscriptions.schema.yaml`
- `ui/ui_manifest.yaml`
- `ui/actions/actions.yaml`
- `ui/queries/queries.yaml`
- `ui/pages/*.yaml`
- `ui/components/*.yaml`

Do not add `extension.json`, `manifest.json`, JSON UI manifests, JSON action/query
manifests, JSON component files, or `schemas/*.schema.json`.

Python code may still use JSON for subprocess request/response envelopes. Flow Steward
REST APIs, database JSONB fields, and CLI `--json` output may also use JSON.
Only author-facing extension contract files must be YAML.

## Validate and Health

```bash
flow-steward extensions validate {extension_id} --health
```

## Test

```bash
flow-steward extensions test {extension_id}
```

The public test command runs the generated smoke check and pytest suite in the
dedicated Development test service. Test dependencies remain outside the
extension bundle and production runtime.

## Lock Dependencies

```bash
flow-steward extensions lock-deps {extension_id}
```

## Bundle

```bash
flow-steward extensions bundle {extension_id}
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


def _envelope(body: dict) -> dict:
    \"\"\"A request as the host sends it: the contract and a root step's scope.\"\"\"
    return {{
        "contract_version": "extension_host_v2",
        "runtime_context": {{
            "group_id": None,
            "for_each_id": None,
            "step_id": None,
            "step_execution_id": None,
        }},
        **body,
    }}


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
        _envelope(
            {{
                "mode": "query",
                "query": {{
                    "query_id": "provider_health",
                    "params": {{"project_id": "local-smoke"}},
                }},
            }}
        ),
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

import pytest

BUNDLE_ROOT = Path(__file__).resolve().parents[1]
ROOT_STEP_SCOPE = {{
    "group_id": None,
    "for_each_id": None,
    "step_id": None,
    "step_execution_id": None,
}}


def _envelope(body: dict) -> dict:
    \"\"\"A request as the host sends it: the contract and a root step's scope.\"\"\"
    return {{
        "contract_version": "extension_host_v2",
        "runtime_context": dict(ROOT_STEP_SCOPE),
        **body,
    }}


def _run_main(payload: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(BUNDLE_ROOT / "main.py")],
        cwd=str(BUNDLE_ROOT),
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def _invoke(payload: dict) -> dict:
    proc = _run_main(payload)
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
        _envelope(
            {{
                "mode": "query",
                "query": {{
                    "query_id": "provider_health",
                    "params": {{"project_id": "pytest"}},
                }},
            }}
        )
    )
    assert payload["ok"] is True
    assert payload["result"]["provider"] == "{extension_id}"


def test_run_tool_action_passes() -> None:
    payload = _invoke(
        _envelope(
            {{
                "mode": "action",
                "action": {{
                    "action_id": "save_settings",
                    "values": {{"name": "demo"}},
                }},
            }}
        )
    )
    assert payload["ok"] is True
    assert payload["result"]["status"] == "saved"


@pytest.mark.parametrize(
    ("contract_version", "runtime_context"),
    [
        ("extension_host_v1", dict(ROOT_STEP_SCOPE)),
        (
            "extension_host_v2",
            {{key: value for key, value in ROOT_STEP_SCOPE.items() if key != "for_each_id"}},
        ),
        ("extension_host_v2", {{**ROOT_STEP_SCOPE, "group_id": "g1", "for_each_id": "f1"}}),
    ],
    ids=["foreign-contract", "missing-for-each-id", "two-owners"],
)
def test_a_foreign_or_incomplete_envelope_is_refused(
    contract_version: str, runtime_context: dict
) -> None:
    proc = _run_main(
        {{
            "contract_version": contract_version,
            "runtime_context": runtime_context,
            "mode": "query",
            "query": {{"query_id": "provider_health", "params": {{}}}},
        }}
    )
    assert proc.returncode == 2, proc.stderr or proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["ok"] is False
    assert payload["error_code"] == "invalid_host_envelope"
    assert payload["definitely_no_external_effect"] is True
"""


def _dump_yaml(payload: dict[str, Any]) -> str:
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)


def _manifest_yaml(payload: dict[str, Any]) -> str:
    return f"{SCAFFOLD_MANIFEST_HEADER}{_dump_yaml(payload)}"


def _write_file(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def _scaffold_tree_snapshot(root: Path) -> tuple[tuple[str, str, int, bytes], ...]:
    root_stat = root.lstat()
    if not stat.S_ISDIR(root_stat.st_mode):
        raise OSError(errno.EINVAL, "Scaffold artifact must be a directory", root)
    entries = [(".", "directory", stat.S_IMODE(root_stat.st_mode), b"")]
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISDIR(metadata.st_mode):
            entries.append((relative, "directory", mode, b""))
        elif stat.S_ISREG(metadata.st_mode):
            entries.append((relative, "file", mode, path.read_bytes()))
        else:
            raise OSError(errno.EINVAL, "Scaffold artifact type is unsupported", path)
    return tuple(entries)


def _scaffold_files(
    *,
    extension_id: str,
    display_name: str,
    description: str,
    vendor_name: str,
    category: str,
) -> dict[str, str | bytes]:
    return {
        "extension.yaml": _manifest_yaml(
            _manifest_payload(
                extension_id=extension_id,
                display_name=display_name,
                description=description,
                vendor_name=vendor_name,
                category=category,
            )
        ),
        "README.md": _readme_md(extension_id, display_name, description),
        SCAFFOLD_ICON_FILENAME: DEFAULT_EXTENSION_ICON_PATH.read_bytes(),
        "requirements.in": (
            "# Add direct runtime dependencies here before running extensions lock-deps.\n"
            "# Leave this file comment-only when the extension has no runtime dependencies.\n"
        ),
        "requirements-dev.txt": (
            "# Add standalone test dependencies or local wheel references here.\n"
            "# Leave this file comment-only when only the generated smoke check is required.\n"
        ),
        MAIN_PY_FILENAME: _main_py(extension_id),
        HEALTH_PY_FILENAME: _health_py(extension_id),
        "scripts/smoke_check.py": _smoke_check_py(extension_id),
        f"tests/test_{extension_folder_name(extension_id)}.py": _pytest_py(extension_id),
        "handlers/__init__.py": _handlers_init_py(),
        "handlers/common.py": _handlers_common_py(),
        "handlers/queries.py": _handlers_queries_py(extension_id),
        "handlers/actions.py": _handlers_actions_py(),
        "handlers/router.py": _handlers_router_py(),
        "ui/actions/actions.yaml": _dump_yaml(_action_manifest_payload()),
        "ui/queries/queries.yaml": _dump_yaml(_query_manifest_payload()),
        "ui/ui_manifest.yaml": _dump_yaml(_ui_manifest_payload()),
        "ui/pages/overview.yaml": _dump_yaml(_overview_page_payload()),
        "ui/pages/form.yaml": _dump_yaml(_form_page_payload()),
        "ui/components/overview.yaml": _dump_yaml(
            _overview_component_payload(extension_id=extension_id)
        ),
        "ui/components/sample_form.yaml": _dump_yaml(_sample_form_component_payload()),
        "schemas/config.schema.yaml": _dump_yaml(_config_schema_payload()),
        "schemas/secrets.schema.yaml": _dump_yaml(_secrets_schema_payload()),
        "schemas/subscriptions.schema.yaml": _dump_yaml(_subscriptions_schema_payload()),
    }


def _create_scaffold(
    *,
    extension_id: str,
    output_root: Path,
    display_name: str = "",
    description: str = "",
    vendor_name: str = _MESSAGE_YOUR_ORGANIZATION,
    category: str = "other",
) -> tuple[Path, dict[str, str | bytes]]:
    normalized_extension_id = normalize_extension_id(extension_id)
    normalized_display_name = str(display_name or "").strip() or _slug_display_name(
        normalized_extension_id
    )
    normalized_description = str(description or "").strip() or (
        f"{normalized_display_name} scaffold for the canonical Flow Steward extension runtime."
    )
    normalized_vendor_name = str(vendor_name or "").strip() or _MESSAGE_YOUR_ORGANIZATION
    normalized_category = str(category or "").strip().lower() or "other"
    target_dir = output_root / extension_folder_name(normalized_extension_id)

    if target_dir.exists():
        raise ExtensionScaffoldConflictError(f"Target directory already exists: {target_dir}")
    output_root.mkdir(parents=True, exist_ok=True)

    files = _scaffold_files(
        extension_id=normalized_extension_id,
        display_name=normalized_display_name,
        description=normalized_description,
        vendor_name=normalized_vendor_name,
        category=normalized_category,
    )
    staged_dir = output_root / f".{target_dir.name}.scaffold-{uuid.uuid4().hex}"
    staged_dir.mkdir(mode=0o777)
    try:
        for rel_path, content in files.items():
            _write_file(staged_dir / rel_path, content)
            if rel_path in {MAIN_PY_FILENAME, HEALTH_PY_FILENAME, "scripts/smoke_check.py"}:
                (staged_dir / rel_path).chmod(0o755)
        if target_dir.exists():
            raise ExtensionScaffoldConflictError(f"Target directory already exists: {target_dir}")
        staged_snapshot = _scaffold_tree_snapshot(staged_dir)
        try:
            _rename_no_replace(staged_dir, target_dir)
        except OSError as exc:
            try:
                ambiguous_success = (
                    not staged_dir.exists()
                    and _scaffold_tree_snapshot(target_dir) == staged_snapshot
                )
            except OSError:
                ambiguous_success = False
            if ambiguous_success:
                return target_dir, files
            if exc.errno in {errno.EEXIST, errno.ENOTEMPTY}:
                raise ExtensionScaffoldConflictError(
                    f"Target directory already exists: {target_dir}"
                ) from exc
            raise
    finally:
        if staged_dir.exists():
            shutil.rmtree(staged_dir, ignore_errors=True)
    return target_dir, files


def create_extension(
    *,
    extension_id: str,
    output_root: Path,
    display_name: str = "",
    description: str = "",
    vendor_name: str = _MESSAGE_YOUR_ORGANIZATION,
    category: str = "other",
) -> Path:
    """Create one scaffold directory without transport concerns or overwrites."""
    target_dir, _ = _create_scaffold(
        extension_id=extension_id,
        output_root=output_root,
        display_name=display_name,
        description=description,
        vendor_name=vendor_name,
        category=category,
    )
    return target_dir


class ExtensionScaffoldService:
    """Creates canonical extension scaffolds from a validated application command."""

    def create(self, command: ExtensionScaffoldCreateInput) -> ExtensionScaffoldResult:
        """Create a scaffold and return its structured creation result."""
        target_dir, files = _create_scaffold(
            extension_id=command.extension_id,
            output_root=command.output_root,
            display_name=command.display_name,
            description=command.description,
            vendor_name=command.vendor_name,
            category=command.category,
        )
        return ExtensionScaffoldResult(
            status="created",
            extension_id=command.extension_id,
            path=str(target_dir),
            files_created=len(files),
        )


__all__ = [
    "ExtensionScaffoldConflictError",
    "ExtensionScaffoldCreateInput",
    "ExtensionScaffoldError",
    "ExtensionScaffoldResult",
    "ExtensionScaffoldService",
]
