from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from core.infrastructure.extension_runtime.extension_runtime import (
    ExtensionCircuitBreaker,
    run_extension_invocation,
)

ROOT = Path(__file__).resolve().parents[2]


def _extension(
    extension_id: str,
    install_id: str,
    script: str,
    *,
    external_effects: list[dict] | None = None,
) -> dict:
    return {
        "runtime": {"contract_version": "extension_host_v2"},
        "bundle_root": str(Path.cwd()),
        "extension_id": extension_id,
        "extension_install_id": install_id,
        "entrypoint": {"command": [sys.executable, "-c", script]},
        "external_effects": list(external_effects or []),
    }


def _invoke(extension: dict, account_id: str, **kwargs) -> dict:
    runtime_context = dict(kwargs.pop("runtime_context", {}) or {})
    runtime_context["account_id"] = account_id
    return run_extension_invocation(
        extension,
        request_payload=kwargs.pop("request_payload", {}),
        runtime_context=runtime_context,
        timeout_seconds=kwargs.pop("timeout_seconds", 5),
        **kwargs,
    )


def _breaker(extension_id: str, account_id: str, install_id: str) -> ExtensionCircuitBreaker:
    return ExtensionCircuitBreaker.for_extension(
        extension_id,
        account_id=account_id,
        extension_install_id=install_id,
    )


def test_account_failure_bucket_does_not_quarantine_another_account() -> None:
    ExtensionCircuitBreaker.reset_all()
    failing = _extension("acme.shared", "install-shared", "raise SystemExit(2)")
    healthy = _extension(
        "acme.shared",
        "install-shared",
        "import json; print(json.dumps({'ok': True}))",
    )

    for _ in range(5):
        assert _invoke(failing, "account-a")["status"] == "failed"

    assert _invoke(healthy, "account-b")["status"] == "succeeded"
    assert _breaker("acme.shared", "account-a", "install-shared").is_quarantined
    assert not _breaker("acme.shared", "account-b", "install-shared").is_quarantined


def test_install_failure_bucket_does_not_quarantine_another_install() -> None:
    ExtensionCircuitBreaker.reset_all()
    failing = _extension("acme.shared", "install-a", "raise SystemExit(2)")
    healthy = _extension(
        "acme.shared",
        "install-b",
        "import json; print(json.dumps({'ok': True}))",
    )

    for _ in range(5):
        assert _invoke(failing, "account-a")["status"] == "failed"

    assert _invoke(healthy, "account-a")["status"] == "succeeded"
    assert _breaker("acme.shared", "account-a", "install-a").is_quarantined
    assert not _breaker("acme.shared", "account-a", "install-b").is_quarantined


def test_success_resets_only_its_exact_failure_bucket() -> None:
    ExtensionCircuitBreaker.reset_all()
    failing = _extension("acme.shared", "install-a", "raise SystemExit(2)")
    healthy = _extension(
        "acme.shared",
        "install-b",
        "import json; print(json.dumps({'ok': True}))",
    )

    for _ in range(2):
        _invoke(failing, "account-a")
    _invoke(healthy, "account-a")
    for _ in range(3):
        _invoke(failing, "account-a")

    assert _breaker("acme.shared", "account-a", "install-a").consecutive_failures == 5
    assert _breaker("acme.shared", "account-a", "install-a").is_quarantined
    assert _breaker("acme.shared", "account-a", "install-b").consecutive_failures == 0


def test_missing_external_effect_claim_does_not_acquire_concurrency_slots() -> None:
    ExtensionCircuitBreaker.reset_all()
    extension = _extension(
        "acme.effects",
        "install-a",
        "import json; print(json.dumps({'ok': True}))",
        external_effects=[{"operation_id": "send", "effect_kind": "network"}],
    )

    for _ in range(8):
        result = _invoke(
            extension,
            "account-a",
            request_payload={"action_id": "send"},
        )
        assert result["detail"] == "external_effect_claim_required"

    breaker = _breaker("acme.effects", "account-a", "install-a")
    assert breaker.active_count == 0
    assert breaker.consecutive_failures == 0


@pytest.mark.parametrize(
    ("extension_fields", "runtime_context"),
    [
        ({"account_id": "account-a"}, {"account_id": "account-b"}),
        (
            {"extension_install_id": "install-a"},
            {"account_id": "account-a", "extension_install_id": "install-b"},
        ),
    ],
)
def test_conflicting_runtime_identity_fails_before_subprocess(
    extension_fields: dict,
    runtime_context: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ExtensionCircuitBreaker.reset_all()
    subprocess_calls: list[bool] = []

    def _unexpected_subprocess(*_args, **_kwargs):
        subprocess_calls.append(True)
        raise AssertionError("identity conflict must fail before subprocess execution")

    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.extension_runtime_invocation._run_subprocess_capped",
        _unexpected_subprocess,
    )
    extension = {
        **_extension(
            "acme.identity",
            "install-a",
            "import json; print(json.dumps({'ok': True}))",
        ),
        **extension_fields,
    }

    result = run_extension_invocation(
        extension,
        request_payload={},
        runtime_context=runtime_context,
        timeout_seconds=5,
    )

    assert result["status"] == "failed"
    assert result["detail"] == "extension_runtime_identity_mismatch"
    assert subprocess_calls == []
    assert _breaker("acme.identity", "account-a", "install-a").active_count == 0


