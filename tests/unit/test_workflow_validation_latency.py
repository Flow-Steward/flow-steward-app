"""Readiness of a large workflow does not repeat work per step.

The builder waits for the canvas graph, which runs readiness. On a 60-node
workflow it took 17 s because every extension step parsed its extension's
contract files again, every child-workflow step reloaded the parent and child
rows, and every step rebuilt the view of the whole graph. These tests pin the
work to once per release, once per run and once per workflow row.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml

import core.application.registry.contract_validation  # noqa: F401 -- joins the split modules as the app does
from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.extensions import workflow_install_contract as contracts
from core.application.extensions.extension_contract_loading import _parse_json_or_yaml_text
from core.application.registry.contract_validation_workflow_readiness import (
    validate_workflow_readiness,
)
from core.application.registry.io_resolver import IOResolver
from core.application.workflows.execution_graph_queries import DefinitionGraph
from tests._workflow_definitions import linear_workflow

INSTALL_ID = "install-mail"


@pytest.fixture(autouse=True)
def _empty_contract_cache():
    contracts.clear_normalized_contract_cache()
    yield
    contracts.clear_normalized_contract_cache()


class _Installs:
    calls = 0

    def __init__(self, _connection) -> None:
        pass

    def list_for_account(self, account_id, **_kwargs):
        type(self).calls += 1
        return [
            SimpleNamespace(
                install_id=INSTALL_ID,
                release_id="release-1",
                extension_id="fixture.mail",
                visibility="account",
                account_id=account_id,
                state="active",
            )
        ]


class _Releases:
    digest = "digest-1"

    def __init__(self, _connection) -> None:
        pass

    def get_release(self, release_id):
        return SimpleNamespace(
            release_id=release_id,
            extension_id="fixture.mail",
            descriptor_sha256=type(self).digest,
            descriptor={
                "bundle_root": "/releases/fixture.mail/1.0.0",
                # File references: the contract has to be read from the bundle.
                "runtime": {"extension_contract_v2": {"operation_manifest": "contracts/ops.yaml"}},
            },
        )


@pytest.fixture
def release_store(monkeypatch):
    """The install and release stores, and a normalizer that counts its runs."""
    _Installs.calls = 0
    _Releases.digest = "digest-1"
    normalized = {"runs": 0, "valid": [True]}

    def normalize(descriptor, *, bundle_root=None):
        normalized["runs"] += 1
        assert bundle_root == descriptor["bundle_root"]
        valid = (
            normalized["valid"].pop(0) if len(normalized["valid"]) > 1 else normalized["valid"][0]
        )
        operations = [{"operation_id": "send_message", "outputs": [{"name": "message_id"}]}]
        return {"valid": valid, "normalized_contract": {"operation_manifest": operations}}

    monkeypatch.setattr("core.application.extensions.install.DBExtensionInstallStore", _Installs)
    monkeypatch.setattr(
        "core.application.extensions.runtime_store_db.DBExtensionRuntimeStore", _Releases
    )
    monkeypatch.setattr(contracts, "validate_extension_contract_v2", normalize)
    return normalized


def _resolve():
    return contracts.resolve_workflow_extension_install_contract(
        object(),
        install_id=INSTALL_ID,
        account_id="account-1",
        default_account_id=DEFAULT_ACCOUNT_ID,
    )


def test_a_release_contract_is_read_from_its_files_once(release_store) -> None:
    first = _resolve()
    second = _resolve()

    assert release_store["runs"] == 1
    assert first == second
    assert first["operation_manifest"][0]["operation_id"] == "send_message"
    assert first["install_id"] == INSTALL_ID
    assert first["descriptor_sha256"] == "digest-1"


def test_each_caller_gets_its_own_copy_of_a_kept_contract(release_store) -> None:
    first = _resolve()
    first["operation_manifest"][0]["outputs"].append({"name": "changed"})

    assert _resolve()["operation_manifest"][0]["outputs"] == [{"name": "message_id"}]


def test_a_different_release_digest_is_read_again(release_store) -> None:
    _resolve()
    _Releases.digest = "digest-2"
    _resolve()

    assert release_store["runs"] == 2


def test_an_invalid_contract_is_not_kept(release_store) -> None:
    release_store["valid"] = [False, True]

    first = _resolve()
    second = _resolve()

    assert "operation_manifest" not in first
    assert second["operation_manifest"][0]["operation_id"] == "send_message"
    assert release_store["runs"] == 2


def test_the_kept_contracts_are_bounded(release_store, monkeypatch) -> None:
    monkeypatch.setattr(contracts, "_NORMALIZED_CONTRACT_CACHE_SIZE", 2)
    for digest in ("a", "b", "c"):
        _Releases.digest = digest
        _resolve()

    assert len(contracts._normalized_contract_cache) == 2
    _Releases.digest = "a"
    _resolve()
    assert release_store["runs"] == 4


def test_inside_one_run_an_install_is_resolved_once_and_shared(release_store) -> None:
    with contracts.shared_install_contracts():
        first = _resolve()
        second = _resolve()
        with contracts.shared_install_contracts():
            third = _resolve()

    assert _Installs.calls == 1
    assert first is second is third
    _resolve()
    assert _Installs.calls == 2


def test_contract_yaml_is_parsed_with_a_safe_loader() -> None:
    text = "operations:\n  - operation_id: send\n    outputs: [a, b]\n"

    assert _parse_json_or_yaml_text(text, ref_hint="ops.yaml") == yaml.safe_load(text)
    with pytest.raises(yaml.YAMLError):
        _parse_json_or_yaml_text("!!python/object/apply:os.getcwd []", ref_hint="ops.yaml")


class _ReadinessDB:
    """The registry reads readiness makes, counting workflow row loads."""

    def __init__(self, *workflows: dict) -> None:
        self.workflows = {row["workflow_id"]: row for row in workflows}
        self.loads: dict[str, int] = {}
        self.conn = object()

    def get_workflow(self, workflow_id: str):
        self.loads[workflow_id] = self.loads.get(workflow_id, 0) + 1
        row = self.workflows.get(workflow_id)
        return dict(row) if row else None

    def get_tool(self, _tool_id: str):
        return None

    def get_skill_inputs(self, _skill_id: str):
        return []

    get_skill_outputs = get_agent_inputs = get_agent_outputs = get_skill_inputs

    def get_agent_skills(self, _agent_id: str):
        return []

    get_agent_tools = get_agent_skills


def _extension_step(step_id: str) -> dict:
    return {
        "step_id": step_id,
        "step_kind": "connector",
        "connector_id": "extension.fixture.mail",
        "connector_install_id": INSTALL_ID,
        "override": {"connector": {"operation_id": "send_message", "extension_id": "fixture.mail"}},
    }


def test_readiness_resolves_an_install_once_for_all_its_steps(monkeypatch) -> None:
    resolved: list[str] = []

    def resolve(_connection, *, install_id, account_id, default_account_id):
        resolved.append(install_id)
        return {
            "extension_id": "fixture.mail",
            "install_id": install_id,
            "account_id": account_id or default_account_id,
            "install_state": "active",
            "operation_manifest": [{"operation_id": "send_message", "outputs": []}],
        }

    monkeypatch.setattr(contracts, "_resolve_install_contract", resolve)
    workflow = linear_workflow(*(_extension_step(f"send_{index}") for index in range(6)))

    validate_workflow_readiness(_ReadinessDB(workflow), workflow["workflow_id"])

    assert resolved == [INSTALL_ID]


def _child() -> dict:
    return linear_workflow(
        {
            "step_id": "fetch",
            "step_kind": "http_request",
            "override": {"http_request": {"method": "GET", "url": "https://example.test"}},
        },
        workflow_id="child_wf",
        settings={
            "callable": {
                "enabled": True,
                "expected_inputs": [],
                "expected_outputs": [],
                "trigger_channels": ["parent_workflow"],
                "allow_empty_inputs": True,
            }
        },
    )


def _parent(children: int) -> dict:
    return linear_workflow(
        *(
            {
                "step_id": f"call_{index}",
                "step_kind": "invoke_workflow",
                "override": {
                    "invoke_workflow": {
                        "workflow_id": "child_wf",
                        "input_mapping": {},
                        "output_mapping": {},
                    }
                },
            }
            for index in range(children)
        ),
        workflow_id="parent_wf",
    )


def test_child_workflow_rows_are_not_reloaded_for_every_child_step() -> None:
    few = _ReadinessDB(_parent(2), _child())
    many = _ReadinessDB(_parent(8), _child())

    few_result = validate_workflow_readiness(few, "parent_wf")
    many_result = validate_workflow_readiness(many, "parent_wf")

    assert many.loads == few.loads
    assert many.loads["child_wf"] <= 2
    assert not [issue for issue in many_result["errors"] if issue["code"].startswith("invoke.")]
    assert few_result["valid"] == many_result["valid"]


def _counting_graph_builds(monkeypatch) -> list[int]:
    builds: list[int] = []
    original = DefinitionGraph.from_workflow.__func__

    def from_workflow(cls, workflow):
        builds.append(1)
        return original(cls, workflow)

    monkeypatch.setattr(DefinitionGraph, "from_workflow", classmethod(from_workflow))
    return builds


def _chain(length: int) -> dict:
    return linear_workflow(
        *(
            {
                "step_id": f"step_{index}",
                "step_kind": "http_request",
                "override": {
                    "http_request": {
                        "method": "GET",
                        "url": "https://example.test",
                        "response_target": f"state.body_{index}",
                    }
                },
            }
            for index in range(length)
        ),
        workflow_id="chain",
    )


def test_a_resolver_that_keeps_graphs_builds_one_per_workflow(monkeypatch) -> None:
    builds = _counting_graph_builds(monkeypatch)
    db = _ReadinessDB(_chain(5))
    resolver = IOResolver(db, keep_definition_graphs=True)

    outputs = [resolver.get_available_outputs_for_step("chain", f"step_{i}") for i in range(5)]
    resolver.get_available_outputs_for_step("chain", "step_4")

    assert len(builds) == 1
    assert {row["name"] for row in outputs[4]} >= {"body_0", "body_3"}


def test_a_default_resolver_still_builds_the_graph_per_question(monkeypatch) -> None:
    builds = _counting_graph_builds(monkeypatch)
    resolver = IOResolver(_ReadinessDB(_chain(3)))

    for index in range(3):
        resolver.get_available_outputs_for_step("chain", f"step_{index}")

    assert len(builds) == 3
