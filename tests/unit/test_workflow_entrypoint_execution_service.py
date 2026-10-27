from __future__ import annotations

import inspect
from types import SimpleNamespace

from core.application.workflows import workflow_entrypoint_execution_service as service_module


def test_entrypoint_fingerprint_is_stable_for_same_dynamic_secret_plaintext(monkeypatch) -> None:
    from core.application.workflows.workflow_entrypoint_submission import (
        _submission_fingerprint,
    )
    from core.infrastructure.security import secret_codec
    from core.infrastructure.security.secret_reference import SecretReference

    monkeypatch.setattr(secret_codec, "runtime_env", {"ENCRYPTION_KEY": "fingerprint-test-key"})
    first = SecretReference(
        name="login", ciphertext=secret_codec.encrypt_secret("same-secret")
    ).to_wire()
    second = SecretReference(
        name="login", ciphertext=secret_codec.encrypt_secret("same-secret")
    ).to_wire()

    assert first["ciphertext"] != second["ciphertext"]
    assert _submission_fingerprint(
        entry_key="orders",
        workflow_id="workflow-1",
        workflow_inputs={"login": first},
        file_descriptors=[],
    ) == _submission_fingerprint(
        entry_key="orders",
        workflow_id="workflow-1",
        workflow_inputs={"login": second},
        file_descriptors=[],
    )


def test_execution_coordinator_has_no_transport_mapping_or_audit_callbacks() -> None:
    parameters = inspect.signature(service_module.WorkflowEntrypointExecutionCoordinator).parameters

    assert "build_inputs" not in parameters
    assert "validate_inputs" not in parameters
    assert "record_execution" not in parameters
    assert "resolve_project" not in parameters


def test_execution_coordinator_prepares_bound_project_and_owns_submission_wiring() -> None:
    captured: dict[str, object] = {}
    entrypoint = {
        "entry_key": "invoice-created",
        "workflow_id": "wf-invoice",
        "project_id": "project-7",
        "enabled": True,
    }
    expected_outcome = service_module.WorkflowEntrypointSubmissionOutcome(job_id="job-7")

    class _SubmissionUseCase:
        def __init__(self, **kwargs):
            captured["wiring"] = kwargs

        def submit(self, **kwargs):
            captured["submission"] = kwargs
            return expected_outcome

    coordinator = service_module.WorkflowEntrypointExecutionCoordinator(
        logger=SimpleNamespace(get_project=lambda _project_id: {"id": "project-7"}),
        artifact_plane=SimpleNamespace(),
        redeliver_task=None,
        enqueue_workflow_start=lambda **_kwargs: None,
        now_iso=lambda: "2026-07-19T00:00:00Z",
        load_entrypoint=lambda _logger, _key: entrypoint,
        resolve_bound_project=lambda _logger, _workflow_id, _project_id: (
            "project-7",
            {"project_name": "Demo"},
        ),
        load_workflow_state=lambda _logger, _workflow_id: {"enabled": True},
        submission_use_case_factory=_SubmissionUseCase,
        new_id=lambda: "claim-owner-7",
    )

    preparation = coordinator.prepare("invoice-created")
    outcome = coordinator.submit(
        entry_key="invoice-created",
        task_id="webhook:invoice-created:req-7",
        project_id=preparation.project_id,
        run_config=preparation.run_config,
        workflow_id=preparation.workflow_id,
        request_id="req-7",
        idempotency_mode="none",
        idempotency_key="",
        replay_key="",
        mapping={},
        sources={},
        fallback_body={},
        staged_files=[],
        request_context={},
    )

    assert preparation.ok is True
    assert preparation.project_id == "project-7"
    assert preparation.workflow_id == "wf-invoice"
    assert outcome is expected_outcome
    assert captured["submission"]["project_id"] == "project-7"
    wiring = captured["wiring"]
    idempotency = wiring["idempotency_service_factory"]("fingerprint-7")
    assert idempotency._claim_owner_id == "claim-owner-7"


