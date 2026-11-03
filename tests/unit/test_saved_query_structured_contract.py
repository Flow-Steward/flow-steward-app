"""Structured saved queries preserve typed arguments and fail closed before dispatch."""

from copy import deepcopy

import pytest

from core.application.registry.saved_query_contract import (
    SavedQueryContractError,
    validate_saved_query_contract,
)


def _dataset():
    return {
        "query_language": "json",
        "resource_scope_jsonb": {"resources": [{"resource_id": "Sales.Orders"}]},
    }


def _payload(**definition):
    return {
        "query_language": "json",
        "query_definition_jsonb": {
            "resource_id": "Sales.Orders",
            "operation": "find",
            "arguments": {},
            **definition,
        },
    }


@pytest.mark.parametrize(
    "operation",
    ["find", "aggregate", "get", "mget", "hget", "hgetall", "lrange", "search", "query"],
)
def test_structured_read_operations_accept_empty_arguments_without_mutation(operation):
    payload = _payload(operation=operation)
    original = deepcopy(payload)
    validate_saved_query_contract(payload, dataset=_dataset())
    assert payload == original


def test_structured_nested_typed_parameters_are_preserved():
    payload = _payload(
        arguments={
            "filter": {"active": {"$param": "active"}},
            "ids": [{"$param": "ids"}],
            "literal": ":undeclared",
        }
    )
    payload["params_schema_jsonb"] = {
        "type": "object",
        "properties": {"active": {"type": "boolean"}, "ids": {"type": "array"}},
    }
    original = deepcopy(payload)
    validate_saved_query_contract(payload, dataset=_dataset())
    assert payload == original


@pytest.mark.parametrize(
    "definition,path",
    [
        ({"resource_id": "sales.orders"}, "query_definition_jsonb.resource_id"),
        ({"resource_id": "Orders"}, "query_definition_jsonb.resource_id"),
        ({"resource_id": " Sales.Orders "}, "query_definition_jsonb.resource_id"),
        ({"resource_id": ""}, "query_definition_jsonb.resource_id"),
        ({"operation": "delete"}, "query_definition_jsonb.operation"),
        ({"operation": "FIND"}, "query_definition_jsonb.operation"),
        ({"arguments": []}, "query_definition_jsonb.arguments"),
        ({"arguments": None}, "query_definition_jsonb.arguments"),
        ({"sql": "select 1 limit 1"}, "query_definition_jsonb.sql"),
        ({"arguments": {"x": {"$param": "missing"}}}, "params_schema_jsonb.properties.missing"),
        (
            {"arguments": {"x": {"$param": "missing", "extra": True}}},
            "query_definition_jsonb.arguments.x",
        ),
        ({"arguments": {"x": {"$param": 1}}}, "query_definition_jsonb.arguments.x"),
        ({"arguments": {"x": float("nan")}}, "query_definition_jsonb.arguments"),
        ({"arguments": {"x": "x" * 70000}}, "query_definition_jsonb.arguments"),
    ],
)
def test_structured_invalid_definitions_fail_at_the_owning_field(definition, path):
    with pytest.raises(SavedQueryContractError) as error:
        validate_saved_query_contract(_payload(**definition), dataset=_dataset())
    assert error.value.path == path


def test_structured_excessive_depth_is_rejected():
    arguments = {}
    for _ in range(40):
        arguments = {"nested": arguments}
    with pytest.raises(SavedQueryContractError) as error:
        validate_saved_query_contract(_payload(arguments=arguments), dataset=_dataset())
    assert error.value.path == "query_definition_jsonb.arguments"


def test_structured_scope_can_be_persisted_in_metadata():
    validate_saved_query_contract(
        _payload(),
        dataset={"metadata_jsonb": {"resource_scope_jsonb": _dataset()["resource_scope_jsonb"]}},
    )


@pytest.mark.parametrize(
    "language,definition,dataset_language",
    [
        ("json", {"resource_id": "Sales.Orders", "operation": "find", "arguments": {}}, "sql"),
        ("sql", {"sql": "select * from Sales.Orders limit 1"}, "json"),
    ],
)
def test_saved_query_language_must_match_dataset(language, definition, dataset_language):
    dataset = {**_dataset(), "query_language": dataset_language}
    with pytest.raises(SavedQueryContractError) as error:
        validate_saved_query_contract(
            {"query_language": language, "query_definition_jsonb": definition}, dataset=dataset
        )
    assert error.value.path == "query_language"


def test_sql_scope_and_named_parameters_remain_compatible():
    validate_saved_query_contract(
        {
            "query_definition_jsonb": {"sql": "select * from orders where id = :id"},
            "params_schema_jsonb": {"properties": {"id": {"type": "integer"}}},
        },
        dataset={"resource_scope_jsonb": {"resources": [{"resource_id": "public.orders"}]}},
    )


def test_dataset_save_derives_language_from_the_installed_connector():
    from types import SimpleNamespace

    from core.entrypoints.admin.datasets_helpers import _validate_dataset_payload

    connection = {
        "connection_id": "json-connection",
        "workspace_id": "workspace",
        "enabled": True,
        "connector_ref_jsonb": {
            "connector_id": "json-connection",
            "extension_id": "acme.json-source",
        },
    }
    db = SimpleNamespace(get_workspace_data_connection=lambda connection_id: connection)
    dataset = _validate_dataset_payload(
        {
            "dataset_id": "orders",
            "display_name": "Orders",
            "workspace_id": "workspace",
            "connection_id": "json-connection",
            "query_language": "sql",
        },
        db=db,
        connector_catalog={
            "json-connection": {
                "connector_id": "json-connection",
                "extension_id": "acme.json-source",
                "query_language": "json",
                "connector_kind": "extension",
                "runtime_kind": "local_extension",
            }
        },
    )
    assert dataset["query_language"] == "json"
    dataset["metadata_jsonb"]["resource_scope_jsonb"] = _dataset()["resource_scope_jsonb"]
    validate_saved_query_contract(_payload(), dataset=dataset)


@pytest.mark.parametrize(
    "arguments", [{"items": [None] * 10001}, {"value": (1, 2)}, {1: "not a JSON object key"}]
)
def test_structured_arguments_reject_excessive_nodes_and_non_json_values(arguments):
    with pytest.raises(SavedQueryContractError) as error:
        validate_saved_query_contract(_payload(arguments=arguments), dataset=_dataset())
    assert error.value.path == "query_definition_jsonb.arguments"


def test_structured_queries_require_nonempty_resource_scope():
    with pytest.raises(SavedQueryContractError) as error:
        validate_saved_query_contract(_payload(), dataset={"query_language": "json"})
    assert error.value.path == "dataset.resource_scope_jsonb"


@pytest.mark.parametrize(
    "definition", [None, [], "{}", {}, {"resource_id": "Sales.Orders", "operation": "find"}]
)
def test_structured_definition_requires_the_complete_object_shape(definition):
    with pytest.raises(SavedQueryContractError):
        validate_saved_query_contract(
            {"query_language": "json", "query_definition_jsonb": definition}, dataset=_dataset()
        )


@pytest.mark.parametrize("placeholder", ["", "bad-name", None, [], {}])
def test_structured_parameter_references_require_identifier_names(placeholder):
    with pytest.raises(SavedQueryContractError) as error:
        validate_saved_query_contract(
            _payload(arguments={"filter": {"$param": placeholder}}), dataset=_dataset()
        )
    assert error.value.path == "query_definition_jsonb.arguments.filter"
