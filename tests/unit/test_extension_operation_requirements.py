from __future__ import annotations

import asyncio
import threading

import pytest

from core.application.extensions import operation_audit, operation_support, operation_targets
from core.application.extensions import operation_service as operation_runtime
from core.application.extensions.binding_service import ExtensionBindingService
from core.application.management.errors import ServiceForbiddenError, ServiceValidationError
from core.application.management.requests import ExtensionActionRequest
from core.application.management.scope import ScopeContext


def _extension() -> dict:
    return {
        "extension_key": "fixture.mail",
        "operation_manifest": [
            {
                "operation_id": "send_message",
                "connection_type_ids": ["mail_account"],
                "required_oauth_scopes": ["mail.send"],
            }
        ],
    }


def _connection(**overrides) -> dict:
    row = {
        "connection_id": "connection-1",
        "account_id": "account-1",
        "project_id": "project-1",
        "extension_id": "fixture.mail",
        "extension_install_id": "install-1",
        "connection_type": "mail_account",
        "oauth_configuration_id": "configuration-1",
        "oauth_provider_id": "provider-1",
        "oauth_granted_scopes": ["profile", "mail.send"],
        "enabled": True,
    }
    row.update(overrides)
    return row


class _Store:
    def __init__(self, connection: dict) -> None:
        self.connection = connection

    def get_project_connection(self, **_kwargs):
        return self.connection


class _IdentityStoreWithoutProjectLookup(_Store):
    conn = object()

    def get_extension_install_for_operator(self, _install_id, *, account_id):
        return {"account_id": account_id}


def test_operation_requirements_are_compiled_from_the_installed_contract() -> None:
    contract = operation_support.extension_operation_contract(
        _extension(), operation_id="send_message"
    )

    assert contract.connection_type_ids == frozenset({"mail_account"})
    assert contract.required_oauth_scopes == frozenset({"mail.send"})


def test_operation_outputs_are_compiled_from_the_installed_contract() -> None:
    extension = _extension()
    extension["operation_manifest"][0]["outputs"] = [
        {
            "name": "messages",
            "value_type": "array",
            "schema": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                },
            },
        }
    ]

    contract = operation_support.extension_operation_contract(
        extension, operation_id="send_message"
    )

    assert contract.outputs == (
        {
            "name": "messages",
            "value_type": "array",
            "schema": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                },
            },
        },
    )


@pytest.mark.parametrize(
    "connection",
    [
        _connection(connection_type="other_account"),
        _connection(oauth_granted_scopes=["profile"]),
        _connection(oauth_granted_scopes="mail.send"),
    ],
)
def test_target_rejects_wrong_connection_type_or_missing_scope_before_token_resolution(
    monkeypatch: pytest.MonkeyPatch, connection: dict
) -> None:
    monkeypatch.setattr(
        operation_targets,
        "_resolve_extension_owned_oauth_runtime",
        lambda *_args, **_kwargs: pytest.fail("token resolution must not run"),
    )

    _raises_input_121_1 = _Store(connection)
    _raises_input_121_2 = frozenset({"mail_account"})
    _raises_input_121_3 = frozenset({"mail.send"})
    with pytest.raises(ServiceValidationError, match="connection_runtime_requirement_failed"):
        operation_targets._direct_action_connection_target(
            _raises_input_121_1,
            connection_id="connection-1",
            account_id="account-1",
            project_id="project-1",
            extension_id="fixture.mail",
            extension_install_id="install-1",
            required_connection_type_ids=_raises_input_121_2,
            required_oauth_scopes=_raises_input_121_3,
        )


def test_target_resolves_token_after_neutral_runtime_requirements_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def resolve(_store, *, connection, get_project_by_id=None):
        calls.append(connection)
        assert callable(get_project_by_id)
        return {"access_token": "runtime-token"}

    monkeypatch.setattr(operation_targets, "_resolve_extension_owned_oauth_runtime", resolve)
    result = operation_targets._direct_action_connection_target(
        _Store(_connection()),
        connection_id="connection-1",
        account_id="account-1",
        project_id="project-1",
        extension_id="fixture.mail",
        extension_install_id="install-1",
        required_connection_type_ids=frozenset({"mail_account"}),
        required_oauth_scopes=frozenset({"mail.send"}),
        get_project_by_id=lambda _project_id: {
            "id": "project-1",
            "account_id": "account-1",
            "tenant_id": "tenant-1",
        },
    )

    assert result["secrets"] == {"access_token": "runtime-token"}
    assert calls == [_connection()]


def test_extension_oauth_runtime_uses_explicit_project_loader_with_identity_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.application.extensions import oauth_runtime

    captured: dict[str, object] = {}

    class RuntimeTokenService:
        def __init__(self, **_kwargs) -> None:
            pass

        def resolve_runtime_access_token(self, **kwargs):
            captured.update(kwargs)
            return {"access_token": "runtime-token"}

    monkeypatch.setattr(oauth_runtime, "OAuthRuntimeTokenService", RuntimeTokenService)
    result = operation_targets._resolve_extension_owned_oauth_runtime(
        _IdentityStoreWithoutProjectLookup(_connection()),
        connection=_connection(),
        get_project_by_id=lambda project_id: {
            "id": project_id,
            "account_id": "account-1",
            "tenant_id": "tenant-1",
        },
    )

    assert result == {"access_token": "runtime-token"}
    assert captured["tenant_id"] == "tenant-1"
    assert captured["project_id"] == "project-1"


def test_extension_oauth_runtime_fails_without_any_project_loader() -> None:
    _raises_input_197_1 = _IdentityStoreWithoutProjectLookup(_connection())
    _raises_input_197_2 = _connection()
    with pytest.raises(RuntimeError, match="runtime dependencies unavailable"):
        operation_targets._resolve_extension_owned_oauth_runtime(
            _raises_input_197_1, connection=_raises_input_197_2
        )


def test_binding_service_applies_installed_operation_requirements_before_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extension = {
        **_extension(),
        "extension_id": "fixture.mail",
        "extension_install_id": "install-1",
        "enabled": True,
        "required_scopes": ["extension:invoke"],
        "action_definitions": [
            {
                "action_id": "send_message",
                "handler": {"mode": "extension"},
                "required_scopes": ["extension:invoke"],
            }
        ],
    }
    store = _Store(_connection(oauth_granted_scopes=["profile"]))
    monkeypatch.setattr(
        operation_targets,
        "_resolve_extension_owned_oauth_runtime",
        lambda *_args, **_kwargs: pytest.fail("token resolution must not run"),
    )
    service = ExtensionBindingService(
        get_logger=lambda: store,
        get_operation_store=lambda: store,
        resolve_extension_runtime_record=lambda _extension_id: (extension, store),
        extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
        extension_operation_handler_mode=lambda *_args, **_kwargs: "extension",
        extension_operation_failure_status=lambda _invocation: 400,
        get_project_by_id=lambda _project_id: {
            "id": "project-1",
            "account_id": "account-1",
        },
    )

    _raises_input_240_1 = ExtensionActionRequest(target={"connection_id": "connection-1"}, input={})
    _raises_input_240_2 = ScopeContext(
        account_id="account-1", project_id="project-1", allowed_project_ids=frozenset({"project-1"})
    )
    with pytest.raises(ServiceValidationError, match="connection_runtime_requirement_failed"):
        service.execute_action(
            "fixture.mail", "send_message", _raises_input_240_1, scope=_raises_input_240_2
        )


