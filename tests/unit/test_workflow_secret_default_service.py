"""Atomic lifecycle tests for workflow input secret defaults."""

from __future__ import annotations

import copy
import inspect
from contextlib import contextmanager

import pytest

from core.application.registry.db_store_workflows_mixin import WorkflowRevisionConflictError
from core.application.registry.workflow_secret_defaults import (
    SecretDefaultRejected,
    secret_default_ref_key,
    secret_default_ref_owner_prefix,
    secret_default_workflow_owner_prefix,
)
from core.application.workflows.workflow_secret_default_service import (
    WorkflowSecretDefaultService,
    WorkflowSecretMutationError,
)
from tests._workflow_definitions import linear_graph, stop_step

ACCOUNT_ID = "account-owner"


class _Repository:
    def __init__(self, *, current_ref: str = "") -> None:
        field = {"name": "login", "type": "secret"}
        if current_ref:
            field["default_secret_ref"] = current_ref
        self.workflow = {
            "workflow_id": "orders",
            "account_id": ACCOUNT_ID,
            "revision": 3,
            "settings": {"callable": {"expected_inputs": [field]}},
        }
        self.secret_refs: dict[tuple[str, str], str] = {}
        self.fail_save = False
        self.fail_write = False
        self.fail_delete = False
        self.fail_transaction_enter = False
        self.fail_transaction_commit = False
        self.write_calls = 0
        self.delete_calls = 0

    @contextmanager
    def transaction(self):
        if self.fail_transaction_enter:
            raise RuntimeError("private transaction enter failure")
        workflow_before = copy.deepcopy(self.workflow)
        refs_before = dict(self.secret_refs)
        try:
            yield self
        except Exception:
            self.workflow = workflow_before
            self.secret_refs = refs_before
            raise
        if self.fail_transaction_commit:
            self.workflow = workflow_before
            self.secret_refs = refs_before
            raise RuntimeError("private transaction commit failure")

    def get_workflow(self, workflow_id: str):
        return copy.deepcopy(self.workflow) if workflow_id == "orders" else None

    def save_workflow(self, data, *, author: str, expected_revision: int | None):
        _ = author
        if self.fail_save:
            raise RuntimeError("database detail that must stay private")
        if expected_revision != self.workflow["revision"]:
            raise WorkflowRevisionConflictError(
                workflow_id="orders", expected_revision=expected_revision
            )
        saved = copy.deepcopy(data)
        saved["revision"] = self.workflow["revision"] + 1
        self.workflow = saved
        return saved["revision"]

    @staticmethod
    def encrypt(plaintext: str) -> str:
        return f"cipher::{plaintext}"

    def write_ciphertext(
        self,
        _transaction,
        *,
        account_id: str,
        ref_key: str,
        ciphertext: str,
    ) -> None:
        self.write_calls += 1
        if self.fail_write:
            raise RuntimeError("write failed with private database detail")
        self.secret_refs[(account_id, ref_key)] = ciphertext

    def delete_owner_ciphertexts(
        self,
        _transaction,
        *,
        account_id: str,
        owner_prefix: str,
        keep_ref_key: str = "",
    ) -> None:
        self.delete_calls += 1
        if self.fail_delete:
            raise RuntimeError("delete failed with private database detail")
        self.secret_refs = {
            key: value
            for key, value in self.secret_refs.items()
            if not (
                key[0] == account_id and key[1].startswith(owner_prefix) and key[1] != keep_ref_key
            )
        }

    def purge_workflow_secrets(
        self,
        _transaction,
        *,
        account_id: str,
        owner_prefix: str,
    ) -> None:
        self.secret_refs = {
            key: value
            for key, value in self.secret_refs.items()
            if not (key[0] == account_id and key[1].startswith(owner_prefix))
        }


def _service(repository: _Repository) -> WorkflowSecretDefaultService:
    return WorkflowSecretDefaultService(repository)


def _set(repository: _Repository, *, expected_revision: int = 3):
    return _service(repository).set_secret(
        workflow_id="orders",
        field_name="login",
        plaintext="never-return-this",
        account_id=ACCOUNT_ID,
        author="operator",
        expected_revision=expected_revision,
    )


