from __future__ import annotations


def _action(action_id: str, *, background: bool = False) -> dict:
    contract: dict = {"serialization": {"resource": "inbound_updates"}}
    if background:
        contract["background"] = {
            "kind": "connection_poll",
            "interval_field": "polling_interval_seconds",
            "mode_field": "inbound_mode",
            "transport_policy": "webhook_preferred",
        }
    return {"action_id": action_id, "contract": contract}


def test_background_polling_is_declared_by_contract_not_action_name() -> None:
    from core.infrastructure.runtime.worker.worker_inbound_polling import (
        _background_poll_actions,
    )

    extension = {
        "action_definitions": [
            {"action_id": "poll_now"},
            {
                **_action("consume_updates", background=True),
                "handler": {
                    "config": {"runtime_resources": {"provider_secrets": ["access_token"]}}
                },
            },
        ]
    }

    declarations = _background_poll_actions(extension)
    assert [row["action_id"] for row in declarations] == ["consume_updates"]
    assert declarations[0]["required_provider_secrets"] == ["access_token"]


def test_shared_resource_serializes_different_actions_per_connection() -> None:
    from core.infrastructure.extension_runtime.extension_operation_lock import (
        _serialization_key,
    )

    extension = {
        "extension_key": "fixture.messaging",
        "action_definitions": [_action("manual_refresh"), _action("consume_updates")],
    }
    common = {
        "extension_install_id": "install-1",
        "project_id": "project-1",
        "connection_id": "connection-1",
    }
    manual = _serialization_key(
        extension,
        action_id="manual_refresh",
        input_payload={},
        runtime_context=common,
    )
    background = _serialization_key(
        extension,
        action_id="consume_updates",
        input_payload={},
        runtime_context=common,
    )
    independent = _serialization_key(
        extension,
        action_id="consume_updates",
        input_payload={},
        runtime_context={**common, "connection_id": "connection-2"},
    )

    assert manual == background
    assert independent != background


def test_same_action_name_without_serialization_has_no_lock_authority() -> None:
    from core.infrastructure.extension_runtime.extension_operation_lock import (
        _serialization_key,
    )

    assert (
        _serialization_key(
            {"extension_key": "fixture", "action_definitions": [{"action_id": "poll_now"}]},
            action_id="poll_now",
            input_payload={},
            runtime_context={},
        )
        == ""
    )


def test_declared_serialization_without_connection_fails_closed_before_runtime() -> None:
    from unittest.mock import patch

    from core.infrastructure.extension_runtime.extension_runtime import run_extension_action

    extension = {
        "extension_key": "fixture.messaging",
        "runtime": {"contract_version": "extension_host_v2"},
        "action_definitions": [
            {**_action("consume_updates", background=True), "handler": {"mode": "extension"}}
        ],
    }
    with patch(
        "core.infrastructure.extension_runtime.extension_runtime_entrypoints.run_extension_invocation"
    ) as invoke:
        result = run_extension_action(
            extension,
            action_id="consume_updates",
            runtime_context={
                "extension_install_id": "install-1",
                "connection_id": "connection-1",
            },
        )

    assert result["detail"] == "operation_serialization_unavailable"
    assert result["response"]["definitely_no_external_effect"] is True
    invoke.assert_not_called()


def test_compiler_rejects_unknown_background_kind_and_serialization_typo() -> None:
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    manifest = {
        "manifest_version": 2,
        "manifest_path": "/tmp/extension.yaml",
        "features": ["notifications"],
        "healthcheck": {"command": ["python3", "health.py"]},
        "ui_manifest_ref": "ui/ui_manifest.yaml",
        "ui_manifest": {"version": "1", "pages": []},
        "action_definitions": [
            {
                "action_id": "consume_updates",
                "contract": {
                    "serialisation": {"resource": "inbound_updates"},
                    "background": {
                        "kind": "typo_poll",
                        "interval_field": "polling_interval_seconds",
                        "mode_field": "inbound_mode",
                        "transport_policy": "webhook_preferred",
                    },
                },
            }
        ],
    }

    result = validate_compiled_manifest(manifest)

    assert result["status"] == "invalid"
    assert "invalid_action_runtime_contract" in {
        str(issue.get("code") or "") for issue in result["errors"]
    }


def test_compiler_accepts_connection_poll_without_provider_secret() -> None:
    from core.infrastructure.extension_runtime.extension_runtime import validate_compiled_manifest

    manifest = {
        "manifest_version": 2,
        "manifest_path": "/tmp/extension.yaml",
        "features": ["notifications"],
        "healthcheck": {"command": ["python3", "health.py"]},
        "ui_manifest_ref": "ui/ui_manifest.yaml",
        "ui_manifest": {"version": "1", "pages": []},
        "action_definitions": [
            {
                **_action("oauth_poll", background=True),
                "handler": {"mode": "extension", "config": {"runtime_resources": {}}},
            }
        ],
    }

    assert validate_compiled_manifest(manifest)["status"] == "valid"
