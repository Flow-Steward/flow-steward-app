"""Qualified n8n HTTP Request translation and runtime object guard."""

from __future__ import annotations

from typing import Any

from .contracts import require
from .json_source import load_json
from .secrets import (
    _portable_reference,
    _sensitive_key,
)
from .target import edge, qualified_keys, step
from .values import CredentialInputs, literal_json, translate


def _parameters(
    raw: Any,
    incoming: str,
    path: str,
    credentials: CredentialInputs,
    *,
    headers: bool = False,
    body_values: bool = False,
) -> dict[str, Any]:
    require(isinstance(raw, dict), "source_invalid", path, "Parameters must be an object.")
    qualified_keys(raw, {"parameters"}, path)
    rows = raw.get("parameters", [])
    require(isinstance(rows, list), "source_invalid", path, "Parameter entries must be an array.")
    result = {}
    names: set[str] = set()
    for row in rows:
        require(
            isinstance(row, dict)
            and set(row) <= {"name", "value"}
            and isinstance(row.get("name"), str)
            and row["name"],
            "source_invalid",
            path,
            "A parameter needs a static name and value.",
        )
        name = row["name"]
        key = name.lower() if headers else name
        authorization = key == "authorization" or (
            headers and key.replace("-", "_").endswith("_authorization")
        )
        require(
            key not in names and "={{" not in name and "{{" not in name,
            "http_option_unsupported",
            path,
            "Dynamic or repeated parameter names need adaptation.",
        )
        names.add(key)
        raw_value = row.get("value", "")
        if _sensitive_key(name) and not body_values:
            if isinstance(raw_value, str) and raw_value.startswith("="):
                translated = translate(raw_value, incoming, path, template=True)
                result[name] = (
                    translated
                    if _portable_reference(translated)
                    else credentials.reference(translated, authorization=authorization)
                )
            else:
                result[name] = credentials.reference(raw_value, authorization=authorization)
        else:
            result[name] = translate(raw_value, incoming, path, template=not body_values)
    return result


