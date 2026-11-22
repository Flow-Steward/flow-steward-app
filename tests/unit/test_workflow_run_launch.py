from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.command_services import ProjectCommandService
from core.application.management.errors import ServiceValidationError
from core.application.management.scope import ScopeContext

PROJECT_ID = "00000000-0000-0000-0000-000000000007"


@pytest.mark.parametrize("field", ["inputs", "client_context"])
def test_workflow_run_request_enforces_depth_boundary(field):
    from core.application.management.command_models import WorkflowRunInput

    payload = {}
    for _ in range(31):
        payload = {"nested": payload}
    WorkflowRunInput.model_validate(
        {"project_id": PROJECT_ID, "workflow_id": "stock-import", field: payload}
    )
    with pytest.raises(ValidationError, match="nesting depth"):
        WorkflowRunInput.model_validate(
            {"project_id": PROJECT_ID, "workflow_id": "stock-import", field: {"nested": payload}}
        )


def test_workflow_run_adapter_preserves_retry_identity_and_caller_scope():
    service = ProjectCommandService(get_logger=MagicMock())
    admitted = []

    def capture(command, **kwargs):
        admitted.append((command, kwargs))
        return SimpleNamespace(job_id="91")

    scope = ScopeContext(account_id=DEFAULT_ACCOUNT_ID, actor_id="user-42")
    command = {
        "project_id": PROJECT_ID,
        "workflow_id": "stock-import",
        "inputs": {"supplier": "acme"},
    }
    with patch.object(service, "enqueue_manual_task", side_effect=capture):
        for key in ("retry-42", "retry-42", "different-request", "", ""):
            service.start_mcp_workflow_run(command, idempotency_key=key, scope=scope)

    assert admitted[0][0].task_id == admitted[1][0].task_id
    assert len({admitted[index][0].task_id for index in (0, 2, 3, 4)}) == 4
    for value, kwargs in admitted:
        assert value.entry_path == "mcp"
        assert value.inputs == {"supplier": "acme"}
        assert kwargs["allow_implicit_project"] is False
        assert kwargs["scope"].actor_id == "user-42"
    assert admitted[0][0].idempotency_key == "retry-42"


def test_workflow_run_request_rejects_invalid_project_identity():
    from core.application.management.command_models import WorkflowRunInput

    with pytest.raises(ValidationError, match="project_id"):
        WorkflowRunInput.model_validate(
            {"project_id": "not-a-project-uuid", "workflow_id": "stock-import"}
        )


@pytest.mark.parametrize("field", ["inputs", "client_context"])
def test_workflow_run_request_rejects_oversized_json(field):
    from core.application.management.command_models import WorkflowRunInput

    with pytest.raises(ValidationError, match="JSON size"):
        WorkflowRunInput.model_validate(
            {
                "project_id": PROJECT_ID,
                "workflow_id": "stock-import",
                field: {"note": "x" * 262_145},
            }
        )


@pytest.mark.parametrize("allowed_channel", ["mcp", "api"])
def test_direct_workflow_launch_enforces_mcp_channel_before_queueing(allowed_channel):
    store = MagicMock()
    store.get_project.return_value = {
        "account_id": DEFAULT_ACCOUNT_ID,
        "id": PROJECT_ID,
        "name": "stock",
    }
    store.get_project_run_config.return_value = {
        "project_name": "stock",
        "project_workflows": [{"workflow_id": "stock-import", "enabled": True}],
    }
    queued = []

    def enqueue_task(**kwargs):
        queued.append(kwargs)
        return 91

    service = ProjectCommandService(
        get_logger=lambda: store,
        resolve_workflow_selection=lambda requested, project_workflows=None: (
            requested,
            "explicit_request",
        ),
        validate_explicit_workflow_override=lambda **kwargs: None,
        enqueue_task=enqueue_task,
    )
    registry = SimpleNamespace(
        db_store=SimpleNamespace(
            get_workflow=lambda workflow_id: {
                "settings": {
                    "callable": {
                        "enabled": True,
                        "trigger_channels": [allowed_channel],
                        "input_mode": "passthrough",
                        "output_mode": "none",
                    }
                }
            }
        )
    )
    with patch(
        "core.application.registry.registry_manager.RegistryManager.instance",
        return_value=registry,
    ):
        command = {
            "project_id": PROJECT_ID,
            "workflow_id": "stock-import",
            "inputs": {"supplier": "acme"},
            "client_context": {"session": "voice-42"},
        }
        scope = ScopeContext(account_id=DEFAULT_ACCOUNT_ID, actor_id="user-42")
        if allowed_channel == "api":
            with pytest.raises(ServiceValidationError):
                service.start_mcp_workflow_run(command, scope=scope)
            assert queued == []
        else:
            result = service.start_mcp_workflow_run(command, scope=scope)
            assert result.job_id == "91"
            assert queued[0]["meta"]["entry_path"] == "mcp"
            assert (
                queued[0]["meta"].get("task_summary_key")
                == "react.jobs.generated_workflow_run_summary"
            )
            assert queued[0]["meta"].get("task_summary_params") == {"workflow_id": "stock-import"}
            assert queued[0]["extra_state"]["inputs"] == {"supplier": "acme"}
            assert queued[0]["extra_state"]["client_context"] == {"session": "voice-42"}
            assert "client_context" not in queued[0]["meta"]


