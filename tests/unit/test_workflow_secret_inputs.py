"""A secret input travels as a reference and becomes a value once, at the end.

The value of a `type: secret` input is never carried by the run. What the run
carries is a pointer to an encrypted record, which survives mappings, aliases,
loops and child-workflow calls unchanged. Only an authorized backend tool or
connector boundary exchanges it for plaintext, so no step, checkpoint, prompt,
log or Test Run response is ever holding the credential in the first place.
"""

from __future__ import annotations

import json

import pytest

from core.application.registry.workflow_contract import WorkflowInterface
from core.application.registry.workflow_contract_service import WorkflowInterfaceService
from core.infrastructure.security.prompt_safety import (
    REDACTED_CREDENTIAL_MARKER,
    redact_sensitive_structure,
)
from core.infrastructure.security.secret_reference import (
    SecretReference,
    as_secret_reference,
    is_secret_reference,
)

_SECRET = "SHOULD-NEVER-APPEAR-4471"


def _interface(**overrides) -> WorkflowInterface:
    field = {
        "name": "login",
        "type": "secret",
        "default_secret_ref": "wf:orders:login",
        **overrides,
    }
    return WorkflowInterface.from_settings(
        {
            "callable": {
                "enabled": True,
                "expected_inputs": [field],
                "output_mode": "none",
                "trigger_channels": ["api"],
            }
        }
    )


def test_a_configured_secret_arrives_as_a_reference_not_a_value() -> None:
    result = WorkflowInterfaceService().validate_inputs(_interface(), {})

    assert result.ok, result.errors
    assert is_secret_reference(result.inputs["login"])
    assert as_secret_reference(result.inputs["login"]).ref_key == "wf:orders:login"


def test_a_caller_cannot_supply_a_configured_secret() -> None:
    result = WorkflowInterfaceService().validate_inputs(_interface(), {"login": _SECRET})

    codes = {str(item.get("code") or "") for item in (result.errors or [])}
    assert "workflow.input.override_forbidden" in codes
    assert _SECRET not in json.dumps(result.errors), "the rejection must not echo the value"


def test_the_reference_survives_serialization_as_a_reference() -> None:
    """State crosses a checkpoint. A reference that arrives as a plain string on
    the other side would be indistinguishable from a value."""
    result = WorkflowInterfaceService().validate_inputs(_interface(), {})

    round_tripped = json.loads(json.dumps(result.inputs))

    assert is_secret_reference(round_tripped["login"])
    assert as_secret_reference(round_tripped["login"]).ref_key == "wf:orders:login"


def test_a_reference_redacts_wherever_a_structure_is_printed() -> None:
    """The target key is `login`, which no name-based rule would ever flag."""
    result = WorkflowInterfaceService().validate_inputs(_interface(), {})

    redacted = redact_sensitive_structure({"step_inputs": result.inputs})

    assert redacted["step_inputs"]["login"] == REDACTED_CREDENTIAL_MARKER


def test_printing_a_reference_does_not_print_a_credential() -> None:
    reference = SecretReference(name="login", ref_key="wf:orders:login")

    assert "wf:orders:login" not in repr(reference)
    assert "wf:orders:login" not in f"{reference}"


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------


def test_the_boundary_exchanges_a_reference_for_the_decrypted_value() -> None:
    from core.infrastructure.security.secret_reference import resolve_secret_reference

    resolved = resolve_secret_reference(
        SecretReference(name="login", ref_key="wf:orders:login"),
        account_id="acct-1",
        authorized_ref_keys=frozenset({"wf:orders:login"}),
        read_ciphertext=lambda account_id, ref_key: "cipher",
        decrypt=lambda ciphertext: _SECRET,
    )

    assert resolved == _SECRET


def test_the_boundary_refuses_a_reference_it_cannot_find() -> None:
    """Returning the reference, or an empty string, would send a request with a
    credential that is not a credential and read as an auth failure downstream."""
    from core.infrastructure.security.secret_reference import (
        SecretReferenceUnresolved,
        resolve_secret_reference,
    )

    _raises_input_120_1 = SecretReference(name="login", ref_key="missing")
    _raises_input_120_2 = frozenset({"missing"})
    with pytest.raises(SecretReferenceUnresolved):
        resolve_secret_reference(
            _raises_input_120_1,
            account_id="acct-1",
            authorized_ref_keys=_raises_input_120_2,
            read_ciphertext=lambda account_id, ref_key: None,
            decrypt=lambda ciphertext: _SECRET,
        )


