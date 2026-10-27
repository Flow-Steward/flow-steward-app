from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError

import pytest

from core.application.extensions import host_contract
from core.application.extensions import remote_runtime_protocol_v2 as protocol
from core.application.extensions.host_contract import HOST_CONTRACT_VERSION
from core.application.extensions.remote_runtime_protocol_v2 import (
    CANONICAL_ARTIFACT_ERROR_CODES,
    RemoteRuntimeClient,
    build_action_envelope,
    build_artifact_access_descriptor,
    build_artifact_access_descriptors,
    build_artifact_access_session,
    build_artifact_audit_record,
    build_event_envelope,
    build_query_envelope,
    canonical_artifact_error,
    validate_artifact_descriptor_access,
    validate_required_remote_endpoints,
)


def test_validate_required_remote_endpoints() -> None:
    missing = validate_required_remote_endpoints(
        {"handshake": "/handshake", "health": "/health", "query": "/queries/{query_id}"}
    )
    assert "action" in missing
    assert "emit_event" in missing


def test_business_result_cannot_spoof_retry_authority() -> None:
    normalized = protocol._normalize_response(
        200,
        {
            "ok": True,
            "result": {
                "accepted": True,
                "request_semantics": "mutation",
                "failure_class": "provider",
                "retryable": True,
                "definitely_no_external_effect": True,
                "external_effect_status": "failed",
            },
        },
    )

    assert normalized["result"]["accepted"] is True
    assert "retryable" not in normalized
    assert "definitely_no_external_effect" not in normalized
    assert "failure_class" not in normalized


def test_envelopes_include_scope_connection_and_canonical_artifacts() -> None:
    now = datetime(2026, 4, 7, 11, 45, tzinfo=UTC)
    artifact_scope = {
        "account_id": "acc_default",
        "project_id": 7,
        "workflow_id": "inventory_sync",
        "run_id": "run_1",
        "group_id": "process_feed",
        "step_id": "transform_input",
        "step_execution_id": "33333333-3333-4333-8333-333333333333",
    }
    input_descriptors = build_artifact_access_descriptors(
        artifact_ids=["art_1"],
        read_url_factory=lambda aid: f"https://signed.local/{aid}/download?sig=in",
        write_url_factory=None,
        ttl_seconds=600,
        role="input",
        now=now,
        scope=artifact_scope,
    )
    output_descriptors = build_artifact_access_descriptors(
        artifact_ids=["art_2"],
        read_url_factory=None,
        write_url_factory=lambda aid: f"https://signed.local/{aid}/upload?sig=out",
        ttl_seconds=600,
        role="output",
        now=now,
        scope=artifact_scope,
    )
    session = build_artifact_access_session(
        api_base_url="https://flow-steward.example.com/runtime/artifacts",
        bearer_token="session-token-1",
        ttl_seconds=600,
        now=now,
    )
    action = build_action_envelope(
        action_id="run",
        input_payload={"value": 1},
        scope={"account_id": "acc_default", "project_id": 7},
        connection={"connection_id": "conn_1"},
        artifact_inputs=input_descriptors,
        artifact_outputs=output_descriptors,
        artifact_access_session=session,
        dataset_grants=[{"dataset_id": "dst_1"}],
        idempotency_key="idem-77",
    )
    query = build_query_envelope(
        query_id="list",
        query_payload={"search": "abc"},
        scope={"account_id": "acc_default"},
        connection={"connection_id": "conn_1"},
        artifact_inputs=input_descriptors,
        artifact_access_session=session,
    )
    event = build_event_envelope(
        event_name="approval.received",
        payload={"approved": True},
        scope={"account_id": "acc_default"},
        connection={"connection_id": "conn_1"},
    )
    assert action["idempotency_key"] == "idem-77"
    assert action["artifacts"]["inputs"][0]["artifact_id"] == "art_1"
    assert action["artifacts"]["inputs"][0]["artifact_handle"] == "artifact:art_1"
    assert action["artifacts"]["inputs"][0]["access"]["download_url"]
    assert action["artifacts"]["outputs"][0]["access"]["upload_url"]
    assert action["artifact_access_session"]["transport"] == "artifact_api"
    removed_grants_key = "artifact_" + "grants"
    removed_descriptors_key = "artifact_" + "descriptors"
    assert removed_grants_key not in action
    assert removed_descriptors_key not in action
    assert query["query_id"] == "list"
    assert query["artifacts"]["inputs"][0]["artifact_id"] == "art_1"
    assert removed_grants_key not in query
    assert removed_descriptors_key not in query
    assert event["event_name"] == "approval.received"
    assert event["event_type"] == "approval.received"
    assert input_descriptors[0]["scope"]["group_id"] == "process_feed"
    assert "phase_id" not in input_descriptors[0]["scope"]
    for envelope in (action, query, event):
        assert envelope["contract_version"] == HOST_CONTRACT_VERSION == "extension_host_v2"
        assert "phase_id" not in envelope["scope"]
        assert {key: envelope["scope"][key] for key in host_contract.EXECUTION_SCOPE_FIELDS} == {
            "group_id": None,
            "for_each_id": None,
            "step_id": None,
            "step_execution_id": None,
        }