@pytest.fixture
def direct_workflow_service(monkeypatch):
    """Keep real admission/contract validation; replace only storage and queue edges."""
    fields = []
    queued = []
    store = MagicMock()
    store.get_project.return_value = {
        "account_id": DEFAULT_ACCOUNT_ID,
        "id": PROJECT_ID,
        "name": "stock",
    }
    store.get_project_run_config.return_value = {
        "project_name": "stock",
        "project_workflows": [{"workflow_id": "stock-import", "enabled": True}],
    }
    workflow = {
        "settings": {
            "callable": {
                "enabled": True,
                "trigger_channels": ["mcp"],
                "input_mode": "fields",
                "expected_inputs": fields,
                "output_mode": "none",
            }
        }
    }
    registry = SimpleNamespace(db_store=SimpleNamespace(get_workflow=lambda _id: workflow))
    monkeypatch.setattr(
        "core.application.registry.registry_manager.RegistryManager.instance", lambda: registry
    )

    def enqueue_task(**kwargs):
        queued.append(kwargs)
        return 91

    service = ProjectCommandService(
        get_logger=lambda: store,
        resolve_workflow_selection=lambda requested, project_workflows=None: (
            requested,
            "explicit_request",
        ),
        validate_explicit_workflow_override=lambda **kwargs: None,
        enqueue_task=enqueue_task,
    )
    return service, fields, queued


@pytest.mark.parametrize("value", ["fake.csv", {"artifact_id": "fabricated"}, None, "", {}, []])
def test_mcp_required_file_rejects_json_before_enqueue(direct_workflow_service, value):
    service, fields, queued = direct_workflow_service
    fields.append({"name": "source_file", "type": "file", "required": True})
    scope = ScopeContext(account_id=DEFAULT_ACCOUNT_ID, actor_id="user-42")
    with pytest.raises(ServiceValidationError) as raised:
        service.start_mcp_workflow_run(
            {
                "project_id": PROJECT_ID,
                "workflow_id": "stock-import",
                "inputs": {"source_file": value},
            },
            scope=scope,
        )
    assert "files" in str(raised.value) or "upload" in str(raised.value)
    assert "source_file" in str(raised.value)
    assert queued == []


@pytest.mark.parametrize("name", ["supplier", "title"])
def test_direct_launch_does_not_fill_missing_required_input_from_job_summary(
    direct_workflow_service, name
):
    service, fields, queued = direct_workflow_service
    fields.append({"name": name, "type": "string", "required": True})
    scope = ScopeContext(account_id=DEFAULT_ACCOUNT_ID, actor_id="user-42")
    with pytest.raises(ServiceValidationError, match="required"):
        service.start_mcp_workflow_run(
            {"project_id": PROJECT_ID, "workflow_id": "stock-import", "inputs": {}},
            scope=scope,
        )
    assert queued == []


def test_direct_launch_enqueues_complete_json_inputs_once(direct_workflow_service):
    service, fields, queued = direct_workflow_service
    fields.append({"name": "supplier", "type": "string", "required": True})
    result = service.start_mcp_workflow_run(
        {
            "project_id": PROJECT_ID,
            "workflow_id": "stock-import",
            "inputs": {"supplier": "acme"},
        },
        scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID, actor_id="user-42"),
    )
    assert result.job_id == "91"
    assert len(queued) == 1
    assert queued[0]["extra_state"]["inputs"] == {"supplier": "acme"}
    assert queued[0]["meta"]["entry_path"] == "mcp"


def test_direct_file_upload_decodes_real_bytes_before_admission():
    import base64

    service = ProjectCommandService(get_logger=MagicMock())
    captured = []

    def capture(command, **kwargs):
        captured.append(command)
        return SimpleNamespace(job_id="91")

    body = b"sku,quantity\nA1,7\n"
    command = {
        "project_id": PROJECT_ID,
        "workflow_id": "stock-import",
        "files": [
            {
                "input_name": "source_file",
                "filename": "actual.csv",
                "mime_type": "text/csv",
                "content_base64": base64.b64encode(body).decode(),
            }
        ],
    }
    with patch.object(service, "enqueue_manual_task", side_effect=capture):
        service.start_mcp_workflow_run(command)
    assert captured[0].files[0].content == body
    assert captured[0].files[0].input_name == "source_file"
    assert "content_base64" not in captured[0].inputs


@pytest.mark.parametrize("encoded", ["not-base64!", "", "YQ==\n"])
def test_direct_file_upload_rejects_invalid_bytes_before_admission(encoded):
    service = ProjectCommandService(get_logger=MagicMock())
    with (
        patch.object(service, "enqueue_manual_task") as enqueue,
        pytest.raises((ValidationError, ServiceValidationError)),
    ):
        service.start_mcp_workflow_run(
            {
                "project_id": PROJECT_ID,
                "workflow_id": "stock-import",
                "files": [
                    {
                        "input_name": "source_file",
                        "filename": "actual.csv",
                        "content_base64": encoded,
                    }
                ],
            }
        )
    enqueue.assert_not_called()


