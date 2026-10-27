from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support.disposable_installer import DisposableInstallerProject

ROOT = Path(__file__).resolve().parents[2]

pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.fixture
def disposable_project(tmp_path: Path) -> DisposableInstallerProject:
    project = DisposableInstallerProject.create(
        work_root=tmp_path,
        source_root=ROOT,
    )
    try:
        yield project
    finally:
        if not project.cleaned:
            project.cleanup()


def _assert_success(result) -> None:
    assert result.returncode == 0, result.combined_output


def test_fresh_scaffold_completes_public_lifecycle(
    disposable_project: DisposableInstallerProject,
) -> None:
    installed = disposable_project.install(
        environment="development",
        renderer="non_interactive",
        storage="bundled",
    )
    _assert_success(installed)

    extension_id = "audit.scaffold-contract"
    _assert_success(
        disposable_project.cli(
            "extensions",
            "create",
            extension_id,
            "--confirm",
            "--plain",
        )
    )
    _assert_success(
        disposable_project.cli(
            "extensions",
            "validate",
            extension_id,
            "--health",
            "--plain",
        )
    )
    tested = disposable_project.cli(
        "extensions",
        "test",
        extension_id,
        "--plain",
    )
    _assert_success(tested)
    assert tested.compose_service == "fs-test-runner"
    _assert_success(
        disposable_project.cli(
            "extensions",
            "lock-deps",
            extension_id,
            "--confirm",
            "--plain",
        )
    )
    _assert_success(
        disposable_project.cli(
            "extensions",
            "bundle",
            extension_id,
            "--confirm",
            "--plain",
        )
    )
    archive = f"dist/extensions/{extension_id}-0.1.0.zip"
    _assert_success(
        disposable_project.cli(
            "extensions",
            "install",
            "--archive",
            archive,
            "--confirm",
            "--plain",
        )
    )

    disposable_project.cleanup()
    assert all(not values for values in disposable_project.cleanup_proof().values())


def test_fresh_scaffold_test_failure_stays_one_json_document(
    disposable_project: DisposableInstallerProject,
) -> None:
    installed = disposable_project.install(
        environment="development",
        renderer="non_interactive",
        storage="bundled",
    )
    _assert_success(installed)

    extension_id = "audit.scaffold-failure"
    _assert_success(
        disposable_project.cli(
            "extensions",
            "create",
            extension_id,
            "--confirm",
            "--plain",
        )
    )
    generated_test = (
        disposable_project.distribution_directory
        / "extensions/audit_scaffold_failure/tests/test_audit_scaffold_failure.py"
    )
    generated_test.write_text(
        generated_test.read_text(encoding="utf-8")
        + "\n\ndef test_intentional_failure():\n    assert False\n",
        encoding="utf-8",
    )

    tested = disposable_project.cli(
        "extensions",
        "test",
        extension_id,
        "--json",
    )

    assert tested.returncode != 0
    payload = json.loads(tested.stdout)
    assert payload["error"]["code"] == "validation_failed"
    assert tested.compose_service == "fs-test-runner"

    disposable_project.cleanup()
    assert all(not values for values in disposable_project.cleanup_proof().values())
