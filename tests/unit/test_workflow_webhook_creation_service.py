from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.application.workflows import workflow_webhook_creation_service as creation_module
from core.application.workflows.workflow_webhook_creation_service import (
    WorkflowWebhookCreateCommand,
    WorkflowWebhookCreationError,
    WorkflowWebhookCreationService,
    WorkflowWebhookMutationService,
    WorkflowWebhookUpdateCommand,
    WorkflowWebhookUpdateService,
    WorkflowWebhookValidationError,
)


@pytest.fixture(autouse=True)
def _unit_transaction_storage(monkeypatch):
    """Command tests supply locked rows; real owner/rollback tests use PostgreSQL."""

    @contextmanager
    def transaction(*_args, **_kwargs):
        yield MagicMock()

    monkeypatch.setattr(creation_module, "workflow_trigger_transaction", transaction)
    monkeypatch.setattr(
        creation_module, "guard_project_credential_versions", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        creation_module,
        "lock_entrypoint",
        lambda *_args, **_kwargs: {
            **_fields(),
            "project_id": "project-1",
            "workflow_id": "workflow-1",
            "entrypoint_id": "entrypoint-1",
        },
    )


def _fields() -> dict[str, object]:
    return {
        "entry_key": "incoming",
        "enabled": True,
        "auth_mode": "bearer_token",
        "auth_config_jsonb": {"secret_ref": "workflow_entrypoint.incoming.secret"},
        "source_restrictions_jsonb": {},
        "allowed_content_types_jsonb": ["application/json"],
        "max_body_bytes": 1024,
        "max_file_bytes": 1024,
        "max_files": 0,
        "secret_ref": "workflow_entrypoint.incoming.secret",
        "idempotency_mode": "none",
        "input_mapping_jsonb": {},
        "response_mode": "accepted",
        "description": "",
    }


def _create_command() -> WorkflowWebhookCreateCommand:
    return WorkflowWebhookCreateCommand.from_request(
        workflow_id="shared-workflow",
        project_id="project-1",
        entry_key="incoming",
        request={
            "auth_mode": "bearer_token",
            "allowed_content_types": ["application/json"],
            "max_body_bytes": 1024,
            "max_file_bytes": 1024,
            "max_files": 0,
        },
        expected_input_names=set(),
    )


def test_create_uses_project_account_instead_of_workflow_manifest_account() -> None:
    create_entrypoint = MagicMock(side_effect=lambda _bl, **kwargs: kwargs)
    bl = SimpleNamespace(
        conn=object(),
        get_project=lambda _project_id: {"id": "project-1", "account_id": "tenant-account"},
        save_project_secret=MagicMock(return_value=True),
    )
    service = WorkflowWebhookCreationService(
        resolve_project_binding=lambda *_args: ("project-1", {}),
        create_entrypoint=create_entrypoint,
    )

    created, secret = service.create(
        bl=bl,
        command=_create_command(),
    )

    assert created["account_id"] == "tenant-account"
    assert secret


def test_create_keeps_secret_and_entrypoint_in_one_transaction(monkeypatch) -> None:
    state = {"entered": False, "rolled_back": False}

    @contextmanager
    def _transaction(_conn, *_args, **_kwargs):
        state["entered"] = True
        try:
            yield MagicMock()
        except Exception:
            state["rolled_back"] = True
            raise

    monkeypatch.setattr(creation_module, "workflow_trigger_transaction", _transaction)
    bl = SimpleNamespace(
        conn=object(),
        get_project=lambda _project_id: {"id": "project-1", "account_id": "tenant-account"},
        save_project_secret=MagicMock(return_value=True),
    )
    service = WorkflowWebhookCreationService(
        resolve_project_binding=lambda *_args: ("project-1", {}),
        create_entrypoint=MagicMock(side_effect=RuntimeError("insert failed")),
    )

    _raises_input_100_1 = _create_command()
    with pytest.raises(RuntimeError, match="insert failed"):
        service.create(bl=bl, command=_raises_input_100_1)

    assert state == {"entered": True, "rolled_back": True}


def test_create_requires_transaction_capable_gateway() -> None:
    bl = SimpleNamespace(
        get_project=lambda _project_id: {"id": "project-1", "account_id": "tenant-account"},
        save_project_secret=MagicMock(return_value=True),
    )
    service = WorkflowWebhookCreationService(
        resolve_project_binding=lambda *_args: ("project-1", {}),
        create_entrypoint=MagicMock(),
    )

    _raises_input_119_1 = _create_command()
    with pytest.raises(WorkflowWebhookCreationError, match="transactional storage"):
        service.create(bl=bl, command=_raises_input_119_1)

    bl.save_project_secret.assert_not_called()


