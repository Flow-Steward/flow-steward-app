from __future__ import annotations

from copy import deepcopy

import pytest

from core.application.workflows.extension_connection_validation import (
    validate_extension_connector_step,
)


def _contract() -> dict:
    return {
        "extension_id": "fixture.mail",
        "install_id": "install-mail",
        "install_state": "active",
        "operation_manifest": [
            {
                "operation_id": "send_message",
                "connection_type_ids": ["mail_account"],
                "required_oauth_scopes": ["mail.send"],
            }
        ],
    }


def _step(operation_id: str = "send_message") -> dict:
    return {
        "step_kind": "connector",
        "connector_id": "extension.fixture.mail",
        "connector_install_id": "install-mail",
        "connection_binding_jsonb": {"connection_id": "mail-1"},
        "override": {"connector": {"operation_id": operation_id}},
    }


def _connection(**overrides) -> dict:
    row = {
        "connection_id": "mail-1",
        "account_id": "account-1",
        "extension_id": "fixture.mail",
        "extension_install_id": "install-mail",
        "connection_type": "mail_account",
        "enabled": True,
        "runtime_status": "ready",
        "health_status": "ok",
        "oauth_granted_scopes": ["profile", "mail.send"],
        "connection_config": {"oauth": {"lifecycle_status": "connected"}},
    }
    row.update(overrides)
    return row


class _DB:
    def __init__(
        self,
        connection: dict | None,
        *,
        contract: dict | None = None,
        install_state: str | None = "active",
    ) -> None:
        self.connection = connection
        self.contract = _contract() if contract is None else contract
        self.install_state = install_state

    # The narrow port readiness actually calls. This double used to implement
    # `get_project_connection` and `get_extension_install_state`, neither of
    # which the runtime store has — which is how a live failure sat behind a
    # green suite.
    def get_extension_connection_metadata(self, _connection_id: str):
        return deepcopy(self.connection)

    def get_extension_install_contract(self, _install_id: str):
        contract = deepcopy(self.contract)
        if isinstance(contract, dict) and self.install_state is not None:
            contract["install_state"] = self.install_state
        return contract

    @staticmethod
    def get_tool(tool_id: str):
        if tool_id != "extension.install-mail.mail_tools":
            return None
        return {
            "tool_id": tool_id,
            "source_install_id": "install-mail",
            "implementation": {
                "type": "extension",
                "extension_key": "fixture.mail",
                "action": "send_message",
            },
        }


def test_extension_connection_with_exact_type_scope_and_health_is_ready() -> None:
    assert (
        validate_extension_connector_step(
            _DB(_connection()),
            step=_step(),
            step_path="workflow.phases[0].steps[0]",
        )
        == []
    )


def test_install_scoped_projected_tool_uses_bound_install_contract_identity() -> None:
    step = _step()
    step["connector_id"] = "extension.install-mail.mail_tools"

    assert (
        validate_extension_connector_step(
            _DB(_connection()),
            step=step,
            step_path="workflow.phases[0].steps[0]",
        )
        == []
    )


def test_install_scoped_projected_tool_rejects_a_different_bound_install() -> None:
    step = _step()
    step["connector_id"] = "extension.install-other.mail_tools"

    issues = validate_extension_connector_step(
        _DB(_connection()),
        step=step,
        step_path="workflow.phases[0].steps[0]",
    )

    assert issues[0]["code"] == "workflow.extension_unavailable"


def test_install_scoped_tool_function_uses_declared_wrapper_operation() -> None:
    """Projected function names are ToolManifest API, not Contract V2 operations."""

    contract = {
        "extension_id": "acme.example-showcase",
        "install_id": "install-showcase",
        "install_state": "active",
        "operation_manifest": [
            {
                "operation_id": "invoke_tool",
                "connection_type_ids": [],
                "required_oauth_scopes": [],
            }
        ],
    }

    class _ShowcaseDB(_DB):
        @staticmethod
        def get_tool(tool_id: str):
            assert tool_id == "extension.install-showcase.sample_content_tools"
            return {
                "tool_id": tool_id,
                "source_install_id": "install-showcase",
                "implementation": {
                    "type": "extension",
                    "extension_key": "acme.example-showcase",
                    "action": "invoke_tool",
                },
            }

    step = {
        "step_kind": "connector",
        "connector_id": "extension.install-showcase.sample_content_tools",
        "connector_install_id": "install-showcase",
        "override": {"connector": {"function_name": "demo_measure_text"}},
    }

    assert (
        validate_extension_connector_step(
            _ShowcaseDB(None, contract=contract),
            step=step,
            step_path="workflow.phases[0].steps[0]",
        )
        == []
    )


