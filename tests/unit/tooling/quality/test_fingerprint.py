from __future__ import annotations

import subprocess
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from core.application.tooling.quality import fingerprint as fingerprint_module
from core.application.tooling.quality import pip_audit_acceptance
from core.application.tooling.quality.check_registry import (
    CheckDefinition,
    load_check_registry,
)
from core.application.tooling.quality.fingerprint import (
    FingerprintContext,
    fingerprint_check,
)


def test_quality_git_reads_trust_only_the_exact_repository_root(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run(command: tuple[str, ...], **_kwargs: object) -> SimpleNamespace:
        calls.append(command)
        stdout = b"deadbeef blob 0\n\n" if "cat-file" in command else b""
        return SimpleNamespace(returncode=0, stdout=stdout, stderr=b"")

    monkeypatch.setattr(fingerprint_module.subprocess, "run", fake_run)

    fingerprint_module._run_git(tmp_path, "ls-files")
    fingerprint_module._read_git_blobs(tmp_path, ("deadbeef",))

    expected_prefix = ("git", "-c", f"safe.directory={tmp_path}")
    assert calls[0] == (*expected_prefix, "ls-files")
    assert calls[1] == (*expected_prefix, "cat-file", "--batch")


def _run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", *args),
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    _run_git(tmp_path, "init", "-q")
    _run_git(tmp_path, "config", "user.email", "quality@example.invalid")
    _run_git(tmp_path, "config", "user.name", "Quality Tests")
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "assets/frontend/src").mkdir(parents=True)
    (tmp_path / "assets/frontend/src/view.tsx").write_text(
        "export const value = 1\n", encoding="utf-8"
    )
    (tmp_path / "assets/frontend/package-lock.json").write_text(
        '{"lockfileVersion": 3}\n', encoding="utf-8"
    )
    _run_git(tmp_path, "add", ".")
    _run_git(tmp_path, "commit", "-qm", "fixture")
    return tmp_path


def _check(
    check_id: str = "mypy",
    *,
    inputs: tuple[str, ...] = ("core/**/*.py",),
    command: tuple[str, ...] = ("mypy", "core"),
) -> CheckDefinition:
    return CheckDefinition(
        id=check_id,
        owner_tier=2,
        command=command,
        inputs=inputs,
    )