def test_artifact_access_descriptors_are_handle_first_and_no_bucket_credentials() -> None:
    descriptors = build_artifact_access_descriptors(
        artifact_ids=["art_1", "art_2"],
        read_url_factory=lambda aid: f"https://signed.local/{aid}/read",
        write_url_factory=lambda aid: f"https://signed.local/{aid}/write",
        ttl_seconds=120,
    )
    assert len(descriptors) == 2
    assert descriptors[0]["artifact_handle"] == "artifact:art_1"
    assert descriptors[0]["access"]["download_url"].startswith("https://signed.local/")
    assert descriptors[0]["access"]["upload_url"].startswith("https://signed.local/")
    assert descriptors[0]["access"]["transport"] == "presigned_url"
    assert descriptors[0]["access"]["mode"] == "read_write"
    assert "bucket" not in descriptors[0]
    assert "access_key" not in descriptors[0]
    assert "secret_key" not in descriptors[0]


def test_remote_action_forwards_dataset_grants(monkeypatch) -> None:
    from core.infrastructure.db import remote_extension_runtime

    captured: dict[str, object] = {}

    class Client:
        def invoke_action(self, operation_id, envelope):
            captured["operation_id"] = operation_id
            captured["envelope"] = envelope
            return {"ok": True, "result": {"accepted": True}}

    monkeypatch.setattr(
        remote_extension_runtime,
        "_build_remote_client",
        lambda *_args, **_kwargs: Client(),
    )

    invocation = remote_extension_runtime.remote_action(
        {
            "extension_key": "remote.dataset",
            "runtime": {"contract_version": "extension_host_v2"},
            "runtime_mode": "remote_https",
            "remote_base_url": "https://runtime.example.com",
            "handshake_status": "ok",
        },
        action_id="sync",
        input_payload={"input": {"value": 1}},
        dataset_inputs=[
            {"dataset_id": "dst_in", "dataset_handle": "dataset:dst_in", "role": "input"}
        ],
        dataset_outputs=[
            {"dataset_id": "dst_out", "dataset_handle": "dataset:dst_out", "role": "output"}
        ],
        idempotency_key="llm-request-42",
    )

    assert invocation["status"] == "succeeded"
    assert captured["operation_id"] == "sync"
    envelope = captured["envelope"]
    assert isinstance(envelope, dict)
    assert envelope["dataset_grants"] == [
        {"dataset_id": "dst_in", "dataset_handle": "dataset:dst_in", "role": "input"},
        {"dataset_id": "dst_out", "dataset_handle": "dataset:dst_out", "role": "output"},
    ]
    assert envelope["idempotency_key"] == "llm-request-42"


def test_input_artifact_read_descriptor_round_trip() -> None:
    now = datetime(2026, 4, 7, 12, 0, tzinfo=UTC)
    descriptor = build_artifact_access_descriptors(
        artifact_ids=["art_input"],
        read_url_factory=lambda aid: f"https://signed.local/{aid}/download?sig=1",
        write_url_factory=None,
        ttl_seconds=300,
        role="input",
        now=now,
        scope={
            "account_id": "acc_default",
            "project_id": 42,
            "workflow_id": "wf_1",
            "run_id": "run_1",
            "phase_id": "phase_1",
            "step_id": "step_1",
        },
    )[0]
    assert descriptor["access"]["mode"] == "read"
    assert descriptor["access"]["upload_url"] is None
    validation = validate_artifact_descriptor_access(
        descriptor,
        scope={
            "account_id": "acc_default",
            "project_id": "42",
            "workflow_id": "wf_1",
            "run_id": "run_1",
            "phase_id": "phase_1",
            "step_id": "step_1",
        },
        expected_mode="read",
        now=now + timedelta(minutes=1),
    )
    assert validation["ok"] is True


