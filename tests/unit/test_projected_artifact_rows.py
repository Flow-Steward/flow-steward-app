from unittest.mock import patch

import pytest


class _ProjectionStore:
    def list_projections(self, *, account_id, only_active):
        assert account_id
        assert only_active is True
        return [
            {
                "artifact_kind": "workflow",
                "artifact_id": "demo_oauth",
                "install_id": "install-123",
                "release_id": "release-123",
                "projection": {
                    "workflow_id": "demo_oauth",
                    "steps": [
                        {
                            "step_id": "profile",
                            "step_kind": "connector",
                            "connector_id": "example.oauth_profile",
                            "connector_install_id": "example",
                            "group_id": None,
                            "lane_id": None,
                        }
                    ],
                    "groups": [],
                },
            }
        ]


class _DB:
    extension_runtime_store = _ProjectionStore()


def test_projection_aware_catalog_hides_inactive_durable_bridge_rows():
    from core.application.registry.projected_artifact_rows import projection_aware_catalog_rows

    manual_rows = [
        {"workflow_id": "manual", "display_name": "Manual"},
        {
            "workflow_id": "extension.install-123.demo",
            "display_name": "Disabled projection bridge",
            "source_install_id": "install-123",
        },
        {
            "dataset_id": "extension.install-123.dataset",
            "display_name": "Disabled dataset bridge",
            "metadata_jsonb": {"projection_bridge": {"install_id": "install-123"}},
        },
    ]

    with patch(
        "core.application.registry.projected_artifact_rows.projected_artifact_rows",
        return_value=[],
    ):
        workflows = projection_aware_catalog_rows(
            object(), "workflow", "workflow_id", manual_rows[:2]
        )
        datasets = projection_aware_catalog_rows(object(), "dataset", "dataset_id", manual_rows[2:])

    assert [row["workflow_id"] for row in workflows] == ["manual"]
    assert datasets == []


def test_projection_aware_catalog_hides_inactive_saved_query_bridge_rows():
    from core.application.registry.projected_artifact_rows import projection_aware_catalog_rows

    saved_query = {
        "query_template_id": "extension.install-123.open_orders",
        "dataset_id": "extension.install-123.orders",
        "validation_result_jsonb": {
            "projection_bridge": {
                "install_id": "install-123",
                "artifact_kind": "saved_query",
            }
        },
    }

    with patch(
        "core.application.registry.projected_artifact_rows.projected_artifact_rows",
        return_value=[],
    ):
        rows = projection_aware_catalog_rows(
            object(),
            "saved_query",
            "query_template_id",
            [saved_query],
        )

    assert rows == []


def test_projected_dataset_inherits_the_account_scope_used_for_projection_lookup():
    from core.application.registry.projected_artifact_rows import projected_artifact_rows

    class _DatasetProjectionStore:
        def list_projections(self, *, account_id, only_active):
            assert account_id == "account-1"
            assert only_active is True
            return [
                {
                    "artifact_kind": "dataset",
                    "artifact_id": "orders",
                    "install_id": "install-123",
                    "release_id": "release-123",
                    "projection": {
                        "dataset_id": "orders",
                        "display_name": "Orders",
                    },
                }
            ]

    class _DatasetDB:
        extension_runtime_store = _DatasetProjectionStore()

    with (
        patch(
            "core.application.registry.projected_artifact_rows.active_extension_ids_for_account",
            return_value={"acme.example"},
        ),
        patch(
            "core.application.registry.projected_artifact_rows.active_extension_versions_for_account",
            return_value={"acme.example": "1.0.0"},
        ),
        patch(
            "core.application.registry.projected_artifact_rows.resolve_projection_dependency_availability",
            return_value={"new_runs_blocked": False},
        ),
    ):
        row = projected_artifact_rows(
            _DatasetDB(),
            "dataset",
            "dataset_id",
            account_id="account-1",
        )[0]

    assert row["account_id"] == "account-1"