@pytest.mark.parametrize(
    ("connection", "expected_code"),
    [
        (_connection(extension_install_id="install-other"), "workflow.connection_unavailable"),
        (_connection(connection_type="other_account"), "workflow.connection_unavailable"),
        (_connection(oauth_granted_scopes=["profile"]), "oauth_scope_required"),
        (_connection(runtime_status="unavailable"), "workflow.connection_unavailable"),
        (_connection(health_status="failed"), "workflow.connection_unavailable"),
        (
            _connection(connection_config={"oauth": {"lifecycle_status": "needs_attention"}}),
            "oauth_needs_attention",
        ),
    ],
)
def test_extension_connection_requirement_failures_are_provider_neutral(
    connection: dict, expected_code: str
) -> None:
    issues = validate_extension_connector_step(
        _DB(connection),
        step=_step(),
        step_path="workflow.phases[0].steps[0]",
    )

    assert issues[0]["code"] == expected_code
    assert issues[0]["path"].endswith("connection_binding_jsonb.connection_id")


def test_unknown_operation_and_inactive_install_fail_closed() -> None:
    unknown = validate_extension_connector_step(
        _DB(_connection()),
        step=_step("future_operation"),
        step_path="workflow.phases[0].steps[0]",
    )
    disabled = validate_extension_connector_step(
        _DB(_connection(), install_state="disabled"),
        step=_step(),
        step_path="workflow.phases[0].steps[0]",
    )

    assert unknown[0]["code"] == "workflow.extension_operation_unavailable"
    assert disabled[0]["code"] == "workflow.extension_disabled"


def test_step_without_persisted_install_identity_fails_closed() -> None:
    step = _step()
    step.pop("connector_install_id")

    issues = validate_extension_connector_step(
        _DB(_connection(), install_state="disabled"),
        step=step,
        step_path="workflow.phases[0].steps[0]",
    )

    assert issues[0]["code"] == "workflow.extension_install_missing"


class _InstallListDB:
    """Answers only which installs of an extension the account can use."""

    def __init__(self, install_ids: dict[str, list[str]]) -> None:
        self.install_ids = install_ids
        self.calls: list[tuple[str, str]] = []

    def list_extension_install_ids(self, extension_id: str, *, account_id: str) -> list[str]:
        self.calls.append((extension_id, account_id))
        return list(self.install_ids.get(extension_id, []))


@pytest.mark.parametrize(
    "step",
    [
        # <extension_id>.<operation_id>, as hand-written and API-created workflows spell it.
        {"step_kind": "connector", "connector_id": "fixture.mail.send_message"},
        # extension_id in the connector override.
        {
            "step_kind": "connector",
            "connector_id": "send_message",
            "override": {
                "connector": {"extension_id": "fixture.mail", "action_id": "send_message"}
            },
        },
    ],
)
def test_step_naming_an_installed_extension_without_install_id_is_not_ready(step: dict) -> None:
    db = _InstallListDB({"fixture.mail": ["install-mail"]})

    issues = validate_extension_connector_step(
        db,
        step=step,
        step_path="workflow.steps.send",
        account_id="account-1",
    )

    assert [issue["code"] for issue in issues] == ["workflow.extension_install_missing"]
    assert issues[0]["path"] == "workflow.steps.send.connector_install_id"
    assert "'install-mail'" in issues[0]["message"]
    assert db.calls == [("fixture.mail", "account-1")]


@pytest.mark.parametrize(
    "step",
    [
        # A dotted registry tool id whose prefix is no installed extension.
        {"step_kind": "connector", "connector_id": "std.text.upper"},
        {"step_kind": "connector", "connector_id": "other.tools.lookup"},
        # Already bound to an install.
        {
            "step_kind": "connector",
            "connector_id": "fixture.mail.send_message",
            "connector_install_id": "install-mail",
        },
        # Projected MCP tool.
        {"step_kind": "connector", "connector_id": "connector.fixture.mail"},
    ],
)
def test_named_extension_check_leaves_tools_and_bound_steps_alone(step: dict) -> None:
    issues = validate_extension_connector_step(
        _InstallListDB({"fixture.mail": ["install-mail"]}),
        step=step,
        step_path="workflow.steps.send",
        account_id="account-1",
    )

    assert issues == []