def test_operation_service_passes_exact_install_to_extension_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.application.extensions.operation_service import ExtensionOperationService
    from core.infrastructure.extension_runtime import extension_runtime

    extension = {
        **_extension(),
        "extension_id": "fixture.mail",
        "extension_install_id": "install-1",
        "enabled": True,
        "external_resource_picker": {"bootstrap_action_id": "send_message"},
        "required_scopes": ["extension:invoke"],
        "action_definitions": [
            {
                "action_id": "send_message",
                "handler": {"mode": "extension"},
                "required_scopes": ["extension:invoke"],
            }
        ],
    }
    runtime_contexts: list[dict] = []
    runtime_targets: list[dict] = []
    monkeypatch.setattr(
        operation_targets,
        "_resolve_extension_owned_oauth_runtime",
        lambda *_args, **_kwargs: {"access_token": "runtime-token"},
    )
    monkeypatch.setattr(
        extension_runtime,
        "run_extension_action",
        lambda *_args, **kwargs: (
            runtime_contexts.append(dict(kwargs["runtime_context"]))
            or runtime_targets.append(dict(kwargs["target"]))
            or {"status": "succeeded"}
        ),
    )
    service = ExtensionOperationService(
        get_logger=lambda: _Store(_connection()),
        get_operation_store=lambda: _Store(_connection()),
        resolve_extension_runtime_record=lambda _extension_id: (extension, _Store(_connection())),
        extension_operation_handler_mode=lambda *_args, **_kwargs: "extension",
        extension_operation_failure_status=lambda _invocation: 400,
        get_project_by_id=lambda _project_id: {"id": "project-1", "account_id": "account-1"},
    )

    context = service._prepare_action_execution(
        "fixture.mail",
        "send_message",
        ExtensionActionRequest(target={"connection_id": "connection-1"}, input={}),
        ScopeContext(
            account_id="account-1",
            project_id="project-1",
            allowed_project_ids=frozenset({"project-1"}),
        ),
    )
    service._invoke_action_with_grants(context)

    assert runtime_contexts == [
        {
            "account_id": "account-1",
            "project_id": "project-1",
            "extension_install_id": "install-1",
            "connection_id": "connection-1",
            "oauth_configuration_id": "configuration-1",
            "oauth_provider_id": "provider-1",
            "logger": context.bl,
        }
    ]
    assert runtime_targets[0]["connection"]["secrets"] == {"access_token": "runtime-token"}


def test_operator_extension_action_requires_trusted_project_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.application.extensions.operation_service import ExtensionOperationService
    from core.infrastructure.extension_runtime import extension_runtime

    extension = {
        **_extension(),
        "extension_id": "fixture.mail",
        "extension_install_id": "install-1",
        "enabled": True,
        "required_scopes": ["extension:invoke"],
        "action_definitions": [
            {
                "action_id": "send_message",
                "handler": {"mode": "extension"},
                "required_scopes": ["extension:invoke"],
            }
        ],
    }
    monkeypatch.setattr(
        extension_runtime,
        "run_extension_action",
        lambda *_args, **_kwargs: pytest.fail("unscoped operator action must not reach runtime"),
    )
    service = ExtensionOperationService(
        get_logger=lambda: _Store(_connection()),
        get_operation_store=lambda: _Store(_connection()),
        resolve_extension_runtime_record=lambda _extension_id: (extension, _Store(_connection())),
        extension_operation_handler_mode=lambda *_args, **_kwargs: "extension",
        extension_operation_failure_status=lambda _invocation: 400,
        get_project_by_id=lambda _project_id: {"id": "project-1", "account_id": "account-1"},
    )

    _raises_input_362_1 = ExtensionActionRequest(input={})
    _raises_input_362_2 = ScopeContext(
        account_id="account-1", actor_id="operator-1", principal_type="operator"
    )
    with pytest.raises(ServiceForbiddenError, match="project context"):
        service.execute_action(
            "fixture.mail", "send_message", _raises_input_362_1, scope=_raises_input_362_2
        )


@pytest.mark.parametrize(
    ("project", "allowed_project_ids"),
    [
        ({"id": "project-1", "account_id": "account-2"}, frozenset()),
        (
            {"id": "project-1", "account_id": "account-1"},
            frozenset({"project-2"}),
        ),
    ],
)
def test_operator_extension_action_rejects_foreign_project_scope_before_runtime(
    monkeypatch: pytest.MonkeyPatch,
    project: dict,
    allowed_project_ids: frozenset[str],
) -> None:
    from core.application.extensions.operation_service import ExtensionOperationService
    from core.infrastructure.extension_runtime import extension_runtime

    extension = {
        **_extension(),
        "extension_id": "fixture.mail",
        "extension_install_id": "install-1",
        "enabled": True,
        "required_scopes": ["extension:invoke"],
        "action_definitions": [
            {
                "action_id": "send_message",
                "handler": {"mode": "extension"},
                "required_scopes": ["extension:invoke"],
            }
        ],
    }
    monkeypatch.setattr(
        extension_runtime,
        "run_extension_action",
        lambda *_args, **_kwargs: pytest.fail("foreign project action must not reach runtime"),
    )
    service = ExtensionOperationService(
        get_logger=lambda: _Store(_connection()),
        get_operation_store=lambda: _Store(_connection()),
        resolve_extension_runtime_record=lambda _extension_id: (extension, _Store(_connection())),
        extension_operation_handler_mode=lambda *_args, **_kwargs: "extension",
        extension_operation_failure_status=lambda _invocation: 400,
        get_project_by_id=lambda _project_id: project,
    )

    _raises_input_421_1 = ExtensionActionRequest(input={})
    _raises_input_421_2 = ScopeContext(
        account_id="account-1",
        project_id="project-1",
        actor_id="operator-1",
        principal_type="operator",
        allowed_project_ids=allowed_project_ids,
    )
    with pytest.raises(ServiceForbiddenError, match="Access denied"):
        service.execute_action(
            "fixture.mail", "send_message", _raises_input_421_1, scope=_raises_input_421_2
        )


