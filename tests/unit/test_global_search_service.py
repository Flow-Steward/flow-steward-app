from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import MagicMock

import pytest

psycopg_runtime_mod = sys.modules.get("psycopg_runtime") or types.ModuleType("psycopg_runtime")
extras_mod = sys.modules.get("psycopg_runtime.extras") or types.ModuleType("psycopg_runtime.extras")
sql_mod = sys.modules.get("psycopg_runtime.sql") or types.ModuleType("psycopg_runtime.sql")
extras_mod.RealDictCursor = getattr(extras_mod, "RealDictCursor", object)
sql_mod.SQL = getattr(sql_mod, "SQL", lambda value="": value)
sql_mod.Identifier = getattr(sql_mod, "Identifier", lambda value="": value)
sql_mod.Literal = getattr(sql_mod, "Literal", lambda value="": value)
psycopg_runtime_mod.extras = extras_mod
psycopg_runtime_mod.sql = sql_mod
sys.modules["psycopg_runtime"] = psycopg_runtime_mod
sys.modules["psycopg_runtime.extras"] = extras_mod
sys.modules["psycopg_runtime.sql"] = sql_mod

import core.application.search.global_search_service as global_search_service_module  # noqa: E402
from core.application.management.errors import (  # noqa: E402
    ServiceUnavailableError,
    ServiceValidationError,
)
from core.application.management.scope import ScopeContext, normalize_scope  # noqa: E402
from core.application.search.global_search_service import GlobalSearchService  # noqa: E402
from core.application.search.search_postgres import search_postgres_sources  # noqa: E402

PROJECT_A = "00000000-0000-0000-0000-000000000007"
PROJECT_B = "00000000-0000-0000-0000-000000000008"
PROJECT_BLOCKED = "00000000-0000-0000-0000-000000000009"
JOB_A = "00000000-0000-0000-0000-000000000012"


def _scope(
    *,
    account_id: str = "acc-1",
    actor_id: str = "user-1",
    principal_type: str | None = "admin",
    project_id: str | None = None,
    allowed_project_ids: frozenset[str] | None = None,
    tenant_id: str | None = None,
) -> ScopeContext:
    scope = normalize_scope(
        account_id=account_id,
        actor_id=actor_id,
        principal_type=principal_type,
        project_id=project_id,
        allowed_project_ids=allowed_project_ids,
    )
    object.__setattr__(scope, "__dict__", {**scope.__dict__, "tenant_id": tenant_id})
    return scope