def test_runtime_failures_increment_the_exact_bucket() -> None:
    ExtensionCircuitBreaker.reset_all()
    extension_id = "acme.runtime-failures"
    install_id = "install-a"

    crash = _extension(extension_id, install_id, "raise SystemExit(2)")
    timeout = _extension(extension_id, install_id, "import time; time.sleep(10)")
    invalid_json = _extension(extension_id, install_id, "print('not-json')")

    assert _invoke(crash, "account-a")["status"] == "failed"
    assert _invoke(timeout, "account-a", timeout_seconds=1)["status"] == "failed"
    assert _invoke(invalid_json, "account-a")["status"] == "failed"
    assert _breaker(extension_id, "account-a", install_id).consecutive_failures == 3


def test_malformed_false_envelope_does_not_mask_runtime_failure() -> None:
    ExtensionCircuitBreaker.reset_all()
    extension = _extension(
        "acme.malformed",
        "install-a",
        "import json,sys; print(json.dumps({'ok': False, 'errors': []})); sys.exit(2)",
    )

    result = _invoke(extension, "account-a")

    assert result["status"] == "failed"
    assert _breaker("acme.malformed", "account-a", "install-a").consecutive_failures == 1


def test_canonical_failure_with_unexpected_exit_code_is_a_runtime_failure() -> None:
    ExtensionCircuitBreaker.reset_all()
    extension = _extension(
        "acme.crash-after-output",
        "install-a",
        "import json,sys; print(json.dumps({'ok': False, "
        "'error_code': 'invalid_payload', 'error': 'Invalid payload', "
        "'errors': [{'code': 'invalid_payload', 'message': 'Invalid payload'}]})); sys.exit(99)",
    )

    for _ in range(5):
        assert _invoke(extension, "account-a")["status"] == "failed"

    breaker = _breaker("acme.crash-after-output", "account-a", "install-a")
    assert breaker.consecutive_failures == 5
    assert breaker.is_quarantined


def test_mismatched_error_message_does_not_mask_runtime_failure() -> None:
    ExtensionCircuitBreaker.reset_all()
    extension = _extension(
        "acme.mismatched",
        "install-a",
        "import json,sys; print(json.dumps({'ok': False, "
        "'error_code': 'invalid_payload', 'error': 'top-level', "
        "'errors': [{'code': 'invalid_payload', 'message': 'different'}]})); sys.exit(2)",
    )

    result = _invoke(extension, "account-a")

    assert result["status"] == "failed"
    assert _breaker("acme.mismatched", "account-a", "install-a").consecutive_failures == 1


def test_json_object_without_boolean_ok_is_a_runtime_failure() -> None:
    ExtensionCircuitBreaker.reset_all()
    extension = _extension(
        "acme.missing-ok",
        "install-a",
        "import json; print(json.dumps({'result': {'value': 1}}))",
    )

    result = _invoke(extension, "account-a")

    assert result["status"] == "failed"
    assert _breaker("acme.missing-ok", "account-a", "install-a").consecutive_failures == 1


@pytest.mark.parametrize("request_text", ["{}", "[]"])
def test_generated_bundle_empty_request_uses_canonical_failure_envelope(
    tmp_path: Path,
    request_text: str,
) -> None:
    from core.application.extensions.scaffold import create_extension

    bundle_root = create_extension(extension_id="acme.empty-envelope", output_root=tmp_path)
    python_path = os.pathsep.join(
        [
            str(ROOT),
            str(ROOT / "core" / "infrastructure" / "extension_sdk" / "public"),
        ]
    )
    completed = subprocess.run(
        [sys.executable, "main.py"],
        cwd=bundle_root,
        input=request_text,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
        env={**os.environ, "PYTHONPATH": python_path},
    )
    response = json.loads(completed.stdout)

    assert completed.returncode == 2
    assert response["ok"] is False
    assert isinstance(response["error_code"], str)
    assert response["error_code"]
    assert isinstance(response["error"], str)
    assert response["error"]
    assert response["errors"]
    assert response["errors"][0]["code"] == response["error_code"]
    assert response["errors"][0]["message"] == response["error"]


@pytest.mark.parametrize("scaffold_kind", ["generic", "task_ingress"])
def test_generated_scaffold_domain_errors_are_canonical_and_do_not_quarantine(
    tmp_path: Path,
    scaffold_kind: str,
) -> None:
    if scaffold_kind == "generic":
        from core.application.extensions.scaffold import create_extension

        extension_id = "acme.generated-generic"
        bundle_root = create_extension(
            extension_id=extension_id,
            output_root=tmp_path,
        )
    else:
        from core.application.tooling.extensions.create_task_ingress_extension import (
            create_task_ingress_extension,
        )

        extension_id = "acme.generated-task-ingress"
        bundle_root = create_task_ingress_extension(
            extension_id=extension_id,
            output_root=tmp_path,
        )
    install_id = f"{scaffold_kind}-install"
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "bundle_root": str(bundle_root),
        "extension_id": extension_id,
        "extension_install_id": install_id,
        "entrypoint": {"command": [sys.executable, "main.py"]},
    }

    for _ in range(5):
        result = _invoke(extension, "account-a")
        response = result["response"]
        assert result["status"] == "failed"
        assert result["exit_code"] == 2
        assert response["ok"] is False
        assert response["error_code"] == "invalid_payload"
        assert response["error"]
        assert response["errors"][0] == {
            "code": response["error_code"],
            "message": response["error"],
        }

    breaker = _breaker(extension_id, "account-a", install_id)
    assert breaker.consecutive_failures == 0
    assert not breaker.is_quarantined