def test_output_upload_slot_round_trip() -> None:
    now = datetime(2026, 4, 7, 12, 0, tzinfo=UTC)
    slot = build_artifact_access_descriptors(
        artifact_ids=["art_output"],
        read_url_factory=None,
        write_url_factory=lambda aid: f"https://signed.local/{aid}/upload?sig=2",
        ttl_seconds=300,
        role="output",
        now=now,
        max_size_bytes=104857600,
        scope={"account_id": "acc_default", "run_id": "run_1", "step_id": "step_2"},
    )[0]
    assert slot["access"]["mode"] == "write"
    assert slot["access"]["download_url"] is None
    assert slot["access"]["upload_url"].startswith("https://signed.local/")
    assert slot["access"]["single_use"] is True
    assert slot["access"]["max_size_bytes"] == 104857600
    allowed = validate_artifact_descriptor_access(
        slot,
        scope={"account_id": "acc_default", "run_id": "run_1", "step_id": "step_2"},
        expected_mode="write",
        now=now + timedelta(minutes=1),
    )
    denied = validate_artifact_descriptor_access(
        slot,
        scope={"account_id": "acc_default", "run_id": "run_1", "step_id": "step_2"},
        expected_mode="read",
        now=now + timedelta(minutes=1),
    )
    assert allowed["ok"] is True
    assert denied["ok"] is False
    assert denied["error"]["code"] == "artifact_access_denied"


def test_expired_descriptor_rejection() -> None:
    now = datetime(2026, 4, 7, 12, 0, tzinfo=UTC)
    descriptor = build_artifact_access_descriptors(
        artifact_ids=["art_expired"],
        read_url_factory=lambda aid: f"https://signed.local/{aid}/download?sig=3",
        ttl_seconds=60,
        role="input",
        now=now,
    )[0]
    validation = validate_artifact_descriptor_access(
        descriptor,
        scope={},
        expected_mode="read",
        now=now + timedelta(seconds=61),
    )
    assert validation["ok"] is False
    assert validation["error"]["code"] == "artifact_grant_expired"


def test_retry_path_mints_new_grants() -> None:
    now = datetime(2026, 4, 7, 12, 0, tzinfo=UTC)
    counter = {"value": 0}

    def _read_factory(artifact_id: str) -> str:
        counter["value"] += 1
        return f"https://signed.local/{artifact_id}/download?nonce={counter['value']}"

    first = build_artifact_access_descriptors(
        artifact_ids=["art_retry"],
        read_url_factory=_read_factory,
        ttl_seconds=300,
        now=now,
    )[0]
    second = build_artifact_access_descriptors(
        artifact_ids=["art_retry"],
        read_url_factory=_read_factory,
        ttl_seconds=300,
        now=now + timedelta(minutes=1),
    )[0]
    assert first["access"]["download_url"] != second["access"]["download_url"]
    assert first["access"]["expires_at"] != second["access"]["expires_at"]


def test_multi_tenant_isolation_for_same_artifact_shape() -> None:
    now = datetime(2026, 4, 7, 12, 0, tzinfo=UTC)
    descriptor = build_artifact_access_descriptors(
        artifact_ids=["art_0001"],
        read_url_factory=lambda aid: f"https://signed.local/{aid}/download?sig=4",
        ttl_seconds=300,
        scope={"account_id": "acc_a", "project_id": 10},
        now=now,
    )[0]
    denied = validate_artifact_descriptor_access(
        descriptor,
        scope={"account_id": "acc_b", "project_id": 10},
        expected_mode="read",
        now=now + timedelta(seconds=30),
    )
    allowed = validate_artifact_descriptor_access(
        descriptor,
        scope={"account_id": "acc_a", "project_id": 10},
        expected_mode="read",
        now=now + timedelta(seconds=30),
    )
    assert denied["ok"] is False
    assert denied["error"]["code"] == "artifact_access_denied"
    assert allowed["ok"] is True