@pytest.mark.parametrize("builder", [False, True])
def test_uploaded_bytes_are_persisted_and_bound_before_dispatch(
    direct_workflow_service, monkeypatch, builder
):
    import base64
    import hashlib
    from contextlib import nullcontext

    service, fields, queued = direct_workflow_service
    fields.append({"name": "source_file", "type": "file", "required": True})
    body = b"sku,quantity\nA1,7\n"
    saved_bytes = []
    prepared = {}
    store = service._get_logger()
    store.get_job.return_value = {"meta": {}}

    def persist(**kwargs):
        content = kwargs["body"]
        saved_bytes.append(content)
        return SimpleNamespace(artifact_id=kwargs["artifact_id"]), {
            "filename": kwargs["filename"],
            "content_type": kwargs["content_type"],
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "object_key": "artifacts/actual.csv",
        }

    def enqueue(**kwargs):
        prepared.update(kwargs["pre_publish_fn"]("91", store))
        queued.append(kwargs)
        return 91

    service._artifact_plane = SimpleNamespace(create_artifact=persist)
    service._enqueue_task = enqueue
    monkeypatch.setattr(
        "core.infrastructure.db.db_pool.transaction_connection", lambda *_a, **_k: nullcontext()
    )
    launch = service.start_mcp_workflow_run
    if builder:
        from core.application.registry.registry_manager import RegistryManager

        workflow = RegistryManager.instance().db_store.get_workflow("stock-import")
        workflow.update(enabled=True, revision=7, account_id=DEFAULT_ACCOUNT_ID)
        workflow["settings"]["callable"]["trigger_channels"] = ["ui"]

        def launch(body):
            body.pop("workflow_id")
            return service.start_builder_workflow_run("stock-import", _builder_request(**body))

    result = launch(
        {
            "project_id": PROJECT_ID,
            "workflow_id": "stock-import",
            "files": [
                {
                    "input_name": "source_file",
                    "filename": "actual.csv",
                    "mime_type": "text/csv",
                    "content_base64": base64.b64encode(body).decode(),
                }
            ],
        }
    )
    assert result.job_id == "91"
    assert saved_bytes == [body]
    descriptor = prepared["inputs"]["source_file"]
    assert descriptor["artifact_id"].startswith("art_")
    assert descriptor["sha256"] == hashlib.sha256(body).hexdigest()
    assert descriptor == prepared["files"][0]
    assert prepared["task_envelope"]["inputs"]["source_file"] == descriptor
    assert "content_base64" not in repr(prepared)


@pytest.mark.parametrize("kind", ["too_many", "too_large", "aggregate", "path", "fake_handle"])
def test_direct_upload_request_rejects_invalid_transport_before_job(kind):
    import base64

    upload = {"input_name": "source_file", "filename": "actual.csv", "content_base64": "YQ=="}
    files = [upload]
    if kind == "too_many":
        files = [{**upload, "input_name": f"file_{i}"} for i in range(11)]
    elif kind == "too_large":
        files = [{**upload, "content_base64": base64.b64encode(b"x" * 1_048_577).decode()}]
    elif kind == "aggregate":
        files = [
            {
                **upload,
                "input_name": f"file_{i}",
                "content_base64": base64.b64encode(b"x" * 600_000).decode(),
            }
            for i in range(2)
        ]
    elif kind == "path":
        files = [{**upload, "content_path": "/etc/passwd"}]
    elif kind == "fake_handle":
        files = [
            {"input_name": "source_file", "filename": "actual.csv", "artifact_id": "fabricated"}
        ]
    service = ProjectCommandService(get_logger=MagicMock())
    with patch.object(service, "enqueue_manual_task") as enqueue, pytest.raises(ValidationError):
        service.start_mcp_workflow_run(
            {"project_id": PROJECT_ID, "workflow_id": "stock-import", "files": files}
        )
    enqueue.assert_not_called()


@pytest.mark.parametrize("binding", ["missing_required", "undeclared", "non_file"])
def test_named_upload_contract_is_rejected_before_queue_or_storage(
    direct_workflow_service, binding
):
    service, fields, queued = direct_workflow_service
    fields.extend(
        [
            {"name": "source_file", "type": "file", "required": True},
            {"name": "supplier", "type": "string", "required": False},
        ]
    )
    create_artifact = MagicMock()
    service._artifact_plane = SimpleNamespace(create_artifact=create_artifact)
    files = []
    if binding != "missing_required":
        files = [
            {
                "input_name": "supplier" if binding == "non_file" else "other_file",
                "filename": "actual.csv",
                "mime_type": "text/csv",
                "content_base64": "YQ==",
            }
        ]
    with pytest.raises(ServiceValidationError) as raised:
        service.start_mcp_workflow_run(
            {"project_id": PROJECT_ID, "workflow_id": "stock-import", "files": files}
        )
    if binding == "missing_required":
        assert raised.value.payload["code"] == "workflow_file_input_required"
    else:
        assert "declared file input" in str(raised.value)
    assert queued == []
    create_artifact.assert_not_called()