def test_the_boundary_resolves_references_nested_in_a_payload() -> None:
    from core.infrastructure.security.secret_reference import resolve_secret_references_in

    payload = {
        "auth": {"user": "bot", "password": SecretReference(name="login", ref_key="k").to_wire()},
        "items": [SecretReference(name="token", ref_key="k2").to_wire()],
        "plain": "left alone",
    }

    resolved = resolve_secret_references_in(
        payload,
        account_id="acct-1",
        authorized_ref_keys=frozenset({"k", "k2"}),
        read_ciphertext=lambda account_id, ref_key: f"cipher:{ref_key}",
        decrypt=lambda ciphertext: ciphertext.replace("cipher:", "value-for-"),
    )

    assert resolved["auth"]["password"] == "value-for-k"
    assert resolved["items"] == ["value-for-k2"]
    assert resolved["plain"] == "left alone"
    assert resolved["auth"]["user"] == "bot"


# ---------------------------------------------------------------------------
# The connector step is the boundary
# ---------------------------------------------------------------------------


def test_the_connector_receives_the_value_and_the_result_keeps_the_reference() -> None:
    """The dispatched payload must carry plaintext, and the payload echoed back
    into logs, job output and Test Run must not. Reporting what was sent is
    exactly how a credential ends up written down."""
    from core.application.orchestration import connector_step_runtime as runtime

    dispatched: dict[str, object] = {}

    def fake_dispatch(*, input_payload, **_kwargs):
        dispatched.update(input_payload)
        return {"handled": True, "ok": True, "result": {}}

    reference = SecretReference(name="login", ref_key="wf:orders:login").to_wire()
    from core.application.registry.workflow_secret_defaults import secret_ref_fingerprint

    state = {
        "inputs": {"login": reference},
        "runtime_context": {"account_id": "acct-1"},
        "_workflow_secret_ref_fingerprints": [secret_ref_fingerprint("wf:orders:login")],
    }
    step_payload = {
        "step_id": "call_api",
        "connector_id": "demo.connector",
        "override": {
            "connector": {
                "function_name": "demo_call",
                "input_mapping": {"password": "state.inputs.login", "user": "bot"},
            }
        },
    }

    with (
        pytest.MonkeyPatch.context() as patch,
    ):
        patch.setattr(runtime, "_dispatch_tool_runtime", fake_dispatch)
        patch.setattr(
            runtime,
            "_read_secret_ciphertext",
            lambda account_id, ref_key: f"cipher:{ref_key}",
        )
        patch.setattr(runtime, "_decrypt_secret_value", lambda ciphertext: _SECRET)
        result = runtime.execute_connector_step(step=step_payload, state=state)

    assert dispatched["password"] == _SECRET, "the connector must get the real value"
    assert dispatched["user"] == "bot"
    assert _SECRET not in json.dumps(result), "the reported payload must stay a reference"
    assert is_secret_reference(result["input_payload"]["password"])


# ---------------------------------------------------------------------------
# Authorization at the boundary
#
# The reference lives in run state, and run state is authored: a literal in an
# input mapping is enough to put one there. Resolving whatever `ref_key` it
# names makes the boundary an oracle for every secret in the account — a
# workflow author reads another workflow's credential by naming its key, which
# is derivable from the workflow and field names.
# ---------------------------------------------------------------------------


def _store_reader(store: dict[str, str]):
    return lambda _account_id, ref_key: store.get(ref_key)


def test_a_reference_the_workflow_did_not_declare_is_refused() -> None:
    from core.infrastructure.security.secret_reference import (
        SecretReferenceUnauthorized,
        resolve_secret_reference,
    )

    store = {"workflow:payroll:input:api_key": "cipher"}
    forged = SecretReference(name="anything", ref_key="workflow:payroll:input:api_key")

    _raises_input_231_1 = frozenset({"workflow:orders:input:login"})
    _raises_input_231_2 = _store_reader(store)
    with pytest.raises(SecretReferenceUnauthorized):
        resolve_secret_reference(
            forged,
            account_id="acct-1",
            authorized_ref_keys=_raises_input_231_1,
            read_ciphertext=_raises_input_231_2,
            decrypt=lambda ciphertext: ciphertext,
        )


def test_a_declared_reference_still_resolves() -> None:
    from core.infrastructure.security.secret_reference import resolve_secret_reference

    store = {"workflow:orders:input:login": "cipher"}

    resolved = resolve_secret_reference(
        SecretReference(name="login", ref_key="workflow:orders:input:login"),
        account_id="acct-1",
        authorized_ref_keys=frozenset({"workflow:orders:input:login"}),
        read_ciphertext=_store_reader(store),
        decrypt=lambda _ciphertext: _SECRET,
    )

    assert resolved == _SECRET