def _clear(repository: _Repository, *, expected_revision: int = 3):
    return _service(repository).clear_secret(
        workflow_id="orders",
        field_name="login",
        account_id=ACCOUNT_ID,
        author="operator",
        expected_revision=expected_revision,
    )


def test_workflow_secret_owner_prefix_is_hierarchical_and_opaque() -> None:
    workflow_prefix = secret_default_workflow_owner_prefix("orders")
    login_prefix = secret_default_ref_owner_prefix("orders", "login")
    token_prefix = secret_default_ref_owner_prefix("orders", "token")

    assert login_prefix.startswith(workflow_prefix)
    assert token_prefix.startswith(workflow_prefix)
    assert login_prefix != token_prefix
    assert "orders" not in workflow_prefix
    assert "login" not in login_prefix


def test_v1_reference_is_not_silently_reinterpreted_as_v2() -> None:
    from core.application.registry.workflow_secret_defaults import (
        secret_default_ref_belongs_to,
    )

    legacy_ref = "workflow-input:v1:" + ("a" * 64) + ":" + ("b" * 24)

    assert not secret_default_ref_belongs_to(
        legacy_ref,
        workflow_id="orders",
        field_name="login",
    )


def test_purge_workflow_secrets_removes_all_versions_only_in_exact_account() -> None:
    repository = _Repository()
    owned_refs = {
        secret_default_ref_key("orders", field_name, version_token=str(version))
        for field_name in ("login", "token")
        for version in (1, 2, 3)
    }
    other_workflow_ref = secret_default_ref_key("returns", "login", version_token="1")
    repository.secret_refs = {
        **{(ACCOUNT_ID, ref): "owned" for ref in owned_refs},
        (ACCOUNT_ID, other_workflow_ref): "other-workflow",
        ("foreign-account", next(iter(owned_refs))): "foreign-account",
    }

    _service(repository).purge_workflow_secrets(
        workflow_id="orders",
        account_id=ACCOUNT_ID,
        transaction=repository,
    )

    assert repository.secret_refs == {
        (ACCOUNT_ID, other_workflow_ref): "other-workflow",
        ("foreign-account", next(iter(owned_refs))): "foreign-account",
    }


def test_initial_set_commits_one_current_secret_reference() -> None:
    repository = _Repository()

    result = _set(repository)

    current_ref = repository.workflow["settings"]["callable"]["expected_inputs"][0][
        "default_secret_ref"
    ]
    assert result == {
        "workflow_id": "orders",
        "name": "login",
        "default_configured": True,
        "revision": 4,
    }
    assert set(repository.secret_refs) == {(ACCOUNT_ID, current_ref)}


def test_replace_deletes_all_historical_siblings_and_keeps_only_current() -> None:
    historical_refs = [
        secret_default_ref_key("orders", "login", version_token=str(version))
        for version in (1, 2, 3)
    ]
    repository = _Repository(current_ref=historical_refs[-1])
    repository.secret_refs = {
        (ACCOUNT_ID, ref): f"old-cipher-{index}" for index, ref in enumerate(historical_refs)
    }

    _set(repository)

    current_ref = repository.workflow["settings"]["callable"]["expected_inputs"][0][
        "default_secret_ref"
    ]
    assert set(repository.secret_refs) == {(ACCOUNT_ID, current_ref)}
    assert current_ref not in historical_refs


def test_clear_deletes_current_and_all_historical_siblings() -> None:
    historical_refs = [
        secret_default_ref_key("orders", "login", version_token=str(version))
        for version in (1, 2, 3)
    ]
    repository = _Repository(current_ref=historical_refs[-1])
    repository.secret_refs = {(ACCOUNT_ID, ref): "old-cipher" for ref in historical_refs}

    result = _clear(repository)

    field = repository.workflow["settings"]["callable"]["expected_inputs"][0]
    assert result["default_configured"] is False
    assert "default_secret_ref" not in field
    assert repository.secret_refs == {}


@pytest.mark.parametrize("mutation", [_set, _clear])
def test_stale_revision_changes_neither_workflow_nor_secrets(mutation) -> None:
    current_ref = secret_default_ref_key("orders", "login", version_token="3")
    repository = _Repository(current_ref=current_ref)
    repository.secret_refs[(ACCOUNT_ID, current_ref)] = "old-cipher"
    workflow_before = copy.deepcopy(repository.workflow)
    refs_before = dict(repository.secret_refs)

    with pytest.raises(WorkflowRevisionConflictError):
        mutation(repository, expected_revision=2)

    assert repository.workflow == workflow_before
    assert repository.secret_refs == refs_before
    assert repository.write_calls == 0
    assert repository.delete_calls == 0