def test_scope_guard_blocks_access_outside_run_and_step() -> None:
    now = datetime(2026, 4, 7, 12, 0, tzinfo=UTC)
    descriptor = build_artifact_access_descriptor(
        binding_key="input_file",
        role="input",
        artifact_id="art_scope",
        transport="presigned_url",
        mode="read",
        access_expires_at="2026-04-07T12:10:00Z",
        download_url="https://signed.local/art_scope/download",
        scope={"account_id": "acc_default", "run_id": "run_ok", "step_id": "step_ok"},
    )
    denied = validate_artifact_descriptor_access(
        descriptor,
        scope={"account_id": "acc_default", "run_id": "run_other", "step_id": "step_ok"},
        expected_mode="read",
        now=now,
    )
    assert denied["ok"] is False
    assert denied["error"]["code"] == "artifact_access_denied"


def _grant(scope: dict) -> dict:
    return build_artifact_access_descriptor(
        binding_key="input_file",
        role="input",
        artifact_id="art_group_scope",
        transport="presigned_url",
        mode="read",
        access_expires_at="2026-04-07T12:10:00Z",
        download_url="https://signed.local/art_group_scope/download",
        scope=scope,
    )


def _access(descriptor: dict, scope: dict) -> bool:
    return validate_artifact_descriptor_access(
        descriptor,
        scope=scope,
        expected_mode="read",
        now=datetime(2026, 4, 7, 12, 0, tzinfo=UTC),
    )["ok"]


_ROOT_STEP = {"account_id": "acc", "job_id": "9", "group_id": None, "step_id": "fetch"}
_GROUP_STEP = {"account_id": "acc", "job_id": "9", "group_id": "per_item", "step_id": "fetch"}


def test_explicit_null_group_binds_the_root_scope_not_a_wildcard() -> None:
    root_grant = _grant(dict(_ROOT_STEP))

    assert root_grant["scope"]["group_id"] is None
    assert _access(root_grant, dict(_ROOT_STEP)) is True
    assert _access(root_grant, {k: v for k, v in _ROOT_STEP.items() if k != "group_id"}) is True
    # A grant issued to a root step never authorizes the same step id in a group.
    assert _access(root_grant, dict(_GROUP_STEP)) is False


def test_group_bound_grant_does_not_authorize_root_and_cannot_be_widened_by_null() -> None:
    group_grant = _grant(dict(_GROUP_STEP))

    assert _access(group_grant, dict(_GROUP_STEP)) is True
    assert _access(group_grant, dict(_ROOT_STEP)) is False
    assert _access(group_grant, {**_GROUP_STEP, "group_id": ""}) is False
    assert _access(group_grant, {k: v for k, v in _GROUP_STEP.items() if k != "group_id"}) is False
    assert _access(group_grant, {**_GROUP_STEP, "group_id": "other_group"}) is False


def test_absent_group_field_keeps_a_broader_grant_unbound() -> None:
    unbound = _grant({"account_id": "acc", "job_id": "9"})

    assert "group_id" not in unbound["scope"]
    assert _access(unbound, dict(_ROOT_STEP)) is True
    assert _access(unbound, dict(_GROUP_STEP)) is True
    # Existing account and job constraints still hold.
    assert _access(unbound, {**_GROUP_STEP, "account_id": "acc_other"}) is False
    assert _access(unbound, {**_GROUP_STEP, "job_id": "10"}) is False


def test_execution_bound_grant_keeps_step_and_execution_constraints() -> None:
    execution = "55555555-5555-4555-8555-555555555555"
    grant = _grant({**_GROUP_STEP, "step_execution_id": execution})

    assert _access(grant, {**_GROUP_STEP, "step_execution_id": execution}) is True
    assert _access(grant, {**_GROUP_STEP, "step_execution_id": None}) is False
    assert (
        _access(grant, {**_GROUP_STEP, "step_execution_id": "66666666-6666-4666-8666-666666666666"})
        is False
    )
    assert (
        _access(grant, {**_GROUP_STEP, "step_id": "other", "step_execution_id": execution}) is False
    )


def test_handshake_request_and_confirmation() -> None:
    assert host_contract.build_handshake_request({"install_id": "pi_1"}) == {
        "install_id": "pi_1",
        "contract_version": "extension_host_v2",
        "execution_scope_fields": ["group_id", "for_each_id", "step_id", "step_execution_id"],
    }
    # The host names its own token even if a caller passed another one.
    assert (
        host_contract.build_handshake_request({"contract_version": "extension_host_v1"})[
            "contract_version"
        ]
        == "extension_host_v2"
    )
    assert (
        host_contract.handshake_contract_error(
            {
                "result": {
                    "contract_version": "extension_host_v2",
                    "execution_scope_fields": list(host_contract.EXECUTION_SCOPE_FIELDS),
                }
            }
        )
        == ""
    )
    assert "did not confirm" in host_contract.handshake_contract_error({"result": {"status": "ok"}})
    assert "did not confirm" in host_contract.handshake_contract_error({"result": None})
    foreign = host_contract.handshake_contract_error(
        {"result": {"contract_version": "extension_host_v1"}}
    )
    assert "another contract" in foreign
    # Text a runtime supplies never reaches install state verbatim.
    assert "extension_host_v1" not in foreign