def test_an_empty_authorization_set_resolves_nothing() -> None:
    """A workflow that declares no secret inputs can reach no secret at all."""
    from core.infrastructure.security.secret_reference import (
        SecretReferenceUnauthorized,
        resolve_secret_references_in,
    )

    _raises_input_264_1 = {
        "password": SecretReference(name="login", ref_key="workflow:orders:input:login").to_wire()
    }
    _raises_input_264_2 = frozenset()
    _raises_input_264_3 = _store_reader({"workflow:orders:input:login": "cipher"})
    with pytest.raises(SecretReferenceUnauthorized):
        resolve_secret_references_in(
            _raises_input_264_1,
            account_id="acct-1",
            authorized_ref_keys=_raises_input_264_2,
            read_ciphertext=_raises_input_264_3,
            decrypt=lambda ciphertext: ciphertext,
        )


def test_the_connector_authorizes_against_the_running_workflow_contract() -> None:
    """The set is derived from the workflow's own declared secret inputs, so a
    forged reference cannot widen it by naming a key that exists elsewhere."""
    from core.application.orchestration import connector_step_runtime as runtime

    dispatched: dict[str, object] = {}

    def fake_dispatch(*, input_payload, **_kwargs):
        dispatched.update(input_payload)
        return {"handled": True, "ok": True, "result": {}}

    state = {
        "workflow_id": "orders",
        "runtime_context": {"account_id": "acct-1"},
        # Authored literal: the author never had this key legitimately.
        "inputs": {},
    }
    step_payload = {
        "step_id": "exfiltrate",
        "connector_id": "demo.connector",
        "override": {
            "connector": {
                "function_name": "demo_call",
                "input_mapping": {
                    "password": SecretReference(
                        name="anything", ref_key="workflow:payroll:input:api_key"
                    ).to_wire()
                },
            }
        },
    }

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(runtime, "_dispatch_tool_runtime", fake_dispatch)
        patch.setattr(runtime, "_read_secret_ciphertext", lambda account_id, ref_key: "cipher")
        patch.setattr(runtime, "_decrypt_secret_value", lambda _ciphertext: _SECRET)
        result = runtime.execute_connector_step(step=step_payload, state=state)

    assert not dispatched, "the connector must not be called at all"
    assert result["ok"] is False
    assert result["error"] == "unresolved_secret_input"
    assert _SECRET not in json.dumps(result)


# ---------------------------------------------------------------------------
# A secret the caller supplies
#
# `secret` was added to the type vocabulary but not to coercion, so a secret
# input with no configured default rejected every caller value as an
# unsupported type. A workflow could declare one and never be runnable.
# ---------------------------------------------------------------------------


def _dynamic_interface(**overrides):
    from core.application.registry.workflow_contract import WorkflowInterface

    field = {"name": "login", "type": "secret", "required": True, **overrides}
    return WorkflowInterface.from_settings(
        {
            "callable": {
                "enabled": True,
                "expected_inputs": [field],
                "output_mode": "none",
                "trigger_channels": ["api"],
            }
        }
    )


def test_a_caller_may_supply_a_secret_that_has_no_configured_default() -> None:
    result = WorkflowInterfaceService().validate_inputs(_dynamic_interface(), {"login": _SECRET})

    assert result.ok, result.errors


def test_a_caller_supplied_secret_does_not_travel_as_plaintext() -> None:
    """It is held for the run the same way a configured one is: as a reference."""
    result = WorkflowInterfaceService().validate_inputs(_dynamic_interface(), {"login": _SECRET})

    assert is_secret_reference(result.inputs["login"])
    assert _SECRET not in json.dumps(result.inputs)


def test_a_missing_required_secret_is_still_reported_as_required() -> None:
    result = WorkflowInterfaceService().validate_inputs(_dynamic_interface(), {})

    codes = {str(item.get("code") or "") for item in (result.errors or [])}
    assert "workflow.input.required" in codes


def test_a_structured_value_is_not_accepted_as_a_secret() -> None:
    result = WorkflowInterfaceService().validate_inputs(
        _dynamic_interface(), {"login": {"nested": "object"}}
    )

    codes = {str(item.get("code") or "") for item in (result.errors or [])}
    assert "workflow.input.invalid_type" in codes


