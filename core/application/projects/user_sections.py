"""Project user sections (ADR 0075): a configured form and history over one workflow.

A section stores presentation only: which contract fields the form shows and how,
which values the history table shows, and how a submission becomes runs. Types,
requirements and allowed values stay in the workflow's canonical contract; the
section refers to them by path and never copies them.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_SECTION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,127}(?:\[\])?$")

MAX_SECTIONS = 50
MAX_FIELDS = 100
MAX_COLUMNS = 30

#: Properties of the job itself a column may show.
JOB_COLUMN_PATHS: frozenset[str] = frozenset(
    {"job_id", "status", "created_at", "started_at", "finished_at", "duration_ms", "submitted_by"}
)
COLUMN_FORMATS: frozenset[str] = frozenset(
    {"auto", "text", "number", "currency", "date", "datetime", "status", "file", "count", "list"}
)

#: The widgets a field kind may use; the first is the default.
WIDGETS_BY_KIND: dict[str, tuple[str, ...]] = {
    "text": ("input", "textarea"),
    "text_long": ("textarea", "input"),
    "enum": ("select", "radio"),
    "number": ("number",),
    "boolean": ("checkbox", "switch"),
    "file": ("file",),
    "string_list": ("list", "tags"),
    "enum_list": ("multiselect", "list"),
    "number_list": ("list",),
    "object": ("group",),
    "object_list": ("repeater",),
}
#: Kinds whose single element a For each submission can send to one run.
_FOREACH_ELEMENT_KINDS = {"string_list", "enum_list", "number_list", "object_list"}


class SectionField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=512)
    label: str = Field(default="", max_length=200)
    help_text: str = Field(default="", max_length=1000)
    widget: str = Field(default="", max_length=32)
    order: int = 0
    visible: bool = True


class SectionColumn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    source: Literal["job", "input", "output"]
    path: str = Field(min_length=1, max_length=512)
    label: str = Field(default="", max_length=200)
    format: str = Field(default="auto", max_length=32)
    order: int = 0


class SectionSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["foreach", "bulk"] = "bulk"
    #: For each: the top-level input whose values become one run each.
    target_input: str = Field(default="", max_length=128)


class ProjectUserSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    icon: str = Field(default="", max_length=64)
    workflow_id: str = Field(min_length=1, max_length=255)
    visible: bool = True
    order: int = 0
    fields: list[SectionField] = Field(default_factory=list, max_length=MAX_FIELDS)
    columns: list[SectionColumn] = Field(default_factory=list, max_length=MAX_COLUMNS)
    submission: SectionSubmission = Field(default_factory=SectionSubmission)
    #: The admin confirmed that a submission runs the workflow with real effects.
    effects_acknowledged: bool = False

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        text = str(value or "").strip()
        if not _SECTION_ID_RE.fullmatch(text):
            raise ValueError("Section id must be 1-64 lowercase letters, digits, '-' or '_'")
        return text

    @field_validator("title", "workflow_id")
    @classmethod
    def _stripped(cls, value: str) -> str:
        return str(value or "").strip()


class SectionIssue(BaseModel):
    code: str
    message: str
    path: str = ""
    severity: Literal["error", "warning"] = "error"


class SectionContract(BaseModel):
    """The parts of a workflow a section depends on, read from the registry."""

    workflow_id: str
    found: bool = True
    name: str = ""
    revision: int = 0
    enabled: bool = True
    linked: bool = True
    ui_channel: bool = True
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    #: Names of top-level inputs that hold credentials; never a form field or column.
    secret_inputs: list[str] = Field(default_factory=list)


class SectionRuntimeView(BaseModel):
    workflow_name: str = ""
    workflow_revision: int = 0
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    runnable: bool = False
    issues: list[SectionIssue] = Field(default_factory=list)


class ProjectUserSectionView(ProjectUserSection):
    runtime: SectionRuntimeView = Field(default_factory=SectionRuntimeView)


class ProjectUserSectionBrief(BaseModel):
    """What the navigation needs: no contract, no configuration detail."""

    id: str
    title: str
    icon: str = ""
    workflow_id: str
    order: int = 0


def parse_sections(raw: Any) -> list[ProjectUserSection]:
    """Stored or submitted sections, structurally validated; unknown keys refused."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("user_sections must be a list")
    if len(raw) > MAX_SECTIONS:
        raise ValueError(f"A project holds at most {MAX_SECTIONS} sections")
    return [ProjectUserSection.model_validate(item) for item in raw]