def test_execution_coordinator_rechecks_disabled_owner_before_constructing_submission() -> None:
    enabled = True
    submission_created = []

    def submission_factory(**_kwargs):
        submission_created.append(True)
        raise AssertionError("Disabled owner must not construct the durable submission lifecycle")

    coordinator = service_module.WorkflowEntrypointExecutionCoordinator(
        logger=SimpleNamespace(),
        artifact_plane=SimpleNamespace(),
        redeliver_task=None,
        enqueue_workflow_start=lambda **_kwargs: None,
        now_iso=lambda: "2026-10-27T00:00:00Z",
        load_entrypoint=lambda _logger, _key: {},
        resolve_bound_project=lambda *_args: (None, {}),
        load_workflow_state=lambda _logger, _workflow_id: {"enabled": enabled},
        submission_use_case_factory=submission_factory,
    )
    assert coordinator.check_workflow_admission("wf-invoice") is None
    enabled = False

    outcome = coordinator.submit(workflow_id="wf-invoice")

    assert outcome.failure_kind == "conflict"
    assert outcome.code == "workflow_disabled"
    assert submission_created == []


def test_execution_coordinator_maps_entrypoint_storage_outage_without_leaking_details() -> None:
    secret = "postgresql://registry:super-secret@db.internal/workflows"
    coordinator = service_module.WorkflowEntrypointExecutionCoordinator(
        logger=SimpleNamespace(
            get_project=lambda _project_id: (_ for _ in ()).throw(RuntimeError(secret))
        ),
        artifact_plane=SimpleNamespace(),
        redeliver_task=None,
        enqueue_workflow_start=lambda **_kwargs: None,
        now_iso=lambda: "2026-07-19T00:00:00Z",
        load_entrypoint=lambda _logger, _key: (_ for _ in ()).throw(RuntimeError(secret)),
        resolve_bound_project=lambda *_args: (None, {}),
        storage_unavailable_error=lambda _exc: True,
    )

    preparation = coordinator.prepare("invoice-created")

    assert preparation.failure_kind == "unavailable"
    assert preparation.code == "entrypoint_storage_unavailable"
    assert secret not in preparation.error
    assert secret not in preparation.operator_error


def test_execution_coordinator_maps_project_lookup_outage_to_retryable_503() -> None:
    secret = "postgresql://projects:super-secret@db.internal/projects"
    coordinator = service_module.WorkflowEntrypointExecutionCoordinator(
        logger=SimpleNamespace(
            get_project=lambda _project_id: (_ for _ in ()).throw(RuntimeError(secret))
        ),
        artifact_plane=SimpleNamespace(),
        redeliver_task=None,
        enqueue_workflow_start=lambda **_kwargs: None,
        now_iso=lambda: "2026-07-19T00:00:00Z",
        load_entrypoint=lambda _logger, _key: {
            "entry_key": "invoice-created",
            "workflow_id": "wf-invoice",
            "project_id": "project-7",
            "enabled": True,
        },
        resolve_bound_project=lambda *_args: ("project-7", {}),
    )

    preparation = coordinator.prepare("invoice-created")

    assert preparation.failure_kind == "unavailable"
    assert preparation.code == "workflow_project_lookup_unavailable"
    assert preparation.retryable is True
    assert secret not in preparation.error
    assert secret not in preparation.operator_error


def test_execution_coordinator_fails_closed_without_schema_owned_project_binding() -> None:
    fallback_calls: list[str] = []
    coordinator = service_module.WorkflowEntrypointExecutionCoordinator(
        logger=SimpleNamespace(),
        artifact_plane=SimpleNamespace(),
        redeliver_task=None,
        enqueue_workflow_start=lambda **_kwargs: None,
        now_iso=lambda: "2026-07-19T00:00:00Z",
        load_entrypoint=lambda _logger, _key: {
            "entry_key": "invoice-created",
            "workflow_id": "wf-invoice",
            "enabled": True,
        },
        resolve_bound_project=lambda *_args: ("project-7", {}),
    )

    preparation = coordinator.prepare("invoice-created")

    assert preparation.failure_kind == "conflict"
    assert preparation.code == "workflow_project_binding_required"
    assert fallback_calls == []