def test_builder_launch_uses_normal_ui_admission(direct_workflow_service):
    service, fields, queued = direct_workflow_service
    from core.application.registry.registry_manager import RegistryManager

    workflow = RegistryManager.instance().db_store.get_workflow("stock-import")
    workflow.update(enabled=True, revision=7, account_id=DEFAULT_ACCOUNT_ID)
    workflow["settings"]["callable"]["trigger_channels"] = ["ui"]
    fields.append({"name": "supplier", "type": "string", "required": True})
    result = service.start_builder_workflow_run(
        "stock-import",
        {
            "project_id": PROJECT_ID,
            "expected_revision": 7,
            "acknowledged": True,
            "summary": "Import stock",
            "inputs": {"supplier": "acme"},
        },
        scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID, actor_id="user-42"),
    )
    assert result.job_id == "91"
    assert len(queued) == 1
    assert queued[0]["meta"]["entry_path"] == "manual_ui"
    assert queued[0]["meta"]["expected_workflow_revision"] == 7
    assert queued[0]["test_mode"] is False
    assert queued[0]["task_source"] == "manual_ui"
    assert queued[0]["extra_state"]["inputs"] == {"supplier": "acme"}
    assert "kept_test_outputs" not in repr(queued[0])


@pytest.fixture
def builder_workflow_service(direct_workflow_service):
    from core.application.registry.registry_manager import RegistryManager

    service, fields, queued = direct_workflow_service
    workflow = RegistryManager.instance().db_store.get_workflow("stock-import")
    workflow.update(enabled=True, revision=7, account_id=DEFAULT_ACCOUNT_ID)
    workflow["settings"]["callable"]["trigger_channels"] = ["ui"]
    return service, fields, queued, workflow


def _builder_request(**overrides):
    return {
        "project_id": PROJECT_ID,
        "expected_revision": 7,
        "acknowledged": True,
        "summary": "Import stock",
        **overrides,
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"acknowledged": False},
        {"acknowledged": "true"},
        {"expected_revision": 0},
        {"expected_revision": "7"},
        {"project_id": ""},
        {"summary": " "},
        {"test_mode": False},
        {"job_meta": {}},
        {"extra_state": {}},
        {"task_source": "workflow_test_run"},
        {"entry_path": "mcp"},
        {"client_context": {}},
        {"kept_test_outputs": {}},
        {"files": [{"input_name": "upload", "filename": "a.csv", "content_base64": "!"}]},
        {"files": [{"input_name": "upload", "filename": "a.csv", "artifact_id": "fake"}]},
    ],
)
def test_builder_rejects_unacknowledged_or_spoofed_requests(builder_workflow_service, overrides):
    service, _, queued, _ = builder_workflow_service
    request = _builder_request(**overrides)
    with pytest.raises(ValidationError):
        service.start_builder_workflow_run("stock-import", request)
    assert queued == []


@pytest.mark.parametrize(
    "links",
    [
        [],
        [{"workflow_id": "other", "enabled": True}],
        [{"workflow_id": "stock-import", "enabled": False}],
        [{"workflow_id": "stock-import", "enabled": True, "deleted_at": "2026-10-24"}],
    ],
)
def test_builder_requires_enabled_project_link_even_for_empty_set(builder_workflow_service, links):
    service, _, queued, _ = builder_workflow_service
    service._get_logger().get_project_run_config.return_value["project_workflows"] = links
    request = _builder_request()
    with pytest.raises(ServiceValidationError, match="enabled project workflow link"):
        service.start_builder_workflow_run("stock-import", request)
    assert queued == []


@pytest.mark.parametrize(
    "change",
    [
        "disabled",
        "deleted",
        "mcp_only",
        "missing_ui",
        "account",
        "project_scope",
        "workflow_scope",
        "revision",
        "paused",
        "not_ready",
    ],
)
def test_builder_denies_unavailable_or_out_of_scope_launches(builder_workflow_service, change):
    from core.application.jobs.workflow_definition_snapshot_repository import (
        WorkflowRevisionChangedError,
    )
    from core.application.management.errors import ServiceForbiddenError

    service, _, queued, workflow = builder_workflow_service
    scope = ScopeContext(account_id=DEFAULT_ACCOUNT_ID, actor_id="user-42")
    if change == "disabled":
        workflow["enabled"] = False
    if change == "deleted":
        workflow["deleted_at"] = "2026-10-24"
    if change == "mcp_only":
        workflow["settings"]["callable"]["trigger_channels"] = ["mcp"]
    if change == "missing_ui":
        workflow["settings"]["callable"]["trigger_channels"] = []
    if change == "account":
        workflow["account_id"] = "other"
    if change == "project_scope":
        scope = scope.model_copy(update={"allowed_project_ids": frozenset({"other"})})
    if change == "workflow_scope":
        scope = scope.model_copy(update={"allowed_workflow_ids": frozenset({"other"})})
    if change == "revision":
        workflow["revision"] = 8

    def refuse(**_kwargs):
        raise ServiceValidationError("paused or not ready")

    if change == "paused":
        service._workflow_availability_assert = refuse
    if change == "not_ready":
        service._validate_explicit_workflow_override = refuse
    request = _builder_request()
    with pytest.raises(
        (ServiceValidationError, ServiceForbiddenError, WorkflowRevisionChangedError)
    ):
        service.start_builder_workflow_run("stock-import", request, scope=scope)
    assert queued == []


@pytest.mark.parametrize("name", ["supplier", "title"])
def test_builder_summary_does_not_fill_required_inputs(builder_workflow_service, name):
    service, fields, queued, _ = builder_workflow_service
    fields.append({"name": name, "type": "string", "required": True})
    request = _builder_request()
    with pytest.raises(ServiceValidationError, match="required"):
        service.start_builder_workflow_run("stock-import", request)
    assert queued == []


