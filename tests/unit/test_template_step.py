"""The Template step assembles one text from declared inputs with standard Jinja."""

from __future__ import annotations

from typing import Any

import pytest

from core.application.orchestration import template_step_runtime
from core.application.orchestration.template_step_runtime import (
    execute_template_step,
    validate_template_config,
)
from core.application.orchestration.workflow_state_selector_contract import (
    discover_step_state_selectors,
)
from core.infrastructure.security import jinja_sandbox


def _input(name: str, source: str, *, type_: str = "text", required: bool = True) -> dict:
    return {"name": name, "type": type_, "required": required, "source": source}


def _config(text: str, inputs: list[dict] | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "text": text,
        "inputs": inputs or [],
        "result_target": "state.outputs.text",
        **extra,
    }


def _render(text: str, values: dict[str, Any], **extra: Any) -> dict[str, Any]:
    inputs = [
        _input(name, f"state.outputs.{name}", type_=_type_of(value))
        for name, value in values.items()
    ]
    return execute_template_step(_config(text, inputs, **extra), {"outputs": values})


def _type_of(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return "text" if isinstance(value, str) else "json"


def _reason(result: dict[str, Any]) -> str:
    assert result["ok"] is False, result
    return str(result["failure_reason"])


# --- rendering -----------------------------------------------------------------------


def test_markdown_post_reads_inputs_by_local_name_with_a_loop_and_a_default() -> None:
    result = execute_template_step(
        _config(
            "# {{ header }}\n\n{{ body }}\n\n{% for image in images %}"
            "![{{ image.alt }}]({{ image.url }})\n{% endfor %}\n---\n"
            '{{ footer | default("") }}',
            [
                _input("header", "state.outputs.title"),
                _input("body", "state.outputs.draft.text"),
                _input("footer", "state.outputs.footer", required=False),
                _input("images", "state.outputs.images", type_="array"),
            ],
            format="markdown",
        ),
        {
            "outputs": {
                "title": "Spring",
                "draft": {"text": "New arrivals."},
                "images": [
                    {"url": "https://cdn.example.com/a.png", "alt": "A"},
                    {"url": "https://cdn.example.com/b.png", "alt": "B"},
                ],
            }
        },
    )

    assert result["ok"] is True
    assert result["text"] == (
        "# Spring\n\nNew arrivals.\n\n"
        "![A](https://cdn.example.com/a.png)\n![B](https://cdn.example.com/b.png)\n"
        "\n---\n"
    )
    assert result["format"] == "markdown"


def test_html_table_has_one_escaped_row_per_parameter_in_order() -> None:
    parameters = [
        {"name": "Weight", "value": "1 kg"},
        {"name": "Size <XL>", "value": "A & B"},
        {"name": "Quote", "value": "\"double\" 'single'"},
    ]
    result = _render(
        "<table>{% for item in parameters %}<tr><td>{{ item.name }}</td>"
        "<td>{{ item.value }}</td></tr>{% endfor %}</table>",
        {"parameters": parameters},
        format="html",
    )

    text = result["text"]
    assert text.count("<tr>") == 3
    assert text.index("Weight") < text.index("Size") < text.index("Quote")
    assert "<td>Size &lt;XL&gt;</td><td>A &amp; B</td>" in text
    assert "&#34;double&#34; &#39;single&#39;" in text


def test_html_keeps_a_fragment_marked_safe_and_escapes_everything_else() -> None:
    result = _render(
        "{{ trusted | safe }}|{{ untrusted }}",
        {"trusted": "<b>bold</b>", "untrusted": "<script>alert(1)</script>"},
        format="html",
    )

    assert result["text"] == "<b>bold</b>|&lt;script&gt;alert(1)&lt;/script&gt;"


@pytest.mark.parametrize("output_format", ["text", "markdown"])
def test_text_and_markdown_insert_values_without_html_escaping(output_format: str) -> None:
    result = _render("{{ value }}", {"value": "<b>a & b</b>"}, format=output_format)

    assert result["text"] == "<b>a & b</b>"


def test_condition_branches_on_typed_values() -> None:
    template = "{% if in_stock and count > 2 %}many{% elif in_stock %}few{% else %}none{% endif %}"

    assert _render(template, {"in_stock": True, "count": 5})["text"] == "many"
    assert _render(template, {"in_stock": True, "count": 1})["text"] == "few"
    assert _render(template, {"in_stock": False, "count": 9})["text"] == "none"


def test_a_value_holding_jinja_delimiters_is_inserted_not_rendered_again() -> None:
    result = _render("{{ note }}", {"note": "{{ 7 * 7 }} {% if x %}"})

    assert result["text"] == "{{ 7 * 7 }} {% if x %}"


def test_raw_block_prints_literal_delimiters() -> None:
    result = _render("{% raw %}{{ header }}{% endraw %} and {}", {})

    assert result["text"] == "{{ header }} and {}"


def test_whitespace_and_the_trailing_newline_are_kept() -> None:
    template = "  indented\n\n{% if true %}\n  kept\n{% endif %}\n"

    assert _render(template, {})["text"] == "  indented\n\n\n  kept\n\n"


def test_jinja_whitespace_control_still_works() -> None:
    assert _render("a\n{%- if true %} b{% endif -%}\n c", {})["text"] == "a bc"


def test_a_template_without_variables_is_static_text_and_an_empty_one_is_empty() -> None:
    assert _render("Static {}", {})["text"] == "Static {}"
    assert _render("", {})["text"] == ""


def test_json_input_braces_need_no_escaping() -> None:
    result = _render('{"name": "{{ name }}", "tags": []}', {"name": "Ada"})

    assert result["text"] == '{"name": "Ada", "tags": []}'


# --- missing values ------------------------------------------------------------------


def test_a_missing_required_input_fails_before_rendering() -> None:
    result = execute_template_step(
        _config("{{ body }}", [_input("body", "state.outputs.body")]), {"outputs": {}}
    )

    assert _reason(result) == "input_missing"
    assert "body" in result["errors"][0]["message"]


def test_a_missing_optional_input_stays_undefined_for_default_and_is_defined() -> None:
    config = _config(
        '{{ footer | default("none") }}|{% if footer is defined %}set{% else %}unset{% endif %}',
        [_input("footer", "state.outputs.footer", required=False)],
    )

    assert execute_template_step(config, {"outputs": {}})["text"] == "none|unset"
    assert execute_template_step(config, {"outputs": {"footer": ""}})["text"] == "|set"


def test_using_a_missing_optional_input_without_a_fallback_fails() -> None:
    result = execute_template_step(
        _config("{{ footer }}", [_input("footer", "state.outputs.footer", required=False)]),
        {"outputs": {}},
    )

    assert _reason(result) == "value_missing"
    assert "footer" in result["errors"][0]["message"]


def test_a_missing_field_of_an_object_input_fails_with_its_line() -> None:
    result = _render("first\n{{ product.price }}", {"product": {"name": "Tea"}})

    assert _reason(result) == "value_missing"
    assert "price" in result["errors"][0]["message"]
    assert result["errors"][0]["line"] == 2


@pytest.mark.parametrize(
    ("value", "type_", "expected"),
    [
        ("", "text", "[]"),
        (0, "number", "[0]"),
        (False, "boolean", "[False]"),
        ([], "array", "[[]]"),
        ({}, "object", "[{}]"),
        (None, "json", "[None]"),
    ],
)
def test_present_empty_values_are_not_missing(value: Any, type_: str, expected: str) -> None:
    result = execute_template_step(
        _config("[{{ value }}]", [_input("value", "state.outputs.value", type_=type_)]),
        {"outputs": {"value": value}},
    )

    assert result["text"] == expected


def test_null_for_a_typed_input_is_a_type_error_not_a_missing_value() -> None:
    result = execute_template_step(
        _config("{{ value }}", [_input("value", "state.outputs.value")]),
        {"outputs": {"value": None}},
    )

    assert _reason(result) == "input_type"
    assert "expected text, got null" in result["errors"][0]["message"]


@pytest.mark.parametrize(
    ("value", "declared", "named"),
    [
        ({"name": "x"}, "array", "got object"),
        (["x"], "object", "got array"),
        ("1", "number", "got text"),
        (1, "text", "got number"),
        (True, "number", "got boolean"),
    ],
)
def test_a_type_error_names_the_value_in_the_declared_type_vocabulary(
    value: Any, declared: str, named: str
) -> None:
    result = execute_template_step(
        _config("{{ value }}", [_input("value", "state.outputs.value", type_=declared)]),
        {"outputs": {"value": value}},
    )

    assert _reason(result) == "input_type"
    assert f"expected {declared}, {named}" in result["errors"][0]["message"]


def test_values_keep_their_types_and_are_not_coerced() -> None:
    result = execute_template_step(
        _config("{{ count + 1 }}", [_input("count", "state.outputs.count", type_="text")]),
        {"outputs": {"count": 1}},
    )

    assert _reason(result) == "input_type"


def test_the_workflow_data_mirror_is_read_like_other_selectors() -> None:
    result = execute_template_step(
        _config("{{ name }}", [_input("name", "state.outputs.name")]),
        {"workflow_data": {"outputs": {"name": "mirror"}}},
    )

    assert result["text"] == "mirror"


# --- configuration -------------------------------------------------------------------


def _codes(config: dict[str, Any]) -> set[str]:
    return {issue["code"] for issue in validate_template_config(config, path_prefix="t")}


def test_a_valid_configuration_has_no_issues() -> None:
    assert _codes(_config("{{ a }}", [_input("a", "state.outputs.a")], format="html")) == set()


@pytest.mark.parametrize(
    ("config", "code"),
    [
        ({"inputs": [], "result_target": "state.outputs.x"}, "workflow.step_template_text_missing"),
        (_config(None), "workflow.step_template_text_missing"),  # type: ignore[arg-type]
        (_config("{{ a"), "workflow.step_template_syntax"),
        (_config("x", format="pdf"), "workflow.step_template_format_invalid"),
        ({"text": "x", "inputs": []}, "workflow.step_template_result_target_missing"),
        (
            _config("x", [_input("1a", "state.outputs.a")]),
            "workflow.step_template_input_name_invalid",
        ),
        (
            _config("x", [_input("loop", "state.outputs.a")]),
            "workflow.step_template_input_name_invalid",
        ),
        (
            _config("x", [_input("a.b", "state.outputs.a")]),
            "workflow.step_template_input_name_invalid",
        ),
        (
            _config("x", [_input("a", "state.outputs.a"), _input("a", "state.outputs.b")]),
            "workflow.step_template_input_name_duplicate",
        ),
        (
            _config("x", [_input("a", "state.outputs.a", type_="secret")]),
            "workflow.step_template_input_type_invalid",
        ),
        (_config("x", [_input("a", "outputs.a")]), "workflow.step_template_input_source_invalid"),
        (_config("x", [_input("a", "")]), "workflow.step_template_input_source_invalid"),
        (_config("{% include 'x' %}"), "workflow.step_template_syntax"),
    ],
)
def test_authoring_issues_name_the_problem(config: dict[str, Any], code: str) -> None:
    assert code in _codes(config)


def test_a_syntax_issue_reports_the_template_line() -> None:
    [issue] = validate_template_config(_config("ok\n{% for x in %}"), path_prefix="t")

    assert issue["path"] == "t.text"
    assert "line 2" in issue["message"]


def test_an_invalid_configuration_fails_the_step_without_rendering() -> None:
    assert _reason(execute_template_step({}, {})) == "config_invalid"


# --- credentials and the sandbox -----------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "state.inputs.api_token",
        "state.outputs.credentials.value",
        "state.inputs.db_password",
        "state.project_config.secrets",
    ],
)
def test_a_credential_source_cannot_be_renamed_into_a_template(source: str) -> None:
    config = _config("{{ plain }}", [_input("plain", source)])

    assert "workflow.step_template_input_source_invalid" in _codes(config)
    assert _reason(execute_template_step(config, {"inputs": {}})) == "config_invalid"