def test_create_command_rejects_unsupported_auth_mode_in_application_layer() -> None:
    _raises_input_126_1 = set()
    with pytest.raises(WorkflowWebhookValidationError, match="Unsupported auth_mode"):
        WorkflowWebhookCreateCommand.from_request(
            workflow_id="workflow-1",
            project_id="project-1",
            entry_key="incoming",
            request={"auth_mode": "unsupported"},
            expected_input_names=_raises_input_126_1,
        )


def test_update_keeps_rotated_secret_and_entrypoint_in_one_transaction(monkeypatch) -> None:
    state = {"entered": False, "rolled_back": False}

    @contextmanager
    def _transaction(_conn, *_args, **_kwargs):
        state["entered"] = True
        try:
            yield MagicMock()
        except Exception:
            state["rolled_back"] = True
            raise

    monkeypatch.setattr(creation_module, "workflow_trigger_transaction", _transaction)
    save_secret = MagicMock(return_value=True)
    bl = SimpleNamespace(conn=object(), save_project_secret=save_secret)
    service = WorkflowWebhookUpdateService(
        resolve_project_binding=lambda *_args: ("project-1", {}),
        update_entrypoint=MagicMock(side_effect=RuntimeError("update failed")),
    )
    command = WorkflowWebhookUpdateCommand.from_request(
        workflow_id="workflow-1",
        entry_key="incoming",
        existing={**_fields(), "project_id": "project-1"},
        request={"auth_mode": "hmac_sha256"},
        expected_input_names=set(),
    )

    with pytest.raises(RuntimeError, match="update failed"):
        service.update(bl=bl, command=command)

    assert save_secret.called
    assert state == {"entered": True, "rolled_back": True}


def test_update_rotation_requires_transaction_capable_gateway() -> None:
    save_secret = MagicMock(return_value=True)
    bl = SimpleNamespace(save_project_secret=save_secret)
    service = WorkflowWebhookUpdateService(
        resolve_project_binding=lambda *_args: ("project-1", {}),
        update_entrypoint=MagicMock(),
    )
    command = WorkflowWebhookUpdateCommand.from_request(
        workflow_id="workflow-1",
        entry_key="incoming",
        existing={**_fields(), "project_id": "project-1"},
        request={"auth_mode": "hmac_sha256"},
        expected_input_names=set(),
    )

    with pytest.raises(WorkflowWebhookCreationError, match="transactional storage"):
        service.update(bl=bl, command=command)

    save_secret.assert_not_called()


SIGNED_WEBHOOK_REFERENCE = "workflow_entrypoint.incoming.secret.webhook_signature"
IMPORTED_SIGNING_SECRET = "whsec_provider_issued"  # pragma: allowlist secret
REPLACEMENT_SIGNING_SECRET = "whsec_rotated_by_provider"  # pragma: allowlist secret


def _signed_webhook_auth_config() -> dict[str, object]:
    return {
        "signature": {
            "source": "header",
            "header": "Stripe-Signature",
            "parser": "comma_kv",
            "value_key": "v1",
            "encoding": "hex",
            "allow_multiple": True,
        },
        "timestamp": {
            "source": "signature_param",
            "key": "t",
            "unit": "seconds",
            "tolerance_seconds": 300,
        },
        "signed_payload": {
            "parts": [
                {"source": "timestamp"},
                {"literal": "."},
                {"source": "raw_body"},
            ]
        },
    }


def _signed_webhook_create_request(**overrides: object) -> dict[str, object]:
    request: dict[str, object] = {
        "auth_mode": "webhook_signature",
        "auth_config": _signed_webhook_auth_config(),
        "allowed_content_types": ["application/json"],
        "max_body_bytes": 1024,
        "max_file_bytes": 1024,
        "max_files": 0,
    }
    request.update(overrides)
    return request


def _signed_webhook_bl(save_secret: MagicMock) -> SimpleNamespace:
    return SimpleNamespace(
        conn=object(),
        get_project=lambda _project_id: {"id": "project-1", "account_id": "tenant-account"},
        save_project_secret=save_secret,
    )