@pytest.mark.parametrize("value", [None, "fake.csv", {"artifact_id": "fake"}])
def test_builder_requires_actual_upload_even_with_a_test_sample(builder_workflow_service, value):
    service, fields, queued, _ = builder_workflow_service
    fields.append(
        {
            "name": "source",
            "type": "file",
            "required": True,
            "sample_file": {"artifact_id": "sample", "filename": "test.csv"},
        }
    )
    request = _builder_request(inputs={} if value is None else {"source": value})
    with pytest.raises(ServiceValidationError, match="files"):
        service.start_builder_workflow_run("stock-import", request)
    assert queued == []


def test_builder_optional_file_remains_optional(builder_workflow_service):
    service, fields, queued, _ = builder_workflow_service
    fields.append({"name": "source", "type": "file", "required": False})
    service.start_builder_workflow_run("stock-import", _builder_request())
    assert "source" not in queued[0]["extra_state"]["inputs"]
    assert not queued[0]["extra_state"].get("files")


def test_builder_retry_preserves_admission_identity(builder_workflow_service):
    service, _, queued, _ = builder_workflow_service
    # Real admission owns durable idempotency; capture its adapter input at that boundary.
    commands = []

    def capture(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(job_id="91")

    with patch.object(service, "enqueue_manual_task", side_effect=capture):
        for key in ("retry", "retry", "new"):
            service.start_builder_workflow_run(
                "stock-import", _builder_request(), idempotency_key=key
            )
    assert commands[0].task_id == commands[1].task_id
    assert commands[0].task_id != commands[2].task_id
    assert commands[0].idempotency_key == "retry"
    assert queued == []


@pytest.mark.parametrize("foreign_catalog", [False, True])
def test_builder_preserves_editor_catalog_and_workflow_owner_project_scope(
    builder_workflow_service, foreign_catalog
):
    from core.application.management.errors import ServiceForbiddenError

    service, _, queued, workflow = builder_workflow_service
    store = service._get_logger()
    if foreign_catalog:
        workflow["account_id"] = "other-account"
        store.get_project.return_value["account_id"] = "other-account"
        scope = ScopeContext(account_id="other-account")
    else:
        workflow["project_id"] = "other-project"
        selected = dict(store.get_project.return_value)
        store.get_project.side_effect = lambda project_id: {**selected, "id": project_id}
        scope = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, allowed_project_ids=frozenset({PROJECT_ID})
        )
    request = _builder_request()
    with pytest.raises(ServiceForbiddenError):
        service.start_builder_workflow_run("stock-import", request, scope=scope)
    assert queued == []


@pytest.mark.parametrize("kind", ["unknown", "empty_name", "oversized", "aggregate", "too_many"])
def test_builder_rejects_bad_actual_uploads_before_admission(
    builder_workflow_service, kind, monkeypatch
):
    import base64

    # ADR 0075: the browser launch follows the attachment policy; pin it at the
    # sizes this test exercises (1 MiB per run, 10 files).
    monkeypatch.setenv("FS_ATTACHMENT_MAX_SIZE_BYTES", str(1_048_576))
    monkeypatch.setenv("FS_ATTACHMENT_MAX_FILES", "10")
    service, fields, queued, _ = builder_workflow_service
    create_artifact = MagicMock()
    service._artifact_plane = SimpleNamespace(create_artifact=create_artifact)
    fields.append({"name": "source", "type": "file", "required": True})
    upload = {"input_name": "source", "filename": "actual.csv", "content_base64": "YQ=="}
    files = [upload]
    if kind == "unknown":
        files = [{**upload, "input_name": "unknown"}]
    if kind == "empty_name":
        files = [{**upload, "filename": ""}]
    if kind == "oversized":
        files = [{**upload, "content_base64": base64.b64encode(b"x" * 1_048_577).decode()}]
    if kind == "aggregate":
        files = [
            {
                **upload,
                "input_name": f"source_{i}",
                "content_base64": base64.b64encode(b"x" * 600_000).decode(),
            }
            for i in range(2)
        ]
    if kind == "too_many":
        files = [{**upload, "input_name": f"source_{i}"} for i in range(11)]
    request = _builder_request(files=files)
    with pytest.raises((ValidationError, ServiceValidationError)):
        service.start_builder_workflow_run("stock-import", request)
    assert queued == []
    create_artifact.assert_not_called()


def test_builder_can_run_a_linked_global_system_definition(builder_workflow_service):
    service, _, queued, workflow = builder_workflow_service
    workflow.update(account_id=None, scope_kind="system")
    service.start_builder_workflow_run("stock-import", _builder_request())
    assert len(queued) == 1


def _publish(workflow: dict, *, acknowledged: bool = True, **settings) -> None:
    """Publish the workflow to operators the way an admin save leaves it (ADR 0075)."""
    from core.application.registry.workflow_contract import interface_from_definition
    from core.application.registry.workflow_operator_form import contract_fingerprint

    operator = {"published": True, **settings}
    if acknowledged:
        operator["effects_acknowledged"] = {
            "by": "admin-1",
            "at": "2026-11-19T10:00:00+00:00",
            "revision": workflow.get("revision") or 0,
            "contract_sha": contract_fingerprint(interface_from_definition(workflow)),
        }
    workflow["settings"]["operator"] = operator