def test_a_value_holding_a_secret_reference_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        template_step_runtime,
        "is_secret_reference",
        lambda value: isinstance(value, dict) and value.get("kind") == "ref",
    )
    result = _render("{{ item.name }}", {"item": {"name": "x", "auth": {"kind": "ref"}}})

    assert _reason(result) == "input_secret"


def test_the_inputs_preview_masks_sensitive_fields() -> None:
    from core.application.orchestration.template_step_runtime import template_step_artifacts

    result = _render("{{ account.name }}", {"account": {"name": "Ada", "password": "pw-1"}})
    artifacts = template_step_artifacts(_config("x"), result, {})

    assert result["text"] == "Ada"
    assert "pw-1" not in str(artifacts["input_payload"])


@pytest.mark.parametrize(
    "template",
    [
        "{{ name.__class__ }}",
        "{{ name.__class__.__mro__ }}",
        "{{ items.append(1) }}",
        "{{ cycler.__init__.__globals__ }}",
    ],
)
def test_the_sandbox_refuses_unsafe_access(template: str) -> None:
    result = _render(template, {"name": "x", "items": [1]})

    assert _reason(result) in {"template_forbidden", "value_missing"}


def test_include_is_refused() -> None:
    assert _reason(_render("{% include 'other' %}", {})) == "config_invalid"


