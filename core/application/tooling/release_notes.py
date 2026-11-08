"""Reviewed product notes bound to the immutable release candidate."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from core.application.tooling.release_automation import stable_version

_COMPARE = "https://github.com/Flow-Steward/flow-steward-app/compare/"
_TECHNICAL_SECTIONS = {
    "registry availability",
    "release verification",
    "upgrade note",
    "upgrade notes",
    "changelog",
    "full changelog",
}


@dataclass(frozen=True)
class ReleaseNotes:
    tag: str
    previous_tag: str
    text: str
    sha256: str


def _version_key(tag: str) -> tuple[int, ...]:
    return tuple(int(part) for part in stable_version(tag).split("."))


def previous_published_tag(releases: Iterable[Mapping[str, object]], tag: str) -> str:
    """Choose the latest lower stable version, not a draft or the retry itself."""
    target = _version_key(tag)
    candidates = []
    for release in releases:
        candidate = release.get("tag_name")
        if release.get("draft") or release.get("prerelease") or not isinstance(candidate, str):
            continue
        try:
            key = _version_key(candidate)
        except ValueError:
            continue
        if key < target:
            candidates.append((key, candidate))
    if not candidates:
        raise ValueError("Release notes require a previous published stable release")
    return max(candidates)[1]


def _without_registry_footer(text: str) -> str:
    return re.sub(r"(?m)^Registry availability:[^\n]*(?:\n|$)", "", text).strip()


def _validate_notes(text: str, tag: str, previous_tag: str) -> str:
    version = stable_version(tag)
    if _version_key(previous_tag) >= _version_key(tag):
        raise ValueError("Release notes require an earlier changelog baseline")
    body = _without_registry_footer(text.replace("\r\n", "\n"))
    visible = re.sub(
        r"<!--.*?-->|<details\b.*?</details>", "", body, flags=re.DOTALL | re.IGNORECASE
    )
    if not re.search(rf"(?m)^#{{1,2}} Flow Steward {re.escape(version)}\s*$", visible):
        raise ValueError(f"Release notes title must identify Flow Steward {version}")
    sections = re.split(r"(?m)^### (.+?)\s*$", visible)
    product_sections = 0
    for heading, content in zip(sections[1::2], sections[2::2], strict=True):
        if heading.strip().casefold() in _TECHNICAL_SECTIONS:
            continue
        bullets = re.findall(r"(?m)^\s*[-*] +(.+)$", content)
        if not bullets or any(
            re.match(r"^(?:TODO\b|TBD\b|\.{3}$|<[^>]+>$)", item.strip("*_` "), re.IGNORECASE)
            for item in bullets
        ):
            raise ValueError(f"Release notes section {heading!r} needs concrete product changes")
        product_sections += 1
    if not product_sections:
        raise ValueError("Release notes need product-facing features, improvements or fixes")
    comparison = _COMPARE + previous_tag + "..." + tag
    if f"]({comparison})" not in visible:
        raise ValueError(f"Release notes must link the full changelog: {comparison}")
    return body + "\n"


def prepare_release_notes(
    root: Path,
    tag: str,
    previous_tag: str,
    *,
    expected_sha256: str | None = None,
) -> ReleaseNotes:
    """Validate the versioned source; never synthesize product changes."""
    version = stable_version(tag)
    path = root / "docs" / "releases" / f"{version}.md"
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise ValueError(f"Release notes missing: docs/releases/{version}.md") from exc
    digest = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None and expected_sha256 != digest:
        raise ValueError("Release notes digest differs from the candidate validated before build")
    text = _validate_notes(raw.decode("utf-8"), tag, previous_tag)
    return ReleaseNotes(tag, previous_tag, text, digest)


def select_publication_notes(notes: ReleaseNotes, existing_body: str = "") -> str:
    """Keep valid human edits on retry; replace technical-only descriptions."""
    if existing_body.strip():
        try:
            return _validate_notes(existing_body, notes.tag, notes.previous_tag)
        except ValueError:
            pass
    return notes.text
