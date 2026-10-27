"""Opt-in helpers for destructive Clone acceptance on disposable installations.

The module deliberately owns no Docker resources by default. Live callers must
declare the two installation roots and the exact resources they created so the
result can prove cleanup instead of treating a green command exit as evidence.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class DomainJourney:
    journey_id: str
    source_host: str
    target_host: str
    dns_cutover: bool = False


DOMAIN_JOURNEYS = (
    DomainJourney("same-domain", "production.com", "production.com"),
    DomainJourney("different-domain", "dev.production.com", "production.com"),
    DomainJourney("production-to-dev", "production.com", "dev.production.com"),
    DomainJourney(
        "clone-domain-cutover",
        "production.com",
        "clone.production.com",
        dns_cutover=True,
    ),
)


@dataclass(frozen=True, slots=True)
class CloneAcceptanceCase:
    case_id: str
    profile: Literal["compact", "scalable"]
    expected_status: Literal["passed", "rejected", "resumed"]
    mutation_expected: bool
    prerequisite: str = ""


CLONE_ACCEPTANCE_CASES = (
    CloneAcceptanceCase("compact-to-compact", "compact", "passed", True),
    CloneAcceptanceCase("scalable-to-scalable", "scalable", "passed", True),
    CloneAcceptanceCase("populated-target", "compact", "passed", True),
    CloneAcceptanceCase("confirmation-declined", "scalable", "rejected", False),
    CloneAcceptanceCase("corrupt-archive", "compact", "rejected", False),
    CloneAcceptanceCase("wrong-passphrase", "scalable", "rejected", False),
    CloneAcceptanceCase(
        "exact-release-unavailable",
        "compact",
        "rejected",
        False,
        "The archived exact release is absent from every configured registry.",
    ),
    CloneAcceptanceCase(
        "registry-fallback",
        "scalable",
        "passed",
        True,
        "GHCR is blocked and Docker Hub has one complete verified role set.",
    ),
    CloneAcceptanceCase("external-effects", "compact", "passed", True),
    CloneAcceptanceCase("interruption-resume", "scalable", "resumed", True),
)


class CloneAcceptanceInputs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_root: Path | None = None
    target_root: Path | None = None
    driver: Path | None = None
    release_version: str = ""
    release_tag: str = ""
    release_commit: str = ""
    image_digests: dict[str, str] = Field(default_factory=dict)
    owned_resources: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return not self.blockers

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> CloneAcceptanceInputs:
        values = dict(os.environ if environment is None else environment)
        required = {
            "FS_CLONE_ACCEPTANCE_SOURCE_ROOT": values.get("FS_CLONE_ACCEPTANCE_SOURCE_ROOT", ""),
            "FS_CLONE_ACCEPTANCE_TARGET_ROOT": values.get("FS_CLONE_ACCEPTANCE_TARGET_ROOT", ""),
            "FS_CLONE_ACCEPTANCE_DRIVER": values.get("FS_CLONE_ACCEPTANCE_DRIVER", ""),
            "FS_CLONE_ACCEPTANCE_VERSION": values.get("FS_CLONE_ACCEPTANCE_VERSION", ""),
            "FS_CLONE_ACCEPTANCE_TAG": values.get("FS_CLONE_ACCEPTANCE_TAG", ""),
            "FS_CLONE_ACCEPTANCE_COMMIT": values.get("FS_CLONE_ACCEPTANCE_COMMIT", ""),
            "FS_CLONE_ACCEPTANCE_IMAGE_DIGESTS_JSON": values.get(
                "FS_CLONE_ACCEPTANCE_IMAGE_DIGESTS_JSON", ""
            ),
            "FS_CLONE_ACCEPTANCE_OWNED_RESOURCES_JSON": values.get(
                "FS_CLONE_ACCEPTANCE_OWNED_RESOURCES_JSON", ""
            ),
        }
        blockers = tuple(sorted(name for name, value in required.items() if not value.strip()))
        if blockers:
            return cls(blockers=blockers)
        try:
            digests = json.loads(required["FS_CLONE_ACCEPTANCE_IMAGE_DIGESTS_JSON"])
            resources = json.loads(required["FS_CLONE_ACCEPTANCE_OWNED_RESOURCES_JSON"])
            if not isinstance(digests, dict) or not isinstance(resources, list):
                raise ValueError
            normalized_digests = {str(key): str(value) for key, value in digests.items()}
            normalized_resources = tuple(str(item) for item in resources)
        except (TypeError, ValueError, json.JSONDecodeError):
            return cls(blockers=("invalid Clone acceptance JSON inputs",))
        invalid: list[str] = []
        if required["FS_CLONE_ACCEPTANCE_TAG"] != ("v" + required["FS_CLONE_ACCEPTANCE_VERSION"]):
            invalid.append("release tag/version mismatch")
        if _SHA.fullmatch(required["FS_CLONE_ACCEPTANCE_COMMIT"]) is None:
            invalid.append("invalid release commit")
        if not normalized_digests or any(
            _DIGEST.fullmatch(value) is None for value in normalized_digests.values()
        ):
            invalid.append("invalid image digest inventory")
        if not normalized_resources:
            invalid.append("empty owned resource inventory")
        return cls(
            source_root=Path(required["FS_CLONE_ACCEPTANCE_SOURCE_ROOT"]),
            target_root=Path(required["FS_CLONE_ACCEPTANCE_TARGET_ROOT"]),
            driver=Path(required["FS_CLONE_ACCEPTANCE_DRIVER"]),
            release_version=required["FS_CLONE_ACCEPTANCE_VERSION"],
            release_tag=required["FS_CLONE_ACCEPTANCE_TAG"],
            release_commit=required["FS_CLONE_ACCEPTANCE_COMMIT"],
            image_digests=normalized_digests,
            owned_resources=normalized_resources,
            blockers=tuple(invalid),
        )


class CloneAcceptanceEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    candidate_sha: str
    release_version: str
    release_tag: str
    release_commit: str
    image_digests: dict[str, str]
    source_installation_id: str
    target_installation_id: str
    assertions: dict[str, bool]
    owned_resources: tuple[str, ...]
    cleanup_results: dict[str, Literal["removed", "retained", "missing"]]
    status: Literal["passed", "rejected", "resumed", "blocked"]

    @model_validator(mode="after")
    def require_complete_exact_evidence(self) -> CloneAcceptanceEvidence:
        if (
            _SHA.fullmatch(self.candidate_sha) is None
            or _SHA.fullmatch(self.release_commit) is None
        ):
            raise ValueError("acceptance evidence requires full commit identities")
        if self.release_tag != f"v{self.release_version}":
            raise ValueError("acceptance release tag and version do not match")
        if not self.image_digests or any(
            _DIGEST.fullmatch(value) is None for value in self.image_digests.values()
        ):
            raise ValueError("acceptance evidence requires exact image digests")
        if set(self.cleanup_results) != set(self.owned_resources):
            raise ValueError("cleanup evidence must cover every owned resource")
        if not self.assertions:
            raise ValueError("acceptance evidence requires explicit assertions")
        return self


class CommandRunner(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        stdin: bytes,
    ) -> Mapping[str, object]: ...


class CloneCommandHarness:
    def __init__(self, *, run: CommandRunner, source_root: Path, target_root: Path) -> None:
        self._run = run
        self._source_root = Path(source_root)
        self._target_root = Path(target_root)

    def create(self, *, output_directory: Path, passphrase: bytes) -> Mapping[str, object]:
        return self._run(
            (
                "flow-steward",
                "clone",
                "create",
                "--output-directory",
                str(output_directory),
                "--passphrase-stdin",
                "--confirm",
                "--json",
                "--non-interactive",
            ),
            cwd=self._source_root,
            stdin=_secret_line(passphrase),
        )

    def restore(
        self,
        *,
        archive: Path,
        confirmation: str,
        passphrase: bytes,
    ) -> Mapping[str, object]:
        return self._run(
            (
                "flow-steward",
                "clone",
                "restore",
                str(archive),
                "--passphrase-stdin",
                "--confirm",
                confirmation,
                "--json",
                "--non-interactive",
            ),
            cwd=self._target_root,
            stdin=_secret_line(passphrase),
        )


def subprocess_json_runner(
    argv: Sequence[str],
    *,
    cwd: Path,
    stdin: bytes,
) -> Mapping[str, object]:
    outcome = subprocess.run(
        list(argv),
        cwd=cwd,
        input=stdin,
        capture_output=True,
        check=False,
        timeout=3600,
    )
    if outcome.returncode != 0:
        raise RuntimeError(f"Clone acceptance command failed with exit code {outcome.returncode}")
    payload = json.loads(outcome.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError("Clone acceptance command returned non-object JSON")
    return payload


def run_live_case(
    inputs: CloneAcceptanceInputs,
    case: CloneAcceptanceCase,
) -> CloneAcceptanceEvidence:
    if not inputs.ready or inputs.driver is None:
        raise ValueError("Clone acceptance inputs are blocked")
    request = {
        "case_id": case.case_id,
        "profile": case.profile,
        "expected_status": case.expected_status,
        "mutation_expected": case.mutation_expected,
        "source_root": str(inputs.source_root),
        "target_root": str(inputs.target_root),
        "release_version": inputs.release_version,
        "release_tag": inputs.release_tag,
        "release_commit": inputs.release_commit,
        "image_digests": inputs.image_digests,
        "owned_resources": inputs.owned_resources,
    }
    outcome = subprocess.run(
        (str(inputs.driver), case.case_id),
        input=json.dumps(request, sort_keys=True, separators=(",", ":")).encode(),
        capture_output=True,
        check=False,
        timeout=7200,
    )
    if outcome.returncode != 0:
        raise RuntimeError(f"Clone acceptance driver failed with exit code {outcome.returncode}")
    evidence = CloneAcceptanceEvidence.model_validate_json(outcome.stdout)
    if evidence.case_id != case.case_id or evidence.status != case.expected_status:
        raise RuntimeError("Clone acceptance evidence does not match the selected case")
    if (
        evidence.release_version != inputs.release_version
        or evidence.release_tag != inputs.release_tag
        or evidence.release_commit != inputs.release_commit
        or evidence.image_digests != inputs.image_digests
        or evidence.owned_resources != inputs.owned_resources
    ):
        raise RuntimeError("Clone acceptance evidence does not match declared exact inputs")
    return evidence


def _secret_line(passphrase: bytes) -> bytes:
    if not passphrase or b"\n" in passphrase or b"\r" in passphrase:
        raise ValueError("Clone acceptance passphrase must be one non-empty line")
    return passphrase + b"\n"


__all__ = [
    "CLONE_ACCEPTANCE_CASES",
    "DOMAIN_JOURNEYS",
    "CloneAcceptanceCase",
    "CloneAcceptanceEvidence",
    "CloneAcceptanceInputs",
    "CloneCommandHarness",
    "DomainJourney",
    "run_live_case",
    "subprocess_json_runner",
]
