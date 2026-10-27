from __future__ import annotations

import base64
import hashlib
import hmac
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def test_fetch_provider_attachments_uses_extension_and_decodes_content():
    from core.application.extensions.provider_attachments import fetch_provider_attachments

    bl = MagicMock()
    bl.get_extension.return_value = {
        "extension_key": "sample_tasks",
        "enabled": True,
        "bundle_root": "/tmp/extensions/sample_tasks",
        "entrypoint": {"command": ["python3", "main.py"]},
    }
    bl.get_project_connection.return_value = {
        "connection_id": "conn_sample_tasks",
        "project_id": 7,
        "extension_id": "sample_tasks",
        "enabled": True,
        "connection_config": {"list_id": "project-list"},
        "secret_refs": ["api_token"],
    }
    bl.get_provider_secret.return_value = "project-token"

    encoded = base64.b64encode(b"data").decode("ascii")
    with patch(
        "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
        return_value={
            "status": "succeeded",
            "response": {
                "ok": True,
                "result": {
                    "attachments": [
                        {
                            "attachment_id": "att-1",
                            "filename": "spec.png",
                            "mime_type": "image/png",
                            "url": "https://attachments.sample_tasks.com/spec.png",
                            "content_base64": encoded,
                        }
                    ]
                },
            },
        },
    ) as invoke:
        rows = fetch_provider_attachments(
            "sample_tasks",
            attachments=[
                {
                    "id": "att-1",
                    "title": "spec.png",
                    "url": "https://attachments.sample_tasks.com/spec.png",
                    "mimetype": "image/png",
                    "size": 4,
                }
            ],
            project_config={"project_id": 7},
            bl=bl,
            project_id=7,
            allowed_mimes={"image/png"},
            allowed_extensions={".png"},
        )

    assert rows[0]["filename"] == "spec.png"
    assert rows[0]["content"] == b"data"
    input_payload = invoke.call_args.kwargs["input_payload"]
    assert input_payload["provider"]["provider_config"]["api_token"] == "project-token"


def test_fetch_provider_attachments_applies_project_state_updates(monkeypatch):
    from core.application.extensions import response_envelope
    from core.application.extensions.provider_attachments import fetch_provider_attachments

    bl = MagicMock()
    bl.get_extension.return_value = {
        "extension_key": "sample_tasks",
        "install_id": "install-sample-tasks",
        "account_id": "account-1",
        "enabled": True,
        "bundle_root": "/tmp/extensions/sample_tasks",
        "entrypoint": {"command": ["python3", "main.py"]},
        "compiled_manifest": {
            "project_state": {"writes": [{"namespace": "attachments", "state_key": "last_fetch"}]}
        },
    }
    bl.get_project_connection.return_value = {
        "connection_id": "conn_sample_tasks",
        "project_id": 7,
        "account_id": "account-1",
        "extension_id": "sample_tasks",
        "enabled": True,
        "connection_config": {"list_id": "project-list"},
        "secret_refs": [],
    }
    captured: dict[str, object] = {}

    def fake_put_state(*_args, **kwargs):
        captured.update(kwargs)
        return {"revision": 1, **kwargs}

    monkeypatch.setattr(response_envelope.extension_project_state, "put_state", fake_put_state)
    with patch(
        "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
        return_value={
            "status": "succeeded",
            "response": {
                "ok": True,
                "result": {"attachments": []},
                "project_state_updates": [
                    {
                        "namespace": "attachments",
                        "state_key": "last_fetch",
                        "value_jsonb": {"count": 0},
                    }
                ],
            },
        },
    ):
        rows = fetch_provider_attachments(
            "sample_tasks",
            attachments=[{"id": "att-1", "title": "spec.png"}],
            project_config={"project_id": 7},
            bl=bl,
            project_id=7,
        )

    assert rows == []
    assert captured["account_id"] == "account-1"
    assert captured["project_id"] == "7"
    assert captured["extension_id"] == "sample_tasks"
    assert captured["extension_install_id"] == "install-sample-tasks"
    assert captured["connection_id"] == "conn_sample_tasks"
    assert captured["namespace"] == "attachments"


