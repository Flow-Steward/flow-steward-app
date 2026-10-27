"""SEC-002: Tests for security policy schema, loader, and validation.

Run:
    pytest tests/unit/test_security_policy.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from core.application.security import policy_service
from core.infrastructure.db.system_settings_store import SECURITY_POLICY_SETTINGS_KEY
from core.infrastructure.security.policy import (
    CustomRule,
    PIIConfig,
    SecurityPolicy,
    SurfaceConfig,
    SurfaceSettings,
)
from core.infrastructure.security.policy_loader import (
    _deep_merge,
    load_default_policy,
    load_global_policy,
    load_global_policy_override,
    load_project_policy_context,
    load_project_policy_override,
    merge_policies,
    validate_policy,
)


class TestSecurityPolicySchema:
    def test_default_construction(self):
        policy = SecurityPolicy()
        assert policy.level == "standard"
        assert policy.scanners.secrets.enabled is True
        assert policy.scanners.injection.contexts == ["prompt", "display", "shell"]
        assert policy.scanners.pii.mode == "pseudonymize"
        assert policy.surfaces.inbound.action == "scan"
        assert policy.custom_rules == []

    def test_minimal_level(self):
        policy = SecurityPolicy(level="minimal")
        assert policy.level == "minimal"

    def test_strict_level(self):
        policy = SecurityPolicy(level="strict")
        assert policy.level == "strict"

    def test_invalid_level_rejected(self):
        with pytest.raises(ValidationError):
            SecurityPolicy(level="invalid")

    def test_custom_rules(self):
        rule = CustomRule(
            rule_id="custom:test",
            scanner="secrets",
            pattern=r"MY_SECRET_\w+",
            severity="high",
            description="Test rule",
        )
        policy = SecurityPolicy(custom_rules=[rule])
        assert len(policy.custom_rules) == 1
        assert policy.custom_rules[0].rule_id == "custom:test"

    def test_pii_modes(self):
        for mode in ("pseudonymize", "hard_redact", "allow"):
            cfg = PIIConfig(mode=mode)
            assert cfg.mode == mode

    def test_pii_detector_toggles_default_enabled(self):
        cfg = PIIConfig()
        assert cfg.detectors.person_name.enabled is True
        assert cfg.detectors.email.enabled is True
        assert cfg.detectors.phone.enabled is True
        assert cfg.detectors.ssn.enabled is True

    def test_invalid_pii_mode_rejected(self):
        with pytest.raises(ValidationError):
            PIIConfig(mode="invalid")

    def test_surface_skip(self):
        policy = SecurityPolicy(
            surfaces=SurfaceSettings(
                inbound=SurfaceConfig(action="skip"),
                tool=SurfaceConfig(action="scan"),
            )
        )
        assert policy.surfaces.inbound.action == "skip"
        assert policy.surfaces.tool.action == "scan"

    def test_serialization_round_trip(self):
        policy = SecurityPolicy(
            level="strict",
            custom_rules=[CustomRule(rule_id="r1", scanner="pii", pattern=r"\d{3}-\d{2}-\d{4}")],
        )
        dumped = policy.model_dump(mode="json")
        restored = SecurityPolicy(**dumped)
        assert restored.level == "strict"
        assert len(restored.custom_rules) == 1
        assert restored.custom_rules[0].rule_id == "r1"


class TestPolicyLoader:
    def test_load_default(self):
        policy = load_default_policy()
        assert isinstance(policy, SecurityPolicy)
        assert policy.level == "standard"

    def test_load_default_cached_copy(self):
        p1 = load_default_policy()
        p2 = load_default_policy()
        assert p1 is not p2  # deep copy, not same object

    def test_merge_project_override(self):
        base = SecurityPolicy()
        merged = merge_policies(base, {"scanners": {"pii": {"mode": "hard_redact"}}})
        assert merged.scanners.pii.mode == "hard_redact"
        assert merged.scanners.secrets.enabled is True  # unchanged

    def test_merge_project_override_pii_detector_toggle(self):
        base = SecurityPolicy()
        merged = merge_policies(
            base,
            {"scanners": {"pii": {"detectors": {"person_name": {"enabled": False}}}}},
        )
        assert merged.scanners.pii.detectors.person_name.enabled is False
        assert merged.scanners.pii.detectors.email.enabled is True

    def test_merge_level_minimal(self):
        base = SecurityPolicy()
        merged = merge_policies(base, {"level": "minimal"})
        assert merged.level == "minimal"
        assert merged.scanners.pii.enabled is False

    def test_merge_level_strict(self):
        base = SecurityPolicy()
        merged = merge_policies(base, {"level": "strict"})
        assert merged.level == "strict"
        assert merged.scanners.pii.mode == "hard_redact"

    def test_merge_none_override(self):
        base = SecurityPolicy()
        merged = merge_policies(base, None)
        assert merged.level == "standard"

    def test_load_global_policy_applies_db_override(self):
        with patch(
            "core.infrastructure.security.policy_loader.load_global_policy_override",
            return_value={"level": "strict"},
        ):
            policy = load_global_policy("account-b")
        assert policy.level == "strict"
        assert policy.scanners.pii.mode == "hard_redact"

    def test_load_global_policy_override_reads_db(self, monkeypatch):
        pool = MagicMock()
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = {
            "setting_value_jsonb": {"level": "minimal"},
            "setting_key": SECURITY_POLICY_SETTINGS_KEY,
        }
        conn.cursor.return_value.__enter__.return_value = cur
        conn.cursor.return_value.__exit__.return_value = False
        pool.getconn.return_value = conn
        monkeypatch.setenv("FS_DB_DSN", "postgres://test")

        with patch(
            "core.infrastructure.security.policy_loader._get_threaded_pool", return_value=pool
        ):
            payload = load_global_policy_override("account-b")

        assert payload == {"level": "minimal"}
        sql, params = cur.execute.call_args.args
        assert "FROM system_settings" in sql
        assert params[0] == "account-b"
        assert params[1] == SECURITY_POLICY_SETTINGS_KEY
        conn.rollback.assert_called_once()
        pool.putconn.assert_called_once_with(conn)

    def test_persist_global_policy_uses_system_settings(self):
        cur = MagicMock()
        cur.fetchone.return_value = {
            "setting_value_jsonb": {"level": "strict"},
            "setting_key": SECURITY_POLICY_SETTINGS_KEY,
        }
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        conn.cursor.return_value.__exit__.return_value = False
        bl = MagicMock(conn=conn)

        saved = policy_service.persist_global_policy(
            bl,
            {"level": "strict"},
            actor="operator@example.com",
            account_id="account-b",
        )

        assert saved is True
        sql, params = cur.execute.call_args.args
        assert "INSERT INTO system_settings" in sql
        assert params[0] == "account-b"
        assert params[1] == SECURITY_POLICY_SETTINGS_KEY
        assert params[3] == "operator@example.com"
        conn.commit.assert_called_once()

    def test_load_project_policy_override_reads_db(self, monkeypatch):
        pool = MagicMock()
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = ({"surfaces": {"inbound": {"action": "skip"}}},)
        conn.cursor.return_value.__enter__.return_value = cur
        conn.cursor.return_value.__exit__.return_value = False
        pool.getconn.return_value = conn
        monkeypatch.setenv("FS_DB_DSN", "postgres://test")

        with patch(
            "core.infrastructure.security.policy_loader._get_threaded_pool", return_value=pool
        ):
            payload = load_project_policy_override(7)

        assert payload == {"surfaces": {"inbound": {"action": "skip"}}}
        conn.rollback.assert_called_once()
        pool.putconn.assert_called_once_with(conn)

    def test_load_project_policy_context_returns_owner_and_override(self, monkeypatch):
        pool = MagicMock()
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = (
            "account-b",
            {"surfaces": {"inbound": {"action": "skip"}}},
        )
        conn.cursor.return_value.__enter__.return_value = cur
        conn.cursor.return_value.__exit__.return_value = False
        pool.getconn.return_value = conn
        monkeypatch.setenv("FS_DB_DSN", "postgres://test")

        with patch(
            "core.infrastructure.security.policy_loader._get_threaded_pool", return_value=pool
        ):
            account_id, payload = load_project_policy_context(7)

        assert account_id == "account-b"
        assert payload == {"surfaces": {"inbound": {"action": "skip"}}}
        sql, params = cur.execute.call_args.args
        assert "SELECT account_id, config->'security_policy'" in sql
        assert params == ("7",)
        conn.rollback.assert_called_once()
        pool.putconn.assert_called_once_with(conn)

    def test_load_project_policy_override_does_not_read_removed_meta_fallback(self, monkeypatch):
        pool = MagicMock()
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = (None,)
        conn.cursor.return_value.__enter__.return_value = cur
        conn.cursor.return_value.__exit__.return_value = False
        pool.getconn.return_value = conn
        monkeypatch.setenv("FS_DB_DSN", "postgres://test")

        with patch(
            "core.infrastructure.security.policy_loader._get_threaded_pool", return_value=pool
        ):
            payload = load_project_policy_override(11)

        assert payload is None
        cur.execute.assert_called_once()
        assert "SELECT config->'security_policy'" in cur.execute.call_args_list[0].args[0]
        assert "meta" not in cur.execute.call_args_list[0].args[0]
        conn.commit.assert_not_called()
        conn.rollback.assert_called_once()
        pool.putconn.assert_called_once_with(conn)


class TestDeepMerge:
    def test_simple(self):
        assert _deep_merge({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}

    def test_nested(self):
        result = _deep_merge(
            {"a": {"x": 1, "y": 2}},
            {"a": {"y": 3, "z": 4}},
        )
        assert result == {"a": {"x": 1, "y": 3, "z": 4}}


class TestValidatePolicy:
    def test_valid_policy(self):
        policy = SecurityPolicy()
        assert validate_policy(policy) == []

    def test_custom_rule_missing_pattern(self):
        policy = SecurityPolicy(
            custom_rules=[CustomRule(rule_id="bad", scanner="secrets", pattern="")]
        )
        errors = validate_policy(policy)
        assert any("missing pattern" in e for e in errors)

    def test_custom_rule_rejects_invalid_regular_expression(self):
        policy = SecurityPolicy(
            custom_rules=[CustomRule(rule_id="audit.invalid.regex", scanner="secrets", pattern="[")]
        )

        errors = validate_policy(policy)

        assert errors == [
            "Custom rule 'audit.invalid.regex' has invalid pattern: unterminated character set at position 0"
        ]
