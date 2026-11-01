"""Qualified n8n Set and If control/data adapters."""

from __future__ import annotations

from typing import Any

from .contracts import require
from .secrets import _sensitive_key
from .target import edge, qualified_keys, step
from .values import (
    CredentialInputs,
    contains_source,
    field_path,
    translate,
)


def set_steps(
    node: dict[str, Any], index: int, incoming: str, credentials: CredentialInputs
) -> tuple[list[dict[str, Any]], str]:
    p = node.get("parameters", {})
    path = f"nodes[{index}].parameters"
    qualified_keys(p, {"assignments", "includeOtherFields", "options", "mode"}, path)
    require(
        p.get("mode", "manual") == "manual"
        and p.get("options", {}) == {}
        and type(p.get("includeOtherFields", False)) is bool,
        "node_option_unsupported",
        path,
        "Set options need explicit adaptation.",
    )
    raw = p.get("assignments", {})
    require(
        isinstance(raw, dict)
        and set(raw) <= {"assignments"}
        and isinstance(raw.get("assignments", []), list),
        "source_invalid",
        path,
        "Set assignments must be an array.",
    )
    base = f"n{index:04d}"
    target = f"state.n8n.{base}.item"
    assignments: dict[str, Any] = {}
    for row in raw.get("assignments", []):
        require(
            isinstance(row, dict) and set(row) <= {"id", "name", "value", "type"},
            "source_invalid",
            path,
            "A Set assignment is invalid.",
        )
        name = field_path(row.get("name"), path)
        require(
            target + "." + name not in assignments,
            "field_unsupported",
            path,
            "Repeated field assignments need adaptation.",
        )
        raw_value = row.get("value")
        declared = row.get("type")
        matching = {
            "string": isinstance(raw_value, str) and not raw_value.startswith("="),
            "number": type(raw_value) in {int, float},
            "boolean": type(raw_value) is bool,
            "object": isinstance(raw_value, dict),
            "array": isinstance(raw_value, list),
        }
        require(
            isinstance(declared, str) and matching.get(declared, False),
            "set_type_unsupported",
            path,
            "Typed Set coercion or dynamic typing needs explicit adaptation.",
        )
        value = translate(raw_value, incoming, path, template=False)
        if _sensitive_key(name):
            require(
                isinstance(raw_value, str),
                "credential_field_ambiguous",
                path,
                "A credential-like field has an unqualified non-text value.",
            )
            credentials.reference(raw_value)
            value = {"source": "state.inputs." + credentials.fields[-1]["name"]}
        elif isinstance(value, (dict, list)):
            require(
                not _nested_sensitive(value),
                "credential_field_ambiguous",
                path,
                "Nested credential fields need explicit target mappings.",
            )
        # set_variable resolves only top-level source records, never nested ones.
        require(
            not isinstance(row.get("value"), (dict, list)) or not contains_source(value),
            "expression_unsupported",
            path,
            "Nested Set expressions need explicit field assignments.",
        )
        assignments[target + "." + name] = value
    initial = {"source": incoming} if p.get("includeOtherFields", False) else {}
    steps = [step(base, "set_variable", {"target": target, "value": initial})]
    if assignments:
        steps.append(step(base + "_fields", "set_variable", {"assignments": assignments}))
    return steps, target


def if_step(node: dict[str, Any], index: int, incoming: str) -> dict[str, Any]:
    p = node.get("parameters", {})
    path = f"nodes[{index}].parameters"
    qualified_keys(p, {"conditions", "options"}, path)
    require(
        p.get("options", {}) == {}, "node_option_unsupported", path, "If options need adaptation."
    )
    c = p.get("conditions", {})
    require(
        isinstance(c, dict)
        and set(c) <= {"options", "conditions", "combinator"}
        and c.get("combinator", "and") == "and",
        "node_option_unsupported",
        path,
        "Combined If logic needs adaptation.",
    )
    rows = c.get("conditions")
    require(
        isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict),
        "node_option_unsupported",
        path,
        "Exactly one If comparison is supported.",
    )
    options = c.get("options", {})
    require(
        isinstance(options, dict)
        and set(options) <= {"caseSensitive", "leftValue", "typeValidation", "version"}
        and options.get("caseSensitive", True) is True
        and options.get("typeValidation", "strict") == "strict",
        "node_option_unsupported",
        path,
        "Case or type coercion semantics need adaptation.",
    )
    row = rows[0]
    require(
        set(row) <= {"id", "leftValue", "rightValue", "operator"},
        "node_option_unsupported",
        path,
        "An If condition field needs adaptation.",
    )
    op = row.get("operator", {})
    require(
        isinstance(op, dict)
        and set(op) <= {"type", "operation"}
        and op.get("type") == "string"
        and isinstance(op.get("operation"), str)
        and op.get("operation") in {"equals", "notEquals"}
        and isinstance(row.get("rightValue"), str)
        and not row["rightValue"].startswith("="),
        "node_option_unsupported",
        path,
        "Only static, case-sensitive string equality is supported.",
    )
    left = translate(row.get("leftValue"), incoming, path, template=False)
    require(
        isinstance(left, dict) and set(left) == {"source"},
        "expression_unsupported",
        path,
        "The If selector must be a simple incoming-item path.",
    )
    require(
        not _sensitive_key(left["source"]),
        "credential_comparison_unsupported",
        path,
        "Comparing a credential requires an explicit safe target policy; literal comparison values are not portable.",
    )
    return step(
        f"n{index:04d}",
        "if",
        {
            "condition": {
                "selector": left["source"],
                "operator": "equals" if op["operation"] == "equals" else "not_equals",
                "value": row["rightValue"],
            }
        },
    )


def _nested_sensitive(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_sensitive_key(k) or _nested_sensitive(v) for k, v in value.items())
    return isinstance(value, list) and any(_nested_sensitive(v) for v in value)


def if_steps(
    node: dict[str, Any], index: int, incoming: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Preserve strict source string validation before evaluating equality."""
    comparison = if_step(node, index, incoming)
    base = comparison["step_id"]
    selector = comparison["override"]["if"]["condition"]["selector"]
    comparison["step_id"] = base + "_condition"
    target = f"state.n8n.{base}.input_type"
    shape = step(
        base,
        "template",
        {
            "text": "{{ 'string' if value is string else 'invalid' }}",
            "format": "text",
            "inputs": [{"name": "value", "type": "json", "required": True, "source": selector}],
            "result_target": target,
        },
    )
    shape["error_policy"] = {"on_error": "route_to_step"}
    guard = step(
        base + "_typed",
        "if",
        {"condition": {"selector": target, "operator": "equals", "value": "string"}},
    )
    links = [
        edge(base, "success", base + "_typed"),
        edge(base, "error", "failed"),
        edge(base + "_typed", "true", base + "_condition"),
        edge(base + "_typed", "false", "failed"),
    ]
    return [shape, guard, comparison], links
