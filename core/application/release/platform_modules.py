"""Platform-module set contracts used by the journaled release-upgrade operation (ADR 0082 §5)."""

from __future__ import annotations

import functools
from collections.abc import Callable, Mapping
from typing import Annotated, Protocol

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from core.application.extensions.semver import compare_semver, normalize_semver
from core.application.lifecycle.errors import LifecycleError

# The descriptor shape and digest belong to the module store that every caller --
# the operator CLI and the Compact runtime image -- shares; this is their contract name.
from core.infrastructure.platform_modules.module_store import (
    MODULE_DESCRIPTOR_KEYS,
    canonical_module_descriptor,
    module_descriptor_digest,
)


class PreparedModuleRef(BaseModel):
    """One platform module staged and verified by the deployment manager."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    version: str = Field(min_length=1, max_length=64)
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


def canonical_module_set(
    modules: tuple[PreparedModuleRef, ...],
) -> tuple[PreparedModuleRef, ...]:
    """Order a module set by id so equal sets compare equal; one version per module."""
    ordered = tuple(sorted(modules, key=lambda module: module.id))
    ids = [module.id for module in ordered]
    if len(ids) != len(set(ids)):
        raise ValueError("a deployment runs exactly one version of each platform module")
    return ordered


ModuleSet = Annotated[tuple[PreparedModuleRef, ...], AfterValidator(canonical_module_set)]

_VERSION_FIELD = "platform module version"


class StagedModuleSource(Protocol):
    """Read-only view of the module artifacts the deployment manager staged."""

    def staged_versions(self) -> Mapping[str, tuple[str, ...]]:
        """Return each staged module id with the names of its version directories."""
        ...

    def staged_ref(self, module_id: str, version: str) -> PreparedModuleRef:
        """Read one staged artifact and return its pinned reference."""
        ...


def _version_order(left: str, right: str) -> int:
    return compare_semver(left, right, field_name=_VERSION_FIELD)


def staged_module_refs(source: StagedModuleSource) -> tuple[PreparedModuleRef, ...]:
    """Return the highest staged version of every module as a module set.

    Versions are ordered as semantic versions with the extension rules
    (``core.application.extensions.semver``): ``MAJOR[.MINOR[.PATCH]]``, a
    pre-release sorts below its release, build metadata is ignored. A version
    directory that is not a semantic version is refused rather than skipped, so a
    request never silently picks an older module.
    """
    refs: list[PreparedModuleRef] = []
    for module_id, versions in sorted(source.staged_versions().items()):
        if not versions:
            continue
        try:
            for version in versions:
                normalize_semver(version, _VERSION_FIELD)
            highest = max(versions, key=functools.cmp_to_key(_version_order))
        except ValueError as exc:
            raise LifecycleError.stable_failure(
                code="release_module_artifact_invalid",
                cause=(
                    f"Staged platform module '{module_id}' has a version directory that "
                    f"is not a semantic version: {exc}"
                ),
                remediation=(
                    "Keep maintenance enabled, let the deployment manager stage the pinned "
                    "platform modules again, then repeat release upgrade."
                ),
                step="modules_activate",
            ) from exc
        refs.append(source.staged_ref(module_id, highest))
    return canonical_module_set(tuple(refs))


class ModuleActivator(Protocol):
    """Port that switches the platform-module set every process role loads.

    The list is written only while no role of the installed release can start on
    its own. The release upgrade guarantees that by calling ``stop_roles`` (after
    maintenance has drained work and the backup is verified) before ``activate``,
    on both profiles:

    * Scalable: ``stop_roles`` stops ``control-plane``, ``fs-worker``,
      ``fs-extension-installer``, ``fs-mcp`` and ``fs-app``. A stopped Compose
      service is not restarted by its restart policy, so none of them can load the
      new list on the old Core.
    * Compact: ``stop_roles`` stops ``compact-runtime``, the one container that
      supervises every role.

    After the write, the roles start only through the same operation: the database
    step (``TargetReleaseMigration`` → ``upgrade_target_database``, which itself
    stops the runtime writers again before running the migrator in a one-off
    container of the role image), then ``restart_roles`` for a module-only change, or
    the installer handoff, which recreates the roles on the target release, for a
    release change. ``CheckoutWriterQuiesce`` additionally stops the checkout's
    writers of a development installation right before that handoff.

    When a module-only change fails after the list was written -- in the database
    step, which leaves every role stopped with the new set in the list, at the role
    restart, or at the health gate -- the roles end up in one of two explicit states:

    * running again on the previous set: the workflow calls ``stop_roles``,
      ``activate(previous_modules)`` and ``restart_roles`` and the journal records
      ``modules_rolled_back``. This happens automatically when the new set adds no
      module migration (``adds_module_migrations`` is false), and on the operator's
      explicit ``restore_previous_modules`` request otherwise;
    * stopped (or partly started on the new set), with ``modules_rollback_refused``
      and its reason in the journal and maintenance enabled, when the new set adds a
      module migration that may be applied. The error names both next commands:
      repeat the same release upgrade to go forward, or restore the pre-upgrade
      backup and request ``restore_previous_modules``.
    """

    @property
    def supports_activation(self) -> bool:
        """Whether this deployment can activate a non-empty module set at all."""
        ...

    @property
    def supports_role_verification(self) -> bool:
        """Whether ``verify_in_roles`` can run the loader in each role's own image."""
        ...

    @property
    def unavailable_reason(self) -> str:
        """Why ``supports_activation`` is false here, in one sentence; empty otherwise."""
        ...

    def installed_set(self) -> tuple[PreparedModuleRef, ...]:
        """Return the module set the running deployment currently uses."""
        ...

    def staged_modules(self) -> tuple[PreparedModuleRef, ...]:
        """Return every artifact the deployment manager staged, each version once."""
        ...

    def verify_in_roles(self, modules: tuple[PreparedModuleRef, ...]) -> None:
        """Verify ``modules`` with the loader of each role's own image.

        Must be checked in each role's own image before the list is written: the
        interpreter tag, machine and bundled distributions a role refuses at start
        are those of that image, not the operator CLI's. Raises a ``LifecycleError``
        when any role would refuse the set. Called only when
        ``supports_role_verification`` is true.
        """
        ...

    def stop_roles(self) -> None:
        """Stop every role that loads platform modules, so none starts on its own.

        Called before ``activate`` writes the list (see the class docstring). It
        must be idempotent: stopping a stopped role is harmless.
        """
        ...

    def activate(self, modules: tuple[PreparedModuleRef, ...]) -> None:
        """Make ``modules`` the set every process role loads on its next start.

        Must be idempotent: a resumed upgrade calls it again with the same pinned set,
        and a rollback calls it with the journal's ``previous_modules``. The release
        upgrade calls it after ``stop_roles`` and before the database step, so the
        migrator and every role started afterwards read this set.
        """
        ...

    def restart_roles(self) -> None:
        """Start again every role that ``stop_roles`` stopped.

        Called for a module-only change, after ``activate`` and the database step and
        before the health-gated reopen, and by a module rollback. It returns only
        after every such role has started on the activated set and reports ready; it
        must be idempotent, because a resume interrupted before the journal records it
        calls it again.
        """
        ...

    def adds_module_migrations(
        self,
        previous: tuple[PreparedModuleRef, ...],
        target: tuple[PreparedModuleRef, ...],
    ) -> bool:
        """Whether ``target`` ships a module migration that ``previous`` does not.

        Module migrations are forward-only. Once the database step has run for
        ``target``, such a migration may be applied, and the module runner refuses
        to run ``previous`` on that newer schema. When the answer cannot be
        determined (a staged artifact is gone), return ``True``.
        """
        ...


