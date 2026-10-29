"""Named date failures survive the actual connector-step exception boundary."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _step_result(*, failure=None):
    from core.application.orchestration.connector_step_runtime import execute_connector_step
    from core.application.registry.builtin_standard_tools import builtin_standard_tool_manifests
    from core.application.registry.tool_manifest import ToolManifest

    manifest = ToolManifest.model_validate(builtin_standard_tool_manifests()["std.date"])
    registry = SimpleNamespace(
        tools=SimpleNamespace(get_tool=lambda tool_id: manifest if tool_id == "std.date" else None)
    )
    step = {
        "step_kind": "connector",
        "connector_id": "connector.std.date",
        "override": {
            "connector": {
                "operation_id": "parse",
                "input_mapping": {"value": "literal:03/04/2026"},
            }
        },
    }
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.application.orchestration.connector_tool_runtime._shared_logger",
            return_value=None,
        ),
        patch(
            "core.application.orchestration.connector_step_runtime._shared_job_store",
            return_value=None,
        ),
    ):
        if failure is None:
            return execute_connector_step(step=step, state={})
        with patch(
            "core.application.orchestration.tools.tool_resolver.invoke_manifest_tool",
            side_effect=failure,
        ):
            return execute_connector_step(step=step, state={})


def test_actual_date_connector_step_preserves_ambiguous_date_code():
    result = _step_result()

    assert result["ok"] is False
    assert result["runtime"] == "tool_manifest"
    assert result["connector_id"] == "connector.std.date"
    assert result["function_name"] == "parse"
    assert result["result"] == {}
    assert result["errors"] == [{"code": "ambiguous_date", "message": result["detail"]}]
    assert result["detail"]


@pytest.mark.parametrize("unknown_domain_code", [False, True])
def test_connector_step_does_not_trust_forged_or_unknown_date_codes(unknown_domain_code):
    from core.infrastructure.date_types import DateValueError

    class ForgedDateFailure(ValueError):
        code = "ambiguous_date"

    failure = (
        DateValueError("unknown_date_failure", "unexpected date failure")
        if unknown_domain_code
        else ForgedDateFailure("ordinary connector failure")
    )
    result = _step_result(failure=failure)

    assert result["ok"] is False
    assert result["result"] == {}
    assert result["detail"] == str(failure)
    assert "errors" not in result
