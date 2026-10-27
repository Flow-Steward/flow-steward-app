"""Install a throwaway `acme.*` extension on a candidate, through the public API.

Release smoke has to prove the platform's install, enable, projection and
uninstall behaviour. Doing that against a shipped bundle makes the smoke a
statement about which extensions the candidate happens to package, and the checks
break the day that bundle changes its artifacts or moves to its own repository.

So the smoke brings its own. The bundle is built here, uploaded through
`POST /api/extensions/install` like any operator archive, and every identifier
the checks use afterwards comes back from the API. Nothing reaches into the
platform's database or its extensions directory.
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

EXTENSION_PREFIX = "acme.release-probe"
BUNDLE_PREFIX = "acme_release_probe"


#: The connection type a probe declares, so a project connection can be created
#: against it the way one is against any extension.
CONNECTION_TYPE_ID = "acme_probe_endpoint"
#: The workflow a probe publishes, so a project can be populated with one. The
#: platform qualifies it as `extension.<install_id>.<workflow_id>` once installed.
WORKFLOW_ID = "acme_probe_workflow"
#: The action the probe exposes so external-effect persistence can be asserted
#: without any real outbound traffic: the handler reports a delivery it did not
#: make, which is exactly what the platform records around.
ACTION_ID = "acme_probe_effect"
#: The action an inbound callback is routed to. Deliberately not the effect
#: action: an inbound delivery has no external effect to claim, and routing one
#: at an effect-declaring action is refused with `external_effect_claim_required`.
INGRESS_ACTION_ID = "acme_probe_ingress"
CALLBACK_ID = "events"
#: The probe's own signing headers, so no bundle's header names are assumed.
CALLBACK_SIGNATURE_HEADER = "X-Acme-Probe-Signature"
CALLBACK_TIMESTAMP_HEADER = "X-Acme-Probe-Timestamp"
CALLBACK_DELIVERY_HEADER = "X-Acme-Probe-Delivery"
CALLBACK_MAX_AGE_SECONDS = 300


def _manifest(extension_id: str) -> str:
    return json.dumps(
        {
            "manifest_version": 2,
            "lifecycle": {"data_schema_version": 1, "data_migrations": []},
            "extension_id": extension_id,
            "kind": "tool_provider",
            "version": "1.0.0",
            "display_name": "Acme Release Probe",
            "description": "Throwaway bundle installed by release smoke.",
            "vendor": {"name": "Acme Test"},
            "features": ["tool", "action"],
            "callbacks": [
                {
                    "callback_id": CALLBACK_ID,
                    "purpose": "inbound_events",
                    "methods": ["POST"],
                    "action_id": INGRESS_ACTION_ID,
                    "connection_scoped": True,
                    "auth": {
                        "type": "hmac_sha256",
                        "signature_header": CALLBACK_SIGNATURE_HEADER,
                        "timestamp_header": CALLBACK_TIMESTAMP_HEADER,
                        "delivery_id_header": CALLBACK_DELIVERY_HEADER,
                        "max_age_seconds": CALLBACK_MAX_AGE_SECONDS,
                    },
                }
            ],
            "external_effects": [
                {
                    "operation_id": ACTION_ID,
                    "effect_kind": "message_delivery",
                    "channel": "probe",
                    "idempotency": "required",
                    "test_mode_behavior": "suppress",
                    "observability": {
                        "target_fields": ["target"],
                        "target_kind": "recipient",
                        "target_label": "target",
                        "payload_size_field": "estimated_bytes",
                    },
                    "rate_limit": {
                        "window_seconds": 60,
                        "account_max": 60,
                        "project_max": 30,
                        "connection_max": 15,
                    },
                }
            ],
            "required_scopes": ["extension:invoke"],
            "entrypoint": {"command": ["python3", "main.py"]},
            "health": {"command": ["python3", "health.py"]},
            "ui_manifest": "ui/ui_manifest.yaml",
            # The actions file is referenced from here, not from the UI manifest:
            # `action_definitions` is compiled from this pointer, and the callback
            # validation reads that set.
            "action_manifest": "ui/actions/actions.yaml",
            "runtime": {
                "contract_version": "extension_host_v2",
                "extension_contract_v2": {"connection_types": "contracts/connection_types.yaml"},
            },
        }
    )


def _actions_manifest() -> str:
    """One action, declaring exactly the result fields the handler returns."""
    return json.dumps(
        {
            "actions": [
                {
                    "action_id": ACTION_ID,
                    "title": "Acme Probe Effect",
                    "description": "Reports a delivery so effect persistence can be asserted.",
                    "required_scopes": ["extension:invoke"],
                    "handler": {"mode": "extension"},
                    "mutates_platform": False,
                    "workflow_visible": True,
                    "parameters": [
                        {"name": "target", "type": "string", "required": True},
                        {"name": "fail", "type": "boolean", "required": False},
                    ],
                    "result_fields": [
                        "delivered",
                        "provider_message_id",
                        "estimated_bytes",
                    ],
                    "error_codes": ["probe_refused"],
                },
                {
                    "action_id": INGRESS_ACTION_ID,
                    "title": "Acme Probe Ingress",
                    "description": "Accepts an inbound callback delivery.",
                    "required_scopes": ["extension:invoke"],
                    "handler": {"mode": "extension"},
                    "mutates_platform": False,
                    "workflow_visible": False,
                    "parameters": [
                        {"name": "event_type", "type": "string", "required": False},
                    ],
                    "result_fields": ["delivered"],
                    "error_codes": [],
                },
            ]
        }
    )


def _action_handler() -> str:
    """A handler that answers on stdout, and can be asked to fail on purpose.

    It contacts nothing. What is under test is the row the platform writes around
    an action that claims an effect, and the status it records when one fails.
    """
    return (
        "import json, sys, uuid\n"
        "payload = json.loads(sys.stdin.read() or '{}')\n"
        # The host puts an action's arguments under `action.input`; the other
        # shapes are the non-action invocations of the same entrypoint.
        "action = payload.get('action') or {}\n"
        "data = action.get('input')\n"
        "if data is None:\n"
        "    data = (payload.get('action_payload') or {}).get('input')\n"
        "if data is None:\n"
        "    data = payload.get('input') or {}\n"
        "target = str(data.get('target') or '')\n"
        "if data.get('fail'):\n"
        "    print(json.dumps({\n"
        "        'ok': False, 'delivered': False, 'error_code': 'probe_refused',\n"
        "        'error': 'The probe was asked to refuse.',\n"
        "        'external_effect_status': 'failed',\n"
        "        'definitely_no_external_effect': True,\n"
        "    }, sort_keys=True))\n"
        "    raise SystemExit(2)\n"
        "print(json.dumps({\n"
        "    'ok': True, 'delivered': True,\n"
        "    'provider_message_id': 'probe-' + uuid.uuid4().hex,\n"
        "    'estimated_bytes': len(target),\n"
        "}, sort_keys=True))\n"
    )


def _workflow_artifact() -> str:
    """A workflow the platform accepts and can finish without reaching anything.

    Deterministic on purpose: checks that need a completed job — trash lifecycle
    on a populated project, a job whose step can be read back — must not depend
    on a model or a reachable endpoint.
    """
    return json.dumps(
        {
            "workflow_id": WORKFLOW_ID,
            "display_name": "Acme Probe Workflow",
            "description": "Throwaway workflow published by the release probe.",
            "enabled": True,
            "settings": {
                "callable": {
                    "enabled": True,
                    "trigger_channels": ["api"],
                    "input_mode": "fields",
                    "strict_inputs": False,
                    # Optional, so a check that submits nothing still runs it;
                    # a check that needs a completed job passes the object.
                    "expected_inputs": [{"name": "payload", "type": "object", "required": False}],
                },
                "execution_graph": {
                    "schema_version": 1,
                    "entry": {"kind": "step", "id": "probe_transform"},
                    "edges": [
                        {
                            "source": {"kind": "step", "id": "probe_transform"},
                            "outcome": {"name": "success"},
                            "target": {"kind": "step", "id": "done"},
                            "rework": False,
                        }
                    ],
                },
            },
            "steps": [
                {
                    "step_id": "probe_transform",
                    "display_name": "Probe Transform",
                    "step_kind": "transform",
                    "override": {
                        "transform": {
                            "input": {"source": "state.inputs.payload"},
                            "result_target": "state.probe",
                            "fields": [{"from": "marker", "to": "marker", "type": "text"}],
                        }
                    },
                },
                {
                    "step_id": "done",
                    "display_name": "Done",
                    "step_kind": "stop_success",
                    "override": {"stop_success": {"reason": "probe complete"}},
                },
            ],
            "groups": [],
        }
    )


def _descriptor(extension_id: str) -> str:
    """The descriptor an install reads, declaring the artifacts to publish."""
    return json.dumps(
        {
            "schema_version": "extension_descriptor_v1",
            "lifecycle": {"data_schema_version": 1, "data_migrations": []},
            "extension_id": extension_id,
            "release_version": "1.0.0",
            "extension_type": "service_extension",
            "identity": {
                "display_name": "Acme Release Probe",
                "description": "Throwaway bundle installed by release smoke.",
                "tags": ["release-smoke"],
            },
            "vendor": {"vendor_name": "Acme Test", "support_url": "https://acme.invalid/issues"},
            "listing": {"visibility": "public_reference", "category": "developer_examples"},
            "distribution": {
                "channel": "private_byo",
                "execution_modes": ["local"],
                "allows_self_hosted": True,
                "requires_connected_control_plane": False,
                "import_sources": ["file"],
            },
            "runtime": {
                "contract_version": "extension_host_v2",
                "compatibility": {"platform_min": "0.1.0"},
                "extension_contract_v2": {"connection_types": "contracts/connection_types.yaml"},
            },
            "provenance": {"origin": "imported", "notes": "Release smoke probe."},
            "commerce": {"billing_mode": "free"},
            "artifacts": {
                "workflows": [
                    {
                        "workflow_id": WORKFLOW_ID,
                        "ref": f"artifacts/workflows/{WORKFLOW_ID}.yaml",
                        "publish": True,
                    }
                ]
            },
        }
    )


def _connection_types() -> str:
    return json.dumps(
        {
            "connection_types": [
                {
                    "connection_type_id": CONNECTION_TYPE_ID,
                    "display_name": "Acme probe endpoint",
                    "description": "Throwaway connection type for release smoke.",
                    "config_schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["base_url"],
                        "properties": {
                            "base_url": {"type": "string"},
                            "allowed_hosts": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                    "secret_schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {},
                    },
                }
            ]
        }
    )


def build_archive(extension_id: str, bundle_dir: str) -> bytes:
    """Return a .zip an operator could upload, containing one minimal bundle."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{bundle_dir}/extension.yaml", _manifest(extension_id))
        archive.writestr(
            f"{bundle_dir}/ui/ui_manifest.yaml",
            "version: '1'\npages: []\n",
        )
        archive.writestr(f"{bundle_dir}/ui/actions/actions.yaml", _actions_manifest())
        archive.writestr(f"{bundle_dir}/main.py", _action_handler())
        archive.writestr(f"{bundle_dir}/health.py", "print('ok')\n")
        archive.writestr(f"{bundle_dir}/contracts/connection_types.yaml", _connection_types())
        archive.writestr(f"{bundle_dir}/extension-descriptor.yaml", _descriptor(extension_id))
        archive.writestr(
            f"{bundle_dir}/artifacts/workflows/{WORKFLOW_ID}.yaml", _workflow_artifact()
        )
        archive.writestr(f"{bundle_dir}/README.md", "# Acme Release Probe\n")
    return buffer.getvalue()