def _service(logger, *, redeliver=None, artifact_plane=None):
    return service_module.WorkflowEntrypointIdempotencyService(
        logger=logger,
        entry_key="invoice-created",
        request_fingerprint="fingerprint-1",
        claim_owner_id="owner-1",
        redeliver_task=redeliver,
        artifact_plane=artifact_plane or SimpleNamespace(),
        claim_reservation=service_module.claim_idempotency_reservation,
        find_existing_job=service_module.find_existing_idempotent_job,
        accept_reservation=service_module.accept_idempotency_reservation,
        fail_reservation=service_module.fail_idempotency_reservation,
        dispatch_confirmed=service_module.durable_job_dispatch_confirmed,
    )


def test_expired_idempotency_tombstone_returns_gone_contract(monkeypatch) -> None:
    monkeypatch.setattr(
        service_module,
        "claim_idempotency_reservation",
        lambda *_args, **_kwargs: {"outcome": "expired", "claimed": False},
    )

    resolution = _service(SimpleNamespace()).resolve("expired-key")

    assert resolution.failure_kind == "gone"
    assert resolution.code == "idempotency_result_expired"


def test_recovered_terminal_job_is_adopted_without_redelivery(monkeypatch) -> None:
    logger = SimpleNamespace(
        get_job=lambda _job_id: {
            "id": "job-1",
            "status": "failed",
            "worker_id": "worker-1",
            "meta": {},
        }
    )
    monkeypatch.setattr(
        service_module,
        "claim_idempotency_reservation",
        lambda *_args, **_kwargs: {
            "claimed": True,
            "recovered": True,
            "request_id": "owner-1",
            "scope_key": "scope-1",
            "key_digest": "digest-1",
        },
    )
    monkeypatch.setattr(
        service_module,
        "find_existing_idempotent_job",
        lambda *_args, **_kwargs: "job-1",
    )
    accepted: list[str] = []
    monkeypatch.setattr(
        service_module,
        "accept_idempotency_reservation",
        lambda *_args, **kwargs: accepted.append(kwargs["job_id"]),
    )
    redeliver_calls: list[str] = []

    resolution = _service(
        logger,
        redeliver=lambda **kwargs: redeliver_calls.append(kwargs["job_id"]),
    ).resolve("request-key")

    assert resolution.replay_job_id == "job-1"
    assert resolution.failure_kind is None
    assert accepted == ["job-1"]
    assert redeliver_calls == []


def test_recovered_unconfirmed_job_fails_closed_when_redelivery_is_not_safe(monkeypatch) -> None:
    logger = SimpleNamespace(
        get_job=lambda _job_id: {
            "id": "job-2",
            "status": "pending",
            "worker_id": "",
            "meta": {"queue_delivery": {"dispatch_state": "known_not_sent"}},
        }
    )
    monkeypatch.setattr(
        service_module,
        "claim_idempotency_reservation",
        lambda *_args, **_kwargs: {"claimed": True, "recovered": True},
    )
    monkeypatch.setattr(
        service_module,
        "find_existing_idempotent_job",
        lambda *_args, **_kwargs: "job-2",
    )

    resolution = _service(logger, redeliver=lambda **_kwargs: False).resolve("request-key")

    assert resolution.failure_kind == "conflict"
    assert resolution.code == "idempotency_recovery_not_safe"
    assert resolution.replay_job_id == "job-2"


def test_recovered_upload_without_job_cleans_journal_before_new_launch(monkeypatch) -> None:
    deleted: list[str] = []
    monkeypatch.setattr(
        service_module,
        "claim_idempotency_reservation",
        lambda *_args, **_kwargs: {
            "claimed": True,
            "recovered": True,
            "recovery_journal": {"pending_artifact_ids": ["artifact-1"]},
        },
    )
    monkeypatch.setattr(
        service_module,
        "find_existing_idempotent_job",
        lambda *_args, **_kwargs: None,
    )

    resolution = _service(
        SimpleNamespace(),
        artifact_plane=SimpleNamespace(delete_artifact=deleted.append),
    ).resolve("request-key")

    assert resolution.proceed is True
    assert deleted == ["artifact-1"]