class TestGlobalSearchService:
    def test_global_search_rejects_retired_kb_group_and_all_never_dispatches_it(self) -> None:
        bl = MagicMock()
        bl.get_projects.return_value = []
        dispatched: list[tuple[str, ...]] = []

        def fake_postgres_search(**kwargs):
            dispatched.append(tuple(kwargs["groups"]))
            return {}

        service = GlobalSearchService(
            get_logger=lambda: bl,
            postgres_search_fn=fake_postgres_search,
        )
        scope = _scope()

        with pytest.raises(ServiceValidationError, match="Unsupported result_groups"):
            service.global_search(
                "retired",
                scope=scope,
                filters={"result_groups": ["kb"]},
            )

        payload = service.global_search("all groups", scope=_scope())
        assert all(group["group"] != "kb" for group in payload["groups"])
        assert dispatched
        assert all("kb" not in groups for groups in dispatched)

    @pytest.mark.parametrize(
        ("principal_type", "expected_dispatched_groups"),
        (
            ("viewer", ("jobs",)),
            ("operator", ("jobs", "workflows")),
            ("admin", ("jobs", "workflows", "model_providers", "users")),
        ),
    )
    def test_global_search_filters_catalogs_by_role_before_backend_dispatch(
        self,
        principal_type: str,
        expected_dispatched_groups: tuple[str, ...],
    ) -> None:
        bl = MagicMock()
        bl.get_projects.return_value = []
        dispatched_groups: list[tuple[str, ...]] = []

        def fake_postgres_search(**kwargs):
            groups = tuple(kwargs["groups"])
            dispatched_groups.append(groups)
            return {
                group: {
                    "items": [
                        {
                            "group": group,
                            "source": f"postgres.{group}",
                            "id": f"{group}-1",
                            "title": f"Protected {group}",
                            "snippet": f"secret-{group}",
                            "score": 1.0,
                            "project_id": None,
                        }
                    ],
                    "total": 1,
                }
                for group in groups
            }

        service = GlobalSearchService(
            get_logger=lambda: bl,
            postgres_search_fn=fake_postgres_search,
        )

        payload = service.global_search(
            "secret",
            scope=_scope(principal_type=principal_type),
            filters={"result_groups": ["jobs", "workflows", "model_providers", "users"]},
            limit=20,
        )

        assert dispatched_groups == [expected_dispatched_groups]
        groups = {group["group"]: group for group in payload["groups"]}
        for group in ("jobs", "workflows", "model_providers", "users"):
            expected_total = 1 if group in expected_dispatched_groups else 0
            assert groups[group]["total"] == expected_total
            assert groups[group]["returned_count"] == expected_total
            assert groups[group]["next_cursor"] is None

    def test_global_search_merges_groups_and_caps_total(self):
        bl = MagicMock()
        bl.get_projects.return_value = [
            {"id": 7, "name": "Demo", "account_id": "acc-1"},
        ]

        def fake_postgres_search(**kwargs):
            return {
                "jobs": [
                    {
                        "group": "jobs",
                        "source": "postgres.jobs",
                        "id": "11",
                        "title": "Job match",
                        "snippet": "Failed due to rate limit",
                        "score": 0.8,
                        "timestamp": "2026-04-04T12:00:00+00:00",
                        "project_id": 7,
                        "project_name": "Demo",
                        "job_id": 11,
                        "deep_link": "/jobs/11",
                        "meta": {"status": "failed"},
                    }
                ],
                "events": [
                    {
                        "group": "events",
                        "source": "postgres.platform_events",
                        "id": "91",
                        "title": "Rate limit event",
                        "snippet": "GitHub returned 429",
                        "score": 0.7,
                        "timestamp": "2026-04-04T11:00:00+00:00",
                        "project_id": 7,
                        "project_name": "Demo",
                        "job_id": 11,
                        "deep_link": "/jobs/11",
                        "meta": {"severity": "error"},
                    }
                ],
            }

        service = GlobalSearchService(
            get_logger=lambda: bl,
            postgres_search_fn=fake_postgres_search,
        )

        payload = service.global_search(
            "rate limit",
            scope=_scope(project_id=7),
            filters={"result_groups": ["jobs", "events"]},
            limit=2,
        )

        assert payload["query"] == "rate limit"
        assert [group["group"] for group in payload["groups"]] == ["jobs", "events"]
        returned_items = [item for group in payload["groups"] for item in group["items"]]
        assert len(returned_items) == 2
        assert {item["source"] for item in returned_items} == {
            "postgres.platform_events",
            "postgres.jobs",
        }
        assert payload["groups"][0]["items"][0]["source"] == "postgres.jobs"
        assert payload["groups"][1]["total"] == 1
        assert payload["groups"][1]["items"][0]["source"] == "postgres.platform_events"

    def test_global_search_filters_out_items_outside_allowed_project_set(self):
        bl = MagicMock()
        bl.get_projects.return_value = [
            {"id": PROJECT_A, "name": "Allowed", "account_id": "acc-1"},
            {"id": PROJECT_BLOCKED, "name": "Blocked", "account_id": "acc-1"},
        ]

        def fake_postgres_search(**kwargs):
            return {
                "jobs": [
                    {
                        "group": "jobs",
                        "source": "postgres.jobs",
                        "id": "1",
                        "title": "Allowed",
                        "snippet": "in scope",
                        "score": 0.7,
                        "timestamp": "2026-04-04T12:00:00+00:00",
                        "project_id": PROJECT_A,
                        "project_name": "Allowed",
                        "job_id": JOB_A,
                        "deep_link": "/jobs/1",
                        "meta": {},
                    },
                    {
                        "group": "jobs",
                        "source": "postgres.jobs",
                        "id": "2",
                        "title": "Blocked",
                        "snippet": "out of scope",
                        "score": 0.95,
                        "timestamp": "2026-04-04T13:00:00+00:00",
                        "project_id": PROJECT_BLOCKED,
                        "project_name": "Blocked",
                        "job_id": 2,
                        "deep_link": "/jobs/2",
                        "meta": {},
                    },
                ]
            }

        service = GlobalSearchService(
            get_logger=lambda: bl,
            postgres_search_fn=fake_postgres_search,
        )

        payload = service.global_search(
            "scope",
            scope=_scope(allowed_project_ids=frozenset({PROJECT_A})),
            filters={"result_groups": ["jobs"]},
            limit=10,
        )

        items = payload["groups"][0]["items"]
        assert len(items) == 1
        assert items[0]["project_id"] == PROJECT_A
        assert items[0]["title"] == "Allowed"

    def test_global_search_keeps_system_registry_item_without_project_id(self):
        bl = MagicMock()
        bl.get_projects.return_value = [{"id": PROJECT_A, "name": "Allowed", "account_id": "acc-1"}]

        service = GlobalSearchService(
            get_logger=lambda: bl,
            postgres_search_fn=lambda **_kwargs: {
                "agents": [
                    {
                        "group": "agents",
                        "source": "postgres.agents",
                        "id": "writer",
                        "title": "Developer",
                        "snippet": "System registry agent",
                        "score": 0.9,
                        "timestamp": None,
                        "project_id": None,
                        "deep_link": "/registry/agents/developer",
                        "meta": {"entity_type": "agents", "entity_id": "writer"},
                    }
                ]
            },
        )

        payload = service.global_search(
            "writer",
            scope=_scope(allowed_project_ids=frozenset({PROJECT_A})),
            filters={"result_groups": ["agents"]},
            limit=10,
        )

        assert [item["id"] for item in payload["groups"][0]["items"]] == ["writer"]

    def test_global_search_scopes_results_to_selected_workspace(self):
        bl = MagicMock()
        bl.get_projects.return_value = [
            {
                "id": PROJECT_A,
                "name": "Workspace A Project",
                "account_id": "acc-1",
                "workspace_id": "ws-a",
            },
            {
                "id": PROJECT_B,
                "name": "Workspace B Project",
                "account_id": "acc-1",
                "workspace_id": "ws-b",
            },
        ]
        captured: dict[str, Any] = {}

        def fake_postgres_search(**kwargs):
            captured.update(kwargs)
            return {
                "jobs": [
                    {
                        "group": "jobs",
                        "source": "postgres.jobs",
                        "id": "1",
                        "title": "Workspace A",
                        "snippet": "in workspace",
                        "score": 0.95,
                        "timestamp": "2026-04-04T12:00:00+00:00",
                        "project_id": PROJECT_A,
                        "project_name": "Workspace A Project",
                        "job_id": JOB_A,
                        "deep_link": "/jobs/1",
                        "meta": {},
                    },
                    {
                        "group": "jobs",
                        "source": "postgres.jobs",
                        "id": "2",
                        "title": "Workspace B",
                        "snippet": "out of workspace",
                        "score": 0.99,
                        "timestamp": "2026-04-04T13:00:00+00:00",
                        "project_id": PROJECT_B,
                        "project_name": "Workspace B Project",
                        "job_id": "2",
                        "deep_link": "/jobs/2",
                        "meta": {},
                    },
                ]
            }

        service = GlobalSearchService(
            get_logger=lambda: bl,
            postgres_search_fn=fake_postgres_search,
        )

        payload = service.global_search(
            "workspace",
            scope=_scope(),
            filters={"result_groups": ["jobs"], "workspace_id": "ws-a"},
            limit=10,
        )

        assert captured["project_ids"] == [PROJECT_A]
        assert payload["scope"]["workspace_id"] == "ws-a"
        items = payload["groups"][0]["items"]
        assert len(items) == 1
        assert items[0]["project_id"] == PROJECT_A

    def test_global_search_selected_empty_workspace_returns_no_results(self):
        bl = MagicMock()
        bl.get_projects.return_value = [
            {
                "id": PROJECT_A,
                "name": "Workspace A Project",
                "account_id": "acc-1",
                "workspace_id": "ws-a",
            },
        ]
        captured: dict[str, Any] = {}

        def fake_postgres_search(**kwargs):
            captured.update(kwargs)
            return {
                "jobs": [
                    {
                        "group": "jobs",
                        "source": "postgres.jobs",
                        "id": "1",
                        "title": "Wrong workspace",
                        "snippet": "should be filtered",
                        "score": 0.95,
                        "timestamp": "2026-04-04T12:00:00+00:00",
                        "project_id": PROJECT_A,
                        "project_name": "Workspace A Project",
                        "job_id": JOB_A,
                        "deep_link": "/jobs/1",
                        "meta": {},
                    }
                ]
            }

        service = GlobalSearchService(
            get_logger=lambda: bl,
            postgres_search_fn=fake_postgres_search,
        )

        payload = service.global_search(
            "workspace",
            scope=_scope(),
            filters={"result_groups": ["jobs"], "workspace_id": "ws-missing"},
            limit=10,
        )

        assert captured["project_ids"] == []
        assert payload["scope"]["workspace_id"] == "ws-missing"
        assert payload["groups"][0]["items"] == []

    def test_global_search_postgres_only_failure_is_typed_unavailable(self):
        bl = MagicMock()
        bl.get_projects.return_value = [{"id": 7, "name": "Demo", "account_id": "acc-1"}]
        service = GlobalSearchService(
            get_logger=lambda: bl,
            postgres_search_fn=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("db down")),
        )

        _raises_input_508_1 = _scope(project_id=7)
        with pytest.raises(ServiceUnavailableError) as captured:
            service.global_search(
                "demo", scope=_raises_input_508_1, filters={"result_groups": ["projects"]}, limit=5
            )

        assert captured.value.status_code == 503
        assert captured.value.payload["code"] == "search_backend_unavailable"
        assert captured.value.payload["backend"] == "postgres"

    def test_global_search_group_preview_caps_each_group_independently(self):
        bl = MagicMock()
        bl.get_projects.return_value = [{"id": 7, "name": "Demo", "account_id": "acc-1"}]

        def fake_postgres_search(**kwargs):
            return {
                "jobs": [
                    {
                        "group": "jobs",
                        "source": "postgres.jobs",
                        "id": "j-1",
                        "title": "Job 1",
                        "snippet": "test",
                        "score": 0.91,
                        "timestamp": "2026-04-04T12:00:00+00:00",
                        "project_id": 7,
                        "project_name": "Demo",
                        "job_id": 1,
                        "deep_link": "/jobs/1",
                        "meta": {},
                    },
                    {
                        "group": "jobs",
                        "source": "postgres.jobs",
                        "id": "j-2",
                        "title": "Job 2",
                        "snippet": "test",
                        "score": 0.89,
                        "timestamp": "2026-04-04T11:00:00+00:00",
                        "project_id": 7,
                        "project_name": "Demo",
                        "job_id": 2,
                        "deep_link": "/jobs/2",
                        "meta": {},
                    },
                ],
                "events": [
                    {
                        "group": "events",
                        "source": "postgres.platform_events",
                        "id": "e-1",
                        "title": "Event 1",
                        "snippet": "test",
                        "score": 0.95,
                        "timestamp": "2026-04-04T13:00:00+00:00",
                        "project_id": 7,
                        "project_name": "Demo",
                        "job_id": 1,
                        "deep_link": "/jobs/1",
                        "meta": {},
                    },
                    {
                        "group": "events",
                        "source": "postgres.platform_events",
                        "id": "e-2",
                        "title": "Event 2",
                        "snippet": "test",
                        "score": 0.87,
                        "timestamp": "2026-04-04T10:00:00+00:00",
                        "project_id": 7,
                        "project_name": "Demo",
                        "job_id": 2,
                        "deep_link": "/jobs/2",
                        "meta": {},
                    },
                ],
            }

        service = GlobalSearchService(
            get_logger=lambda: bl,
            postgres_search_fn=fake_postgres_search,
        )

        payload = service.global_search(
            "test",
            scope=_scope(project_id=7),
            filters={"result_groups": ["jobs", "events"], "group_item_limit": 1},
            limit=1,
        )

        assert payload["view_mode"] == "grouped_preview"
        assert payload["group_item_limit"] == 1
        assert payload["groups"][0]["items"][0]["id"] == "j-1"
        assert payload["groups"][1]["items"][0]["id"] == "e-1"
        assert payload["groups"][0]["has_more"] is True
        assert payload["groups"][1]["has_more"] is True

    def test_global_search_group_preview_marks_full_groups_as_maybe_more_without_totals(self):
        bl = MagicMock()
        bl.get_projects.return_value = [{"id": 7, "name": "Demo", "account_id": "acc-1"}]

        service = GlobalSearchService(
            get_logger=lambda: bl,
            postgres_search_fn=lambda **_kwargs: {
                "jobs": [
                    {
                        "group": "jobs",
                        "source": "postgres.jobs",
                        "id": "j-1",
                        "title": "Job 1",
                        "snippet": "test",
                        "score": 0.91,
                        "timestamp": "2026-04-04T12:00:00+00:00",
                        "project_id": 7,
                        "project_name": "Demo",
                        "job_id": 1,
                        "deep_link": "/jobs/1",
                        "meta": {},
                    }
                ]
            },
        )

        payload = service.global_search(
            "test",
            scope=_scope(project_id=7),
            filters={"result_groups": ["jobs"], "group_item_limit": 1},
            limit=1,
        )

        assert payload["groups"][0]["has_more"] is False
        assert payload["groups"][0]["maybe_more"] is True

    def test_global_search_group_preview_uses_absolute_backend_totals(self):
        bl = MagicMock()
        bl.get_projects.return_value = [{"id": 7, "name": "Demo", "account_id": "acc-1"}]

        service = GlobalSearchService(
            get_logger=lambda: bl,
            postgres_search_fn=lambda **_kwargs: {
                "jobs": {
                    "items": [
                        {
                            "group": "jobs",
                            "source": "postgres.jobs",
                            "id": "j-1",
                            "title": "Job 1",
                            "snippet": "test",
                            "score": 0.91,
                            "timestamp": "2026-04-04T12:00:00+00:00",
                            "project_id": 7,
                            "project_name": "Demo",
                            "job_id": 1,
                            "deep_link": "/jobs/1",
                            "meta": {},
                        }
                    ],
                    "total": 20,
                }
            },
        )

        payload = service.global_search(
            "test",
            scope=_scope(project_id=7),
            filters={"result_groups": ["jobs"], "group_item_limit": 1},
            limit=5,
        )

        assert payload["groups"][0]["total"] == 20
        assert payload["groups"][0]["next_cursor"]

    def test_global_search_single_group_cursor_returns_next_page_from_opaque_cursor(self):
        bl = MagicMock()
        bl.get_projects.return_value = [{"id": 7, "name": "Demo", "account_id": "acc-1"}]
        captured: dict[str, Any] = {}

        def fake_postgres_search(**kwargs):
            captured.update(kwargs)
            return {
                "jobs": {
                    "items": [
                        {
                            "group": "jobs",
                            "source": "postgres.jobs",
                            "id": "j-1",
                            "title": "Job 1",
                            "snippet": "test",
                            "score": 0.99,
                            "timestamp": "2026-04-04T15:00:00+00:00",
                            "project_id": 7,
                            "project_name": "Demo",
                            "job_id": 1,
                            "deep_link": "/jobs/1",
                            "meta": {},
                        },
                        {
                            "group": "jobs",
                            "source": "postgres.jobs",
                            "id": "j-2",
                            "title": "Job 2",
                            "snippet": "test",
                            "score": 0.98,
                            "timestamp": "2026-04-04T14:00:00+00:00",
                            "project_id": 7,
                            "project_name": "Demo",
                            "job_id": 2,
                            "deep_link": "/jobs/2",
                            "meta": {},
                        },
                        {
                            "group": "jobs",
                            "source": "postgres.jobs",
                            "id": "j-3",
                            "title": "Job 3",
                            "snippet": "test",
                            "score": 0.97,
                            "timestamp": "2026-04-04T13:00:00+00:00",
                            "project_id": 7,
                            "project_name": "Demo",
                            "job_id": 3,
                            "deep_link": "/jobs/3",
                            "meta": {},
                        },
                        {
                            "group": "jobs",
                            "source": "postgres.jobs",
                            "id": "j-4",
                            "title": "Job 4",
                            "snippet": "test",
                            "score": 0.96,
                            "timestamp": "2026-04-04T12:00:00+00:00",
                            "project_id": 7,
                            "project_name": "Demo",
                            "job_id": 4,
                            "deep_link": "/jobs/4",
                            "meta": {},
                        },
                    ],
                    "total": 7,
                }
            }

        service = GlobalSearchService(
            get_logger=lambda: bl,
            postgres_search_fn=fake_postgres_search,
        )

        payload = service.global_search(
            "test",
            scope=_scope(project_id=7),
            filters={
                "result_groups": ["jobs"],
                "cursor": global_search_service_module._encode_search_cursor("jobs", 2),
            },
            limit=2,
        )

        assert captured["limit"] == 4
        assert [item["id"] for item in payload["groups"][0]["items"]] == ["j-3", "j-4"]
        assert payload["groups"][0]["total"] == 7
        assert payload["groups"][0]["loaded_count"] == 4
        assert payload["groups"][0]["has_more"] is True
        assert payload["groups"][0]["next_cursor"]

    def test_global_search_rejects_unknown_group(self):
        service = GlobalSearchService(get_logger=lambda: MagicMock())
        _raises_input_964_1 = _scope()
        with pytest.raises(ServiceValidationError, match="Unsupported result_groups value"):
            service.global_search(
                "test", scope=_raises_input_964_1, filters={"result_groups": ["jobs", "unknown"]}
            )

    def test_global_search_fails_closed_when_scope_account_is_missing(self):
        service = GlobalSearchService(get_logger=lambda: MagicMock())
        with pytest.raises(ServiceValidationError, match="account_id is required"):
            service.global_search(
                "test",
                scope={},
                filters={"result_groups": ["jobs"]},
            )

    def test_global_search_tenant_scope_without_tenant_safe_projects_returns_empty(self):
        bl = MagicMock()
        bl.get_projects.return_value = [{"id": 7, "name": "Demo", "account_id": "acc-1"}]
        captured: dict[str, Any] = {}

        def fake_postgres_search(**kwargs):
            captured["project_ids"] = kwargs["project_ids"]
            return {
                "jobs": [
                    {
                        "group": "jobs",
                        "source": "postgres.jobs",
                        "id": "11",
                        "title": "Job",
                        "snippet": "unsafe tenant result",
                        "score": 0.9,
                        "timestamp": "2026-04-04T11:00:00+00:00",
                        "project_id": 7,
                        "project_name": "Demo",
                        "job_id": 11,
                        "deep_link": "/jobs/11",
                        "meta": {},
                    }
                ]
            }

        service = GlobalSearchService(
            get_logger=lambda: bl,
            postgres_search_fn=fake_postgres_search,
        )

        payload = service.global_search(
            "tenant query",
            scope=_scope(tenant_id="tenant-1"),
            filters={"result_groups": ["jobs"]},
            limit=10,
        )

        assert captured["project_ids"] == []
        assert payload["groups"][0]["total"] == 0
        assert payload["groups"][0]["items"] == []