class NoModuleActivator:
    """Default activator for deployments without platform modules.

    The composition root replaces this default where platform modules can be
    activated (``FilesystemModuleActivator`` on Scalable, ``CompactModuleActivator``
    on Compact), and falls back to it, with ``reason``, where they cannot.
    """

    def __init__(self, reason: str = "") -> None:
        self.reason = reason

    @property
    def supports_activation(self) -> bool:
        return False

    @property
    def supports_role_verification(self) -> bool:
        return False

    @property
    def unavailable_reason(self) -> str:
        return self.reason

    def installed_set(self) -> tuple[PreparedModuleRef, ...]:
        return ()

    def staged_modules(self) -> tuple[PreparedModuleRef, ...]:
        return ()

    def verify_in_roles(self, modules: tuple[PreparedModuleRef, ...]) -> None:
        del modules
        raise role_verification_unsupported()

    def stop_roles(self) -> None:
        return None

    def activate(self, modules: tuple[PreparedModuleRef, ...]) -> None:
        del modules

    def restart_roles(self) -> None:
        return None

    def adds_module_migrations(
        self,
        previous: tuple[PreparedModuleRef, ...],
        target: tuple[PreparedModuleRef, ...],
    ) -> bool:
        del previous, target
        return False


class LazyModuleActivator:
    """Build the real activator on first use, never while the runtime is composed.

    Every lifecycle command composes the release service, and most of them have
    nothing to do with platform modules. Probing the module root -- a path, or a
    disposable container on Compact -- while composing made unrelated commands fail
    where that probe could not run. The factory is called at most once.
    """

    def __init__(self, factory: Callable[[], ModuleActivator]) -> None:
        self._factory = factory
        self._activator: ModuleActivator | None = None

    @property
    def built(self) -> bool:
        return self._activator is not None

    def _resolved(self) -> ModuleActivator:
        if self._activator is None:
            self._activator = self._factory()
        return self._activator

    @property
    def supports_activation(self) -> bool:
        return self._resolved().supports_activation

    @property
    def supports_role_verification(self) -> bool:
        return self._resolved().supports_role_verification

    @property
    def unavailable_reason(self) -> str:
        return str(getattr(self._resolved(), "unavailable_reason", "") or "")

    def installed_set(self) -> tuple[PreparedModuleRef, ...]:
        return self._resolved().installed_set()

    def staged_modules(self) -> tuple[PreparedModuleRef, ...]:
        return self._resolved().staged_modules()

    def verify_in_roles(self, modules: tuple[PreparedModuleRef, ...]) -> None:
        self._resolved().verify_in_roles(modules)

    def stop_roles(self) -> None:
        self._resolved().stop_roles()

    def activate(self, modules: tuple[PreparedModuleRef, ...]) -> None:
        self._resolved().activate(modules)

    def restart_roles(self) -> None:
        self._resolved().restart_roles()

    def adds_module_migrations(
        self,
        previous: tuple[PreparedModuleRef, ...],
        target: tuple[PreparedModuleRef, ...],
    ) -> bool:
        return self._resolved().adds_module_migrations(previous, target)


