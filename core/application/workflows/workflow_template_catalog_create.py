"""Create a workflow from one catalog entry, through the existing import path.

This adapter adds exactly three things to the workflow portability path it
delegates to:

1. it downloads one selected template's YAML — never the whole catalog — and
   refuses to parse it until the declared byte size and SHA-256 both match;
2. it maps a template's logical ``extension_id`` connector references onto this
   account's local install ids, expressed as ordinary portability resolutions
   so the existing resolution and substitution logic does the rewrite; and
3. it hands the verified YAML to the existing preflight and apply.

It is copy-only. Nothing records that a workflow came from the catalog, nothing
links the two, and a catalog refresh never looks at, changes, or flags a local
workflow.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.application.extensions.extension_catalog_install import (
    active_install_by_install_id,
    active_installs_for_extension,
)
from core.application.extensions.semver import compare_semver
from core.application.management.scope import ScopeContext, normalize_scope
from core.application.workflows.workflow_template_catalog import (
    IMPORTABLE_BUNDLE_SCHEMA,
    WorkflowTemplateCatalogError,
    WorkflowTemplateCatalogItem,
    fetch_workflow_template_artifact,
    load_workflow_template_catalog,
)
from core.infrastructure.config.version import get_app_version
from core.infrastructure.static_catalog.http_fetch import (
    CatalogArtifactMismatchError,
    CatalogFetchError,
)

_EXTENSION_DEPENDENCY_TYPE = "extension"
_INSTALL_ACTION = "install_extension_required"

#: One entry per logical extension reference the template carries, so the UI can
#: say *why* a create is blocked instead of only that it is.
RESOLUTION_OUTCOME_RESOLVED = "resolved"
RESOLUTION_OUTCOME_MISSING = "missing"
RESOLUTION_OUTCOME_AMBIGUOUS = "ambiguous"
RESOLUTION_OUTCOME_INCOMPATIBLE = "incompatible"


def _template_compatible(item: WorkflowTemplateCatalogItem, *, platform_version: str) -> bool:
    """Whether this deployment falls inside the entry's declared range.

    Bounds are inclusive at both ends, matching how the extension installer
    reads ``platform_min`` / ``platform_max``, so the two cannot disagree.
    """
    try:
        if item.compatible_min_version and (
            compare_semver(platform_version, item.compatible_min_version, field_name="platform") < 0
        ):
            return False
        if item.compatible_max_version and (
            compare_semver(platform_version, item.compatible_max_version, field_name="platform") > 0
        ):
            return False
    except ValueError:
        return False
    return True


def _assert_one_template(preflight: dict[str, Any], *, item_id: str) -> None:
    """Refuse a catalog bundle that is more than one template.

    A template is one workflow plus the child workflows it calls, carried in the same
    file so it installs complete. Every workflow in the file other than the one it
    was exported for must be called by one in the file; an unrelated workflow riding
    along would be created without the card saying so.
    """
    strays = [
        str(row.get("workflow_id") or "")
        for row in preflight.get("workflows") or []
        if isinstance(row, dict)
        and str(row.get("portability_kind") or "workflow_definition") == "workflow_definition"
        and str(row.get("role") or "") not in {"root", "child"}
    ]
    if strays:
        raise WorkflowTemplateCatalogError(
            (
                f"Workflow template '{item_id}' carries workflows nothing in it calls: "
                f"{', '.join(sorted(strays))}."
            ),
            code="workflow_template_multiple_workflows",
            status_code=422,
        )


class InstallAwareWorkflowDb:
    """The workflow store, plus the one extension-install lookup preflight needs.

    ``WorkflowPortabilityService._install_exists`` asks its workflow store for
    ``get_extension_by_install_id``. The registry store the admin routes wire in
    has no such method, so that lookup answers "no such install" for *every*
    install id — including one this adapter just read out of the account's own
    install store, which would leave a correctly resolved dependency blocked.

    This wrapper answers that single question from the account-scoped install
    store and delegates everything else to the real store untouched. It is
    applied only on the catalog create path: manual YAML import builds the
    portability service without it and keeps its existing behaviour.
    """

    def __init__(
        self,
        db: Any,
        *,
        get_install_store: Callable[[], Any],
        account_id: str,
    ) -> None:
        self._db = db
        self._get_install_store = get_install_store
        self._account_id = str(account_id or "").strip() or "acc_default"

    def get_extension_by_install_id(
        self, install_id: str, *, account_id: str | None = None
    ) -> dict[str, Any] | None:
        """Resolve an active install this account can see. Never widens scope."""
        record = active_install_by_install_id(
            self._get_install_store(),
            install_id=install_id,
            account_id=str(account_id or "").strip() or self._account_id,
        )
        if record is None:
            return None
        dump = getattr(record, "model_dump", None)
        if callable(dump):
            dumped = dump()
            return dict(dumped) if isinstance(dumped, dict) else None
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)


class WorkflowTemplateCatalogCreateService:
    """Preflight and create one catalog workflow via workflow portability."""

    def __init__(
        self,
        *,
        portability: Any,
        get_install_store: Callable[[], Any],
        load_index: Callable[[], Any] = load_workflow_template_catalog,
        fetch_artifact: Callable[..., bytes] = fetch_workflow_template_artifact,
        # `compatible_flow_steward` is a product range, so it is checked against the
        # product version, not the extension host contract version.
        platform_version: Callable[[], str] = get_app_version,
    ) -> None:
        self._portability = portability
        self._get_install_store = get_install_store
        self._load_index = load_index
        self._fetch_artifact = fetch_artifact
        self._platform_version = platform_version

    # -- catalog reads ----------------------------------------------------

    def require_item(self, item_id: str) -> WorkflowTemplateCatalogItem:
        index = self._load_index()
        item = index.item(item_id)
        if item is None:
            raise WorkflowTemplateCatalogError(
                f"Workflow template '{str(item_id or '').strip()}' is not in the catalog.",
                code="workflow_template_catalog_item_not_found",
                status_code=404,
            )
        platform_version = self._platform_version()
        if not _template_compatible(item, platform_version=platform_version):
            # The range is published so it can gate installation. Validating it
            # into the API response and then ignoring it would let a template
            # built for a later release be created on this one.
            raise WorkflowTemplateCatalogError(
                (
                    f"Workflow template '{item.item_id}' {item.version} is not compatible "
                    f"with Flow Steward {platform_version}."
                ),
                code="workflow_template_incompatible",
                status_code=409,
            )
        if not item.importable:
            # Downloading the file only to have the bundle parser refuse it would
            # surface the failure inside the dialog; the index already says.
            raise WorkflowTemplateCatalogError(
                (
                    f"Workflow template '{item.item_id}' {item.version} uses the bundle format "
                    f"'{item.bundle_schema or 'unknown'}'; this installation imports "
                    f"{IMPORTABLE_BUNDLE_SCHEMA} templates."
                ),
                code="workflow_template_bundle_schema_unsupported",
                status_code=409,
            )
        return item

    def load_item_yaml(self, item: WorkflowTemplateCatalogItem) -> str:
        """Download and verify one template bundle. Nothing else is fetched."""
        try:
            raw = self._fetch_artifact(
                item.workflow_yaml_url,
                expected_bytes=item.workflow_yaml_bytes,
                expected_sha256=item.workflow_yaml_sha256,
                purpose=f"workflow template '{item.item_id}' bundle",
            )
        except CatalogArtifactMismatchError as exc:
            raise WorkflowTemplateCatalogError(
                str(exc),
                code="workflow_template_artifact_mismatch",
                status_code=422,
            ) from exc
        except CatalogFetchError as exc:
            raise WorkflowTemplateCatalogError(
                str(exc),
                code="workflow_template_artifact_unavailable",
            ) from exc
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkflowTemplateCatalogError(
                f"Workflow template '{item.item_id}' bundle is not UTF-8 text.",
                code="workflow_template_artifact_mismatch",
                status_code=422,
            ) from exc

    # -- import path ------------------------------------------------------

    def preflight(
        self,
        item_id: str,
        *,
        target_workflow_id: str = "",
        resolutions: list[dict[str, Any]] | None = None,
        scope: ScopeContext | None = None,
    ) -> dict[str, Any]:
        item = self.require_item(item_id)
        bundle_yaml = self.load_item_yaml(item)
        merged, outcomes, discovery = self._resolve_extensions(
            bundle_yaml,
            item_id=item.item_id,
            target_workflow_id=target_workflow_id,
            resolutions=resolutions or [],
            scope=scope,
        )
        # The discovery pass is a full preflight already. Re-running it is only
        # worth it when this adapter actually added a resolution.
        preflight = (
            discovery
            if merged == list(resolutions or [])
            else self._portability.preflight_import(
                bundle_yaml,
                target_workflow_id=target_workflow_id,
                resolutions=merged,
            )
        )
        return self._annotate(preflight, item=item, outcomes=outcomes)

    def create(
        self,
        item_id: str,
        *,
        target_workflow_id: str = "",
        resolutions: list[dict[str, Any]] | None = None,
        author: str = "admin",
        owner_account_id: str = "",
        owner_project_id: str = "",
        scope: ScopeContext | None = None,
    ) -> dict[str, Any]:
        item = self.require_item(item_id)
        bundle_yaml = self.load_item_yaml(item)
        merged, outcomes, _discovery = self._resolve_extensions(
            bundle_yaml,
            item_id=item.item_id,
            target_workflow_id=target_workflow_id,
            resolutions=resolutions or [],
            scope=scope,
        )
        # apply_import re-runs preflight itself and refuses on a blocking
        # dependency or an existing workflow id, so the catalog path cannot
        # overwrite a workflow or skip a check the manual import enforces.
        result = self._portability.apply_import(
            bundle_yaml,
            target_workflow_id=target_workflow_id,
            resolutions=merged,
            author=author,
            owner_account_id=owner_account_id,
            owner_project_id=owner_project_id,
        )
        result["catalog_item"] = {"item_id": item.item_id, "version": item.version}
        result["catalog_extension_resolutions"] = outcomes
        return result

    # -- logical extension id resolution ----------------------------------

    def _resolve_extensions(
        self,
        bundle_yaml: str,
        *,
        item_id: str,
        target_workflow_id: str,
        resolutions: list[dict[str, Any]],
        scope: ScopeContext | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        """Map the template's extensions to this account's installs (shared with import)."""
        return resolve_portable_extensions(
            self._portability,
            bundle_yaml,
            target_workflow_id=target_workflow_id,
            resolutions=resolutions,
            account_id=normalize_scope(scope).account_id or "acc_default",
            get_install_store=self._get_install_store,
            check_discovery=lambda discovery: _assert_one_template(discovery, item_id=item_id),
        )

    def _annotate(
        self,
        preflight: dict[str, Any],
        *,
        item: WorkflowTemplateCatalogItem,
        outcomes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        annotated = dict(preflight)
        annotated["catalog_item"] = {
            "item_id": item.item_id,
            "version": item.version,
            "name": item.name,
            "setup_requirements": [entry.as_dict() for entry in item.setup_requirements],
        }
        annotated["catalog_extension_resolutions"] = outcomes
        return annotated


def resolve_portable_extensions(
    portability: Any,
    bundle_yaml: str,
    *,
    target_workflow_id: str,
    resolutions: list[dict[str, Any]],
    account_id: str,
    get_install_store: Callable[[], Any],
    check_discovery: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Turn unresolved extension refs into install-id resolutions.

    Used by the catalog and by the manual YAML import alike, so a bundle that names
    an extension this account has installed maps the same way on both paths. A first
    preflight pass is what discovers the refs: it already knows which of them the
    account cannot satisfy, so nothing here re-parses the YAML. That pass is
    returned too, so a bundle with nothing to map is not preflighted twice.
    Resolutions the operator already chose are kept; only unresolved refs are added.
    """
    discovery = portability.preflight_import(
        bundle_yaml,
        target_workflow_id=target_workflow_id,
        resolutions=list(resolutions),
    )
    if check_discovery is not None:
        check_discovery(discovery)
    pending = [
        row
        for row in discovery.get("dependencies") or []
        if isinstance(row, dict)
        and str(row.get("dependency_type") or "") == _EXTENSION_DEPENDENCY_TYPE
        and str(row.get("status") or "") == "requires_extension_install"
    ]
    if not pending:
        return list(resolutions), [], discovery

    store = get_install_store()
    merged = list(resolutions)
    outcomes: list[dict[str, Any]] = []
    for row in pending:
        ref = str(row.get("ref") or "").strip()
        if not ref:
            continue
        raw_metadata = row.get("metadata")
        metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
        # A hand-written template names the extension by logical id in `ref`. An
        # exported workflow keeps the exporting machine's install id there and the
        # logical id beside it (AC17-R60), and only the logical id is portable.
        logical_id = str(metadata.get("extension_id") or "").strip() or ref
        outcome = _resolve_logical_extension(
            logical_id,
            metadata=metadata,
            store=store,
            account_id=account_id,
        )
        outcomes.append(outcome)
        install_id = str(outcome.get("install_id") or "")
        if install_id:
            merged.append(
                {
                    "dependency_key": f"{_EXTENSION_DEPENDENCY_TYPE}:{ref}",
                    "dependency_type": _EXTENSION_DEPENDENCY_TYPE,
                    "ref": ref,
                    "action": _INSTALL_ACTION,
                    # The existing rewrite path reads target_ref; the classifier
                    # accepts either. One local install id, two spellings, so
                    # neither side has to learn a catalog-specific field.
                    "target_ref": install_id,
                    "target_install_id": install_id,
                    "extension_id": logical_id,
                }
            )
    return merged, outcomes, discovery


