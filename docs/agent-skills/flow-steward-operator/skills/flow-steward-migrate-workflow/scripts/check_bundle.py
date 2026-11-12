#!/usr/bin/env python3
"""Review local bundle authoring hazards offline; target validation is still required."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

_MESSAGES = {
    "bundle_shape_invalid": "Expected a workflow_bundle v2 object with workflow, step and execution_graph.edges lists.",
    "duplicate_source_outcome": "A source has multiple edges for the same outcome; make routing explicit.",
    "continue_has_outgoing_edge": "Continue is terminal within its loop branch and cannot have outgoing edges.",
    "transform_bare_state_source": "Transform input.source needs a state.* field selector, not bare state.",
    "skill_input_source_unsupported": "Portable skill expected_inputs cannot declare source; map inputs on the agent step.",
    "connector_output_numeric_segment": "Verify the output type for this numeric selector segment: object keys can be numeric. If this is a list index, the audited runtime requires mapping whole rows, then using SetVariable; no output shape was verified.",
    "agent_messages_runtime_collision": "A required business input named messages maps from another state field; the audited runtime also owns messages. Review the collision and rename the business input if needed.",
}


def _object(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _rows(value: Any) -> list:
    return value if isinstance(value, list) else []


def _mapping(step: dict, kind: str, direction: str) -> dict:
    override = _object(_object(step.get("override")).get(kind))
    for value in (
        step.get(f"{direction}_mapping_jsonb"),
        override.get(f"{direction}_mapping_jsonb"),
        override.get(f"{direction}_mapping"),
    ):
        # Agent input materialization preserves an explicitly empty step mapping;
        # connector output projection instead falls through empty mappings.
        if isinstance(value, dict) and (kind == "agent" or value):
            return value
    return {}


def _check_document_bounds(root: Any) -> None:
    """Bound a YAML object DAG iteratively while permitting shared acyclic aliases."""
    if not isinstance(root, (dict, list)):
        return
    stack = [(root, False)]
    active: set[int] = set()
    heights: dict[int, int] = {}
    links = 0
    while stack:
        value, exiting = stack.pop()
        identity = id(value)
        children = value.values() if isinstance(value, dict) else value
        if exiting:
            height = 1 + max(
                (heights[id(child)] for child in children if isinstance(child, (dict, list))),
                default=0,
            )
            if height > 64:
                raise ValueError
            heights[identity] = height
            active.remove(identity)
            continue
        if identity in active:
            raise ValueError
        if identity in heights:
            continue
        if len(active) + len(heights) >= 100_000:
            raise ValueError
        links += len(value)
        if links > 200_000:
            raise ValueError
        active.add(identity)
        stack.append((value, True))
        stack.extend((child, False) for child in children if isinstance(child, (dict, list)))


def _check_review_budget(bundle: Any) -> None:
    """Charge repeated alias uses before nested authoring analysis expands them."""
    remaining = 20_000
    selector_characters = 2_000_000

    def spend(count: int) -> None:
        nonlocal remaining
        remaining -= count
        if remaining < 0:
            raise ValueError

    root = _object(bundle)
    workflows = _rows(root.get("workflows"))
    spend(len(workflows))
    for workflow in workflows:
        workflow = _object(workflow)
        steps = _rows(workflow.get("steps"))
        edges = _rows(_object(workflow.get("execution_graph")).get("edges"))
        spend(len(steps) + len(edges))
        for step in steps:
            step = _object(step)
            if step.get("step_kind") == "connector":
                mapping = _mapping(step, "connector", "output")
                spend(len(mapping))
                for source in mapping:
                    if isinstance(source, str):
                        selector_characters -= len(source)
                        if selector_characters < 0:
                            raise ValueError
    definitions = _object(_object(root.get("metadata")).get("authoring_definitions"))
    for owner in (_object(root.get("definitions")), definitions):
        skills = _rows(owner.get("skills"))
        spend(len(skills))
        for skill in skills:
            spend(len(_rows(_object(skill).get("expected_inputs"))))


def review_bundle(bundle: Any) -> dict[str, Any]:
    """Return fixed, value-free diagnostics for the audited authoring profile only."""
    diagnostics: list[dict[str, str]] = []

    def add(code: str, path: str, severity: str = "error") -> None:
        diagnostics.append(
            {"code": code, "severity": severity, "path": path, "message": _MESSAGES[code]}
        )

    root = _object(bundle)
    workflows = root.get("workflows")
    if (
        root.get("kind") != "workflow_bundle"
        or root.get("schema_version") != "platform.workflow_bundle.v2"
        or not isinstance(workflows, list)
        or not workflows
        or any(
            not isinstance(workflow, dict)
            or not isinstance(workflow.get("steps"), list)
            or not isinstance(_object(workflow.get("execution_graph")).get("edges"), list)
            or any(
                not isinstance(row, dict)
                for row in workflow["steps"] + workflow["execution_graph"]["edges"]
            )
            for workflow in workflows
        )
    ):
        add("bundle_shape_invalid", "bundle")
        workflows = []

    definitions = _object(_object(root.get("metadata")).get("authoring_definitions"))
    required_message_skills: set[str] = set()
    for owner, prefix in [
        (_object(root.get("definitions")), "definitions"),
        (definitions, "metadata.authoring_definitions"),
    ]:
        for index, value in enumerate(_rows(owner.get("skills"))):
            skill = _object(value)
            for field_index, field in enumerate(_rows(skill.get("expected_inputs"))):
                field = _object(field)
                if "source" in field:
                    add(
                        "skill_input_source_unsupported",
                        f"{prefix}.skills[{index}].expected_inputs[{field_index}]",
                    )
                skill_id = skill.get("skill_id")
                if (
                    isinstance(skill_id, str)
                    and field.get("name") == "messages"
                    and field.get("required") is True
                ):
                    required_message_skills.add(skill_id)

    for workflow_index, workflow in enumerate(workflows):
        path = f"workflows[{workflow_index}]"
        continue_ids = {
            step["step_id"]
            for step in workflow["steps"]
            if step.get("step_kind") == "continue" and isinstance(step.get("step_id"), str)
        }
        seen: set[tuple[str, str, str]] = set()
        for edge_index, edge in enumerate(workflow["execution_graph"]["edges"]):
            source = _object(edge.get("source"))
            kind, identifier = source.get("kind"), source.get("id")
            outcome = _object(edge.get("outcome")).get("name")
            edge_path = f"{path}.execution_graph.edges[{edge_index}]"
            if all(isinstance(part, str) for part in (kind, identifier, outcome)):
                key = (kind, identifier, outcome)
                if key in seen:
                    add("duplicate_source_outcome", edge_path)
                seen.add(key)
            if kind == "step" and isinstance(identifier, str) and identifier in continue_ids:
                add("continue_has_outgoing_edge", edge_path)
        for step_index, step in enumerate(workflow["steps"]):
            step_path = f"{path}.steps[{step_index}]"
            kind = step.get("step_kind")
            if kind == "transform":
                transform = _object(_object(step.get("override")).get("transform"))
                if _object(transform.get("input")).get("source") == "state":
                    add("transform_bare_state_source", f"{step_path}.override.transform.input")
            if kind == "connector":
                for mapping_index, source in enumerate(_mapping(step, "connector", "output")):
                    if isinstance(source, str) and any(
                        part.isdecimal() for part in source.split(".")
                    ):
                        add(
                            "connector_output_numeric_segment",
                            f"{step_path}.output_mapping[{mapping_index}]",
                            "warning",
                        )
            if kind == "agent":
                skill_id = step.get("skill_id")
                source = _mapping(step, "agent", "input").get("messages")
                if (
                    isinstance(skill_id, str)
                    and skill_id in required_message_skills
                    and isinstance(source, str)
                    and source.startswith("state.")
                    and source != "state.messages"
                ):
                    add("agent_messages_runtime_collision", f"{step_path}.input_mapping", "warning")

    return {
        "status": "blocked" if any(row["severity"] == "error" for row in diagnostics) else "passed",
        "runtime_verified": False,
        "target_validation": "required",
        "compatibility_profile": {"audited_target": "target21601831", "core_version": "0.9.9"},
        "diagnostics": diagnostics,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--report", type=Path)
    arguments = parser.parse_args()
    report = arguments.report
    if report and (report.exists() or report.is_symlink() or not report.parent.is_dir()):
        parser.exit(2, "Use a new report file in an existing directory.\n")
    try:
        import yaml
    except ModuleNotFoundError:
        parser.exit(2, "The preflight requires PyYAML 6.x in this Python environment.\n")
    try:
        with arguments.source.open("rb") as stream:
            raw = stream.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError
        source = raw.decode("utf-8")
        bundle = yaml.safe_load(source)
        _check_document_bounds(bundle)
        _check_review_budget(bundle)
    except (OSError, UnicodeError, ValueError, RecursionError, yaml.YAMLError):
        parser.exit(2, "Cannot parse a bounded UTF-8 YAML bundle.\n")
    details = review_bundle(bundle)
    text = json.dumps(details, ensure_ascii=False, indent=2) + "\n"
    try:
        if report:
            descriptor = os.open(report, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(text)
        else:
            print(text, end="")
    except OSError:
        parser.exit(
            2, "Could not create a new report file; inspect the destination before retrying.\n"
        )
    return 0 if details["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
