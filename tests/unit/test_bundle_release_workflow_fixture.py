"""A release suite's declared workflow is built the way every client authors one."""

from __future__ import annotations

from typing import Any

import pytest

from scripts.smoke.bundle_release_checks import _prepare_workflow

_CHANGES = [
    {
        "op": "create_step",
        "step": {"step_id": "send_release_email", "step_kind": "connector"},
    },
    {"op": "set_entry", "target": {"kind": "step", "id": "send_release_email"}},
]


class _Api:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    def request(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        self.calls.append((method, path, body))
        if method == "GET":
            return 200, {"workflow_id": path.rsplit("/", 1)[-1], "revision": 1}
        if path.endswith("/changes"):
            return 200, {"revision": 2}
        return (201 if path == "/api/workflows" else 200), {}


def test_a_declared_workflow_is_metadata_then_one_change_set_then_enabled() -> None:
    api = _Api()

    key, workflow_id = _prepare_workflow(
        api,
        {
            "key": "email",
            "workflow_id": "send_release_email",
            "display_name": "Send release email",
            "enabled": True,
            "changes": _CHANGES,
        },
        "proj_1",
        {},
    )

    assert key == "email"
    assert workflow_id.startswith("send_release_email_")
    methods = [(method, path) for method, path, _ in api.calls]
    assert methods == [
        ("POST", "/api/workflows"),
        ("GET", f"/api/workflows/{workflow_id}"),
        ("POST", f"/api/workflows/{workflow_id}/changes"),
        ("PATCH", f"/api/workflows/{workflow_id}"),
        ("PUT", "/api/projects/proj_1/workflows"),
    ]
    metadata = api.calls[0][2]
    assert metadata["enabled"] is False
    assert "changes" not in metadata
    assert api.calls[2][2] == {"expected_revision": 1, "operations": _CHANGES}
    # The enable names the revision the change set produced: PATCH requires it.
    assert api.calls[3][2] == {"enabled": True, "expected_revision": 2}


def test_a_disabled_declaration_is_never_enabled() -> None:
    api = _Api()

    _prepare_workflow(api, {"workflow_id": "w", "changes": _CHANGES}, "proj_1", {})

    assert [method for method, _path, _body in api.calls] == ["POST", "GET", "POST", "PUT"]


@pytest.mark.parametrize("field", ["steps", "groups", "phases"])
def test_a_declaration_with_definition_fields_is_refused_before_any_write(field: str) -> None:
    api = _Api()

    with pytest.raises(RuntimeError, match="declare its steps and graph as 'changes'"):
        _prepare_workflow(api, {"workflow_id": "w", field: [], "changes": _CHANGES}, "proj_1", {})

    assert api.calls == []


def test_a_declaration_without_changes_is_refused_before_any_write() -> None:
    api = _Api()

    with pytest.raises(RuntimeError, match="has no 'changes'"):
        _prepare_workflow(api, {"workflow_id": "w"}, "proj_1", {})

    assert api.calls == []


def test_a_refused_change_set_names_the_fixture() -> None:
    class _Refusing(_Api):
        def request(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
            if path.endswith("/changes"):
                self.calls.append((method, path, body))
                return 400, {"detail": "bad op"}
            return super().request(method, path, body)

    refusing_api = _Refusing()
    with pytest.raises(
        RuntimeError, match="building fixture workflow 'email' failed with HTTP 400"
    ):
        _prepare_workflow(
            refusing_api, {"key": "email", "workflow_id": "w", "changes": _CHANGES}, "proj_1", {}
        )
