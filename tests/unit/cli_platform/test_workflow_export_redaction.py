"""A CLI workflow export keeps its prompts and templates intact."""

from __future__ import annotations

import pytest

from core.application.management.command_specs.workflow_authoring import WorkflowExportResult
from core.application.security.credential_redaction import RedactionContext, redact_output
from core.application.workflows.workflow_portability_common import (
    _redact_sensitive,
    _sensitive_key,
)
from core.entrypoints.cli.platform.execution import _public_result_payload

_TEMPLATE = "{% for token in lower.split() %}{{ token }}{% endfor %}"
_PROSE = "the token is required; a password and a secret are words here"


def _export(bundle_yaml: str) -> WorkflowExportResult:
    return WorkflowExportResult(
        workflow_id="wf", filename="wf.workflow.yaml", bundle_yaml=bundle_yaml
    )


def test_cli_export_keeps_template_and_prose_words_after_token() -> None:
    bundle = f"steps:\n  - template: '{_TEMPLATE}'\n    prompt: {_PROSE}\n"

    result = _public_result_payload(
        _export(bundle), context=RedactionContext(), secret_fields=frozenset()
    )

    assert result["bundle_yaml"] == bundle


def test_cli_export_still_removes_real_secret_values() -> None:
    opaque = "ghp_" + "a" * 30  # pragma: allowlist secret
    bundle = (
        "url: postgresql://reader:hunter2@db/orders\n"  # pragma: allowlist secret
        f"note: {opaque}\n"
        "headers:\n  Authorization: Bearer abc.def\n"
        "known: s3cr3t-value\n"
    )

    result = _public_result_payload(
        _export(bundle),
        context=RedactionContext(known_secrets=("s3cr3t-value",)),
        secret_fields=frozenset(),
    )["bundle_yaml"]

    assert "hunter2" not in result
    assert opaque not in result
    assert "abc.def" not in result
    assert "s3cr3t-value" not in result


def test_document_exemption_is_declared_by_the_result_model_not_by_field_name() -> None:
    payload = {"bundle_yaml": f"prompt: {_PROSE}"}

    assert redact_output(payload) != payload
    assert (
        redact_output(payload, server_redacted_document_paths=frozenset({("bundle_yaml",)}))
        == payload
    )


@pytest.mark.parametrize(
    "key",
    ["max_tokens", "maxTokens", "prompt_tokens", "tokens_total", "token_limit", "temperature"],
)
def test_export_keeps_non_secret_token_settings(key: str) -> None:
    assert _sensitive_key(key) is False
    assert _redact_sensitive({"config": {key: 4000}}) == {"config": {key: 4000}}


@pytest.mark.parametrize(
    "key",
    [
        "token",
        "tokens",
        "access_token",
        "accessToken",
        "api_key",
        "x-api-key",
        "apiKey",
        "password",
        "db_password",
        "secrets",
        "client_secret",
        "credentials",
        "bearer",
        # Run-together and all-caps keys: matched as substrings, never as whole words.
        "accesstoken",
        "authtoken",
        "dbpassword",
        "SMTPPassword",
        "clientsecret",
        "api_keys",
        "max_tokens_secret",
    ],
)
def test_export_still_masks_secret_keys(key: str) -> None:
    assert _sensitive_key(key) is True
    assert _redact_sensitive({key: "value"}) == {key: "[redacted]"}


def test_cli_export_scrubs_assigned_and_quoted_credentials_but_not_counts() -> None:
    bundle = (
        "prompt: connect with password=hunter2 please\n"  # pragma: allowlist secret
        'payload: \'{"api_key": "abc123"}\'\n'  # pragma: allowlist secret
        "config:\n  max_tokens: 4000\n"
        "schema:\n  properties:\n    password:\n      type: string\n"
    )

    result = _public_result_payload(
        _export(bundle), context=RedactionContext(), secret_fields=frozenset()
    )["bundle_yaml"]

    assert "hunter2" not in result
    assert "abc123" not in result
    assert "max_tokens: 4000" in result
    assert "    password:\n      type: string\n" in result