def stored_sections(config: Any) -> list[ProjectUserSection]:
    """The sections kept in `projects.config`; an unreadable entry is skipped."""
    raw = config.get("user_sections") if isinstance(config, dict) else None
    sections: list[ProjectUserSection] = []
    for item in raw if isinstance(raw, list) else []:
        try:
            sections.append(ProjectUserSection.model_validate(item))
        except ValueError:
            continue
    return sections


def field_kind(schema: Any) -> str:
    """The form kind a JSON Schema node supports, or "" when the form cannot render it."""
    if not isinstance(schema, dict) or not schema:
        return ""
    if schema.get("x-fs-type") == "file":
        return "file"
    if schema.get("writeOnly") or schema.get("x-fs-type") == "secret":
        return ""
    node_type = schema.get("type")
    if not isinstance(node_type, str):
        # A union such as ["string", "null"], or no single type at all: the form
        # supports one declared type per field.
        return ""
    if node_type == "string":
        if schema.get("enum"):
            return "enum"
        return "text_long" if schema.get("format") == "textarea" else "text"
    if node_type in {"number", "integer"}:
        return "number"
    if node_type == "boolean":
        return "boolean"
    if node_type == "object":
        properties = schema.get("properties")
        return "object" if isinstance(properties, dict) and properties else ""
    if node_type == "array":
        item_kind = field_kind(schema.get("items"))
        return {
            "text": "string_list",
            "text_long": "string_list",
            "enum": "enum_list",
            "number": "number_list",
            "object": "object_list",
            "file": "file_list",
        }.get(item_kind, "")
    return ""


def resolve_schema_path(schema: Any, path: str) -> dict[str, Any] | None:
    """The schema node at a dotted path; `name[]` steps into an array's items."""
    node: Any = schema
    for segment in str(path or "").split("."):
        if not _PATH_SEGMENT_RE.fullmatch(segment):
            return None
        name, into_items = (segment[:-2], True) if segment.endswith("[]") else (segment, False)
        properties = node.get("properties") if isinstance(node, dict) else None
        if not isinstance(properties, dict) or name not in properties:
            return None
        node = properties[name]
        if into_items:
            node = node.get("items") if isinstance(node, dict) else None
    return node if isinstance(node, dict) else None


def _unsupported_field_message(path: str, node: dict[str, Any], kind: str) -> str:
    if kind == "file_list":
        return (
            f"'{path}' is a list of files in one input, which the form cannot upload yet; "
            "use a single file input with For each"
        )
    node_type = node.get("type")
    if isinstance(node_type, list):
        names = ", ".join(str(item) for item in node_type)
        return (
            f"The form cannot show '{path}': its type is a union ({names}); "
            "declare one type in the workflow contract"
        )
    return (
        f"The form cannot show '{path}': describe its properties or items "
        "in the workflow contract (JSON Schema mode)"
    )


def _required_top_level(schema: dict[str, Any]) -> list[str]:
    required = schema.get("required")
    return [str(name) for name in required] if isinstance(required, list) else []


def _top_level_names(schema: dict[str, Any]) -> list[str]:
    properties = schema.get("properties")
    return list(properties) if isinstance(properties, dict) else []


