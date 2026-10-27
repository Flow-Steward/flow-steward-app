from unittest.mock import patch

import pytest


@pytest.mark.parametrize("provider_type, expected", [("openai", "test-key"), ("anthropic", None)])
def test_embeddings_use_explicit_account_provider(provider_type, expected):
    from core.application.settings.embedding_credentials import resolve_embedding_openai_api_key

    with (
        patch(
            "core.application.settings.embedding_repository.get_system_setting",
            return_value={
                "account_id": "acc-1",
                "setting_value_jsonb": {"provider": "custom:provider-1"},
            },
        ) as setting,
        patch(
            "core.application.orchestration.model_provider._resolve_named_provider",
            return_value={
                "provider_type": provider_type,
                "_decrypted_api_key": "test-key",  # pragma: allowlist secret - intentional test literal
            },
        ) as provider,
    ):
        assert resolve_embedding_openai_api_key(account_id="acc-1", conn=object()) == expected
    assert setting.call_args.kwargs["account_id"] == "acc-1"
    assert setting.call_args.kwargs["setting_key"] == "embedding_settings"
    provider.assert_called_once_with("provider-1", account_id="acc-1")


@pytest.mark.parametrize(
    "row",
    [
        None,
        {},
        {"account_id": "acc-2", "setting_value_jsonb": {"provider": "custom:provider-1"}},
        {"account_id": "acc-1", "setting_value_jsonb": "invalid"},
        {"account_id": "acc-1", "setting_value_jsonb": {"provider": "openai"}},
        {"account_id": "acc-1", "setting_value_jsonb": {"provider": "custom:"}},
    ],
)
def test_embeddings_fail_closed_for_invalid_selection(row, monkeypatch):
    from core.application.settings.embedding_credentials import resolve_embedding_openai_api_key

    monkeypatch.setenv("OPENAI_API_KEY", "ambient-key-must-not-be-used")
    with patch(
        "core.application.settings.embedding_repository.get_system_setting", return_value=row
    ):
        assert resolve_embedding_openai_api_key(account_id="acc-1", conn=object()) is None


@pytest.mark.parametrize("current_selection", [None, {"provider": "custom:current-provider"}])
def test_embeddings_use_only_current_setting_selection(current_selection, monkeypatch):
    from core.application.settings.embedding_credentials import resolve_embedding_openai_api_key

    monkeypatch.setenv("OPENAI_API_KEY", "ambient-key-must-not-be-used")

    def read_setting(*, conn, setting_key, account_id):
        assert conn is connection
        assert account_id == "acc-1"
        settings = {
            "assistant_settings_v1": {"provider": "custom:retired-provider"},
            "embedding_settings": current_selection,
        }[setting_key]
        return {"account_id": account_id, "setting_value_jsonb": settings}

    connection = object()
    with (
        patch(
            "core.application.settings.embedding_repository.get_system_setting",
            side_effect=read_setting,
        ),
        patch(
            "core.application.orchestration.model_provider._resolve_named_provider",
            side_effect=lambda provider_id, **_: {
                "provider_type": "openai",
                "_decrypted_api_key": f"resolved-{provider_id}",
            },
        ),
    ):
        expected = "resolved-current-provider" if current_selection else None
        assert resolve_embedding_openai_api_key(account_id="acc-1", conn=connection) == expected


@pytest.mark.parametrize("failure_owner", ["setting", "provider"])
def test_embeddings_fail_closed_when_storage_or_provider_unavailable(failure_owner):
    from core.application.settings.embedding_credentials import resolve_embedding_openai_api_key

    with (
        patch(
            "core.application.settings.embedding_repository.get_system_setting",
            side_effect=RuntimeError("unavailable") if failure_owner == "setting" else None,
            return_value={
                "account_id": "acc-1",
                "setting_value_jsonb": {"provider": "custom:provider-1"},
            },
        ),
        patch(
            "core.application.orchestration.model_provider._resolve_named_provider",
            side_effect=RuntimeError("unavailable"),
        ),
    ):
        assert resolve_embedding_openai_api_key(account_id="acc-1", conn=object()) is None


@pytest.mark.parametrize("account_id, conn", [("", object()), ("acc-1", None)])
def test_embeddings_require_account_and_connection(account_id, conn):
    from core.application.settings.embedding_credentials import resolve_embedding_openai_api_key

    assert resolve_embedding_openai_api_key(account_id=account_id, conn=conn) is None


@pytest.mark.parametrize(
    "settings", [{"provider": "custom:provider-1"}, '{"provider":"custom:provider-1"}']
)
def test_embedding_repository_reads_only_provider(settings):
    from core.application.settings.embedding_repository import get_embedding_provider

    with patch(
        "core.application.settings.embedding_repository.get_system_setting",
        return_value={"account_id": "acc-1", "setting_value_jsonb": settings},
    ):
        assert get_embedding_provider(account_id="acc-1", conn=object()) == "custom:provider-1"


def test_memory_embedding_consumer_keeps_account_and_connection():
    from types import SimpleNamespace

    from core.application.memory.retrieval import CandidateVectorBackend

    conn = object()
    with (
        patch(
            "core.infrastructure.db.platform_stores.PlatformConnectionStore.get_shared_instance",
            return_value=SimpleNamespace(conn=conn),
        ),
        patch(
            "core.application.settings.embedding_credentials.resolve_embedding_openai_api_key",
            return_value="test-key",
        ) as resolve,
    ):
        assert CandidateVectorBackend._embedding_key("acc-1") == "test-key"
    resolve.assert_called_once_with(account_id="acc-1", conn=conn)


@pytest.mark.parametrize("api_key", [None, "test-key"])
def test_workflow_knowledge_embedding_consumer_preserves_fail_closed_selection(api_key):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from core.infrastructure.db.workflow_knowledge_store_common import (
        PostgresAgeWorkflowKnowledgeStoreBase,
    )

    conn = object()

    @contextmanager
    def connect():
        yield conn

    store = object.__new__(PostgresAgeWorkflowKnowledgeStoreBase)
    store._connect = connect
    with (
        patch(
            "core.infrastructure.db.workflow_knowledge_store_common.resolve_embedding_openai_api_key",
            return_value=api_key,
        ) as resolve,
        patch("openai.OpenAI") as client,
    ):
        client.return_value.embeddings.create.return_value = SimpleNamespace(
            data=[SimpleNamespace(embedding=[0.1, 0.2])]
        )
        assert store._embed_texts(["document"], account_id="acc-1") == (
            [[0.1, 0.2]] if api_key else None
        )
        if api_key:
            client.assert_called_once_with(
                api_key="test-key",  # pragma: allowlist secret - intentional test literal
            )
        else:
            client.assert_not_called()
    resolve.assert_called_once_with(account_id="acc-1", conn=conn)
