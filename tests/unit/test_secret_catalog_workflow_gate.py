"""Workflow projection, destination rules and reserved definition keys (ADR 0057)."""

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

#: A `frozen_definition`: steps live beside the settings, as a run pins them.
_DEFINITION = {
    "workflow_id": "wf",
    "protected": False,
    "settings": {
        "callable": {"expected_inputs": [{"name": "key", "type": "secret"}]},
        "canvas_annotations": [{"text": "note"}],
    },
    "execution_graph": {"schema_version": 1, "entry": {"kind": "step", "id": "a"}, "edges": []},
    "groups": [],
    "for_each": [],
    "steps": [
        {
            "step_id": "a",
            "step_kind": "http_request",
            "display_name": "A",
            "position": {"x": 1},
            "override": {"http_request": {"url": "https://api.test/v1", "headers": {"K": "v"}}},
        },
        {
            "step_id": "b",
            "step_kind": "http_request",
            "override": {"http_request": {"url": "{{ x }}"}},
        },
    ],
}


def _fp(definition) -> str:
    return workflow_fingerprint(workflow_projection(definition, bindings={"key": "s1"}))


def test_canvas_and_labels_are_not_executable() -> None:
    import copy

    moved = copy.deepcopy(_DEFINITION)
    moved["steps"][0]["display_name"] = "Renamed"
    moved["steps"][0]["position"] = {"x": 9}
    moved["settings"]["canvas_annotations"] = []
    # Dragging cards saves the canvas layout; an approval must not depend on it.
    moved["settings"]["canvas_layout"] = {"mode": "manual", "positions": {"a": {"x": 9, "y": 1}}}
    moved["protected"] = True
    assert _fp(moved) == _fp(_DEFINITION)


@pytest.mark.parametrize(
    "edit",
    [
        lambda d: d["steps"][0]["override"]["http_request"].update(url="https://other.test"),
        # Same host, but the secret could now travel in another header.
        lambda d: d["steps"][0]["override"]["http_request"]["headers"].update(L="{{ key }}"),
        lambda d: d["steps"].append({"step_id": "c", "step_kind": "transform"}),
        lambda d: d["execution_graph"]["edges"].append({"source": "a", "target": "b"}),
    ],
)
def test_every_executable_edit_changes_the_fingerprint(edit) -> None:
    import copy

    changed = copy.deepcopy(_DEFINITION)
    edit(changed)
    assert _fp(changed) != _fp(_DEFINITION)
    assert workflow_fingerprint(workflow_projection(_DEFINITION, bindings={"key": "s2"})) != _fp(
        _DEFINITION
    )


def test_literal_and_computed_urls_are_told_apart() -> None:
    rules, dynamic = http_destinations(_DEFINITION)
    assert rules == [{"kind": "exact_host", "scheme": "https", "host": "api.test", "port": 443}]
    assert dynamic == ["b"]


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        # A computed path or query keeps a literal origin.
        ("https://api.test/v1/{{ state.inputs.id }}", ("https", "api.test", 443)),
        ("https://api.test:8443?q={{ x }}", ("https", "api.test", 8443)),
        # A computed scheme, host or port does not.
        ("https://{{ host }}/v1", None),
        ("https://api.{{ zone }}.test/v1", None),
        ("https://api.test{{ suffix }}", None),
        ("https://api.test:{{ port }}/v1", None),
        ("{{ scheme }}://api.test/v1", None),
    ],
)
def test_a_template_after_the_authority_keeps_the_origin(url, origin) -> None:
    definition = {
        "steps": [
            {
                "step_id": "s",
                "step_kind": "http_request",
                "override": {"http_request": {"url": url}},
            }
        ]
    }
    rules, dynamic = http_destinations(definition)
    if origin is None:
        assert (rules, dynamic) == ([], ["s"])
    else:
        scheme, host, port = origin
        assert rules == [{"kind": "exact_host", "scheme": scheme, "host": host, "port": port}]
        assert dynamic == []


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
        # Dot segments are resolved the way the client sends the request.
        ("https://api.test/api/./v1", True),
        ("https://api.test/api/v1/..", True),
        ("https://api.test/api/../outside", False),
        ("https://api.test/api/v1/../../outside", False),
        ("https://api.test/api/..", False),
        # Encodings a server may decode into a traversal are refused, not guessed.
        ("https://api.test/api/%2e%2e/outside", False),
        ("https://api.test/api/%2E%2E/outside", False),
        ("https://api.test/api/..%2foutside", False),
        ("https://api.test/api%2F..%2Foutside", False),
        ("https://api.test/api/..\\outside", False),
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
    for prefix in ("/api/../outside", "/api/%2e%2e", "/api\\x"):
        with pytest.raises(ValueError, match="path_prefix"):
            normalize_rule({"scheme": "https", "host": "api.test", "path_prefix": prefix})


def test_a_definition_cannot_claim_a_binding() -> None:
    settings = {
        "callable": {
            "expected_inputs": [{"name": "key", "type": "secret", "catalog_secret_id": "x"}]
        }
    }
    assert reserved_binding_keys(settings) == ["key.catalog_secret_id"]
    assert reserved_binding_keys(_DEFINITION["settings"]) == []


def test_a_captured_release_stays_put_when_the_active_link_moves(tmp_path) -> None:
    from core.application.extensions.invocation_release import InvocationReleaseSelection

    releases = tmp_path / ".releases" / "partner"
    (releases / "1.0.0-aaaa").mkdir(parents=True)
    (releases / "1.1.0-bbbb").mkdir(parents=True)
    active = tmp_path / "partner"
    active.symlink_to(releases / "1.0.0-aaaa")

    captured = InvocationReleaseSelection.capture({"bundle_root": str(active)}).runtime_extension()
    active.unlink()
    active.symlink_to(releases / "1.1.0-bbbb")

    # The invocation captures again what the step dispatched: the same directory.
    again = InvocationReleaseSelection.capture(captured)
    assert again.bundle_root == (releases / "1.0.0-aaaa").resolve()
    assert (
        InvocationReleaseSelection.capture({"bundle_root": str(active)}).bundle_root
        == (releases / "1.1.0-bbbb").resolve()
    )
