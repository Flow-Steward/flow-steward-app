"""Typed embedded OCI identity verification for Compact startup."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class EmbeddedBuildIdentity:
    """Identity that can be baked into an image without digest circularity."""

    revision: str
    role: str
    platform: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> EmbeddedBuildIdentity:
        expected = set(cls.__dataclass_fields__)
        if set(value) != expected:
            raise ValueError("embedded build identity has an invalid shape")
        normalized = {name: str(value[name] or "").strip() for name in expected}
        if any(not item for item in normalized.values()):
            raise ValueError("embedded build identity fields must not be empty")
        return cls(**normalized)


@dataclass(frozen=True, slots=True)
class EmbeddedImageIdentity:
    index_digest: str
    manifest_digest: str
    config_digest: str
    revision: str
    role: str
    platform: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> EmbeddedImageIdentity:
        expected = set(cls.__dataclass_fields__)
        if set(value) != expected:
            raise ValueError("embedded image identity has an invalid shape")
        normalized = {name: str(value[name] or "").strip() for name in expected}
        if any(not item for item in normalized.values()):
            raise ValueError("embedded image identity fields must not be empty")
        return cls(**normalized)

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def _identity_mismatch_error(
    *,
    expected: object,
    actual: object,
    field_names: Iterable[str],
) -> ValueError | None:
    """Describe every mismatched field at once, with both values.

    Returning on the first mismatch made an operator rebuild and reinstall once
    per wrong field, and naming the field without its values left them guessing
    what the image actually carried. These are build provenance values (digests,
    revision, role, platform), never secrets, so they are safe to print.
    """
    mismatches = [
        f"{name.replace('_', ' ')} (expected {getattr(expected, name)!r}, "
        f"actual {getattr(actual, name)!r})"
        for name in field_names
        if getattr(expected, name) != getattr(actual, name)
    ]
    if not mismatches:
        return None
    return ValueError("embedded image does not match release metadata: " + "; ".join(mismatches))


def verify_embedded_image_identity(
    *,
    expected: EmbeddedImageIdentity,
    actual: EmbeddedImageIdentity,
) -> None:
    error = _identity_mismatch_error(
        expected=expected, actual=actual, field_names=expected.__dataclass_fields__
    )
    if error is not None:
        raise error


def verify_compact_environment_identity(environ: Mapping[str, str]) -> EmbeddedBuildIdentity:
    """Verify only non-circular build identity inside a platform runtime.

    Registry/index/manifest/config digests are deployment evidence. They cannot
    be independently discovered from inside an OCI container and are therefore
    deliberately excluded from this runtime comparison.

    The comparison itself is kept: release metadata that disagrees with the image it
    describes is a real fault and must stop the runtime. What changed in AC17-R109 is
    that "no release metadata was supplied" is no longer read as "release metadata is
    empty". Those were indistinguishable because emptiness was rejected before the
    comparison, which made a published image refuse to start anywhere our installer
    had not been -- and say only that a field was empty, without naming the side or
    the variables.
    """
    fields = {
        "revision": "IMAGE_REVISION",
        "role": "IMAGE_ROLE",
        "platform": "IMAGE_PLATFORM",
    }
    embedded = {field: environ.get(f"FS_EMBEDDED_{suffix}", "") for field, suffix in fields.items()}
    if any(not str(value or "").strip() for value in embedded.values()):
        missing = sorted(
            f"FS_EMBEDDED_{fields[field]}"
            for field, value in embedded.items()
            if not str(value or "").strip()
        )
        raise ValueError(
            "this image carries no build identity of its own and cannot be verified: "
            f"{', '.join(missing)} is empty. It was built without BUILD_SHA, so rebuild "
            "it with infra/docker/Dockerfile rather than supplying the values by hand."
        )
    actual = EmbeddedBuildIdentity.from_mapping(embedded)

    declared = {field: environ.get(f"FS_COMPACT_{suffix}", "") for field, suffix in fields.items()}
    present = {field for field, value in declared.items() if str(value or "").strip()}
    if not present:
        # AC17-R109: nothing outside the image claims anything about it, which is the
        # normal state anywhere our installer did not run -- a PaaS, a `docker run`, a
        # person trying the product. The image knows its own revision, role and
        # platform because they were baked in at build time, so there is nothing to
        # compare and nothing wrong. Refusing here made the published image unable to
        # start anywhere except behind our own installer, and said only that some
        # field was empty.
        return actual
    if present != set(fields):
        missing = sorted(f"FS_COMPACT_{fields[field]}" for field in set(fields) - present)
        raise ValueError(
            "release metadata for this image is incomplete: "
            f"{', '.join(missing)} is empty while the others are set. Set all three or "
            "none -- with none, the identity baked into the image is used as it stands."
        )
    expected = EmbeddedBuildIdentity.from_mapping(declared)
    error = _identity_mismatch_error(expected=expected, actual=actual, field_names=fields)
    if error is not None:
        raise error
    return actual