def _operator(**fields) -> ScopeContext:
    return ScopeContext(
        **{
            "account_id": DEFAULT_ACCOUNT_ID,
            "actor_id": "operator-7",
            "principal_type": "operator",
            "auth_source": "local",
            **fields,
        }
    )


def test_builder_run_records_the_server_owned_author(builder_workflow_service):
    """ADR 0075: the history's author comes from the authenticated scope only."""
    service, _, queued, workflow = builder_workflow_service
    _publish(workflow)

    service.start_builder_workflow_run(
        "stock-import",
        _builder_request(),
        scope=ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, actor_id="operator-7", principal_type="operator"
        ),
    )

    assert queued[0]["meta"]["submitted_by"] == {"id": "operator-7", "kind": "person"}


def test_a_form_cannot_name_the_author(builder_workflow_service):
    """An undeclared input such as `submitted_by` is refused by the strict contract."""
    service, _, queued, _ = builder_workflow_service

    with pytest.raises(ServiceValidationError, match="not declared"):
        service.start_builder_workflow_run(
            "stock-import",
            _builder_request(inputs={"submitted_by": {"id": "someone-else"}}),
            scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID, actor_id="operator-7"),
        )
    assert queued == []


def test_api_run_records_the_same_author_as_the_form(direct_workflow_service):
    from core.application.registry.registry_manager import RegistryManager

    service, fields, queued = direct_workflow_service
    fields.append({"name": "supplier", "type": "string", "required": True})
    workflow = RegistryManager.instance().db_store.get_workflow("stock-import")
    workflow.update(enabled=True, revision=7, account_id=DEFAULT_ACCOUNT_ID)
    workflow["settings"]["callable"]["trigger_channels"] = ["ui", "mcp"]
    _publish(workflow)

    service.start_mcp_workflow_run(
        {"project_id": PROJECT_ID, "workflow_id": "stock-import", "inputs": {"supplier": "a"}},
        scope=ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, actor_id="operator-7", principal_type="operator"
        ),
    )

    assert queued[0]["meta"]["submitted_by"] == {"id": "operator-7", "kind": "person"}


# --- ADR 0075: an operator runs only what an admin published to the project -------------


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("unpublished", "operator.not_published"),
        ("unacknowledged", "operator.effects_not_acknowledged"),
        ("unbound", "operator.not_bound"),
        ("broken_form", "form.widget_incompatible"),
        ("stale_acknowledgement", "operator.effects_not_acknowledged"),
    ],
)
def test_an_operator_form_run_needs_publication_binding_and_acknowledgement(
    builder_workflow_service, change, code
):
    from core.application.management.errors import ServiceForbiddenError

    service, fields, queued, workflow = builder_workflow_service
    fields.append({"name": "supplier", "type": "string"})
    _publish(workflow, acknowledged=change != "unacknowledged")
    if change == "unpublished":
        workflow["settings"]["operator"]["published"] = False
    if change == "unbound":
        service._get_logger().get_project_run_config.return_value["project_workflows"] = [
            {"workflow_id": "stock-import", "enabled": True},
            {"workflow_id": "other", "enabled": True},
        ]
        service._get_logger().get_project_run_config.return_value["project_workflows"][0][
            "deleted_at"
        ] = "2026-11-01"
    if change == "broken_form":
        workflow["settings"]["operator"]["form"] = {"fields": {"supplier": {"widget": "number"}}}
    if change == "stale_acknowledgement":
        # The contract changed after the admin confirmed its effects.
        fields.append({"name": "warehouse", "type": "string"})

    with pytest.raises((ServiceForbiddenError, ServiceValidationError)) as refused:
        service.start_builder_workflow_run("stock-import", _builder_request(), scope=_operator())

    if isinstance(refused.value, ServiceForbiddenError):
        assert code in [reason["code"] for reason in refused.value.payload["reasons"]]
        assert refused.value.payload["code"] == "operator_run_not_allowed"
    else:
        # The Builder's own link check answers first for an unbound workflow.
        assert change == "unbound"
    assert queued == []


def test_an_operator_runs_a_published_acknowledged_bound_workflow(builder_workflow_service):
    service, fields, queued, workflow = builder_workflow_service
    fields.append({"name": "supplier", "type": "string"})
    _publish(workflow, menu_title="Stock")

    result = service.start_builder_workflow_run(
        "stock-import", _builder_request(inputs={"supplier": "acme"}), scope=_operator()
    )

    assert result.job_id == "91"
    assert len(queued) == 1


@pytest.mark.parametrize(
    "scope",
    [
        ScopeContext(account_id=DEFAULT_ACCOUNT_ID, actor_id="admin-1", principal_type="admin"),
        ScopeContext(account_id=DEFAULT_ACCOUNT_ID, actor_id="user-42"),
    ],
)
def test_admins_keep_the_builder_checks_without_publication(builder_workflow_service, scope):
    service, _, queued, _ = builder_workflow_service

    service.start_builder_workflow_run("stock-import", _builder_request(), scope=scope)

    assert len(queued) == 1


