"""Step-test values bind runtime selectors, never overwrite saved HTTP configuration."""

from types import SimpleNamespace

import pytest

from core.application.registry.io_resolver import IOResolver
from core.application.workflows.platform_step_test_run_helpers import _preflight_step_test_run
from core.application.workflows.test_run_input_bindings import apply_test_run_input_bindings


def preview(monkeypatch, config):
    step = {"step_id": "fetch", "step_kind": "http_request", "override": {"http_request": config}}
    io = IOResolver(SimpleNamespace())
    monkeypatch.setattr(io, "_get_workflow_step", lambda *_args: ({"workflow_id": "wf"}, step))
    monkeypatch.setattr(io, "_build_available_type_map", lambda *_args: {})
    contract = io.resolve_step_contract_preview(workflow_id="wf", step_id="fetch")
    rows = [
        row
        for group in ("locked_required", "unresolved_required", "optional", "custom")
        for row in contract[group]
    ]
    return step, rows


def test_http_saved_literals_remain_configuration_not_bindings(monkeypatch):
    _, rows = preview(monkeypatch, {"method": "GET", "url": "https://example.test/?page=2"})
    assert {row["name"]: row["source"] for row in rows if row["name"] in {"method", "url"}} == {
        "method": "literal",
        "url": "literal",
    }
    assert "page=2" not in repr(rows)


def test_http_method_and_url_template_inputs_feed_the_saved_request(monkeypatch):
    step, rows = preview(
        monkeypatch,
        {
            "method": "{{ state.verb }}",
            "url": "https://example.test/{{ state.resource }}/{{ state.record_id }}",
        },
    )
    bound = [row for row in rows if row["source"].startswith("state.")]
    assert {row["source"] for row in bound} == {"state.verb", "state.resource", "state.record_id"}
    assert len({row["name"] for row in bound}) == len(bound)
    by_source = {"state.verb": "GET", "state.resource": "orders", "state.record_id": "42"}
    values = {row["name"]: by_source[row["source"]] for row in bound}
    state = apply_test_run_input_bindings(
        {}, {"metadata": {"input_bindings": bound, "input_binding_values": values}}
    )
    request, error = _preflight_step_test_run(state=state, step=step)
    assert error is None
    assert request["method"] == "GET"
    assert request["url"] == "https://example.test/orders/42"
    assert step["override"]["http_request"]["method"] == "{{ state.verb }}"


@pytest.mark.parametrize(
    "field,value",
    [
        ("query_params", {"filter": "{{ state.first }}:{{ state.second }}"}),
        ("headers", {"X-Range": "{{ state.first }}:{{ state.second }}"}),
        ("body_template", "{{ state.first }}:{{ state.second }}"),
    ],
)
def test_each_selector_in_a_shared_http_field_has_an_independent_test_value(
    monkeypatch, field, value
):
    step, rows = preview(
        monkeypatch, {"method": "GET", "url": "https://example.test", field: value}
    )
    bound = [row for row in rows if row["source"].startswith("state.")]
    assert {row["source"] for row in bound} == {"state.first", "state.second"}
    assert len({row["name"] for row in bound}) == 2
    values = {
        row["name"]: {"state.first": "one", "state.second": "two"}[row["source"]] for row in bound
    }
    state = apply_test_run_input_bindings(
        {}, {"metadata": {"input_bindings": bound, "input_binding_values": values}}
    )
    request, error = _preflight_step_test_run(state=state, step=step)
    assert error is None
    target = {
        "query_params": request["query_params"].get("filter"),
        "headers": request["headers"].get("X-Range"),
        "body_template": request["body"],
    }[field]
    assert target == "one:two"


def test_single_and_repeated_identical_selectors_preserve_existing_field_names(monkeypatch):
    _, rows = preview(
        monkeypatch,
        {
            "method": "GET",
            "url": "https://example.test",
            "headers": {"X-Request": "{{ state.id }}:{{ state.id }}"},
        },
    )
    bound = [row for row in rows if row["source"].startswith("state.")]
    assert [(row["name"], row["source"]) for row in bound] == [("headers.X-Request", "state.id")]


def test_generated_selector_names_do_not_collide_with_existing_field_paths(monkeypatch):
    step, rows = preview(
        monkeypatch,
        {
            "method": "GET",
            "url": "https://example.test",
            "query_params": {
                "filter": "{{ state.first }}:{{ state.second }}",
                "filter (state.first)": "{{ state.third }}",
                "filter (state.first) [2]": "{{ state.fourth }}",
            },
        },
    )
    bound = [row for row in rows if row["source"].startswith("state.")]
    assert len({row["name"] for row in bound}) == 4
    by_source = {
        "state.first": "one",
        "state.second": "two",
        "state.third": "three",
        "state.fourth": "four",
    }
    values = {row["name"]: by_source[row["source"]] for row in bound}
    state = apply_test_run_input_bindings(
        {}, {"metadata": {"input_bindings": bound, "input_binding_values": values}}
    )
    request, error = _preflight_step_test_run(state=state, step=step)
    assert error is None
    assert request["query_params"] == {
        "filter": "one:two",
        "filter (state.first)": "three",
        "filter (state.first) [2]": "four",
    }