# ---------------------------------------------------------------------------
# The production store boundary
#
# The double above implements `get_project_connection` and
# `get_extension_install_state`. `RegistryDBStore` — the object readiness
# actually receives — implements neither, so in production the connection
# metadata was always `None` and the install-state check was silently skipped.
# Thirty-one tests passed while the live product rejected a valid step.
# ---------------------------------------------------------------------------


class _ProductionShapedStore:
    """Only what `RegistryDBStore` really exposes on this path.

    Deliberately has no `get_project_connection` and no
    `get_extension_install_state`: a double that answers calls production cannot
    answer is how the original defect stayed invisible.
    """

    def __init__(self, connection: dict | None, *, contract: dict | None = None) -> None:
        self._connection = connection
        self._contract = _contract() if contract is None else contract
        self.conn = object()

    def get_extension_connection_metadata(self, connection_id: str):
        assert connection_id == "mail-1"
        return deepcopy(self._connection)

    def contract_for_test(self):
        return deepcopy(self._contract)


@pytest.fixture
def production_contract(monkeypatch):
    """Resolve the install contract the way production does, through `.conn`."""

    def _resolve(_conn, *, install_id, account_id, default_account_id):
        return _current_contract["value"]

    _current_contract: dict = {"value": _contract()}
    monkeypatch.setattr(
        "core.application.extensions.workflow_install_contract"
        ".resolve_workflow_extension_install_contract",
        _resolve,
    )
    return _current_contract


def test_a_valid_step_passes_through_the_production_store_shape(production_contract) -> None:
    """The reported failure: an explicit install id and a real connection, and
    readiness still answered `workflow.connection_unavailable` because the
    metadata lookup found no method it could call."""
    issues = validate_extension_connector_step(
        _ProductionShapedStore(_connection()),
        step=_step(),
        step_path="workflow.phases[0].steps[0]",
    )

    assert issues == []


@pytest.mark.parametrize(
    ("connection", "expected_code"),
    [
        (_connection(extension_install_id="install-other"), "workflow.connection_unavailable"),
        (_connection(extension_id="fixture.other"), "workflow.connection_unavailable"),
        (_connection(connection_type="other_account"), "workflow.connection_unavailable"),
        (_connection(enabled=False), "workflow.connection_unavailable"),
        (_connection(runtime_status="unavailable"), "workflow.connection_unavailable"),
        (_connection(oauth_granted_scopes=["profile"]), "oauth_scope_required"),
    ],
)
def test_the_production_store_shape_still_fails_closed(
    production_contract, connection: dict, expected_code: str
) -> None:
    issues = validate_extension_connector_step(
        _ProductionShapedStore(connection),
        step=_step(),
        step_path="workflow.phases[0].steps[0]",
    )

    assert [item["code"] for item in issues] == [expected_code]


def test_an_unreadable_connection_fails_closed(production_contract) -> None:
    """No metadata is not permission to proceed."""
    issues = validate_extension_connector_step(
        _ProductionShapedStore(None),
        step=_step(),
        step_path="workflow.phases[0].steps[0]",
    )

    assert [item["code"] for item in issues] == ["workflow.connection_unavailable"]


@pytest.mark.parametrize(
    ("install_state", "expected_code"),
    [
        ("disabled", "workflow.extension_disabled"),
        ("removed", "workflow.extension_unavailable"),
        ("unavailable", "workflow.extension_unavailable"),
    ],
)
def test_install_state_is_enforced_from_the_contract(
    production_contract, install_state: str, expected_code: str
) -> None:
    """The state check used to be skipped entirely in production, because it
    depended on a getter the runtime store does not implement. The contract is
    the authoritative source, and it is present on the real path."""
    contract = _contract()
    contract["install_state"] = install_state
    production_contract["value"] = contract

    issues = validate_extension_connector_step(
        _ProductionShapedStore(_connection()),
        step=_step(),
        step_path="workflow.phases[0].steps[0]",
    )

    assert [item["code"] for item in issues] == [expected_code]