def test_stale_initial_set_writes_nothing() -> None:
    repository = _Repository()
    workflow_before = copy.deepcopy(repository.workflow)

    with pytest.raises(WorkflowRevisionConflictError):
        _set(repository, expected_revision=2)

    assert repository.workflow == workflow_before
    assert repository.secret_refs == {}
    assert repository.write_calls == 0
    assert repository.delete_calls == 0


@pytest.mark.parametrize("failure", ["fail_write", "fail_delete"])
def test_secret_persistence_failure_rolls_back_the_workflow(failure: str) -> None:
    current_ref = secret_default_ref_key("orders", "login", version_token="3")
    repository = _Repository(current_ref=current_ref)
    repository.secret_refs[(ACCOUNT_ID, current_ref)] = "old-cipher"
    workflow_before = copy.deepcopy(repository.workflow)
    refs_before = dict(repository.secret_refs)
    setattr(repository, failure, True)

    with pytest.raises(WorkflowSecretMutationError) as exc_info:
        _set(repository)

    assert exc_info.value.code == "workflow_secret_storage_unavailable"
    assert "private" not in str(exc_info.value)
    assert repository.workflow == workflow_before
    assert repository.secret_refs == refs_before


def test_clear_delete_failure_rolls_back_the_workflow_reference() -> None:
    current_ref = secret_default_ref_key("orders", "login", version_token="3")
    repository = _Repository(current_ref=current_ref)
    repository.secret_refs[(ACCOUNT_ID, current_ref)] = "old-cipher"
    workflow_before = copy.deepcopy(repository.workflow)
    repository.fail_delete = True

    with pytest.raises(WorkflowSecretMutationError):
        _clear(repository)

    assert repository.workflow == workflow_before
    assert repository.secret_refs == {(ACCOUNT_ID, current_ref): "old-cipher"}


def test_workflow_save_failure_writes_and_deletes_nothing() -> None:
    current_ref = secret_default_ref_key("orders", "login", version_token="3")
    repository = _Repository(current_ref=current_ref)
    repository.secret_refs[(ACCOUNT_ID, current_ref)] = "old-cipher"
    workflow_before = copy.deepcopy(repository.workflow)
    repository.fail_save = True

    with pytest.raises(WorkflowSecretMutationError):
        _set(repository)

    assert repository.workflow == workflow_before
    assert repository.write_calls == 0
    assert repository.delete_calls == 0


def test_cleanup_cannot_cross_account_workflow_or_field_scope() -> None:
    current_ref = secret_default_ref_key("orders", "login", version_token="3")
    other_field_ref = secret_default_ref_key("orders", "token", version_token="3")
    other_workflow_ref = secret_default_ref_key("billing", "login", version_token="3")
    repository = _Repository(current_ref=current_ref)
    repository.secret_refs = {
        (ACCOUNT_ID, current_ref): "owned",
        ("account-other", current_ref): "other-account",
        (ACCOUNT_ID, other_field_ref): "other-field",
        (ACCOUNT_ID, other_workflow_ref): "other-workflow",
    }

    _clear(repository)

    assert repository.secret_refs == {
        ("account-other", current_ref): "other-account",
        (ACCOUNT_ID, other_field_ref): "other-field",
        (ACCOUNT_ID, other_workflow_ref): "other-workflow",
    }


def test_invalid_stored_reference_cannot_authorize_cross_field_cleanup() -> None:
    foreign_ref = secret_default_ref_key("orders", "token", version_token="3")
    repository = _Repository(current_ref=foreign_ref)
    repository.secret_refs[(ACCOUNT_ID, foreign_ref)] = "other-field"

    with pytest.raises(SecretDefaultRejected) as exc_info:
        _clear(repository)

    assert "workflow-input" not in str(exc_info.value)
    assert repository.secret_refs == {(ACCOUNT_ID, foreign_ref): "other-field"}
    assert repository.delete_calls == 0


