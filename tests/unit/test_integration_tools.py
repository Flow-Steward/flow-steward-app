"""Unit tests for provider-agnostic integration connector tools."""

from __future__ import annotations


class TestMcpConnectorInvoke:
    def test_requires_connector_id(self):
        from core.application.orchestration.tools.integration_tools import mcp_connector_invoke

        result = mcp_connector_invoke.invoke({"connector_id": "   "})
        assert "connector_id is required" in result

    def test_rejects_invalid_arguments_json(self):
        from core.application.orchestration.tools.integration_tools import mcp_connector_invoke

        result = mcp_connector_invoke.invoke(
            {
                "connector_id": "connector.example.lookup",
                "arguments_json": "{not-json}",
            }
        )
        assert "valid JSON" in result

    def test_rejects_non_object_arguments_json(self):
        from core.application.orchestration.tools.integration_tools import mcp_connector_invoke

        result = mcp_connector_invoke.invoke(
            {
                "connector_id": "connector.example.lookup",
                "arguments_json": '["not", "an", "object"]',
            }
        )
        assert "JSON object" in result

    def test_rejects_direct_connector_invocation_without_runtime_scope(self):
        from core.application.orchestration.tools.integration_tools import mcp_connector_invoke

        result = mcp_connector_invoke.invoke(
            {
                "connector_id": "connector.example.lookup",
                "function_name": "search_docs",
                "arguments_json": '{"query":"FastAPI"}',
            }
        )

        assert "requires workflow runtime scope" in result


class TestStandardToolActions:
    def test_standard_number_round_returns_result_field(self):
        from core.application.orchestration.tools.standard_tools import round as std_round

        assert std_round.invoke({"value": 12.345, "decimals": 2}) == {"result": 12.35}

    def test_standard_text_actions_are_deterministic(self):
        from core.application.orchestration.tools.standard_tools import length, lower, upper

        assert upper.invoke({"value": "Hello"}) == {"result": "HELLO"}
        assert lower.invoke({"value": "Hello"}) == {"result": "hello"}
        assert upper.invoke({"value": 0}) == {"result": "0"}
        assert lower.invoke({"value": False}) == {"result": "false"}
        assert length.invoke({"value": ["a", "b", "c"]}) == {"result": 3}

    def test_standard_json_actions_are_deterministic(self):
        from core.application.orchestration.tools.standard_tools import pick, stringify

        assert stringify.invoke({"value": {"b": 2, "a": 1}}) == {
            "result": '{"a": 1, "b": 2}'
        }
        assert pick.invoke(
            {"value": {"items": [{"name": "first"}]}, "path": "items.0.name"}
        ) == {"result": "first"}
