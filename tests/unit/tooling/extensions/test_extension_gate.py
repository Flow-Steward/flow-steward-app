from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from core.application.tooling.extensions import extension_gate
from core.application.tooling.extensions.models import ExtensionTestResult


def _checkout(root: Path, extension_id: str = "acme.crm-sync") -> Path:
    source = root / "checkout"
    for folder in ("tests", "scripts", "connections", "contracts", "wheels", ".git", "__pycache__"):
        (source / folder).mkdir(parents=True)
    (source / "extension.yaml").write_text(f"extension_id: {extension_id}\n", encoding="utf-8")
    for name in ("__init__.py", "main.py", "health.py"):
        (source / name).write_text("", encoding="utf-8")
    (source / "connections" / "s3.py").write_text("", encoding="utf-8")
    (source / "contracts" / "operations.yaml").write_text("", encoding="utf-8")
    (source / "scripts" / "validate_bundle.py").write_text("", encoding="utf-8")
    (source / "tests" / "test_a.py").write_text("", encoding="utf-8")
    (source / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return source


def test_bundle_modules_are_its_own_files_and_packages(tmp_path: Path) -> None:
    source = _checkout(tmp_path)

    assert extension_gate.bundle_modules(source) == ["connections", "health.py", "main.py"]


def test_the_checkout_is_staged_under_its_canonical_folder_without_vcs_state(
    tmp_path: Path, monkeypatch
) -> None:
    source = _checkout(tmp_path)
    seen: dict[str, object] = {}

    def _test_extension(request):
        root = Path(request.root)
        seen["folders"] = sorted(path.name for path in root.iterdir())
        seen["git"] = (root / "acme_crm_sync" / ".git").exists()
        seen["test_paths"] = request.test_paths
        return ExtensionTestResult(
            status="passed", extension_id=request.extension_id, exit_code=0, output="ok\n"
        )

    monkeypatch.setattr(extension_gate, "test_extension", _test_extension)
    exit_code = extension_gate.main(
        ["tests", "--source", str(source), "--test-path", "tests/test_a.py"]
    )

    assert exit_code == 0
    assert seen == {
        "folders": ["acme_crm_sync"],
        "git": False,
        "test_paths": ("tests/test_a.py",),
    }


def test_an_extension_id_that_contradicts_the_manifest_is_refused(tmp_path: Path) -> None:
    source = _checkout(tmp_path)

    assert extension_gate.main(["tests", "--source", str(source), "--extension-id", "acme.x"]) == 2


def test_static_runs_every_step_and_counts_each_failure(tmp_path: Path, monkeypatch) -> None:
    extension_dir = _checkout(tmp_path)
    commands: list[list[str]] = []

    @contextmanager
    def _standalone_python(_extension_dir, _name):
        yield "venv-python"

    def _run(cmd, *, cwd, env=None):
        commands.append(list(cmd))
        return 1 if cmd[1:3] == ["-m", "mypy"] else 0

    monkeypatch.setattr(extension_gate, "standalone_python", _standalone_python)
    monkeypatch.setattr(extension_gate, "_run", _run)

    assert extension_gate.check_extension_static(extension_dir) == 1
    modules = ["connections", "health.py", "main.py"]
    assert commands == [
        ["venv-python", "scripts/validate_bundle.py"],
        ["venv-python", "-m", "compileall", "-q", *modules, "tests", "scripts"],
        ["venv-python", "-m", "ruff", "check", "--no-cache", "."],
        ["venv-python", "-m", "ruff", "format", "--check", "--no-cache", "."],
        ["venv-python", "-m", "mypy", *modules, "tests", "scripts"],
        ["venv-python", "-m", "bandit", "-q", "-r", *modules],
    ]