class _RecordingCursor:
    def __init__(
        self, *, results: list[list[dict[str, Any]]] | None = None, error: Exception | None = None
    ):
        self.calls: list[tuple[str, tuple[Any, ...] | None]] = []
        self._results = list(results or [])
        self._error = error

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.calls.append((str(sql), tuple(params) if params is not None else None))
        if self._error is not None:
            raise self._error

    def fetchall(self):
        if self._results:
            return self._results.pop(0)
        return []


class _RecordingConn:
    def __init__(
        self, *, results: list[list[dict[str, Any]]] | None = None, error: Exception | None = None
    ):
        self.cursor_impl = _RecordingCursor(results=results, error=error)
        self.rollback_called = False

    def cursor(self, *args, **kwargs):
        return self.cursor_impl

    def rollback(self):
        self.rollback_called = True


class TestSearchPostgresSources:
    @pytest.mark.parametrize(
        ("group", "expected_table", "expected_link"),
        [
            ("agents", "registry_agents", "/registry/agents/entity-1"),
            ("tools", "registry_tools", "/registry/tools/entity-1"),
            ("skills", "registry_skills", "/registry/skills/entity-1"),
            ("mcp_connectors", "mcp_connectors", "/registry/mcp/entity-1"),
            ("projects", "projects", "/projects/entity-1"),
            ("datasets", "registry_datasets", "/registry/datasets/entity-1"),
            (
                "data_connections",
                "workspace_data_connections",
                "/registry/data-connections",
            ),
            ("extensions", "extension_installs", "/extensions/entity-1"),
            ("guardrails", "registry_guardrails", "/registry/guardrails/entity-1"),
            (
                "data_contracts",
                "registry_data_contracts",
                "/registry/data-contracts/entity-1",
            ),
            (
                "saved_queries",
                "dataset_query_templates",
                "/registry/datasets/owner-dataset-1?tab=saved-queries",
            ),
            ("model_providers", "model_providers", "/settings/providers"),
            ("model_routes", "llm_provider_models", "/settings/providers"),
            ("users", "users", "/settings/users"),
            ("workspaces", "workspaces", "/registry/workspaces/entity-1"),
            ("fields", "registry_fields", "/registry/fields/entity-1"),
            (
                "published_blocks",
                "registry_published_block_contracts",
                "/registry/published-blocks/entity-1",
            ),
        ],
    )
    def test_search_postgres_sources_returns_user_visible_catalog_entities(
        self, group: str, expected_table: str, expected_link: str
    ) -> None:
        conn = _RecordingConn(
            results=[
                [
                    {
                        "entity_id": "entity-1",
                        "title": "Visible entity",
                        "description": "Catalog description",
                        "search_timestamp": "2026-07-30T12:00:00+00:00",
                        "project_id": None,
                        "dataset_id": "owner-dataset-1",
                    }
                ]
            ]
        )
        bl = MagicMock(conn=conn)
        bl._column_exists = lambda _table, _column: False

        payload = search_postgres_sources(
            bl=bl,
            query="Visible",
            scope=_scope(),
            filters={},
            limit=5,
            project_ids=None,
            groups=[group],
        )

        assert payload[group][0]["id"] == "entity-1"
        assert payload[group][0]["deep_link"] == expected_link
        sql, params = conn.cursor_impl.calls[0]
        assert expected_table in sql
        assert sql.count("%s") == len(params)

    @pytest.mark.parametrize(
        ("group", "required_sql"),
        [
            (
                "extensions",
                ("ei.state = 'active'", "ei.account_id", "ei.visibility = 'global'"),
            ),
            (
                "guardrails",
                ("g.enabled = true", "g.deleted_at IS NULL", "g.account_id"),
            ),
            (
                "saved_queries",
                (
                    "q.enabled = true",
                    "q.deleted_at IS NULL",
                    "d.enabled = true",
                    "d.deleted_at IS NULL",
                    "project_datasets",
                ),
            ),
            (
                "model_routes",
                (
                    "m.enabled = true",
                    "m.supports_structured_output = true",
                    "s.enabled = true",
                    "ei.state = 'active'",
                ),
            ),
            ("users", ("u.is_active = true", "u.deleted_at IS NULL", "u.account_id")),
            (
                "workspaces",
                ("w.status = 'active'", "w.deleted_at IS NULL", "w.account_id"),
            ),
            (
                "published_blocks",
                (
                    "b.enabled = true",
                    "b.visible_in_catalog = true",
                    "b.availability_status = 'available'",
                    "b.new_runs_blocked = false",
                ),
            ),
        ],
    )
    def test_catalog_search_reuses_owner_visibility_predicates(
        self, group: str, required_sql: tuple[str, ...]
    ) -> None:
        conn = _RecordingConn(results=[[]])
        bl = MagicMock(conn=conn)
        bl._column_exists = lambda _table, _column: False

        search_postgres_sources(
            bl=bl,
            query="visible",
            scope=_scope(),
            filters={"workspace_id": "workspace-1"},
            limit=5,
            project_ids=[PROJECT_A],
            groups=[group],
        )

        sql, params = conn.cursor_impl.calls[0]
        assert all(clause in sql for clause in required_sql)
        assert sql.count("%s") == len(params)

    @pytest.mark.parametrize(
        ("project_ids", "expected_projection", "expected_project_id"),
        [
            ([PROJECT_A], "CAST(%s AS TEXT) AS project_id", PROJECT_A),
            ([PROJECT_A, PROJECT_B], "NULL::text AS project_id", None),
        ],
    )
    def test_saved_query_project_attribution_requires_single_scoped_project(
        self,
        project_ids: list[str],
        expected_projection: str,
        expected_project_id: str | None,
    ) -> None:
        conn = _RecordingConn(
            results=[
                [
                    {
                        "entity_id": "query-template-1",
                        "dataset_id": "dataset-1",
                        "title": "Saved query",
                        "description": "Visible saved query",
                        "search_timestamp": "2026-07-30T12:00:00+00:00",
                        "project_id": expected_project_id,
                    }
                ]
            ]
        )
        bl = MagicMock(conn=conn)
        bl._column_exists = lambda _table, _column: False

        payload = search_postgres_sources(
            bl=bl,
            query="Visible",
            scope=_scope(tenant_id="tenant-1"),
            filters={"workspace_id": "workspace-1"},
            limit=5,
            project_ids=project_ids,
            groups=["saved_queries"],
        )

        item = payload["saved_queries"][0]
        assert item["project_id"] == expected_project_id
        sql, params = conn.cursor_impl.calls[0]
        assert expected_projection in sql
        assert "CAST(w.account_id AS TEXT) = %s" in sql
        assert "CAST(w.tenant_id AS TEXT) = %s" in sql
        assert "CAST(d.workspace_id AS TEXT) = %s" in sql
        assert "SELECT 1 FROM project_datasets pd" in sql
        assert "d.availability_mode = 'all_workspace_projects'" in sql
        assert sql.count("%s") == len(params)
        if expected_project_id is not None:
            assert params[0] == expected_project_id

    def test_search_postgres_sources_scopes_queries_by_projects_and_job_id(self):
        conn = _RecordingConn(
            results=[
                [
                    {
                        "id": JOB_A,
                        "project_id": PROJECT_A,
                        "project_name": "Demo",
                        "task_id": "job-12",
                        "task_summary": "Find rate limit failures",
                        "status": "failed",
                        "selected_workflow": "triage",
                        "last_error": "GitHub 429",
                        "search_timestamp": "2026-04-04T12:00:00+00:00",
                        "relevance": 0.9,
                    }
                ]
            ]
        )
        bl = MagicMock(conn=conn)
        bl._column_exists = lambda _table, _column: False

        payload = search_postgres_sources(
            bl=bl,
            query="rate limit",
            scope=_scope(project_id=PROJECT_A),
            filters={"job_id": JOB_A},
            limit=5,
            project_ids=[PROJECT_A, PROJECT_B],
            groups=["jobs"],
        )

        assert payload["jobs"][0]["job_id"] == JOB_A
        sql, params = conn.cursor_impl.calls[0]
        assert "j.project_id = ANY(ARRAY[%s, %s]::uuid[])" in sql
        assert "j.id = %s" in sql
        assert JOB_A in params
        assert params[-1] == 5

    def test_search_postgres_sources_tenant_scope_fails_closed_without_tenant_columns(self):
        conn = _RecordingConn(results=[])
        bl = MagicMock(conn=conn)
        bl._column_exists = lambda _table, _column: False

        payload = search_postgres_sources(
            bl=bl,
            query="tenant scoped search",
            scope=_scope(tenant_id="tenant-1"),
            filters={},
            limit=5,
            project_ids=None,
            groups=["jobs", "events", "runtime_messages"],
        )

        assert payload == {"jobs": [], "events": [], "runtime_messages": []}
        assert conn.cursor_impl.calls == []

    def test_search_postgres_sources_events_use_normalized_detail_text_and_deleted_filters(self):
        conn = _RecordingConn(
            results=[
                [
                    {
                        "id": 21,
                        "job_id": 12,
                        "timestamp": "2026-04-04T12:00:00+00:00",
                        "phase": "writer",
                        "agent": "writer",
                        "event_type": "job.failed",
                        "severity": "error",
                        "title": "Workflow failed",
                        "detail_text": "timeout while calling provider",
                        "project_id": 7,
                        "project_name": "Demo",
                        "relevance": 0.8,
                    }
                ]
            ]
        )
        bl = MagicMock(conn=conn)
        bl._column_exists = lambda table, column: (
            (table, column)
            in {
                ("jobs", "deleted_at"),
                ("projects", "deleted_at"),
            }
        )

        payload = search_postgres_sources(
            bl=bl,
            query="timeout provider",
            scope=_scope(project_id=7),
            filters={},
            limit=5,
            project_ids=[7],
            groups=["events"],
        )

        sql, _params = conn.cursor_impl.calls[0]
        assert "j.deleted_at IS NULL" in sql
        assert "p.deleted_at IS NULL" in sql
        assert "detail::text" not in sql
        assert "detail->>'message'" in sql
        assert payload["events"][0]["snippet"] == "timeout while calling provider"

    def test_search_postgres_sources_can_return_absolute_totals(self):
        conn = _RecordingConn(
            results=[
                [{"total": 31}],
                [
                    {
                        "id": JOB_A,
                        "project_id": PROJECT_A,
                        "project_name": "Demo",
                        "task_id": "job-12",
                        "task_summary": "Find rate limit failures",
                        "status": "failed",
                        "selected_workflow": "triage",
                        "last_error": "GitHub 429",
                        "search_timestamp": "2026-04-04T12:00:00+00:00",
                        "relevance": 0.9,
                    }
                ],
            ]
        )
        bl = MagicMock(conn=conn)
        bl._column_exists = lambda _table, _column: False

        payload = search_postgres_sources(
            bl=bl,
            query="rate limit",
            scope=_scope(project_id=PROJECT_A),
            filters={},
            limit=5,
            project_ids=[PROJECT_A],
            groups=["jobs"],
            include_totals=True,
        )

        assert payload["jobs"]["total"] == 31
        assert payload["jobs"]["items"][0]["job_id"] == JOB_A
        assert len(conn.cursor_impl.calls) == 2
        assert "Composed([" not in conn.cursor_impl.calls[0][0]

    def test_search_postgres_sources_workflows_return_step_deep_links(self):
        conn = _RecordingConn(
            results=[
                [
                    {
                        "id": "wf.berlin:step:berlin_review",
                        "workflow_id": "wf.berlin",
                        "workflow_display_name": "Berlin Launch",
                        "workflow_description": "Prepare the Berlin launch workflow.",
                        "project_id": PROJECT_A,
                        "group_id": "planning",
                        "group_display_name": "Planning",
                        "step_id": "berlin_review",
                        "step_display_name": "Berlin review",
                        "step_ordinal": 2,
                        "match_scope": "step",
                        "matched_text": "Review Berlin partner notes and constraints.",
                        "search_timestamp": "2026-04-04T12:00:00+00:00",
                        "relevance": 0.91,
                    }
                ]
            ]
        )
        bl = MagicMock(conn=conn)
        bl._column_exists = lambda table, column: (
            (table, column)
            in {
                ("registry_workflows", "deleted_at"),
                ("registry_workflows", "visible_in_catalog"),
                ("registry_workflows", "account_id"),
                ("project_workflows", "enabled"),
                ("project_workflows", "deleted_at"),
            }
        )

        payload = search_postgres_sources(
            bl=bl,
            query="Berlin",
            scope=_scope(project_id=PROJECT_A),
            filters={},
            limit=5,
            project_ids=[PROJECT_A],
            groups=["workflows"],
        )

        item = payload["workflows"][0]
        assert item["workflow_id"] == "wf.berlin"
        assert item["deep_link"] == (
            "/registry/workflows/wf.berlin/builder?drawer=step&step_id=berlin_review"
        )
        assert item["meta"]["match_scope"] == "step"
        assert item["meta"]["group_id"] == "planning"
        assert item["meta"]["step_id"] == "berlin_review"
        assert "phase_id" not in item["meta"]
        assert "Step: Berlin review" in item["snippet"]
        assert "Group: Planning" in item["snippet"]
        sql, params = conn.cursor_impl.calls[0]
        assert "FROM project_workflows pw" in sql
        assert "JOIN registry_workflow_steps s ON s.owner_workflow_id = w.workflow_id" in sql
        assert "LEFT JOIN registry_workflow_groups g ON g.id = s.group_id" in sql
        assert "JOIN registry_workflow_groups g ON g.workflow_id = w.workflow_id" in sql
        assert "CONCAT(w.workflow_id, ':step:', s.step_id) AS id" in sql
        assert "'group' AS match_scope" in sql
        assert "registry_workflow_phases" not in sql
        assert "registry_phase_steps" not in sql
        assert sql.count("%s") == len(params)
        assert params[-1] == 5

    def test_workflow_search_root_step_match_has_no_group(self):
        conn = _RecordingConn(
            results=[
                [
                    {
                        "id": "wf.berlin:step:fetch_notes",
                        "workflow_id": "wf.berlin",
                        "workflow_display_name": "Berlin Launch",
                        "workflow_description": "Prepare the Berlin launch workflow.",
                        "project_id": None,
                        "group_id": None,
                        "group_display_name": None,
                        "step_id": "fetch_notes",
                        "step_display_name": "Fetch notes",
                        "step_ordinal": 1,
                        "match_scope": "step",
                        "matched_text": "Fetch notes",
                        "search_timestamp": "2026-04-04T12:00:00+00:00",
                        "relevance": 0.92,
                    }
                ]
            ]
        )
        bl = MagicMock(conn=conn)
        bl._column_exists = lambda _table, _column: False

        payload = search_postgres_sources(
            bl=bl,
            query="fetch",
            scope=_scope(),
            filters={},
            limit=5,
            project_ids=None,
            groups=["workflows"],
        )

        item = payload["workflows"][0]
        assert item["deep_link"] == (
            "/registry/workflows/wf.berlin/builder?drawer=step&step_id=fetch_notes"
        )
        assert item["meta"]["group_id"] is None
        assert "Group:" not in item["snippet"]

    def test_workflow_search_group_match_links_to_group(self):
        conn = _RecordingConn(
            results=[
                [
                    {
                        "id": "wf.berlin:group:per_partner",
                        "workflow_id": "wf.berlin",
                        "workflow_display_name": "Berlin Launch",
                        "workflow_description": "Prepare the Berlin launch workflow.",
                        "project_id": None,
                        "group_id": "per_partner",
                        "group_display_name": "Per partner",
                        "step_id": None,
                        "step_display_name": None,
                        "step_ordinal": None,
                        "match_scope": "group",
                        "matched_text": "Loop over Berlin partners",
                        "search_timestamp": "2026-04-04T12:00:00+00:00",
                        "relevance": 0.86,
                    }
                ]
            ]
        )
        bl = MagicMock(conn=conn)
        bl._column_exists = lambda _table, _column: False

        payload = search_postgres_sources(
            bl=bl,
            query="partner",
            scope=_scope(),
            filters={},
            limit=5,
            project_ids=None,
            groups=["workflows"],
        )

        item = payload["workflows"][0]
        assert item["deep_link"] == (
            "/registry/workflows/wf.berlin/builder?drawer=group&group_id=per_partner"
        )
        assert item["meta"]["match_scope"] == "group"
        assert item["meta"]["group_id"] == "per_partner"
        assert item["snippet"].startswith("Group: Per partner")

    def test_workflow_search_deduplicates_group_and_step_matches_by_workflow(self):
        conn = _RecordingConn(
            results=[
                [
                    {
                        "id": "audit:workflow",
                        "workflow_id": "audit",
                        "workflow_display_name": "Audit workflow",
                        "workflow_description": "Audit",
                        "project_id": PROJECT_A,
                        "group_id": None,
                        "group_display_name": None,
                        "step_id": None,
                        "step_display_name": None,
                        "step_ordinal": None,
                        "match_scope": "workflow",
                        "matched_text": "Audit workflow",
                        "search_timestamp": "2026-07-30T12:00:00+00:00",
                        "relevance": 0.99,
                    },
                    {
                        "id": "audit:step:audit_step",
                        "workflow_id": "audit",
                        "workflow_display_name": "Audit workflow",
                        "workflow_description": "Audit",
                        "project_id": PROJECT_A,
                        "group_id": "review",
                        "group_display_name": "Audit review",
                        "step_id": "audit_step",
                        "step_display_name": "Audit step",
                        "step_ordinal": 1,
                        "match_scope": "step",
                        "matched_text": "Audit step",
                        "search_timestamp": "2026-07-30T12:00:00+00:00",
                        "relevance": 0.88,
                    },
                ]
            ]
        )
        bl = MagicMock(conn=conn)
        bl._column_exists = lambda _table, _column: False

        payload = search_postgres_sources(
            bl=bl,
            query="audit",
            scope=_scope(),
            filters={},
            limit=5,
            project_ids=None,
            groups=["workflows"],
        )

        assert [item["workflow_id"] for item in payload["workflows"]] == ["audit"]

    def test_workflow_search_keeps_global_catalog_rows_when_projects_are_scoped(self):
        conn = _RecordingConn(
            results=[
                [
                    {
                        "id": "monthly_review:workflow",
                        "workflow_id": "monthly_review",
                        "workflow_display_name": "Contextual Bugfix",
                        "workflow_description": "Bugfix workflow",
                        "project_id": None,
                        "group_id": None,
                        "group_display_name": None,
                        "step_id": None,
                        "step_display_name": None,
                        "step_ordinal": None,
                        "match_scope": "workflow",
                        "matched_text": "Bugfix workflow",
                        "search_timestamp": "2026-07-01T00:00:00+00:00",
                        "relevance": 0.99,
                    }
                ]
            ]
        )
        bl = MagicMock(conn=conn)
        bl._column_exists = lambda table, column: (
            (table, column)
            in {
                ("registry_workflows", "visible_in_catalog"),
                ("registry_workflows", "account_id"),
                ("project_workflows", "enabled"),
                ("project_workflows", "deleted_at"),
            }
        )

        payload = search_postgres_sources(
            bl=bl,
            query="bugfix",
            scope=_scope(allowed_project_ids=frozenset({PROJECT_A})),
            filters={},
            limit=5,
            project_ids=[PROJECT_A],
            groups=["workflows"],
        )

        assert payload["workflows"][0]["workflow_id"] == "monthly_review"
        assert payload["workflows"][0]["deep_link"] == "/registry/workflows/monthly_review/builder"
        sql, _params = conn.cursor_impl.calls[0]
        assert "w.project_id IS NOT NULL OR w.globally_visible = true" in sql

    def test_workflow_search_includes_account_scoped_extension_catalog_projections(self):
        conn = _RecordingConn(
            results=[
                [
                    {
                        "id": "extension.install-a.story-cycle:workflow",
                        "workflow_id": "extension.install-a.story-cycle",
                        "workflow_display_name": "Story Cycle",
                        "workflow_description": "Extension workflow",
                        "project_id": None,
                        "group_id": None,
                        "group_display_name": None,
                        "step_id": None,
                        "step_display_name": None,
                        "step_ordinal": None,
                        "match_scope": "workflow",
                        "matched_text": "Story Cycle",
                        "search_timestamp": "2026-07-30T12:00:00+00:00",
                        "relevance": 0.97,
                    }
                ]
            ]
        )
        bl = MagicMock(conn=conn)
        bl._column_exists = lambda table, column: (
            (table, column)
            in {
                ("registry_workflows", "account_id"),
                ("extension_registry_projections", "projection_jsonb"),
            }
        )

        payload = search_postgres_sources(
            bl=bl,
            query="Story Cycle",
            scope=_scope(),
            filters={},
            limit=5,
            project_ids=None,
            groups=["workflows"],
        )

        assert payload["workflows"][0]["workflow_id"] == "extension.install-a.story-cycle"
        sql, params = conn.cursor_impl.calls[0]
        assert "FROM extension_registry_projections ep" in sql
        assert "CAST(ep.account_id AS TEXT) = %s" in sql
        assert params.count("acc-1") == 2
        assert sql.count("%s") == len(params)


def test_global_search_get_logger_prefers_observability_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core_config_module = types.ModuleType("core.infrastructure.config.runtime_config")
    core_config_module.CONFIG = {
        "fs_observability_dsn": "postgres://observability-dsn",
        "fs_db_dsn": "postgres://retired-dsn",
    }

    class _FakeLogger:
        def __init__(self, dsn: str):
            self.dsn = dsn

    platform_stores_module = types.ModuleType("core.infrastructure.db.platform_stores")
    platform_stores_module.PlatformProjectStore = _FakeLogger

    monkeypatch.setitem(
        sys.modules, "core.infrastructure.config.runtime_config", core_config_module
    )
    monkeypatch.setitem(
        sys.modules, "core.infrastructure.db.platform_stores", platform_stores_module
    )

    logger = global_search_service_module._get_logger()

    assert isinstance(logger, _FakeLogger)
    assert logger.dsn == "postgres://observability-dsn"
