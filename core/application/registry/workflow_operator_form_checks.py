"""Checks, reading and saving of the operator form (ADR 0075).

Checks run on save (an error stops ``published: true``, not the save of an
unpublished configuration) and on read, where an error blocks the form with its
reason while the history stays readable.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from .workflow_operator_form_model import (
    _EMPTINESS,
    _ID_RE,
    COLUMN_FORMATS,
    CONDITION_OPERATORS_BY_KIND,
    JOB_COLUMN_PATHS,
    JOB_FILTER_TYPES,
    OPERATOR_SETTING,
    RESULT_DISPLAYS,
    RUN_ONLY_ISSUE_CODES,
    EffectsAcknowledgement,
    OperatorContract,
    OperatorFormField,
    OperatorIssue,
    OperatorSettings,
    _properties,
    _required,
    _sub_field_container,
    compatible_filter_types,
    compatible_widgets,
    field_kind,
    operator_contract,
    resolve_schema_path,
)

# --- Checks -------------------------------------------------------------------

_BASE = "settings.operator"


def _issue(code: str, message: str, path: str, severity: str = "error") -> OperatorIssue:
    return OperatorIssue(code=code, message=message, path=path, severity=severity)  # type: ignore[arg-type]


def _condition_value_ok(kind: str, operator: str, value: Any, schema: dict[str, Any]) -> bool:
    if operator in _EMPTINESS:
        return value in (None, "")
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "number" or operator in {">", "<"}:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "select":
        return isinstance(value, str) and value in (schema.get("enum") or [])
    if kind == "select_list":
        options = (schema.get("items") or {}).get("enum") or []
        return isinstance(value, str) and value in options
    if operator == "==pattern":
        if not isinstance(value, str):
            return False
        try:
            re.compile(value)
        except re.error:
            return False
        return True
    return isinstance(value, str)


def _condition_issues(
    field: OperatorFormField,
    *,
    name: str,
    siblings: dict[str, Any],
    secret_names: set[str],
    path: str,
) -> list[OperatorIssue]:
    issues: list[OperatorIssue] = []
    for group_index, group in enumerate(field.conditional_logic):
        for rule_index, rule in enumerate(group):
            rule_path = f"{path}.conditional_logic[{group_index}][{rule_index}]"
            target = siblings.get(rule.field)
            if rule.field == name or target is None or rule.field in secret_names:
                issues.append(
                    _issue(
                        "form.condition_field_unknown",
                        f"The condition of '{name}' refers to '{rule.field}', "
                        "which is not another field of this form",
                        f"{rule_path}.field",
                    )
                )
                continue
            kind = field_kind(target)
            if rule.operator not in CONDITION_OPERATORS_BY_KIND.get(kind, ()):
                issues.append(
                    _issue(
                        "form.condition_operator_incompatible",
                        f"Operator '{rule.operator}' does not fit the "
                        f"{kind or 'unsupported'} field '{rule.field}'",
                        f"{rule_path}.operator",
                    )
                )
                continue
            if not _condition_value_ok(kind, rule.operator, rule.value, target):
                issues.append(
                    _issue(
                        "form.condition_value_invalid",
                        f"The value compared with '{rule.field}' does not fit its type "
                        "or its options",
                        f"{rule_path}.value",
                    )
                )
    return issues


def _form_field_issues(
    name: str,
    field: OperatorFormField,
    node: dict[str, Any],
    *,
    required: bool,
    fixed: bool,
    siblings: dict[str, Any],
    secret_names: set[str],
    path: str,
    depth: int,
) -> list[OperatorIssue]:
    issues: list[OperatorIssue] = []
    kind = field_kind(node)
    widgets = compatible_widgets(node)
    if field.widget and field.widget not in widgets:
        issues.append(
            _issue(
                "form.widget_incompatible",
                f"Widget '{field.widget}' does not fit the {kind or 'unsupported'} field "
                f"'{name}'" + (f"; use one of: {', '.join(widgets)}" if widgets else ""),
                f"{path}.widget",
            )
        )
    if not widgets and not field.hidden:
        issues.append(
            _issue(
                "form.field_unsupported",
                f"The form cannot show '{name}': declare one type with its items or "
                "properties in the workflow contract",
                path,
                "error" if required and not fixed else "warning",
            )
        )
    if field.hidden and required and not fixed:
        issues.append(
            _issue(
                "form.hidden_required_without_default",
                f"'{name}' is required and hidden, but the contract gives it no default",
                f"{path}.hidden",
            )
        )
    if fixed and not field.hidden and depth == 1:
        issues.append(
            _issue(
                "form.field_fixed_by_contract",
                f"'{name}' has a fixed value in the contract; the form shows it read-only",
                path,
                "warning",
            )
        )
    if field.conditional_logic and required:
        issues.append(
            _issue(
                "form.condition_on_required",
                f"'{name}' has a display condition, so it cannot be required in the contract; "
                "the form checks it only while shown",
                f"{path}.conditional_logic",
            )
        )
    issues.extend(
        _condition_issues(field, name=name, siblings=siblings, secret_names=secret_names, path=path)
    )
    options = node.get("enum") if kind == "select" else (node.get("items") or {}).get("enum")
    for option in field.option_labels:
        if option not in (options or []):
            issues.append(
                _issue(
                    "form.option_label_unknown",
                    f"'{option}' is not an option of '{name}'",
                    f"{path}.option_labels.{option}",
                    "warning",
                )
            )
    if field.sub_fields:
        if kind not in {"object", "object_list"}:
            issues.append(
                _issue(
                    "form.sub_fields_unsupported",
                    f"'{name}' is not a group or repeater, so it has no sub-fields",
                    f"{path}.sub_fields",
                )
            )
        else:
            container = _sub_field_container(node)
            sub_properties = _properties(container)
            sub_required = _required(container)
            for sub_name, sub_field in field.sub_fields.items():
                sub_path = f"{path}.sub_fields.{sub_name}"
                sub_node = sub_properties.get(sub_name)
                if not isinstance(sub_node, dict):
                    issues.append(
                        _issue(
                            "form.field_unknown",
                            f"'{name}' has no sub-field '{sub_name}' in the contract",
                            sub_path,
                        )
                    )
                    continue
                issues.extend(
                    _form_field_issues(
                        sub_name,
                        sub_field,
                        sub_node,
                        required=sub_name in sub_required,
                        fixed=False,
                        siblings=sub_properties,
                        secret_names=set(),
                        path=sub_path,
                        depth=depth + 1,
                    )
                )
    return issues


def _form_issues(settings: OperatorSettings, contract: OperatorContract) -> list[OperatorIssue]:
    issues: list[OperatorIssue] = []
    properties = _properties(contract.input_schema)
    required = _required(contract.input_schema)
    secret_names = set(contract.secret_inputs) | {
        name for name, node in properties.items() if field_kind(node) == "secret"
    }
    fixed = set(contract.fixed_inputs)
    prefix = f"{_BASE}.form"
    for name, field in settings.form.fields.items():
        path = f"{prefix}.fields.{name}"
        if name in secret_names:
            if not field.hidden:
                issues.append(
                    _issue(
                        "form.field_secret",
                        f"'{name}' is a secret; its value is entered in the secret catalog, "
                        "not in the form",
                        path,
                    )
                )
            continue
        node = properties.get(name)
        if not isinstance(node, dict):
            issues.append(
                _issue(
                    "form.field_unknown",
                    f"The workflow contract has no input '{name}'",
                    path,
                )
            )
            continue
        issues.extend(
            _form_field_issues(
                name,
                field,
                node,
                required=name in required,
                fixed=name in fixed,
                siblings=properties,
                secret_names=secret_names,
                path=path,
                depth=1,
            )
        )
    seen: set[str] = set()
    for index, name in enumerate(settings.form.order):
        path = f"{prefix}.order[{index}]"
        if name in seen:
            issues.append(_issue("form.order_duplicate", f"'{name}' is listed twice", path))
        seen.add(name)
        if name in secret_names:
            issues.append(
                _issue("form.field_secret", f"'{name}' is a secret and cannot be ordered", path)
            )
        elif name not in properties:
            issues.append(
                _issue("form.field_unknown", f"The workflow contract has no input '{name}'", path)
            )
    for name in sorted(required - secret_names - fixed - set(settings.form.fields)):
        # A required input without an entry is shown with its default widget; one the
        # form cannot render blocks the form.
        if not compatible_widgets(properties.get(name)):
            issues.append(
                _issue(
                    "form.field_unsupported",
                    f"The form cannot show the required input '{name}': declare one type "
                    "with its items or properties in the workflow contract",
                    f"{prefix}.fields.{name}",
                )
            )
    return issues


def _result_issues(settings: OperatorSettings, contract: OperatorContract) -> list[OperatorIssue]:
    issues: list[OperatorIssue] = []
    outputs = _properties(contract.output_schema)
    prefix = f"{_BASE}.result"
    for name, field in settings.result.fields.items():
        path = f"{prefix}.fields.{name}"
        if name not in outputs:
            issues.append(
                _issue("result.field_unknown", f"The workflow returns no output '{name}'", path)
            )
        if field.display not in RESULT_DISPLAYS:
            issues.append(
                _issue(
                    "result.display_unknown",
                    f"Unknown display '{field.display}'",
                    f"{path}.display",
                )
            )
    for index, name in enumerate(settings.result.order):
        if name not in outputs:
            issues.append(
                _issue(
                    "result.field_unknown",
                    f"The workflow returns no output '{name}'",
                    f"{prefix}.order[{index}]",
                )
            )
    return issues


def _value_node(contract: OperatorContract, source: str, path: str) -> dict[str, Any] | None:
    if source == "input":
        if path.split(".")[0].removesuffix("[]") in contract.secret_inputs:
            return None
        node = resolve_schema_path(contract.input_schema, path)
        return None if field_kind(node) == "secret" else node
    return resolve_schema_path(contract.output_schema, path)


def _list_issues(settings: OperatorSettings, contract: OperatorContract) -> list[OperatorIssue]:
    issues: list[OperatorIssue] = []
    prefix = f"{_BASE}.list"
    seen: set[str] = set()
    for index, column in enumerate(settings.list.columns):
        path = f"{prefix}.columns[{index}]"
        if not _ID_RE.fullmatch(column.id) or column.id in seen:
            issues.append(
                _issue(
                    "list.column_id_invalid",
                    f"Column id '{column.id}' must be unique and simple",
                    f"{path}.id",
                )
            )
        seen.add(column.id)
        if column.format not in COLUMN_FORMATS:
            issues.append(
                _issue(
                    "list.column_format_unknown",
                    f"Unknown format '{column.format}'",
                    f"{path}.format",
                )
            )
        known = (
            column.path in JOB_COLUMN_PATHS
            if column.source == "job"
            else _value_node(contract, column.source, column.path) is not None
        )
        if not known:
            issues.append(
                _issue(
                    "list.column_unknown_path",
                    f"'{column.path}' is not a {column.source} value this workflow declares",
                    f"{path}.path",
                )
            )
    seen = set()
    for index, row in enumerate(settings.list.filters):
        path = f"{prefix}.filters[{index}]"
        if not _ID_RE.fullmatch(row.id) or row.id in seen:
            issues.append(
                _issue(
                    "list.filter_id_invalid",
                    f"Filter id '{row.id}' must be unique and simple",
                    f"{path}.id",
                )
            )
        seen.add(row.id)
        if row.source == "job":
            allowed = JOB_FILTER_TYPES.get(row.path)
            if allowed is None:
                issues.append(
                    _issue(
                        "list.filter_unknown_path",
                        f"Runs cannot be filtered by '{row.path}'; use the status or the "
                        "list's date filter",
                        f"{path}.path",
                    )
                )
                continue
        else:
            # Full values are kept per top-level input and output (meta.filter_values,
            # meta.workflow_outputs), so a filter names one of them.
            node = None if "." in row.path else _value_node(contract, row.source, row.path)
            if node is None:
                issues.append(
                    _issue(
                        "list.filter_unknown_path",
                        f"'{row.path}' is not a top-level {row.source} this workflow declares",
                        f"{path}.path",
                    )
                )
                continue
            if field_kind(node) in {"file", "file_list"}:
                issues.append(
                    _issue(
                        "list.filter_on_file",
                        f"'{row.path}' is a file; files cannot be filtered",
                        f"{path}.path",
                    )
                )
                continue
            allowed = compatible_filter_types(node)
        if row.type not in allowed:
            issues.append(
                _issue(
                    "list.filter_type_incompatible",
                    f"Filter type '{row.type}' does not fit '{row.path}'"
                    + (f"; use one of: {', '.join(allowed)}" if allowed else ""),
                    f"{path}.type",
                )
            )
    return issues


def _submission_issues(
    settings: OperatorSettings, contract: OperatorContract
) -> list[OperatorIssue]:
    submission = settings.submission
    if submission.mode == "bulk":
        return []
    path = f"{_BASE}.submission.target_input"
    target = submission.target_input
    properties = _properties(contract.input_schema)
    node = properties.get(target) if target else None
    if not isinstance(node, dict) or target in contract.secret_inputs:
        return [
            _issue(
                "submission.foreach_target_missing",
                "For each needs a top-level input whose values become one run each",
                path,
            )
        ]
    config = settings.form.fields.get(target)
    if target in contract.fixed_inputs or (config is not None and config.hidden):
        # The form never sends a hidden or fixed value, so the operator would
        # have nothing to split; a silent single run would be a different mode.
        return [
            _issue(
                "submission.foreach_target_hidden",
                f"For each splits '{target}', but the form does not show it to the operator",
                path,
            )
        ]
    node_type = node.get("type")
    if field_kind(node) != "file" and node_type != "array":
        return [
            _issue(
                "submission.foreach_target_kind",
                f"For each cannot split '{target}': choose a list or a file input",
                path,
            )
        ]
    min_items = node.get("minItems")
    if isinstance(min_items, int) and min_items > 1:
        return [
            _issue(
                "submission.foreach_min_items",
                f"'{target}' needs at least {min_items} items, so one item per run is invalid",
                path,
            )
        ]
    return []


def acknowledgement_current(settings: OperatorSettings, contract: OperatorContract) -> bool:
    ack = settings.effects_acknowledged
    return bool(ack and ack.contract_sha and ack.contract_sha == contract.contract_sha)


def operator_settings_issues(
    settings: OperatorSettings, contract: OperatorContract
) -> list[OperatorIssue]:
    """Every problem of the configuration against the contract, each with a path."""
    issues: list[OperatorIssue] = []
    if settings.published and not contract.ui_channel:
        issues.append(
            _issue(
                "operator.ui_channel_missing",
                "Showing the workflow to operators needs the UI trigger channel",
                f"{_BASE}.published",
            )
        )
    if settings.published and not contract.enabled:
        issues.append(
            _issue(
                "operator.workflow_disabled",
                "The workflow is disabled",
                f"{_BASE}.published",
            )
        )
    if settings.published and not acknowledgement_current(settings, contract):
        issues.append(
            _issue(
                "operator.effects_not_acknowledged",
                "An admin has not confirmed the real effects of this workflow's current contract",
                f"{_BASE}.effects_acknowledged",
            )
        )
    issues.extend(_form_issues(settings, contract))
    issues.extend(_result_issues(settings, contract))
    issues.extend(_list_issues(settings, contract))
    issues.extend(_submission_issues(settings, contract))
    return issues


# --- Reading and saving -------------------------------------------------------


class OperatorSettingsRejected(ValueError):
    """A save that would publish a configuration with blocking issues."""

    def __init__(self, issues: list[OperatorIssue]):
        self.issues = issues
        super().__init__("; ".join(f"{issue.path}: {issue.message}" for issue in issues[:20]))


def _validation_issues(exc: ValidationError) -> list[OperatorIssue]:
    return [
        _issue(
            "operator.settings_invalid",
            str(error.get("msg") or "Invalid value"),
            ".".join([_BASE, *(str(part) for part in error.get("loc") or ())]),
        )
        for error in exc.errors()[:50]
    ]


def parse_operator_settings(raw: Any) -> tuple[OperatorSettings | None, list[OperatorIssue]]:
    """Stored or submitted settings; ``None`` with issues when they are malformed."""
    if raw is None:
        return None, []
    if not isinstance(raw, Mapping):
        return None, [
            _issue("operator.settings_invalid", "settings.operator must be an object", _BASE)
        ]
    try:
        return OperatorSettings.model_validate(dict(raw)), []
    except ValidationError as exc:
        return None, _validation_issues(exc)


def stored_operator_settings(workflow: Mapping[str, Any] | None) -> Any:
    settings = workflow.get("settings") if isinstance(workflow, Mapping) else None
    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except ValueError:
            return None
    return settings.get(OPERATOR_SETTING) if isinstance(settings, Mapping) else None


def blocking_save_issues(
    settings: OperatorSettings, issues: Iterable[OperatorIssue]
) -> list[OperatorIssue]:
    if not settings.published:
        return []
    return [
        issue
        for issue in issues
        if issue.severity == "error" and issue.code not in RUN_ONLY_ISSUE_CODES
    ]


def _comparable(settings: OperatorSettings | None) -> dict[str, Any] | None:
    """What the admin configured, without the server-owned acknowledgement."""
    if settings is None:
        return None
    payload = settings.stored_dump()
    payload.pop("effects_acknowledged", None)
    return payload


def prepare_operator_settings_for_save(
    *,
    existing: Mapping[str, Any],
    final_payload: dict[str, Any],
    actor_id: str,
    now: datetime | None = None,
) -> list[OperatorIssue]:
    """Normalize ``settings.operator`` of a workflow save in place; refuse a broken publication.

    - ``effects_acknowledged``: ``{"confirm": true}`` acknowledges the saved contract;
      the stored acknowledgement is kept while the contract is unchanged and is
      dropped when it changes;
    - a save that changes ``settings.operator`` (publishing included) is refused when
      the result is malformed, or published with an error;
    - any other save goes through, even when a contract change breaks a published
      form: the admin must always be able to edit the workflow. The issues are
      returned, and the section shows ``runnable: false`` with them.
    """
    settings = final_payload.get("settings")
    if not isinstance(settings, dict) or OPERATOR_SETTING not in settings:
        return []
    raw = settings.get(OPERATOR_SETTING)
    if raw is None:
        settings.pop(OPERATOR_SETTING, None)
        return []
    previous_raw = stored_operator_settings(existing)
    parsed, parse_issues = parse_operator_settings(raw)
    if parsed is None:
        if raw == previous_raw:
            # Stored as it was; this save does not touch it.
            return parse_issues
        raise OperatorSettingsRejected(parse_issues)
    candidate = {**dict(existing), **final_payload, "settings": settings}
    if "steps" not in final_payload:
        candidate["steps"] = existing.get("steps")
    from .workflow_contract import interface_from_definition

    interface = interface_from_definition(candidate)
    revision = int(existing.get("revision") or 0) + 1
    contract = operator_contract(
        interface,
        workflow_id=str(existing.get("workflow_id") or final_payload.get("workflow_id") or ""),
        revision=revision,
        enabled=bool(final_payload.get("enabled", existing.get("enabled", False))),
    )
    previous, _ = parse_operator_settings(previous_raw)
    previous_ack = previous.effects_acknowledged if previous is not None else None
    ack = parsed.effects_acknowledged
    confirmed = bool(ack and ack.confirm)
    if ack is None:
        parsed.effects_acknowledged = None
    elif confirmed:
        moment = now or datetime.now(UTC)
        parsed.effects_acknowledged = EffectsAcknowledgement(
            by=str(actor_id or "").strip(),
            at=moment.isoformat(),
            revision=revision,
            contract_sha=contract.contract_sha,
        )
    elif previous_ack is not None and previous_ack.contract_sha == contract.contract_sha:
        parsed.effects_acknowledged = previous_ack.model_copy(update={"confirm": False})
    else:
        # A changed contract voids the acknowledgement; a caller cannot write one.
        parsed.effects_acknowledged = None
    issues = operator_settings_issues(parsed, contract)
    operator_changed = confirmed or _comparable(parsed) != _comparable(previous)
    blocking = blocking_save_issues(parsed, issues) if operator_changed else []
    if blocking:
        raise OperatorSettingsRejected(blocking)
    settings[OPERATOR_SETTING] = parsed.stored_dump()
    return issues


def portable_settings(settings: Any) -> Any:
    """Settings as they may leave this installation: no operator acknowledgement.

    The acknowledgement records one admin of one installation confirming real
    effects; an export, import, template or clone keeps the rest of
    ``settings.operator`` and asks the new owner to confirm again.
    """
    if not isinstance(settings, Mapping):
        return settings
    operator = settings.get(OPERATOR_SETTING)
    if not isinstance(operator, Mapping) or "effects_acknowledged" not in operator:
        return settings
    return {
        **dict(settings),
        OPERATOR_SETTING: {
            key: value for key, value in operator.items() if key != "effects_acknowledged"
        },
    }
