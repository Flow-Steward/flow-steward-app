"""SEC-004: Tests for the secret scanner engine.

Run:
    pytest tests/unit/test_secret_scanner.py -v
"""

from __future__ import annotations

import hashlib

import pytest

from core.infrastructure.security.policy import ScannerConfig
from core.infrastructure.security.scanners.secrets import SecretScanner


@pytest.fixture
def scanner():
    return SecretScanner()


@pytest.fixture
def config():
    return ScannerConfig()


class TestTruePositives:
    """Known secrets that MUST be detected."""

    def test_aws_access_key(self, scanner, config):
        text = "key = AKIAIOSFODNN7EXAMPLE"
        findings = scanner.scan(text, "inbound", config)
        assert any(f.rule_id == "secret:aws_access_key" for f in findings)

    def test_github_pat(self, scanner, config):
        text = "token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"
        findings = scanner.scan(text, "inbound", config)
        assert any(f.rule_id == "secret:github_pat" for f in findings)

    def test_github_fine_grained(self, scanner, config):
        text = "github_pat_11ABCDEFG0abcdefghijkl_AbCdEfGhIjKlMnOpQrStUvWxYz"
        findings = scanner.scan(text, "inbound", config)
        assert any(f.rule_id == "secret:github_fine_grained" for f in findings)

    def test_gitlab_pat(self, scanner, config):
        text = "token = glpat-ABCDEFGHIJKLMNOPQRSTuv"
        findings = scanner.scan(text, "inbound", config)
        assert any(f.rule_id == "secret:gitlab_pat" for f in findings)

    def test_slack_bot_token(self, scanner, config):
        text = "xoxb-1234567890-9876543210-AbCdEfGhIjKlMnOpQrStUvWx"  # pragma: allowlist secret
        findings = scanner.scan(text, "inbound", config)
        assert any(f.rule_id == "secret:slack_bot_token" for f in findings)

    def test_stripe_secret(self, scanner, config):
        text = "sk_live_ABCDEFGHIJKLMNOPQRSTUVWXYZab"  # pragma: allowlist secret
        findings = scanner.scan(text, "inbound", config)
        assert any(f.rule_id == "secret:stripe_secret" for f in findings)

    def test_sendgrid_key(self, scanner, config):
        text = "SG.abcdefghijklmnopqrstuv.abcdefghijklmnopqrstuvwxyz"
        findings = scanner.scan(text, "inbound", config)
        assert any(f.rule_id == "secret:sendgrid_key" for f in findings)

    def test_gcp_api_key(self, scanner, config):
        text = "AIzaSyA-abcdefghijklmnopqrstuvwxyz01234"
        findings = scanner.scan(text, "inbound", config)
        assert any(f.rule_id == "secret:gcp_api_key" for f in findings)

    def test_jwt_token(self, scanner, config):
        text = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        findings = scanner.scan(text, "inbound", config)
        assert any(f.rule_id == "secret:jwt_token" for f in findings)

    def test_pem_private_key(self, scanner, config):
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA0Z3VS5JJcds3xfn/ygWyF4PBl3Gv+V8bVl\n-----END RSA PRIVATE KEY-----"
        findings = scanner.scan(text, "inbound", config)
        assert any(f.rule_id == "secret:pem_private_key" for f in findings)

    def test_db_uri_postgres(self, scanner, config):
        text = "postgres://user:pass@host:5432/mydb"
        findings = scanner.scan(text, "inbound", config)
        assert any(f.rule_id == "secret:db_uri" for f in findings)

    def test_db_uri_mongodb(self, scanner, config):
        text = "mongodb+srv://admin:password@cluster.mongodb.net/db"
        findings = scanner.scan(text, "inbound", config)
        assert any(f.rule_id == "secret:db_uri" for f in findings)

    def test_generic_api_key(self, scanner, config):
        text = 'api_key = "AbCdEfGhIjKlMnOpQrStUvWx1234"'
        findings = scanner.scan(text, "inbound", config)
        assert any(f.rule_id == "secret:generic_api_key" for f in findings)

    def test_azure_connection_string(self, scanner, config):
        text = "DefaultEndpointsProtocol=https;AccountName=myaccount;AccountKey=abc123def456ghi789+/=;EndpointSuffix=core.windows.net"
        findings = scanner.scan(text, "inbound", config)
        assert any(f.rule_id == "secret:azure_connection_string" for f in findings)


class TestFalsePositives:
    """Safe text that should NOT trigger detection."""

    def test_normal_text(self, scanner, config):
        text = "This is a normal project description with no secrets."
        findings = scanner.scan(text, "inbound", config)
        assert len(findings) == 0

    def test_code_variable_names(self, scanner, config):
        text = "const apiKey = getApiKey(); // fetch from vault"
        findings = scanner.scan(text, "inbound", config)
        # May match generic_api_key, but shouldn't have high-severity
        critical = [f for f in findings if f.severity == "critical"]
        assert len(critical) == 0

    def test_placeholder_key(self, scanner, config):
        text = "AKIA_PLACEHOLDER_KEY_HERE"
        findings = scanner.scan(text, "inbound", config)
        # Should not match the exact AKIA pattern (wrong format)
        aws = [f for f in findings if f.rule_id == "secret:aws_access_key"]
        assert len(aws) == 0

    def test_empty_text(self, scanner, config):
        assert scanner.scan("", "inbound", config) == []


class TestRedaction:
    def test_redact_replaces_secrets(self, scanner, config):
        text = "key=AKIAIOSFODNN7EXAMPLE rest"
        findings = scanner.scan(text, "inbound", config)
        redacted = scanner.redact(text, findings)
        assert "AKIAIOSFODNN7EXAMPLE" not in redacted
        assert "[SECRET:" in redacted

    def test_redact_preserves_safe_text(self, scanner, config):
        text = "just normal text"
        redacted = scanner.redact(text, [])
        assert redacted == text


class TestAllowlist:
    def test_allowlisted_hash_skipped(self, scanner, config):
        key = "AKIAIOSFODNN7EXAMPLE"
        h = hashlib.sha256(key.encode()).hexdigest()
        findings = scanner.scan(f"key={key}", "inbound", config, allowlist_hashes={h})
        aws = [f for f in findings if f.rule_id == "secret:aws_access_key"]
        assert len(aws) == 0


class TestSeverityThreshold:
    def test_high_threshold_skips_medium(self, scanner):
        config = ScannerConfig(severity_threshold="high")
        text = 'api_key = "AbCdEfGhIjKlMnOpQrStUvWx1234"'
        findings = scanner.scan(text, "inbound", config)
        medium = [f for f in findings if f.severity == "medium"]
        assert len(medium) == 0

    def test_disabled_scanner(self, scanner):
        config = ScannerConfig(enabled=False)
        text = "AKIAIOSFODNN7EXAMPLE"
        assert scanner.scan(text, "inbound", config) == []