@pytest.mark.parametrize(
    ("reflected_value", "as_result_key"),
    [
        ("runtime-token", False),
        ("Bearer runtime-token", False),
        ("x", False),
        ("runtime-token", True),
        ("x", True),
    ],
)
def test_normal_action_blocks_reflected_runtime_secret_before_processing_and_audit(
    monkeypatch: pytest.MonkeyPatch,
    reflected_value: str,
    as_result_key: bool,
) -> None:
    from core.application.extensions.operation_service import ExtensionOperationService
    from core.application.management.errors import ServiceError
    from core.infrastructure.extension_runtime import extension_runtime

    extension = {
        **_extension(),
        "extension_id": "fixture.mail",
        "extension_install_id": "install-1",
        "enabled": True,
        "required_scopes": ["extension:invoke"],
        "action_definitions": [
            {
                "action_id": "send_message",
                "handler": {"mode": "extension"},
                "required_scopes": ["extension:invoke"],
            }
        ],
    }
    runtime_targets: list[dict] = []
    audit_invocations: list[dict] = []
    cleanup_confirmation: list[bool] = []
    monkeypatch.setattr(
        operation_targets,
        "_resolve_extension_owned_oauth_runtime",
        lambda *_args, **_kwargs: {"access_token": "runtime-token", "api_key": "x"},
    )
    monkeypatch.setattr(
        extension_runtime,
        "run_extension_action",
        lambda *_args, **kwargs: (
            runtime_targets.append(dict(kwargs["target"]))
            or {
                "status": "succeeded",
                "response": {
                    "ok": True,
                    "result": (
                        {reflected_value: True} if as_result_key else {"ticket": reflected_value}
                    ),
                },
                "stdout": reflected_value,
                "stderr": f"provider said {reflected_value}",
            }
        ),
    )
    monkeypatch.setattr(
        operation_audit,
        "log_host_extension_invocation",
        lambda **kwargs: audit_invocations.append(dict(kwargs["invocation"])),
    )
    service = ExtensionOperationService(
        get_logger=lambda: _Store(_connection()),
        get_operation_store=lambda: _Store(_connection()),
        resolve_extension_runtime_record=lambda _extension_id: (extension, _Store(_connection())),
        extension_operation_handler_mode=lambda *_args, **_kwargs: "extension",
        extension_operation_failure_status=lambda _invocation: 400,
        get_project_by_id=lambda _project_id: {"id": "project-1", "account_id": "account-1"},
    )
    monkeypatch.setattr(
        service,
        "_process_action_response",
        lambda *_args, **_kwargs: pytest.fail("response processing must not run"),
    )
    monkeypatch.setattr(
        service,
        "_finalize_action_grants",
        lambda *_args, **_kwargs: pytest.fail("grant finalization must not run"),
    )
    original_cleanup = service._cleanup_action_resources

    def capture_cleanup(
        context,
        invocation,
        *,
        confirm_external_effect=True,
        protected_values=frozenset(),
        finalized_grants=None,
    ):
        cleanup_confirmation.append(confirm_external_effect)
        return original_cleanup(
            context,
            invocation,
            confirm_external_effect=confirm_external_effect,
            protected_values=protected_values,
            finalized_grants=finalized_grants,
        )

    monkeypatch.setattr(service, "_cleanup_action_resources", capture_cleanup)

    _raises_input_538_1 = ExtensionActionRequest(target={"connection_id": "connection-1"}, input={})
    _raises_input_538_2 = ScopeContext(
        account_id="account-1", project_id="project-1", allowed_project_ids=frozenset({"project-1"})
    )
    with pytest.raises(ServiceError) as captured:
        service.execute_action(
            "fixture.mail", "send_message", _raises_input_538_1, scope=_raises_input_538_2
        )

    assert runtime_targets[0]["connection"]["secrets"] == {
        "access_token": "runtime-token",
        "api_key": "x",
    }
    assert captured.value.payload["action"] == {
        "status": "failed",
        "detail": "extension_secret_exposure_blocked",
        "response": {
            "ok": False,
            "error": "The extension returned protected credential material. Contact the extension author.",
            "error_code": "extension_secret_exposure_blocked",
        },
        "errors": [{"code": "extension_secret_exposure_blocked"}],
    }
    assert cleanup_confirmation == [True]
    assert audit_invocations == [captured.value.payload["action"]]
    assert "runtime-token" not in repr(captured.value.payload)
    assert "runtime-token" not in repr(audit_invocations)


def test_normal_action_blocks_short_secret_embedded_only_in_nested_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.application.extensions.operation_service import ExtensionOperationService
    from core.application.management.errors import ServiceError
    from core.infrastructure.extension_runtime import extension_runtime

    extension = {
        **_extension(),
        "extension_id": "fixture.mail",
        "extension_install_id": "install-1",
        "enabled": True,
        "required_scopes": ["extension:invoke"],
        "action_definitions": [
            {
                "action_id": "send_message",
                "handler": {"mode": "extension"},
                "required_scopes": ["extension:invoke"],
            }
        ],
    }
    audit_invocations: list[dict] = []
    cleanup_confirmation: list[bool] = []
    monkeypatch.setattr(
        operation_targets,
        "_resolve_extension_owned_oauth_runtime",
        lambda *_args, **_kwargs: {"api_key": "x"},
    )
    monkeypatch.setattr(
        extension_runtime,
        "run_extension_action",
        lambda *_args, **_kwargs: {
            "status": "succeeded",
            "response": {
                "ok": True,
                "result": {"messages": [{"text": "provider echoed prefix-x-suffix"}]},
            },
            "stdout": "runtime completed",
            "stderr": "",
        },
    )
    monkeypatch.setattr(
        operation_audit,
        "log_host_extension_invocation",
        lambda **kwargs: audit_invocations.append(dict(kwargs["invocation"])),
    )
    service = ExtensionOperationService(
        get_logger=lambda: _Store(_connection()),
        get_operation_store=lambda: _Store(_connection()),
        resolve_extension_runtime_record=lambda _extension_id: (extension, _Store(_connection())),
        extension_operation_handler_mode=lambda *_args, **_kwargs: "extension",
        extension_operation_failure_status=lambda _invocation: 400,
        get_project_by_id=lambda _project_id: {"id": "project-1", "account_id": "account-1"},
    )
    monkeypatch.setattr(
        service,
        "_process_action_response",
        lambda *_args, **_kwargs: pytest.fail("response processing must not run"),
    )
    monkeypatch.setattr(
        service,
        "_finalize_action_grants",
        lambda *_args, **_kwargs: pytest.fail("grant finalization must not run"),
    )
    original_cleanup = service._cleanup_action_resources

    def capture_cleanup(
        context,
        invocation,
        *,
        confirm_external_effect=True,
        protected_values=frozenset(),
        finalized_grants=None,
    ):
        cleanup_confirmation.append(confirm_external_effect)
        return original_cleanup(
            context,
            invocation,
            confirm_external_effect=confirm_external_effect,
            protected_values=protected_values,
            finalized_grants=finalized_grants,
        )

    monkeypatch.setattr(service, "_cleanup_action_resources", capture_cleanup)

    _raises_input_654_1 = ExtensionActionRequest(target={"connection_id": "connection-1"}, input={})
    _raises_input_654_2 = ScopeContext(
        account_id="account-1", project_id="project-1", allowed_project_ids=frozenset({"project-1"})
    )
    with pytest.raises(ServiceError) as captured:
        service.execute_action(
            "fixture.mail", "send_message", _raises_input_654_1, scope=_raises_input_654_2
        )

    assert captured.value.payload["action"] == {
        "status": "failed",
        "detail": "extension_secret_exposure_blocked",
        "response": {
            "ok": False,
            "error": "The extension returned protected credential material. Contact the extension author.",
            "error_code": "extension_secret_exposure_blocked",
        },
        "errors": [{"code": "extension_secret_exposure_blocked"}],
    }
    assert cleanup_confirmation == [True]
    assert audit_invocations == [captured.value.payload["action"]]
    assert "prefix-x-suffix" not in repr(captured.value.payload)
    assert "prefix-x-suffix" not in repr(audit_invocations)