def test_signed_webhook_mode_is_accepted_and_normalized() -> None:
    command = WorkflowWebhookCreateCommand.from_request(
        workflow_id="workflow-1",
        project_id="project-1",
        entry_key="incoming",
        request=_signed_webhook_create_request(),
        expected_input_names=set(),
    )

    assert command.auth_mode == "webhook_signature"
    assert command.fields["secret_ref"] == SIGNED_WEBHOOK_REFERENCE
    assert command.auth_config == {
        "signature": {
            "source": "header",
            "header": "Stripe-Signature",
            "parser": "comma_kv",
            "encoding": "hex",
            "value_key": "v1",
            "allow_multiple": True,
        },
        "timestamp": {
            "source": "signature_param",
            "unit": "seconds",
            "tolerance_seconds": 300,
            "key": "t",
        },
        "signed_payload": {
            "parts": [
                {"source": "timestamp"},
                {"literal": "."},
                {"source": "raw_body"},
            ]
        },
        "secret_ref": SIGNED_WEBHOOK_REFERENCE,
    }
    assert "signing_secret" not in command.fields
    assert "signing_secret" not in command.auth_config


@pytest.mark.parametrize(
    "auth_config",
    [
        {},
        {
            "signature": {"header": "X-Sig", "parser": "rot13"},
            "signed_payload": {"parts": [{"source": "raw_body"}]},
        },
        {
            "signature": {"header": "X-Sig", "parser": "raw", "encoding": "base32"},
            "signed_payload": {"parts": [{"source": "raw_body"}]},
        },
        {
            "signature": {"header": "X-Sig", "parser": "raw"},
            "signed_payload": {"parts": [{"source": "timestamp"}]},
        },
        {
            "signature": {"header": "X-Sig", "parser": "raw"},
            "signed_payload": {
                "parts": [{"source": "raw_body"}, {"source": "raw_body"}],
            },
        },
    ],
)
def test_invalid_signed_webhook_config_is_rejected(auth_config: dict[str, object]) -> None:
    _raises_input_307_1 = _signed_webhook_create_request(auth_config=auth_config)
    _raises_input_307_2 = set()
    with pytest.raises(WorkflowWebhookValidationError):
        WorkflowWebhookCreateCommand.from_request(
            workflow_id="workflow-1",
            project_id="project-1",
            entry_key="incoming",
            request=_raises_input_307_1,
            expected_input_names=_raises_input_307_2,
        )


@pytest.mark.parametrize("selector", ["headers.Stripe-Signature", "headers.stripe-signature"])
def test_configured_signature_header_is_blocked_from_mapping_selectors(selector: str) -> None:
    _raises_input_319_1 = _signed_webhook_create_request(input_mapping={"title": selector})
    with pytest.raises(WorkflowWebhookValidationError, match="Unsupported input mapping selectors"):
        WorkflowWebhookCreateCommand.from_request(
            workflow_id="workflow-1",
            project_id="project-1",
            entry_key="incoming",
            request=_raises_input_319_1,
            expected_input_names={"title"},
        )


def test_configured_timestamp_header_is_blocked_from_mapping_selectors() -> None:
    auth_config = {
        "signature": {
            "source": "header",
            "header": "X-Slack-Signature",
            "parser": "prefixed",
            "prefix": "v0=",
            "encoding": "hex",
        },
        "timestamp": {
            "source": "header",
            "header": "X-Slack-Request-Timestamp",
            "unit": "seconds",
            "tolerance_seconds": 300,
        },
        "signed_payload": {
            "parts": [
                {"literal": "v0:"},
                {"source": "timestamp"},
                {"literal": ":"},
                {"source": "raw_body"},
            ]
        },
    }

    _raises_input_354_1 = _signed_webhook_create_request(
        auth_config=auth_config, input_mapping={"title": "headers.x-slack-request-timestamp"}
    )
    with pytest.raises(WorkflowWebhookValidationError, match="Unsupported input mapping selectors"):
        WorkflowWebhookCreateCommand.from_request(
            workflow_id="workflow-1",
            project_id="project-1",
            entry_key="incoming",
            request=_raises_input_354_1,
            expected_input_names={"title"},
        )


