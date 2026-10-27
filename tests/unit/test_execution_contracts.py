from core.application.jobs.execution_contracts import terminal_failure_context


def test_terminal_failure_context_falls_back_to_handled_graph_step_failure() -> None:
    result = {
        "unresolved_step_failure": {
            "step_id": "validate-inputs",
            "step_execution_id": "6f1c2a1e-3b9d-4c55-9a0e-2f7f3e8d1b42",
            "attempt": 2,
            "group_id": "implementation",
            "occurrence": 2,
            "error_code": "skill_input_invalid",
            "selector": "state.seed_value",
            "diagnostic_kind": "transform",
            "message": "Required input repository is missing",
        }
    }

    assert terminal_failure_context(result, observed={}) == {
        "step_id": "validate-inputs",
        "step_execution_id": "6f1c2a1e-3b9d-4c55-9a0e-2f7f3e8d1b42",
        "group_id": "implementation",
        "occurrence": 2,
        "error_code": "skill_input_invalid",
        "selector": "state.seed_value",
        "diagnostic_kind": "transform",
    }


def test_terminal_failure_context_prefers_observed_callback_location() -> None:
    result = {
        "unresolved_step_failure": {
            "step_id": "handled-step",
            "group_id": "build",
            "occurrence": 1,
            "error_code": "step_failed",
        }
    }

    assert terminal_failure_context(
        result,
        observed={
            "step_id": "provider-call",
            "group_id": "delivery",
            "occurrence": 3,
        },
    ) == {
        "step_id": "provider-call",
        "group_id": "delivery",
        "occurrence": 3,
        "error_code": "step_failed",
    }


def test_terminal_failure_context_preserves_tabular_state_diagnostic() -> None:
    result = {
        "unresolved_step_failure": {
            "step_id": "read-rows",
            "group_id": "read",
            "error_code": "state_reference_missing",
            "selector": "state.created_dataset.dataset_handle",
            "diagnostic_kind": "tabular_feed",
        }
    }

    assert terminal_failure_context(result) == {
        "step_id": "read-rows",
        "group_id": "read",
        "error_code": "state_reference_missing",
        "selector": "state.created_dataset.dataset_handle",
        "diagnostic_kind": "tabular_feed",
    }


def test_terminal_failure_context_preserves_dataset_path_and_correlation() -> None:
    result = {
        "unresolved_step_failure": {
            "step_id": "read-orders",
            "group_id": "read",
            "error_code": "query_sql_missing",
            "path": "query_definition_jsonb.sql",
            "diagnostic_kind": "dataset_query",
            "correlation_id": "corr-dataset-1",
        }
    }

    assert terminal_failure_context(result) == {
        "step_id": "read-orders",
        "group_id": "read",
        "error_code": "query_sql_missing",
        "path": "query_definition_jsonb.sql",
        "diagnostic_kind": "dataset_query",
        "correlation_id": "corr-dataset-1",
    }
