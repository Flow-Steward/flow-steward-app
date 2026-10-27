"""Synchronize the tracked environment documentation artifacts."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from core.application.tooling.ops.generate_env_catalog import render_env_catalog_markdown
from core.infrastructure.config.env_audit import build_env_audit

ROOT = Path(__file__).resolve().parents[4]
AUDIT_PATH = Path("docs/configuration/flow-steward-env-audit.json")
CATALOG_PATH = Path("docs/configuration/flow-steward-env-catalog.md")


class EnvArtifactsOutOfDate(RuntimeError):
    """Raised when tracked outputs differ from one fresh audit snapshot."""

    def __init__(self, paths: tuple[Path, ...]) -> None:
        super().__init__("tracked environment artifacts are out of date")
        self.paths = paths


def _expected_outputs(repo_root: Path) -> dict[Path, str]:
    snapshot = build_env_audit(repo_root=repo_root)
    return {
        repo_root / AUDIT_PATH: json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
        repo_root / CATALOG_PATH: render_env_catalog_markdown(
            repo_root,
            audit_rows=snapshot,
        ),
    }


def _current_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _replace_all(outputs: dict[Path, str]) -> None:
    staged: dict[Path, Path] = {}
    try:
        for target, content in outputs.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            mode = target.stat().st_mode & 0o777 if target.exists() else 0o644
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=target.parent,
                prefix=f".{target.name}.",
                delete=False,
            ) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
                staged[target] = Path(handle.name)
            os.chmod(staged[target], mode)
        for target, temporary in staged.items():
            os.replace(temporary, target)
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)


def synchronize_env_artifacts(repo_root: Path, *, check: bool) -> tuple[Path, ...]:
    root = repo_root.resolve()
    outputs = _expected_outputs(root)
    changed = tuple(path for path, expected in outputs.items() if _current_text(path) != expected)
    if not changed:
        return ()
    if check:
        raise EnvArtifactsOutOfDate(changed)
    _replace_all(outputs)
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("sync", "check"))
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        changed = synchronize_env_artifacts(args.repo_root, check=args.mode == "check")
    except EnvArtifactsOutOfDate as exc:
        print(
            "environment artifacts are stale: " + ", ".join(str(path) for path in exc.paths),
            file=sys.stderr,
        )
        print(
            "run: python -m core.application.tooling.ops.sync_env_artifacts sync",
            file=sys.stderr,
        )
        return 1
    if args.mode == "sync" and changed:
        print("updated environment artifacts: " + ", ".join(str(path) for path in changed))
        print("review and manually stage the generated files", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