def test_a_public_caller_cannot_supply_a_secret_reference_wire() -> None:
    forged = SecretReference(
        name="credential",
        ciphertext="replayable-ciphertext",
    ).to_wire()

    result = WorkflowInterfaceService().validate_inputs(
        _dynamic_interface(name="credential"),
        {"credential": forged},
        channel="api",
    )

    assert not result.ok
    assert result.inputs == {}
    assert "replayable-ciphertext" not in json.dumps(result.errors)


def test_inline_reference_requires_launch_authority_even_when_ciphertext_decrypts() -> None:
    from core.infrastructure.security.secret_reference import (
        SecretReferenceUnauthorized,
        resolve_secret_reference,
    )

    forged = SecretReference(name="credential", ciphertext="cipher-from-another-run")

    _raises_input_402_1 = frozenset()
    _raises_input_402_2 = frozenset()
    with pytest.raises(SecretReferenceUnauthorized):
        resolve_secret_reference(
            forged,
            account_id="different-account",
            authorized_ref_keys=_raises_input_402_1,
            authorized_inline_ref_fingerprints=_raises_input_402_2,
            read_ciphertext=lambda _account_id, _ref_key: None,
            decrypt=lambda _ciphertext: _SECRET,
        )


def test_minted_inline_reference_resolves_with_its_launch_authority() -> None:
    from core.application.registry.workflow_secret_defaults import workflow_secret_launch_snapshot
    from core.infrastructure.security.secret_reference import resolve_secret_reference

    interface = _dynamic_interface(name="credential")
    validation = WorkflowInterfaceService().validate_inputs(
        interface,
        {"credential": _SECRET},
        channel="api",
    )
    reference = as_secret_reference(validation.inputs["credential"])
    snapshot = workflow_secret_launch_snapshot(interface, inputs=validation.inputs)

    assert reference is not None
    assert (
        resolve_secret_reference(
            reference,
            account_id="acct-1",
            authorized_ref_keys=frozenset(),
            authorized_inline_ref_fingerprints=frozenset(snapshot),
            read_ciphertext=lambda _account_id, _ref_key: None,
            decrypt=lambda _ciphertext: _SECRET,
        )
        == _SECRET
    )


def test_trusted_child_delegation_preserves_a_parent_minted_reference() -> None:
    interface = WorkflowInterface.from_settings(
        {
            "callable": {
                "enabled": True,
                "expected_inputs": [{"name": "credential", "type": "secret", "required": True}],
                "output_mode": "none",
                "trigger_channels": ["api", "parent_workflow"],
            }
        }
    )
    public_validation = WorkflowInterfaceService().validate_inputs(
        interface,
        {"credential": _SECRET},
        channel="api",
    )

    delegated = WorkflowInterfaceService().validate_delegated_inputs(
        interface,
        public_validation.inputs,
    )

    assert delegated.ok, delegated.errors
    assert delegated.inputs == public_validation.inputs


def test_trusted_child_alias_preserves_inline_capability_without_widening_it() -> None:
    from core.application.registry.workflow_secret_defaults import (
        delegated_workflow_secret_launch_snapshot,
        workflow_secret_launch_snapshot,
    )

    parent_interface = _dynamic_interface(name="store_api_token")
    child_interface = WorkflowInterface.from_settings(
        {
            "callable": {
                "enabled": True,
                "expected_inputs": [{"name": "credential", "type": "secret", "required": True}],
                "output_mode": "none",
                "trigger_channels": ["parent_workflow"],
            }
        }
    )
    parent_validation = WorkflowInterfaceService().validate_inputs(
        parent_interface,
        {"store_api_token": _SECRET},
        channel="api",
    )
    parent_snapshot = workflow_secret_launch_snapshot(
        parent_interface,
        inputs=parent_validation.inputs,
    )

    child_validation = WorkflowInterfaceService().validate_delegated_inputs(
        child_interface,
        {"credential": parent_validation.inputs["store_api_token"]},
    )
    child_snapshot = delegated_workflow_secret_launch_snapshot(
        parent_state={"_workflow_secret_ref_fingerprints": parent_snapshot},
        child_interface=child_interface,
        child_inputs=child_validation.inputs,
    )

    assert child_validation.ok, child_validation.errors
    assert as_secret_reference(child_validation.inputs["credential"]).name == "credential"
    assert child_snapshot == parent_snapshot


@pytest.fixture(autouse=True)
def _hermetic_encryption_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.infrastructure.security import secret_codec

    monkeypatch.setattr(secret_codec, "runtime_env", {"ENCRYPTION_KEY": "workflow-input-test"})