def http_step(
    node: dict[str, Any],
    index: int,
    incoming: str,
    credentials: CredentialInputs,
    *,
    object_response_nodes: list[int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    path = f"nodes[{index}].parameters"
    p = node.get("parameters", {})
    qualified_keys(
        p,
        {
            "method",
            "url",
            "authentication",
            "sendQuery",
            "queryParameters",
            "specifyQuery",
            "sendHeaders",
            "headerParameters",
            "specifyHeaders",
            "sendBody",
            "contentType",
            "specifyBody",
            "bodyParameters",
            "jsonBody",
            "options",
        },
        path,
        code="http_option_unsupported",
    )
    require(
        p.get("authentication", "none") == "none",
        "credentials_require_mapping",
        path,
        "HTTP authentication requires target mapping.",
    )
    options = p.get("options", {})
    require(isinstance(options, dict), "source_invalid", path, "HTTP options must be an object.")
    qualified_keys(options, {"timeout"}, path + ".options", code="http_option_unsupported")
    require(
        index in object_response_nodes,
        "http_object_contract_required",
        path,
        "n8n HTTP JSON arrays expand into items; an object response contract is required.",
        "Verify the provider returns one JSON object; otherwise build explicit For each/item mappings. Never assert this from a node name alone.",
    )
    method = p.get("method", "GET")
    require(
        isinstance(method, str)
        and method in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"},
        "http_option_unsupported",
        path,
        "The HTTP method is not supported.",
    )
    raw_url = p.get("url")
    require(isinstance(raw_url, str), "source_invalid", path, "A URL is required.")
    # Expressions are translated before URL parsing, so unsupported JS is reported precisely.
    url_value = translate(raw_url, incoming, path + ".url", template=True)
    require(
        isinstance(url_value, str),
        "http_url_unsupported",
        path,
        "URL expressions must produce text.",
    )
    if url_value.startswith("{{ state.") and url_value.endswith(" }}"):
        url, query = url_value, {}
    else:
        url, query = credentials.url(url_value, path + ".url")
    for flag in ("sendQuery", "sendHeaders", "sendBody"):
        require(
            type(p.get(flag, False)) is bool,
            "source_invalid",
            path,
            "HTTP enable flags must be booleans.",
        )
    if p.get("sendQuery"):
        require(
            p.get("specifyQuery", "keypair") == "keypair",
            "http_option_unsupported",
            path,
            "This query encoding needs adaptation.",
        )
        explicit = _parameters(p.get("queryParameters", {}), incoming, path, credentials)
        require(
            not set(query).intersection(explicit),
            "http_option_unsupported",
            path,
            "Duplicate URL and explicit secret query fields need adaptation.",
        )
        query.update(explicit)
    else:
        require(
            not p.get("queryParameters"),
            "http_option_unsupported",
            path,
            "Inactive query settings need review.",
        )
    headers = {}
    if p.get("sendHeaders"):
        require(
            p.get("specifyHeaders", "keypair") == "keypair",
            "http_option_unsupported",
            path,
            "This header encoding needs adaptation.",
        )
        headers = _parameters(
            p.get("headerParameters", {}), incoming, path, credentials, headers=True
        )
    else:
        require(
            not p.get("headerParameters"),
            "http_option_unsupported",
            path,
            "Inactive headers need review.",
        )
    body = None
    if p.get("sendBody"):
        require(
            p.get("contentType", "json") == "json",
            "http_option_unsupported",
            path,
            "Only JSON request bodies are supported.",
        )
        if p.get("specifyBody", "keypair") == "json":
            raw = p.get("jsonBody")
            require(isinstance(raw, str), "source_invalid", path, "JSON body must be JSON text.")
            try:
                body = load_json(raw)
            except (ValueError, RecursionError):
                require(
                    False,
                    "expression_unsupported",
                    path,
                    "A JSON body expression needs adaptation.",
                )
            require(
                isinstance(body, (dict, list)),
                "http_option_unsupported",
                path,
                "Scalar JSON bodies need explicit target encoding.",
            )
            body = credentials.body(literal_json(body, path))
        else:
            require(
                p.get("specifyBody", "keypair") == "keypair",
                "http_option_unsupported",
                path,
                "This body encoding needs adaptation.",
            )
            body = credentials.body(
                _parameters(
                    p.get("bodyParameters", {}), incoming, path, credentials, body_values=True
                )
            )
        headers = {"Content-Type": "application/json", **headers}
    else:
        require(
            not p.get("bodyParameters") and not p.get("jsonBody"),
            "http_option_unsupported",
            path,
            "Inactive body settings need review.",
        )
    timeout = options.get("timeout", 300_000)
    require(
        type(timeout) in {int, float} and 0 < timeout <= 3_600_000,
        "http_option_unsupported",
        path,
        "A bounded HTTP timeout is required.",
    )
    base = f"n{index:04d}"
    target = f"state.n8n.{base}.response"
    config = {
        "method": method,
        "url": url,
        "headers": headers,
        "query_params": query,
        "auth_mode": "none",
        "auth_ref": "",
        "timeout_seconds": timeout / 1000,
        "expected_statuses": list(range(200, 300)),
        "response_mode": "inline",
        "response_target": target,
    }
    if body is not None:
        config["body_template"] = body
    request = step(base, "http_request", config)
    request["error_policy"] = {"on_error": "route_to_step"}
    # A violated object contract fails instead of silently treating an array as one item.
    shape = step(
        base + "_shape",
        "template",
        {
            "text": "{{ 'object' if response is mapping else 'invalid' }}",
            "format": "text",
            "inputs": [
                {
                    "name": "response",
                    "type": "json",
                    "required": True,
                    "source": target + ".response_body",
                }
            ],
            "result_target": f"state.n8n.{base}.shape",
        },
    )
    shape["error_policy"] = {"on_error": "route_to_step"}
    guard = step(
        base + "_object",
        "if",
        {
            "condition": {
                "selector": f"state.n8n.{base}.shape",
                "operator": "equals",
                "value": "object",
            }
        },
    )
    edges = [
        edge(base, "success", base + "_shape"),
        edge(base, "error", "failed"),
        edge(base + "_shape", "success", base + "_object"),
        edge(base + "_shape", "error", "failed"),
        edge(base + "_object", "false", "failed"),
    ]
    return [request, shape, guard], edges, target + ".response_body"
