"""Run a standalone extension checkout's checks inside the quality gate.

`lint.sh --checks extension-static,extension-tests --extension-root <dir>` streams
the checkout into the disposable fs-audit container and runs this module there.
The copy is staged under a parent folder named after the bundle's canonical
folder (`acme.crm-sync` -> `acme_crm_sync`), the layout
`extensions validate` and `extensions test` expect.

`tests` is the `extensions test` path: validation, then pytest in an offline
venv built from the bundle's own wheels. `static` runs the static steps an
extension repository's own `scripts/ci.sh` runs, with the gate image's tools.
Neither step reaches the network, so dependency auditing (`pip-audit`) is not
part of this gate.
"""

from __future__ import annotations

import argparse
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path

import yaml

from core.application.extensions.identity import extension_folder_name
from core.application.tooling.extensions.models import ExtensionTestRequest
from core.application.tooling.extensions.test_extension import (
    _run,
    _standalone_test_env,
    standalone_python,
    test_extension,
)

#: Version-control and tool state that is never part of a bundle.
_UNSTAGED = (
    ".git",
    ".venv",
    ".fs-python",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
)
#: Top-level folders that hold no bundle runtime modules.
_NOT_MODULES = frozenset({"tests", "scripts", "wheels", "dev-wheels", *_UNSTAGED})


def manifest_extension_id(source: Path) -> str:
    """Return `extension_id` from the checkout's `extension.yaml`."""
    payload = yaml.safe_load((source / "extension.yaml").read_text(encoding="utf-8")) or {}
    extension_id = (
        str(payload.get("extension_id") or "").strip() if isinstance(payload, dict) else ""
    )
    if not extension_id:
        raise ValueError(f"{source / 'extension.yaml'} declares no extension_id")
    return extension_id


def stage_extension(source: Path, extension_id: str, parent: Path) -> Path:
    """Copy ``source`` to ``parent/<canonical folder>`` and return that folder."""
    extension_dir = parent / extension_folder_name(extension_id)
    shutil.copytree(source, extension_dir, ignore=shutil.ignore_patterns(*_UNSTAGED))
    return extension_dir


def bundle_modules(extension_dir: Path) -> list[str]:
    """Return the bundle's own runtime modules: top-level files and packages.

    Tests, scripts and vendored wheels are support code, and the root
    `__init__.py` only marks the bundle folder as a package.
    """
    modules: list[str] = []
    for entry in sorted(extension_dir.iterdir()):
        if (entry.is_file() and entry.suffix == ".py" and entry.name != "__init__.py") or (
            entry.is_dir()
            and entry.name not in _NOT_MODULES
            and not entry.name.startswith(".")
            and any(entry.rglob("*.py"))
        ):
            modules.append(entry.name)
    return modules


def _static_steps(extension_dir: Path, python: str) -> list[list[str]]:
    modules = bundle_modules(extension_dir)
    support = [name for name in ("tests", "scripts") if (extension_dir / name).is_dir()]
    steps: list[list[str]] = []
    if (extension_dir / "scripts" / "validate_bundle.py").is_file():
        steps.append([python, "scripts/validate_bundle.py"])
    steps += [
        [python, "-m", "compileall", "-q", *modules, *support],
        [python, "-m", "ruff", "check", "--no-cache", "."],
        [python, "-m", "ruff", "format", "--check", "--no-cache", "."],
        [python, "-m", "mypy", *modules, *support],
        [python, "-m", "bandit", "-q", "-r", *modules],
    ]
    return steps


def check_extension_static(extension_dir: Path) -> int:
    """Run every static step to the end and return the number that failed."""
    if not bundle_modules(extension_dir):
        print(f"[FAIL] no bundle modules found in {extension_dir}", flush=True)
        return 1
    with standalone_python(extension_dir, extension_dir.name) as python:
        if python is None:
            return 1
        env = _standalone_test_env(extension_dir, python)
        return sum(
            int(_run(step, cwd=extension_dir, env=env) != 0)
            for step in _static_steps(extension_dir, python)
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("stage", choices=("static", "tests"))
    parser.add_argument("--source", required=True, help="Extension checkout to stage.")
    parser.add_argument("--extension-id", default="", help="Default: from extension.yaml.")
    parser.add_argument(
        "--test-path",
        action="append",
        default=[],
        help="Pytest target relative to the bundle; repeatable. Selected targets run fail-fast.",
    )
    args = parser.parse_args(argv)
    source = Path(args.source).resolve()
    declared = manifest_extension_id(source)
    extension_id = args.extension_id.strip() or declared
    if extension_id != declared:
        print(
            f"[FAIL] --extension-id {extension_id} does not match extension.yaml ({declared})",
            flush=True,
        )
        return 2

    with tempfile.TemporaryDirectory(prefix="fs-extension-gate-") as parent:
        extension_dir = stage_extension(source, extension_id, Path(parent))
        print(f"[extension] {extension_id} staged at {extension_dir}", flush=True)
        if args.stage == "static":
            failures = check_extension_static(extension_dir)
            print(
                f"[summary] extension={extension_id} static ok={str(not failures).lower()}",
                flush=True,
            )
            return int(bool(failures))
        result = test_extension(
            ExtensionTestRequest(
                extension_id=extension_id,
                root=parent,
                test_paths=tuple(args.test_path),
            )
        )
        print(result.output, end="", flush=True)
        return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
