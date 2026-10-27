"""Keep rollback releases and clean safe local files (AC17-R158).

Every Scalable update builds its images with scripts/release/build_candidate.sh, which
leaves .flow-steward/release/<revision>/ -- metadata, and a 3.3 GB docker-save archive --
and a flow-steward-*:candidate-<short> image set. Nothing removed either: a live host
held 48 GB of releases after one working day.

What is protected: the running revision, the one it was updated from (the rollback), any
revision the installation's env file still names, and then the newest others up to the
retention count. The image archive is removed from every release: the installer never
reads it once the images are in Docker, and a rollback records the kept revision's own
images from its release record (AC17-R159) -- which is why the previous revision's
record stays. Which revision is "previous" comes from the installation's record of its
updates, never from directory times: a build made by hand is newer and is not a rollback.
Global candidate tags, build labels and logs prove use, not exclusive ownership.
Ambiguous images and their release records stay: another installation may need the
same image for rollback even when no Docker container currently uses it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

_REVISION = re.compile(r"^[0-9a-f]{40}$")
_CANDIDATE_TAG = re.compile(r"^flow-steward-[a-z0-9-]+:candidate-(?P<short>[0-9a-f]{12})$")
_ARCHIVE = "candidate-images.tar"
CANDIDATE_IMAGE_LISTING = (
    "docker",
    "image",
    "ls",
    "--filter",
    "reference=flow-steward-*:candidate-*",
    "--format",
    "{{.Repository}}:{{.Tag}}",
)
MINIMUM_RETENTION = 2


@dataclass
class PruneReport:
    releases: list[str] = field(default_factory=list)
    freed_bytes: int = 0
    ambiguous_images: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if not (self.releases or self.ambiguous_images or self.freed_bytes):
            return ""
        # R159 acceptance: "Removed 0 superseded releases and 0 candidate images,
        # freeing 3.1 GB" -- when all that went was image archives, say that.
        removed = [
            text
            for count, text in ((len(self.releases), f"{len(self.releases)} superseded releases"),)
            if count
        ] or ["image archives"]
        parts = (
            [
                f"Removed {' and '.join(removed)}, freeing {_gigabytes(self.freed_bytes)} of "
                f"release files; kept {', '.join(revision[:12] for revision in self.kept)}."
            ]
            if self.releases or self.freed_bytes
            else []
        )
        if self.ambiguous_images:
            parts.append(
                "Retained images and associated release records: exclusive installation "
                "ownership is unproven; another installation may need them for rollback: "
                + ", ".join(sorted(self.ambiguous_images))
                + "."
            )
        return " ".join(parts)


def retention_count(raw: str | None) -> int:
    """The configured count, never below the current release and its rollback."""
    text = str(raw or "").strip()
    return max(MINIMUM_RETENTION, int(text)) if text.isdigit() else MINIMUM_RETENTION


_RELEASE_REFERENCE = re.compile(r"\.flow-steward/release/(?P<revision>[0-9a-f]{40})(?![0-9a-f])")


def referenced_revisions(env_values: Mapping[str, str]) -> set[str]:
    """Revisions whose release directory a value in the installation's env names.

    Matched on the path's tail, not the whole path: the installer may see the checkout
    under another prefix than the one written into the env file.
    """
    marker = _RELEASE_REFERENCE
    return {
        match.group("revision")
        for value in env_values.values()
        for match in marker.finditer(str(value or ""))
    }


def kept_revisions(release_root: Path, *, protected: Iterable[str], retention: int) -> list[str]:
    releases = sorted(
        (
            path
            for path in release_root.iterdir()
            if path.is_dir() and _REVISION.fullmatch(path.name)
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    names = [path.name for path in releases]
    protected = set(protected)
    kept = [name for name in names if name in protected]
    # The protected revisions take their places whether or not a directory is left.
    used = len(protected)
    for name in names:
        if used >= retention:
            break
        if name not in kept:
            kept.append(name)
            used += 1
    return kept


@dataclass(frozen=True)
class Superseded:
    """Safe local cleanup and retained ambiguity, decided without mutation."""

    kept: tuple[str, ...] = ()
    releases: tuple[str, ...] = ()  # release directories outside the kept revisions
    archives: tuple[str, ...] = ()  # local archives, never read by installed rollback
    ambiguous_images: tuple[str, ...] = ()  # global tags are not exclusive ownership

    @property
    def any(self) -> bool:
        return bool(self.releases or self.archives or self.ambiguous_images)


def superseded(
    release_root: Path,
    *,
    current: str,
    protected: Iterable[str],
    retention: int,
    candidate_tags: Iterable[str],
) -> Superseded:
    """The one decision of what is superseded, for prune() and for the doctor alike."""
    if not release_root.is_dir():
        return Superseded()
    protected = set(protected) | ({current} if current else set())
    kept = kept_revisions(release_root, protected=protected, retention=retention)
    releases = sorted(
        path for path in release_root.iterdir() if path.is_dir() and _REVISION.fullmatch(path.name)
    )
    # A local release record proves use, not exclusive ownership. Another installation
    # may retain the same global tag for rollback without a live Docker container.
    ours = {path.name[:12] for path in releases}
    # A protected revision keeps its images even when its release directory is gone.
    kept_shorts = {revision[:12] for revision in (*kept, *protected)}
    images = []
    for tag in sorted(set(candidate_tags)):
        match = _CANDIDATE_TAG.fullmatch(tag.strip())
        if match is None or match.group("short") in kept_shorts:
            continue
        if match.group("short") in ours:
            images.append(tag)
    return Superseded(
        kept=tuple(kept),
        releases=tuple(
            path.name
            for path in releases
            if path.name not in kept and not any(path.name[:12] in tag for tag in images)
        ),
        archives=tuple(path.name for path in releases if (path / _ARCHIVE).is_file()),
        ambiguous_images=tuple(images),
    )


def prune(
    release_root: Path,
    *,
    current: str,
    protected: Iterable[str],
    retention: int,
    candidate_tags: Iterable[str],
) -> PruneReport:
    """Remove safe local files; retain globally shared images and their records.

    Idempotent: interruption leaves the remaining local files for the next run.
    Docker's in-use check cannot prove the absence of another installation's rollback.
    """
    report = PruneReport()
    plan = superseded(
        release_root,
        current=current,
        protected=protected,
        retention=retention,
        candidate_tags=candidate_tags,
    )
    report.kept = list(plan.kept)
    report.ambiguous_images = list(plan.ambiguous_images)
    for name in plan.archives:
        archive = release_root / name / _ARCHIVE
        directory_times = archive.parent.stat()
        report.freed_bytes += archive.stat().st_size
        try:
            archive.unlink()
        finally:
            # Legacy installations without a previous-release record fall back to
            # directory age. Housekeeping must not make an older build look newer.
            os.utime(archive.parent, ns=(directory_times.st_atime_ns, directory_times.st_mtime_ns))
    for name in plan.releases:
        path = release_root / name
        report.freed_bytes += _tree_size(path)
        shutil.rmtree(path)
        report.releases.append(name)
    return report


# -- Compact (AC17-R178) --------------------------------------------------------------
#
# A Compact update builds flow-steward-compact:<installation>-<revision> and keeps no
# release directory, so nothing above ever saw its images: fs-acc held 31 after one day (26.7 GB of
# images, 12.8 GB reclaimable). The same rule as Scalable -- the running revision, its
# rollback, the newest others up to the retention count -- over the images this
# installation built: labelled with its Compose project since R178, or, for the ones
# built before, recorded by exact image ID in its own install logs. These establish
# inventory, not exclusive ownership: automatic global image deletion is refused.

COMPACT_REPOSITORY = "flow-steward-compact"
INSTALLATION_LABEL = "com.flow-steward.installation"
_COMPACT_BUILT = re.compile(
    r"Built Compact runtime image \S+ \((?P<image>sha256:[0-9a-f]{64})\) at revision "
    r"(?P<revision>[0-9a-f]{40})\."
)


@dataclass(frozen=True)
class CompactImage:
    image_id: str
    revision: str
    created: str  # RFC 3339, as Docker reports it: newest first sorts it
    size: int = 0
    tags: tuple[str, ...] = ()


def compact_images_built_in(logs_dir: Path) -> set[str]:
    """The image IDs this installation's own install logs record building."""
    try:
        logs = sorted(logs_dir.glob("install-*.log"))
    except OSError:
        return set()
    built: set[str] = set()
    for log in logs:
        try:
            text = log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        built.update(match.group("image") for match in _COMPACT_BUILT.finditer(text))
    return built