@pytest.mark.parametrize("state", ["unpublished", "unbound", "published"])
def test_the_api_and_mcp_launch_hold_an_operator_to_the_same_rule(direct_workflow_service, state):
    from core.application.management.errors import ServiceForbiddenError
    from core.application.registry.registry_manager import RegistryManager

    service, fields, queued = direct_workflow_service
    fields.append({"name": "supplier", "type": "string"})
    workflow = RegistryManager.instance().db_store.get_workflow("stock-import")
    workflow.update(enabled=True, revision=7, account_id=DEFAULT_ACCOUNT_ID)
    workflow["settings"]["callable"]["trigger_channels"] = ["ui", "mcp"]
    if state != "unpublished":
        _publish(workflow)
    if state == "unbound":
        service._get_logger().get_project_run_config.return_value["project_workflows"] = []
    command = {"project_id": PROJECT_ID, "workflow_id": "stock-import", "inputs": {"supplier": "a"}}

    if state == "published":
        service.start_mcp_workflow_run(command, scope=_operator(auth_source="pat"))
        assert len(queued) == 1
        return
    with pytest.raises(ServiceForbiddenError) as refused:
        service.start_mcp_workflow_run(command, scope=_operator(auth_source="pat"))
    expected = "operator.not_published" if state == "unpublished" else "operator.not_bound"
    assert expected in [reason["code"] for reason in refused.value.payload["reasons"]]
    assert queued == []


def test_a_service_account_keeps_its_api_launch(direct_workflow_service):
    service, fields, queued = direct_workflow_service
    fields.append({"name": "supplier", "type": "string"})

    service.start_mcp_workflow_run(
        {"project_id": PROJECT_ID, "workflow_id": "stock-import", "inputs": {"supplier": "a"}},
        scope=ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID,
            actor_id="sa-1",
            principal_type="service_account",
            auth_source="machine",
        ),
    )

    assert len(queued) == 1


# --- ADR 0075: several files in one input ----------------------------------------------

PAGES = {"name": "pages", "type": "array", "items": {"type": "file"}, "required": True}


def _page(index: int, *, filename: str = "", input_name: str = "pages") -> dict:
    import base64

    return {
        "input_name": input_name,
        "filename": filename or f"page-{index}.jpg",
        "mime_type": "image/jpeg",
        "content_base64": base64.b64encode(f"page {index}".encode()).decode(),
    }


@pytest.fixture
def stored_uploads(builder_workflow_service, monkeypatch):
    """Real admission and binding; artifact storage and the queue are recorded."""
    import hashlib
    from contextlib import nullcontext

    service, fields, queued, _ = builder_workflow_service
    saved = []
    prepared = {}
    store = service._get_logger()

    def create_artifact(**kwargs):
        saved.append((kwargs["artifact_id"], kwargs["filename"], kwargs["body"]))
        return SimpleNamespace(artifact_id=kwargs["artifact_id"]), {
            "filename": kwargs["filename"],
            "content_type": kwargs["content_type"],
            "size_bytes": len(kwargs["body"]),
            "sha256": hashlib.sha256(kwargs["body"]).hexdigest(),
            "object_key": f"artifacts/{kwargs['artifact_id']}/{kwargs['filename']}",
        }

    def enqueue(**kwargs):
        store.get_job.return_value = {"meta": kwargs["meta"]}
        prepared.update(kwargs["pre_publish_fn"]("91", store))
        queued.append(kwargs)
        return 91

    service._artifact_plane = SimpleNamespace(create_artifact=create_artifact)
    service._enqueue_task = enqueue
    monkeypatch.setattr(
        "core.infrastructure.db.db_pool.transaction_connection", lambda *_a, **_k: nullcontext()
    )
    monkeypatch.setenv("FS_ATTACHMENT_MAX_SIZE_BYTES", str(1_048_576))
    monkeypatch.setenv("FS_ATTACHMENT_MAX_FILES", "10")
    return service, fields, queued, saved, prepared


def test_four_pages_become_four_artifacts_and_one_list_input(stored_uploads):
    service, fields, queued, saved, prepared = stored_uploads
    fields.append(PAGES)
    # Two pages share a filename: each still keeps its own stored copy.
    files = [_page(1), _page(2, filename="scan.jpg"), _page(3, filename="scan.jpg"), _page(4)]

    result = service.start_builder_workflow_run("stock-import", _builder_request(files=files))

    assert result.job_id == "91"
    assert len(queued) == 1
    assert [row[1] for row in saved] == ["page-1.jpg", "scan.jpg", "scan.jpg", "page-4.jpg"]
    assert [row[2] for row in saved] == [f"page {index}".encode() for index in range(1, 5)]
    pages = prepared["inputs"]["pages"]
    assert [ref["artifact_id"] for ref in pages] == [row[0] for row in saved]
    assert len({ref["artifact_id"] for ref in pages}) == 4
    assert all(ref["field_name"] == "pages" for ref in pages)
    assert pages == prepared["files"]
    assert prepared["task_envelope"]["inputs"]["pages"] == pages
    invocation = prepared["invocation"]
    assert [row["artifact_id"] for row in invocation["files"]] == [row[0] for row in saved]
    assert [row["field_name"] for row in invocation["files"]] == ["pages"] * 4
    mapped = queued[0]["meta"]["invocation"]["mapped_inputs"]["pages"]
    assert [row["filename"] for row in mapped] == [
        "page-1.jpg",
        "scan.jpg",
        "scan.jpg",
        "page-4.jpg",
    ]
    assert "content_base64" not in repr(prepared)