def test_a_template_over_the_size_limit_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(template_step_runtime, "MAX_TEMPLATE_CHARS", 10)

    assert _reason(_render("x" * 11, {})) == "config_invalid"
    with pytest.raises(jinja_sandbox.TemplateRenderError) as caught:
        jinja_sandbox.render_template(
            "x" * (jinja_sandbox.MAX_TEMPLATE_CHARS + 1), {}, output_format="text"
        )
    assert caught.value.reason == "template_too_large"


def test_inputs_over_the_size_limit_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jinja_sandbox, "MAX_CONTEXT_BYTES", 100)

    assert _reason(_render("{{ body }}", {"body": "x" * 200})) == "inputs_too_large"


def test_output_over_the_limit_stops_rendering() -> None:
    template = "{% for i in range(99999) %}{{ chunk }}{% endfor %}"

    assert _reason(_render(template, {"chunk": "y" * 20})) == "output_too_large"


def test_repetition_beyond_the_output_limit_is_refused_before_it_is_built() -> None:
    assert _reason(_render("{{ 'a' * 100000000 }}", {})) == "template_forbidden"


def test_a_render_past_the_time_limit_is_killed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jinja_sandbox, "RENDER_TIMEOUT_SECONDS", 1.0)
    template = "{% for i in range(100000) %}{% for j in range(100000) %}{% endfor %}{% endfor %}"

    assert _reason(_render(template, {})) == "render_timeout"


