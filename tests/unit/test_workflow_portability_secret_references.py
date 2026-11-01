"""Credential references stay portable without exporting credential material."""

from __future__ import annotations

from copy import deepcopy

import pytest
import yaml

from core.application.workflows.workflow_portability import WorkflowPortabilityService
from core.application.workflows.workflow_portability_common import _redact_sensitive
from core.infrastructure.security.secret_reference import SecretReference
from tests._workflow_definitions import linear_workflow
from tests.unit.test_workflow_portability import _PortabilityDB

REFERENCE = "{{ state.inputs.provider_credential }}"
LITERAL = "fixture-credential-value-never-export"


@pytest.mark.parametrize(
    "value",
    [
        REFERENCE,
        {"source": "state.inputs.provider_credential"},
        "Bearer " + REFERENCE,
        "Token " + REFERENCE,
        {"source": "state.credentials.entries[0].value"},
        "{{ state.inputs['provider-key'] }}",
        "{{ state.credentials['provider.key'] }}",
        {"source": 'state.inputs["provider key"]'},
    ],
)
@pytest.mark.parametrize("key", ["api_key", "x-api-key", "Authorization", "password"])
def test_preserves_pure_portable_references(key: str, value: object) -> None:
    original = {key: deepcopy(value)}
    assert _redact_sensitive(original) == original
    assert _redact_sensitive(_redact_sensitive(original)) == original


@pytest.mark.parametrize(
    "key",
    [
        "api_key",
        "x-api-key",
        "X-Api-Key",
        "apiKey",
        "apikey",
        "X-APIKey",
        "Authorization",
        "Proxy-Authorization",
        "Cookie",
        "Set-Cookie",
        "password",
        "client_secret",
    ],
)
@pytest.mark.parametrize("value", [LITERAL, "Bearer " + LITERAL, {"nested": LITERAL}, [LITERAL]])
def test_masks_literal_credentials(key: str, value: object) -> None:
    exported = _redact_sensitive({key: value})
    assert exported[key] == "[redacted]"
    assert LITERAL not in yaml.safe_dump(exported)


@pytest.mark.parametrize(
    "value",
    [
        LITERAL + REFERENCE,
        REFERENCE + LITERAL,
        "{{ state.inputs.key | default: '" + LITERAL + "' }}",
        "{{ state.inputs.key or '" + LITERAL + "' }}",
        "{{ '" + LITERAL + "' }}",
        {"source": "state.inputs.key", "default": LITERAL},
        {"source": "state.inputs.key | default: '" + LITERAL + "'"},
        "{{ state }}",
        "{{ state.inputs.key..invalid }}",
    ],
)
def test_does_not_mistake_mixed_literals_for_references(value: object) -> None:
    assert _redact_sensitive({"api_key": value}) == {"api_key": "[redacted]"}


@pytest.mark.parametrize(
    "reference",
    [
        SecretReference(name="credential", ref_key="fixture-local-secret-record"),
        SecretReference(name="credential", ciphertext="fixture-encrypted-material"),
        SecretReference(name="credential", ref_key="fixture-local-secret-record").to_wire(),
        SecretReference(name="credential", ciphertext="fixture-encrypted-material").to_wire(),
    ],
)
def test_runtime_secret_records_do_not_travel_even_under_ordinary_keys(reference: object) -> None:
    assert _redact_sensitive({"default": reference}) == {"default": "[redacted]"}


def test_local_defaults_stay_excluded() -> None:
    assert _redact_sensitive(
        {
            "name": "credential",
            "type": "secret",
            "default_secret_ref": LITERAL,
            "sample_file": {"artifact_id": LITERAL},
        }
    ) == {"name": "credential", "type": "secret"}


@pytest.mark.parametrize("placement", ["query", "header", "body"])
def test_public_export_import_preserves_request_mapping_without_secret_values(
    placement: str,
) -> None:
    config = {
        "url": "https://example.test/search",
        "method": "POST",
        "response_mode": "inline",
        "response_target": "state.response",
        "headers": {},
        "query_params": {},
        "body_template": {},
    }
    key, container = {
        "query": ("api_key", "query_params"),
        "header": ("x-api-key", "headers"),
        "body": ("client_secret", "body_template"),
    }[placement]
    config[container][key] = REFERENCE
    config["headers"]["Authorization"] = "Bearer " + LITERAL
    workflow = linear_workflow(
        {"step_id": "search", "step_kind": "http_request", "override": {"http_request": config}},
        workflow_id="portable_search",
        display_name="Search a provider",
        description="Search a provider with an opaque workflow credential input.",
        settings={
            "callable": {
                "enabled": True,
                "trigger_channels": ["parent_workflow"],
                "expected_inputs": [
                    {
                        "name": "provider_credential",
                        "type": "secret",
                        "required": True,
                        "default_secret_ref": "fixture-local-secret-record",
                    }
                ],
            }
        },
    )
    original = deepcopy(workflow)
    service = WorkflowPortabilityService(_PortabilityDB(workflows={"portable_search": workflow}))
    exported = service.export_yaml("portable_search")
    parsed = service.parse_bundle(exported)
    actual = parsed["workflows"][0]["steps"][0]["override"]["http_request"]
    assert actual[container][key] == REFERENCE
    assert actual["headers"]["Authorization"] == "[redacted]"
    assert LITERAL not in exported
    assert "fixture-local-secret-record" not in exported
    assert workflow == original


@pytest.mark.parametrize(
    "header", ["X-Authorization", "x-authorization", "X-AUTHORIZATION", "x_authorization"]
)
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (LITERAL, "[redacted]"),
        ("Bearer " + LITERAL, "[redacted]"),
        (REFERENCE, REFERENCE),
        ("Bearer " + REFERENCE, "Bearer " + REFERENCE),
        (
            {"source": "state.inputs.provider_credential"},
            {"source": "state.inputs.provider_credential"},
        ),
        (
            SecretReference(name="credential", ref_key="fixture-opaque-auth-record").to_wire(),
            "[redacted]",
        ),
    ],
)
def test_public_export_handles_prefixed_authorization_headers(header, value, expected):
    workflow = linear_workflow(
        {
            "step_id": "request",
            "step_kind": "http_request",
            "override": {
                "http_request": {
                    "url": "https://example.test",
                    "method": "GET",
                    "response_mode": "inline",
                    "response_target": "state.response",
                    "headers": {header: deepcopy(value), "Accept": "application/json"},
                }
            },
        },
        workflow_id="authorization_headers",
    )
    original = deepcopy(workflow)
    service = WorkflowPortabilityService(
        _PortabilityDB(workflows={"authorization_headers": workflow})
    )
    exported = service.export_yaml("authorization_headers")
    headers = service.parse_bundle(exported)["workflows"][0]["steps"][0]["override"][
        "http_request"
    ]["headers"]
    assert headers[header] == expected
    assert headers["Accept"] == "application/json"
    assert LITERAL not in exported
    assert "fixture-opaque-auth-record" not in exported
    assert workflow == original
