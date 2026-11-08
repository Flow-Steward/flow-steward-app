"""Product notes must exist before the release pipeline builds or publishes."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from core.application.tooling.release_notes import (
    prepare_release_notes,
    previous_published_tag,
    select_publication_notes,
)

TAG = "v0.9.13"
PREVIOUS = "v0.9.12"
NOTES = """## Flow Steward 0.9.13

### Features and improvements

- Require reviewed product notes before building official release images.

### Fixes

- Stop replacing a release description with registry availability alone.

[Full changelog: 0.9.12 → 0.9.13](https://github.com/Flow-Steward/flow-steward-app/compare/v0.9.12...v0.9.13)
"""


def write_notes(root: Path, text: str = NOTES) -> Path:
    path = root / "docs/releases/0.9.13.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_previous_release_uses_published_stable_versions_below_target() -> None:
    releases = [
        {"tag_name": "v0.9.10"},
        {"tag_name": "v0.9.12"},
        {"tag_name": TAG},
        {"tag_name": "v0.10.0"},
        {"tag_name": "v0.9.12-beta.1"},
        {"tag_name": "v0.9.12", "draft": True},
        {"tag_name": "v0.9.12", "prerelease": True},
        {"tag_name": "not-a-release"},
    ]
    assert previous_published_tag(releases, TAG) == PREVIOUS


def test_previous_release_does_not_treat_draft_as_a_stable_baseline() -> None:
    with pytest.raises(ValueError, match="previous published"):
        previous_published_tag([{"tag_name": PREVIOUS, "draft": True}], TAG)


def test_prepare_pins_the_exact_versioned_file(tmp_path: Path) -> None:
    path = write_notes(tmp_path)
    result = prepare_release_notes(tmp_path, TAG, PREVIOUS)
    assert result.text == NOTES
    assert result.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert result.previous_tag == PREVIOUS
    assert (
        prepare_release_notes(tmp_path, TAG, PREVIOUS, expected_sha256=result.sha256).text == NOTES
    )


def test_missing_notes_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"docs/releases/0\.9\.13\.md"):
        prepare_release_notes(tmp_path, TAG, PREVIOUS)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Registry availability: verified complete images in two registries.\n",
        NOTES.replace("Flow Steward 0.9.13", "Flow Steward 0.9.12"),
        NOTES.replace(
            "- Require reviewed product notes before building official release images.", "- TODO"
        ),
        NOTES.replace(
            "- Require reviewed product notes before building official release images.",
            "- TBD: describe the feature",
        ),
        NOTES.replace(
            "- Stop replacing a release description with registry availability alone.", ""
        ),
        NOTES.replace("v0.9.12...v0.9.13", "v0.9.11...v0.9.13"),
        NOTES.replace("v0.9.12...v0.9.13", "v0.9.12...v0.9.14"),
        "## Flow Steward 0.9.13\n\n### Release verification\n\n- All checks passed.\n",
    ],
)
def test_unreviewed_or_wrong_release_notes_are_rejected(tmp_path: Path, text: str) -> None:
    write_notes(tmp_path, text)
    with pytest.raises(ValueError, match=r"[Rr]elease notes"):
        prepare_release_notes(tmp_path, TAG, PREVIOUS)


def test_changed_notes_cannot_replace_the_file_validated_before_build(tmp_path: Path) -> None:
    write_notes(tmp_path)
    with pytest.raises(ValueError, match="digest"):
        prepare_release_notes(tmp_path, TAG, PREVIOUS, expected_sha256="0" * 64)


def test_registry_only_existing_release_is_repaired_from_reviewed_notes(tmp_path: Path) -> None:
    write_notes(tmp_path)
    notes = prepare_release_notes(tmp_path, TAG, PREVIOUS)
    assert select_publication_notes(notes, "Registry availability: GHCR.\n") == NOTES


def test_retry_preserves_valid_human_edits_and_removes_old_registry_footer(tmp_path: Path) -> None:
    write_notes(tmp_path)
    notes = prepare_release_notes(tmp_path, TAG, PREVIOUS)
    edited = NOTES.replace("### Fixes", "### Additional fixes")
    assert (
        select_publication_notes(notes, edited + "\nRegistry availability: old mirror.\n") == edited
    )


def test_fix_only_release_does_not_require_invented_features(tmp_path: Path) -> None:
    text = NOTES.replace(
        "### Features and improvements\n\n- Require reviewed product notes before building official release images.\n\n",
        "",
    )
    write_notes(tmp_path, text)
    assert prepare_release_notes(tmp_path, TAG, PREVIOUS).text == text


def test_notes_cli_validates_before_tagging_using_published_history(tmp_path: Path) -> None:
    write_notes(tmp_path)
    (tmp_path / "VERSION").write_text("0.9.13\n")
    history = tmp_path / "history.json"
    history.write_text(json.dumps([{"tag_name": "v0.9.11"}, {"tag_name": PREVIOUS}]))
    script = (
        Path(__file__).resolve().parents[2] / "core/entrypoints/cli/scripts/release-automation.py"
    )
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "notes",
            "--root",
            str(tmp_path),
            "--tag",
            TAG,
            "--release-history",
            str(history),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["previous_tag"] == PREVIOUS
    assert payload["body"] == NOTES
    assert payload["notes_sha256"] == hashlib.sha256(NOTES.encode()).hexdigest()


def test_notes_cli_refuses_a_different_version(tmp_path: Path) -> None:
    write_notes(tmp_path)
    (tmp_path / "VERSION").write_text("0.9.14\n")
    script = (
        Path(__file__).resolve().parents[2] / "core/entrypoints/cli/scripts/release-automation.py"
    )
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "notes",
            "--root",
            str(tmp_path),
            "--tag",
            TAG,
            "--previous-tag",
            PREVIOUS,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "VERSION" in result.stderr
