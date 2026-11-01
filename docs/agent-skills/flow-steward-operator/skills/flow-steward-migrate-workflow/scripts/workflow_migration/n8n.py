"""Deterministic, read-only n8n export to disabled v2 bundle conversion."""

from __future__ import annotations

import re
from itertools import pairwise
from typing import Any

import yaml

from .adapters import if_steps, set_steps
from .contracts import (
    ConversionBlocked,
    ConversionResult,
    require,
)
from .graph import parse_graph
from .http import http_step
from .parallel import (
    completion_edges,
    lane_http_steps,
    parallel_layout,
)
from .target import edge, step
from .values import CredentialInputs

SCHEMA_VERSION = "platform.workflow_bundle.v2"
BUNDLE_KIND = "workflow_bundle"


def convert_n8n(
    source_json: str,
    *,
    workflow_id: str,
    object_response_nodes: list[int] | None = None,
    parallel_fanout: bool = False,
) -> ConversionResult:
    """Convert a qualified subset, returning no bundle whenever semantics are unresolved.

    object_response_nodes uses zero-based source node indexes backed by an
    inspected provider response contract. It is not inferred from saved test data.
    Generated guards refuse a response outside that contract at runtime.
    """
    credentials = CredentialInputs()
    result: ConversionResult = {
        "status": "blocked",
        "source_format": "n8n.workflow.json",
        "bundle_yaml": None,
        "runtime_verified": False,
        "target_validation": "required",
        "issues": [],
        "node_mappings": [],
        "credential_inputs": [],
    }
    try:
        require(
            isinstance(workflow_id, str)
            and len(workflow_id) <= 120
            and re.fullmatch(r"[a-z][a-z0-9_]*", workflow_id),
            "target_id_invalid",
            "workflow_id",
            "Use a stable lowercase workflow identifier.",
        )
        require(
            type(parallel_fanout) is bool,
            "parallel_mode_invalid",
            "parallel_fanout",
            "Parallel fan-out must be selected explicitly with a boolean.",
        )
        graph = parse_graph(source_json, parallel_fanout=parallel_fanout)
        layout = parallel_layout(graph)
        confirmed = object_response_nodes or []
        require(
            isinstance(confirmed, list)
            and all(
                type(i) is int
                and i in range(len(graph.nodes))
                and graph.nodes[i]["type"] == "n8n-nodes-base.httpRequest"
                for i in confirmed
            ),
            "object_contract_invalid",
            "object_response_nodes",
            "Object-response indexes must name HTTP nodes in this export.",
        )
        steps: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        items: dict[int, str] = {}
        ends: dict[int, str] = {}
        end_outcomes: dict[int, list[str]] = {}
        failures: list[ConversionBlocked] = []
        for index in graph.order:
            node = graph.nodes[index]
            base = f"n{index:04d}"
            kind = node["type"].removeprefix("n8n-nodes-base.")
            parent = graph.parents.get(index)
            incoming = items[parent] if parent is not None else "state.inputs"
            try:
                if kind == "httpRequest":
                    expanded, internal, item = http_step(
                        node, index, incoming, credentials, object_response_nodes=confirmed
                    )
                    if index in layout.membership:
                        expanded, internal = lane_http_steps(expanded)
                    edges.extend(internal)
                    outcomes = ["success"] if index in layout.membership else ["true"]
                elif kind == "set":
                    expanded, item = set_steps(node, index, incoming, credentials)
                    edges.extend(
                        edge(a["step_id"], "success", b["step_id"]) for a, b in pairwise(expanded)
                    )
                    outcomes = ["success"]
                elif kind == "if":
                    expanded, internal = if_steps(node, index, incoming)
                    edges.extend(internal)
                    item, outcomes = incoming, ["true", "false"]
                else:
                    require(
                        node.get("parameters", {}) == {},
                        "node_option_unsupported",
                        f"nodes[{index}].parameters",
                        "Trigger or No Op parameters need adaptation.",
                    )
                    item = f"state.n8n.{base}.item"
                    value = {} if kind == "manualTrigger" else {"source": incoming}
                    expanded, outcomes = (
                        [step(base, "set_variable", {"target": item, "value": value})],
                        ["success"],
                    )
                if index in layout.membership:
                    for member in expanded:
                        member.update(layout.membership[index])
                steps.extend(expanded)
                ends[index] = expanded[-1]["step_id"]
                items[index] = item
                end_outcomes[index] = outcomes
                result["node_mappings"].append(
                    {
                        "source_index": index,
                        "source_type": node["type"],
                        "target_step_ids": [s["step_id"] for s in expanded],
                        "status": "converted",
                    }
                )
            except ConversionBlocked as exc:
                if exc.issue["path"] == "parameters":
                    exc.issue["path"] = f"nodes[{index}].parameters"
                failures.append(exc)
                # Continue inspection using a non-emitted alias; no partial bundle is returned.
                items[index] = incoming
        if failures:
            result["issues"] = [f.issue for f in failures]
            return result
        edges.extend(completion_edges(graph, layout, ends, end_outcomes))
        steps.append(step("done", "stop_success", {"message": "Complete"}))
        if any(e["target"].get("id") == "failed" for e in edges):
            steps.append(
                step("failed", "stop_fail", {"message": "HTTP request or response contract failed"})
            )
        workflow = {
            "workflow_id": workflow_id,
            "display_name": workflow_id,
            "version": "0.1.0",
            "enabled": False,
            "trigger": {},
            "settings": {
                "callable": {
                    "enabled": True,
                    "input_mode": "fields",
                    "strict_inputs": True,
                    "trigger_channels": ["ui", "api", "mcp", "parent_workflow"],
                    "expected_inputs": credentials.fields,
                }
            },
            "participants": [],
            "tags": ["migration", "n8n"],
            "steps": steps,
            "groups": layout.groups,
            "execution_graph": {
                "schema_version": 1,
                "entry": {"kind": "step", "id": f"n{graph.order[0]:04d}"},
                "edges": edges,
            },
        }
        bundle = {
            "schema_version": SCHEMA_VERSION,
            "kind": BUNDLE_KIND,
            "metadata": {"source_format": "n8n.workflow.json"},
            "workflows": [workflow],
            "dependencies": {},
            "import_hints": {"default_enabled": False, "import_state": "draft"},
        }
        if layout.groups:
            result["issues"].append(
                {
                    "code": "n8n.parallel_fanout",
                    "path": "connections",
                    "message": "Independent source branches were explicitly adapted to native Parallel Run groups.",
                    "severity": "warning",
                    "blocking": False,
                    "repair_guidance": "Verify branch independence and source ordering/failure differences before live execution.",
                }
            )
        result["status"] = "converted"
        result["bundle_yaml"] = yaml.safe_dump(bundle, sort_keys=False)
        result["credential_inputs"] = credentials.fields
    except ConversionBlocked as exc:
        result["issues"].append(exc.issue)
    return result
