"""Platform variable descriptions carry locale provenance; authored text does not."""

from unittest.mock import MagicMock

from core.application.registry.io_resolver import IOResolver
from core.application.registry.io_resolver_common import DOTTED_PATH_VARS, GLOBAL_STATE_VARS
from tests._workflow_definitions import linear_workflow


def _resolver_for_steps(steps: list[dict], skill_outputs: list[dict] | None = None) -> IOResolver:
    db = MagicMock()
    db.get_workflow.return_value = linear_workflow(*steps, workflow_id="description_example")
    db.list_published_block_contracts.return_value = []
    db.get_skill_outputs.return_value = skill_outputs or []
    db.get_agent_outputs.return_value = []
    return IOResolver(db)


def test_platform_state_and_dotted_variables_have_explicit_description_keys():
    variables = [*GLOBAL_STATE_VARS, *DOTTED_PATH_VARS]
    names = [variable["name"] for variable in variables]
    assert len(names) == len(set(names))
    assert not {"current_phase", "resume_phase"}.intersection(names)
    assert {"current_step_id", "current_group_id"}.issubset(names)

    assert all(variable.get("description_key") for variable in variables)
    by_name = {variable["name"]: variable for variable in variables}
    assert by_name["task_summary"]["description_key"] == (
        "react.workflows.variable_description.task_summary"
    )
    assert by_name["project_config.conventions"]["description_key"] == (
        "react.workflows.variable_description.project_conventions"
    )


def test_generated_state_outputs_supply_description_keys_and_source_parameters():
    resolver = _resolver_for_steps(
        [
            {
                "step_id": "prepare_result",
                "step_kind": "set_variable",
                "override": {"set_variable": {"assignments": {"state.result.summary": "Ready"}}},
            }
        ]
    )

    outputs = resolver.get_step_outputs("description_example", "prepare_result")
    by_name = {output["name"]: output for output in outputs}

    assert (
        by_name["result"]["description_key"] == "react.workflows.variable_description.state_object"
    )
    assert by_name["result.summary"]["description_key"] == (
        "react.workflows.variable_description.state_output"
    )
    assert by_name["result"]["description_params"] == {"source": "set_variable"}
    assert by_name["result.summary"]["description_params"] == {"source": "set_variable"}
    assert by_name["result.summary"]["description"] == "State output written by set_variable"


def test_authored_output_descriptions_are_not_marked_as_platform_copy():
    resolver = _resolver_for_steps(
        [{"step_id": "write_summary", "agent_id": "writer", "skill_id": "summary"}],
        [{"name": "result", "type": "text", "description": "State output written by set_variable"}],
    )

    output = resolver.get_step_outputs("description_example", "write_summary")[0]

    assert output["description"] == "State output written by set_variable"
    assert "description_key" not in output
    assert "description_params" not in output


def test_generated_description_keeps_metadata_when_filling_an_empty_contract_description():
    # Outputs are collected per step, so the empty contract description and the
    # generated state-output description must meet inside one step: the skill
    # declares ``result`` without a description and the step maps it to state.
    resolver = _resolver_for_steps(
        [
            {
                "step_id": "write_summary",
                "agent_id": "writer",
                "skill_id": "summary",
                "output_mapping_jsonb": {"result": "state.result"},
            },
        ],
        [{"name": "result", "type": "text", "description": ""}],
    )

    output = resolver.get_step_outputs("description_example", "write_summary")[0]

    assert output["description_key"] == "react.workflows.variable_description.state_output"
    assert output["description_params"] == {"source": "agent"}