def _resolve_logical_extension(
    ref: str,
    *,
    metadata: dict[str, Any],
    store: Any,
    account_id: str,
) -> dict[str, Any]:
    candidates = active_installs_for_extension(store, extension_id=ref, account_id=account_id)
    compatible = [
        record
        for record in candidates
        if _install_version_matches(str(getattr(record, "version", "") or ""), metadata)
    ]
    if not candidates:
        return _outcome(ref, RESOLUTION_OUTCOME_MISSING, "No active install of this extension.")
    if not compatible:
        return _outcome(
            ref,
            RESOLUTION_OUTCOME_INCOMPATIBLE,
            "The installed version does not satisfy the template's declared range.",
        )
    if len(compatible) > 1:
        return _outcome(
            ref,
            RESOLUTION_OUTCOME_AMBIGUOUS,
            (
                f"{len(compatible)} active installs share this extension id; "
                "resolve the duplicate before importing."
            ),
        )
    install_id = str(getattr(compatible[0], "install_id", "") or "").strip()
    if not install_id:
        return _outcome(ref, RESOLUTION_OUTCOME_MISSING, "The matching install has no install id.")
    outcome = _outcome(ref, RESOLUTION_OUTCOME_RESOLVED, "")
    outcome["install_id"] = install_id
    outcome["installed_version"] = str(getattr(compatible[0], "version", "") or "")
    return outcome


def _outcome(ref: str, outcome: str, message: str) -> dict[str, Any]:
    return {"extension_id": ref, "outcome": outcome, "message": message}


def _install_version_matches(version: str, metadata: dict[str, Any]) -> bool:
    """Honour a template's optional min/max on the extension it depends on.

    A template that names no range accepts any active install, which keeps the
    common case free of version bookkeeping.
    """
    minimum = str(metadata.get("min_version") or "").strip()
    maximum = str(metadata.get("max_version") or "").strip()
    if not minimum and not maximum:
        return True
    if not version:
        return False
    try:
        if minimum and compare_semver(version, minimum, field_name="extension version") < 0:
            return False
        if maximum and compare_semver(version, maximum, field_name="extension version") > 0:
            return False
    except ValueError:
        return False
    return True


__all__ = [
    "RESOLUTION_OUTCOME_AMBIGUOUS",
    "RESOLUTION_OUTCOME_INCOMPATIBLE",
    "RESOLUTION_OUTCOME_MISSING",
    "RESOLUTION_OUTCOME_RESOLVED",
    "InstallAwareWorkflowDb",
    "WorkflowTemplateCatalogCreateService",
    "resolve_portable_extensions",
]
