"""Run titles: say which run a job is without opening its inputs.

One workflow often runs for several purposes (three schedules of one scraper,
each for another supplier site). A job is then named by what started it and by
what it was started for:

- the *run label* is the name of the schedule, or the description of the
  incoming webhook, that started the job (``meta.run_label``);
- the workflow may declare ``settings.run_title``, a template over its inputs
  such as ``"{{ inputs.site_url }}"``, rendered for every new run (UI, schedule,
  webhook, Invoke child, MCP and API).

The job's ``meta.run_title`` is ``"<label> · <rendered>"`` when both exist,
else whichever exists, else absent. Only the host writes it: a value a caller
put there is dropped. Rendering is plain text substitution, never evaluation;
a secret input, a secret reference or a value that is not a scalar renders as
nothing, and every part is bounded.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from typing import Any

logger = logging.getLogger(__name__)

RUN_TITLE_SETTING = "run_title"
RUN_TITLE_META_KEY = "run_title"
RUN_LABEL_META_KEY = "run_label"
RUN_TITLE_SEPARATOR = " · "
RUN_TITLE_MAX_CHARS = 160
RUN_LABEL_MAX_CHARS = 120
RUN_TITLE_TEMPLATE_MAX_CHARS = 300
RUN_TITLE_VALUE_MAX_CHARS = 120

_PLACEHOLDER = re.compile(r"\{\{(.*?)\}\}")
_INPUT_REFERENCE = re.compile(r"^inputs\.([A-Za-z_][A-Za-z0-9_]{0,127})$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

WorkflowLoader = Callable[[str, str], Any]


def bounded_text(value: Any, limit: int) -> str:
    """One line of plain text of at most *limit* characters."""
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    text = " ".join(_CONTROL.sub(" ", str(value)).split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def run_title_template(settings: Any) -> str:
    """The workflow's declared template, or "" when it declares none."""
    if not isinstance(settings, Mapping):
        return ""
    raw = settings.get(RUN_TITLE_SETTING)
    return raw.strip() if isinstance(raw, str) else ""


def _placeholder_names(template: str) -> list[tuple[str, str | None]]:
    """Each placeholder's raw text and the input it names (None when malformed)."""
    found: list[tuple[str, str | None]] = []
    for match in _PLACEHOLDER.finditer(template):
        expression = match.group(1).strip()
        reference = _INPUT_REFERENCE.match(expression)
        found.append((expression, reference.group(1) if reference else None))
    return found


def _secret_input_names(interface: Any) -> set[str]:
    return {
        str(row.name or "").strip()
        for row in getattr(interface, "expected_inputs", None) or []
        if getattr(row, "is_secret", False) and str(row.name or "").strip()
    }


def _declared_input_names(interface: Any) -> set[str] | None:
    """The names a template may read, or None when the workflow accepts any input."""
    mode = str(getattr(interface, "input_mode", "") or "fields")
    if mode == "passthrough":
        return None
    if mode == "json_schema":
        schema = getattr(interface, "input_schema", None) or {}
        properties = schema.get("properties") if isinstance(schema, Mapping) else None
        return {str(name) for name in properties} if isinstance(properties, Mapping) else None
    return {
        str(row.name or "").strip()
        for row in getattr(interface, "expected_inputs", None) or []
        if str(row.name or "").strip()
    }


def _issue(code: str, message: str) -> dict[str, str]:
    return {
        "code": code,
        "message": message,
        "path": f"workflow.settings.{RUN_TITLE_SETTING}",
        "severity": "error",
    }


def run_title_issues(settings: Any, interface: Any) -> list[dict[str, str]]:
    """Problems of ``settings.run_title`` against the workflow's Interface."""
    if not isinstance(settings, Mapping) or RUN_TITLE_SETTING not in settings:
        return []
    raw = settings.get(RUN_TITLE_SETTING)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return []
    if not isinstance(raw, str):
        return [_issue("workflow.run_title.not_text", "The run title must be text.")]
    template = raw.strip()
    if len(template) > RUN_TITLE_TEMPLATE_MAX_CHARS:
        return [
            _issue(
                "workflow.run_title.too_long",
                f"The run title is longer than {RUN_TITLE_TEMPLATE_MAX_CHARS} characters.",
            )
        ]
    from core.application.jobs.job_invocation_artifacts import is_secret_like_input

    declared = _declared_input_names(interface)
    secret = _secret_input_names(interface)
    issues: list[dict[str, str]] = []
    for expression, name in _placeholder_names(template):
        if name is None:
            issues.append(
                _issue(
                    "workflow.run_title.placeholder_invalid",
                    f"'{{{{ {expression} }}}}' is not an input; write {{{{ inputs.<name> }}}}.",
                )
            )
        elif name in secret or is_secret_like_input(name, secret):
            issues.append(
                _issue(
                    "workflow.run_title.secret_input",
                    f"The run title cannot show the secret input '{name}'.",
                )
            )
        elif declared is not None and name not in declared:
            issues.append(
                _issue(
                    "workflow.run_title.unknown_input",
                    f"The run title reads '{name}', which is not a declared input.",
                )
            )
    return issues


def _rendered_value(value: Any) -> str:
    from core.infrastructure.security.secret_reference import is_secret_reference

    if is_secret_reference(value) or isinstance(value, (dict, list, tuple, set)):
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return bounded_text(value, RUN_TITLE_VALUE_MAX_CHARS)
    return ""


