"""Run search YAMLs through real LangGraph nodes with an offline HTTP transport.

Run inside the version-matched Flow Steward audit runtime, with networking off.
No source n8n code or provider API is executed. This verifies a preselected,
pinned workflow; UI admission, broker and database persistence are out of scope.
"""

from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import ipaddress
import json
import socket
import traceback
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from core.application.orchestration import http_request_step_runtime as http_runtime
from core.application.orchestration.control_flow_step_node import control_flow_step_node
from core.application.orchestration.graph.nodes import execute_step_node
from core.application.orchestration.operation_retry_wait_node import operation_retry_wait_node
from core.application.orchestration.run_cursor import RUNTIME_SCHEMA_VERSION
from core.application.orchestration.run_definition import (
    clear_run_definition_cache,
    prime_run_definition,
)
from core.application.orchestration.state import OrchestratorState, build_initial_orchestrator_state
from core.application.orchestration.step_hub import orchestrator_hub_node
from core.application.orchestration.validation_node import orchestrator_validation_node
from core.application.registry.workflow_contract_service import WorkflowInterfaceService
from core.application.workflows.workflow_definition_snapshot import definition_digest
from core.application.workflows.workflow_definition_validation import validate_workflow_definition
from core.application.workflows.workflow_portability import WorkflowPortabilityService
from core.application.workflows.workflow_step_config_contracts import (
    validate_workflow_step_config_keys,
)
from core.infrastructure.security.secret_reference import inline_secret_ref_fingerprint

CANARY = "offline-fixture-not-a-real-provider-key"
CIPHERTEXT = "offline-fixture-ciphertext"
HOSTS = {"serpapi.com", "api.tavily.com", "api.exa.ai"}


def cases_for(provider: str, contract: dict[str, Any]) -> list[dict[str, Any]]:
    fixtures = contract["fixtures"]
    success = copy.deepcopy(fixtures["success"])
    empty = copy.deepcopy(fixtures["empty_success"])
    cases = [
        {"name": "success", "body": success, "success": True},
        {"name": "empty_success", "body": empty, "success": True},
        {
            "name": "quoted_unicode_query",
            "body": success,
            "success": True,
            "query": 'Vilnius "B2B" & café / {{ example }}',
        },
        {"name": "missing_secret", "omit": "provider_credential", "admission_error": True},
        {"name": "empty_secret", "credential": "  ", "admission_error": True},
        {"name": "missing_query", "omit": "query", "admission_error": True},
        {"name": "blank_query", "query": "   ", "admission_error": True},
        {"name": "long_query", "query": "x" * 2001, "no_dispatch": True},
        {"name": "wrong_query_type", "query": {"x": 1}, "admission_error": True},
        {"name": "unauthorized", "status": 401, "body": {"error": "fixture unauthorized"}},
        {"name": "forbidden", "status": 403, "body": {"error": "fixture forbidden"}},
        {
            "name": "rate_limited",
            "status": 429,
            "body": {"error": "fixture rate limit"},
            "headers": {"Retry-After": "0"},
        },
        {"name": "provider_unavailable", "status": 503, "body": {"error": "fixture unavailable"}},
        {"name": "connect_timeout", "exception": "connect_timeout"},
        {"name": "read_timeout", "exception": "read_timeout"},
        {
            "name": "redirect_not_followed",
            "status": 302,
            "body": {},
            "headers": {"Location": "https://untrusted.invalid/next"},
        },
        {"name": "invalid_json", "raw": "{not JSON", "content_type": "application/json"},
        {"name": "html_response", "raw": "<html>fixture error</html>", "content_type": "text/html"},
        {"name": "root_array", "body": []},
        {"name": "root_null", "body": None},
        {"name": "missing_envelope", "body": {"unrelated": "object"}},
        {"name": "oversized_response", "raw": "x" * (1024 * 1024 + 1)},
        {"name": "compressed_oversized_response", "raw": "x" * (1024 * 1024 + 1), "gzip": True},
        {"name": "secret_reflected", "body": {**success, "debug": CANARY}},
        {"name": "secret_in_transport_error", "exception": "secret_error"},
        {"name": "gzip_success", "body": success, "success": True, "gzip": True},
    ]
    if provider == "serpapi":
        cases += [
            {
                "name": "provider_error_200",
                "body": {"search_metadata": {"status": "Error"}, "error": "fixture provider error"},
            },
            {
                "name": "incomplete_processing",
                "body": {"search_metadata": {"status": "Processing"}},
            },
            {"name": "incomplete_queued", "body": {"search_metadata": {"status": "Queued"}}},
            {
                "name": "knowledge_graph_only",
                "body": {
                    "search_metadata": {"status": "Success"},
                    "knowledge_graph": {"title": "Fixture"},
                },
                "success": True,
            },
            {"name": "wrong_results_type", "body": {**empty, "organic_results": {}}},
            {
                "name": "wrong_result_item",
                "body": {
                    "search_metadata": {"status": "Success"},
                    "organic_results": ["not an object"],
                },
            },
            {
                "name": "unqualified_success_error",
                "body": {"search_metadata": {"status": "Success"}, "error": "not documented empty"},
            },
        ]
    else:
        cases += [
            {"name": "wrong_results_type", "body": {"results": {}}},
            {"name": "null_results", "body": {"results": None}},
            {"name": "wrong_result_item", "body": {"results": ["not an object"]}},
            {
                "name": "provider_error_200",
                "body": {"error": "fixture provider error", "results": []},
            },
        ]
    if provider == "exa":
        cases += [
            {"name": "missing_item_title", "body": {"results": [{"url": "https://example.com/"}]}},
            {"name": "missing_item_url", "body": {"results": [{"title": "Fixture"}]}},
            {
                "name": "wrong_item_field_type",
                "body": {"results": [{"title": "Fixture", "url": 123}]},
            },
        ]
    if provider == "tavily":
        cases.append(
            {
                "name": "nested_provider_error_200",
                "body": {"results": [], "detail": {"error": "fixture error"}},
            }
        )
    return cases


