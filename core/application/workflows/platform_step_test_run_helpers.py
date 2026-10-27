"""Step test-run preflight and lookup helpers."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from fastapi import HTTPException

from core.application.workflows.platform_test_run_common import (
    STATE_PREFIX,
    _as_dict,
    _text,
    redact_test_run_payload,
)
from core.application.workflows.workflow_step_kinds import STEP_KIND_AGENT, normalize_step_kind

_STATE_SELECTOR_RE = re.compile(r"^\s*+state\.[^|]++$")
_TEMPLATE_SELECTOR_RE = re.compile(r"\{\{\s*+([^{}]++)\}\}")


def _state_selector_without_default(value: Any) -> str:
    text = str(value or "").strip()
    if not text or not _STATE_SELECTOR_RE.match(text):
        return ""
    return text


def _extract_unresolved_template_selectors(
    raw_value: Any,
    rendered_value: Any,
    *,
    field_path: str,
) -> list[dict[str, str]]:
    if isinstance(raw_value, dict) and isinstance(rendered_value, dict):
        issues: list[dict[str, str]] = []
        for key, child in raw_value.items():
            issues.extend(
                _extract_unresolved_template_selectors(
                    child,
                    rendered_value.get(key),
                    field_path=f"{field_path}.{key}" if field_path else str(key),
                )
            )
        return issues
    if isinstance(raw_value, list) and isinstance(rendered_value, list):
        issues: list[dict[str, str]] = []
        for index, child in enumerate(raw_value):
            rendered_child = rendered_value[index] if index < len(rendered_value) else None
            issues.extend(
                _extract_unresolved_template_selectors(
                    child,
                    rendered_child,
                    field_path=f"{field_path}[{index}]",
                )
            )
        return issues
    if not isinstance(raw_value, str):
        return []
    if rendered_value not in ("", None):
        return []
    issues: list[dict[str, str]] = []
    for match in _TEMPLATE_SELECTOR_RE.finditer(raw_value):
        selector = str(match.group(1) or "").strip()
        if not selector or "|" in selector or not selector.startswith(STATE_PREFIX):
            continue
        issues.append(
            {
                "field": field_path or "value",
                "source": selector,
            }
        )
    return issues


def _fail_step_test_run(
    *,
    workflow_id: str,
    step: dict[str, Any],
    step_kind: str,
    summary: str,
    error: str,
    input_payload: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    return {
        "workflow_id": workflow_id,
        "step_id": _text(step.get("step_id")),
        "step_kind": step_kind,
        "status": "fail",
        "summary": summary,
        "error": error,
        "inputs": redact_test_run_payload(input_payload),
        "result": redact_test_run_payload(result),
        "state_patch": None,
        "final_status": "fail",
        "final_report": summary,
        "workflow_outputs": {},
    }


def _preflight_step_test_run(
    *,
    state: Mapping[str, Any],
    step: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    state_dict = dict(state)
    step_kind = normalize_step_kind(step.get("step_kind"), default=STEP_KIND_AGENT)
    if step_kind == "connector":
        from core.application.orchestration.connector_mapping import (
            _render_input_payload,
            _to_object,
        )

        override = _to_object(step.get("override"))
        connector_override = _to_object(override.get("connector"))
        raw_mapping = (
            _to_object(step.get("input_mapping_jsonb"))
            or _to_object(connector_override.get("input_mapping_jsonb"))
            or _to_object(connector_override.get("input_mapping"))
        )
        # The runtime reports which mappings it could not resolve, so preflight
        # and the live step agree on what "missing" means instead of each
        # deciding for itself.
        rendered, unresolved_sources = _render_input_payload(raw_mapping, state_dict)
        unresolved: list[dict[str, str]] = [
            {"field": str(field), "source": str(source)}
            for field, source in unresolved_sources.items()
        ]
        for key, raw_value in raw_mapping.items():
            selector = _state_selector_without_default(raw_value)
            if selector and str(key) not in unresolved_sources and rendered.get(key) in (None, ""):
                unresolved.append({"field": str(key), "source": selector})
        if unresolved:
            summary = "Missing mapped input" if len(unresolved) == 1 else "Missing mapped inputs"
            missing = ", ".join(f"{item['field']} from {item['source']}" for item in unresolved)
            return rendered, {
                "summary": f"{summary}: {missing}",
                "error": "missing_mapped_inputs",
                "result": {
                    "missing_inputs": unresolved,
                    "input_payload": rendered,
                },
            }
        return rendered, None

    if step_kind == "http_request":
        from core.application.orchestration.http_request_step_runtime import (
            _render_value,
            _request_body_template,
        )

        override = _as_dict(step.get("override"))
        config = _as_dict(override.get("http_request"))
        rendered_method = str(
            _render_value(config.get("method") or "GET", state_dict) or ""
        ).strip()
        rendered_url = str(_render_value(config.get("url") or "", state_dict) or "").strip()
        rendered_query = _render_value(config.get("query_params") or {}, state_dict)
        rendered_headers = _render_value(config.get("headers") or {}, state_dict)
        body_template = _request_body_template(config)
        rendered_body = _render_value(body_template, state_dict)
        unresolved = []
        unresolved.extend(
            _extract_unresolved_template_selectors(
                config.get("method") or "GET",
                rendered_method,
                field_path="method",
            )
        )
        unresolved.extend(
            _extract_unresolved_template_selectors(
                config.get("url") or "",
                rendered_url,
                field_path="url",
            )
        )
        unresolved.extend(
            _extract_unresolved_template_selectors(
                config.get("query_params") or {},
                rendered_query,
                field_path="query_params",
            )
        )
        unresolved.extend(
            _extract_unresolved_template_selectors(
                config.get("headers") or {},
                rendered_headers,
                field_path="headers",
            )
        )
        unresolved.extend(
            _extract_unresolved_template_selectors(
                body_template,
                rendered_body,
                field_path="body",
            )
        )
        input_payload = {
            "method": rendered_method.upper() or "GET",
            "url": rendered_url,
            "query_params": rendered_query if isinstance(rendered_query, dict) else {},
            "headers": rendered_headers if isinstance(rendered_headers, dict) else {},
            "body": rendered_body,
        }
        if unresolved:
            summary = (
                "Missing runtime request context"
                if len(unresolved) == 1
                else "Missing runtime request context fields"
            )
            missing = ", ".join(f"{item['field']} from {item['source']}" for item in unresolved)
            return input_payload, {
                "summary": f"{summary}: {missing}",
                "error": "missing_runtime_request_context",
                "result": {
                    "missing_inputs": unresolved,
                    "request": input_payload,
                },
            }
        return input_payload, None

    if step_kind == "dataset_query":
        override = _as_dict(step.get("override"))
        config = _as_dict(override.get("dataset_query"))
        input_payload = {
            "query_template_id": config.get("query_template_id") or step.get("query_template_id"),
            "result_target": config.get("result_target") or step.get("result_target"),
            "row_limit": config.get("row_limit") or step.get("row_limit"),
            "timeout_seconds": config.get("timeout_seconds") or step.get("timeout_seconds"),
            "params": config.get("params")
            or config.get("input_params")
            or step.get("params")
            or {},
        }
        return redact_test_run_payload(input_payload), None

    if step_kind == "transform":
        from core.application.orchestration.step_templating import _resolve_path

        override = _as_dict(step.get("override"))
        config = _as_dict(override.get("transform"))
        input_cfg = _as_dict(config.get("input"))
        source = _text(input_cfg.get("source"))
        input_payload = {
            "input_source": source,
            "source_value": _resolve_path(state_dict, source) if source else None,
            "mode": config.get("mode") or "object",
            "result_target": config.get("result_target"),
        }
        return redact_test_run_payload(input_payload), None

    return dict((state.get("task_envelope") or {}).get("inputs") or {}), None


def resolve_step_for_test_run(
    db,
    *,
    workflow_id: str,
    step_id: str,
    workflow: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The workflow-owned step a step test run executes, looked up by its id."""
    workflow = workflow if isinstance(workflow, dict) else db.get_workflow(workflow_id)
    if not isinstance(workflow, dict):
        raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found")
    steps = workflow.get("steps")
    target_step_id = _text(step_id)
    for step in steps if isinstance(steps, list) else []:
        if (
            isinstance(step, dict)
            and target_step_id
            and _text(step.get("step_id")) == target_step_id
        ):
            return dict(step)
    raise HTTPException(
        status_code=404,
        detail=f"Step '{step_id}' not found in workflow '{workflow_id}'",
    )


