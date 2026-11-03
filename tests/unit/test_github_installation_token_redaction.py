"""Opaque GitHub installation credentials must never leave a visible suffix."""

from __future__ import annotations

import hashlib

import pytest
from core.application.jobs.public_secret_redaction import redact_public_secrets
from core.infrastructure.security.policy import ScannerConfig

from core.application.security.credential_redaction import (
    RedactionContext,
    redact_document_text,
    redact_output,
    redact_text,
)
from core.infrastructure.security.prompt_safety import redact_diagnostic_identifier_for_persistence
from core.infrastructure.security.scanners.secrets import SecretScanner

# Synthetic strings, never issued credentials. The payload and signature contain
# every newly allowed character; final '-'/'_' must not escape a word boundary.
_HEADER = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9"
_STATELESS = "ghs_" + "123456_" + _HEADER + "." + ("Ab9_" * 80) + "." + ("Cd7-" * 40)
_TOKENS = [
    pytest.param("ghs_" + "Ab7" * 12, id="legacy"),
    pytest.param(_STATELESS + "-", id="stateless-final-hyphen"),
    pytest.param(_STATELESS + "_", id="stateless-final-underscore"),
    pytest.param(_STATELESS + ("Ef8_" * 200), id="stateless-long"),
    pytest.param(
        "ghs_123456_header-" + _HEADER + ".eyJ" + ("Ab9_" * 80) + "." + ("Cd7-" * 40),
        id="jwt-looking-component",
    ),
]


@pytest.mark.parametrize("credential", _TOKENS)
def test_installation_token_is_fully_masked_in_public_and_cli_text(credential: str) -> None:
    text = f"before ({credential}). after"

    assert {
        "public": redact_public_secrets(text),
        "cli": redact_text(text),
        "document": redact_document_text(text),
        "structured": redact_output({"message": text}),
    } == {
        "public": "before ([SECRET:github_token:REDACTED]). after",
        "cli": "before ([REDACTED]). after",
        "document": "before ([REDACTED]). after",
        "structured": {"message": "before ([REDACTED]). after"},
    }


@pytest.mark.parametrize("credential", _TOKENS)
def test_installation_token_scanner_matches_and_allowlists_the_entire_value(
    credential: str,
) -> None:
    scanner = SecretScanner()
    text = f"before ({credential}). after"
    findings = scanner.scan(text, "inbound", ScannerConfig())

    assert [(item.rule_id, item.context["match_length"]) for item in findings] == [
        ("secret:github_token", len(credential))
    ]
    assert scanner.redact(text, findings) == "before ([SECRET:github_token:REDACTED]). after"
    assert (
        scanner.scan(
            text,
            "inbound",
            ScannerConfig(),
            allowlist_hashes={hashlib.sha256(credential.encode()).hexdigest()},
        )
        == []
    )


@pytest.mark.parametrize("credential", _TOKENS)
def test_installation_token_is_not_preserved_as_a_diagnostic_identifier(credential: str) -> None:
    assert redact_diagnostic_identifier_for_persistence("request_id", credential) == (
        "[REDACTED_DIAGNOSTIC_ID]"
    )


@pytest.mark.parametrize("text", ["ghs_reference", "req_abc-123", "plain message"])
def test_noncredential_text_remains_intact(text: str) -> None:
    assert redact_public_secrets(text) == text
    assert redact_text(text) == text
    assert redact_document_text(text) == text


def test_safe_diagnostic_identifier_is_preserved() -> None:
    assert (
        redact_diagnostic_identifier_for_persistence("request_id", "req_abc-123") == "req_abc-123"
    )


@pytest.mark.parametrize("credential", _TOKENS)
def test_prefixed_installation_tokens_are_not_hidden_from_redaction(credential: str) -> None:
    text = "req_" + credential
    assert {
        "public": redact_public_secrets(text),
        "cli": redact_text(text),
        "document": redact_document_text(text),
        "diagnostic_id": redact_diagnostic_identifier_for_persistence("request_id", text),
    } == {
        "public": "req_[SECRET:github_token:REDACTED]",
        "cli": "req_[REDACTED]",
        "document": "req_[REDACTED]",
        "diagnostic_id": "[REDACTED_DIAGNOSTIC_ID]",
    }


@pytest.mark.parametrize("redactor", [redact_text, redact_document_text])
@pytest.mark.parametrize("relationship", ["superset", "overlap", "substring"])
def test_known_values_and_installation_tokens_are_masked_without_splitting_either(
    redactor, relationship: str
) -> None:
    body = "Ab7" * 12
    credential = "ghs_" + body
    suffix = "+" + "Cd8" * 12
    if relationship == "superset":
        text, known = credential + suffix, credential + suffix
    elif relationship == "overlap":
        text, known = credential + suffix, body + suffix
    else:
        text, known = credential, body
    assert redactor(text, context=RedactionContext(known_secrets=(known,))) == "[REDACTED]"