def test_runtime_side_envelope_contract_check() -> None:
    scope = dict.fromkeys(host_contract.EXECUTION_SCOPE_FIELDS)
    assert (
        host_contract.envelope_contract_error(
            {"contract_version": "extension_host_v2", "scope": scope}
        )
        == ""
    )
    # The token alone is not the contract: the final scope must come with it.
    assert host_contract.envelope_contract_error({"contract_version": "extension_host_v2"})
    assert host_contract.envelope_contract_error({"contract_version": "extension_host_v1"})
    assert host_contract.envelope_contract_error({})


def test_canonical_scope_never_carries_a_phase() -> None:
    scope = host_contract.canonical_envelope_scope(
        {
            "account_id": "acc",
            "phase_id": "legacy",
            "group_id": "",
            "for_each_id": None,
            "step_id": "s",
            "step_execution_id": "44444444-4444-4444-8444-444444444444",
        }
    )

    assert "phase_id" not in scope
    assert scope["group_id"] is None
    assert scope["for_each_id"] is None
    assert scope["step_id"] == "s"


def test_client_handshake_sends_the_host_contract() -> None:
    calls: list[dict] = []

    def _http_call(method, url, headers, payload, timeout):
        calls.append({"url": url, "payload": payload})
        return 200, {"ok": True, "result": {"contract_version": "extension_host_v2"}}

    client = RemoteRuntimeClient(
        base_url="https://runtime.example.com",
        endpoints={"handshake": "/handshake"},
        http_call=_http_call,
    )

    response = client.handshake({"install_id": "pi_1"})

    assert response["ok"] is True
    assert calls == [
        {
            "url": "https://runtime.example.com/handshake",
            "payload": {
                "install_id": "pi_1",
                "contract_version": "extension_host_v2",
                "execution_scope_fields": list(host_contract.EXECUTION_SCOPE_FIELDS),
            },
        }
    ]


def test_transform_file_pattern_requires_new_output_artifact() -> None:
    input_descriptor = build_artifact_access_descriptor(
        binding_key="input_file",
        role="input",
        artifact_id="art_immutable_in",
        filename="input.csv",
        transport="presigned_url",
        mode="read",
        access_expires_at="2026-04-07T12:15:00Z",
        download_url="https://signed.local/art_immutable_in/download",
    )
    same_output_descriptor = build_artifact_access_descriptor(
        binding_key="output_file",
        role="output",
        artifact_id="art_immutable_in",
        filename="input-updated.csv",
        transport="presigned_url",
        mode="write",
        access_expires_at="2026-04-07T12:15:00Z",
        upload_url="https://signed.local/art_immutable_in/upload",
    )
    with pytest.raises(ValueError, match="immutable"):
        build_action_envelope(
            action_id="transform",
            input_payload={"mode": "replace"},
            scope={"account_id": "acc_default"},
            connection={"connection_id": "conn_1"},
            artifact_inputs=[input_descriptor],
            artifact_outputs=[same_output_descriptor],
        )

    new_output_descriptor = build_artifact_access_descriptor(
        binding_key="output_file",
        role="output",
        artifact_id="art_immutable_out",
        filename="input-updated.csv",
        transport="presigned_url",
        mode="write",
        access_expires_at="2026-04-07T12:15:00Z",
        upload_url="https://signed.local/art_immutable_out/upload",
    )
    envelope = build_action_envelope(
        action_id="transform",
        input_payload={"mode": "replace"},
        scope={"account_id": "acc_default"},
        connection={"connection_id": "conn_1"},
        artifact_inputs=[input_descriptor],
        artifact_outputs=[new_output_descriptor],
    )
    assert envelope["artifacts"]["inputs"][0]["artifact_id"] == "art_immutable_in"
    assert envelope["artifacts"]["outputs"][0]["artifact_id"] == "art_immutable_out"