def render_run_title(
    template: str, inputs: Mapping[str, Any] | None, *, secret_inputs: set[str] | None = None
) -> str:
    """The template with each ``{{ inputs.<name> }}`` replaced by that input's text."""
    from core.application.jobs.job_invocation_artifacts import is_secret_like_input

    clean_template = template.strip() if isinstance(template, str) else ""
    if not clean_template or len(clean_template) > RUN_TITLE_TEMPLATE_MAX_CHARS:
        return ""
    values = inputs if isinstance(inputs, Mapping) else {}
    secret = set(secret_inputs or ())

    def substitute(match: re.Match[str]) -> str:
        reference = _INPUT_REFERENCE.match(match.group(1).strip())
        if reference is None:
            return ""
        name = reference.group(1)
        if name in secret or is_secret_like_input(name, secret):
            return ""
        return _rendered_value(values.get(name))

    rendered = bounded_text(_PLACEHOLDER.sub(substitute, clean_template), RUN_TITLE_MAX_CHARS)
    # A template whose every value is empty leaves only punctuation behind.
    return rendered if any(char.isalnum() for char in rendered) else ""


def compose_run_title(label: Any, rendered: Any) -> str:
    """``"<label> · <rendered>"``, or the one that exists, bounded."""
    parts = [
        bounded_text(label, RUN_LABEL_MAX_CHARS),
        bounded_text(rendered, RUN_TITLE_MAX_CHARS),
    ]
    present = [part for part in parts if part]
    if len(present) == 2 and present[0] == present[1]:
        present = present[:1]
    return bounded_text(
        RUN_TITLE_SEPARATOR.join(present), RUN_LABEL_MAX_CHARS + RUN_TITLE_MAX_CHARS
    )


def _running_registry_workflow(workflow_id: str, account_id: str) -> Any:
    """The workflow row from the registry this process already runs, never a new one."""
    from core.application.registry.registry_manager import RegistryManager
    from core.application.registry.workflow_lookup import get_workflow_row

    registry = RegistryManager.current()
    db = getattr(registry, "db_store", None) if registry is not None else None
    if db is None:
        return None
    kwargs = {"account_id": account_id} if account_id else {}
    return get_workflow_row(db, workflow_id, **kwargs)


def _rendered_for_workflow(
    workflow_id: str, account_id: str, inputs: Any, load_workflow: WorkflowLoader | None
) -> str:
    if not workflow_id:
        return ""
    try:
        row = (load_workflow or _running_registry_workflow)(workflow_id, account_id)
        if not isinstance(row, Mapping):
            return ""
        template = run_title_template(row.get("settings"))
        if not template:
            return ""
        from core.application.registry.workflow_contract import interface_from_definition

        interface = interface_from_definition(row)
        return render_run_title(
            template,
            inputs if isinstance(inputs, Mapping) else {},
            secret_inputs=_secret_input_names(interface),
        )
    except Exception:
        # A title never decides whether a run starts.
        logger.debug("Run title not rendered for workflow_id=%s", workflow_id, exc_info=True)
        return ""


def stamp_run_title(
    meta: dict[str, Any],
    *,
    workflow_id: Any,
    account_id: Any,
    inputs: Any,
    load_workflow: WorkflowLoader | None = None,
) -> dict[str, Any]:
    """Write the job's run title into *meta* (returned); drop a caller-supplied one."""
    meta.pop(RUN_TITLE_META_KEY, None)
    label = bounded_text(meta.get(RUN_LABEL_META_KEY), RUN_LABEL_MAX_CHARS)
    if label:
        meta[RUN_LABEL_META_KEY] = label
    else:
        meta.pop(RUN_LABEL_META_KEY, None)
    rendered = _rendered_for_workflow(
        str(workflow_id or "").strip(), str(account_id or "").strip(), inputs, load_workflow
    )
    title = compose_run_title(label, rendered)
    if title:
        meta[RUN_TITLE_META_KEY] = title
    return meta


def job_run_title(row: Any) -> str:
    """A job row's run title, read from a projected column or from its ``meta``."""
    if not isinstance(row, Mapping):
        return ""
    direct = row.get(RUN_TITLE_META_KEY)
    if isinstance(direct, str) and direct.strip():
        return bounded_text(direct, RUN_LABEL_MAX_CHARS + RUN_TITLE_MAX_CHARS)
    meta = row.get("meta")
    if isinstance(meta, str):
        import json

        try:
            meta = json.loads(meta)
        except ValueError:
            return ""
    if not isinstance(meta, Mapping):
        return ""
    return bounded_text(meta.get(RUN_TITLE_META_KEY), RUN_LABEL_MAX_CHARS + RUN_TITLE_MAX_CHARS)


__all__ = [
    "RUN_LABEL_MAX_CHARS",
    "RUN_LABEL_META_KEY",
    "RUN_TITLE_MAX_CHARS",
    "RUN_TITLE_META_KEY",
    "RUN_TITLE_SEPARATOR",
    "RUN_TITLE_SETTING",
    "RUN_TITLE_TEMPLATE_MAX_CHARS",
    "bounded_text",
    "compose_run_title",
    "job_run_title",
    "render_run_title",
    "run_title_issues",
    "run_title_template",
    "stamp_run_title",
]