def test_extension_action_response_applies_project_state_updates(monkeypatch):
    from core.application.extensions import response_envelope
    from core.application.extensions.provider_completion import _extension_action_response

    bl = MagicMock()
    bl.get_extension.return_value = {
        "extension_key": "sample_tasks",
        "install_id": "install-sample-tasks",
        "account_id": "account-1",
        "enabled": True,
        "compiled_manifest": {
            "project_state": {"writes": [{"namespace": "sync", "state_key": "last_status"}]}
        },
    }
    captured: dict[str, object] = {}

    def fake_put_state(*_args, **kwargs):
        captured.update(kwargs)
        return {"revision": 1, **kwargs}

    monkeypatch.setattr(response_envelope.extension_project_state, "put_state", fake_put_state)
    with patch(
        "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
        return_value={
            "status": "succeeded",
            "response": {
                "ok": True,
                "result": {"status": "updated"},
                "project_state_updates": [
                    {
                        "namespace": "sync",
                        "state_key": "last_status",
                        "value_jsonb": {"status": "updated"},
                    }
                ],
            },
        },
    ):
        response = _extension_action_response(
            "sample_tasks",
            project_id=7,
            action_id="sync_completion_status",
            input_payload={},
            request_context={
                "account_id": "account-1",
                "connection_id": "conn-sample_tasks",
                "actor_id": "actor-1",
            },
            bl=bl,
        )

    assert response["ok"] is True
    assert captured["account_id"] == "account-1"
    assert captured["project_id"] == "7"
    assert captured["extension_id"] == "sample_tasks"
    assert captured["extension_install_id"] == "install-sample-tasks"
    assert captured["connection_id"] == "conn-sample_tasks"
    assert captured["actor_id"] == "actor-1"


def test_resolve_extension_runtime_config_reads_connection_scoped_secret_refs():
    from core.application.extensions.provider_runtime import resolve_extension_runtime_config

    bl = MagicMock()
    bl.get_extension.return_value = {
        "extension_key": "sample_tasks",
        "account_id": "acc_default",
        "enabled": True,
        "config": {},
    }
    bl.get_project_connection.return_value = {
        "connection_id": "conn_sample_tasks",
        "project_id": 7,
        "account_id": "acc_default",
        "extension_id": "sample_tasks",
        "enabled": True,
        "connection_config": {"list_id": "project-list"},
        "secret_refs": {"refs": {"api_token": "project_connection:conn_sample_tasks:api_token"}},
    }

    with patch(
        "core.application.management.project_connection_secrets.resolve_connection_secrets",
        return_value={"api_token": "connection-token"},
    ) as resolve_secrets:
        resolved = resolve_extension_runtime_config(
            "sample_tasks",
            {"project_id": 7},
            bl=bl,
            project_id=7,
        )

    assert resolved["binding_source"] == "project_connection"
    assert resolved["config"]["api_token"] == "connection-token"
    assert resolved["secret_refs"] == {
        "refs": {"api_token": "project_connection:conn_sample_tasks:api_token"}
    }
    resolve_secrets.assert_called_once_with(
        bl,
        account_id="acc_default",
        secret_refs={"refs": {"api_token": "project_connection:conn_sample_tasks:api_token"}},
    )


def test_resolve_extension_runtime_config_uses_explicit_connection_id():
    from core.application.extensions.provider_runtime import resolve_extension_runtime_config

    bl = MagicMock()
    bl.get_extension.return_value = {
        "extension_key": "sample_tasks",
        "account_id": "acc_default",
        "enabled": True,
        "config": {},
    }
    connections = {
        "conn_primary": {
            "connection_id": "conn_primary",
            "project_id": 7,
            "account_id": "acc_default",
            "extension_id": "sample_tasks",
            "enabled": True,
            "connection_config": {"list_id": "primary-list"},
            "secret_refs": [],
        },
        "conn_secondary": {
            "connection_id": "conn_secondary",
            "project_id": 7,
            "account_id": "acc_default",
            "extension_id": "sample_tasks",
            "enabled": True,
            "connection_config": {"list_id": "secondary-list"},
            "secret_refs": [],
        },
    }

    def get_project_connection(**kwargs):
        connection_id = str(kwargs.get("connection_id") or "")
        if connection_id:
            return connections.get(connection_id)
        return connections["conn_primary"]

    bl.get_project_connection.side_effect = get_project_connection

    resolved = resolve_extension_runtime_config(
        "sample_tasks",
        {"project_id": 7, "connection_id": "conn_secondary"},
        bl=bl,
        project_id=7,
    )

    assert resolved["project_connection"]["connection_id"] == "conn_secondary"
    assert resolved["config"]["list_id"] == "secondary-list"
    assert bl.get_project_connection.call_args.kwargs["connection_id"] == "conn_secondary"


