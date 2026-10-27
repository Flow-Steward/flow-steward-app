"""A synthetic LLM provider manifest for platform tests.

The platform's own tests must not read a shipped provider bundle to get a valid
`llm_provider` contract: what they exercise is the platform's handling of that
contract, and a bundle is free to change its own declaration or move out of this
repository entirely.

Two shapes are offered because the platform treats them differently: one that
requires an upstream base URL in the connection config, and one that does not.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def llm_provider_manifest(
    *,
    extension_id: str = "acme.local-llm",
    supplier_key: str = "acme_local_llm",
    supplier_label: str = "Acme Local LLM",
    required_connection_config: tuple[str, ...] = ("upstream_base_url",),
    required_provider_secrets: tuple[str, ...] = ("runtime_bearer_token",),
    optional_provider_secrets: tuple[str, ...] = ("bearer_token",),
) -> dict[str, Any]:
    """Return a manifest declaring a remote-https LLM provider.

    The defaults model a self-hosted provider shape: an upstream base URL the
    operator supplies, a required runtime bearer token and an optional upstream
    one.
    """
    return {
        "manifest_version": 2,
        "lifecycle": {"data_schema_version": 1, "data_migrations": []},
        "extension_id": extension_id,
        "kind": "tool_provider",
        "version": "1.0.0",
        "release_stage": "active",
        "display_name": supplier_label,
        "description": "Synthetic LLM provider for platform tests.",
        "vendor": {"name": "Acme Test"},
        "listing": {"visibility": "public_reference", "category": "developer_examples"},
        "features": ["tool"],
        "required_scopes": ["extension:invoke"],
        "entrypoint": {"command": ["python3", "main.py"]},
        # A bundle's manifest spells this `health`; the compiled runtime row the
        # platform builds from it spells the same thing `healthcheck`. These
        # tests hand one dict in as both, so it carries both keys - otherwise the
        # enable gate sees health declared under the manifest spelling and the
        # runner, which reads only the compiled one, reports that no health
        # command is configured.
        "health": {"command": ["python3", "health.py"]},
        "healthcheck": {"command": ["python3", "health.py"]},
        "ui_manifest": "ui/ui_manifest.yaml",
        "runtime": {
            "contract_version": "extension_host_v2",
            "compatibility": {"platform_min": "1.0.0"},
            "remote": {
                "base_url": "",
                "endpoints": {
                    "handshake": "/handshake",
                    "health": "/health",
                    "query": "/queries/{query_id}",
                    "action": "/actions/{action_id}",
                    "emit_event": "/events",
                },
            },
            "extension_contract_v2": {
                "llm_provider": {
                    "schema_version": "llm_provider_extension_v1",
                    "supplier_key": supplier_key,
                    "supplier_label": supplier_label,
                    "runtime_mode": "remote_https",
                    "credential_provider_key": supplier_key,
                    "required_provider_secrets": list(required_provider_secrets),
                    "optional_provider_secrets": list(optional_provider_secrets),
                    "required_connection_config": list(required_connection_config),
                    "operations": {
                        "list_models": {"query_id": "llm.list_models"},
                        "chat": {"action_id": "llm.chat"},
                        "structured_chat": {
                            "action_id": "llm.chat_structured",
                            "optional": True,
                        },
                    },
                    "model_grouping": {
                        "strategy": "endpoint",
                        "owner_key_field": "upstream_owner_key",
                        "owner_label_field": "upstream_owner_label",
                    },
                    "required_capabilities_for_enablement": [
                        "chat",
                        "tools",
                        "structured_output",
                    ],
                }
            },
        },
    }


def hosted_llm_provider_manifest(**kwargs: Any) -> dict[str, Any]:
    """A provider whose upstream is fixed, so no base URL is asked of the operator.

    Models a hosted-router provider shape: it authenticates to its upstream with
    its own `api_key` as well as to its runtime, and declares no optional secret.
    """
    kwargs.setdefault("extension_id", "acme.router-llm")
    kwargs.setdefault("supplier_key", "acme_router_llm")
    kwargs.setdefault("supplier_label", "Acme Router LLM")
    kwargs.setdefault("required_provider_secrets", ("api_key", "runtime_bearer_token"))
    kwargs.setdefault("optional_provider_secrets", ())
    kwargs["required_connection_config"] = ()
    return llm_provider_manifest(**kwargs)


def write_llm_provider_bundle(root: Path, descriptor: dict[str, Any]) -> Path:
    """Materialize a descriptor as a real bundle and return its root.

    A descriptor alone does not get an install through enablement: the health gate
    validates `health.command` against an existing bundle root and then runs it,
    and the browser-authority check compiles the bundle from disk. The shipped
    bundle these fixtures replaced used to satisfy both. Same shape as
    `vendored_wheels.write_bundle` - manifest, UI manifest, entrypoint, health.

    Two things the compiler insists on, so they are not left to call sites:
    the folder is named for the canonical package id, and the manifest on disk
    carries `health` only - `healthcheck` is the compiled spelling and the
    manifest validator rejects it as an unknown field.
    """
    extension_id = str(descriptor.get("extension_id") or "").strip()
    folder = extension_id.replace(".", "_").replace("-", "_")
    bundle = root / folder if folder else root
    (bundle / "ui").mkdir(parents=True, exist_ok=True)
    manifest = {key: value for key, value in descriptor.items() if key != "healthcheck"}
    (bundle / "extension.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )
    (bundle / "ui" / "ui_manifest.yaml").write_text("version: '1'\npages: []\n", encoding="utf-8")
    (bundle / "main.py").write_text("print('{\"ok\": true}')\n", encoding="utf-8")
    (bundle / "health.py").write_text('print(\'{"status": "healthy"}\')\n', encoding="utf-8")
    return bundle