def test_projected_saved_query_namespaces_its_projected_dataset_reference():
    from core.application.extensions.projected_dataset_bridge import projection_bridge_id
    from core.application.registry.projected_artifact_rows import projected_artifact_rows

    class _SavedQueryProjectionStore:
        def list_projections(self, *, account_id, only_active):
            assert account_id == "account-1"
            assert only_active is True
            common = {"install_id": "install-123", "release_id": "release-123"}
            return [
                {
                    **common,
                    "artifact_kind": "dataset",
                    "artifact_id": "orders",
                    "projection": {"dataset_id": "orders"},
                },
                {
                    **common,
                    "artifact_kind": "saved_query",
                    "artifact_id": "open_orders",
                    "projection": {
                        "query_template_id": "open_orders",
                        "dataset_id": "orders",
                    },
                },
            ]

    class _SavedQueryDB:
        extension_runtime_store = _SavedQueryProjectionStore()

    with (
        patch(
            "core.application.registry.projected_artifact_rows.active_extension_ids_for_account",
            return_value={"acme.example"},
        ),
        patch(
            "core.application.registry.projected_artifact_rows.active_extension_versions_for_account",
            return_value={"acme.example": "1.0.0"},
        ),
        patch(
            "core.application.registry.projected_artifact_rows.resolve_projection_dependency_availability",
            return_value={"new_runs_blocked": False},
        ),
    ):
        row = projected_artifact_rows(
            _SavedQueryDB(),
            "saved_query",
            "query_template_id",
            account_id="account-1",
        )[0]

    assert row["query_template_id"] == projection_bridge_id("install-123", "open_orders")
    assert row["dataset_id"] == projection_bridge_id("install-123", "orders")


def test_projected_workflow_binds_extension_connector_to_own_install():
    from core.application.registry.projected_artifact_rows import projected_artifact_rows

    with (
        patch(
            "core.application.registry.projected_artifact_rows.active_extension_ids_for_account",
            return_value={"acme.example"},
        ),
        patch(
            "core.application.registry.projected_artifact_rows.active_extension_versions_for_account",
            return_value={"acme.example": "1.0.0"},
        ),
        patch(
            "core.application.registry.projected_artifact_rows.resolve_projection_dependency_availability",
            return_value={"new_runs_blocked": False},
        ),
    ):
        rows = projected_artifact_rows(_DB(), "workflow", "workflow_id")

    step = rows[0]["steps"][0]
    assert step["connector_install_id"] == "install-123"
    assert step["group_id"] is None
    assert "_phases" not in rows[0]


def test_projected_agent_exposes_typed_skill_and_tool_assignments():
    from core.application.extensions.projected_dataset_bridge import projection_bridge_id
    from core.application.management.query_models_admin import AgentRecordView
    from core.application.registry.projected_artifact_rows import projected_artifact_rows

    class _AgentProjectionStore:
        def list_projections(self, *, account_id, only_active):
            assert account_id
            assert only_active is True
            common = {"install_id": "install-123", "release_id": "release-123"}
            return [
                {
                    **common,
                    "artifact_kind": "agent",
                    "artifact_id": "demo_agent",
                    "projection": {
                        "agent_id": "demo_agent",
                        "skills": ["demo_skill"],
                        "tools": ["demo_tool"],
                    },
                },
                {
                    **common,
                    "artifact_kind": "skill",
                    "artifact_id": "demo_skill",
                    "projection": {"skill_id": "demo_skill"},
                },
                {
                    **common,
                    "artifact_kind": "tool",
                    "artifact_id": "demo_tool",
                    "projection": {"tool_id": "demo_tool"},
                },
            ]

    class _AgentDB:
        extension_runtime_store = _AgentProjectionStore()

    with (
        patch(
            "core.application.registry.projected_artifact_rows.active_extension_ids_for_account",
            return_value={"acme.example"},
        ),
        patch(
            "core.application.registry.projected_artifact_rows.active_extension_versions_for_account",
            return_value={"acme.example": "1.0.0"},
        ),
        patch(
            "core.application.registry.projected_artifact_rows.resolve_projection_dependency_availability",
            return_value={"new_runs_blocked": False},
        ),
    ):
        row = projected_artifact_rows(_AgentDB(), "agent", "agent_id")[0]

    skill_id = projection_bridge_id("install-123", "demo_skill")
    tool_id = projection_bridge_id("install-123", "demo_tool")
    assert row["skills"] == [{"skill_id": skill_id, "skill_prompt": "", "sort_order": 0}]
    assert row["tools"] == [{"tool_id": tool_id, "sort_order": 0}]
    assert row["_skills"] == row["skills"]
    assert row["_tools"] == row["tools"]
    AgentRecordView.model_validate(row)