class _ListedStagedModules:
    """``StagedModuleSource`` over an already-read listing of staged artifacts."""

    def __init__(self, refs: tuple[PreparedModuleRef, ...]) -> None:
        self._refs = {(ref.id, ref.version): ref for ref in refs}

    def staged_versions(self) -> Mapping[str, tuple[str, ...]]:
        versions: dict[str, list[str]] = {}
        for module_id, version in self._refs:
            versions.setdefault(module_id, []).append(version)
        return {module_id: tuple(found) for module_id, found in versions.items()}

    def staged_ref(self, module_id: str, version: str) -> PreparedModuleRef:
        return self._refs[(module_id, version)]


def highest_staged_modules(
    staged: tuple[PreparedModuleRef, ...],
) -> tuple[PreparedModuleRef, ...]:
    """The highest staged version of every module, from a listing of all of them."""
    return staged_module_refs(_ListedStagedModules(staged))


def select_staged_modules(
    staged: tuple[PreparedModuleRef, ...], selection: tuple[str, ...]
) -> tuple[PreparedModuleRef, ...]:
    """Pin the staged artifacts an operator named as ``id@version``."""
    by_key = {(ref.id, ref.version): ref for ref in staged}
    chosen: list[PreparedModuleRef] = []
    for item in selection:
        module_id, separator, version = item.partition("@")
        if not separator or not module_id or not version:
            raise LifecycleError.stable_failure(
                code="release_module_selection_invalid",
                cause=f"'{item}' does not name a platform module as id@version.",
                remediation="Name each module as id@version, separated by commas.",
                step="release_policy",
            )
        ref = by_key.get((module_id, version))
        if ref is None:
            raise LifecycleError.stable_failure(
                code="release_module_artifact_missing",
                cause=f"Platform module {module_id}@{version} is not staged.",
                remediation=(
                    "Run flow-steward release modules-list to see the staged versions, "
                    "or let the deployment manager stage this one first."
                ),
                step="release_policy",
            )
        chosen.append(ref)
    try:
        return canonical_module_set(tuple(chosen))
    except ValueError as exc:
        raise LifecycleError.stable_failure(
            code="release_module_selection_invalid",
            cause=f"The selection names one module more than once: {exc}.",
            remediation="Name each module once.",
            step="release_policy",
        ) from exc


def role_verification_unsupported() -> LifecycleError:
    """The error ``verify_in_roles`` raises where no role-image check exists."""
    return LifecycleError.stable_failure(
        code="release_module_role_verification_unsupported",
        cause=(
            "This deployment cannot run the platform-module loader inside each role's "
            "image before the module list is written."
        ),
        remediation=(
            "Nothing was changed by this check. The release upgrade verifies the set "
            "with the operator CLI's loader instead; repeat release upgrade."
        ),
        step="modules_activate",
    )


__all__ = [
    "MODULE_DESCRIPTOR_KEYS",
    "LazyModuleActivator",
    "ModuleActivator",
    "ModuleSet",
    "NoModuleActivator",
    "PreparedModuleRef",
    "StagedModuleSource",
    "canonical_module_descriptor",
    "canonical_module_set",
    "highest_staged_modules",
    "module_descriptor_digest",
    "role_verification_unsupported",
    "select_staged_modules",
    "staged_module_refs",
]