def test_canonical_artifact_error_and_audit_record() -> None:
    unsupported = canonical_artifact_error("something_unknown", "oops", artifact_id="art_1")
    assert unsupported["code"] == "artifact_transport_unsupported"
    assert unsupported["code"] in CANONICAL_ARTIFACT_ERROR_CODES

    audit = build_artifact_audit_record(
        account_id="acc_default",
        project_id=14,
        workflow_id="inventory_sync",
        run_id="run_1",
        group_id=None,
        for_each_id=None,
        step_id="step_1",
        step_execution_id="44444444-4444-4444-8444-444444444444",
        extension_id="fixture_files_bridge",
        install_id="pi_1",
        operation_id="transform_excel",
        artifact_id="art_1",
        grant_mode="read",
        grant_transport="presigned_url",
        issued_at="2026-04-07T11:50:00Z",
        expires_at="2026-04-07T12:00:00Z",
        final_status="used",
    )
    assert audit["artifact_id"] == "art_1"
    assert audit["group_id"] is None
    assert audit["for_each_id"] is None
    assert audit["step_id"] == "step_1"
    assert audit["step_execution_id"] == "44444444-4444-4444-8444-444444444444"
    assert "phase_id" not in audit
    assert audit["grant_transport"] == "presigned_url"
    assert audit["final_status"] == "used"


def test_remote_runtime_client_reuses_single_http_path_and_normalizes_response() -> None:
    calls: list[dict] = []

    def _http_call(method, url, headers, payload, timeout):
        calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "payload": payload,
                "timeout": timeout,
            }
        )
        if url.endswith("/health"):
            return 200, {"ok": True, "result": {"status": "ok"}}
        if "/actions/" in url:
            return 404, {
                "ok": False,
                "error": {"code": "unknown_action", "message": "missing"},
                "errors": [{"code": "unknown_action", "message": "missing"}],
            }
        return 200, {
            "ok": True,
            "result": {"echo": payload},
            "commands": [{"type": "notify", "message": "next"}],
            "events": [{"event_name": "job.completed"}],
            "reload": ["builder_catalog"],
        }

    client = RemoteRuntimeClient(
        base_url="https://runtime.example.com/flow-steward",
        endpoints={
            "handshake": "/handshake",
            "health": "/health",
            "query": "/queries/{query_id}",
            "action": "/actions/{action_id}",
            "emit_event": "/events",
        },
        bearer_token="token-123",
        http_call=_http_call,
    )

    health = client.health()
    action = client.invoke_action("missing_action", {"kind": "action"})
    query = client.invoke_query("list_projects", {"kind": "query"})

    assert health["ok"] is True
    assert action["ok"] is False
    assert action["error"]["code"] == "unknown_action"
    assert action["errors"][0]["code"] == "unknown_action"
    assert query["ok"] is True
    assert query["commands"][0]["type"] == "notify"
    assert query["events"][0]["event_name"] == "job.completed"
    assert query["reload"] == ["builder_catalog"]
    assert len(calls) == 3
    assert all(call["headers"]["Authorization"] == "Bearer token-123" for call in calls)


def test_remote_runtime_normalizer_preserves_only_closed_retry_facts() -> None:
    normalized = protocol._normalize_response(
        429,
        {
            "ok": False,
            "errors": [{"code": "rate_limited"}],
            "failure_class": "provider",
            "retryable": True,
            "retry_after_seconds": 13,
            "definitely_no_external_effect": True,
            "external_effect_status": "failed",
            "provider_error_code": "rate_limited",
            "secret_header": "must-not-cross",  # pragma: allowlist secret - boundary fixture
        },
    )

    assert (
        normalized.items()
        >= {
            "failure_class": "provider",
            "retryable": True,
            "retry_after_seconds": 13.0,
            "definitely_no_external_effect": True,
            "external_effect_status": "failed",
            "provider_error_code": "rate_limited",
            "http_status": 429,
        }.items()
    )
    assert "secret_header" not in normalized


@pytest.mark.parametrize(
    ("status_code", "payload"),
    [
        (500, {"ok": True, "result": {"unsafe": "success"}}),
        (200, {"ok": "false", "result": {"unsafe": "success"}}),
    ],
)
def test_remote_runtime_never_accepts_malformed_success(status_code, payload) -> None:
    normalized = protocol._normalize_response(status_code, payload)

    assert normalized["ok"] is False
    assert normalized["result"] is None
    assert normalized["error"]["code"] == "invalid_response"