def test_pip_audit_owns_utc_day_identity_without_mutating_caller_context(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _repo(tmp_path)
    current_day = date(2026, 10, 14)
    monkeypatch.setattr(pip_audit_acceptance, "acceptance_evaluation_date", lambda: current_day)
    identity = {"pip_audit_acceptance_utc_date": "1999-01-01", "runtime": "fixture"}
    context = FingerprintContext(root=repo, source="worktree", runtime_identity=identity)
    clean_context = FingerprintContext(
        root=repo, source="worktree", runtime_identity={"runtime": "fixture"}
    )
    pip_check = _check("pip-audit")
    before = fingerprint_check(pip_check, context)
    ordinary_before = fingerprint_check(_check(), context)
    assert before == fingerprint_check(pip_check, clean_context)
    assert identity == {"pip_audit_acceptance_utc_date": "1999-01-01", "runtime": "fixture"}

    current_day = date(2026, 10, 15)
    assert fingerprint_check(pip_check, context) != before
    assert fingerprint_check(_check(), context) == ordinary_before
    identity["pip_audit_acceptance_utc_date"] = "2099-01-01"
    assert fingerprint_check(pip_check, context) == fingerprint_check(pip_check, clean_context)
    assert identity["pip_audit_acceptance_utc_date"] == "2099-01-01"


def test_unrelated_frontend_change_does_not_invalidate_python_check(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    context = FingerprintContext(root=repo, source="worktree")
    before = fingerprint_check(_check(), context)

    (repo / "assets/frontend/src/view.tsx").write_text("export const value = 2\n", encoding="utf-8")

    assert fingerprint_check(_check(), context) == before


def test_lock_change_invalidates_frontend_node(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    check = _check("frontend-tests", inputs=("assets/frontend/**",))
    context = FingerprintContext(root=repo, source="worktree")
    before = fingerprint_check(check, context)

    (repo / "assets/frontend/package-lock.json").write_text(
        '{"lockfileVersion": 4}\n', encoding="utf-8"
    )

    assert fingerprint_check(check, context) != before


def test_stylelint_config_change_invalidates_frontend_sql_evidence(tmp_path: Path) -> None:
    registry_path = (
        Path(__file__).resolve().parents[4] / "infra/tooling/quality/check-registry.json"
    )
    registry = load_check_registry(registry_path)
    check = next(check for check in registry.checks if check.id == "frontend-sql")
    repo = _repo(tmp_path)
    configuration = repo / "infra/tooling/.stylelintrc.json"
    configuration.parent.mkdir(parents=True)
    configuration.write_text('{"rules": {"media-query-no-invalid": true}}\n')
    context = FingerprintContext(root=repo, source="worktree")
    before = fingerprint_check(check, context)

    configuration.write_text('{"rules": {"media-query-no-invalid": false}}\n')

    assert fingerprint_check(check, context) != before


def test_untracked_matching_file_invalidates_worktree_fingerprint(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    context = FingerprintContext(root=repo, source="worktree")
    before = fingerprint_check(_check(), context)

    (repo / "core/new_value.py").write_text("VALUE = 2\n", encoding="utf-8")

    assert fingerprint_check(_check(), context) != before


def test_ignored_matching_file_does_not_invalidate_worktree_fingerprint(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    (repo / ".gitignore").write_text("core/generated.py\n", encoding="utf-8")
    context = FingerprintContext(root=repo, source="worktree")
    before = fingerprint_check(_check(), context)

    (repo / "core/generated.py").write_text("VALUE = 2\n", encoding="utf-8")

    assert fingerprint_check(_check(), context) == before


def test_staged_and_worktree_content_match_when_blobs_match(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "core/value.py").write_text("VALUE = 2\n", encoding="utf-8")
    _run_git(repo, "add", "core/value.py")

    assert fingerprint_check(
        _check(), FingerprintContext(root=repo, source="index")
    ) == fingerprint_check(_check(), FingerprintContext(root=repo, source="worktree"))


def test_unstaged_change_does_not_change_index_fingerprint(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    context = FingerprintContext(root=repo, source="index")
    before = fingerprint_check(_check(), context)

    (repo / "core/value.py").write_text("VALUE = 2\n", encoding="utf-8")

    assert fingerprint_check(_check(), context) == before


def test_commit_fingerprint_ignores_later_worktree_changes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    commit_sha = _run_git(repo, "rev-parse", "HEAD")
    context = FingerprintContext(root=repo, source="commit", commit_sha=commit_sha)
    before = fingerprint_check(_check(), context)

    (repo / "core/value.py").write_text("VALUE = 2\n", encoding="utf-8")

    assert fingerprint_check(_check(), context) == before


def test_runtime_identity_changes_fingerprint_without_serializing_values(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    first = fingerprint_check(
        _check(),
        FingerprintContext(
            root=repo,
            source="worktree",
            runtime_identity={"python": "3.12.1"},
        ),
    )
    second = fingerprint_check(
        _check(),
        FingerprintContext(
            root=repo,
            source="worktree",
            runtime_identity={"python": "3.12.2"},
        ),
    )

    assert first != second
    assert "3.12" not in first


def test_command_change_invalidates_check_fingerprint(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    context = FingerprintContext(root=repo, source="worktree")

    assert fingerprint_check(_check(command=("mypy", "core")), context) != fingerprint_check(
        _check(command=("mypy", "--strict", "core")), context
    )


def test_gate_implementation_change_invalidates_existing_fingerprint(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    gate = repo / "core/entrypoints/cli/scripts/lint.sh"
    gate.parent.mkdir(parents=True, exist_ok=True)
    gate.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    context = FingerprintContext(root=repo, source="worktree")
    before = fingerprint_check(_check(), context)

    gate.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")

    assert fingerprint_check(_check(), context) != before


def test_path_only_check_ignores_content_but_detects_new_path(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    check = _check("repository-boundary", inputs=("core/**",)).model_copy(
        update={"input_mode": "paths"}
    )
    context = FingerprintContext(root=repo, source="worktree")
    before = fingerprint_check(check, context)
    (repo / "core/value.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert fingerprint_check(check, context) == before

    (repo / "core/other.py").write_text("VALUE = 3\n", encoding="utf-8")
    assert fingerprint_check(check, context) != before


def test_installed_sdk_edit_invalidates_otherwise_unrelated_gate_evidence(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    sdk = repo / "core/infrastructure/extension_sdk/public"
    package = sdk / "flowsteward_extension_sdk"
    package.mkdir(parents=True)
    module = package / "__init__.py"
    module.write_text("PUBLIC_CONTRACT = 1\n", encoding="utf-8")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-qm", "installed sdk fixture")
    check = _check("frontend-only", inputs=("assets/frontend/**",))
    context = FingerprintContext(root=repo, source="worktree")
    original = fingerprint_check(check, context)
    module.write_text("PUBLIC_CONTRACT = 2\n", encoding="utf-8")
    changed = fingerprint_check(check, context)
    assert changed != original
    _run_git(repo, "add", ".")
    assert fingerprint_check(check, FingerprintContext(root=repo, source="index")) == changed
    _run_git(repo, "commit", "-qm", "revised installed sdk")
    commit = _run_git(repo, "rev-parse", "HEAD")
    assert (
        fingerprint_check(check, FingerprintContext(root=repo, source="commit", commit_sha=commit))
        == changed
    )
