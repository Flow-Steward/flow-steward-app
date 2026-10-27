"""Jobs public CLI is schema-driven only."""

from __future__ import annotations

import importlib.util
from types import SimpleNamespace
from typing import Any

from click.testing import CliRunner

from core.application.management.command_specs.jobs import JOB_COMMAND_SPECS


class _Registry:
    async def invoke(self, key: str, value: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError(f"Unexpected service invocation: {key} {value!r}")

    async def choices(self, _key: str) -> tuple[Any, ...]:
        return ()


def _app() -> Any:
    from core.entrypoints.cli.platform.click_adapter import build_click_app

    return build_click_app(_Registry(), SimpleNamespace())


def test_legacy_jobs_parser_module_is_removed() -> None:
    assert importlib.util.find_spec("core.entrypoints.cli.jobs") is None


def test_jobs_specs_are_canonical_and_in_process_approval_refs() -> None:
    by_id = {spec.command_id: spec for spec in JOB_COMMAND_SPECS}

    assert tuple(spec.path[1] for spec in JOB_COMMAND_SPECS) == (
        "list",
        "show",
        "status",
        "logs",
        "messages",
        "errors",
        "approve",
        "reject",
        "ack",
        "unack",
        "cancel",
        "restart",
        "health-snapshot",
    )
    assert by_id["jobs.approve"].service_ref == "jobs.approve"
    assert by_id["jobs.reject"].service_ref == "jobs.reject"
    for command_id in ("jobs.approve", "jobs.reject"):
        spec = by_id[command_id]
        approval_field = next(field for field in spec.fields if field.key == "approval_id")
        assert approval_field.required is True
        assert "--approval-id approval_123" in spec.examples[0]


def test_removed_jobs_http_approval_options_are_rejected() -> None:
    result = CliRunner().invoke(
        _app(),
        ["jobs", "approve", "thread_1", "--bearer", "token", "--base-url", "https://old"],
    )

    assert result.exit_code == 2, result.output