# --- workflow integration ------------------------------------------------------------


def _template_state(config: dict[str, Any], **state: Any) -> dict[str, Any]:
    from tests._step_run_state import step_run_state

    return step_run_state(
        {
            "step_id": "compose_post",
            "display_name": "Compose post",
            "step_kind": "template",
            "override": {"template": config},
        },
        **state,
    )


def test_the_step_writes_its_text_to_the_result_target_and_records_inputs_and_format() -> None:
    from core.application.orchestration.graph.nodes import execute_step_node
    from core.application.reports.ui_projection_helpers import _message_step_debug_fields

    config = _config(
        "# {{ header }}",
        [_input("header", "state.outputs.title")],
        format="markdown",
        result_target="state.outputs.post_markdown",
    )
    update = execute_step_node(_template_state(config, outputs={"title": "Hello"}))

    assert update["outputs"]["post_markdown"] == "# Hello"
    message = update["agent_messages"][-1]
    assert message["from_agent"] == "template_step"
    assert message["status"] == "pass"
    assert message["artifacts"]["template"]["format"] == "markdown"
    debug = _message_step_debug_fields(message)
    assert debug["inputs"] == {"header": "Hello"}
    assert debug["outputs"]["value"] == "# Hello"
    assert debug["outputs"]["format"] == "markdown"


