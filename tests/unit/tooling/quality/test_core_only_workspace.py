from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def test_core_only_workspace_does_not_copy_discarded_dependencies(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[4]
    source = tmp_path / "source"
    runner = source / "core/entrypoints/cli/scripts/run-core-only-tests.sh"
    runner.parent.mkdir(parents=True)
    shutil.copy2(root / "core/entrypoints/cli/scripts/run-core-only-tests.sh", runner)
    dependency = source / "assets/frontend/node_modules/dependency/secret"
    dependency.parent.mkdir(parents=True)
    dependency.write_text("not part of the isolated workspace")
    dependency.chmod(0)
    built = source / "assets/frontend/dist/index.html"
    built.parent.mkdir(parents=True)
    built.write_text("verified frontend shell")
    (source / "assets/build").write_text("ordinary file, not a build directory")
    command = (
        "from pathlib import Path; "
        "assert not Path('assets/frontend/node_modules').exists(); "
        "assert Path('assets/frontend/dist/index.html').read_text() == 'verified frontend shell'; "
        "assert Path('assets/build').read_text() == 'ordinary file, not a build directory'"
    )
    result = subprocess.run(
        ["bash", str(runner), sys.executable, "-c", command],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