def test_secret_routes_contain_no_sql_encryption_or_transaction_orchestration() -> None:
    from core.entrypoints.admin.workflows_routes_write import (
        clear_workflow_input_secret,
        set_workflow_input_secret,
    )

    source = inspect.getsource(set_workflow_input_secret) + inspect.getsource(
        clear_workflow_input_secret
    )
    assert "secret_refs" not in source
    assert "encrypt_secret" not in source
    assert "_transaction" not in source
    assert ".execute(" not in source


def test_metadata_routes_delegate_transaction_and_secret_cleanup_to_application() -> None:
    from core.entrypoints.admin.workflows_routes_write import patch_workflow, update_workflow

    source = inspect.getsource(update_workflow) + inspect.getsource(patch_workflow)
    assert "_transaction" not in source
    assert "secret_refs" not in source
    assert ".execute(" not in source
    assert "cleanup_displaced_secret_fields" not in source
    assert "save_workflow_metadata" in source


def test_owner_prefix_is_exact_for_workflow_and_field() -> None:
    prefix = secret_default_ref_owner_prefix("orders", "login")

    assert secret_default_ref_key("orders", "login", version_token="3").startswith(prefix)
    assert not secret_default_ref_key("orders", "token", version_token="3").startswith(prefix)
    assert not secret_default_ref_key("billing", "login", version_token="3").startswith(prefix)


def test_ordinary_update_cleanup_removes_all_displaced_versions_only() -> None:
    owner_refs = [
        secret_default_ref_key("orders", "login", version_token=str(version))
        for version in (1, 2, 3)
    ]
    other_field_ref = secret_default_ref_key("orders", "token", version_token="3")
    repository = _Repository(current_ref=owner_refs[-1])
    repository.secret_refs = {
        **{(ACCOUNT_ID, ref): "owned" for ref in owner_refs},
        (ACCOUNT_ID, other_field_ref): "other-field",
        ("account-other", owner_refs[-1]): "other-account",
    }
    service = _service(repository)
    existing = copy.deepcopy(repository.workflow)
    final_payload = copy.deepcopy(existing)
    final_payload["settings"] = {
        "callable": {"expected_inputs": [{"name": "login", "type": "string"}]}
    }

    service.save_workflow_metadata(
        workflow_id="orders",
        account_id=ACCOUNT_ID,
        existing=existing,
        final_payload=final_payload,
        author="operator",
        expected_revision=3,
        validate_saved=lambda: {"valid": True},
        final_enabled=False,
    )

    assert repository.secret_refs == {
        (ACCOUNT_ID, other_field_ref): "other-field",
        ("account-other", owner_refs[-1]): "other-account",
    }


def test_ordinary_update_cleanup_fails_closed_for_foreign_stored_reference() -> None:
    foreign_ref = secret_default_ref_key("orders", "token", version_token="3")
    repository = _Repository(current_ref=foreign_ref)
    repository.secret_refs[(ACCOUNT_ID, foreign_ref)] = "other-field"
    existing = copy.deepcopy(repository.workflow)
    final_payload = copy.deepcopy(existing)
    final_payload["settings"] = {"callable": {"expected_inputs": []}}

    _raises_callable_447_1 = _service(repository).save_workflow_metadata
    with pytest.raises(SecretDefaultRejected):
        _raises_callable_447_1(
            workflow_id="orders",
            account_id=ACCOUNT_ID,
            existing=existing,
            final_payload=final_payload,
            author="operator",
            expected_revision=3,
            validate_saved=lambda: {"valid": True},
            final_enabled=False,
        )

    assert repository.secret_refs == {(ACCOUNT_ID, foreign_ref): "other-field"}
    assert repository.delete_calls == 0