def test_artifact_lifecycle_journals_and_rolls_back_reserved_artifacts() -> None:
    journals: list[dict] = []
    deleted: list[str] = []
    lifecycle = service_module.WorkflowEntrypointArtifactLifecycle(
        logger=SimpleNamespace(),
        artifact_plane=SimpleNamespace(delete_artifact=deleted.append),
        entry_key="invoice-created",
        request_id="owner-1",
        run_id="request-1",
        request_fingerprint="fingerprint-1",
        idempotency_keys=["header-key"],
        renew_reservation=lambda *_args, **_kwargs: True,
        journal_reservation=lambda *_args, **kwargs: journals.append(kwargs["journal"]) or True,
    )

    lifecycle.heartbeat()
    lifecycle.reserve({"artifact_id": "artifact-1", "object_key": "objects/1"})
    lifecycle.rollback([{"artifact_id": "artifact-1"}])

    assert journals == [
        {
            "pending_artifact_ids": ["artifact-1"],
            "pending_artifacts": [{"artifact_id": "artifact-1", "object_key": "objects/1"}],
        }
    ]
    assert deleted == ["artifact-1"]


def test_artifact_lifecycle_owns_staged_attachment_persistence() -> None:
    calls: list[dict] = []
    lifecycle = service_module.WorkflowEntrypointArtifactLifecycle(
        logger=SimpleNamespace(),
        artifact_plane=SimpleNamespace(),
        entry_key="invoice-created",
        request_id="owner-1",
        run_id="request-1",
        request_fingerprint="fingerprint-1",
        idempotency_keys=[],
        renew_reservation=lambda *_args, **_kwargs: True,
        journal_reservation=lambda *_args, **_kwargs: True,
        persist_attachments=lambda **kwargs: (
            calls.append(kwargs) or [{"artifact_id": "artifact-1"}]
        ),
    )

    refs = lifecycle.persist([SimpleNamespace(content_path="/tmp/staged")])

    assert refs == [{"artifact_id": "artifact-1"}]
    assert calls[0]["owner_scope"] == {
        "entry_path": "webhook",
        "entry_key": "invoice-created",
    }
    assert calls[0]["run_id"] == "request-1"
    assert calls[0]["step_id"] == "workflow_entrypoint"
    assert calls[0]["source_ref_prefix"] == "multipart"
    assert calls[0]["heartbeat"] == lifecycle.heartbeat
    assert calls[0]["on_artifact_reserved"] == lifecycle.reserve


def test_artifact_lifecycle_fails_closed_when_lease_is_lost() -> None:
    lifecycle = service_module.WorkflowEntrypointArtifactLifecycle(
        logger=SimpleNamespace(),
        artifact_plane=SimpleNamespace(),
        entry_key="invoice-created",
        request_id="owner-1",
        request_fingerprint="fingerprint-1",
        idempotency_keys=["header-key"],
        renew_reservation=lambda *_args, **_kwargs: False,
        journal_reservation=lambda *_args, **_kwargs: True,
    )

    try:
        lifecycle.heartbeat()
    except service_module.WorkflowEntrypointLeaseLostError as exc:
        assert exc.failure_kind == "conflict"
    else:
        raise AssertionError("lost lease must abort artifact persistence")


