"""SEC-013: Tests for the security event logger.

Run:
    pytest tests/unit/test_security_event_logger.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock

from core.infrastructure.security.event_logger import SecurityEventLogger, _safe_json
from core.infrastructure.security.models import ScanFinding, ScanReport


def _make_finding(**overrides) -> ScanFinding:
    defaults = {
        "surface": "inbound",
        "severity": "high",
        "rule_id": "secret:aws_access_key",
        "action": "redact",
        "redacted_preview": "[SECRET:aws_access_key:REDACTED]",
        "scanner": "secrets",
    }
    defaults.update(overrides)
    return ScanFinding(**defaults)


class TestSecurityEventLogger:
    def test_log_scan_event_no_pool_returns_none(self):
        logger = SecurityEventLogger("fake://dsn")
        logger._pool = None
        result = logger.log_scan_event(
            findings=[_make_finding()],
            surface="inbound",
        )
        # No pool means no connection, returns None
        assert result is None

    def test_log_scan_event_empty_findings(self):
        logger = SecurityEventLogger("fake://dsn")
        result = logger.log_scan_event(findings=[], surface="inbound")
        assert result is None

    def test_log_scan_event_with_mocked_pool(self):
        logger = SecurityEventLogger("fake://dsn")
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.side_effect = [("acc-alpha",), (42,)]
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        mock_pool = MagicMock()
        mock_pool.getconn.return_value = mock_conn
        logger._pool = mock_pool

        report = ScanReport(surface="inbound", scan_id="test-123", policy_level="standard")
        result = logger.log_scan_event(
            findings=[_make_finding(), _make_finding(severity="medium", rule_id="pii:email")],
            surface="inbound",
            project_id=1,
            scan_report=report,
        )
        assert result == 42
        assert mock_cursor.execute.call_count == 4  # account lookup + 1 event + 2 findings
        event_params = mock_cursor.execute.call_args_list[1].args[1]
        assert event_params[0] == "acc-alpha"
        mock_conn.commit.assert_called_once()

    def test_log_scan_event_without_account_or_project_does_not_write(self):
        logger = SecurityEventLogger("fake://dsn")
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        mock_pool = MagicMock()
        mock_pool.getconn.return_value = mock_conn
        logger._pool = mock_pool

        result = logger.log_scan_event(
            findings=[_make_finding()],
            surface="inbound",
            account_id="default",
        )

        assert result is None
        mock_cursor.execute.assert_not_called()
        mock_conn.commit.assert_not_called()

    def test_list_events_no_pool(self):
        logger = SecurityEventLogger("fake://dsn")
        assert logger.list_events() == []

    def test_list_events_includes_job_id_filter(self):
        logger = SecurityEventLogger("fake://dsn")
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.description = [("id",), ("job_id",), ("created_at",)]
        mock_cursor.fetchall.return_value = []
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        mock_pool = MagicMock()
        mock_pool.getconn.return_value = mock_conn
        logger._pool = mock_pool

        logger.list_events(account_id="default", job_id=77)
        sql = str(mock_cursor.execute.call_args[0][0] or "")
        params = list(mock_cursor.execute.call_args[0][1] or [])
        assert "job_id = %s" in sql
        assert 77 in params

    def test_log_policy_resolution_error_writes_security_event(self):
        logger = SecurityEventLogger("fake://dsn")
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.side_effect = [("acc-beta",), (91,)]
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        mock_pool = MagicMock()
        mock_pool.getconn.return_value = mock_conn
        logger._pool = mock_pool

        event_id = logger.log_policy_resolution_error(
            project_id=3,
            job_id=12,
            surface="inbound",
            mode="monitor",
            message="security policy resolution failed",
        )

        assert event_id == 91
        params = mock_cursor.execute.call_args_list[1].args[1]
        assert params[0] == "acc-beta"
        assert params[2] == 12  # job_id
        assert params[4] == "policy:resolution_error"
        mock_conn.commit.assert_called_once()

    def test_list_findings_no_pool(self):
        logger = SecurityEventLogger("fake://dsn")
        assert logger.list_findings() == []

    def test_count_findings_no_pool(self):
        logger = SecurityEventLogger("fake://dsn")
        assert logger.count_findings_by_severity(1) == {}

    def test_get_latest_posture_snapshot_returns_latest_row(self):
        logger = SecurityEventLogger("fake://dsn")
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (91, "secure", {"secrets": 92}, {"high": 1}, "now")
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        mock_pool = MagicMock()
        mock_pool.getconn.return_value = mock_conn
        logger._pool = mock_pool

        snapshot = logger.get_latest_posture_snapshot(project_id=7)

        assert snapshot == {
            "score": 91,
            "level": "secure",
            "category_scores": {"secrets": 92},
            "finding_counts": {"high": 1},
            "created_at": "now",
        }
        sql = str(mock_cursor.execute.call_args[0][0] or "")
        params = mock_cursor.execute.call_args[0][1]
        assert "WHERE project_id = %s" in sql
        assert params == (7,)

    def test_get_latest_posture_snapshot_scopes_global_query_by_account(self):
        logger = SecurityEventLogger("fake://dsn")
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (88, "secure", {}, {}, "now")
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        mock_pool = MagicMock()
        mock_pool.getconn.return_value = mock_conn
        logger._pool = mock_pool

        snapshot = logger.get_latest_posture_snapshot(account_id="acc-a")

        assert snapshot is not None
        sql = str(mock_cursor.execute.call_args[0][0] or "")
        params = mock_cursor.execute.call_args[0][1]
        assert "WHERE account_id = %s" in sql
        assert params == ("acc-a",)

    def test_save_posture_snapshot_persists_and_commits(self):
        logger = SecurityEventLogger("fake://dsn")
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = ("acc-gamma",)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        mock_pool = MagicMock()
        mock_pool.getconn.return_value = mock_conn
        logger._pool = mock_pool

        saved = logger.save_posture_snapshot(
            project_id=3,
            score=88,
            level="secure",
            category_scores={"pii": 90},
            finding_counts={"medium": 2},
        )

        assert saved is True
        params = mock_cursor.execute.call_args_list[1].args[1]
        assert params[0] == "acc-gamma"
        assert params[1] == 3
        assert params[2] == 88
        assert params[3] == "secure"
        mock_conn.commit.assert_called_once()

    def test_save_posture_snapshot_without_account_context_does_not_write(self):
        logger = SecurityEventLogger("fake://dsn")
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        mock_pool = MagicMock()
        mock_pool.getconn.return_value = mock_conn
        logger._pool = mock_pool

        saved = logger.save_posture_snapshot(
            project_id=3,
            score=88,
            level="secure",
            category_scores={"pii": 90},
            finding_counts={"medium": 2},
            account_id="default",
        )

        assert saved is False
        assert mock_cursor.execute.call_count == 1
        mock_conn.commit.assert_not_called()


class TestSafeJson:
    def test_dict(self):
        assert _safe_json({"key": "value"}) == '{"key": "value"}'

    def test_non_serializable(self):
        assert _safe_json(object()) is None

    def test_none(self):
        assert _safe_json(None) == "null"


class TestRedactionAssertions:
    """Verify that raw secrets/PII are never stored."""

    def test_finding_preview_is_redacted(self):
        f = _make_finding(redacted_preview="[SECRET:aws:REDACTED]")
        assert "AKIA" not in f.redacted_preview
        assert "REDACTED" in f.redacted_preview

    def test_pii_preview_is_redacted(self):
        f = _make_finding(
            scanner="pii",
            rule_id="pii:email",
            redacted_preview="[PII:email:REDACTED]",
        )
        assert "@" not in f.redacted_preview
