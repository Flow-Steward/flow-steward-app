"""Files a workflow returns, derived from its steps' saved result settings.

A step's file result carries the "Return from workflow" setting in
``override.returned_files``: one entry per ticked result, ``{"result": <the
step's result id>, "key": <stable output key>}``. The workflow's returned files
(its file outputs) are derived from those entries; nothing else declares them.

The key is created once, when the result is ticked, and never depends on the
file name, a timestamp, or the file of a particular run. A parent workflow maps
that key, so a child that writes ``report-2026-10-22T10:00.pdf`` on one run and
``report-2026-10-23T09:12.pdf`` on the next returns both through the same
output.

A workflow saved before this setting existed declared its file outputs by hand
under ``settings.callable.expected_artifacts``. Each such declaration is linked
to the one step result it can only mean (the result written to ``state.<name>``,
or the step output called ``<name>``); a declaration that matches no result or
more than one is a setup issue, never a guess.
"""

from __future__ import annotations

import copy
import dataclasses
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from core.application.orchestration.state_path_guards import canonical_state_write_parts
from core.application.registry.workflow_contract import (
    RESERVED_CONTRACT_NAMES,
    coerce_bool,
    json_object,
)

RETURNED_FILES_FIELD = "returned_files"
ORIGIN_STEP = "step"
ORIGIN_LEGACY = "legacy"
STATE_TARGET_PREFIX = "state."

#: The result id of the one file a step kind produces.
FILE_FROM_BASE64_RESULT = "file"
HTTP_RESPONSE_RESULT = "response"


@dataclass(frozen=True, slots=True)
class StepFileResult:
    """A result a step writes to the workflow state that can be returned."""

    step_id: str
    step_name: str
    result: str
    target: str


@dataclass(frozen=True, slots=True)
class ReturnedFile:
    """One file output of the workflow, bound to the step result it comes from."""

    key: str
    step_id: str
    step_name: str
    result: str
    target: str
    required: bool = False
    origin: str = ORIGIN_STEP

    def contract_row(self) -> dict[str, Any]:
        """The Outputs row: a file output and the step result it is read from."""
        row: dict[str, Any] = {
            "name": self.key,
            "type": "file",
            "required": bool(self.required),
            "source": {
                "step_id": self.step_id,
                "step_name": self.step_name,
                "result": self.result,
                "target": self.target,
            },
        }
        if self.origin != ORIGIN_STEP:
            row["origin"] = self.origin
        return row


def _text(value: Any) -> str:
    return str(value or "").strip()


def _override(step: Mapping[str, Any]) -> dict[str, Any]:
    return json_object(step.get("override"))


def _state_target(value: Any) -> str:
    target = _text(value)
    if not target.startswith(STATE_TARGET_PREFIX):
        return ""
    parts = canonical_state_write_parts(target)
    return target if parts else ""


def _step_output_mapping(step: Mapping[str, Any], kind: str) -> dict[str, Any]:
    override = _override(step)
    step_mapping = json_object(step.get("output_mapping_jsonb")) or json_object(
        step.get("output_mapping")
    )
    if step_mapping:
        return step_mapping
    kind_override = json_object(override.get(kind))
    return json_object(kind_override.get("output_mapping_jsonb")) or json_object(
        kind_override.get("output_mapping")
    )


def step_file_results(step: Mapping[str, Any]) -> list[StepFileResult]:
    """The results of *step* that hold a file and are written to workflow state.

    A connector or agent output is offered by the editor only when its declared
    type is a file; here every mapped output qualifies, because the declared type
    lives in the extension catalog, not in the step.
    """
    step_id = _text(step.get("step_id") or step.get("id"))
    if not step_id:
        return []
    step_name = _text(step.get("display_name")) or step_id
    kind = _text(step.get("step_kind")).lower() or "agent"
    override = _override(step)
    rows: list[tuple[str, str]] = []
    if kind == "file_from_base64":
        config = json_object(override.get("file_from_base64"))
        rows.append((FILE_FROM_BASE64_RESULT, _state_target(config.get("result_target"))))
    elif kind == "http_request":
        config = json_object(override.get("http_request"))
        if _text(config.get("response_mode")).lower() == "artifact":
            rows.append((HTTP_RESPONSE_RESULT, _state_target(config.get("response_target"))))
    elif kind == "invoke_workflow":
        config = json_object(override.get("invoke_workflow"))
        for name, target in json_object(config.get("artifact_mapping")).items():
            rows.append((_text(name), _state_target(target)))
    elif kind in {"connector", "agent"}:
        for name, target in _step_output_mapping(step, kind).items():
            rows.append((_text(name), _state_target(target)))
    return [
        StepFileResult(step_id=step_id, step_name=step_name, result=result, target=target)
        for result, target in rows
        if result and target
    ]