def test_create_generates_a_revealable_secret_when_none_is_imported() -> None:
    save_secret = MagicMock(return_value=True)
    service = WorkflowWebhookCreationService(
        resolve_project_binding=lambda *_args: ("project-1", {}),
        create_entrypoint=MagicMock(side_effect=lambda _bl, **kwargs: kwargs),
    )
    command = WorkflowWebhookCreateCommand.from_request(
        workflow_id="workflow-1",
        project_id="project-1",
        entry_key="incoming",
        request=_signed_webhook_create_request(),
        expected_input_names=set(),
    )

    _created, secret = service.create(bl=_signed_webhook_bl(save_secret), command=command)

    assert secret
    assert save_secret.call_args.kwargs["secret_value"] == secret


def test_create_stores_an_imported_signing_secret_without_revealing_it() -> None:
    save_secret = MagicMock(return_value=True)
    service = WorkflowWebhookCreationService(
        resolve_project_binding=lambda *_args: ("project-1", {}),
        create_entrypoint=MagicMock(side_effect=lambda _bl, **kwargs: kwargs),
    )
    command = WorkflowWebhookCreateCommand.from_request(
        workflow_id="workflow-1",
        project_id="project-1",
        entry_key="incoming",
        request=_signed_webhook_create_request(
            signing_secret=f"  {IMPORTED_SIGNING_SECRET}  ",
        ),
        expected_input_names=set(),
    )

    created, secret = service.create(bl=_signed_webhook_bl(save_secret), command=command)

    assert secret == ""
    assert save_secret.call_args.kwargs == {
        "project_id": "project-1",
        "secret_key": SIGNED_WEBHOOK_REFERENCE,
        "secret_value": IMPORTED_SIGNING_SECRET,
    }
    assert IMPORTED_SIGNING_SECRET not in str(created)


@pytest.mark.parametrize(
    "mode", ["bearer_token", "shared_secret_header", "basic_auth", "hmac_sha256"]
)
def test_imported_signing_secret_is_rejected_outside_the_signed_webhook_mode(mode: str) -> None:
    _raises_input_418_1 = set()
    with pytest.raises(WorkflowWebhookValidationError, match="only supported for auth_mode"):
        WorkflowWebhookCreateCommand.from_request(
            workflow_id="workflow-1",
            project_id="project-1",
            entry_key="incoming",
            request={"auth_mode": mode, "signing_secret": IMPORTED_SIGNING_SECRET},
            expected_input_names=_raises_input_418_1,
        )


def test_update_rejects_an_imported_secret_when_switching_away_from_signed_webhook() -> None:
    _raises_input_429_1 = _signed_webhook_existing()
    _raises_input_429_2 = set()
    with pytest.raises(WorkflowWebhookValidationError, match="only supported for auth_mode"):
        WorkflowWebhookUpdateCommand.from_request(
            workflow_id="workflow-1",
            entry_key="incoming",
            existing=_raises_input_429_1,
            request={"auth_mode": "bearer_token", "signing_secret": IMPORTED_SIGNING_SECRET},
            expected_input_names=_raises_input_429_2,
        )


def test_update_rejects_an_imported_secret_for_an_inherited_non_signed_mode() -> None:
    _raises_input_440_1 = {**_fields(), "project_id": "project-1"}
    _raises_input_440_2 = set()
    with pytest.raises(WorkflowWebhookValidationError, match="only supported for auth_mode"):
        WorkflowWebhookUpdateCommand.from_request(
            workflow_id="workflow-1",
            entry_key="incoming",
            existing=_raises_input_440_1,
            request={"signing_secret": IMPORTED_SIGNING_SECRET},
            expected_input_names=_raises_input_440_2,
        )


@pytest.mark.parametrize("value", ["a" * 4097, "line-one\nline-two", "tab\tseparated"])
def test_imported_signing_secret_is_validated(value: str) -> None:
    _raises_input_452_1 = _signed_webhook_create_request(signing_secret=value)
    _raises_input_452_2 = set()
    with pytest.raises(WorkflowWebhookValidationError, match="signing_secret"):
        WorkflowWebhookCreateCommand.from_request(
            workflow_id="workflow-1",
            project_id="project-1",
            entry_key="incoming",
            request=_raises_input_452_1,
            expected_input_names=_raises_input_452_2,
        )


def _signed_webhook_existing() -> dict[str, object]:
    return {
        **_fields(),
        "project_id": "project-1",
        "auth_mode": "webhook_signature",
        "auth_config_jsonb": {
            **_signed_webhook_auth_config(),
            "secret_ref": SIGNED_WEBHOOK_REFERENCE,
        },
        "secret_ref": SIGNED_WEBHOOK_REFERENCE,
    }