@dataclass(frozen=True)
class CompactPlan:
    kept: tuple[str, ...] = ()  # revisions
    ambiguous_images: tuple[CompactImage, ...] = ()  # beyond them, ownership unproven


def compact_superseded(
    images: Iterable[CompactImage],
    *,
    protected: Iterable[str],
    current_image: str,
    retention: int,
) -> CompactPlan:
    """Which of this installation's Compact images are beyond the retention.

    Only an image whose every tag is a flow-steward-compact one is a candidate: a tag of
    anyone else's keeps it. The image .env.prod records is never one, whatever its label.
    """
    ordered = sorted(images, key=lambda image: image.created, reverse=True)
    protected = [revision for revision in dict.fromkeys(protected) if revision]
    kept = list(protected)
    for revision in dict.fromkeys(image.revision for image in ordered):
        if len(kept) >= max(retention, len(protected)):
            break
        if revision and revision not in kept:
            kept.append(revision)
    beyond = tuple(
        image
        for image in ordered
        if image.revision not in kept
        and image.image_id != current_image
        and all(tag.startswith(f"{COMPACT_REPOSITORY}:") for tag in image.tags)
    )
    return CompactPlan(kept=tuple(kept), ambiguous_images=beyond)


@dataclass
class CompactPruneReport:
    retained: tuple[CompactImage, ...] = ()
    kept: tuple[str, ...] = ()

    def summary(self) -> str:
        if not self.retained:
            return ""
        return (
            f"Retained {len(self.retained)} Compact images: exclusive installation ownership "
            "is unproven; build labels and logs cannot exclude another installation's rollback. "
            f"Retention protects {', '.join(revision[:12] for revision in self.kept)}."
        )


_UPDATED = re.compile(r"Updated from (?P<previous>[0-9a-f]{40}) to (?P<current>[0-9a-f]{40})\.")
RECORD_NAME = "installed-releases.json"


def read_record(state_root: Path) -> dict[str, str]:
    try:
        payload = json.loads((state_root / RECORD_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        key: str(payload.get(key) or "")
        for key in ("current", "previous")
        if _REVISION.fullmatch(str(payload.get(key) or ""))
    }


def previous_from_logs(logs_dir: Path, current: str) -> str:
    """For an installation older than the record: what its last update to `current` replaced."""
    try:
        logs = sorted(logs_dir.glob("install-*.log"), reverse=True)
    except OSError:
        return ""
    for log in logs:
        try:
            text = log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in reversed(list(_UPDATED.finditer(text))):
            if match.group("current") == current:
                return match.group("previous")
    return ""


def _tree_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _gigabytes(size: int) -> str:
    return f"{size / 1024**3:.1f} GB" if size >= 1024**3 else f"{size / 1024**2:.0f} MB"
