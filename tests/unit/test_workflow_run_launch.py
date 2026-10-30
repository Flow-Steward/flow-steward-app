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


@pytest.mark.parametrize(
    "kind", ["too_many", "too_large", "aggregate", "duplicate", "path", "fake_handle"]
)
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
    elif kind == "duplicate":
        files = [upload, upload]
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
        {"files": [{"input_name": "upload", "filename": "a.csv", "content_base64": "YQ=="}] * 2},
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
def test_builder_rejects_bad_actual_uploads_before_admission(builder_workflow_service, kind):
    import base64

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
