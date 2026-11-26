"""Stateless issue checks used by workflow readiness validation."""

from __future__ import annotations

from core.application.orchestration.state_path_guards import is_reserved_state_target

from .contract_item_validation import _issue, _normalize_name, validate_contract_items


def _for_each_name_issues(item: dict, for_each_id: str, path: str) -> list[dict]:
    """Item and index names must not shadow a reserved runtime state field.

    The definition validator checks their shape; the reserved roots are a
    runtime contract, so readiness refuses them before a run publishes into one.
    """
    issues: list[dict] = []
    for field in ("item_name", "index_name"):
        name = str(item.get(field) or "").strip()
        if name and is_reserved_state_target(name):
            issues.append(
                _issue(
                    code="workflow.for_each_name_reserved",
                    message=(
                        f"For each '{for_each_id}' {field} '{name}' is a reserved runtime "
                        "state field and cannot hold the current item"
                    ),
                    path=f"{path}.{field}",
                )
            )
    return issues


def _approval_response_mapping_issues(step: dict, step_path: str) -> list[dict]:
    """Reject approval response mappings that target host-owned runtime state."""
    override = step.get("override")
    config = override.get("approval") if isinstance(override, dict) else None
    response_mapping = config.get("response_mapping") if isinstance(config, dict) else None
    if not isinstance(response_mapping, dict):
        return []
    raw_targets = (
        response_mapping.get("override_content_targets")
        or response_mapping.get("override_content")
        or response_mapping.get("writeback")
    )
    if isinstance(raw_targets, str):
        targets = [raw_targets]
    elif isinstance(raw_targets, list):
        targets = raw_targets
    else:
        return []
    return [
        _issue(
            code="workflow.approval_response_target_reserved",
            message="approval response mapping cannot write security-sensitive runtime state",
            path=(
                f"{step_path}.override.approval.response_mapping.override_content_targets[{index}]"
            ),
        )
        for index, target in enumerate(targets)
        if is_reserved_state_target(target)
    ]


def _workflow_input_rows(workflow: dict) -> list[dict]:
    settings = workflow.get("settings") if isinstance(workflow, dict) else None
    callable_cfg = settings.get("callable") if isinstance(settings, dict) else None
    rows = callable_cfg.get("expected_inputs") if isinstance(callable_cfg, dict) else None
    return [row for row in rows or [] if isinstance(row, dict)]


def _output_names(outputs) -> set[str]:
    return {
        str(item.get("name") or "").strip()
        for item in outputs or []
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    }


def _boundary_contract_issues(
    contract, *, owner_label: str, owner_path: str, produced: set[str]
) -> list[dict]:
    """Entry rows keep the expected-input format; exit keys must be produced.

    This is the check a phase's expected_inputs/required_outputs used to carry,
    now attached to the step or real group where the boundary actually is.
    """
    if not isinstance(contract, dict):
        return []
    issues: list[dict] = []
    _, entry_issues = validate_contract_items(
        contract.get("entry"),
        path_prefix=f"{owner_path}.boundary_contract.entry",
        default_required=False,
        allow_source=True,
    )
    issues.extend(entry_issues)
    exit_outputs = [
        _normalize_name(item) for item in (contract.get("exit") or []) if _normalize_name(item)
    ]
    if len(exit_outputs) != len(set(exit_outputs)):
        issues.append(
            _issue(
                code="workflow.boundary_exit_duplicates",
                message=f"{owner_label} has duplicate boundary exit outputs",
                path=f"{owner_path}.boundary_contract.exit",
            )
        )
    for name in exit_outputs:
        if name not in produced:
            issues.append(
                _issue(
                    code="workflow.boundary_exit_not_produced",
                    message=f"{owner_label} requires output '{name}' that no step produces",
                    path=f"{owner_path}.boundary_contract.exit",
                )
            )
    return issues


def _workflow_issue_path(path) -> str:
    text = str(path or "").strip()
    return f"workflow.{text}" if text else "workflow"


def _coerce_bool_flag(value) -> bool:
    """Truthy coercion tolerant of JSON booleans and stringified flags from settings."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _unique_issues(issues: list[dict]) -> list[dict]:
    """One issue per code, path and message.

    The definition validator and the per-step config check both judge the
    graph-owned configs (if, switch, Approval, Wait), so the same issue can
    arrive from each; the author needs to see it once.
    """
    seen: set[tuple[str, str, str]] = set()
    unique: list[dict] = []
    for issue in issues:
        key = (
            str(issue.get("code") or ""),
            str(issue.get("path") or ""),
            str(issue.get("message") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(issue)
    return unique