def test_remote_client_build_rejects_http_before_attaching_credentials() -> None:
    from core.infrastructure.db.remote_extension_runtime import _build_remote_client

    with pytest.raises(ValueError, match="must use https"):
        _build_remote_client(
            {
                "remote_base_url": "http://1.1.1.1",
                "remote_bearer_token": "secret-token",
            }
        )


def test_remote_client_preserves_subsecond_deadline_remainder(monkeypatch) -> None:
    from core.infrastructure.db.remote_extension_runtime import _build_remote_client

    monkeypatch.setattr(
        "core.application.extensions.url_safety.assert_safe_remote_https_url",
        lambda value, **_kwargs: value,
    )

    client = _build_remote_client(
        {
            "remote_base_url": "https://runtime.example.com",
            "runtime": {
                "remote": {
                    "endpoints": {
                        "handshake": "/handshake",
                        "health": "/health",
                        "query": "/queries/{query_id}",
                        "action": "/actions/{action_id}",
                        "emit_event": "/events",
                    }
                }
            },
        },
        timeout_seconds=0.05,
    )

    assert client.timeout_seconds == pytest.approx(0.05)


def test_client_raises_when_endpoint_missing() -> None:
    client = RemoteRuntimeClient(
        base_url="https://runtime.example.com",
        endpoints={"health": "/health"},
    )
    try:
        client.handshake({})
    except ValueError as exc:
        assert "handshake" in str(exc)
    else:
        raise AssertionError("Expected ValueError for missing handshake endpoint")


def test_client_rejects_absolute_endpoint_urls() -> None:
    client = RemoteRuntimeClient(
        base_url="https://runtime.example.com/flow-steward",
        endpoints={
            "handshake": "https://evil.example/steal",
            "health": "/health",
            "query": "/queries/{query_id}",
            "action": "/actions/{action_id}",
            "emit_event": "/events",
        },
        bearer_token="token-123",
        http_call=lambda *_args, **_kwargs: (200, {"ok": True, "result": {}}),
    )

    try:
        client.handshake({"extension_id": "x"})
    except ValueError as exc:
        assert "relative" in str(exc).lower()
    else:
        raise AssertionError("Expected ValueError for absolute endpoint URL")


def test_client_rejects_redirect_without_following_location(monkeypatch) -> None:
    opened_urls: list[str] = []

    def _redirect(request, *, timeout_seconds: float):
        opened_urls.append(request.full_url)
        raise HTTPError(
            request.full_url,
            302,
            "Found",
            {"Location": "http://127.0.0.1/internal"},
            None,
        )

    monkeypatch.setattr(protocol, "_open_url_no_redirect", _redirect)
    client = RemoteRuntimeClient(
        base_url="https://runtime.example.com/flow-steward",
        endpoints={
            "handshake": "/handshake",
            "health": "/health",
            "query": "/queries/{query_id}",
            "action": "/actions/{action_id}",
            "emit_event": "/events",
        },
    )

    with pytest.raises(HTTPError) as exc:
        client.health()

    assert exc.value.code == 302
    assert opened_urls == ["https://runtime.example.com/flow-steward/health"]


def test_remote_runtime_client_blocks_dns_rebound_private_peer(monkeypatch) -> None:
    """CAND-007/008/050: a stored remote runtime base URL that passes preflight
    as public but rebinds to a private peer at connect time must be blocked
    before any bearer-bearing request is sent."""
    import socket

    from core.application.extensions import url_safety

    monkeypatch.delenv("FS_ALLOW_PRIVATE_REMOTE_URLS", raising=False)
    # Preflight + pin resolve to a public IP.
    monkeypatch.setattr(
        url_safety._descriptor_io,
        "_resolve_hostname_ips",
        lambda _host, *, port: {"93.184.216.34"},
    )

    class _RebindSocket:
        def getpeername(self):
            return ("10.0.0.7", 443)

        def close(self):
            return None

    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: _RebindSocket())

    client = RemoteRuntimeClient(
        base_url="https://runtime.example.com/flow-steward",
        endpoints={
            "handshake": "/handshake",
            "health": "/health",
            "query": "/queries/{query_id}",
            "action": "/actions/{action_id}",
            "emit_event": "/events",
        },
        bearer_token="secret-token",
    )

    with pytest.raises(url_safety.PinnedPeerError):
        client.health()