def test_fetch_provider_attachments_raises_normalized_error_code_for_ok_false():
    from core.application.extensions.provider_attachments import fetch_provider_attachments

    bl = MagicMock()
    bl.get_extension.return_value = {
        "extension_key": "sample_tasks",
        "enabled": True,
        "bundle_root": "/tmp/extensions/sample_tasks",
        "entrypoint": {"command": ["python3", "main.py"]},
    }
    bl.get_project_connection.return_value = {
        "connection_id": "conn_sample_tasks",
        "project_id": 7,
        "extension_id": "sample_tasks",
        "enabled": True,
        "connection_config": {"list_id": "project-list"},
        "secret_refs": ["api_token"],
    }
    bl.get_provider_secret.return_value = "project-token"

    with patch(
        "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
        return_value={
            "status": "succeeded",
            "response": {
                "ok": False,
                "error": "missing_api_token",
            },
        },
    ):
        try:
            fetch_provider_attachments(
                "sample_tasks",
                attachments=[
                    {
                        "id": "att-1",
                        "title": "spec.png",
                        "url": "https://attachments.sample_tasks.com/spec.png",
                    }
                ],
                project_config={"project_id": 7},
                bl=bl,
                project_id=7,
            )
        except RuntimeError as exc:
            assert "[missing_credentials]" in str(exc)
        else:
            raise AssertionError("Expected RuntimeError for ok=false attachment response")


def test_verify_hmac_signature_accepts_sha256_prefix():
    from core.application.extensions.provider_webhook_security import verify_hmac_signature

    body = b'{"hello":"world"}'
    secret = "test-secret"
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    assert verify_hmac_signature(body, f"sha256={expected}", secret) is True


def test_sync_provider_completion_status_prefers_extension_action():
    from core.application.extensions.provider_completion import sync_provider_completion_status

    bl = MagicMock()
    bl.conn.cursor.return_value.__enter__.return_value.fetchone.return_value = {
        "id": 17,
        "project_id": 5,
        "task_id": "cu-17",
        "task_source": "provider",
        "meta": {"external_provider_key": "sample_tasks", "external_source_id": "cu-17"},
    }
    bl.get_project_run_config.return_value = {"project_id": 5}
    bl.get_project_connection.return_value = {
        "connection_id": "conn-sample_tasks",
        "project_id": 5,
        "extension_id": "sample_tasks",
        "enabled": True,
        "connection_config": {"api_token": "cfg-token"},
        "secret_refs": [],
    }
    bl.get_extension.return_value = {
        "extension_key": "sample_tasks",
        "enabled": True,
        "action_definitions": [
            {"action_id": "sync_completion_status", "handler": {"mode": "extension"}}
        ],
    }

    with (
        patch("core.infrastructure.runtime.test_mode.is_test_mode_enabled", return_value=False),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
            return_value={"status": "succeeded", "response": {"ok": True, "status": "updated"}},
        ) as run_extension_action,
    ):
        sync_provider_completion_status(17, "completed", bl)

    run_extension_action.assert_called_once()
    input_payload = run_extension_action.call_args.kwargs["input_payload"]
    assert input_payload["task_id"] == "cu-17"
    assert input_payload["final_status"] == "completed"
    assert input_payload["provider_key"] == "sample_tasks"
    assert input_payload["provider_config"]["api_token"] == "cfg-token"


def test_sync_provider_completion_status_uses_provider_key_metadata():
    from core.application.extensions.provider_completion import sync_provider_completion_status

    bl = MagicMock()
    bl.conn.cursor.return_value.__enter__.return_value.fetchone.return_value = {
        "id": 20,
        "project_id": 5,
        "task_id": "cu-20",
        "task_source": "provider",
        "meta": {"provider_key": "sample_tasks", "source_event_id": "cu-20"},
    }
    bl.get_project_run_config.return_value = {"project_id": 5}
    bl.get_project_connection.return_value = {
        "connection_id": "conn-sample_tasks",
        "project_id": 5,
        "extension_id": "sample_tasks",
        "enabled": True,
        "connection_config": {"api_token": "cfg-token"},
        "secret_refs": [],
    }
    bl.get_extension.return_value = {
        "extension_key": "sample_tasks",
        "enabled": True,
        "action_definitions": [
            {"action_id": "sync_completion_status", "handler": {"mode": "extension"}}
        ],
    }

    with (
        patch("core.infrastructure.runtime.test_mode.is_test_mode_enabled", return_value=False),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
            return_value={"status": "succeeded", "response": {"ok": True, "status": "updated"}},
        ) as run_extension_action,
    ):
        sync_provider_completion_status(20, "completed", bl)

    run_extension_action.assert_called_once()
    input_payload = run_extension_action.call_args.kwargs["input_payload"]
    assert input_payload["provider_key"] == "sample_tasks"
    assert input_payload["task_id"] == "cu-20"


