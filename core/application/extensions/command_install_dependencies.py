from __future__ import annotations

from typing import Any

from core.application.management.errors import ServiceValidationError

from .semver import compare_semver
from .service_support import _normalize_text


class ExtensionInstallDependencyPolicy:
    """Validate extension-to-extension install dependencies."""

    def validate(
        self,
        *,
        release: Any,
        account_id: str,
        runtime_store: Any,
        install_store: Any,
    ) -> list[dict[str, Any]]:
        descriptor = dict(getattr(release, "descriptor", {}) or {})
        extension_id = _normalize_text(getattr(release, "extension_id", ""))
        self.reject_required_cycles(
            root_extension_id=extension_id,
            root_descriptor=descriptor,
            runtime_store=runtime_store,
        )
        dependencies = self.extension_dependency_specs(descriptor)
        if not dependencies:
            return []
        active_installs = self._active_installs(install_store, account_id=account_id)
        warnings: list[dict[str, Any]] = []
        for dependency in dependencies:
            warning = self._validate_dependency(dependency, active_installs=active_installs)
            if warning is not None:
                warnings.append(warning)
        return warnings

    @staticmethod
    def _active_installs(install_store: Any, *, account_id: str) -> dict[str, Any]:
        rows = (
            install_store.list_for_account(
                account_id,
                include_global=True,
                include_inactive_owned=False,
            )
            if hasattr(install_store, "list_for_account")
            else []
        )
        return {_normalize_text(getattr(row, "extension_id", "")): row for row in rows}

    def _validate_dependency(
        self,
        dependency: dict[str, Any],
        *,
        active_installs: dict[str, Any],
    ) -> dict[str, Any] | None:
        dep_id = _normalize_text(dependency.get("extension_id"))
        if not dep_id:
            return None
        required = bool(dependency.get("required", True))
        installed = active_installs.get(dep_id)
        if installed is None:
            payload = {
                "extension_id": dep_id,
                "reason": dependency.get("reason") or "",
                "status": "missing",
            }
            if required:
                raise ServiceValidationError(
                    f"Required extension dependency '{dep_id}' is not installed or active",
                    payload={"dependency": payload},
                )
            return payload
        installed_version = _normalize_text(getattr(installed, "version", ""))
        version_error = self.dependency_version_error(
            dependency=dependency,
            installed_version=installed_version,
        )
        if not version_error:
            return None
        payload = {
            "extension_id": dep_id,
            "reason": dependency.get("reason") or "",
            "status": "version_mismatch",
            "installed_version": installed_version,
            "detail": version_error,
        }
        if required:
            raise ServiceValidationError(version_error, payload={"dependency": payload})
        return payload

    @staticmethod
    def extension_dependency_specs(descriptor: dict[str, Any]) -> list[dict[str, Any]]:
        dependencies = descriptor.get("dependencies")
        if not isinstance(dependencies, dict):
            return []
        raw_extensions = dependencies.get("extensions")
        if not isinstance(raw_extensions, list):
            return []
        return [dict(item) for item in raw_extensions if isinstance(item, dict)]

    @staticmethod
    def dependency_version_error(
        *,
        dependency: dict[str, Any],
        installed_version: str,
    ) -> str:
        dep_id = _normalize_text(dependency.get("extension_id"))
        min_version = _normalize_text(dependency.get("min_version"))
        max_version = _normalize_text(dependency.get("max_version_exclusive"))
        if min_version and (
            compare_semver(installed_version, min_version, field_name=f"{dep_id} version") < 0
        ):
            return (
                f"Required extension dependency '{dep_id}' version '{installed_version}' "
                f"is below minimum '{min_version}'"
            )
        if max_version and (
            compare_semver(installed_version, max_version, field_name=f"{dep_id} version") >= 0
        ):
            return (
                f"Required extension dependency '{dep_id}' version '{installed_version}' "
                f"must be below '{max_version}'"
            )
        return ""

    def reject_required_cycles(
        self,
        *,
        root_extension_id: str,
        root_descriptor: dict[str, Any],
        runtime_store: Any,
    ) -> None:
        descriptors = self._release_descriptors(
            root_extension_id=root_extension_id,
            root_descriptor=root_descriptor,
            runtime_store=runtime_store,
        )
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(extension_id: str, path: list[str]) -> None:
            if extension_id in visiting:
                cycle = [*path, extension_id]
                if root_extension_id in cycle:
                    raise ServiceValidationError(
                        "Required extension dependency cycle detected",
                        payload={"cycle": cycle},
                    )
                return
            if extension_id in visited:
                return
            visiting.add(extension_id)
            descriptor = descriptors.get(extension_id) or {}
            for dependency in self.extension_dependency_specs(descriptor):
                if not bool(dependency.get("required", True)):
                    continue
                dep_id = _normalize_text(dependency.get("extension_id"))
                if dep_id and dep_id in descriptors:
                    visit(dep_id, [*path, extension_id])
            visiting.remove(extension_id)
            visited.add(extension_id)

        visit(root_extension_id, [])

    @staticmethod
    def _release_descriptors(
        *,
        root_extension_id: str,
        root_descriptor: dict[str, Any],
        runtime_store: Any,
    ) -> dict[str, dict[str, Any]]:
        descriptors = {root_extension_id: dict(root_descriptor)}
        if not hasattr(runtime_store, "list_releases"):
            return descriptors
        for release in runtime_store.list_releases() or []:
            extension_id = _normalize_text(getattr(release, "extension_id", ""))
            if extension_id and extension_id not in descriptors:
                descriptors[extension_id] = dict(getattr(release, "descriptor", {}) or {})
        return descriptors