def test_a_missing_contract_is_reported_as_removed(production_contract) -> None:
    production_contract["value"] = None

    issues = validate_extension_connector_step(
        _ProductionShapedStore(_connection()),
        step=_step(),
        step_path="workflow.phases[0].steps[0]",
    )

    assert [item["code"] for item in issues] == ["workflow.extension_removed"]


def test_an_active_install_proceeds(production_contract) -> None:
    contract = _contract()
    contract["install_state"] = "active"
    production_contract["value"] = contract

    assert (
        validate_extension_connector_step(
            _ProductionShapedStore(_connection()),
            step=_step(),
            step_path="workflow.phases[0].steps[0]",
        )
        == []
    )


def test_the_runtime_store_implements_the_port_readiness_calls() -> None:
    """The port has to exist where readiness runs. A capability that lives only
    in a test double is the shape this whole defect had."""
    from core.application.registry.db_store import RegistryDBStore

    assert hasattr(RegistryDBStore, "get_extension_connection_metadata")


def test_the_projection_readiness_uses_carries_no_credentials() -> None:
    """Readiness needs to know whether a connection is usable, not what it
    authenticates with."""
    import inspect

    from core.infrastructure.db import project_connection_store

    source = inspect.getsource(project_connection_store.get_project_connection_runtime_metadata)

    assert "secret_refs" not in source
    assert "credential" not in source
    # The OAuth block is projected explicitly rather than the whole config.
    assert "connection_config->'oauth'" in source


# ---------------------------------------------------------------------------
# Three cases, told apart
#
# Live: `send_email` is in the installed contract, binding its valid connection
# returned `workflow.extension_operation_unavailable`, and omitting the
# connection was accepted. Existence and connection policy were the same signal.
# ---------------------------------------------------------------------------


def _multi_type_contract(*connection_type_ids: str) -> dict:
    contract = _contract()
    contract["operation_manifest"] = [
        {
            "operation_id": "send_message",
            "connection_type_ids": list(connection_type_ids),
            "required_oauth_scopes": ["mail.send"],
        }
    ]
    return contract


def _connectionless_contract() -> dict:
    contract = _contract()
    contract["operation_manifest"] = [{"operation_id": "send_message"}]
    return contract


def _inventory_extension_contract(operation_id: str, outputs: list[dict]) -> dict:
    return {
        "extension_id": "acme.inventory",
        "install_id": "install-inventory",
        "install_state": "active",
        "operation_manifest": [
            {
                "operation_id": operation_id,
                "connection_type_ids": ["inventory_api"],
                "required_oauth_scopes": ["write_inventory"],
                "outputs": deepcopy(outputs),
            }
        ],
    }


def _inventory_extension_step(operation_id: str, output_mapping: dict[str, str]) -> dict:
    return {
        "step_kind": "connector",
        "connector_id": "extension.acme.inventory",
        "connector_install_id": "install-inventory",
        "connection_binding_jsonb": {"connection_id": "inventory-1"},
        "output_mapping_jsonb": deepcopy(output_mapping),
        "override": {"connector": {"operation_id": operation_id}},
    }


def _inventory_extension_connection() -> dict:
    return _connection(
        connection_id="inventory-1",
        extension_id="acme.inventory",
        extension_install_id="install-inventory",
        connection_type="inventory_api",
        oauth_granted_scopes=["write_inventory"],
    )


def _inventory_outputs() -> list[dict]:
    return [
        {
            "name": "inventory_adjustment_group",
            "value_type": "object",
            "schema": {
                "type": ["object", "null"],
                "properties": {
                    "id": {"type": ["string", "null"]},
                    "changes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"delta": {"type": ["integer", "null"]}},
                        },
                    },
                },
            },
        },
        {"name": "external_effect_status", "value_type": "text"},
        {"name": "definitely_no_external_effect", "value_type": "boolean"},
    ]


