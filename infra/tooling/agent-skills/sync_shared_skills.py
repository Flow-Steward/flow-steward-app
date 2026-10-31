#!/usr/bin/env python3
"""Copy the shared agent skills into every package that ships them.

`docs/agent-skills/shared/skills/<name>/` is the one canonical source of a skill
that more than one package ships. Each package directory must stay a
self-contained plugin root that a client can install on its own, so it carries
a byte-identical copy at `docs/agent-skills/<package>/skills/<name>/`.

Run without arguments after editing a shared skill; `--check` only reports
drift and exits 1 when a copy differs from its source.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILLS_DIR = Path("docs/agent-skills")
SHARED_SKILLS = SKILLS_DIR / "shared" / "skills"
PACKAGES = ("flow-steward-operator", "flow-steward-developer")


def _tree(root: Path) -> dict[str, bytes]:
    if not root.is_dir():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def shared_skill_names(repo_root: Path = REPO_ROOT) -> list[str]:
    source = repo_root / SHARED_SKILLS
    return sorted(path.name for path in source.iterdir() if path.is_dir())


def drift(repo_root: Path = REPO_ROOT) -> list[str]:
    """Every packaged copy that differs from its shared source, as readable lines."""
    problems: list[str] = []
    for name in shared_skill_names(repo_root):
        expected = _tree(repo_root / SHARED_SKILLS / name)
        for package in PACKAGES:
            target = SKILLS_DIR / package / "skills" / name
            actual = _tree(repo_root / target)
            for relative in sorted(expected.keys() | actual.keys()):
                if relative not in actual:
                    problems.append(f"{target.as_posix()}/{relative}: missing")
                elif relative not in expected:
                    problems.append(f"{target.as_posix()}/{relative}: not in the shared source")
                elif actual[relative] != expected[relative]:
                    problems.append(f"{target.as_posix()}/{relative}: differs from the source")
    return problems


def sync(repo_root: Path = REPO_ROOT) -> list[str]:
    """Replace every packaged copy with its shared source; return the copies written."""
    written: list[str] = []
    for name in shared_skill_names(repo_root):
        source = repo_root / SHARED_SKILLS / name
        for package in PACKAGES:
            target = repo_root / SKILLS_DIR / package / "skills" / name
            if _tree(target) == _tree(source):
                continue
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source, target)
            written.append(target.relative_to(repo_root).as_posix())
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="report drift, change nothing")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    args = parser.parse_args(argv)
    if args.check:
        problems = drift(args.repo_root)
        for line in problems:
            print(line, file=sys.stderr)
        if problems:
            print("run infra/tooling/agent-skills/sync_shared_skills.py", file=sys.stderr)
            return 1
        return 0
    for line in sync(args.repo_root):
        print(f"updated {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
