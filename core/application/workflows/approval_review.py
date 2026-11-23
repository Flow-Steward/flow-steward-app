"""Typed review fields of an Approval step (ADR 0075, item 9).

An Approval step may declare ``override.approval.review``::

    {
      "fields": [<WorkflowContractField> + {"editable": bool, "from": "<selector>"}],
      "original": "<selector of a file or a list of files>",
      "fields_from_workflow": "<workflow id; an editor hint only>"
    }

A field is the public workflow contract field (``WorkflowContractField``) with
two more keys: ``editable`` (the person may change the value) and ``from`` (the
selector of its initial value, the same syntax as an ``invoke_workflow`` input
mapping: ``message.step.<id>.artifacts.<name>``, ``state.<path>``, a bare state
name or ``literal:<json>``). A field without a ``type`` is a ``paragraph``.

When the gate opens, the schema, the editable names and the initial values are
captured whole and typed (``open_review``) and travel with the approval's
interrupt payload, so a decision is checked against what the person saw, never
against a definition edited later. A decision's ``values`` hold edits of
editable fields only; they are laid over the captured values and the result is
checked field by field with ``coerce_contract_value`` (``decide_review``). On
approve the checked values become the step's outputs ``<step>.<field>``.

An initial value that fails its type is kept as it was found, with a note in
``invalid``: the person sees it and can correct it, and approving it unchanged
is refused with the field named. A number typed with a decimal comma
(``"318,50"``) is not a number to the API: a client sends JSON numbers, and the
review page parses the person's locale before it sends.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import ConfigDict, Field, ValidationError, field_validator

from core.application.registry.workflow_contract import (
    CANONICAL_FIELD_TYPES,
    WorkflowContractField,
    coerce_bool,
)
from core.application.registry.workflow_contract_shape import (
    coerce_contract_value,
    field_shape_issues,
    is_missing_contract_value,
)

REVIEW_CONFIG_KEY = "review"
#: The key of the captured review on the approval's interrupt payload.
REVIEW_PAYLOAD_KEY = "review"
REVIEW_SCHEMA_VERSION = 1
REVIEW_CONFIG_KEYS = frozenset({"fields", "original", "fields_from_workflow"})
DEFAULT_REVIEW_FIELD_TYPE = "paragraph"
#: Artifact names the decision message of an Approval step already carries
#: (``builder_approval_gate._decision_message``): a review field with one of
#: these names would be silently shadowed by the gate's own value. ``_outcome``
#: routes the decision to its branch.
RESERVED_REVIEW_FIELD_NAMES = frozenset(
    {
        "decision",
        "comment",
        "approved",
        "feedback",
        "approval_id",
        "step_kind",
        "step_id",
        "review_changes",
        "_outcome",
    }
)
#: Item types of a list the review page edits (text, numbers, options, objects).
EDITABLE_REVIEW_LIST_ITEMS = frozenset({"string", "paragraph", "number", "select", "object"})
#: The bound of the captured values and of a decision's edits: the default body
#: limit of a workflow run entrypoint, so a review holds what a run accepts.
REVIEW_VALUES_MAX_BYTES = 1024 * 1024
#: Producer types an ``original`` may come from when the type is known.
ORIGINAL_SOURCE_TYPES = frozenset({"file", "artifact_handle", "array", "list", "json"})
_FIELD_NAME_PATTERN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*")
_PUBLIC_FIELD_KEYS = (
    "name",
    "label",
    "type",
    "required",
    "editable",
    "options",
    "items",
    "properties",
    "min_items",
    "max_items",
    "format",
    "description",
)
_REDACTED = "[REDACTED]"


class ReviewDecisionError(ValueError):
    """A decision's values do not fit the captured review; ``field`` names the culprit."""

    def __init__(self, code: str, message: str, *, field: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field


class ReviewField(WorkflowContractField):
    """A contract field the reviewer sees, with its editability and source."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    editable: bool = False
    source: str = Field(default="", alias="from")

    @field_validator("editable", mode="before")
    @classmethod
    def _normalize_editable(cls, value: Any) -> bool:
        return coerce_bool(value, False)

    @field_validator("source", mode="before")
    @classmethod
    def _normalize_source(cls, value: Any) -> str:
        return str(value or "").strip()

    def dump(self) -> dict[str, Any]:
        payload = super().dump()
        payload.pop("source", None)
        payload.pop("from", None)
        payload["editable"] = self.editable
        if self.source:
            payload["from"] = self.source
        return payload


def _issue(code: str, message: str, path: str) -> dict[str, Any]:
    return {"code": code, "message": message, "path": path, "severity": "error"}


def review_config(approval_config: Mapping[str, Any] | None) -> Any:
    """The raw ``review`` value of an Approval config; ``None`` when not declared."""
    if not isinstance(approval_config, Mapping):
        return None
    return approval_config.get(REVIEW_CONFIG_KEY)


def parse_review_field(raw: Mapping[str, Any]) -> ReviewField:
    """One declared field; a field without a type is a ``paragraph``."""
    data = dict(raw)
    if not str(data.get("type") or "").strip():
        data["type"] = DEFAULT_REVIEW_FIELD_TYPE
    return ReviewField.model_validate(data)


def review_fields(review: Any) -> list[ReviewField]:
    """The usable declared fields, skipping rows the definition check reports."""
    rows = review.get("fields") if isinstance(review, Mapping) else None
    fields: list[ReviewField] = []
    seen: set[str] = set()
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        try:
            field = parse_review_field(row)
        except ValidationError:
            continue
        if (
            not field.name
            or field.name in seen
            or field.is_secret
            or field.name in RESERVED_REVIEW_FIELD_NAMES
        ):
            continue
        seen.add(field.name)
        fields.append(field)
    return fields


# --- Definition issues -------------------------------------------------------


def review_config_issues(review: Any, *, path: str) -> list[dict[str, Any]]:
    """Problems of a declared ``review`` that need no other workflow to find."""
    if review is None:
        return []
    if not isinstance(review, Mapping):
        return [
            _issue(
                "workflow.approval_review_invalid",
                "Approval review must be an object with fields",
                path,
            )
        ]
    issues = [
        _issue(
            "workflow.approval_review_key_unknown",
            f"Unknown Approval review key '{key}'",
            f"{path}.{key}",
        )
        for key in sorted(set(review) - REVIEW_CONFIG_KEYS)
    ]
    for key in ("original", "fields_from_workflow"):
        value = review.get(key)
        if value is not None and not isinstance(value, str):
            issues.append(
                _issue(
                    f"workflow.approval_review_{key}_invalid",
                    f"Approval review {key} must be a text value",
                    f"{path}.{key}",
                )
            )
    if isinstance(review.get("original"), str) and review["original"].strip() == "state":
        # A bare ``state`` reads the value named like the field; the original has
        # no name of its own, so it needs a path.
        issues.append(
            _issue(
                "workflow.approval_review_original_needs_path",
                "Approval review original needs a path, for example state.files",
                f"{path}.original",
            )
        )
    rows = review.get("fields")
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        return [
            *issues,
            _issue(
                "workflow.approval_review_fields_invalid",
                "Approval review fields must be a list",
                f"{path}.fields",
            ),
        ]
    seen: set[str] = set()
    for index, row in enumerate(rows):
        issues.extend(_field_issues(row, path=f"{path}.fields[{index}]", seen=seen))
    return issues


def _field_issues(row: Any, *, path: str, seen: set[str]) -> list[dict[str, Any]]:
    if not isinstance(row, Mapping):
        return [
            _issue(
                "workflow.approval_review_field_invalid",
                "An Approval review field must be an object",
                path,
            )
        ]
    try:
        field = parse_review_field(row)
    except ValidationError:
        return [
            _issue(
                "workflow.approval_review_field_invalid",
                "An Approval review field could not be read",
                path,
            )
        ]
    name = field.name
    if not name:
        return [
            _issue(
                "workflow.approval_review_field_name_required",
                "An Approval review field has no name",
                f"{path}.name",
            )
        ]
    if not _FIELD_NAME_PATTERN.fullmatch(name):
        return [
            _issue(
                "workflow.approval_review_field_name_invalid",
                f"Approval review field '{name}' may use only letters, digits, '_' and '-'",
                f"{path}.name",
            )
        ]
    if name in RESERVED_REVIEW_FIELD_NAMES:
        return [
            _issue(
                "workflow.approval_review_field_name_reserved",
                f"Approval review field '{name}' uses a name of the step's own result",
                f"{path}.name",
            )
        ]
    if name in seen:
        return [
            _issue(
                "workflow.approval_review_field_duplicate",
                f"Duplicate Approval review field '{name}'",
                f"{path}.name",
            )
        ]
    seen.add(name)
    if field.type not in CANONICAL_FIELD_TYPES:
        return [
            _issue(
                "workflow.approval_review_field_type_unknown",
                f"Approval review field '{name}' has unsupported type '{field.type}'",
                f"{path}.type",
            )
        ]
    if field.is_secret:
        return [
            _issue(
                "workflow.approval_review_field_secret",
                f"Approval review field '{name}' cannot be a secret: a reviewer never sees one",
                f"{path}.type",
            )
        ]
    issues: list[dict[str, Any]] = []
    if field.required and not field.editable and not field.source:
        issues.append(
            _issue(
                "workflow.approval_review_field_source_required",
                f"Required Approval review field '{name}' is not editable and has no source",
                f"{path}.from",
            )
        )
    if field.editable and (field.type == "file" or field.is_file_list):
        issues.append(
            _issue(
                "workflow.approval_review_field_file_editable",
                f"Approval review field '{name}' holds files, which a reviewer only views",
                f"{path}.editable",
            )
        )
    if field.editable and field.type == "array" and not field.is_file_list:
        items = field.dump().get("items")
        item_type = str(items.get("type") or "") if isinstance(items, Mapping) else ""
        if item_type not in EDITABLE_REVIEW_LIST_ITEMS:
            # The review page edits lists of text, numbers, options or objects;
            # anything else would be a field the person sees but cannot fill.
            issues.append(
                _issue(
                    "workflow.approval_review_field_list_not_editable",
                    f"Approval review field '{name}' is a list the review page cannot edit; "
                    "declare its items as text, number, select or object, or make it read-only",
                    f"{path}.items",
                )
            )
    if field.type == "select" and not field.options:
        issues.append(
            _issue(
                "workflow.approval_review_field_options_missing",
                f"Select Approval review field '{name}' must declare options",
                f"{path}.options",
            )
        )
    issues.extend(field_shape_issues(field, path=path))
    return issues


def review_source_issues(
    review: Any,
    *,
    path: str,
    source_type: Callable[[str, str], tuple[str | None, str]],
    compatible: Callable[[str, str], bool],
    original_is_file: Callable[[str, str | None], bool] | None = None,
) -> list[dict[str, Any]]:
    """Problems with the sources of the fields and the original.

    ``source_type(selector, field_name)`` answers ``(type, error)`` the way an
    Invoke input mapping is resolved; an unknown type is not a problem, so a
    field from a free agent answer stays as declared.
    """
    if not isinstance(review, Mapping):
        return []
    issues: list[dict[str, Any]] = []
    rows = review.get("fields")
    for index, row in enumerate(rows if isinstance(rows, list) else []):
        if not isinstance(row, Mapping):
            continue
        try:
            field = parse_review_field(row)
        except ValidationError:
            continue
        if not field.name or not field.source:
            continue
        field_path = f"{path}.fields[{index}].from"
        found, error = source_type(field.source, field.name)
        if error == "source_not_available":
            issues.append(
                {
                    **_issue(
                        "workflow.approval_review_source_unavailable",
                        f"Approval review field '{field.name}' reads '{field.source}', "
                        "which no earlier step or input provides",
                        field_path,
                    ),
                    "source": field.source,
                }
            )
            continue
        if found and not compatible(field.type, found):
            issues.append(
                {
                    **_issue(
                        "workflow.approval_review_source_type_mismatch",
                        f"Approval review field '{field.name}' is '{field.type}' but its "
                        f"source '{field.source}' is '{found}'",
                        field_path,
                    ),
                    "source": field.source,
                }
            )
    original = review.get("original")
    if isinstance(original, str) and original.strip():
        selector = original.strip()
        found, error = source_type(selector, "")
        original_path = f"{path}.original"
        if error == "source_not_available":
            issues.append(
                _issue(
                    "workflow.approval_review_original_unavailable",
                    f"Approval review original '{selector}' is not provided by an input "
                    "or an earlier step",
                    original_path,
                )
            )
        elif found and (
            found not in ORIGINAL_SOURCE_TYPES
            or (original_is_file is not None and not original_is_file(selector, found))
        ):
            issues.append(
                _issue(
                    "workflow.approval_review_original_not_file",
                    f"Approval review original '{selector}' is '{found}', not a file",
                    original_path,
                )
            )
    return issues


# --- Opening a review --------------------------------------------------------


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))


def _field_selector(field: ReviewField) -> str:
    """The selector a field reads: a bare ``state`` is the state value of the field's name.

    Readiness resolves it the same way (as an Invoke input mapping does), so
    the check and the run read the same value.
    """
    return f"state.{field.name}" if field.source == "state" else field.source


def open_review(
    review: Any,
    *,
    resolve: Callable[[str], tuple[Any, str]],
) -> dict[str, Any] | None:
    """Capture the schema, the editable names and the initial values of a review.

    ``resolve(selector)`` answers ``(value, error)``. A value that fails its
    type stays as found, with the reason in ``invalid``; one that does not fit
    in ``REVIEW_VALUES_MAX_BYTES`` is left out with a note.
    """
    if not isinstance(review, Mapping):
        return None
    fields = review_fields(review)
    values: dict[str, Any] = {}
    invalid: dict[str, str] = {}
    budget = REVIEW_VALUES_MAX_BYTES
    for field in fields:
        raw: Any = None
        if field.source:
            found, error = resolve(_field_selector(field))
            raw = None if error else found
        if is_missing_contract_value(raw, field):
            values[field.name] = None
            continue
        ok, coerced, message = coerce_contract_value(raw, field)
        value = _json_safe(coerced if ok else raw)
        size = _json_size(value)
        if size > budget:
            values[field.name] = None
            invalid[field.name] = (
                f"Review field '{field.name}' is larger than {REVIEW_VALUES_MAX_BYTES} bytes"
            )
            continue
        budget -= size
        values[field.name] = value
        if not ok:
            invalid[field.name] = message
    original_from = str(review.get("original") or "").strip()
    original: Any = None
    if original_from and original_from != "state":
        found, error = resolve(original_from)
        original = None if error else _json_safe(found)
    captured: dict[str, Any] = {
        "version": REVIEW_SCHEMA_VERSION,
        "fields": [field.dump() for field in fields],
        "editable": [field.name for field in fields if field.editable],
        "values": values,
        "invalid": invalid,
    }
    if original_from:
        captured["original_from"] = original_from
        captured["original"] = original
    return captured


def captured_review(payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The review captured on an approval payload; ``None`` for a gate without one."""
    review = payload.get(REVIEW_PAYLOAD_KEY) if isinstance(payload, Mapping) else None
    if not isinstance(review, Mapping) or not isinstance(review.get("fields"), list):
        return None
    return dict(review)


def _captured_fields(review: Mapping[str, Any]) -> list[ReviewField]:
    fields: list[ReviewField] = []
    for row in review.get("fields") or []:
        if not isinstance(row, Mapping):
            continue
        try:
            fields.append(ReviewField.model_validate(dict(row)))
        except ValidationError:
            continue
    return fields


# --- Deciding ----------------------------------------------------------------


def decide_review(
    review: Mapping[str, Any],
    values: Mapping[str, Any] | None,
    *,
    approved: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The checked values of a decision and what the person changed.

    Edits name editable fields only. On approve, every field of the merged
    result passes its captured type, requiredness and shape, or the decision
    is refused naming the field. A rejection keeps the edits unchecked: they
    are feedback, not outputs.
    """
    fields = _captured_fields(review)
    by_name = {field.name: field for field in fields}
    editable = {str(name) for name in review.get("editable") or [] if str(name) in by_name}
    raw_initial = review.get("values")
    initial = raw_initial if isinstance(raw_initial, Mapping) else {}
    edits = dict(values or {})
    for name in edits:
        if str(name) in RESERVED_REVIEW_FIELD_NAMES:
            raise ReviewDecisionError(
                "approval_review_field_reserved",
                f"Review field '{name}' is a name of the Approval step's own result",
                field=str(name),
            )
        if name not in by_name:
            raise ReviewDecisionError(
                "approval_review_field_unknown",
                f"Review field '{name}' is not part of this review",
                field=str(name),
            )
        if name not in editable:
            raise ReviewDecisionError(
                "approval_review_field_not_editable",
                f"Review field '{name}' is not editable",
                field=str(name),
            )
    if edits and _json_size(edits) > REVIEW_VALUES_MAX_BYTES:
        raise ReviewDecisionError(
            "approval_review_values_too_large",
            f"Review values are larger than {REVIEW_VALUES_MAX_BYTES} bytes",
        )
    merged = {field.name: initial.get(field.name) for field in fields}
    merged.update(edits)
    if approved:
        result: dict[str, Any] = {}
        for field in fields:
            value = merged.get(field.name)
            if is_missing_contract_value(value, field):
                if field.required:
                    raise ReviewDecisionError(
                        "approval_review_field_required",
                        f"Review field '{field.name}' is required",
                        field=field.name,
                    )
                result[field.name] = None
                continue
            ok, coerced, message = coerce_contract_value(value, field)
            if not ok:
                raise ReviewDecisionError(
                    "approval_review_field_invalid", message, field=field.name
                )
            result[field.name] = _json_safe(coerced)
    else:
        result = {name: _json_safe(value) for name, value in merged.items()}
    changes = [
        {"field": name, "before": initial.get(name), "after": result.get(name)}
        for name in edits
        if result.get(name) != initial.get(name)
    ]
    return result, changes


# --- Public projection -------------------------------------------------------


def masked_review_value(value: Any, *, key: Any = None) -> Any:
    """A review value as shown publicly, at any depth.

    A value under a secret-like key (``password``, ``api_key``, ``token``... by
    the shared rule of the job input projections, ``is_secret_like_input``), a
    secret reference and a secret detected in text are masked. ``key`` is the
    name the value sits under: the field name at the top level, the property
    name inside an object. A list's items keep the key of the list.
    """
    from core.application.jobs.job_invocation_artifacts import is_secret_like_input
    from core.application.jobs.public_secret_redaction import redact_public_secrets
    from core.infrastructure.security.secret_reference import SecretReference

    if value is None:
        return None
    if key is not None and is_secret_like_input(key):
        return _REDACTED
    if isinstance(value, Mapping):
        if SecretReference.from_wire(dict(value)) is not None:
            return _REDACTED
        return {str(name): masked_review_value(item, key=str(name)) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [masked_review_value(item) for item in value]
    if isinstance(value, str):
        return redact_public_secrets(value)
    return value


def _masked_field_value(field: ReviewField | None, name: str, value: Any) -> Any:
    """A field's value as shown publicly; a secret field never shows its value."""
    if field is not None and field.is_secret and value is not None:
        return _REDACTED
    return masked_review_value(value, key=name)


def public_review_changes(
    review: Mapping[str, Any], changes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """What a person changed, masked as the public review is, for the decision history.

    The history is kept in the job and shown to whoever may read the job, so it
    never holds a value the public review would mask. Only the interrupt
    payload, which never leaves the server, keeps the captured values raw: the
    decision is checked against them.
    """
    by_name = {field.name: field for field in _captured_fields(review)}
    rows: list[dict[str, Any]] = []
    for change in changes:
        name = str(change.get("field") or "")
        field = by_name.get(name)
        rows.append(
            {
                "field": name,
                "before": _masked_field_value(field, name, change.get("before")),
                "after": _masked_field_value(field, name, change.get("after")),
            }
        )
    return rows


def original_references(review: Mapping[str, Any]) -> list[Any]:
    """The captured original as a list of file references."""
    original = review.get("original")
    if original is None:
        return []
    return list(original) if isinstance(original, list) else [original]


#: What the decision history keeps of a field: enough to show a value by its type,
#: a list of objects as a table with its columns included.
REVIEW_DECISION_FIELD_KEYS = ("name", "label", "type", "format", "items", "properties")


def _public_fields(review: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The captured fields a person may see: never a secret one."""
    fields = []
    for field in _captured_fields(review):
        if field.is_secret:
            continue
        dumped = field.dump()
        row: dict[str, Any] = {key: dumped.get(key) for key in _PUBLIC_FIELD_KEYS}
        row["label"] = field.public_label()
        row["required"] = bool(field.required)
        row["editable"] = bool(field.editable)
        row["options"] = list(field.options)
        row["description"] = field.description
        row["format"] = field.format or None
        fields.append(row)
    return fields


def public_review_decision(review: Mapping[str, Any], values: Mapping[str, Any]) -> dict[str, Any]:
    """The accepted values of an approved review with their fields, masked, for the history.

    The operator's view of a finished job reads it back: what was accepted,
    under which labels. Secret fields are left out, as in the public review.
    """
    fields: list[dict[str, Any]] = [
        {key: row.get(key) for key in REVIEW_DECISION_FIELD_KEYS} for row in _public_fields(review)
    ]
    names = [str(row["name"]) for row in fields]
    return {
        "fields": fields,
        "values": {
            name: masked_review_value(values.get(name), key=name)
            for name in names
            if name in values
        },
    }


def public_review(
    review: Mapping[str, Any] | None,
    *,
    original: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """What a person who may see the job is shown: fields, values and the original.

    ``original`` is the caller's authorized artifact summaries of
    ``original_references``; references it could not authorize are not shown.
    """
    from core.application.jobs.public_secret_redaction import redact_public_secrets

    if not isinstance(review, Mapping):
        return None
    fields = _public_fields(review)
    names = {row["name"] for row in fields}
    raw_values = review.get("values")
    values = raw_values if isinstance(raw_values, Mapping) else {}
    raw_invalid = review.get("invalid")
    invalid = raw_invalid if isinstance(raw_invalid, Mapping) else {}
    return {
        "fields": fields,
        "values": {
            name: masked_review_value(values.get(name), key=name)
            for name in names
            if name in values
        },
        "invalid": {
            str(name): redact_public_secrets(str(text))
            for name, text in invalid.items()
            if name in names
        },
        "original": list(original or []),
    }


__all__ = [
    "DEFAULT_REVIEW_FIELD_TYPE",
    "ORIGINAL_SOURCE_TYPES",
    "RESERVED_REVIEW_FIELD_NAMES",
    "REVIEW_CONFIG_KEY",
    "REVIEW_PAYLOAD_KEY",
    "REVIEW_VALUES_MAX_BYTES",
    "ReviewDecisionError",
    "ReviewField",
    "captured_review",
    "decide_review",
    "masked_review_value",
    "open_review",
    "original_references",
    "parse_review_field",
    "public_review",
    "public_review_changes",
    "public_review_decision",
    "review_config",
    "review_config_issues",
    "review_fields",
    "review_source_issues",
]