def test_the_operator_list_gives_each_page_its_artifact(stored_uploads):
    from core.application.registry.workflow_operator_form import OperatorListColumn
    from core.application.registry.workflow_operator_list import operator_row_values

    service, fields, queued, saved, prepared = stored_uploads
    fields.append(PAGES)
    files = [_page(1, filename="scan.jpg"), _page(2, filename="scan.jpg")]
    service.start_builder_workflow_run("stock-import", _builder_request(files=files))
    column = OperatorListColumn.model_validate({"id": "pages", "source": "input", "path": "pages"})

    values = operator_row_values(
        [column],
        {"status": "running"},
        {
            "inputs": queued[0]["meta"]["invocation"]["mapped_inputs"],
            "input_files": prepared["invocation"]["files"],
        },
    )

    assert [row["artifact_id"] for row in values["pages"]] == [row[0] for row in saved]


def test_a_single_file_input_refuses_a_second_file_before_storage(stored_uploads):
    service, fields, queued, saved, _ = stored_uploads
    fields.append({"name": "source", "type": "file", "required": True})
    files = [_page(1, input_name="source"), _page(2, input_name="source")]

    with pytest.raises(ServiceValidationError) as refused:
        service.start_builder_workflow_run("stock-import", _builder_request(files=files))

    assert refused.value.status_code == 422
    assert refused.value.payload["code"] == "workflow_file_input_single"
    assert refused.value.payload["input_name"] == "source"
    assert "'source'" in str(refused.value)
    assert queued == []
    assert saved == []


def test_the_api_launch_takes_several_files_for_a_list_and_one_for_a_file(
    direct_workflow_service,
):
    service, fields, _ = direct_workflow_service
    fields.extend([dict(PAGES), {"name": "source", "type": "file", "required": False}])
    commands = []

    def capture(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(job_id="91")

    create_artifact = MagicMock()
    service._artifact_plane = SimpleNamespace(create_artifact=create_artifact)
    with pytest.raises(ServiceValidationError) as refused:
        service.start_mcp_workflow_run(
            {
                "project_id": PROJECT_ID,
                "workflow_id": "stock-import",
                "files": [_page(1), _page(2), *[_page(i, input_name="source") for i in (3, 4)]],
            }
        )
    assert refused.value.status_code == 422
    assert refused.value.payload["input_name"] == "source"
    create_artifact.assert_not_called()

    with patch.object(service, "enqueue_manual_task", side_effect=capture):
        service.start_mcp_workflow_run(
            {"project_id": PROJECT_ID, "workflow_id": "stock-import", "files": [_page(1), _page(2)]}
        )
    assert [row.input_name for row in commands[0].files] == ["pages", "pages"]
    assert [row.filename for row in commands[0].files] == ["page-1.jpg", "page-2.jpg"]


@pytest.mark.parametrize(
    ("shape", "count", "message"),
    [
        ({"min_items": 2}, 1, "at least 2"),
        ({"max_items": 3}, 4, "at most 3"),
    ],
)
def test_item_bounds_of_a_file_list_are_checked_before_storage(
    stored_uploads, shape, count, message
):
    service, fields, queued, saved, _ = stored_uploads
    fields.append({**PAGES, **shape})
    files = [_page(index) for index in range(1, count + 1)]

    with pytest.raises(ServiceValidationError, match=message):
        service.start_builder_workflow_run("stock-import", _builder_request(files=files))

    assert queued == []
    assert saved == []


def test_a_required_file_list_needs_uploads_not_json(stored_uploads):
    service, fields, queued, saved, _ = stored_uploads
    fields.append(PAGES)

    with pytest.raises(ServiceValidationError) as missing:
        service.start_builder_workflow_run("stock-import", _builder_request())
    assert missing.value.payload["code"] == "workflow_file_input_required"
    with pytest.raises(ServiceValidationError, match="through files"):
        service.start_builder_workflow_run(
            "stock-import", _builder_request(inputs={"pages": [{"artifact_id": "art_fake"}]})
        )
    assert queued == []
    assert saved == []


def test_more_files_than_the_policy_allows_are_refused_before_storage(stored_uploads, monkeypatch):
    service, fields, queued, saved, _ = stored_uploads
    fields.append(PAGES)
    monkeypatch.setenv("FS_ATTACHMENT_MAX_FILES", "3")

    with pytest.raises(ValidationError, match="at most 3 files"):
        service.start_builder_workflow_run(
            "stock-import", _builder_request(files=[_page(index) for index in range(4)])
        )

    assert queued == []
    assert saved == []


def test_one_file_input_still_receives_one_reference(stored_uploads):
    service, fields, _, saved, prepared = stored_uploads
    fields.append({"name": "source", "type": "file", "required": True})

    service.start_builder_workflow_run(
        "stock-import", _builder_request(files=[_page(1, input_name="source")])
    )

    assert prepared["inputs"]["source"]["artifact_id"] == saved[0][0]
    assert prepared["inputs"]["source"]["field_name"] == "source"
