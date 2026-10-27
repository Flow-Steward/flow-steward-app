"""SEC-006: Tests for the PII guard engine.

Run:
    pytest tests/unit/test_pii_guard.py -v
"""

from __future__ import annotations

import pytest

from core.infrastructure.security.policy import PIIConfig
from core.infrastructure.security.scanners.pii import PIIGuard, _luhn_valid


@pytest.fixture
def guard():
    return PIIGuard(project_id=42)


@pytest.fixture
def config():
    return PIIConfig()


class TestDetection:
    def test_email(self, guard, config):
        text = "Contact john.doe@example.com for details."
        findings = guard.scan(text, "inbound", config)
        assert any(f.rule_id == "pii:email" for f in findings)

    def test_escaped_newline_before_python_decorator_is_not_email(self, guard, config):
        text = r'previous line\n@pytest.mark.parametrize("value", [1, 2])'

        findings = guard.scan(text, "tool", config)

        assert all(f.rule_id != "pii:email" for f in findings)
        assert guard.redact(text, findings) == text

    def test_us_phone(self, guard, config):
        text = "Call 555-123-4567 now."
        findings = guard.scan(text, "inbound", config)
        assert any("phone" in f.rule_id for f in findings)

    def test_us_phone_without_separators_before_sentence_period(self, guard, config):
        text = "Call 5551234567."
        findings = guard.scan(text, "inbound", config)
        assert any(f.rule_id == "pii:phone_us" for f in findings)

    def test_decimal_numeric_literal_is_not_us_phone(self, guard, config):
        text = 'assert invoice_total_label(1000000.99) == "$1000000.99"'
        findings = guard.scan(text, "agent_message", config)
        assert all(f.rule_id != "pii:phone_us" for f in findings)

    def test_hex_correlation_id_is_not_us_phone(self, guard, config):
        text = "correlation_id=cda019ec1ad42d1b4c58187a04769f7"
        findings = guard.scan(text, "agent_message", config)
        assert all(f.rule_id != "pii:phone_us" for f in findings)

    def test_git_diff_zero_index_hash_is_not_us_phone(self, guard, config):
        text = "index 0000000..e69de29"
        findings = guard.scan(text, "agent_message", config)
        assert all(f.rule_id != "pii:phone_us" for f in findings)

    def test_hyphenated_audit_suffix_is_not_us_phone(self, guard, config):
        text = "audit-20260914-1234567"

        findings = guard.scan(text, "agent_message", config)

        assert all(f.rule_id != "pii:phone_us" for f in findings)

    def test_international_phone(self, guard, config):
        text = "Reach us at +44-2071234567."
        findings = guard.scan(text, "inbound", config)
        assert any("phone" in f.rule_id for f in findings)

    def test_ssn(self, guard, config):
        text = "SSN: 123-45-6789"
        findings = guard.scan(text, "inbound", config)
        assert any(f.rule_id == "pii:ssn" for f in findings)

    def test_credit_card_valid_luhn(self, guard, config):
        text = "Card: 4111-1111-1111-1111"
        findings = guard.scan(text, "inbound", config)
        assert any(f.rule_id == "pii:credit_card" for f in findings)

    def test_credit_card_invalid_luhn(self, guard, config):
        text = "Number: 1234-5678-9012-3456"
        findings = guard.scan(text, "inbound", config)
        cc = [f for f in findings if f.rule_id == "pii:credit_card"]
        assert len(cc) == 0  # fails Luhn check

    def test_uuid_is_not_a_credit_card(self, guard, config):
        text = "job_id=00000000-0000-0000-0000-000000000501"

        findings = guard.scan(text, "outbound", config)

        assert all(f.rule_id != "pii:credit_card" for f in findings)

    def test_ipv4(self, guard, config):
        text = "Server at 192.168.1.100"
        findings = guard.scan(text, "inbound", config)
        assert any(f.rule_id == "pii:ipv4" for f in findings)

    def test_loopback_ipv4_literal_is_not_pii(self, guard, config):
        text = 'server.listen(0, "127.0.0.1")'

        findings = guard.scan(text, "tool", config)

        assert all(f.rule_id != "pii:ipv4" for f in findings)