@pytest.mark.parametrize("failure", ["validation", "cleanup"])
def test_ordinary_update_failure_rolls_back_workflow_and_secret_rows(failure: str) -> None:
    current_ref = secret_default_ref_key("orders", "login", version_token="3")
    repository = _Repository(current_ref=current_ref)
    repository.secret_refs[(ACCOUNT_ID, current_ref)] = "owned"
    existing = copy.deepcopy(repository.workflow)
    final_payload = copy.deepcopy(existing)
    final_payload["settings"] = {"callable": {"expected_inputs": []}}
    if failure == "cleanup":
        repository.fail_delete = True

    def validate_saved():
        if failure == "validation":
            raise RuntimeError("validation failed")
        return {"valid": True}

    _raises_callable_479_1 = _service(repository).save_workflow_metadata
    expected_error = RuntimeError if failure == "validation" else WorkflowSecretMutationError
    with pytest.raises(expected_error) as exc_info:
        _raises_callable_479_1(
            workflow_id="orders",
            account_id=ACCOUNT_ID,
            existing=existing,
            final_payload=final_payload,
            author="operator",
            expected_revision=3,
            validate_saved=validate_saved,
            final_enabled=False,
        )

    if failure == "validation":
        assert type(exc_info.value) is RuntimeError
        assert str(exc_info.value) == "validation failed"
    else:
        assert exc_info.value.code == "workflow_secret_storage_unavailable"
        assert exc_info.value.status_code == 503
    assert repository.workflow == existing
    assert repository.secret_refs == {(ACCOUNT_ID, current_ref): "owned"}


def test_malformed_boundary_enable_rolls_back_revision_settings_and_secret_ownership() -> None:
    from core.application.registry.contract_validation import validate_workflow_readiness
    from core.application.workflows.workflow_secret_default_service import (
        WorkflowMetadataValidationError,
    )

    current_ref = secret_default_ref_key("orders", "login", version_token="3")
    other_ref = secret_default_ref_key("billing", "login", version_token="3")
    repository = _Repository(current_ref=current_ref)
    repository.workflow["enabled"] = False
    repository.workflow["steps"] = [
        {
            "step_id": "legacy",
            "step_kind": "set_variable",
            "group_id": None,
            "lane_id": None,
            "boundary_contract": {"entry": ["message"]},
            "override": {"set_variable": {"target": "state.ready", "value": True}},
        },
        stop_step(),
    ]
    repository.workflow["groups"] = []
    repository.workflow["settings"]["execution_graph"] = linear_graph(["legacy", "done"])
    repository.secret_refs = {
        (ACCOUNT_ID, current_ref): "owned ciphertext",
        (ACCOUNT_ID, other_ref): "unrelated ciphertext",
    }
    before = copy.deepcopy(repository.workflow)
    refs_before = dict(repository.secret_refs)
    final_payload = copy.deepcopy(before)
    final_payload["enabled"] = True
    service = _service(repository)
    with pytest.raises(WorkflowMetadataValidationError) as exc_info:
        service.save_workflow_metadata(
            workflow_id="orders",
            account_id=ACCOUNT_ID,
            existing=before,
            final_payload=final_payload,
            author="operator",
            expected_revision=3,
            validate_saved=lambda: validate_workflow_readiness(repository, "orders"),
            final_enabled=True,
        )
    assert any(
        issue["code"] == "contract.item_not_object"
        and issue["path"] == "workflow.steps.legacy.boundary_contract.entry[0]"
        for issue in exc_info.value.validation["errors"]
    )
    assert repository.workflow == before
    assert repository.secret_refs == refs_before
    assert repository.delete_calls == 0


@pytest.mark.parametrize("failure", ["enter", "save", "commit"])
def test_ordinary_update_repository_failure_is_typed_and_value_independent(
    failure: str,
) -> None:
    current_ref = secret_default_ref_key("orders", "login", version_token="3")
    repository = _Repository(current_ref=current_ref)
    repository.secret_refs[(ACCOUNT_ID, current_ref)] = "owned"
    existing = copy.deepcopy(repository.workflow)
    final_payload = copy.deepcopy(existing)
    final_payload["settings"] = {"callable": {"expected_inputs": []}}
    if failure == "enter":
        repository.fail_transaction_enter = True
    elif failure == "save":
        repository.fail_save = True
    else:
        repository.fail_transaction_commit = True

    _raises_callable_512_1 = _service(repository).save_workflow_metadata
    with pytest.raises(WorkflowSecretMutationError) as exc_info:
        _raises_callable_512_1(
            workflow_id="orders",
            account_id=ACCOUNT_ID,
            existing=existing,
            final_payload=final_payload,
            author="operator",
            expected_revision=3,
            validate_saved=lambda: {"valid": True},
            final_enabled=False,
        )

    assert exc_info.value.code == "workflow_secret_storage_unavailable"
    assert "private" not in str(exc_info.value)
    assert repository.workflow == existing
    assert repository.secret_refs == {(ACCOUNT_ID, current_ref): "owned"}
