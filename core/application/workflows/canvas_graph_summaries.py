from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from core.application.workflows.canvas_graph_common import (
    _empty_scope_summary,
    _to_object,
)
from core.application.workflows.reaction_scopes import split_scoped_reaction_key

_STEP_PATH_RE = re.compile(r"workflow\.steps\.([^.\[]+)(?:\.(.+))?$")
_GROUP_PATH_RE = re.compile(r"workflow\.groups\.([^.\[]+)(?:\.(.+))?$")
_FOR_EACH_PATH_RE = re.compile(r"workflow\.for_each\.([^.\[]+)(?:\.(.+))?$")


def _build_validation_issues(
    workflow: dict[str, Any], validation: dict[str, Any]
) -> list[dict[str, Any]]:
    step_ids = {
        str(step.get("step_id") or "").strip()
        for step in workflow.get("steps") or []
        if isinstance(step, dict)
    }
    grouped: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for bucket_name, severity in (("errors", "error"), ("warnings", "warning")):
        for issue in validation.get(bucket_name) or []:
            if not isinstance(issue, dict):
                continue
            scope, scope_id = _scope_for_issue(issue, step_ids)
            code = str(issue.get("code") or "").strip()
            message = str(issue.get("message") or "").strip() or code or "Validation issue"
            field_path = str(issue.get("path") or "").strip()
            message_key = str(issue.get("message_key") or "").strip()
            message_metadata = (
                {
                    "message_key": message_key,
                    "message_params": _to_object(issue.get("message_params")),
                }
                if message_key
                else {}
            )
            suggested_action_key, suggested_action = _suggested_action_metadata_for_issue(
                scope, code, message
            )
            key = (scope, scope_id, severity, code or message, message_key)
            current = grouped.get(key)
            if not current:
                current = {
                    "scope": scope,
                    "scope_id": scope_id,
                    "authored_id": scope_id,
                    "step_id": scope_id if scope == "step" else "",
                    "group_id": scope_id if scope == "group" else "",
                    "for_each_id": scope_id if scope == "for_each" else "",
                    "severity": severity,
                    "level": severity,
                    "code": code,
                    "title": message,
                    "message": message,
                    **message_metadata,
                    **(
                        {
                            "title_key": message_key,
                            "title_params": message_metadata["message_params"],
                        }
                        if message_key
                        else {}
                    ),
                    "field_path": field_path,
                    "suggested_action": suggested_action,
                    "suggested_action_key": suggested_action_key,
                    "issues": [],
                }
                grouped[key] = current
            current["issues"].append(
                {
                    "path": field_path,
                    "message": message,
                    "source_step_id": str(issue.get("source_step_id") or "").strip(),
                    **(
                        {
                            "outcome_name": str(issue["outcome_name"]),
                            "outcome_case_id": str(issue.get("outcome_case_id") or ""),
                        }
                        if code == "workflow.graph.outcome_missing" and issue.get("outcome_name")
                        else {}
                    ),
                    **message_metadata,
                    "field_path": field_path,
                    "suggested_action": suggested_action,
                    "suggested_action_key": suggested_action_key,
                }
            )

    result = list(grouped.values())
    result.sort(key=lambda item: (item["scope"], item["scope_id"], item["severity"], item["code"]))
    return result


def _suggested_action_for_issue(scope: str, code: str, message: str) -> str:
    return _suggested_action_metadata_for_issue(scope, code, message)[1]


def _suggested_action_metadata_for_issue(scope: str, code: str, message: str) -> tuple[str, str]:
    if code == "workflow.graph.outcome_missing":
        metadata = {
            "label_key": "react.workflows.diagnostic_action_missing_outcome",
            "label": "Open Connections and connect the missing outcome.",
        }
        return metadata["label_key"], metadata["label"]
    if code == "workflow.step_http_request_url_missing":
        metadata = {
            "label_key": "react.workflows.diagnostic_action_http_url",
            "label": "Open URL in Smart setup and enter the request address.",
        }
        return metadata["label_key"], metadata["label"]
    if code == "workflow.terminal_fail_intent_missing":
        metadata = {
            "label_key": "react.workflows.diagnostic_action_expected_failure",
            "label": "Open workflow settings and declare an expected failure.",
        }
        return metadata["label_key"], metadata["label"]
    normalized = f"{code} {message}".lower()
    if scope == "workflow":
        metadata = {
            "label_key": "react.workflows.diagnostic_action_workflow_contract",
            "label": "Open workflow settings and update the workflow contract.",
        }
    elif "input" in normalized or "output" in normalized:
        metadata = {
            "label_key": "react.workflows.diagnostic_action_io_contract",
            "label": "Open this step or group and align its input/output contract.",
        }
    elif "outcome" in normalized or "edge" in normalized or "branch" in normalized:
        metadata = {
            "label_key": "react.workflows.diagnostic_action_outcome_edge",
            "label": "Connect every outcome of the affected step to its next step.",
        }
    elif "selector" in normalized or "condition" in normalized:
        metadata = {
            "label_key": "react.workflows.diagnostic_action_branch_condition",
            "label": "Open the affected router step and fix its condition or selector.",
        }
    else:
        metadata = {
            "label_key": "react.workflows.diagnostic_action_configuration",
            "label": "Open the affected object and update the highlighted configuration.",
        }
    return metadata["label_key"], metadata["label"]