def step_returned_file_settings(step: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    """The step's saved "Return from workflow" entries; ``None`` if never set."""
    override = _override(step)
    if RETURNED_FILES_FIELD not in override:
        return None
    raw = override.get(RETURNED_FILES_FIELD)
    if not isinstance(raw, list):
        return []
    entries: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        result = _text(item.get("result"))
        if not result:
            continue
        entry: dict[str, Any] = {"result": result, "key": _text(item.get("key"))}
        if coerce_bool(item.get("required"), default=False):
            entry["required"] = True
        entries.append(entry)
    return entries


def _returned_file_settings_issues(step: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Validate the raw returned-file declaration before permissive normalization."""
    override = step.get("override")
    if not isinstance(override, Mapping) or RETURNED_FILES_FIELD not in override:
        return []

    step_id = _text(step.get("step_id") or step.get("id"))
    base_path = f"workflow.steps.{step_id}.override.{RETURNED_FILES_FIELD}"
    raw = override[RETURNED_FILES_FIELD]
    if not isinstance(raw, list):
        return [
            _issue(
                "workflow.interface.returned_file_declaration_invalid",
                "Returned files must be a list of result declarations",
                base_path,
            )
        ]

    issues: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        path = f"{base_path}[{index}]"
        if not isinstance(item, Mapping):
            issues.append(
                _issue(
                    "workflow.interface.returned_file_declaration_invalid",
                    "Returned file declaration must be an object",
                    path,
                )
            )
            continue
        result = item.get("result")
        if not isinstance(result, str) or not result.strip():
            issues.append(
                _issue(
                    "workflow.interface.returned_file_declaration_invalid",
                    "Returned file declaration must name a non-empty result",
                    f"{path}.result",
                )
            )
        if "key" in item and not isinstance(item["key"], str):
            issues.append(
                _issue(
                    "workflow.interface.returned_file_declaration_invalid",
                    "Returned file key must be a string when provided",
                    f"{path}.key",
                )
            )
    return issues


_KEY_CHARS = re.compile(r"[^A-Za-z0-9_]+")


def default_returned_file_key(result: StepFileResult) -> str:
    """The key a newly ticked result gets: its variable name, made safe."""
    parts = canonical_state_write_parts(result.target) or ()
    base = _KEY_CHARS.sub("_", parts[-1] if parts else result.result).strip("_")
    if not base or base in RESERVED_CONTRACT_NAMES:
        base = f"{base or 'file'}_file"
    return base


def _issue(code: str, message: str, path: str, severity: str = "error") -> dict[str, Any]:
    return {"code": code, "message": message, "path": path, "severity": severity}


def _iter_steps(steps: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(steps, Mapping):
        steps = list(steps.values())
    if not isinstance(steps, (list, tuple)):
        return []
    rows: list[Mapping[str, Any]] = []
    for step in steps:
        if isinstance(step, Mapping):
            rows.append(step)
        elif dataclasses.is_dataclass(step) and not isinstance(step, type):
            # A registry manifest's StepConfig carries the same fields.
            rows.append(dataclasses.asdict(step))
    return rows


def derive_returned_files(
    steps: Any,
    *,
    legacy_declarations: Iterable[Mapping[str, Any]] = (),
    taken_names: Iterable[str] = (),
) -> tuple[list[ReturnedFile], list[dict[str, Any]]]:
    """The workflow's returned files and the setup issues found deriving them.

    *taken_names* are other output names (the variable outputs), which a file
    output may not share.
    """
    issues: list[dict[str, Any]] = []
    returned: list[ReturnedFile] = []
    used: set[str] = {name for name in taken_names if name}
    unconverted: list[StepFileResult] = []
    for step in _iter_steps(steps):
        results = step_file_results(step)
        declaration_issues = _returned_file_settings_issues(step)
        if declaration_issues:
            issues.extend(declaration_issues)
            continue
        settings = step_returned_file_settings(step)
        if settings is None:
            unconverted.extend(results)
            continue
        by_result = {row.result: row for row in results}
        step_id = _text(step.get("step_id") or step.get("id"))
        for index, entry in enumerate(settings):
            path = f"workflow.steps.{step_id}.override.{RETURNED_FILES_FIELD}[{index}]"
            source = by_result.get(entry["result"])
            if source is None:
                issues.append(
                    _issue(
                        "workflow.interface.returned_file_source_missing",
                        f"Step '{step_id}' returns result '{entry['result']}', "
                        "which the step no longer saves as a file",
                        path,
                    )
                )
                continue
            key = entry["key"] or default_returned_file_key(source)
            if any(ch.isspace() for ch in key) or key in RESERVED_CONTRACT_NAMES:
                issues.append(
                    _issue(
                        "workflow.interface.returned_file_key_invalid",
                        f"Returned file key '{key}' is not a valid output name",
                        f"{path}.key",
                    )
                )
                continue
            if key in used:
                issues.append(
                    _issue(
                        "workflow.interface.duplicate_name",
                        f"Returned file '{key}' has the same name as another output",
                        f"{path}.key",
                    )
                )
                continue
            used.add(key)
            returned.append(
                ReturnedFile(
                    key=key,
                    step_id=source.step_id,
                    step_name=source.step_name,
                    result=source.result,
                    target=source.target,
                    required=bool(entry.get("required")),
                )
            )
    for index, declaration in enumerate(legacy_declarations):
        name = _text(declaration.get("name"))
        if not name or name in used:
            # Already returned under that key: the declaration is satisfied.
            continue
        path = f"workflow.callable.expected_artifacts[{index}]"
        candidates = {
            (row.step_id, row.result): row
            for row in unconverted
            if row.result == name or (canonical_state_write_parts(row.target) or ()) == (name,)
        }
        required = coerce_bool(declaration.get("required"), default=False)
        if len(candidates) == 1:
            source = next(iter(candidates.values()))
            used.add(name)
            returned.append(
                ReturnedFile(
                    key=name,
                    step_id=source.step_id,
                    step_name=source.step_name,
                    result=source.result,
                    target=source.target,
                    required=required,
                    origin=ORIGIN_LEGACY,
                )
            )
            continue
        if candidates:
            steps_text = ", ".join(sorted({row.step_id for row in candidates.values()}))
            issues.append(
                _issue(
                    "workflow.interface.legacy_artifact_ambiguous",
                    f"Returned file '{name}' could come from more than one step ({steps_text}). "
                    "Tick “Return from workflow” on the result to return",
                    path,
                )
            )
            continue
        issues.append(
            _issue(
                "workflow.interface.legacy_artifact_unlinked",
                f"Returned file '{name}' matches no step result. Tick “Return from "
                "workflow” on the result to return",
                path,
                severity="error" if required else "warning",
            )
        )
    return returned, issues


def returned_files_for_step(returned: Iterable[ReturnedFile], step_id: str) -> list[ReturnedFile]:
    return [row for row in returned if row.step_id == step_id]


def _value_at(container: Any, parts: tuple[str, ...]) -> tuple[bool, Any]:
    cursor = container
    for part in parts:
        if not isinstance(cursor, Mapping) or part not in cursor:
            return False, None
        cursor = cursor[part]
    return True, cursor


def written_value(written: Mapping[str, Any], path: str) -> tuple[bool, Any]:
    """The value a step's state update (*written*) holds at *path*, if it wrote one. Reads only."""
    parts = canonical_state_write_parts(path)
    if not parts:
        return False, None
    if parts[0] != "workflow_data":
        found, value = _value_at(written.get("workflow_data"), parts)
        if found:
            return True, value
    return _value_at(written, parts)


def _is_produced(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict)):
        return bool(value)
    return True


def captured_returned_files(
    *,
    state: Mapping[str, Any],
    update: Mapping[str, Any],
    returned: Iterable[ReturnedFile],
) -> dict[str, Any] | None:
    """The run's returned files after a step whose *update* produced some.

    Each value is stored under its stable key, so two steps never overwrite
    each other, and a collection is kept whole. ``None`` when nothing changed.
    """
    captured: dict[str, Any] = {}
    for row in returned:
        found, value = written_value(update, row.target)
        if found and _is_produced(value):
            captured[row.key] = copy.deepcopy(value)
    if not captured:
        return None
    current = update.get("artifacts")
    if not isinstance(current, Mapping):
        current = state.get("artifacts")
    merged = dict(current) if isinstance(current, Mapping) else {}
    merged.update(captured)
    return merged


__all__ = [
    "FILE_FROM_BASE64_RESULT",
    "HTTP_RESPONSE_RESULT",
    "ORIGIN_LEGACY",
    "ORIGIN_STEP",
    "RETURNED_FILES_FIELD",
    "ReturnedFile",
    "StepFileResult",
    "captured_returned_files",
    "default_returned_file_key",
    "derive_returned_files",
    "returned_files_for_step",
    "step_file_results",
    "step_returned_file_settings",
    "written_value",
]