@pytest.mark.parametrize(
    ("secret", "candidate", "expected"),
    [
        ("", "ignored", False),
        ("x", "", False),
        ("x", "x", True),
        ("x", "prefix-x-suffix", True),
        ("x", "next", False),
        ("aa", "aaaa", False),
        ("aa", "aa-aa", True),
        ("x", "éxé", False),
        ("x", "_x_", True),
        ("x", "prefixxsuffix", False),
        ("runtime-token", "Bearer runtime-token", True),
    ],
)
def test_secret_exposure_detection_uses_unicode_alphanumeric_boundaries(
    secret: str,
    candidate: str,
    expected: bool,
) -> None:
    from core.application.extensions.secret_reflection import value_exposes_secret

    invocation = {
        "status": "succeeded",
        "response": {"result": {"messages": [{"text": candidate}]}},
    }

    assert value_exposes_secret(invocation, frozenset({secret})) is expected


def test_short_secret_undelimited_alphanumeric_text_is_intentionally_allowed() -> None:
    """The reflection guard avoids ordinary-word false positives; it is not absolute DLP."""
    from core.application.extensions.secret_reflection import candidate_exposes_secret

    assert candidate_exposes_secret("prefixxsuffix", "x") is False


@pytest.mark.parametrize(
    ("invocation", "secrets", "expected"),
    [
        ({"prefix-x-suffix": "safe"}, frozenset({"x"}), True),
        ({123: "safe"}, frozenset({"123"}), False),
        ({"result": (["safe", {"deep": "prefix-x-suffix"}],)}, frozenset({"x"}), True),
        ({"result": "Bearer runtime-token"}, frozenset({"x", "runtime-token"}), True),
    ],
)
def test_secret_exposure_detection_covers_supported_nested_candidates(
    invocation: dict,
    secrets: frozenset[str],
    expected: bool,
) -> None:
    from core.application.extensions.secret_reflection import value_exposes_secret

    assert value_exposes_secret(invocation, secrets) is expected


def test_short_secret_scan_does_not_execute_python_per_occurrence() -> None:
    from core.application.extensions.secret_reflection import candidate_exposes_secret

    class FindCountingText(str):
        find_calls = 0

        def find(self, *args, **kwargs):
            self.find_calls += 1
            return super().find(*args, **kwargs)

    candidate = FindCountingText("x" * (64 * 1024))
    exposed = candidate_exposes_secret(candidate, "x")

    assert exposed is False
    assert candidate.find_calls <= 1


def test_many_secrets_large_candidate_fails_closed_before_per_secret_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.application.extensions import secret_reflection

    calls = 0

    def count_scan(_candidate: str, _secret: str) -> bool:
        nonlocal calls
        calls += 1
        return False

    monkeypatch.setattr(secret_reflection, "candidate_exposes_secret", count_scan)
    candidate = "z" * (5 * 1024 * 1024)
    secrets = frozenset(f"s{index:03d}" for index in range(100))

    assert secret_reflection.value_exposes_secret(candidate, secrets) is True
    assert calls == 0


def test_oversized_secret_iterable_stops_normalization_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.application.extensions import secret_reflection

    visited = 0

    def secrets():
        nonlocal visited
        for index in range(1_000):
            visited += 1
            yield f"secret-{index:04d}"

    monkeypatch.setattr(secret_reflection, "_MAX_SECRET_COUNT", 8)

    assert secret_reflection.value_exposes_secret("safe", secrets()) is True
    assert visited == 9


def test_large_non_string_array_fails_closed_at_total_node_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.application.extensions import secret_reflection

    class CountingList(list):
        visited = 0

        def __iter__(self):
            for value in super().__iter__():
                self.visited += 1
                yield value

    values = CountingList(range(1_000))
    monkeypatch.setattr(secret_reflection, "_MAX_TRAVERSAL_NODES", 8, raising=False)

    assert secret_reflection.value_exposes_secret(values, frozenset({"runtime-token"})) is True
    assert values.visited <= 8


def test_empty_candidate_comparisons_still_consume_work_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.application.extensions import secret_reflection

    monkeypatch.setattr(secret_reflection, "_MAX_COMPARISON_CODEPOINTS", 8)

    assert (
        secret_reflection.value_exposes_secret(
            ["", "", "", "", ""], frozenset({"runtime-token", "another-token"})
        )
        is True
    )


def test_excessive_secret_scan_nesting_fails_closed_without_recursion_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.application.extensions import secret_reflection

    nested: object = "safe"
    for _index in range(32):
        nested = [nested]
    monkeypatch.setattr(secret_reflection, "_MAX_NESTING_DEPTH", 8, raising=False)

    assert secret_reflection.value_exposes_secret(nested, frozenset({"runtime-token"})) is True


def test_secret_scan_allows_ordinary_safe_nested_output() -> None:
    from core.application.extensions.secret_reflection import value_exposes_secret

    invocation = {
        "status": "succeeded",
        "response": {
            "ok": True,
            "result": ["safe", {"nested": ("still safe", 42, None)}],
        },
    }

    assert value_exposes_secret(invocation, frozenset({"runtime-token", "x"})) is False


def test_oversized_hydrated_secret_map_stops_early_and_blocks_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.application.extensions import operation_service
    from core.application.extensions.operation_service import ExtensionOperationService
    from core.application.management.errors import ServiceError

    class CountingSecretMap(dict):
        visited = 0

        def items(self):
            for item in super().items():
                self.visited += 1
                yield item

        def values(self):
            for value in super().values():
                self.visited += 1
                yield value

    secrets = CountingSecretMap(
        {f"credential_{index}": f"secret-{index:04d}" for index in range(1_000)}
    )
    collection_visits: list[int] = []
    collect_secret_values = operation_service._collect_secret_values

    def tracked_secret_collection(value):
        visited_before = secrets.visited
        result = collect_secret_values(value)
        collection_visits.append(secrets.visited - visited_before)
        return result

    monkeypatch.setattr(operation_service, "_collect_secret_values", tracked_secret_collection)
    extension = {
        **_extension(),
        "extension_id": "fixture.mail",
        "extension_install_id": "install-1",
        "enabled": True,
        "required_scopes": ["extension:invoke"],
        "action_definitions": [
            {
                "action_id": "send_message",
                "handler": {"mode": "extension"},
                "required_scopes": ["extension:invoke"],
            }
        ],
    }
    monkeypatch.setattr(
        operation_targets,
        "_resolve_extension_owned_oauth_runtime",
        lambda *_args, **_kwargs: secrets,
    )

    service = ExtensionOperationService(
        get_logger=lambda: _Store(_connection()),
        get_operation_store=lambda: _Store(_connection()),
        resolve_extension_runtime_record=lambda _extension_id: (
            extension,
            _Store(_connection()),
        ),
        extension_operation_handler_mode=lambda *_args, **_kwargs: "extension",
        extension_operation_failure_status=lambda _invocation: 400,
        get_project_by_id=lambda _project_id: {
            "id": "project-1",
            "account_id": "account-1",
        },
    )
    external_effects: list[str] = []
    monkeypatch.setattr(
        operation_runtime,
        "_claim_action_external_effect",
        lambda _context: external_effects.append("claim"),
    )
    monkeypatch.setattr(
        operation_runtime,
        "_mark_action_external_effect_running",
        lambda _context: external_effects.append("mark"),
    )
    monkeypatch.setattr(
        service,
        "_invoke_action_with_grants",
        lambda _context: external_effects.append("invoke") or {"status": "succeeded"},
    )
    monkeypatch.setattr(
        service,
        "_cleanup_action_resources",
        lambda *_args, **_kwargs: external_effects.append("cleanup"),
    )
    monkeypatch.setattr(
        operation_runtime,
        "_log_action_invocation",
        lambda *_args, **_kwargs: external_effects.append("log"),
    )

    _raises_input_951_1 = ExtensionActionRequest(target={"connection_id": "connection-1"}, input={})
    _raises_input_951_2 = ScopeContext(
        account_id="account-1", project_id="project-1", allowed_project_ids=frozenset({"project-1"})
    )
    with pytest.raises(ServiceError) as captured:
        service.execute_action(
            "fixture.mail", "send_message", _raises_input_951_1, scope=_raises_input_951_2
        )

    assert captured.value.payload["action"]["detail"] == "extension_secret_exposure_blocked"
    assert collection_visits
    assert collection_visits[0] <= 257
    assert external_effects == []


