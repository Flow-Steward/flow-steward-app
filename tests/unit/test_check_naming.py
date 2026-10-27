"""SEC-001: Unit tests for the naming guardrail script.

Run:
    pytest tests/unit/test_check_naming.py -v
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from core.application.tooling.quality import check_naming

_RETIRED_TOKEN = "retiredcodename"
_RETIRED_TOKEN_UPPER = _RETIRED_TOKEN.upper()


class TestFilenameCheck:
    def test_rejects_retired_token_in_filename(self):
        violations = check_naming._check_filename(f"security/{_RETIRED_TOKEN}_scanner.py")
        assert len(violations) == 1
        assert "retired token marker" in violations[0].lower()

    def test_accepts_clean_filename(self):
        assert check_naming._check_filename("security/scanner.py") == []

    def test_case_insensitive(self):
        violations = check_naming._check_filename(f"security/{_RETIRED_TOKEN_UPPER}_thing.py")
        assert len(violations) == 1


class TestSQLEntityCheck:
    def test_rejects_retired_token_table(self):
        sql = f"CREATE TABLE IF NOT EXISTS {_RETIRED_TOKEN}_new_table (id SERIAL);"
        violations = check_naming._check_sql_entities("db/schema/099_test.sql", sql)
        assert len(violations) == 1
        assert f"{_RETIRED_TOKEN}_new_table" in violations[0]

    def test_rejects_old_runtime_table_name(self):
        sql = f"CREATE TABLE IF NOT EXISTS {_RETIRED_TOKEN}_action_log (id SERIAL);"
        violations = check_naming._check_sql_entities("db/schema/099_test.sql", sql)
        assert len(violations) == 1
        assert f"{_RETIRED_TOKEN}_action_log" in violations[0]

    def test_rejects_retired_schema_version_table(self):
        sql = f"CREATE TABLE IF NOT EXISTS {_RETIRED_TOKEN}_schema_version (version INTEGER);"
        violations = check_naming._check_sql_entities("db/schema/099_test.sql", sql)
        assert len(violations) == 1
        assert f"{_RETIRED_TOKEN}_schema_version" in violations[0]

    def test_allows_clean_table(self):
        sql = "CREATE TABLE IF NOT EXISTS security_events (id SERIAL);"
        violations = check_naming._check_sql_entities("db/schema/099_test.sql", sql)
        assert violations == []

    def test_rejects_retired_token_index(self):
        sql = f"CREATE INDEX idx_{_RETIRED_TOKEN}_foo ON sometable (col);"
        violations = check_naming._check_sql_entities("db/schema/099_test.sql", sql)
        assert len(violations) == 1


class TestPyRouteCheck:
    def test_rejects_retired_token_route(self):
        code = f'@app.get("/api/{_RETIRED_TOKEN}/status")'
        violations = check_naming._check_py_routes("routes/foo.py", code)
        assert len(violations) == 1

    def test_allows_clean_route(self):
        code = '@app.get("/api/security/posture")'
        violations = check_naming._check_py_routes("routes/foo.py", code)
        assert violations == []


class TestAllowlist:
    def test_zero_allowlist_mode(self):
        assert not check_naming._is_allowlisted("docs/GUIDE.md")
        assert not check_naming._is_allowlisted("CLAU" + "DE.md")

    def test_new_paths_not_allowed(self):
        assert not check_naming._is_allowlisted("security/scanner.py")
        assert not check_naming._is_allowlisted("routes/security.py")
        assert not check_naming._is_allowlisted("assets/ap" + "p.js")


class TestCheckFiles:
    def test_docs_files_are_checked_like_any_other_path(self, tmp_path):
        f = tmp_path / "docs" / f"{_RETIRED_TOKEN}_docs_ext.md"
        f.parent.mkdir(parents=True)
        f.write_text("# retired")
        violations = check_naming.check_files(
            [f"docs/{_RETIRED_TOKEN}_docs_ext.md"], repo_root=tmp_path
        )
        assert len(violations) == 1
        assert "retired token marker" in violations[0]

    def test_sql_content_checked(self, tmp_path):
        f = tmp_path / "db" / "schema" / "099_new.sql"
        f.parent.mkdir(parents=True)
        f.write_text(f"CREATE TABLE {_RETIRED_TOKEN}_bad (id SERIAL);")
        violations = check_naming.check_files(["db/schema/099_new.sql"], repo_root=tmp_path)
        assert len(violations) == 1


class TestMainEntrypoint:
    @patch("core.application.tooling.quality.check_naming.get_changed_files", return_value=[])
    @patch("core.application.tooling.quality.check_naming.Path")
    def test_no_files_passes(self, mock_path, mock_gcf):
        from unittest.mock import MagicMock

        mock_path.return_value = MagicMock()
        # Use --files with nothing to check
        exit_code = check_naming.main(["--files"])
        assert exit_code == 0

    @patch("core.application.tooling.quality.check_naming.subprocess.run")
    def test_files_mode_falls_back_when_git_root_unavailable(self, mock_run, tmp_path):
        candidate = tmp_path / "feature.py"
        candidate.write_text("print('ok')\n")
        mock_run.side_effect = subprocess.CalledProcessError(
            128,
            ["git", "rev-parse", "--show-toplevel"],
            stderr="fatal: not a git repository",
        )

        exit_code = check_naming.main(["--files", str(candidate)])

        assert exit_code == 0


def test_get_changed_files_does_not_crash_on_unresolvable_base():
    # Shallow-CI regression: a base SHA absent from the clone must degrade (fetch/fallback,
    # else skip) instead of raising CalledProcessError and killing the whole gate.
    from core.application.tooling.quality.check_naming import get_changed_files

    files = get_changed_files("0000000000000000000000000000000000000000")
    assert isinstance(files, list)
