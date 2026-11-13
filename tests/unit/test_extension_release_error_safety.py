"""Release-selection failures must not expose host paths to extension consumers."""

from __future__ import annotations

import json

import pytest


@pytest.mark.parametrize("operation", ["invocation", "health"])
@pytest.mark.parametrize(
    "failure", ["missing", "not_directory", "permission", "removed_before_start"]
)
def test_release_selection_failure_is_safe_for_all_consumers(
    tmp_path, monkeypatch, caplog, operation, failure
):
    from core.application.datasets.connection_actions import (
        _response_from_invocation as connection_response,
    )
    from core.application.extensions.invocation_release import InvocationReleaseSelection
    from core.entrypoints.admin.datasets_helpers import _response_from_invocation as query_response
    from core.infrastructure.extension_runtime import extension_runtime_invocation as runtime
    from core.infrastructure.extension_runtime.extension_runtime_invocation import (
        run_extension_health,
        run_extension_invocation,
    )

    bundle = tmp_path / "private-release-location"
    if failure == "not_directory":
        bundle.write_text("not a directory")
    elif failure == "permission":

        def denied(_extension):
            raise PermissionError(f"permission denied: {bundle}")

        monkeypatch.setattr(InvocationReleaseSelection, "capture", denied)
    elif failure == "removed_before_start":
        bundle.mkdir()
        run_subprocess = runtime._run_subprocess_capped

        def remove_before_start(command, **kwargs):
            bundle.rmdir()
            return run_subprocess(command, **kwargs)

        monkeypatch.setattr(runtime, "_run_subprocess_capped", remove_before_start)
    extension = {
        "entrypoint": {"command": ["python3", "-c", "print('ok')"]},
        "healthcheck": {"command": ["python3", "-c", "print('ok')"]},
        "extension_key": "acme.dataset",
        "bundle_root": str(bundle),
        "compiled_manifest": {"runtime": {"contract_version": "extension_host_v2"}},
    }
    result = (run_extension_invocation if operation == "invocation" else run_extension_health)(
        extension
    )

    assert result["status"] == "failed"
    expected_code = (
        "extension_runtime_unavailable"
        if failure == "removed_before_start"
        else "extension_release_unavailable"
    )
    assert result["detail"] == expected_code
    assert str(bundle) not in json.dumps(result)
    assert str(bundle) not in caplog.text
    if operation == "invocation":
        if failure == "removed_before_start":
            # A failure after launch preparation must not assert absence of effects.
            assert result["response"].get("definitely_no_external_effect") is not True
        else:
            assert result["response"]["definitely_no_external_effect"] is True
        for adapt in (connection_response, query_response):
            response = adapt(result)
            assert response["ok"] is False
            assert response["errors"][0]["code"] == expected_code
            assert str(bundle) not in json.dumps(response)


def test_removed_script_does_not_expose_interpreter_stderr_to_datasets(tmp_path, monkeypatch):
    from core.application.datasets.connection_actions import (
        _response_from_invocation as connection_response,
    )
    from core.entrypoints.admin.datasets_helpers import _response_from_invocation as query_response
    from core.infrastructure.extension_runtime import extension_runtime_invocation as runtime

    bundle = tmp_path / "private-interpreter-location"
    bundle.mkdir()
    script = bundle / "main.py"
    script.write_text("print('ok')")
    run_subprocess = runtime._run_subprocess_capped

    def remove_script_before_start(command, **kwargs):
        script.unlink()
        return run_subprocess(command, **kwargs)

    monkeypatch.setattr(runtime, "_run_subprocess_capped", remove_script_before_start)
    invocation = runtime.run_extension_invocation(
        {
            "runtime": {"contract_version": "extension_host_v2"},
            "bundle_root": str(bundle),
            "entrypoint": {"command": ["python3", "main.py"]},
        }
    )

    assert invocation["status"] == "failed"
    assert invocation["response"].get("definitely_no_external_effect") is not True
    for adapt in (connection_response, query_response):
        response = adapt(invocation)
        assert response["ok"] is False
        assert str(bundle) not in json.dumps(response)
        assert response["errors"][0]["code"] == "extension_invocation_failed"


@pytest.mark.parametrize("raw_field", ["raw_stdout", "error", "detail"])
def test_unstructured_failure_copies_are_not_public(raw_field):
    from core.application.datasets.connection_actions import (
        _response_from_invocation as connection_response,
    )
    from core.entrypoints.admin.datasets_helpers import _response_from_invocation as query_response

    diagnostic = "/srv/private-extension-location/interpreter failure"
    invocation = {
        "status": "failed",
        "detail": diagnostic,
        "response": {raw_field: diagnostic, "result": {"diagnostic": diagnostic}},
    }
    for adapt in (connection_response, query_response):
        response = adapt(invocation)
        assert response["ok"] is False
        assert diagnostic not in json.dumps(response)
        assert response["errors"][0]["code"] == "extension_invocation_failed"


@pytest.mark.parametrize("legacy", ["list", "coded", "message"])
def test_structured_extension_failure_keeps_business_error_and_result(legacy):
    from core.application.datasets.connection_actions import (
        _response_from_invocation as connection_response,
    )
    from core.entrypoints.admin.datasets_helpers import _response_from_invocation as query_response

    error = {"code": "query_invalid", "message": "Choose an existing table."}
    envelope = {"ok": False, "result": {"column": "orders"}, "events": []}
    if legacy == "list":
        envelope["errors"] = [error]
    else:
        envelope["error"] = error["message"]
        if legacy == "coded":
            envelope["error_code"] = error["code"]
        else:
            error = {**error, "code": "extension_error"}
    invocation = {"status": "failed", "detail": error["message"], "response": envelope}
    for adapt in (connection_response, query_response):
        response = adapt(invocation)
        assert response["ok"] is False
        assert response["result"] == {"column": "orders"}
        assert response["errors"] == [error]
