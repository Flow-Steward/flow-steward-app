"""Regression coverage for CLAUDE-T-01 (remediation epic 2026-07-29).

An unreachable database DSN must turn DB-dependent tests into honest skips, not
into red results. The original defect was narrower than "no guard exists": the
guards used ``urlparse``, which reports ``hostname=None`` for libpq
keyword/value DSNs — the exact spelling ``.env`` ships — so the guard silently
never fired and every DB-dependent test died on ``psycopg.OperationalError``.

These tests pin the DSN parser against both libpq spellings and assert that the
shared harness guards actually skip.
"""

from __future__ import annotations

import pytest

from tests._db_env import dsn_host_port, dsn_unreachable_reason

UNRESOLVABLE_HOST = "pg-fs-does-not-resolve.invalid"


class TestDsnHostPort:
    def test_keyword_value_dsn_exposes_host_and_port(self):
        """The `.env` spelling must be parsed — this is the CLAUDE-T-01 root cause."""
        dsn = "dbname=flow_steward user=flow_steward password=secret host=pg-fs port=5432"

        assert dsn_host_port(dsn) == ("pg-fs", 5432)

    def test_keyword_value_dsn_defaults_missing_port(self):
        assert dsn_host_port("dbname=flow_steward host=pg-fs") == ("pg-fs", 5432)

    def test_keyword_value_dsn_tolerates_quoted_values(self):
        dsn = "dbname=flow_steward host='pg-fs' port='6432'"

        assert dsn_host_port(dsn) == ("pg-fs", 6432)

    def test_uri_dsn_exposes_host_and_port(self):
        dsn = "postgresql://flow_steward:secret@pg-fs:6432/flow_steward"  # pragma: allowlist secret

        assert dsn_host_port(dsn) == ("pg-fs", 6432)

    def test_uri_dsn_defaults_missing_port(self):
        assert dsn_host_port("postgresql://flow_steward@pg-fs/flow_steward") == ("pg-fs", 5432)

    @pytest.mark.parametrize(
        "dsn",
        [
            "",
            "   ",
            "dbname=flow_steward",
            "dbname=flow_steward host=/var/run/postgresql",
            "dbname=flow_steward host=@/var/run/postgresql",
        ],
    )
    def test_dsns_without_a_tcp_host_have_nothing_to_probe(self, dsn):
        assert dsn_host_port(dsn) is None

    def test_malformed_port_falls_back_to_the_default(self):
        assert dsn_host_port("host=pg-fs port=not-a-number") == ("pg-fs", 5432)


class TestDsnUnreachableReason:
    def test_unresolvable_keyword_value_host_is_reported(self):
        reason = dsn_unreachable_reason(f"dbname=flow_steward host={UNRESOLVABLE_HOST} port=5432")

        assert reason is not None
        assert UNRESOLVABLE_HOST in reason

    def test_unresolvable_uri_host_is_reported(self):
        reason = dsn_unreachable_reason(f"postgresql://u@{UNRESOLVABLE_HOST}:5432/db")

        assert reason is not None
        assert UNRESOLVABLE_HOST in reason

    def test_reported_reason_never_leaks_dsn_credentials(self):
        dsn = f"dbname=flow_steward user=flow_steward password=sup3rs3cret host={UNRESOLVABLE_HOST}"

        reason = dsn_unreachable_reason(dsn)

        assert reason is not None
        assert "sup3rs3cret" not in reason

    def test_resolvable_host_reports_no_reason(self):
        assert dsn_unreachable_reason("dbname=flow_steward host=localhost port=5432") is None

    def test_dsn_without_tcp_host_reports_no_reason(self):
        assert dsn_unreachable_reason("dbname=flow_steward") is None


class TestHarnessGuardsSkipInsteadOfFailing:
    """The guards themselves must raise Skipped, not surface a connection error."""

    def test_conftest_registry_guard_skips_on_unreachable_host(self, monkeypatch):
        from tests.conftest import _ensure_registry_dsn_available

        with pytest.raises(pytest.skip.Exception) as excinfo:
            _ensure_registry_dsn_available(
                f"dbname=flow_steward host={UNRESOLVABLE_HOST}", monkeypatch
            )

        assert "not reachable" in str(excinfo.value)

    def test_registry_module_guard_skips_on_unreachable_host(self, monkeypatch):
        import tests.unit.test_registry as registry_tests

        monkeypatch.setattr(
            registry_tests,
            "resolve_test_db_dsn",
            lambda: f"dbname=flow_steward host={UNRESOLVABLE_HOST}",
        )

        with pytest.raises(pytest.skip.Exception) as excinfo:
            registry_tests._require_db()

        assert "not reachable" in str(excinfo.value)