def test_unknown_output_mapping_fails_closed_through_registry_db_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale mapping must fail readiness before an external effect can run."""
    from core.application.registry.db_store import RegistryDBStore

    contract = _inventory_extension_contract("set_inventory_quantities", _inventory_outputs())
    connection = _inventory_extension_connection()

    class _OpenConnection:
        closed = 0

    store = RegistryDBStore("postgresql://unused")
    store._conn = _OpenConnection()
    store._backend_initialized = True
    monkeypatch.setattr(
        "core.infrastructure.db.project_connection_store.get_project_connection_runtime_metadata",
        lambda _conn, connection_id: (
            deepcopy(connection) if connection_id == "inventory-1" else None
        ),
    )
    monkeypatch.setattr(
        "core.application.extensions.workflow_install_contract"
        ".resolve_workflow_extension_install_contract",
        lambda _conn, **_kwargs: deepcopy(contract),
    )

    issues = validate_extension_connector_step(
        store,
        step=_inventory_extension_step(
            "set_inventory_quantities", {"no_op": "state.inventory_was_unchanged"}
        ),
        step_path="workflow.phases[0].steps[0]",
    )

    assert issues == [
        {
            "code": "workflow.extension_output_mapping_invalid",
            "message": (
                "Output 'no_op' is not declared by the installed extension operation. "
                "Choose one of: definitely_no_external_effect, external_effect_status, "
                "inventory_adjustment_group."
            ),
            "path": "workflow.phases[0].steps[0].output_mapping_jsonb.no_op",
        }
    ]


@pytest.mark.parametrize(
    "selector",
    [
        "inventory_adjustment_group",
        "inventory_adjustment_group.changes[].delta",
        "result.inventory_adjustment_group.id",
    ],
)
def test_declared_inventory_output_and_schema_valid_nested_selectors_pass(
    selector: str,
) -> None:
    contract = _inventory_extension_contract("set_inventory_quantities", _inventory_outputs())

    assert (
        validate_extension_connector_step(
            _DB(_inventory_extension_connection(), contract=contract),
            step=_inventory_extension_step(
                "set_inventory_quantities", {selector: "state.inventory_result"}
            ),
            step_path="workflow.phases[0].steps[0]",
        )
        == []
    )


@pytest.mark.parametrize(
    "selector",
    ["result.data.id", "result.data.pagination", "result.data.releases[]"],
)
def test_fully_qualified_declared_output_paths_pass(selector: str) -> None:
    contract = _inventory_extension_contract(
        "lookup",
        [
            {"name": "result.data.id", "value_type": "integer"},
            {"name": "result.data.pagination", "value_type": "object"},
            {"name": "result.data.releases[]", "value_type": "object"},
        ],
    )

    assert (
        validate_extension_connector_step(
            _DB(_inventory_extension_connection(), contract=contract),
            step=_inventory_extension_step("lookup", {selector: "state.result"}),
            step_path="workflow.phases[0].steps[0]",
        )
        == []
    )


@pytest.mark.parametrize(
    "selector",
    ["product_variants", "product_variants[].inventory_item.id", "result.product_variants[].id"],
)
def test_declared_product_variant_output_and_schema_valid_nested_selectors_pass(
    selector: str,
) -> None:
    contract = _inventory_extension_contract(
        "create_product",
        [
            {
                "name": "product_variants",
                "value_type": "array",
                "schema": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "inventory_item": {
                                "type": "object",
                                "properties": {"id": {"type": "string"}},
                            },
                        },
                    },
                },
            }
        ],
    )

    assert (
        validate_extension_connector_step(
            _DB(_inventory_extension_connection(), contract=contract),
            step=_inventory_extension_step("create_product", {selector: "state.product_variants"}),
            step_path="workflow.phases[0].steps[0]",
        )
        == []
    )


def test_an_undeclared_operation_is_unavailable(production_contract) -> None:
    contract = _contract()
    contract["operation_manifest"] = [{"operation_id": "something_else"}]
    production_contract["value"] = contract

    issues = validate_extension_connector_step(
        _ProductionShapedStore(_connection()),
        step=_step(),
        step_path="workflow.phases[0].steps[0]",
    )

    assert [item["code"] for item in issues] == ["workflow.extension_operation_unavailable"]


def test_a_declared_connectionless_operation_is_not_called_unavailable(
    production_contract,
) -> None:
    """The reported failure, in one assertion."""
    production_contract["value"] = _connectionless_contract()

    step = _step()
    step.pop("connection_binding_jsonb")

    assert (
        validate_extension_connector_step(
            _ProductionShapedStore(None),
            step=step,
            step_path="workflow.phases[0].steps[0]",
        )
        == []
    )


def test_a_connection_bound_operation_requires_its_binding(production_contract) -> None:
    """Omitting the connection used to be the way to make the step "valid"."""
    production_contract["value"] = _multi_type_contract("mail_account")

    step = _step()
    step.pop("connection_binding_jsonb")

    issues = validate_extension_connector_step(
        _ProductionShapedStore(None),
        step=step,
        step_path="workflow.phases[0].steps[0]",
    )

    assert [item["code"] for item in issues] == ["workflow.connection_required"]


def _input_mapping_rules_contract() -> dict:
    contract = _contract()
    contract["operation_manifest"][0]["input_mapping_rules"] = [
        {
            "schema": {
                "type": "object",
                "anyOf": [{"required": ["body_text"]}, {"required": ["template_id"]}],
            },
            "error": {
                "code": "body_required",
                "message": "Map a body source.",
                "field": "body_text",
            },
        },
        {
            "schema": {
                "type": "object",
                "oneOf": [
                    {
                        "required": ["body_text"],
                        "properties": {"template_id": {"type": "null"}},
                    },
                    {
                        "required": ["template_id"],
                        "properties": {"body_text": {"type": "null"}},
                    },
                ],
            },
            "error": {
                "code": "body_source_ambiguous",
                "message": "Map one body source.",
                "field": "body_text",
            },
        },
        {
            "schema": {
                "type": "object",
                "if": {"required": ["body_text"]},
                "then": {"required": ["subject"]},
            },
            "error": {
                "code": "subject_required",
                "message": "Subject is required for a direct message.",
                "field": "subject",
            },
        },
    ]
    return contract


@pytest.mark.parametrize(
    ("input_mapping", "expected_code"),
    [
        ({}, "body_required"),
        (
            {
                "body_text": "state.inputs.message",
                "template_id": "state.inputs.template_id",
            },
            "body_source_ambiguous",
        ),
        ({"body_text": "state.inputs.message"}, "subject_required"),
    ],
)
def test_operation_input_mapping_rules_block_invalid_authoring_before_runtime(
    input_mapping: dict, expected_code: str
) -> None:
    step = _step()
    step["input_mapping_jsonb"] = input_mapping

    issues = validate_extension_connector_step(
        _DB(_connection(), contract=_input_mapping_rules_contract()),
        step=step,
        step_path="workflow.phases[0].steps[0]",
    )

    assert [issue["code"] for issue in issues] == [expected_code]


def test_operation_input_mapping_rules_accept_template_without_request_subject() -> None:
    step = _step()
    step["input_mapping_jsonb"] = {"template_id": "state.inputs.template_id"}

    assert (
        validate_extension_connector_step(
            _DB(_connection(), contract=_input_mapping_rules_contract()),
            step=step,
            step_path="workflow.phases[0].steps[0]",
        )
        == []
    )


def test_a_valid_binding_for_a_connection_bound_operation_passes(production_contract) -> None:
    production_contract["value"] = _multi_type_contract("mail_account")

    assert (
        validate_extension_connector_step(
            _ProductionShapedStore(_connection()),
            step=_step(),
            step_path="workflow.phases[0].steps[0]",
        )
        == []
    )


@pytest.mark.parametrize("bound_type", ["ftp", "sftp"])
def test_either_declared_connection_type_is_accepted(production_contract, bound_type: str) -> None:
    """A storage extension's operations work over either; a single-string projection
    could only ever name one of them, so the other read as a wrong type."""
    production_contract["value"] = _multi_type_contract("ftp", "sftp")

    assert (
        validate_extension_connector_step(
            _ProductionShapedStore(
                _connection(connection_type=bound_type, oauth_granted_scopes=["mail.send"])
            ),
            step=_step(),
            step_path="workflow.phases[0].steps[0]",
        )
        == []
    )


def test_an_undeclared_connection_type_is_still_rejected(production_contract) -> None:
    production_contract["value"] = _multi_type_contract("ftp", "sftp")

    issues = validate_extension_connector_step(
        _ProductionShapedStore(_connection(connection_type="acme_site")),
        step=_step(),
        step_path="workflow.phases[0].steps[0]",
    )

    assert [item["code"] for item in issues] == ["workflow.connection_unavailable"]


def test_a_binding_on_a_connectionless_operation_is_still_checked(production_contract) -> None:
    """A binding that is present is validated even when none was required: a
    step pointing at another install's connection must not be called ready."""
    production_contract["value"] = _connectionless_contract()

    issues = validate_extension_connector_step(
        _ProductionShapedStore(_connection(extension_install_id="install-other")),
        step=_step(),
        step_path="workflow.phases[0].steps[0]",
    )

    assert [item["code"] for item in issues] == ["workflow.connection_unavailable"]


