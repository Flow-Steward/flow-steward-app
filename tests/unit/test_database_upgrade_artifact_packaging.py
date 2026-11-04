"""Upgrade inputs must survive the same filtering used by release images."""

from __future__ import annotations

import hashlib
import json
import shlex
from pathlib import Path, PurePosixPath

import pytest

from tests.unit.test_runtime_source_slice import _source_slice_module

ROOT = Path(__file__).resolve().parents[2]
RELEASED_MANIFEST = "db/released_migrations.json"
KNOWN_RELEASED_SQL = {
    "db/migrations/20261101153533_inbound_oauth_tables.sql",
    "db/migrations/20261103090000_retired_agent_type.sql",
    "db/migrations/20261103090000_secret_catalog.sql",
}


def _upgrade_artifacts() -> set[str]:
    released = json.loads((ROOT / RELEASED_MANIFEST).read_text(encoding="utf-8"))
    migrations = {f"db/migrations/{name}" for name in released["migrations"]}
    assert migrations >= KNOWN_RELEASED_SQL
    return {RELEASED_MANIFEST, *migrations}


@pytest.mark.parametrize("role", ("app", "worker", "control", "compact"))
def test_materialized_runtime_contains_complete_released_upgrade_artifacts(
    tmp_path: Path, role: str
) -> None:
    output = tmp_path / role
    _source_slice_module().materialize_source_slice(
        source_root=ROOT,
        manifest_path=ROOT / "infra/docker/source-slices" / f"{role}.json",
        output_root=output,
    )

    required = _upgrade_artifacts()
    missing = sorted(path for path in required if not (output / path).is_file())
    assert not missing, f"{role} release slice loses database upgrade inputs: {missing}"
    for path in sorted(required):
        assert (output / path).read_bytes() == (ROOT / path).read_bytes(), path
    released = json.loads((output / RELEASED_MANIFEST).read_text(encoding="utf-8"))
    for name, checksum in released["migrations"].items():
        assert (
            hashlib.sha256((output / "db/migrations" / name).read_bytes()).hexdigest() == checksum
        )


def test_retained_cli_copies_complete_released_upgrade_artifacts() -> None:
    dockerfile = (ROOT / "infra/docker/Dockerfile").read_text(encoding="utf-8")
    cli = dockerfile.split("FROM base AS cli\n", 1)[1].split("\nFROM ", 1)[0]
    copies: list[tuple[str, str]] = []
    for instruction in cli.replace("\\\n", " ").splitlines():
        if not instruction.startswith("COPY "):
            continue
        tokens = shlex.split(instruction, comments=True)
        paths = [token for token in tokens[1:] if not token.startswith("--")]
        copies.extend((source, paths[-1]) for source in paths[:-1])

    def included(artifact: str) -> bool:
        for source, destination in copies:
            if source.endswith("/") and artifact.startswith(source):
                target = PurePosixPath(destination) / artifact.removeprefix(source)
            elif source == artifact:
                target = PurePosixPath(destination)
                if destination.endswith("/"):
                    target /= PurePosixPath(source).name
            else:
                continue
            if target.as_posix() == artifact:
                return True
        return False

    missing = sorted(path for path in _upgrade_artifacts() if not included(path))
    assert not missing, f"retained CLI image loses database upgrade inputs: {missing}"