def _field_issues(
    section: ProjectUserSection, contract: SectionContract, prefix: str
) -> list[SectionIssue]:
    issues: list[SectionIssue] = []
    schema = contract.input_schema
    secret_inputs = set(contract.secret_inputs)
    seen: set[str] = set()
    for index, field in enumerate(section.fields):
        path = f"{prefix}.fields[{index}]"
        if field.path in seen:
            issues.append(
                SectionIssue(code="field_duplicate", message="The field is listed twice", path=path)
            )
        seen.add(field.path)
        if field.path.split(".")[0].removesuffix("[]") in secret_inputs:
            issues.append(
                SectionIssue(
                    code="field_secret",
                    message="A credential input cannot be a form field; it keeps its configured secret",
                    path=path,
                )
            )
            continue
        node = resolve_schema_path(schema, field.path)
        if node is None:
            issues.append(
                SectionIssue(
                    code="field_unknown",
                    message=f"The workflow contract has no input '{field.path}'",
                    path=path,
                )
            )
            continue
        kind = field_kind(node)
        if kind not in WIDGETS_BY_KIND:
            # Includes a list of files in one input, visible or hidden: the form
            # cannot upload it yet, so the configuration is refused, not skipped.
            issues.append(
                SectionIssue(
                    code="field_unsupported",
                    message=_unsupported_field_message(field.path, node, kind),
                    path=path,
                )
            )
            continue
        if field.widget and field.widget not in WIDGETS_BY_KIND[kind]:
            issues.append(
                SectionIssue(
                    code="field_widget",
                    message=f"Widget '{field.widget}' does not fit a {kind} field",
                    path=f"{path}.widget",
                )
            )
    configured = {field.path: field for field in section.fields}
    for name in _required_top_level(schema):
        if name in secret_inputs:
            continue
        field = configured.get(name)
        if field is not None and not field.visible:
            issues.append(
                SectionIssue(
                    code="field_required_hidden",
                    message=f"'{name}' is required and has no configured value, so it must stay visible",
                    path=f"{prefix}.fields",
                )
            )
        elif field is None and section.fields:
            issues.append(
                SectionIssue(
                    code="field_required_missing",
                    message=f"'{name}' is required; add it to the form",
                    path=f"{prefix}.fields",
                )
            )
        elif not field_kind(resolve_schema_path(schema, name) or {}):
            issues.append(
                SectionIssue(
                    code="field_unsupported",
                    message=f"The form cannot show the required input '{name}'",
                    path=f"{prefix}.fields",
                )
            )
    return issues


def _column_issues(
    section: ProjectUserSection, contract: SectionContract, prefix: str
) -> list[SectionIssue]:
    issues: list[SectionIssue] = []
    seen: set[str] = set()
    secret_inputs = set(contract.secret_inputs)
    for index, column in enumerate(section.columns):
        path = f"{prefix}.columns[{index}]"
        if column.id in seen:
            issues.append(
                SectionIssue(
                    code="column_duplicate", message="Column ids must be unique", path=path
                )
            )
        seen.add(column.id)
        if column.format not in COLUMN_FORMATS:
            issues.append(
                SectionIssue(
                    code="column_format", message=f"Unknown format '{column.format}'", path=path
                )
            )
        if column.source == "job":
            known = column.path in JOB_COLUMN_PATHS
        elif column.source == "input":
            known = (
                column.path.split(".")[0].removesuffix("[]") not in secret_inputs
                and resolve_schema_path(contract.input_schema, column.path) is not None
            )
        else:
            # Ordinary object/array outputs do not describe their insides, so only
            # declared top-level outputs are columns (ADR 0075).
            known = column.path in _top_level_names(contract.output_schema)
        if not known:
            issues.append(
                SectionIssue(
                    code="column_unknown",
                    message=f"'{column.path}' is not a {column.source} value this workflow declares",
                    path=path,
                )
            )
    return issues


def _submission_issues(
    section: ProjectUserSection, contract: SectionContract, prefix: str
) -> list[SectionIssue]:
    schema = contract.input_schema
    submission = section.submission
    path = f"{prefix}.submission"
    if submission.mode == "bulk":
        file_lists = [
            name
            for name in _top_level_names(schema)
            if field_kind(resolve_schema_path(schema, name) or {}) == "file_list"
        ]
        if file_lists:
            return [
                SectionIssue(
                    code="bulk_file_collection_unsupported",
                    message="Bulk cannot send several files in one input yet; use For each",
                    path=path,
                )
            ]
        return []
    target = submission.target_input
    node = resolve_schema_path(schema, target) if target and "." not in target else None
    if node is None:
        return [
            SectionIssue(
                code="foreach_target",
                message="For each needs a top-level input whose values become one run each",
                path=f"{path}.target_input",
            )
        ]
    kind = field_kind(node)
    if kind == "file":
        return []
    if kind not in _FOREACH_ELEMENT_KINDS:
        return [
            SectionIssue(
                code="foreach_target_kind",
                message=f"For each cannot split '{target}': choose a file input or a described list",
                path=f"{path}.target_input",
            )
        ]
    min_items = node.get("minItems")
    if isinstance(min_items, int) and min_items > 1:
        return [
            SectionIssue(
                code="foreach_min_items",
                message=f"'{target}' needs at least {min_items} items, so one item per run is invalid",
                path=f"{path}.target_input",
            )
        ]
    return []


