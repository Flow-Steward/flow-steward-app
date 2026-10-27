from __future__ import annotations

from types import SimpleNamespace

from core.application.tooling.testing import fuzz_api


class _FakeSession:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.cookies = {}

    def post(self, *_args, **_kwargs):
        return SimpleNamespace(status_code=self.status_code)


def test_workflow_run_malformed_json_accepts_forbidden_protected_mutation() -> None:
    result = fuzz_api._check_workflow_run_malformed_json(
        session=_FakeSession(403),
        base_url="http://fs-app:8099",
        verify=False,
        timeout=5.0,
    )

    assert result.status == "pass"
    assert result.http_status == 403


def test_workflow_run_type_confusion_accepts_forbidden_protected_mutation() -> None:
    result = fuzz_api._check_workflow_run_type_confusion(
        session=_FakeSession(403),
        base_url="http://fs-app:8099",
        verify=False,
        timeout=5.0,
    )

    assert result.status == "pass"
    assert result.http_status == 403