def _nullable_mapping_preservation_contract() -> dict:
    contract = _contract()
    operation = contract["operation_manifest"][0]
    operation["inputs"] = [
        {"name": "query", "value_type": "text", "required": True},
        *[
            {"name": name, "value_type": "json", "required": False}
            for name in (
                "filter",
                "note",
                "zero",
                "flag",
                "encoded_null",
                "renamed_filter",
                "renamed_note",
            )
        ],
    ]
    operation["input_mapping_rules"] = [
        {
            "schema": {
                "type": "object",
                "required": ["query"],
                "additionalProperties": False,
                "properties": {
                    "query": {"type": "string"},
                    "filter": {"type": ["string", "null"]},
                    "note": {"type": "string"},
                    "zero": {"type": ["integer", "string"]},
                    "flag": {"type": ["boolean", "string"]},
                    "encoded_null": {"type": ["string", "null"]},
                    "renamed_filter": {"type": ["string", "null"]},
                    "renamed_note": {"type": "string"},
                },
            },
            "error": {
                "code": "mapping_fixture_invalid",
                "message": "Map supported values.",
                "field": "query",
            },
        }
    ]
    return contract


@pytest.mark.parametrize(
    "mapping",
    [
        {
            "query": "state.query",
            "filter": None,
            "note": "",
            "zero": 0,
            "flag": False,
            "encoded_null": "literal:null",
        },
        {
            "query": "state.changed",
            "filter": None,
            "note": "",
            "zero": "literal:0",
            "flag": "literal:false",
            "encoded_null": "literal:null",
        },
        {"query": "state.changed", "renamed_filter": None, "renamed_note": ""},
        {"query": "state.changed"},
    ],
)
def test_nullable_optional_mapping_fixture_accepts_preserved_and_renamed_values(
    mapping: dict,
) -> None:
    step = _step()
    step["input_mapping_jsonb"] = mapping
    assert (
        validate_extension_connector_step(
            _DB(_connection(), contract=_nullable_mapping_preservation_contract()),
            step=step,
            step_path="workflow.steps.mapping",
        )
        == []
    )