def scope_workflow_validation_for_step(
    validation: Mapping[str, Any],
    *,
    step_id: str,
    selected_registry_tool_issues: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Keep only readiness issues that can prevent the selected step from running.

    Whole-workflow readiness deliberately includes terminal, topology, output,
    and every connector/agent dependency. A one-step test run has a narrower
    contract: the workflow and target must be structurally readable, workflow
    inputs must be usable, and the selected step itself must be ready. Filtering
    by the canonical validation path (``workflow.steps.<step_id>``) keeps that
    boundary independent of any connector provider or operation name.
    """

    target_step_id = _text(step_id)
    step_path = f"workflow.steps.{target_step_id}"
    selected_tool_issue_keys = {
        (_text(issue.get("tool_id")), _text(issue.get("usage")))
        for issue in selected_registry_tool_issues
        if isinstance(issue, Mapping)
    }
    structural_codes = {
        "workflow.not_found",
        "workflow.steps_missing",
        "workflow.step_invalid_type",
    }

    def relevant(issue: Any) -> bool:
        if not isinstance(issue, Mapping):
            return False
        code = _text(issue.get("code"))
        path = _text(issue.get("path"))
        issue_step_id = _text(issue.get("step_id"))
        if code in structural_codes:
            return True
        if target_step_id and (path == step_path or path.startswith(f"{step_path}.")):
            return True
        if path.startswith("workflow.settings.callable.expected_inputs"):
            return True
        if code == "workflow.interface.invalid_callable_shape":
            return True
        if code in {
            "workflow.step_connector_tool_unavailable",
            "workflow.agent_tool_unavailable",
        }:
            return (_text(issue.get("tool_id")), _text(issue.get("usage"))) in (
                selected_tool_issue_keys
            )
        return bool(target_step_id and issue_step_id == target_step_id)

    errors = [dict(issue) for issue in validation.get("errors") or [] if relevant(issue)]
    warnings = [dict(issue) for issue in validation.get("warnings") or [] if relevant(issue)]
    return {"valid": not errors, "errors": errors, "warnings": warnings}
