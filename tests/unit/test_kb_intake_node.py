from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def _state(**overrides) -> dict:
    base = {
        "project_name": "Demo",
        "project_config": {"project_id": "project-7", "account_id": "account-1"},
        "trusted_runtime_context": {
            "project_id": "project-7",
            "account_id": "account-1",
        },
        "job_id": 11,
    }
    base.update(overrides)
    return base


def test_kb_intake_node_sanitizes_profile_with_runtime_state():
    from core.application.orchestration.agents.kb_intake_node import kb_intake_node

    state = _state()
    with (
        patch(
            "core.application.orchestration.agents.kb_intake_node.load_project_context",
            return_value="raw profile",
        ) as load_context,
        patch(
            "core.application.orchestration.agents.common.sanitize_external_input", return_value="safe profile"
        ) as sanitize_mock,
    ):
        payload = kb_intake_node(state)

    assert payload["kb_context"] == "safe profile"
    load_context.assert_called_once_with(account_id="account-1", project_id="project-7")
    assert sanitize_mock.call_count == 1
    _, kwargs = sanitize_mock.call_args
    assert kwargs["context_type"] == "kb"
    assert kwargs["state"] is state


def test_kb_intake_node_returns_none_when_profile_missing():
    from core.application.orchestration.agents.kb_intake_node import kb_intake_node

    with patch(
        "core.application.orchestration.agents.kb_intake_node.load_project_context",
        return_value="",
    ):
        payload = kb_intake_node(_state())

    assert payload == {"kb_context": None}


def test_kb_intake_node_missing_identity_is_optional_by_default():
    from core.application.orchestration.agents.kb_intake_node import kb_intake_node

    state = _state(project_config={}, trusted_runtime_context=None, account_id=None)
    with patch(
        "core.application.orchestration.agents.kb_intake_node.load_project_context"
    ) as load_context:
        assert kb_intake_node(state) == {"kb_context": None}

    load_context.assert_not_called()


def test_kb_intake_node_missing_identity_fails_when_grounding_is_mandatory():
    import pytest

    from core.application.orchestration.agents.kb_intake_node import kb_intake_node

    state = _state(project_config={}, trusted_runtime_context=None, account_id=None)
    with (
        patch(
            "core.application.orchestration.agents.kb_intake_node._runtime_env",
            {"FS_MCP_GROUNDING_REQUIRED": "1"},
        ),
        pytest.raises(RuntimeError, match="project grounding.*identity"),
    ):
        kb_intake_node(state)