def test_normal_action_allows_safe_result_after_secret_leak_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.application.extensions.operation_service import ExtensionOperationService
    from core.infrastructure.extension_runtime import extension_runtime

    extension = {
        **_extension(),
        "extension_id": "fixture.mail",
        "extension_install_id": "install-1",
        "enabled": True,
        "required_scopes": ["extension:invoke"],
        "action_definitions": [
            {
                "action_id": "send_message",
                "handler": {"mode": "extension"},
                "required_scopes": ["extension:invoke"],
            }
        ],
    }
    monkeypatch.setattr(
        operation_targets,
        "_resolve_extension_owned_oauth_runtime",
        lambda *_args, **_kwargs: {"access_token": "runtime-token", "api_key": "x"},
    )
    monkeypatch.setattr(
        extension_runtime,
        "run_extension_action",
        lambda *_args, **_kwargs: {
            "status": "succeeded",
            "response": {
                "ok": True,
                "result": {
                    "ticket": "safe-ticket",
                    "api_key_status": "healthy",
                    "message": "next",
                    "next_status": True,
                },
            },
        },
    )
    service = ExtensionOperationService(
        get_logger=lambda: _Store(_connection()),
        get_operation_store=lambda: _Store(_connection()),
        resolve_extension_runtime_record=lambda _extension_id: (extension, _Store(_connection())),
        extension_operation_handler_mode=lambda *_args, **_kwargs: "extension",
        extension_operation_failure_status=lambda _invocation: 400,
        get_project_by_id=lambda _project_id: {"id": "project-1", "account_id": "account-1"},
    )

    result = service.execute_action(
        "fixture.mail",
        "send_message",
        ExtensionActionRequest(target={"connection_id": "connection-1"}, input={}),
        scope=ScopeContext(
            account_id="account-1",
            project_id="project-1",
            allowed_project_ids=frozenset({"project-1"}),
        ),
    )

    assert result.action["response"]["result"] == {
        "ticket": "safe-ticket",
        "api_key_status": "healthy",
        "message": "next",
        "next_status": True,
    }


@pytest.mark.parametrize(
    (
        "install_release_id",
        "configuration_overrides",
        "expected_configuration",
        "queries_configuration",
    ),
    [
        (
            "release-current",
            {},
            {"client_id": "browser-client-id", "browser_api_key": "browser-capability-key"},
            True,
        ),
        ("", {}, {}, False),
        ("release-stale", {}, {}, True),
        ("release-current", {"account_id": "account-foreign"}, {}, True),
        ("release-current", {"project_id": "project-foreign"}, {}, True),
        ("release-current", {"extension_install_id": "install-foreign"}, {}, True),
        ("release-current", {"extension_id": "fixture.foreign"}, {}, True),
        ("release-current", {"provider_id": "provider-foreign"}, {}, True),
        ("release-current", {"configuration_id": "configuration-foreign"}, {}, True),
    ],
)
def test_browser_picker_action_projects_only_current_declared_oauth_configuration(
    monkeypatch: pytest.MonkeyPatch,
    install_release_id: str,
    configuration_overrides: dict[str, str],
    expected_configuration: dict[str, str],
    queries_configuration: bool,
) -> None:
    from core.application.extensions.operation_service import ExtensionOperationService
    from core.infrastructure.extension_runtime import extension_runtime

    contract = {
        "oauth_providers": [
            {
                "provider_id": "fixture_oauth",
                "credential_fields": [
                    {"field_id": "client_id", "secret": False, "browser_exposable": True},
                    {"field_id": "browser_api_key", "secret": False, "browser_exposable": True},
                    {"field_id": "client_secret", "secret": True, "browser_exposable": False},
                    {"field_id": "server_only", "secret": False, "browser_exposable": False},
                ],
            }
        ]
    }
    extension = {
        "extension_id": "fixture.browser-oauth",
        "extension_key": "fixture.browser-oauth",
        "extension_install_id": "install-current",
        "enabled": True,
        "required_scopes": ["extension:invoke"],
        "compiled_manifest": {
            "extension_contract_v2_normalized": contract,
            "step_ui_manifest": {
                "forms": [
                    {
                        "operation_id": "browse_resources",
                        "fields": [
                            {
                                "name": "resource_id",
                                "external_resource_picker": {
                                    "bootstrap_action_id": "get_picker_bootstrap"
                                },
                            }
                        ],
                    }
                ]
            },
        },
        "action_definitions": [
            {
                "action_id": "get_picker_bootstrap",
                "handler": {"mode": "extension"},
                "required_scopes": ["extension:invoke"],
            }
        ],
    }
    connection = _connection(
        extension_id="fixture.browser-oauth",
        extension_install_id="install-current",
        oauth_provider_id="fixture_oauth",
        connection_config={
            "oauth": {
                "lifecycle_status": "connected",
                "verified_email": "confirmed@example.test",
                "refresh_token": "must-not-reach-runtime",
            }
        },
        secret_refs={"refs": {"access_token": "must-not-reach-runtime"}},
    )

    class _BrowserStore(_Store):
        conn = object()

        def get_project(self, _project_id: str) -> dict:
            return {"id": "project-1", "account_id": "account-1", "tenant_id": "tenant-1"}

        def get_extension_install_for_operator(self, install_id: str, *, account_id: str) -> dict:
            assert (install_id, account_id) == ("install-current", "account-1")
            return {
                "extension_id": "fixture.browser-oauth",
                "release_id": install_release_id,
                "compiled_manifest": {"extension_contract_v2_normalized": contract},
            }

    repository_calls: list[dict] = []

    class _Repository:
        def __init__(self, _conn: object) -> None:
            pass

        def get_configuration_for_scope(self, **kwargs: object) -> dict:
            repository_calls.append(dict(kwargs))
            return {
                **kwargs,
                "extension_release_id": "release-current",
                **configuration_overrides,
                "non_secret_config": {
                    "client_id": "browser-client-id",
                    "browser_api_key": "browser-capability-key",
                    "client_secret": "must-not-reach-runtime",
                    "refresh_token": "must-not-reach-runtime",
                    "access_token": "must-not-reach-runtime",
                    "secret_refs": "must-not-reach-runtime",
                    "server_only": "must-not-reach-runtime",
                },
            }

    runtime_targets: list[dict] = []
    resolved_runtime_secrets: list[dict] = []
    monkeypatch.setattr(
        "core.application.extensions.operation_targets._resolve_extension_owned_oauth_runtime",
        lambda *_args, **kwargs: (
            resolved_runtime_secrets.append(dict(kwargs["connection"]))
            or {"access_token": "runtime-only-token"}
        ),
    )
    monkeypatch.setattr(
        "core.application.extensions.oauth_repository.ExtensionOAuthRepository",
        _Repository,
    )
    monkeypatch.setattr(
        extension_runtime,
        "run_extension_action",
        lambda *_args, **kwargs: (
            runtime_targets.append(dict(kwargs["target"]))
            or {
                "status": "succeeded",
                "response": {
                    "ok": True,
                    "result": {
                        "ticket": dict(kwargs["target"]["connection"].get("secrets") or {}).get(
                            "access_token"
                        )
                    },
                },
            }
        ),
    )
    store = _BrowserStore(connection)
    service = ExtensionOperationService(
        get_logger=lambda: store,
        get_operation_store=lambda: store,
        resolve_extension_runtime_record=lambda _extension_id: (extension, store),
        extension_operation_handler_mode=lambda *_args, **_kwargs: "extension",
        extension_operation_failure_status=lambda _invocation: 400,
        get_project_by_id=store.get_project,
    )

    result = service.execute_action(
        "fixture.browser-oauth",
        "get_picker_bootstrap",
        ExtensionActionRequest(target={"connection_id": "connection-1"}, input={}),
        scope=ScopeContext(
            account_id="account-1",
            project_id="project-1",
            allowed_project_ids=frozenset({"project-1"}),
        ),
    )

    expected_repository_calls = (
        [
            {
                "tenant_id": "tenant-1",
                "account_id": "account-1",
                "project_id": "project-1",
                "extension_install_id": "install-current",
                "extension_id": "fixture.browser-oauth",
                "provider_id": "fixture_oauth",
                "configuration_id": "configuration-1",
            }
        ]
        if queries_configuration
        else []
    )
    assert repository_calls == expected_repository_calls
    runtime_connection = runtime_targets[0]["connection"]
    assert runtime_connection["oauth_configuration"] == expected_configuration
    assert runtime_connection["config"]["oauth"]["verified_email"] == ("confirmed@example.test")
    assert "refresh_token" not in runtime_connection["config"]["oauth"]
    assert "must-not-reach-runtime" not in repr(runtime_connection["config"])
    assert resolved_runtime_secrets == []
    assert "secrets" not in runtime_connection
    assert "secret_refs" not in runtime_connection
    assert "session" not in runtime_connection
    assert "access_token" not in runtime_connection["oauth_configuration"]
    assert result.action["response"]["result"] == {"ticket": None}
    assert "runtime-only-token" not in repr(result.action)