@pytest.mark.parametrize(
    "base_skill,assignments,expected",
    [
        ("write_copy", [], "extension.install-123.write_copy"),
        ("write_copy", ["fallback", "write_copy"], "extension.install-123.write_copy"),
        ("extension.install-123.write_copy", ["fallback"], "extension.install-123.write_copy"),
        ("host_skill", [], "host_skill"),
        ("extension.other.write_copy", [], "extension.other.write_copy"),
    ],
)
def test_projected_base_skill_resolves_through_canonical_primary_selector(
    base_skill, assignments, expected
):
    from copy import deepcopy

    from core.application.management.query_models_admin import AgentRecordView
    from core.application.registry.projected_artifact_rows import projected_artifact_rows
    from core.application.registry.step_skill_resolution import agent_primary_skill_id

    class Store:
        active = True

        def __init__(self):
            self.rows = [
                {
                    "artifact_kind": kind,
                    "artifact_id": identifier,
                    "install_id": install_id,
                    "release_id": "release-1",
                    "projection": payload,
                }
                for install_id in ["install-123", "install-456"]
                for kind, identifier, payload in [
                    (
                        "agent",
                        "copywriter",
                        {"agent_id": "copywriter", "base_skill": base_skill, "skills": assignments},
                    ),
                    ("skill", "write_copy", {"skill_id": "write_copy"}),
                    ("skill", "fallback", {"skill_id": "fallback"}),
                ]
            ]

        def list_projections(self, *, account_id, only_active):
            assert only_active is True
            return self.rows if self.active and account_id == "account-copy" else []

    class DB:
        extension_runtime_store = Store()

        def get_agent(self, _):
            return None

        def get_agent_skills(self, _):
            return []

    db = DB()
    before = deepcopy(db.extension_runtime_store.rows)
    rows = projected_artifact_rows(db, "agent", "agent_id", account_id="account-copy")
    assert rows[0]["base_skill"] == expected
    assert (
        agent_primary_skill_id(db, "extension.install-123.copywriter", account_id="account-copy")
        == expected
    )
    other_expected = "extension.install-456.write_copy" if base_skill == "write_copy" else expected
    assert rows[1]["base_skill"] == other_expected
    assert (
        agent_primary_skill_id(db, "extension.install-456.copywriter", account_id="account-copy")
        == other_expected
    )
    AgentRecordView.model_validate(rows[0])
    assert db.extension_runtime_store.rows == before
    assert (
        agent_primary_skill_id(db, "extension.install-123.copywriter", account_id="foreign") == ""
    )
    db.extension_runtime_store.active = False
    assert (
        agent_primary_skill_id(db, "extension.install-123.copywriter", account_id="account-copy")
        == ""
    )


def test_package_snapshot_cannot_mint_host_dataset_ownership_or_assignments():
    from types import SimpleNamespace

    from core.application.registry.projected_artifact_rows import projection_aware_catalog_row

    class Runtime:
        def list_projections(self, **_kwargs):
            return [
                {
                    "account_id": "account-1",
                    "install_id": "install-1",
                    "release_id": "release-1",
                    "artifact_kind": "dataset",
                    "artifact_id": "orders",
                    "projection": {
                        "dataset_id": "orders",
                        "source_install_id": "forged",
                        "source_release_id": "forged",
                        "account_id": "forged",
                        "workspace_id": "forged",
                        "project_ids": ["forged"],
                        "availability_mode": "all_workspace_projects",
                        "metadata_jsonb": {
                            "connection_resolution": "project_scope",
                            "projection_bridge": {"immutable": True, "install_id": "forged"},
                        },
                    },
                }
            ]

    row = projection_aware_catalog_row(
        SimpleNamespace(extension_runtime_store=Runtime()),
        "dataset",
        "dataset_id",
        "extension.install-1.orders",
        None,
        account_id="account-1",
    )
    assert {
        key: row.get(key)
        for key in (
            "source_install_id",
            "source_release_id",
            "account_id",
            "workspace_id",
            "project_ids",
            "availability_mode",
        )
    } | {"projection_bridge": row["metadata_jsonb"].get("projection_bridge")} == {
        "source_install_id": "install-1",
        "source_release_id": "release-1",
        "account_id": "account-1",
        "workspace_id": None,
        "project_ids": [],
        "availability_mode": "specific_projects_only",
        "projection_bridge": None,
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", "foreign"),
        ("install_id", "foreign"),
        ("release_id", "stale"),
        ("artifact_kind", "saved_query"),
        ("artifact_id", "other"),
        ("immutable", False),
    ],
)
def test_dataset_projection_does_not_copy_unmatched_durable_host_fields(field, value):
    from core.application.registry.projected_artifact_rows import projection_aware_catalog_row
    from tests.unit.test_saved_query_project_scope_preview import _RawPackageDB

    db = _RawPackageDB()
    durable = db.get_dataset(db.dataset_id)
    if field == "account_id":
        durable[field] = value
    else:
        durable["metadata_jsonb"]["projection_bridge"][field] = value
    row = projection_aware_catalog_row(
        db, "dataset", "dataset_id", db.dataset_id, durable, account_id="account-1"
    )
    assert row["display_name"] == "Package orders"
    assert row.get("project_ids", []) == []
    assert "projection_bridge" not in row["metadata_jsonb"]