class TestPseudonymization:
    def test_deterministic(self, guard, config):
        text = "john@example.com"
        f1 = guard.scan(text, "inbound", config)
        f2 = guard.scan(text, "inbound", config)
        assert f1[0].redacted_preview == f2[0].redacted_preview

    def test_different_values_different_pseudonyms(self, guard, config):
        f1 = guard.scan("alice@example.com", "inbound", config)
        f2 = guard.scan("bob@example.com", "inbound", config)
        assert f1[0].redacted_preview != f2[0].redacted_preview

    def test_different_projects_different_pseudonyms(self, config):
        g1 = PIIGuard(project_id=1)
        g2 = PIIGuard(project_id=2)
        f1 = g1.scan("john@example.com", "inbound", config)
        f2 = g2.scan("john@example.com", "inbound", config)
        assert f1[0].redacted_preview != f2[0].redacted_preview

    def test_pseudonym_format(self, guard, config):
        findings = guard.scan("john@example.com", "inbound", config)
        preview = findings[0].redacted_preview
        assert preview.startswith("[PII:email:")
        assert preview.endswith("]")


class TestHardRedact:
    def test_hard_redact_mode(self, guard):
        config = PIIConfig(mode="hard_redact")
        findings = guard.scan("john@example.com", "inbound", config)
        assert findings[0].redacted_preview == "[PII:email:REDACTED]"


class TestAllowMode:
    def test_allow_mode_logs_but_no_redact(self, guard):
        config = PIIConfig(mode="allow")
        findings = guard.scan("john@example.com", "inbound", config)
        assert len(findings) > 0
        assert findings[0].action == "allow"
        assert findings[0].redacted_preview == "john@example.com"


class TestRedaction:
    def test_redact_replaces_pii(self, guard, config):
        text = "Email: john@example.com and SSN: 123-45-6789"
        findings = guard.scan(text, "inbound", config)
        redacted = guard.redact(text, findings)
        assert "john@example.com" not in redacted
        assert "123-45-6789" not in redacted
        assert "[PII:" in redacted


class TestLuhn:
    def test_valid_visa(self):
        assert _luhn_valid("4111111111111111") is True

    def test_valid_mastercard(self):
        assert _luhn_valid("5500000000000004") is True

    def test_invalid(self):
        assert _luhn_valid("1234567890123456") is False

    def test_too_short(self):
        assert _luhn_valid("123") is False


class TestPersonNameDetection:
    def test_name_with_label(self, guard, config):
        findings = guard.scan("Name: John Smith", "inbound", config)
        name_findings = [f for f in findings if f.rule_id == "pii:person_name"]
        assert len(name_findings) >= 1

    def test_name_with_title_prefix(self, guard, config):
        findings = guard.scan("Contact Dr. Jane Wilson for details", "inbound", config)
        name_findings = [f for f in findings if f.rule_id == "pii:person_name"]
        assert len(name_findings) >= 1

    def test_no_false_positive_on_plain_words(self, guard, config):
        findings = guard.scan("The project status is good", "inbound", config)
        name_findings = [f for f in findings if f.rule_id == "pii:person_name"]
        assert len(name_findings) == 0

    def test_name_redacted(self, guard, config):
        findings = guard.scan("Author: Alice Johnson", "inbound", config)
        name_findings = [f for f in findings if f.rule_id == "pii:person_name"]
        if name_findings:
            result = guard.redact("Author: Alice Johnson", name_findings)
            assert "Alice Johnson" not in result

    def test_false_positive_common_words(self, guard, config):
        findings = guard.scan("Name: Test User", "inbound", config)
        name_findings = [f for f in findings if f.rule_id == "pii:person_name"]
        assert len(name_findings) == 0  # "Test User" are both in false positives


class TestGranularDetectors:
    def test_person_name_disabled(self, guard):
        config = PIIConfig(detectors={"person_name": {"enabled": False}})
        findings = guard.scan("Name: John Smith", "inbound", config)
        assert all(f.rule_id != "pii:person_name" for f in findings)

    def test_person_name_enabled(self, guard):
        config = PIIConfig(detectors={"person_name": {"enabled": True}})
        findings = guard.scan("Name: John Smith", "inbound", config)
        assert any(f.rule_id == "pii:person_name" for f in findings)

    def test_email_detector_disabled(self, guard):
        config = PIIConfig(detectors={"email": {"enabled": False}})
        findings = guard.scan("Contact john@example.com", "inbound", config)
        assert all(f.rule_id != "pii:email" for f in findings)

    def test_phone_detector_disabled_blocks_us_and_international(self, guard):
        config = PIIConfig(detectors={"phone": {"enabled": False}})
        findings = guard.scan("Call 555-123-4567 or +44-2071234567", "inbound", config)
        assert all("phone" not in f.rule_id for f in findings)


class TestEdgeCases:
    def test_empty_text(self, guard, config):
        assert guard.scan("", "inbound", config) == []

    def test_disabled(self, guard):
        config = PIIConfig(enabled=False)
        assert guard.scan("john@example.com", "inbound", config) == []
