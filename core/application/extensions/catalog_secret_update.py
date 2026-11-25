"""Host-owned source and trust for catalog updates that retain secret approvals."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from core.application.management.errors import ServiceConflictError
from core.application.management.scope import ScopeContext, normalize_scope

_TRUST_KEY = "catalog_secret_update_trust"
_REPOSITORY_PART = re.compile(r"[A-Za-z0-9_.-]+")
_ASSET_PART = re.compile(r"[A-Za-z0-9_.+-]+")


def catalog_release_repository(repository_url: str, archive_url: str) -> str:
    """Return the repository only when the catalog downloads a release from it."""
    repository, archive = urlsplit(repository_url), urlsplit(archive_url)
    if any(
        url.scheme != "https" or url.netloc.lower() != "github.com" for url in (repository, archive)
    ):
        return ""
    owner_and_name = repository.path.strip("/").split("/")[:2]
    source = archive.path.removeprefix("/").split("/")
    if (
        len(owner_and_name) != 2
        or len(source) != 6
        or source[2:4] != ["releases", "download"]
        or archive.query
        or archive.fragment
        or any(
            part in {".", ".."} or not _REPOSITORY_PART.fullmatch(part) for part in owner_and_name
        )
        or any(part in {".", ".."} or not _ASSET_PART.fullmatch(part) for part in source[4:])
        or [part.lower() for part in source[:2]] != [part.lower() for part in owner_and_name]
    ):
        return ""
    return "https://github.com/" + "/".join(owner_and_name).lower()


def _trust(install: Any) -> dict[str, Any]:
    provenance = getattr(install, "provenance", None)
    value = provenance.get(_TRUST_KEY) if isinstance(provenance, dict) else None
    return value if isinstance(value, dict) else {}


def catalog_repository_is_trusted(install: Any, repository: str) -> bool:
    trust = _trust(install)
    return bool(
        repository
        and getattr(install, "release_id", None)
        and trust.get("repository") == repository
        and trust.get("install_id") == getattr(install, "install_id", None)
        and trust.get("release_id") == install.release_id
    )


@dataclass(frozen=True)
class CatalogSecretUpdate:
    """Passed only by the catalog after it verifies the selected archive."""

    install_id: str
    repository: str
    requested_repository: str | None

    def trusted_repository(self, install: Any, release: dict[str, Any]) -> str:
        if self.requested_repository is not None:
            return self.requested_repository
        if (
            catalog_repository_is_trusted(install, self.repository)
            and install.release_id == release["release_id"]
            and _trust(install).get("descriptor_sha256") == release["descriptor_sha256"]
        ):
            return self.repository
        return ""

    def persist(
        self,
        conn: Any,
        *,
        scope: ScopeContext | None,
        previous_install: Any,
        previous_release: dict[str, Any],
        install_store: Any,
    ) -> None:
        """Carry exact prior approvals and record trust in the update transaction."""
        trusted_repository = self.trusted_repository(previous_install, previous_release)
        if not trusted_repository and self.requested_repository is None:
            return
        from core.application.secrets import repository
        from core.application.secrets.authority import actor_identity
        from core.application.secrets.release_approvals import carry_release_approvals

        scoped = normalize_scope(scope)
        current = install_store.get(
            extension_id=previous_install.extension_id, account_id=scoped.account_id
        )
        if current is None or current.install_id != self.install_id:
            raise ServiceConflictError("Extension installation changed during the catalog update")
        actor_kind, actor = actor_identity(scoped)
        provenance = {key: value for key, value in current.provenance.items() if key != _TRUST_KEY}
        with conn.cursor() as cur:
            candidate = repository.install_release(cur, install_id=self.install_id)
            if trusted_repository:
                if (
                    not candidate["release_id"]
                    or candidate["release_id"] != current.release_id
                    or not candidate["descriptor_sha256"]
                ):
                    raise ServiceConflictError(
                        "The catalog update did not produce a verifiable release"
                    )
                provenance[_TRUST_KEY] = {
                    "repository": trusted_repository,
                    "install_id": self.install_id,
                    **candidate,
                }
                if (
                    previous_install.runtime_mode == current.runtime_mode
                    and previous_install.remote_base_url == current.remote_base_url
                ):
                    carry_release_approvals(
                        cur,
                        account_id=scoped.account_id,
                        install_id=self.install_id,
                        previous=previous_release,
                        candidate=candidate,
                        repository=trusted_repository,
                        actor=actor,
                        actor_kind=actor_kind,
                    )
            if self.requested_repository is not None:
                repository.record_audit(
                    cur,
                    account_id=scoped.account_id,
                    command_type="secret.catalog_update_trust",
                    actor_id=actor,
                    actor_kind=actor_kind,
                    target={"install_id": self.install_id},
                    payload={
                        "repository": trusted_repository,
                        "release_id": candidate["release_id"],
                    },
                )
        if (
            install_store.set_provenance(
                extension_id=current.extension_id,
                account_id=scoped.account_id,
                provenance=provenance,
            )
            is None
        ):
            raise ServiceConflictError(
                "Extension installation was removed during the catalog update"
            )
