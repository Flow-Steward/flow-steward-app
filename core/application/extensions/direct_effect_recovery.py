"""Restore and authorize recovery of an exact existing direct-action intent."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from core.application.auth.principal import principal_type_has_minimum_role
from core.application.extensions.direct_effect_recovery_models import (
    DirectEffectDecisionRequest,
    DirectEffectDecisionView,
    DirectEffectListView,
    DirectEffectResolutionRef,
    DirectEffectSummary,
)
from core.application.extensions.direct_effect_recovery_repository import (
    DirectEffectRecoveryRepository,
)
from core.application.extensions.external_effect_contracts import (
    build_external_effect_identity,
    stable_json_hash,
)
from core.application.extensions.external_effect_resolution import (
    ExternalEffectResolutionError,
    resolve_external_effect,
)
from core.application.extensions.lifecycle_ports import (
    require_project_access,
    require_role,
    service_scope,
)
from core.application.extensions.operation_targets import _load_direct_action_connection_row
from core.application.management.errors import (
    ServiceConflictError,
    ServiceForbiddenError,
    ServiceNotFoundError,
)
from core.application.management.scope import ScopeContext
from core.infrastructure.db.db_pool import transaction_connection

_SCAN_PAGE = 100
_SCAN_MAX = 5000


def _rows(repository: DirectEffectRecoveryRepository, **scope: Any) -> Iterator[dict[str, Any]]:
    after = None
    for _ in range(_SCAN_MAX // _SCAN_PAGE):
        page = repository.candidates(**scope, after=after, limit=_SCAN_PAGE)
        yield from page
        if len(page) < _SCAN_PAGE:
            return
        after = str(page[-1]["effect_id"])
    raise ServiceConflictError("external_effect_recovery_ambiguity_limit")


def _pending_ref(
    repository: DirectEffectRecoveryRepository, effect: dict[str, Any]
) -> dict[str, Any] | None:
    receipt = repository.resolution(effect)
    if not receipt:
        return None
    ref = dict(receipt.get("result_jsonb", {}).get("resolution_ref") or {})
    version = ref.get("fenced_version")
    if type(version) is not int or ref.get("approval_id") != "":
        return None
    if str(ref.get("effect_id")) != str(effect["effect_id"]):
        return None
    if effect["status"] == "timeout_unknown":
        return (
            ref
            if effect["status_reason"] == "operator_confirmed_no_effect"
            and effect["claim_version"] == version
            else None
        )
    return ref if effect["claim_version"] == version + 1 else None


def _connection_visible(store: Any, effect: dict[str, Any]) -> bool:
    return bool(
        _load_direct_action_connection_row(
            store,
            connection_id=str(effect.get("connection_id") or ""),
            account_id=str(effect["account_id"]),
            project_id=str(effect.get("project_id") or ""),
            extension_id=effect["extension_id"],
            extension_install_id=effect["extension_install_id"],
        )
    )


class DirectEffectRecoveryService:
    def __init__(
        self,
        *,
        get_operation_store: Callable[[], Any],
        get_install_store: Callable[[], Any],
        get_project_by_id: Callable[[str], Any] | None,
    ) -> None:
        self._get_operation_store = get_operation_store
        self._get_install_store = get_install_store
        self._get_project_by_id = get_project_by_id

    def _authorize(
        self, install_id: str, project_id: str, scope: ScopeContext | None
    ) -> tuple[Any, ScopeContext, dict[str, Any]]:
        scope = service_scope(scope)
        require_role(scope, "viewer")
        store = self._get_operation_store()
        project = (
            self._get_project_by_id(project_id)
            if self._get_project_by_id
            else store.get_project(project_id)
        )
        if not isinstance(project, dict):
            raise ServiceNotFoundError("Project not found")
        require_project_access(scope, project)
        if scope.project_id and str(scope.project_id) != project_id:
            raise ServiceForbiddenError("Access denied")
        install = self._get_install_store().get_extension_install_for_operator(
            install_id, account_id=scope.account_id
        )
        if (
            not isinstance(install, dict)
            or str(install.get("install_account_id") or install.get("account_id") or "")
            != scope.account_id
            or install.get("enabled") is not True
        ):
            raise ServiceNotFoundError("Extension install not found")
        return store, scope, install

    def list_external_effects(
        self, install_id: str, *, project_id: str, scope: ScopeContext | None = None
    ) -> DirectEffectListView:
        store, scope, install = self._authorize(install_id, project_id, scope)
        repository = DirectEffectRecoveryRepository(store)
        visible = []
        for effect in _rows(
            repository, account_id=scope.account_id, project_id=project_id, install_id=install_id
        ):
            if effect["extension_id"] != install["extension_id"] or not _connection_visible(
                store, effect
            ):
                continue
            ref = _pending_ref(repository, effect)
            if effect["status"] != "timeout_unknown" and not ref:
                continue
            visible.append((effect, ref))
        visible.sort(
            key=lambda pair: (str(pair[0]["created_at"]), str(pair[0]["effect_id"])), reverse=True
        )
        entries = []
        for effect, ref in visible[:20]:
            active = effect["status"] in {"claimed", "running"}
            entries.append(
                DirectEffectSummary(
                    effect_id=str(effect["effect_id"]),
                    observed_version=effect["claim_version"],
                    operation_id=effect["operation_id"],
                    status=effect["status"],
                    decision_allowed=not ref
                    and effect["status_reason"] != "operator_confirmed_no_effect"
                    and principal_type_has_minimum_role(scope.principal_type, "operator"),
                    retry_supported=bool(ref)
                    and not active
                    and principal_type_has_minimum_role(scope.principal_type, "operator"),
                    resolution_ref=(
                        DirectEffectResolutionRef.model_validate(ref) if ref is not None else None
                    ),
                    retry_unavailable_reason="retry_in_progress" if active else None,
                )
            )
        return DirectEffectListView(effects=entries, truncated=len(visible) > 20)

    def resolve_external_effect(
        self,
        install_id: str,
        effect_id: str,
        body: DirectEffectDecisionRequest,
        *,
        project_id: str,
        scope: ScopeContext | None = None,
    ) -> DirectEffectDecisionView:
        store, scope, install = self._authorize(install_id, project_id, scope)
        require_role(scope, "operator")
        actor_id = str(scope.actor_id or "").strip()
        if not actor_id:
            raise ServiceForbiddenError("A trusted actor is required to resolve an external effect")
        repository = DirectEffectRecoveryRepository(store)
        effect = repository.effect(
            account_id=scope.account_id,
            project_id=project_id,
            install_id=install_id,
            effect_id=effect_id,
        )
        if (
            not effect
            or effect["extension_id"] != install["extension_id"]
            or not _connection_visible(store, effect)
        ):
            raise ServiceNotFoundError("External effect not found")
        try:
            result = resolve_external_effect(
                store,
                account_id=scope.account_id,
                project_id=project_id,
                effect_id=effect_id,
                observed_version=body.observed_version,
                action=body.action,
                comment=body.comment,
                actor_id=actor_id,
                actor_type=str(scope.principal_type),
                expected_source_ref=effect["source_ref_jsonb"],
            )
        except ExternalEffectResolutionError as exc:
            raise ServiceConflictError(exc.code) from exc
        return DirectEffectDecisionView.model_validate(result)


@contextmanager
def prepare_direct_effect_recovery(
    context: Any, *, source_ref: dict[str, Any], request_payload: dict[str, Any]
) -> Iterator[dict[str, Any]]:
    """Serialize ordinary claims; let the accepted retry claim commit its own fence.

    The persisted source supplies the original actor/key hash for comparison;
    current complete validated inputs still have to reproduce its fingerprint.
    """
    ref_model = getattr(context.body, "resolution_ref", None)
    ref = ref_model.model_dump() if ref_model else None
    current = {
        "source_ref": source_ref,
        "idempotency_key_override": source_ref["direct_action_intent_hash"] or None,
    }
    if not callable(getattr(context.bl, "cursor", None)):
        yield current
        return
    scope = {
        "account_id": str(context.audit.get("account_id") or ""),
        "project_id": str(context.audit.get("project_id") or "") or None,
        "install_id": str(
            context.extension.get("extension_install_id")
            or context.extension.get("install_id")
            or ""
        ),
        "operation_id": context.action_id,
        "connection_id": str(context.target.get("connection_id") or "") or None,
    }
    with transaction_connection(context.bl) as conn:
        repository = DirectEffectRecoveryRepository(conn)
        # This transient lock serializes equal requests across fresh browser keys.
        # It is never persisted or exposed as a new request identity.
        repository.lock_intent(stable_json_hash({**scope, "request": request_payload}))
        candidates = (
            [
                repository.effect(
                    account_id=scope["account_id"],
                    project_id=scope["project_id"],
                    install_id=scope["install_id"],
                    effect_id=ref["effect_id"],
                )
            ]
            if ref
            else _rows(repository, **scope, match_connection=True)
        )
        for effect in candidates:
            if not effect:
                raise ServiceConflictError("external_effect_resolution_not_found")
            identity = build_external_effect_identity(
                account_id=scope["account_id"],
                project_id=scope["project_id"],
                extension_id=context.extension_id,
                extension_install_id=scope["install_id"],
                connection_id=scope["connection_id"],
                operation_id=context.action_id,
                source_ref=effect["source_ref_jsonb"],
                request_payload=request_payload,
                test_mode=bool(
                    context.body.context.get("test_mode") or context.target.get("test_mode")
                ),
            )
            matches = identity["request_fingerprint"] == effect["request_fingerprint"]
            pending = _pending_ref(repository, effect)
            if ref:
                if not matches or pending != ref or effect["status"] != "timeout_unknown":
                    raise ServiceConflictError("external_effect_resolution_intent_mismatch")
                current = {
                    "source_ref": effect["source_ref_jsonb"],
                    "idempotency_key_override": effect["idempotency_key"],
                    "resolution_ref": ref,
                }
                break
            if matches and (effect["status"] == "timeout_unknown" or pending):
                raise ServiceConflictError(f"external_effect_{effect['status']}")
        if not ref:
            yield current
            return
    # A01 rejects any enclosing transaction: it alone commits the one-use
    # authorization before returning spawn permission. It rechecks full intent,
    # current fence and receipt atomically, so this read cannot grant permission.
    yield current
