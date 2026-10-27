"""AC17-R164: a stale graph catalogue is a readiness failure, named.

After a Compact restore died before db_restore.sh's graph catalogue repair,
ag_catalog.ag_graph held graphid 21389 for a namespace whose oid was 95769 -- graph
search broken -- while ./install.sh and /health/ready reported everything ready.
"""

from __future__ import annotations

from contextlib import contextmanager

from core.infrastructure.runtime.readiness_runtime import (
    _STALE_GRAPHS_SQL,
    _graph_catalog_check,
)


class _Cursor:
    def __init__(self, *, age: bool, stale: list[str]) -> None:
        self.age = age
        self.stale = stale
        self.executed: list[str] = []

    def execute(self, sql: str) -> None:
        self.executed.append(sql)

    def fetchone(self):
        return (self.age,)

    def fetchall(self):
        return [(name,) for name in self.stale]


class _Bl:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

        @contextmanager
        def cursor():
            yield self._cursor

        self.conn = type("Conn", (), {"cursor": staticmethod(cursor)})()


def test_a_stale_graph_catalogue_fails_readiness_by_name() -> None:
    result = _graph_catalog_check(_Bl(_Cursor(age=True, stale=["platform_workflow_graph"])))

    assert result["ok"] is False
    assert result["title"] == "Graph catalogue identity"
    assert "platform_workflow_graph" in result["detail"]
    assert "db_restore.sh" in result["remediation"]


def test_a_matching_graph_catalogue_passes() -> None:
    """The green direction, and the query is the repair's own predicate."""
    cursor = _Cursor(age=True, stale=[])

    assert _graph_catalog_check(_Bl(cursor))["ok"] is True
    assert cursor.executed[-1] == _STALE_GRAPHS_SQL
    assert "graphid IS DISTINCT FROM namespace::oid" in _STALE_GRAPHS_SQL


def test_without_the_graph_extension_there_is_nothing_to_check() -> None:
    cursor = _Cursor(age=False, stale=["never read"])

    assert _graph_catalog_check(_Bl(cursor))["ok"] is True
    assert len(cursor.executed) == 1


def test_the_restore_repairs_exactly_what_readiness_reports() -> None:
    """Derived: db_restore.sh's repair and this check share the predicate. The repair is
    one file, run by the restore and by `doctor --fix graph_catalog`."""
    from pathlib import Path

    scripts = Path(__file__).resolve().parents[2] / "core/entrypoints/cli/scripts"
    restore = (scripts / "db_restore.sh").read_text(encoding="utf-8")
    assert '"${SCRIPT_DIR}/age_catalog_repair.sql"' in restore
    repair = (scripts / "age_catalog_repair.sql").read_text(encoding="utf-8")
    assert "WHERE graphid IS DISTINCT FROM namespace::oid" in repair


def test_the_compact_installer_probe_sees_the_applications_own_checks(monkeypatch) -> None:
    """Live on the auditor's Compact stand: an update over a stale graph exited 0, because
    Compact's installer readiness was only the aggregate of role liveness."""
    from core.entrypoints.cli import compact_platform_runtime as compact

    monkeypatch.setattr(compact, "_compact_health_payload", lambda: {"ready": True})
    monkeypatch.setattr(
        compact,
        "_runtime_health_payload",
        lambda _host, _port: {
            "ready": False,
            "checks": {
                "postgres": {"ok": True},
                "graph_catalog": _graph_catalog_check(
                    _Bl(_Cursor(age=True, stale=["platform_workflow_graph"]))
                ),
            },
        },
    )
    monkeypatch.delenv("FS_COMPACT_HEALTH_URL", raising=False)

    checks = {check["id"]: check for check in compact._readiness_payload()["checks"]}

    assert checks["compact_runtime"]["status"] == "pass"
    assert checks["graph_catalog"]["status"] == "fail"
    assert checks["graph_catalog"]["title"] == "Graph catalogue identity"


def test_the_one_shot_cli_keeps_the_aggregate(monkeypatch) -> None:
    """fs-cli reaches only the router, whose /health/ready is the aggregate itself."""
    from core.entrypoints.cli import compact_platform_runtime as compact

    monkeypatch.setattr(compact, "_compact_health_payload", lambda: {"ready": True})
    monkeypatch.setattr(
        compact, "_runtime_health_payload", lambda *_: (_ for _ in ()).throw(AssertionError)
    )
    monkeypatch.setenv("FS_COMPACT_HEALTH_URL", "http://compact-runtime:8080/health/ready")

    checks = compact._readiness_payload()["checks"]

    assert [check["id"] for check in checks] == ["compact_runtime"]
