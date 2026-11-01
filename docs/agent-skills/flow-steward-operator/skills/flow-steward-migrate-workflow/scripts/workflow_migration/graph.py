"""Validate n8n exports as connected, single-entry trees of main-flow edges."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .contracts import require
from .json_source import load_json

_BASE = "n8n-nodes-base."
VERSIONS = {
    "manualTrigger": {1},
    "noOp": {1},
    "set": {3, 3.1, 3.2, 3.3, 3.4},
    "httpRequest": {3, 4, 4.1, 4.2, 4.3},
    "if": {2, 2.1, 2.2},
}
_UI_KEYS = {
    "id",
    "name",
    "type",
    "typeVersion",
    "parameters",
    "position",
    "notes",
    "notesInFlow",
    "color",
    "webhookId",
}
_BEHAVIOR_DEFAULTS = {
    "disabled": False,
    "retryOnFail": False,
    "alwaysOutputData": False,
    "executeOnce": False,
    "continueOnFail": False,
    "onError": "stopWorkflow",
}


@dataclass
class SourceGraph:
    nodes: list[dict[str, Any]]
    order: list[int]
    links: dict[int, list[int | None]]
    parents: dict[int, int]
    fanouts: dict[int, list[int]]


def parse_graph(source_json: str, *, parallel_fanout: bool = False) -> SourceGraph:
    """Refuse branching fanout, joins, cycles, auxiliary edges and hidden node options."""
    require(
        isinstance(source_json, str) and len(source_json) <= 1_000_000,
        "source_invalid",
        "source_json",
        "Supply one bounded n8n JSON workflow.",
    )
    try:
        data = load_json(source_json)
    except (ValueError, RecursionError):
        data = None
    require(
        isinstance(data, dict),
        "source_invalid",
        "source_json",
        "Supply one n8n workflow JSON object.",
    )
    assert isinstance(data, dict)
    nodes = data.get("nodes")
    require(
        isinstance(nodes, list)
        and 1 <= len(nodes) <= 500
        and all(isinstance(n, dict) for n in nodes),
        "source_invalid",
        "nodes",
        "Supply between one and 500 nodes.",
    )
    assert isinstance(nodes, list)
    require(
        not data.get("pinData") and not data.get("staticData"),
        "source_state_unsupported",
        "source_json",
        "Pinned or persistent source state needs explicit adaptation.",
    )
    settings = data.get("settings") or {}
    require(
        isinstance(settings, dict)
        and not (set(settings) - {"executionOrder"})
        and settings.get("executionOrder", "v1") == "v1",
        "source_settings_unsupported",
        "settings",
        "Source execution settings need adaptation.",
    )
    names: dict[str, int] = {}
    starts: list[int] = []
    for i, node in enumerate(nodes):
        path = f"nodes[{i}]"
        name = node.get("name")
        require(
            isinstance(name, str) and 0 < len(name) <= 200 and name not in names,
            "source_invalid",
            path,
            "Node names must be nonempty and unique.",
        )
        names[name] = i
        require(
            not node.get("credentials"),
            "credentials_require_mapping",
            path + ".credentials",
            "Source credentials require target connection mapping.",
            "Discover the live operation and permitted connection; do not copy credential IDs, names or tokens.",
        )
        unknown = set(node) - _UI_KEYS - set(_BEHAVIOR_DEFAULTS) - {"credentials"}
        require(
            not unknown and all(node.get(k, v) == v for k, v in _BEHAVIOR_DEFAULTS.items()),
            "node_option_unsupported",
            path,
            "A node execution option needs explicit adaptation.",
        )
        kind = str(node.get("type", ""))
        key = kind.removeprefix(_BASE) if kind.startswith(_BASE) else ""
        require(
            key in VERSIONS,
            "node_unsupported",
            path + ".type",
            "This node needs an adapter or a verified operation mapping.",
            "Discover native operations first, then assess HTTP or a reusable child workflow; an unrecognized node does not establish a missing extension.",
        )
        version = node.get("typeVersion")
        require(
            type(version) in {int, float} and version in VERSIONS[key],
            "node_version_unsupported",
            path + ".typeVersion",
            "This node version has not been qualified.",
        )
        require(
            isinstance(node.get("parameters", {}), dict),
            "source_invalid",
            path + ".parameters",
            "Node parameters must be an object.",
        )
        if key == "manualTrigger":
            starts.append(i)
    require(
        len(starts) == 1,
        "trigger_unsupported",
        "nodes",
        "Exactly one Manual Trigger is supported.",
        "Map webhook, schedule and app triggers explicitly; manual invocation does not preserve them.",
    )
    raw = data.get("connections", {})
    require(
        isinstance(raw, dict), "source_invalid", "connections", "Connections must be an object."
    )
    links: dict[int, list[int | None]] = {}
    parents: dict[int, int] = {}
    fanouts: dict[int, list[int]] = {}
    for name, channels in raw.items():
        require(
            name in names and isinstance(channels, dict),
            "graph_unsupported",
            "connections",
            "A source connection is invalid.",
        )
        require(
            not (set(channels) - {"main"}),
            "connection_kind_unsupported",
            "connections",
            "Non-main AI or auxiliary connections need semantic adaptation.",
        )
        outputs = channels.get("main", [])
        index = names[name]
        is_if = nodes[index]["type"] == _BASE + "if"
        require(
            isinstance(outputs, list) and len(outputs) <= (2 if is_if else 1),
            "graph_unsupported",
            "connections",
            "Unexpected output channels need adaptation.",
        )
        links[index] = []
        for targets in outputs:
            require(
                isinstance(targets, list),
                "graph_unsupported",
                "connections",
                "Output targets must be an array.",
            )
            require(
                len(targets) <= 1 or (parallel_fanout and not is_if),
                "fanout_unsupported",
                "connections",
                "Source fan-out needs explicit execution-order mapping.",
                "Map independent branches to native Group Parallel Run with parallel_fanout=true; source v1 sequential branch ordering must not be changed implicitly.",
            )
            translated = []
            for connection in targets:
                require(
                    isinstance(connection, dict)
                    and set(connection) <= {"node", "type", "index"}
                    and isinstance(connection.get("node"), str)
                    and connection["node"] in names
                    and connection.get("type") == "main"
                    and type(connection.get("index")) is int
                    and connection["index"] == 0,
                    "graph_unsupported",
                    "connections",
                    "An edge or target input needs explicit adaptation.",
                )
                target = names[connection["node"]]
                require(
                    target not in parents and target != starts[0],
                    "graph_unsupported",
                    "connections",
                    "Joins, repeated incoming edges or cycles need explicit adaptation.",
                )
                parents[target] = index
                translated.append(target)
            if len(translated) > 1:
                fanouts[index] = translated
                links[index].append(None)
            else:
                links[index].append(translated[0] if translated else None)
    order: list[int] = []
    queue = [starts[0]]
    while queue:
        index = queue.pop(0)
        require(
            index not in order,
            "graph_unsupported",
            "connections",
            "Cyclic control flow needs explicit adaptation.",
        )
        order.append(index)
        queue.extend(t for t in links.get(index, []) if t is not None)
        queue.extend(fanouts.get(index, []))
    require(
        len(order) == len(nodes),
        "graph_unsupported",
        "connections",
        "Disconnected nodes or cycles need explicit adaptation.",
    )
    return SourceGraph(nodes, order, links, parents, fanouts)