def test_sync_provider_completion_status_raises_when_extension_is_unavailable():
    from core.application.extensions.provider_completion import sync_provider_completion_status

    bl = MagicMock()
    bl.conn.cursor.return_value.__enter__.return_value.fetchone.return_value = {
        "id": 18,
        "project_id": 5,
        "task_id": "cu-18",
        "task_source": "provider",
        "meta": {"external_provider_key": "sample_tasks", "external_source_id": "cu-18"},
    }
    bl.get_project_run_config.return_value = {"project_id": 5}
    bl.get_project_connection.return_value = {
        "connection_id": "conn-sample_tasks",
        "project_id": 5,
        "extension_id": "sample_tasks",
        "enabled": True,
        "connection_config": {"api_token": "cfg-token"},
        "secret_refs": [],
    }
    bl.get_extension.return_value = None

    with (
        patch("core.infrastructure.runtime.test_mode.is_test_mode_enabled", return_value=False),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action"
        ) as run_extension_action,
    ):
        try:
            sync_provider_completion_status(18, "completed", bl)
        except RuntimeError as exc:
            assert "Extension 'sample_tasks' is not available" in str(exc)
        else:
            raise AssertionError("Expected RuntimeError when provider extension is unavailable")

    run_extension_action.assert_not_called()


def test_sync_provider_completion_status_skips_internal_invoke_workflow_jobs():
    from core.application.extensions.provider_completion import sync_provider_completion_status

    bl = MagicMock()
    bl.conn.cursor.return_value.__enter__.return_value.fetchone.return_value = {
        "id": 19,
        "project_id": 5,
        "task_id": "parent:invoke:daily_briefing:story_loop:STORY-001",
        "task_source": "invoke_workflow",
        "meta": {
            "invoke_parent_job_id": 17,
            "invoke_child_workflow_id": "daily_briefing",
        },
    }

    with (
        patch("core.infrastructure.runtime.test_mode.is_test_mode_enabled", return_value=False),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action"
        ) as run_extension_action,
    ):
        sync_provider_completion_status(19, "completed", bl)

    bl.get_project_run_config.assert_not_called()
    bl.get_extension.assert_not_called()
    run_extension_action.assert_not_called()


def test_sync_provider_completion_status_skips_manual_ui_web_jobs():
    from core.application.extensions.provider_completion import sync_provider_completion_status

    bl = MagicMock()
    bl.conn.cursor.return_value.__enter__.return_value.fetchone.return_value = {
        "id": 21,
        "project_id": 5,
        "task_id": "manual-task",
        "task_source": "web",
        "meta": {
            "entry_path": "manual_ui",
            "source_system": "manual",
            "source_event_id": "manual-task",
        },
    }

    with (
        patch("core.infrastructure.runtime.test_mode.is_test_mode_enabled", return_value=False),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action"
        ) as run_extension_action,
    ):
        sync_provider_completion_status(21, "awaiting_approval", bl)

    bl.get_project_run_config.assert_not_called()
    bl.get_extension.assert_not_called()
    run_extension_action.assert_not_called()


def test_resolve_extension_runtime_config_ignores_project_config_extensions():
    from core.application.extensions.provider_runtime import resolve_extension_runtime_config

    bl = MagicMock()
    bl.get_extension.return_value = {
        "extension_key": "sample_tasks",
        "enabled": True,
        "config": {"enabled": True},
    }
    bl.get_project_connection.return_value = None

    resolved = resolve_extension_runtime_config(
        "sample_tasks",
        {
            "project_id": 7,
            "extensions": {"sample_tasks": {"api_token": "ignored-token"}},
        },
        bl=bl,
        project_id=7,
    )

    assert resolved["binding_source"] == "extension_config"
    assert resolved["config"].get("api_token") is None


def test_get_project_connection_returns_none_without_db():
    """With no Flow Steward logger (bl=None), JSON config is not used — DB is canonical."""
    from core.application.extensions.provider_runtime import get_project_connection

    connection = get_project_connection(
        {
            "project_connections": [
                {
                    "extension_id": "sample_tasks",
                    "connection_kind": "extension",
                    "enabled": True,
                    "connection_config": {"list_id": "abc"},
                }
            ]
        },
        extension_id="sample_tasks",
    )

    assert connection is None
