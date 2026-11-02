"""Workflow projection, destination rules and reserved definition keys (ADR 0055)."""

from __future__ import annotations

import pytest

from core.application.secrets.workflow_gate import (
    http_destinations,
    normalize_rule,
    reserved_binding_keys,
    url_allowed,
    workflow_fingerprint,
    workflow_projection,
)

_WORKFLOW = {
    "workflow_id": "wf",
    "settings": {
        "callable": {"expected_inputs": [{"name": "key", "type": "secret"}]},
        "canvas_annotations": [{"text": "note"}],
        "steps": [
            {
                "step_id": "a",
                "step_kind": "http_request",
                "label": "A",
                "position": {"x": 1},
                "override": {"http_request": {"url": "https://api.test/v1"}},
            },
            {
                "step_id": "b",
                "step_kind": "http_request",
                "override": {"http_request": {"url": "{{ x }}"}},
            },
        ],
    },
}


def _fp(workflow) -> str:
    return workflow_fingerprint(workflow_projection(workflow, bindings={"key": "s1"}))


def test_canvas_and_labels_are_not_executable() -> None:
    import copy

    moved = copy.deepcopy(_WORKFLOW)
    moved["settings"]["steps"][0]["label"] = "Renamed"
    moved["settings"]["steps"][0]["position"] = {"x": 9}
    moved["settings"]["canvas_annotations"] = []
    assert _fp(moved) == _fp(_WORKFLOW)
    changed = copy.deepcopy(_WORKFLOW)
    changed["settings"]["steps"][0]["override"]["http_request"]["url"] = "https://other.test"
    assert _fp(changed) != _fp(_WORKFLOW)


def test_literal_and_computed_urls_are_told_apart() -> None:
    rules, dynamic = http_destinations(_WORKFLOW["settings"])
    assert rules == [{"kind": "exact_host", "scheme": "https", "host": "api.test", "port": 443}]
    assert dynamic == ["b"]


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://api.test/api", True),
        ("https://api.test/api/v1", True),
        ("https://api.test/api-v1", False),
        ("https://API.test/api/v1", True),
        ("https://api.test:443/api/v1", True),
        ("https://api.test:444/api/v1", False),
        ("http://api.test/api", False),
        ("https://api.test.evil/api", False),
    ],
)
def test_path_prefixes_match_on_segment_boundaries(url, allowed) -> None:
    rule = normalize_rule({"scheme": "https", "host": "api.test", "path_prefix": "/api/"})
    assert url_allowed(url, [rule]) is allowed


def test_unsupported_rules_are_refused_not_approximated() -> None:
    with pytest.raises(ValueError, match="not supported"):
        normalize_rule({"kind": "subdomains", "scheme": "https", "host": "test"})
    with pytest.raises(ValueError):
        normalize_rule({"scheme": "ftp", "host": "api.test"})


def test_a_definition_cannot_claim_a_binding() -> None:
    settings = {
        "callable": {
            "expected_inputs": [{"name": "key", "type": "secret", "catalog_secret_id": "x"}]
        }
    }
    assert reserved_binding_keys(settings) == ["key.catalog_secret_id"]
    assert reserved_binding_keys(_WORKFLOW["settings"]) == []