def build_graph():
    graph = StateGraph(OrchestratorState)
    for name, node in [
        ("orchestrator", orchestrator_hub_node),
        ("execute_step", execute_step_node),
        ("control_flow_step", control_flow_step_node),
        ("orchestrator_validation", orchestrator_validation_node),
        ("operation_retry_wait", operation_retry_wait_node),
    ]:
        graph.add_node(name, node)
    graph.add_edge(START, "orchestrator")
    graph.add_edge("execute_step", "orchestrator")
    graph.add_edge("control_flow_step", "orchestrator")
    graph.add_edge("orchestrator_validation", END)
    return graph.compile(checkpointer=MemorySaver())


def verify_case(
    provider: str,
    definition: dict[str, Any],
    case: dict[str, Any],
    contract: dict[str, Any],
    fixture_public_ip: str,
) -> dict[str, Any]:
    requests: list[httpx.Request] = []
    connects: list[str] = []
    wire_errors: list[str] = []
    initial_inputs = {
        "query": case.get("query", "offline B2B fixture"),
        "provider_credential": case.get("credential", CANARY),
    }
    initial_inputs.pop(case.get("omit", ""), None)

    def dns(host, *_args, **_kwargs):
        if host not in HOSTS:
            raise AssertionError("Unexpected DNS lookup outside the fixture hosts")
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (fixture_public_ip, 443))
        ]

    def no_socket(_socket, address):
        connects.append(str(address))
        raise AssertionError("Socket connections are forbidden")

    def transport_impl(request):
        requests.append(request)
        assert len(requests) <= 3, "Unbounded retry or pagination"
        assert request.url.host in HOSTS
        assert str(request.url.copy_with(query=None)) == contract["request"]["url"]
        assert request.method == contract["request"]["method"]
        if provider == "serpapi":
            assert request.url.params["engine"] == "google"
            assert request.url.params["q"] == initial_inputs["query"]
            assert request.url.params["api_key"] == CANARY
            assert request.url.params.get("async", "false") == "false"
            assert not request.content
        else:
            payload = json.loads(request.content)
            assert payload["query"] == initial_inputs["query"]
            if provider == "tavily":
                assert request.headers["Authorization"] == "Bearer " + CANARY
                assert payload["max_results"] == 5
                assert payload["search_depth"] == "basic"
                assert payload["include_answer"] is False
                assert payload["include_raw_content"] is False
            else:
                assert request.headers["x-api-key"] == CANARY
                assert payload["numResults"] == 5
                assert payload["type"] == "auto"
        if case.get("exception") == "connect_timeout":
            raise httpx.ConnectTimeout("fixture connection timeout", request=request)
        if case.get("exception") == "read_timeout":
            raise httpx.ReadTimeout("fixture read timeout", request=request)
        if case.get("exception") == "secret_error":
            raise httpx.ReadError("fixture reflected credential " + CANARY, request=request)
        content = case["raw"].encode() if "raw" in case else json.dumps(case.get("body")).encode()
        headers = {
            "Content-Type": case.get("content_type", "application/json"),
            **case.get("headers", {}),
        }
        if case.get("gzip"):
            content = gzip.compress(content)
            headers["Content-Encoding"] = "gzip"
        return httpx.Response(
            case.get("status", 200), headers=headers, stream=httpx.ByteStream(content)
        )

    def transport(request):
        try:
            return transport_impl(request)
        except AssertionError as exc:
            wire_errors.append(str(exc))
            raise

    row: dict[str, Any] = {"provider": provider, "case": case["name"]}
    try:
        with ExitStack() as stack:
            stack.enter_context(patch("socket.getaddrinfo", side_effect=dns))
            stack.enter_context(patch("socket.socket.connect", no_socket))
            stack.enter_context(
                patch.object(
                    http_runtime,
                    "_SSRFGuardedHTTPTransport",
                    lambda: httpx.MockTransport(transport),
                )
            )
            stack.enter_context(
                patch(
                    "core.infrastructure.security.secret_codec.encrypt_secret",
                    return_value=CIPHERTEXT,
                )
            )
            stack.enter_context(
                patch(
                    "core.infrastructure.security.secret_codec.decrypt_secret", return_value=CANARY
                )
            )
            stack.enter_context(
                patch("core.application.orchestration.agents.common.emit_event", return_value=None)
            )
            stack.enter_context(
                patch(
                    "core.application.orchestration.agents.common.publish_agent_message",
                    return_value=True,
                )
            )
            service = WorkflowInterfaceService()
            interface = service.interface_from_definition(definition)
            admitted = service.validate_inputs(interface, initial_inputs, channel="parent_workflow")
            row["admission_ok"] = admitted.ok
            if case.get("admission_error"):
                assert not admitted.ok, "Invalid input was admitted"
                assert not requests
                row.update(
                    passed=True,
                    terminal="rejected_before_execution",
                    admission_errors=[e["code"] for e in admitted.errors],
                )
                return row
            assert admitted.ok, admitted.errors
            digest = definition_digest(definition)
            ref = {"snapshot_id": "offline-" + uuid.uuid4().hex, "revision": 1, "digest": digest}
            prime_run_definition(ref, definition)
            fingerprints = [inline_secret_ref_fingerprint(admitted.inputs["provider_credential"])]
            initial = build_initial_orchestrator_state(
                task_id="offline-search-fixture",
                task_summary="Offline search workflow verification",
                task_source="manual",
                project_name="offline",
                project_config={
                    "account_id": "offline-account",
                    "project_id": "offline-project",
                    "project_policy": {
                        "retry_backoff_initial_seconds": 0,
                        "retry_backoff_max_seconds": 0,
                    },
                },
                extra_state={
                    "workflow_id": definition["workflow_id"],
                    "runtime_schema_version": RUNTIME_SCHEMA_VERSION,
                    "workflow_definition_ref": ref,
                    "inputs": admitted.inputs,
                    "_workflow_secret_ref_fingerprints": fingerprints,
                },
            )
            compiled = build_graph()
            cfg = {"configurable": {"thread_id": uuid.uuid4().hex}, "recursion_limit": 150}
            final = compiled.invoke(initial, cfg)
            checkpoints = list(compiled.get_state_history(cfg))
            assert CANARY not in json.dumps(final, default=str), (
                "Plaintext fixture credential leaked to final state"
            )
            assert all(CANARY not in json.dumps(c.values, default=str) for c in checkpoints), (
                "Plaintext fixture credential leaked to a checkpoint"
            )
            assert not connects, "A real socket connection was attempted"
            assert not wire_errors, "Request contract assertion failed: " + "; ".join(wire_errors)
            expected = "completed" if case.get("success") else "failed"
            outputs = final.get("workflow_outputs") or {}
            row.update(
                actual_status=final.get("status"),
                expected_status=expected,
                terminal=final.get("workflow_terminal"),
                checkpoint_count=len(checkpoints),
                requests=len(requests),
                definition_digest=digest,
                trace=[
                    {
                        "step_id": m.get("step_id"),
                        "attempt": m.get("attempt"),
                        "status": m.get("status"),
                        "outcome": m.get("artifacts", {}).get("_outcome"),
                    }
                    for m in final.get("agent_messages", [])
                ],
            )
            assert final.get("status") == expected, (
                f"Expected {expected}, got {final.get('status')}: {final.get('final_report')}"
            )
            if case.get("success"):
                assert final["workflow_terminal"]["kind"] == "stop_success"
                assert outputs.get("ok") is True, "Final output contract rejected"
                assert outputs["outputs"]["response"] == case["body"], (
                    "Output response was lost or changed"
                )
                assert len(requests) == 1, "Successful run made extra requests"
                row["output_sha256"] = hashlib.sha256(
                    json.dumps(outputs["outputs"], sort_keys=True).encode()
                ).hexdigest()
            else:
                assert "response" not in outputs.get("outputs", {}), (
                    "Failure exposed a success output"
                )
            expected_requests = 0 if case.get("no_dispatch") else 1
            if provider == "serpapi" and case["name"] in {
                "rate_limited",
                "provider_unavailable",
                "connect_timeout",
                "read_timeout",
            }:
                expected_requests = 3
            assert len(requests) == expected_requests, "Unexpected dispatch/retry count"
            if case.get("no_dispatch"):
                assert not requests, "Rejected query reached HTTP transport"
            row["passed"] = True
    except Exception as exc:
        row.update(
            passed=False,
            error=str(exc).replace(CANARY, "[fixture credential]")[:2500],
            traceback=traceback.format_exc().replace(CANARY, "[fixture credential]")[-3500:],
            requests=len(requests),
        )
    finally:
        clear_run_definition_cache()
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--contracts", type=Path, required=True)
    parser.add_argument("--case", action="append", default=[], help="provider:case filter")
    parser.add_argument(
        "--fixture-public-ip", required=True, help="Offline IPv4 DNS fixture; no socket is opened"
    )
    args = parser.parse_args()
    try:
        fixture_address = ipaddress.IPv4Address(args.fixture_public_ip)
    except ipaddress.AddressValueError:
        parser.error("fixture-public-ip must be an IPv4 address")
    if not fixture_address.is_global:
        parser.error("fixture-public-ip must pass the real SSRF public-address check")
    contracts = json.loads(args.contracts.read_text())
    report: dict[str, Any] = {
        "schema_version": 1,
        "scope": "real compiled LangGraph; production hub, step nodes, retry node, output validation and MemorySaver; preselected pinned workflow",
        "network": "disabled by outer container; sockets forbidden in process",
        "mocked_boundaries": [
            "HTTP transport using real httpx.Client/MockTransport",
            "offline DNS for three known provider hosts",
            "inline fixture encrypt/decrypt",
            "external event/message sinks",
        ],
        "unperformed": [
            "provider API calls",
            "live key/account/quota verification",
            "UI routing and job admission",
            "broker/database persistence",
            "production deployment",
        ],
        "cases": [],
        "bundles": [],
    }
    for provider, contract in contracts["providers"].items():
        path = args.bundle_dir / f"catalog_search_{provider}.yaml"
        raw = path.read_bytes()
        parsed = WorkflowPortabilityService(object()).parse_bundle(raw.decode())
        definition = parsed["workflows"][0]
        issues = validate_workflow_definition(definition) + validate_workflow_step_config_keys(
            definition
        )
        report["bundles"].append(
            {
                "provider": provider,
                "path": path.name,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "structural_issues": issues,
            }
        )
        if issues:
            report["cases"].append(
                {
                    "provider": provider,
                    "case": "structural_validation",
                    "passed": False,
                    "issues": issues,
                }
            )
            continue
        for case in cases_for(provider, contract):
            if not args.case or f"{provider}:{case['name']}" in args.case:
                report["cases"].append(
                    verify_case(provider, definition, case, contract, str(fixture_address))
                )
    report["passed"] = sum(bool(row["passed"]) for row in report["cases"])
    report["failed"] = len(report["cases"]) - report["passed"]
    import inspect

    report["runtime_sources_sha256"] = {
        node.__name__: hashlib.sha256(Path(inspect.getfile(node)).read_bytes()).hexdigest()
        for node in (
            orchestrator_hub_node,
            execute_step_node,
            control_flow_step_node,
            orchestrator_validation_node,
            operation_retry_wait_node,
        )
    }
    report["fixture_retry_policy"] = {
        "retry_backoff_initial_seconds": 0,
        "retry_backoff_max_seconds": 0,
    }
    report["contracts_sha256"] = hashlib.sha256(args.contracts.read_bytes()).hexdigest()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