def _scope_for_issue(issue: dict[str, Any], step_ids: set[str]) -> tuple[str, str]:
    """The canvas object an issue belongs to: a step, a container, or the workflow."""
    issue_step_id = str(issue.get("step_id") or "").strip()
    if issue_step_id:
        return "step", issue_step_id
    path = str(issue.get("path") or "").strip()
    match = _STEP_PATH_RE.match(path)
    if match and match.group(1) in step_ids:
        return "step", match.group(1)
    match = _GROUP_PATH_RE.match(path)
    if match:
        return "group", match.group(1)
    match = _FOR_EACH_PATH_RE.match(path)
    if match:
        return "for_each", match.group(1)
    return "workflow", "workflow"


def _build_validation_summary_by_scope(
    validation_issues: list[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    summary: dict[tuple[str, str], dict[str, Any]] = defaultdict(_empty_scope_summary)
    for issue in validation_issues:
        key = (str(issue.get("scope") or "workflow"), str(issue.get("scope_id") or "workflow"))
        bucket = summary[key]
        issues = list(issue.get("issues") or [])
        count = max(len(issues), 1)
        if issue.get("severity") == "warning":
            bucket["warning_count"] += count
        else:
            bucket["error_count"] += count
        bucket["issue_count"] += count
    return dict(summary)


def _build_automation_summary_by_scope(
    db: Any, workflow_id: str
) -> dict[tuple[str, str], dict[str, Any]]:
    rows = []
    if hasattr(db, "list_workflow_reactions"):
        try:
            rows = list(db.list_workflow_reactions(workflow_id, enabled_only=False) or [])
        except Exception:
            rows = []
    summary: dict[tuple[str, str], dict[str, Any]] = defaultdict(_empty_scope_summary)
    for row in rows:
        if not isinstance(row, dict):
            continue
        scope_kind, scope_id, _ = split_scoped_reaction_key(row.get("reaction_key"))
        normalized_scope_id = scope_id or "workflow"
        bucket = summary[(scope_kind, normalized_scope_id)]
        bucket["rule_count"] += 1
        if bool(row.get("enabled", True)):
            bucket["enabled_count"] += 1
        event_type = str(row.get("event_type") or "").strip()
        action_type = str(_to_object(row.get("action")).get("type") or "").strip()
        if event_type:
            bucket.setdefault("event_types", []).append(event_type)
        if action_type:
            bucket.setdefault("action_types", []).append(action_type)
    for bucket in summary.values():
        bucket["event_types"] = sorted(set(bucket.get("event_types") or []))
        bucket["action_types"] = sorted(set(bucket.get("action_types") or []))
    return dict(summary)


def _list_automation_rules(db: Any, workflow_id: str) -> list[dict[str, Any]]:
    if not hasattr(db, "list_workflow_reactions"):
        return []
    try:
        rows = list(db.list_workflow_reactions(workflow_id, enabled_only=False) or [])
    except Exception:
        return []
    result: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        scope_kind, scope_id, base_key = split_scoped_reaction_key(row.get("reaction_key"))
        action = _to_object(row.get("action"))
        result.append(
            {
                "scope": scope_kind,
                "scope_id": scope_id or "workflow",
                "reaction_key": base_key,
                "display_name": str(row.get("display_name") or base_key).strip() or base_key,
                "event_type": str(row.get("event_type") or "").strip(),
                "action_type": str(action.get("type") or "").strip(),
                "enabled": bool(row.get("enabled", True)),
            }
        )
    return result