@pytest.mark.parametrize(
    "mapping",
    [
        {"query": "state.query", "filter": {}},
        {"filter": None},
        {"query": "state.query", "retainedValue": {"key": "filter", "value": None}},
    ],
)
def test_nullable_optional_mapping_fixture_rejects_wrong_type_missing_required_and_ui_metadata(
    mapping: dict,
) -> None:
    step = _step()
    step["input_mapping_jsonb"] = mapping
    issues = validate_extension_connector_step(
        _DB(_connection(), contract=_nullable_mapping_preservation_contract()),
        step=step,
        step_path="workflow.steps.mapping",
    )
    assert [issue["code"] for issue in issues] == ["mapping_fixture_invalid"]


@pytest.mark.parametrize(
    "column,alias,canonical",
    [
        ({}, {"query": "literal:old"}, {}),
        ({"query": "literal:new"}, None, {"query": "literal:new"}),
        ({}, None, {}),
    ],
)
def test_optional_mapping_operation_accepts_imported_edit_and_fully_empty_clear(
    column: dict, alias: dict | None, canonical: dict
) -> None:
    contract = _nullable_mapping_preservation_contract()
    operation = contract["operation_manifest"][0]
    operation["inputs"][0]["required"] = False
    operation["input_mapping_rules"][0]["schema"]["required"] = []
    step = _step()
    step["input_mapping_jsonb"] = column
    step["override"]["connector"]["input_mapping"] = canonical
    if alias is not None:
        step["override"]["connector"]["input_mapping_jsonb"] = alias
    assert (
        validate_extension_connector_step(
            _DB(_connection(), contract=contract),
            step=step,
            step_path="workflow.steps.mapping",
        )
        == []
    )
