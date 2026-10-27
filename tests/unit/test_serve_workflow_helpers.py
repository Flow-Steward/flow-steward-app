from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from core.application.workflows import serve_workflow_helpers

FOREIGN_ACCOUNT_ID = "11111111-1111-1111-1111-111111111111"


def test_default_agent_models_are_registry_driven() -> None:
    registry = SimpleNamespace(
        agents=SimpleNamespace(
            list_all=lambda: [
                SimpleNamespace(agent_id="custom_researcher", enabled=True),
                SimpleNamespace(agent_id="delivery_bot", enabled=True),
                SimpleNamespace(agent_id="disabled_agent", enabled=False),
            ]
        )
    )
    result = serve_workflow_helpers.default_agent_models(
        "gpt-4.1-nano",
        registry_manager_factory=lambda: registry,
    )

    assert result == {
        "custom_researcher": {"provider": "", "model": ""},
        "delivery_bot": {"provider": "", "model": ""},
    }


def test_agent_model_override_targets_do_not_require_model_catalog_connection(monkeypatch) -> None:
    monkeypatch.setattr(
        serve_workflow_helpers,
        "default_agent_models",
        lambda *_args, **_kwargs: {
            "custom_researcher": {"provider": "", "model": ""},
            "delivery_bot": {"provider": "", "model": ""},
        },
    )
    monkeypatch.setattr(
        serve_workflow_helpers,
        "resolve_model_selection_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("override target discovery must not load model options")
        ),
    )
    targets = serve_workflow_helpers.agent_model_override_targets("gpt-4.1-nano")

    assert targets == ["custom_researcher", "delivery_bot"]


def test_list_workflow_choices_returns_empty_without_db_store() -> None:
    logger = MagicMock()

    result = serve_workflow_helpers.list_workflow_choices(
        logger=logger,
        registry_manager_factory=lambda: SimpleNamespace(db_store=None),
    )

    assert result == []
    logger.debug.assert_not_called()


def test_list_workflow_choices_reads_registry_store_when_available() -> None:
    db_store = MagicMock()
    db_store.list_workflows.return_value = [{"workflow_id": "wf-1"}]

    result = serve_workflow_helpers.list_workflow_choices(
        enabled_only=False,
        logger=MagicMock(),
        registry_manager_factory=lambda: SimpleNamespace(db_store=db_store),
    )

    assert result == [{"workflow_id": "wf-1"}]
    db_store.list_workflows.assert_called_once_with(
        enabled_only=False,
        include_definitions=False,
    )


def test_list_workflow_choices_includes_projected_extension_workflows(monkeypatch) -> None:
    db_store = MagicMock()
    db_store.list_workflows.return_value = [{"workflow_id": "platform-workflow"}]
    monkeypatch.setattr(
        "core.application.registry.projected_artifact_rows.projected_artifact_rows",
        lambda store, kind, id_key, **kwargs: [
            {
                "workflow_id": "extension.install-1.demo-workflow",
                "enabled": True,
                "visible_in_catalog": True,
            }
        ],
    )

    result = serve_workflow_helpers.list_workflow_choices(
        enabled_only=True,
        logger=MagicMock(),
        registry_manager_factory=lambda: SimpleNamespace(db_store=db_store),
    )

    assert [row["workflow_id"] for row in result] == [
        "platform-workflow",
        "extension.install-1.demo-workflow",
    ]


def test_list_workflow_choices_excludes_inactive_durable_projection_rows(monkeypatch) -> None:
    db_store = MagicMock()
    db_store.list_workflows.return_value = [
        {"workflow_id": "platform-workflow", "enabled": True},
        {
            "workflow_id": "extension.install-disabled.demo-workflow",
            "enabled": True,
            "source_install_id": "install-disabled",
        },
    ]
    monkeypatch.setattr(
        "core.application.registry.projected_artifact_rows.projected_artifact_rows",
        lambda store, kind, id_key, **kwargs: [],
    )

    result = serve_workflow_helpers.list_workflow_choices(
        enabled_only=True,
        logger=MagicMock(),
        registry_manager_factory=lambda: SimpleNamespace(db_store=db_store),
    )

    assert [row["workflow_id"] for row in result] == ["platform-workflow"]


def test_list_workflow_choices_rejects_foreign_account_before_registry_access() -> None:
    registry_manager_factory = MagicMock()

    result = serve_workflow_helpers.list_workflow_choices(
        logger=MagicMock(),
        account_id=FOREIGN_ACCOUNT_ID,
        registry_manager_factory=registry_manager_factory,
    )

    assert result == []
    registry_manager_factory.assert_not_called()