def test_a_failed_render_fails_the_step_and_writes_nothing() -> None:
    from core.application.orchestration.graph.nodes import execute_step_node

    config = _config("{{ body }}", [_input("body", "state.outputs.body")])
    update = execute_step_node(_template_state(config, outputs={"text": "previous"}))

    assert "outputs" not in update
    message = update["agent_messages"][-1]
    assert message["status"] == "fail"
    assert message["artifacts"]["template"]["failure_reason"] == "input_missing"
    assert "output_payload" not in message["artifacts"]


def test_show_full_text_reads_the_whole_output_from_the_message() -> None:
    from core.application.orchestration.graph.nodes import execute_step_node
    from core.application.reports.step_full_value import full_step_output_value

    long_body = "word " * 2_000
    config = _config("{{ body }}", [_input("body", "state.outputs.body")])
    update = execute_step_node(_template_state(config, outputs={"body": long_body}))
    message = update["agent_messages"][-1]

    assert "characters in total" in message["artifacts"]["output_payload"]["value"]
    assert full_step_output_value(message, "value") == {"found": True, "value": long_body}


def test_a_very_long_text_is_not_copied_into_the_message() -> None:
    from core.application.orchestration.graph.nodes import execute_step_node

    body = "x" * (template_step_runtime.MESSAGE_TEXT_MAX_CHARS + 1)
    config = _config("{{ body }}", [_input("body", "state.outputs.body")])
    update = execute_step_node(_template_state(config, outputs={"body": body}))

    assert update["outputs"]["text"] == body
    assert "_state_patch" not in update["agent_messages"][-1]["artifacts"]


def test_test_run_drawer_gets_the_whole_text_even_when_the_message_keeps_a_cut() -> None:
    from core.application.orchestration.graph.nodes import execute_step_node
    from core.application.workflows.test_run_result_values import _step_test_result_artifacts

    body = "x" * (template_step_runtime.MESSAGE_TEXT_MAX_CHARS + 1)
    config = _config("{{ body }}", [_input("body", "state.outputs.body")], format="html")
    state = _template_state(config, outputs={"body": body})
    update = execute_step_node(state)
    final_state = {**state, **update}

    artifacts = _step_test_result_artifacts(final_state, update["agent_messages"][-1]["artifacts"])

    assert artifacts["template"]["output"] == body
    assert artifacts["template"]["format"] == "html"


def test_only_the_declared_sources_are_workflow_selectors() -> None:
    step = {
        "step_kind": "template",
        "override": {
            "template": _config(
                "{{ header }} {{ state.not_a_selector }}",
                [_input("header", "state.outputs.title")],
            )
        },
    }

    references = discover_step_state_selectors(step)

    assert [reference.selector for reference in references] == ["state.outputs.title"]


def test_other_steps_keep_their_own_substitution_rules() -> None:
    from core.application.orchestration.step_templating import _render_value

    # HTTP Request and similar fields still use the simple {{ state.x }} form,
    # where a missing value is an empty string, not a Jinja error.
    assert _render_value("a{{ state.missing }}b", {}) == "ab"
    assert _render_value("{{ state.x | default: 'd' }}", {}) == "d"