def test_update_replaces_an_imported_secret_without_revealing_it(monkeypatch) -> None:
    monkeypatch.setattr(
        creation_module, "lock_entrypoint", lambda *_args, **_kwargs: _signed_webhook_existing()
    )
    save_secret = MagicMock(return_value=True)
    bl = SimpleNamespace(conn=object(), save_project_secret=save_secret)
    service = WorkflowWebhookUpdateService(
        resolve_project_binding=lambda *_args: ("project-1", {}),
        update_entrypoint=MagicMock(side_effect=lambda _bl, **kwargs: kwargs),
    )
    command = WorkflowWebhookUpdateCommand.from_request(
        workflow_id="workflow-1",
        entry_key="incoming",
        existing=_signed_webhook_existing(),
        request={"signing_secret": REPLACEMENT_SIGNING_SECRET},
        expected_input_names=set(),
    )

    _updated, secret = service.update(bl=bl, command=command)

    assert command.rotate_secret is False
    assert secret is None
    assert save_secret.call_args.kwargs["secret_value"] == REPLACEMENT_SIGNING_SECRET


def test_update_of_non_secret_settings_preserves_an_imported_secret(monkeypatch) -> None:
    monkeypatch.setattr(
        creation_module, "lock_entrypoint", lambda *_args, **_kwargs: _signed_webhook_existing()
    )
    save_secret = MagicMock(return_value=True)
    bl = SimpleNamespace(conn=object(), save_project_secret=save_secret)
    service = WorkflowWebhookUpdateService(
        resolve_project_binding=lambda *_args: ("project-1", {}),
        update_entrypoint=MagicMock(side_effect=lambda _bl, **kwargs: kwargs),
    )
    changed = _signed_webhook_auth_config()
    changed["signature"]["header"] = "X-Renamed-Signature"
    command = WorkflowWebhookUpdateCommand.from_request(
        workflow_id="workflow-1",
        entry_key="incoming",
        existing=_signed_webhook_existing(),
        request={"auth_config": changed, "description": "renamed header"},
        expected_input_names=set(),
    )

    updated, secret = service.update(bl=bl, command=command)

    assert secret is None
    save_secret.assert_not_called()
    assert updated["auth_config_jsonb"]["signature"]["header"] == "X-Renamed-Signature"
    assert updated["secret_ref"] == SIGNED_WEBHOOK_REFERENCE


def test_update_rotation_still_generates_a_revealable_secret(monkeypatch) -> None:
    monkeypatch.setattr(
        creation_module, "lock_entrypoint", lambda *_args, **_kwargs: _signed_webhook_existing()
    )
    save_secret = MagicMock(return_value=True)
    bl = SimpleNamespace(conn=object(), save_project_secret=save_secret)
    service = WorkflowWebhookUpdateService(
        resolve_project_binding=lambda *_args: ("project-2", {}),
        update_entrypoint=MagicMock(side_effect=lambda _bl, **kwargs: kwargs),
    )
    command = WorkflowWebhookUpdateCommand.for_rotation(
        workflow_id="workflow-1",
        entry_key="incoming",
        existing=_signed_webhook_existing(),
        project_id="project-1",
        expected_input_names=set(),
    )

    _updated, secret = service.update(bl=bl, command=command)

    assert secret
    assert save_secret.call_args.kwargs["secret_value"] == secret


def test_webhook_mutation_service_owns_regenerate_and_delete_writes(monkeypatch) -> None:
    monkeypatch.setattr(
        "core.application.workflows.workflow_entrypoint_repository.load_workflow_entrypoint",
        lambda *_args, **_kwargs: {"workflow_id": "workflow-1"},
    )
    monkeypatch.setattr(
        "core.application.workflows.workflow_credential_retirement.retire_entrypoint_credentials",
        lambda *_args, **_kwargs: None,
    )
    regenerate = MagicMock(return_value={"entry_key": "next-key"})
    delete = MagicMock(return_value=True)
    service = WorkflowWebhookMutationService(
        regenerate_entry_key=regenerate,
        delete_entrypoint=delete,
    )

    updated = service.regenerate(bl=object(), entry_key="old-key", next_entry_key="next-key")
    deleted = service.delete(bl=object(), entry_key="next-key")

    assert updated == {"entry_key": "next-key"}
    assert deleted is True
    regenerate.assert_called_once()
    delete.assert_called_once()