def test_launch_service_builds_the_internal_workflow_command() -> None:
    calls: list[dict] = []
    launcher = service_module.WorkflowEntrypointLaunchService(
        enqueue_workflow_start=lambda **kwargs: calls.append(kwargs) or "job-1",
        now_iso=lambda: "2026-07-18T10:00:00Z",
    )

    result = launcher.launch(
        entry_key="invoice-created",
        task_id="task-1",
        project_id="project-1",
        run_config={
            "project_name": "Accounts",
            "private": "preserved",
        },
        workflow_id="workflow-1",
        request_id="request-1",
        idempotency_key="key-1",
        workflow_inputs={"object_id": "invoice-42", "summary": "Review invoice"},
        workflow_interface={"enabled": True},
        files=[{"artifact_id": "artifact-1"}],
        request_context={
            "source_ip": "192.0.2.10",
            "content_type": "multipart/form-data",
            "payload_summary": {"file_count": 1},
        },
        idempotency_identities=[{"scope_key": "scope-1", "key_digest": "digest-1"}],
        primary_idempotency_identity={"scope_key": "scope-1", "key_digest": "digest-1"},
    )

    assert result.job_id == "job-1"
    assert result.task_summary == "Review invoice"
    assert calls == [
        {
            "task_id": "task-1",
            "task_summary": "Review invoice",
            "task_source": "webhook",
            "project_id": "project-1",
            "project_name": "Accounts",
            "project_config": {
                "project_name": "Accounts",
                "private": "preserved",
            },
            "selected_workflow_id": "workflow-1",
            "workflow_selection_source": "entry_binding",
            "entry_path": "webhook",
            "input_contract": "direct_inputs",
            "source_system": "webhook",
            "source_event_id": "request-1",
            "source_object_id": "invoice-42",
            "idempotency_key": "key-1",
            "test_mode": False,
            "inputs": {"object_id": "invoice-42", "summary": "Review invoice"},
            "files": [{"artifact_id": "artifact-1"}],
            "extra_state": {
                "entry_key": "invoice-created",
            },
            "workflow_secret_ref_fingerprints": [],
            "job_meta": {
                "entry_key": "invoice-created",
                "received_at": "2026-07-18T10:00:00Z",
                "invocation": {
                    "channel": "webhook",
                    "trigger_family": "incoming_webhook",
                    "entry_path": "webhook",
                    "source_system": "webhook",
                    "request_id": "request-1",
                    "received_at": "2026-07-18T10:00:00Z",
                    "source_ip": "192.0.2.10",
                    "content_type": "multipart/form-data",
                    "idempotency_key_digest": "digest-1",
                    "entry_key": "invoice-created",
                    "request_context_summary": {"file_count": 1},
                },
                "request_idempotency_identities": [
                    {"scope_key": "scope-1", "key_digest": "digest-1"}
                ],
                "idempotency": {"scope_key": "scope-1", "key_digest": "digest-1"},
            },
        }
    ]


def test_submission_use_case_validates_before_claim_persist_and_dispatch() -> None:
    events: list[str] = []

    class _Idempotency:
        def resolve(self, _key):
            events.append("claim")
            return service_module.WorkflowEntrypointIdempotencyResolution(
                claimed=True,
                identity={"scope_key": "scope-1"},
            )

        def accept(self, _key, _job_id):
            events.append("accept")

        def fail(self, _key, _reason):
            events.append("fail")

    class _Lifecycle:
        def persist(self, _staged):
            events.append("persist")
            return [{"artifact_id": "artifact-1"}]

        def rollback(self, _refs):
            events.append("rollback")

    use_case = service_module.WorkflowEntrypointSubmissionUseCase(
        build_inputs=lambda **_kwargs: events.append("build") or {"invoice_id": "INV-42"},
        validate_inputs=lambda *_args, **_kwargs: (
            events.append("validate")
            or SimpleNamespace(
                ok=True,
                errors=[],
                inputs={"invoice_id": "INV-42"},
                interface={"enabled": True},
            )
        ),
        idempotency_service_factory=lambda _fingerprint: _Idempotency(),
        artifact_lifecycle_factory=lambda _fingerprint, _keys: _Lifecycle(),
        launch_service=SimpleNamespace(
            launch=lambda **_kwargs: (
                events.append("dispatch")
                or service_module.WorkflowEntrypointLaunchResult(
                    job_id="job-1", task_summary="Review invoice"
                )
            )
        ),
    )

    outcome = use_case.submit(
        entry_key="invoice-created",
        task_id="task-1",
        project_id="project-1",
        run_config={},
        workflow_id="workflow-1",
        request_id="request-1",
        idempotency_mode="header",
        idempotency_key="header-key",
        replay_key="",
        mapping={},
        sources={},
        fallback_body={},
        staged_files=[
            SimpleNamespace(
                filename="invoice.pdf",
                mime_type="application/pdf",
                size_bytes=7,
                sha256="abc",
            )
        ],
        request_context={},
    )

    assert outcome.ok is True
    assert events == ["build", "validate", "claim", "persist", "dispatch", "accept"]