def _availability_issues(contract: SectionContract, prefix: str) -> list[SectionIssue]:
    path = f"{prefix}.workflow_id"
    if not contract.found:
        return [
            SectionIssue(
                code="workflow_missing", message="The workflow no longer exists", path=path
            )
        ]
    issues: list[SectionIssue] = []
    if not contract.linked:
        issues.append(
            SectionIssue(
                code="workflow_unlinked",
                message="The workflow is not linked to this project",
                path=path,
            )
        )
    if not contract.enabled:
        issues.append(
            SectionIssue(code="workflow_disabled", message="The workflow is disabled", path=path)
        )
    if not contract.ui_channel:
        issues.append(
            SectionIssue(
                code="workflow_ui_channel",
                message="The workflow does not accept runs from the UI channel",
                path=path,
            )
        )
    return issues


def section_issues(
    section: ProjectUserSection, contract: SectionContract, *, index: int = 0
) -> list[SectionIssue]:
    prefix = f"user_sections[{index}]"
    availability = _availability_issues(contract, prefix)
    if not contract.found:
        return availability
    return [
        *availability,
        *_field_issues(section, contract, prefix),
        *_column_issues(section, contract, prefix),
        *_submission_issues(section, contract, prefix),
    ]


ContractLoader = Callable[[str], SectionContract]


def validate_sections(
    sections: Iterable[ProjectUserSection], *, load_contract: ContractLoader
) -> list[SectionIssue]:
    """Every problem that should stop an admin from saving the sections."""
    issues: list[SectionIssue] = []
    seen: set[str] = set()
    for index, section in enumerate(sections):
        if section.id in seen:
            issues.append(
                SectionIssue(
                    code="section_duplicate",
                    message=f"Section id '{section.id}' is used twice",
                    path=f"user_sections[{index}].id",
                )
            )
        seen.add(section.id)
        issues.extend(section_issues(section, load_contract(section.workflow_id), index=index))
    return [issue for issue in issues if issue.severity == "error"]


def resolve_sections(
    sections: Iterable[ProjectUserSection],
    *,
    load_contract: ContractLoader,
    include_hidden: bool,
) -> list[ProjectUserSectionView]:
    """Sections with the contract they render against and whether they can run now.

    A section that became incompatible stays readable: its history still shows,
    and `runnable` is false with the reasons.
    """
    views: list[ProjectUserSectionView] = []
    for index, section in enumerate(sorted(sections, key=lambda item: (item.order, item.title))):
        if not section.visible and not include_hidden:
            continue
        contract = load_contract(section.workflow_id)
        try:
            issues = section_issues(section, contract, index=index)
        except (TypeError, ValueError, AttributeError, KeyError):
            # A contract shape this check does not understand must not hide the
            # section or its history; the form stays blocked with a reason.
            issues = [
                SectionIssue(
                    code="section_unreadable",
                    message="The workflow contract has a shape the form does not support",
                    path=f"user_sections[{index}]",
                )
            ]
        views.append(
            ProjectUserSectionView(
                **section.model_dump(),
                runtime=SectionRuntimeView(
                    workflow_name=contract.name,
                    workflow_revision=contract.revision,
                    input_schema=contract.input_schema if contract.found else {},
                    output_schema=contract.output_schema if contract.found else {},
                    runnable=contract.found
                    and section.effects_acknowledged
                    and not any(issue.severity == "error" for issue in issues),
                    issues=issues,
                ),
            )
        )
    return views


def section_briefs(
    config: Any, *, allowed_workflow_ids: Iterable[str] = ()
) -> list[ProjectUserSectionBrief]:
    """Visible sections for navigation, without loading any workflow.

    A section whose workflow was unlinked or disabled stays in the menu: its page
    explains why the form is blocked and keeps the history readable (ADR 0075).
    Only the caller's scope hides it: a token limited to some workflows does not
    learn about sections of the others.
    """
    allowed = set(allowed_workflow_ids)
    return [
        ProjectUserSectionBrief(
            id=section.id,
            title=section.title,
            icon=section.icon,
            workflow_id=section.workflow_id,
            order=section.order,
        )
        for section in sorted(stored_sections(config), key=lambda item: (item.order, item.title))
        if section.visible and (not allowed or section.workflow_id in allowed)
    ]


__all__ = [
    "JOB_COLUMN_PATHS",
    "ProjectUserSection",
    "ProjectUserSectionBrief",
    "ProjectUserSectionView",
    "SectionColumn",
    "SectionContract",
    "SectionField",
    "SectionIssue",
    "SectionSubmission",
    "field_kind",
    "parse_sections",
    "resolve_schema_path",
    "resolve_sections",
    "section_briefs",
    "section_issues",
    "stored_sections",
    "validate_sections",
]
