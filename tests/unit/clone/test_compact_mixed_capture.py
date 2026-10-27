from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from core.infrastructure.clone import compact_volume_capture


def _run_capture(
    monkeypatch: pytest.MonkeyPatch,
    *,
    validate: bool = False,
    durable_state_only: bool = True,
) -> list[dict]:
    output = io.StringIO()
    monkeypatch.setattr(
        "sys.argv",
        [
            "capture",
            *(["--durable-state-only"] if durable_state_only else []),
            *(["--validate-only"] if validate else []),
        ],
    )
    monkeypatch.setattr(
        "sys.stdin", io.StringIO('[{"data_class":"durable_state","archive_root":"state"}]')
    )
    monkeypatch.setattr("sys.stdout", output)
    assert compact_volume_capture.main() == 0
    return json.loads(output.getvalue())


def test_mixed_compact_capture_excludes_sibling_bootstrap_and_retained_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    shell = source / "state/.flow-steward"
    shell.mkdir(parents=True)
    (source / "state/bootstrap.json").write_text("not-clone-data")
    (source / "state/runtime-secrets").mkdir()
    (source / "state/runtime-secrets/secret").write_text("not-clone-data")
    (shell / "stack.toml").write_text("retained-target")
    (shell / "product.json").write_text('{"synthetic":true}')
    (shell / "product.json").chmod(0o640)
    capture = tmp_path / "capture"
    capture.mkdir()
    monkeypatch.setattr(compact_volume_capture, "_SOURCE", source)
    monkeypatch.setattr(compact_volume_capture, "_CAPTURE", capture)

    entries = _run_capture(monkeypatch)

    assert [entry["path"] for entry in entries] == ["state", "state/product.json"]
    assert entries[1]["mode"] == 0o640
    assert (capture / "state/product.json").read_text() == '{"synthetic":true}'
    assert sorted(path.name for path in (capture / "state").iterdir()) == ["product.json"]


@pytest.mark.parametrize("durable_state_only", (False, True))
def test_native_compact_capture_excludes_local_credential_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, durable_state_only: bool
) -> None:
    source = tmp_path / "source"
    state = source / "state"
    shell = state / ".flow-steward" if durable_state_only else state
    shell.mkdir(parents=True)
    (shell / "product.json").write_text("synthetic-product")
    for name in ("secret-input", "secret-output"):
        credential = shell / name / "synthetic-credential"
        credential.parent.mkdir(mode=0o700)
        credential.write_text("synthetic-local-only")
        credential.chmod(0o600)
    capture = tmp_path / "capture"
    capture.mkdir()
    monkeypatch.setattr(compact_volume_capture, "_SOURCE", source)
    monkeypatch.setattr(compact_volume_capture, "_CAPTURE", capture)

    entries = _run_capture(monkeypatch, durable_state_only=durable_state_only)

    assert [entry["path"] for entry in entries] == ["state", "state/product.json"]
    assert sorted(path.name for path in (capture / "state").iterdir()) == ["product.json"]
    assert (capture / "state/product.json").read_text() == "synthetic-product"
    for name in ("secret-input", "secret-output"):
        assert (shell / name / "synthetic-credential").read_text() == "synthetic-local-only"


@pytest.mark.parametrize("symlink_component", ["state", ".flow-steward"])
def test_mixed_compact_capture_rejects_symlinked_source_components(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, symlink_component: str
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    if symlink_component == "state":
        (outside / ".flow-steward").mkdir()
        (source / "state").symlink_to(outside, target_is_directory=True)
    else:
        (source / "state").mkdir()
        (source / "state/.flow-steward").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(compact_volume_capture, "_SOURCE", source)

    with pytest.raises(ValueError, match="real directory"):
        _run_capture(monkeypatch, validate=True)
