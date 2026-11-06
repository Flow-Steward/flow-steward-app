"""The browser receives an import provider exactly in the shape pinned here.

The frontend renders its Connection page tests from the same JSON file, so a
change to what Core emits for a `refresh_token_import` provider fails here and
there together, instead of crashing a live page that the tests never saw.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from core.application.extensions.browser_manifest_projection import project_browser_manifest
from core.application.extensions.extension_contract_v2 import validate_extension_contract_v2

FIXTURE = Path("assets/frontend/src/pages/extensions/oauth-import-manifest.fixture.json")
#: Hashes, not shape; their presence is asserted, their bytes are not pinned.
_FINGERPRINTS = ("security_fingerprint", "revocation_fingerprint")


def _descriptor() -> dict[str, Any]:
    return {
        "extension_id": "fixture.oauth-import",
        "action_definitions": [
            {
                "action_id": "verify_identity",
                "handler": {"mode": "extension"},
                "mutates_platform": False,
                "workflow_visible": False,
                "parameters": [],
            }
        ],
        "ui_manifest": {
            "version": "1",
            "pages": [
                {"page_id": "oauth_setup", "title": "Setup", "components": []},
                {"page_id": "oauth_help", "title": "Help", "components": []},
                {
                    "page_id": "accounts",
                    "title": "Accounts",
                    "components": [
                        {
                            "type": "connection_form",
                            "data": {
                                "oauth_provider_id": "console",
                                "connection_type": "console_account",
                                "test_action": "test_connection",
                            },
                        }
                    ],
                },
            ],
        },
        "runtime": {
            "extension_contract_v2": {
                "oauth_providers": [
                    {
                        "provider_id": "console",
                        "display_name": "Console Identity",
                        "description": "Connect with a refresh token issued in the provider console.",
                        "setup_page_id": "oauth_setup",
                        "help_page_id": "oauth_help",
                        "flow": {"type": "refresh_token_import"},
                        "credential_fields": [
                            {
                                "field_id": "client_id",
                                "value_type": "text",
                                "required": True,
                                "secret": False,
                                "change_policy": "reauthorization_required",
                                "label": "Client ID",
                                "help_text": "Enter the application client ID.",
                                "validation": {"min_length": 1, "max_length": 512},
                            },
                            {
                                "field_id": "client_secret",
                                "value_type": "secret",
                                "required": True,
                                "secret": True,
                                "change_policy": "runtime_revalidate",
                                "label": "Client secret",
                                "help_text": "Enter the application client secret.",
                                "validation": {"min_length": 1, "max_length": 4096},
                            },
                        ],
                        "token_endpoint": {
                            "url": "https://identity.example.test/token",
                            "client_auth_method": "client_secret_post",
                        },
                        "post_connect_action_id": "verify_identity",
                    }
                ],
                "connection_types": [
                    {
                        "connection_type_id": "console_account",
                        "display_name": "Console account",
                        "auth": {
                            "type": "oauth2",
                            "provider_id": "console",
                            "scopes": {"required": [], "optional": []},
                        },
                        "config_schema": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["account_ref"],
                            "properties": {
                                "account_ref": {
                                    "type": "string",
                                    "title": "Account reference",
                                    "minLength": 1,
                                }
                            },
                        },
                    }
                ],
            }
        },
    }


def emitted_manifests() -> dict[str, dict[str, Any]]:
    """What an administrator and any other role receive for this extension."""
    descriptor = _descriptor()
    result = validate_extension_contract_v2(descriptor)
    assert result["valid"] is True, result["errors"]
    contract = result["normalized_contract"]
    compiled = {
        "extension_id": descriptor["extension_id"],
        "oauth_providers": copy.deepcopy(contract["oauth_providers"]),
        "connection_types": copy.deepcopy(contract["connection_types"]),
        "ui_manifest": copy.deepcopy(descriptor["ui_manifest"]),
    }
    projected = project_browser_manifest(compiled)
    for provider in compiled["oauth_providers"]:
        for key in _FINGERPRINTS:
            assert provider.pop(key)
    return {"compiled": compiled, "projected": projected}


def test_browser_receives_the_pinned_import_provider_shapes() -> None:
    pinned = json.loads(FIXTURE.read_text(encoding="utf-8"))

    assert emitted_manifests() == pinned


def test_the_compiled_shape_has_no_redirect_only_fields() -> None:
    compiled = emitted_manifests()["compiled"]["oauth_providers"][0]

    # An administrator receives the compiled manifest itself, which names no
    # intents at all; the browser must not assume the array exists.
    for redirect_only in ("authorization_intents", "authorization_endpoint", "callback_id"):
        assert redirect_only not in compiled
    assert compiled["flow"] == {"type": "refresh_token_import"}