@pytest.mark.parametrize("status", ["succeeded", "failed"])
def test_extension_action_client_payload_omits_server_runtime_details(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    from core.application.extensions.operation_service import ExtensionOperationService
    from core.application.management.errors import ServiceError
    from core.infrastructure.extension_runtime import extension_runtime

    extension = {
        "extension_id": "fixture.client-safe",
        "extension_key": "fixture.client-safe",
        "extension_install_id": "install-1",
        "enabled": True,
        "required_scopes": ["extension:invoke"],
        "action_definitions": [
            {
                "action_id": "inspect",
                "handler": {"mode": "extension"},
                "required_scopes": ["extension:invoke"],
            }
        ],
    }
    invocation = {
        "status": status,
        "detail": "/private/runtime/error" if status == "failed" else "",
        "response": {
            "ok": status == "succeeded",
            "result": {"value": "visible"},
            "error": "Reconnect the provider and retry.",
            "error_code": "provider_unavailable",
        },
        "errors": [
            {"code": "provider_unavailable", "message": "/private/runtime/error"},
            {"code": "/private/runtime/error"},
        ],
        "command": ["python3", "/private/extensions/main.py"],
        "bundle_root": "/private/extensions",
        "stdout": "technical-output",
        "stderr": "technical-error",
        "audit_context": {"account_id": "account-1"},
    }
    monkeypatch.setattr(
        extension_runtime,
        "run_extension_action",
        lambda *_args, **_kwargs: invocation,
    )
    service = ExtensionOperationService(
        get_logger=lambda: None,
        get_operation_store=lambda: None,
        resolve_extension_runtime_record=lambda _extension_id: (extension, None),
        extension_operation_handler_mode=lambda *_args, **_kwargs: "extension",
        extension_operation_failure_status=lambda _invocation: 400,
    )

    if status == "failed":
        _raises_input_1306_1 = ExtensionActionRequest(input={})
        with pytest.raises(ServiceError) as captured:
            service.execute_action("fixture.client-safe", "inspect", _raises_input_1306_1)
        action = captured.value.payload["action"]
    else:
        action = service.execute_action(
            "fixture.client-safe",
            "inspect",
            ExtensionActionRequest(input={}),
        ).action

    assert action == {
        "status": status,
        "detail": "Extension action failed" if status == "failed" else "",
        "response": {
            "ok": status == "succeeded",
            "result": {"value": "visible"},
            "error": "Reconnect the provider and retry.",
            "error_code": "provider_unavailable",
        },
        "errors": [{"code": "provider_unavailable"}],
    }


def test_extension_action_client_payload_rejects_noncanonical_types_and_codes() -> None:
    from core.application.extensions.operation_projection import _client_action_invocation

    assert _client_action_invocation(
        {
            "status": "completed",
            "detail": "/private/runtime/provider.py:42",
            "response": {
                "ok": "yes",
                "error_code": "/private/runtime/error",
                "error": "provider failure\x00/private/runtime/error",
                "definitely_no_external_effect": "yes",
                "result": object(),
            },
            "errors": [
                {"code": "/private/runtime/error"},
                {"code": True},
            ],
        }
    ) == {
        "status": "failed",
        "detail": "Extension action failed",
        "response": {},
        "errors": [],
    }


def test_chat_failure_keeps_friendly_error_separate_from_machine_code() -> None:
    from core.application.extensions.operation_projection import _client_action_invocation

    action = _client_action_invocation(
        {
            "status": "failed",
            "detail": "project_context_required",
            "response": {
                "ok": False,
                "error_code": "project_context_required",
                "error": "Select a project before running this chat action.",
            },
        }
    )

    assert action["response"] == {
        "ok": False,
        "error_code": "project_context_required",
        "error": "Select a project before running this chat action.",
    }


def test_deterministic_provider_rejection_keeps_no_external_effect_marker() -> None:
    """Catch dropping the safe marker that distinguishes a typed 502 from uncertainty."""
    from core.application.extensions.operation_projection import _client_action_invocation

    action = _client_action_invocation(
        {
            "status": "failed",
            "detail": "polling_unavailable",
            "response": {
                "ok": False,
                "error_code": "polling_unavailable",
                "error": ("Polling was rejected because another bot instance is already polling."),
                "definitely_no_external_effect": True,
            },
        }
    )

    assert action["response"] == {
        "ok": False,
        "error_code": "polling_unavailable",
        "error": ("Polling was rejected because another bot instance is already polling."),
        "definitely_no_external_effect": True,
    }


def test_extension_action_projection_preserves_closed_retry_signal() -> None:
    from core.application.extensions.operation_projection import _client_action_invocation

    action = _client_action_invocation(
        {
            "status": "failed",
            "response": {
                "ok": False,
                "failure_class": "provider",
                "retryable": True,
                "retry_after_seconds": 6,
                "definitely_no_external_effect": True,
                "external_effect_status": "failed",
                "provider_error_code": "rate_limited",
                "http_status": 429,
                "headers": {"Authorization": "secret"},
            },
        }
    )

    assert action["failure_class"] == "provider"
    assert action["retryable"] is True
    assert action["retry_after_seconds"] == 6.0
    assert action["definitely_no_external_effect"] is True
    assert action["external_effect_status"] == "failed"
    assert action["provider_error_code"] == "rate_limited"
    assert action["http_status"] == 429
    retry_projection = {
        key: action[key]
        for key in (
            "failure_class",
            "retryable",
            "retry_after_seconds",
            "definitely_no_external_effect",
            "external_effect_status",
            "provider_error_code",
            "http_status",
        )
    }
    assert "Authorization" not in repr(retry_projection)


@pytest.mark.parametrize(
    "unsafe_error",
    [
        "/private/runtime/provider.py:42",
        "C:\\runtime\\provider.py:42",
        "Provider failed at /srv/extensions/provider.py:42",
        "Provider failed at C:\\runtime\\provider.py:42",
    ],
)
def test_extension_action_client_payload_rejects_absolute_runtime_path_errors(
    unsafe_error: str,
) -> None:
    from core.application.extensions.operation_projection import _client_action_invocation

    action = _client_action_invocation(
        {
            "status": "failed",
            "response": {
                "ok": False,
                "error": unsafe_error,
                "error_code": "provider_unavailable",
            },
        }
    )

    assert action["response"] == {"ok": False, "error_code": "provider_unavailable"}
    safe_action = _client_action_invocation(
        {
            "status": "failed",
            "response": {
                "ok": False,
                "error": "Reconnect the provider; see https://docs.example.test/oauth.",
            },
        }
    )
    assert safe_action["response"]["error"] == (
        "Reconnect the provider; see https://docs.example.test/oauth."
    )


def test_service_serialization_is_scoped_to_oauth_configuration_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the production service → runtime path, not the lock helper alone."""
    from core.application.extensions.operation_service import ExtensionOperationService
    from core.application.management.errors import ServiceError

    class _SharedLockConnection:
        def __init__(self) -> None:
            self._locks: dict[str, threading.Lock] = {}
            self._guard = threading.Lock()

        def cursor(self):
            return _LockCursor(self)

    class _LockCursor:
        def __init__(self, connection: _SharedLockConnection) -> None:
            self._connection = connection
            self._key = ""
            self._result = False

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> bool:
            return False

        def execute(self, sql: str, params: tuple[str]) -> None:
            self._key = params[0]
            with self._connection._guard:
                lock = self._connection._locks.setdefault(self._key, threading.Lock())
            if "pg_try_advisory_lock" in sql:
                self._result = lock.acquire(blocking=False)
            else:
                if lock.locked():
                    lock.release()
                self._result = True

        def fetchone(self) -> tuple[bool]:
            return (self._result,)

    shared_connection = _SharedLockConnection()
    first_started = threading.Event()
    allow_first_to_finish = threading.Event()
    block_first_invocation = True
    next_outcomes: list[object] = []
    extension = {
        "extension_id": "fixture.serialized-oauth",
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "fixture.serialized-oauth",
        "extension_install_id": "install-current",
        "enabled": True,
        "required_scopes": ["extension:invoke"],
        "action_definitions": [
            {
                "action_id": "append_resource",
                "handler": {"mode": "extension"},
                "required_scopes": ["extension:invoke"],
                "contract": {"serialization": {"key_fields": ["resource_id"]}},
            }
        ],
        "compiled_manifest": {
            "external_effects": [
                {
                    "operation_id": "append_resource",
                    "effect_kind": "resource_write",
                    "channel": "provider",
                    "idempotency": "required",
                }
            ]
        },
    }
    connections = {
        "connection-current": _connection(
            connection_id="connection-current",
            extension_id="fixture.serialized-oauth",
            extension_install_id="install-current",
            oauth_configuration_id="configuration-current",
            oauth_provider_id="provider-current",
        ),
        "connection-other": _connection(
            connection_id="connection-other",
            extension_id="fixture.serialized-oauth",
            extension_install_id="install-current",
            oauth_configuration_id="configuration-other",
            oauth_provider_id="provider-current",
        ),
        "connection-project": _connection(
            connection_id="connection-project",
            extension_id="fixture.serialized-oauth",
            extension_install_id="install-current",
            project_id="project-2",
            oauth_configuration_id="configuration-current",
            oauth_provider_id="provider-current",
        ),
        "connection-account": _connection(
            connection_id="connection-account",
            account_id="account-2",
            project_id="project-3",
            extension_id="fixture.serialized-oauth",
            extension_install_id="install-current",
            oauth_configuration_id="configuration-current",
            oauth_provider_id="provider-current",
        ),
        "connection-install": _connection(
            connection_id="connection-install",
            extension_id="fixture.serialized-oauth",
            extension_install_id="install-other",
            oauth_configuration_id="configuration-current",
            oauth_provider_id="provider-current",
        ),
    }

    class _SerializedStore:
        conn = shared_connection

        def get_project(self, project_id: str) -> dict:
            projects = {
                "project-1": {
                    "id": "project-1",
                    "account_id": "account-1",
                    "tenant_id": "tenant-1",
                },
                "project-2": {
                    "id": "project-2",
                    "account_id": "account-1",
                    "tenant_id": "tenant-1",
                },
                "project-3": {
                    "id": "project-3",
                    "account_id": "account-2",
                    "tenant_id": "tenant-1",
                },
            }
            return projects[project_id]

        def get_project_connection(self, *, connection_id: str, **_kwargs) -> dict:
            return dict(connections[connection_id])

    def run_while_first_is_locked(*_args, **kwargs) -> dict:
        nonlocal block_first_invocation
        connection = kwargs["request_payload"]["action"]["target"]["connection"]
        if connection["connection_id"] == "connection-current" and block_first_invocation:
            block_first_invocation = False
            first_started.set()
            assert allow_first_to_finish.wait(timeout=3)
        if next_outcomes:
            outcome = next_outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return {"status": "succeeded", "response": {}}

    monkeypatch.setattr(
        "core.application.extensions.operation_targets._resolve_extension_owned_oauth_runtime",
        lambda *_args, **_kwargs: {"access_token": "runtime-only"},
    )
    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.extension_runtime_entrypoints.run_extension_invocation",
        run_while_first_is_locked,
    )
    monkeypatch.setattr(
        operation_runtime,
        "_claim_action_external_effect",
        lambda context: context.runtime_ctx.update({"external_effect_id": "effect-current"}),
    )
    monkeypatch.setattr(
        operation_runtime,
        "_mark_action_external_effect_running",
        lambda _context: None,
    )
    store = _SerializedStore()

    def service_for(install_id: str) -> ExtensionOperationService:
        return ExtensionOperationService(
            get_logger=lambda: store,
            get_operation_store=lambda: store,
            resolve_extension_runtime_record=lambda _extension_id: (
                {**extension, "extension_install_id": install_id},
                store,
            ),
            extension_operation_handler_mode=lambda *_args, **_kwargs: "extension",
            extension_operation_failure_status=lambda _invocation: 409,
            get_project_by_id=store.get_project,
        )

    service = service_for("install-current")
    scope = ScopeContext(
        tenant_id="tenant-1",
        account_id="account-1",
        project_id="project-1",
        allowed_project_ids=frozenset({"project-1"}),
    )

    first_result: list[object] = []

    def invoke_first() -> None:
        try:
            first_result.append(
                service.execute_action(
                    "fixture.serialized-oauth",
                    "append_resource",
                    ExtensionActionRequest(
                        target={"connection_id": "connection-current"},
                        input={"resource_id": "resource-1"},
                    ),
                    scope=scope,
                )
            )
        except BaseException as exc:
            first_result.append(exc)

    first = threading.Thread(
        target=invoke_first,
    )
    first.start()
    assert first_started.wait(timeout=3), [
        getattr(result, "payload", result) for result in first_result
    ]

    _raises_input_1712_1 = ExtensionActionRequest(
        target={"connection_id": "connection-current"}, input={"resource_id": "resource-1"}
    )
    with pytest.raises(ServiceError) as busy:
        service.execute_action(
            "fixture.serialized-oauth", "append_resource", _raises_input_1712_1, scope=scope
        )
    other = service.execute_action(
        "fixture.serialized-oauth",
        "append_resource",
        ExtensionActionRequest(
            target={"connection_id": "connection-other"},
            input={"resource_id": "resource-1"},
        ),
        scope=scope,
    )
    independent_operations = [
        (
            service,
            "connection-project",
            ScopeContext(
                tenant_id="tenant-1",
                account_id="account-1",
                project_id="project-2",
                allowed_project_ids=frozenset({"project-2"}),
            ),
        ),
        (
            service,
            "connection-account",
            ScopeContext(
                tenant_id="tenant-1",
                account_id="account-2",
                project_id="project-3",
                allowed_project_ids=frozenset({"project-3"}),
            ),
        ),
        (service_for("install-other"), "connection-install", scope),
    ]
    independent_results = [
        independent_service.execute_action(
            "fixture.serialized-oauth",
            "append_resource",
            ExtensionActionRequest(
                target={"connection_id": connection_id},
                input={"resource_id": "resource-1"},
            ),
            scope=independent_scope,
        )
        for independent_service, connection_id, independent_scope in independent_operations
    ]
    allow_first_to_finish.set()
    first.join(timeout=3)

    assert busy.value.payload["action"]["detail"] == "operation_serialization_busy"
    assert other.action["status"] == "succeeded"
    assert all(result.action["status"] == "succeeded" for result in independent_results)
    assert len(first_result) == 1

    assert (
        service.execute_action(
            "fixture.serialized-oauth",
            "append_resource",
            ExtensionActionRequest(
                target={"connection_id": "connection-current"},
                input={"resource_id": "resource-1"},
            ),
            scope=scope,
        ).action["status"]
        == "succeeded"
    )
    next_outcomes.append({"status": "failed", "response": {}})
    _raises_input_1787_1 = ExtensionActionRequest(
        target={"connection_id": "connection-current"}, input={"resource_id": "resource-1"}
    )
    with pytest.raises(ServiceError):
        service.execute_action(
            "fixture.serialized-oauth", "append_resource", _raises_input_1787_1, scope=scope
        )
    next_outcomes.append(RuntimeError("runtime boom"))
    _raises_input_1798_1 = ExtensionActionRequest(
        target={"connection_id": "connection-current"}, input={"resource_id": "resource-1"}
    )
    with pytest.raises(RuntimeError, match="runtime boom"):
        service.execute_action(
            "fixture.serialized-oauth", "append_resource", _raises_input_1798_1, scope=scope
        )
    next_outcomes.append(asyncio.CancelledError())
    _raises_input_1809_1 = ExtensionActionRequest(
        target={"connection_id": "connection-current"}, input={"resource_id": "resource-1"}
    )
    with pytest.raises(asyncio.CancelledError):
        service.execute_action(
            "fixture.serialized-oauth", "append_resource", _raises_input_1809_1, scope=scope
        )
    assert (
        service.execute_action(
            "fixture.serialized-oauth",
            "append_resource",
            ExtensionActionRequest(
                target={"connection_id": "connection-current"},
                input={"resource_id": "resource-1"},
            ),
            scope=scope,
        ).action["status"]
        == "succeeded"
    )


# ---------------------------------------------------------------------------
# Existence is not a connection policy
#
# The projection returned an empty connection type both for an operation that
# does not exist and for one that needs no connection, so readiness could not
# tell them apart and answered `workflow.extension_operation_unavailable` for a
# declared action. Live, a mail extension's `send_email` was rejected when its
# valid connection was bound and accepted when it was left off.
# ---------------------------------------------------------------------------


def _contract(*operations: dict) -> dict:
    return {"extension_key": "fixture.ext", "operation_manifest": list(operations)}


def test_an_undeclared_operation_is_reported_as_absent() -> None:
    contract = operation_support.extension_operation_contract(
        _contract({"operation_id": "known", "connection_type_ids": ["smtp"]}),
        operation_id="never_declared",
    )

    assert contract.declared is False
    assert contract.connection_type_ids == frozenset()


def test_a_declared_operation_with_no_connection_is_declared() -> None:
    """The case that used to be indistinguishable from "no such operation"."""
    contract = operation_support.extension_operation_contract(
        _contract({"operation_id": "ping"}),
        operation_id="ping",
    )

    assert contract.declared is True
    assert contract.requires_connection is False
    assert contract.connection_type_ids == frozenset()


def test_a_single_declared_connection_type_is_carried() -> None:
    contract = operation_support.extension_operation_contract(
        _contract({"operation_id": "send_email", "connection_type_ids": ["custom_smtp"]}),
        operation_id="send_email",
    )

    assert contract.declared is True
    assert contract.requires_connection is True
    assert contract.connection_type_ids == frozenset({"custom_smtp"})


def test_several_declared_connection_types_are_all_accepted() -> None:
    """A file-transfer operation can work over either FTP or SFTP; a single-string
    projection could only ever name one of them."""
    contract = operation_support.extension_operation_contract(
        _contract({"operation_id": "list_files", "connection_type_ids": ["ftp", "sftp"]}),
        operation_id="list_files",
    )

    assert contract.connection_type_ids == frozenset({"ftp", "sftp"})
    assert contract.accepts_connection_type("sftp")
    assert contract.accepts_connection_type("ftp")
    assert not contract.accepts_connection_type("acme_cms_site")


def test_a_connectionless_operation_accepts_nothing() -> None:
    contract = operation_support.extension_operation_contract(
        _contract({"operation_id": "ping"}),
        operation_id="ping",
    )

    assert not contract.accepts_connection_type("ftp")


def test_extension_operation_contract_projects_operation_kind() -> None:
    contract = operation_support.extension_operation_contract(
        _contract({"operation_id": "list_records", "operation_kind": "source"}),
        operation_id="list_records",
    )

    assert contract.declared is True
    assert contract.operation_kind == "source"