def test_submission_use_case_maps_registry_exception_to_retryable_503_before_side_effects() -> None:
    secret = "postgresql://registry:super-secret@db.internal/workflows"
    use_case = service_module.WorkflowEntrypointSubmissionUseCase(
        build_inputs=lambda **_kwargs: {"invoice_id": "INV-42"},
        validate_inputs=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(secret)),
        idempotency_service_factory=lambda _fingerprint: (_ for _ in ()).throw(
            AssertionError("claim must not run")
        ),
        artifact_lifecycle_factory=lambda *_args: (_ for _ in ()).throw(
            AssertionError("persistence must not run")
        ),
        launch_service=SimpleNamespace(),
    )

    outcome = use_case.submit(
        entry_key="invoice-created",
        task_id="task-1",
        project_id="project-1",
        run_config={},
        workflow_id="workflow-1",
        request_id="request-1",
        idempotency_mode="header",
        idempotency_key="header-key",
        replay_key="",
        mapping={},
        sources={},
        fallback_body={},
        staged_files=[],
        request_context={},
    )

    assert outcome.failure_kind == "unavailable"
    assert outcome.code == "workflow_contract_unavailable"
    assert outcome.error == "Workflow contract service is unavailable; retry later"
    assert secret not in outcome.operator_error


def test_submission_rejects_malformed_secret_marker_before_idempotency_claim() -> None:
    from core.application.registry.workflow_contract import WorkflowInterface
    from core.application.registry.workflow_contract_service import WorkflowInterfaceService

    marker_value = "malformed-ciphertext-must-not-echo"
    marker = {
        "__type__": "flow-steward.secret-ref.v1",
        "name": "login",
        "ciphertext": marker_value,
    }
    interface = WorkflowInterface.from_settings(
        {
            "callable": {
                "enabled": True,
                "trigger_channels": ["webhook"],
                "expected_inputs": [{"name": "login", "type": "secret"}],
                "output_mode": "none",
            }
        }
    )
    service = WorkflowInterfaceService()
    use_case = service_module.WorkflowEntrypointSubmissionUseCase(
        build_inputs=lambda **_kwargs: {"login": marker},
        validate_inputs=lambda _workflow_id, inputs, **kwargs: service.validate_inputs(
            interface, inputs, **kwargs
        ),
        idempotency_service_factory=lambda _fingerprint: (_ for _ in ()).throw(
            AssertionError("claim must not run")
        ),
        artifact_lifecycle_factory=lambda *_args: (_ for _ in ()).throw(
            AssertionError("persistence must not run")
        ),
        launch_service=SimpleNamespace(),
    )

    outcome = use_case.submit(
        entry_key="secret-entry",
        task_id="task-1",
        project_id="project-1",
        run_config={},
        workflow_id="workflow-1",
        request_id="request-1",
        idempotency_mode="header",
        idempotency_key="header-key",
        replay_key="",
        mapping={},
        sources={},
        fallback_body={},
        staged_files=[],
        request_context={},
    )

    assert outcome.failure_kind == "invalid_request"
    assert outcome.code == "workflow_input_validation_failed"
    assert marker_value not in repr(outcome)


def test_submission_use_case_maps_mapping_registry_exception_to_retryable_503() -> None:
    use_case = service_module.WorkflowEntrypointSubmissionUseCase(
        build_inputs=lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("registry connection refused")
        ),
        validate_inputs=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("validation must not run")
        ),
        idempotency_service_factory=lambda _fingerprint: (_ for _ in ()).throw(
            AssertionError("claim must not run")
        ),
        artifact_lifecycle_factory=lambda *_args: (_ for _ in ()).throw(
            AssertionError("persistence must not run")
        ),
        launch_service=SimpleNamespace(),
    )

    outcome = use_case.submit(
        entry_key="invoice-created",
        task_id="task-1",
        project_id="project-1",
        run_config={},
        workflow_id="workflow-1",
        request_id="request-1",
        idempotency_mode="header",
        idempotency_key="header-key",
        replay_key="",
        mapping={},
        sources={},
        fallback_body={},
        staged_files=[],
        request_context={},
    )

    assert outcome.failure_kind == "unavailable"
    assert outcome.code == "workflow_contract_unavailable"
    assert outcome.error == "Workflow contract service is unavailable; retry later"