def _multipart(field: str, filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = f"----flowsteward-smoke-{uuid.uuid4().hex}"
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode(),
            b"Content-Type: application/zip\r\n\r\n",
            content,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
    return body, f"multipart/form-data; boundary={boundary}"


@contextmanager
def installed_probe_extension(api: Any) -> Iterator[dict[str, Any]]:
    """Install a throwaway bundle for the block, and remove it afterwards.

    Yields what the API reported: at least `extension_id`, plus whatever install
    identifiers the response carries. Cleanup runs even when the block raises,
    because a probe left installed changes the candidate for every later check.
    """
    suffix = uuid.uuid4().hex[:8]
    extension_id = f"{EXTENSION_PREFIX}-{suffix}"
    bundle_dir = f"{BUNDLE_PREFIX}_{suffix}"
    body, content_type = _multipart(
        "file", f"{bundle_dir}.zip", build_archive(extension_id, bundle_dir)
    )
    status, payload = api.request_raw(
        "POST",
        "/api/extensions/install",
        body,
        extra_headers={"Content-Type": content_type},
    )
    if status not in {200, 201}:
        raise RuntimeError(f"probe extension install failed with HTTP {status}: {payload}")
    detail = payload if isinstance(payload, dict) else {}
    detail.setdefault("extension_id", extension_id)
    try:
        yield detail
    finally:
        # Uninstall is gated on the revision of a plan the caller has seen, which
        # is what stops a stale confirmation removing something else.
        plan_status, plan = api.request("GET", f"/api/extensions/{extension_id}/uninstall-plan")
        revision = str((plan or {}).get("revision") or "") if plan_status == 200 else ""
        status, payload = api.request(
            "POST",
            f"/api/extensions/{extension_id}/uninstall",
            {"revision": revision},
        )
        if status not in {200, 202, 204}:
            raise RuntimeError(
                f"probe extension {extension_id} was left installed: HTTP {status}: {payload}"
            )


__all__ = [
    "ACTION_ID",
    "CALLBACK_DELIVERY_HEADER",
    "CALLBACK_ID",
    "CALLBACK_MAX_AGE_SECONDS",
    "CALLBACK_SIGNATURE_HEADER",
    "CALLBACK_TIMESTAMP_HEADER",
    "CONNECTION_TYPE_ID",
    "EXTENSION_PREFIX",
    "INGRESS_ACTION_ID",
    "WORKFLOW_ID",
    "build_archive",
    "installed_probe_extension",
]
