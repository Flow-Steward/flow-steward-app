from __future__ import annotations

import json
import stat
from pathlib import Path

from core.application.release.candidate_requirements import AggregatedRequirements
from core.application.release.candidate_snapshot import SnapshotInputs
from core.application.release.candidate_store import CandidateSnapshotStore
from core.infrastructure.release_candidate.env_source import read_last_wins


def _requirements(*required_env: str) -> AggregatedRequirements:
    return AggregatedRequirements(
        check_ids=("A01",),
        groups=("A",),
        required_env=tuple(required_env),
        optional_env=(),
        services=("fs-app",),
        scopes=(),
        mutable_resources=(),
        cleanup_obligations=(),
    )


def _inputs(source_env: Path) -> SnapshotInputs:
    return SnapshotInputs(
        candidate_commit="a" * 40,
        compose_project="fs-candidate-aaaaaaaaaaaa",
        source_env=source_env,
        plain_values={"APP_PORT": "18099"},
    )


def test_snapshot_contains_hmac_but_never_raw_secret(tmp_path: Path) -> None:
    source = tmp_path / "prod.env"
    source.write_text("TOKEN=super-secret-value\n", encoding="utf-8")

    store = CandidateSnapshotStore(tmp_path / "release")
    snapshot = store.materialize(_inputs(source), requirements=_requirements("TOKEN"))

    encoded = json.dumps(snapshot.to_json())
    assert "super-secret-value" not in encoded
    assert snapshot.protected_inputs["TOKEN"].startswith("hmac-sha256:")
    assert stat.S_IMODE(store.key_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.snapshot_path.stat().st_mode) == 0o600


def test_check_only_recalculates_from_source_instead_of_stored_expected(
    tmp_path: Path,
) -> None:
    source = tmp_path / "prod.env"
    source.write_text("TOKEN=first\n", encoding="utf-8")
    store = CandidateSnapshotStore(tmp_path / "release")
    inputs = _inputs(source)
    requirements = _requirements("TOKEN")
    store.materialize(inputs, requirements=requirements)
    source.write_text("TOKEN=changed\n", encoding="utf-8")

    result = store.check(inputs, requirements=requirements)

    assert result.matches is False
    assert result.reasons == ("protected input changed: TOKEN",)


def test_environment_source_decodes_quotes_and_uses_last_assignment(tmp_path: Path) -> None:
    source = tmp_path / "prod.env"
    source.write_text(
        "TOKEN=obsolete\nexport TOKEN='current value'\nIGNORED\n",
        encoding="utf-8",
    )

    result = read_last_wins(source)

    assert result.values == {"TOKEN": "current value"}
    assert result.sha256.startswith("sha256:")
