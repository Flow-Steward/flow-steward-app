"""Canonical AI provider/model catalog with source detection, visibility policy, and validation.

This module is the single source of truth for which provider/model combinations
are valid and available.  It replaces the static lists formerly hardcoded in
``core/application/workflows/serve_workflow_helpers.py``.

Key concepts:
- **ProviderFamily**: frozen record for each provider (Anthropic, OpenAI, …).
- **ModelEntry**: frozen record for each model within a family.
- **Source detection**: account-scoped DB-backed BYOK providers.
- **Visibility policy**: account-scoped enable/disable stored in ``system_settings``.
- **Effective options**: ``catalog ∩ available_sources ∩ platform_allowlist ∩ visibility``.
- **Validation**: ``validate_provider_model_pair()`` rejects mismatched or unavailable pairs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from core.application.settings import model_provider_catalog_repository
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

_TOKEN_HTTPS_PLATFORM_CLAUDE_COM_DOCS_EN_ABOUT_CLAUDE_MODELS_OVERVIEW = (
    "https://platform.claude.com/docs/en/about-claude/models/overview"
)
_TOKEN_HTTPS_DEVELOPERS_OPENAI_COM_API_DOCS_MODELS = "https://developers.openai.com/api/docs/models"

_logger = logging.getLogger(__name__)


class ModelCatalogUnavailableError(RuntimeError):
    """Raised when account-scoped catalog evidence cannot be loaded safely."""


def _require_catalog_connection(conn: Any) -> None:
    if conn is None:
        raise ModelCatalogUnavailableError("model catalog database is unavailable")


# ---------------------------------------------------------------------------
# 1. Canonical catalog
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelEntry:
    model_id: str
    label: str
    capabilities: tuple[str, ...] = ("chat",)
    lifecycle: str = "active"
    official_source_url: str = ""


@dataclass(frozen=True)
class ProviderFamily:
    family_id: str
    label: str
    transport: str  # anthropic | openai | openai_compatible | google | azure_openai
    env_keys: tuple[str, ...] = ()
    credential_modes: tuple[str, ...] = ("env",)
    models: tuple[ModelEntry, ...] = ()
    default_base_url: str = ""


PROVIDER_CATALOG: dict[str, ProviderFamily] = {}

# A catalog entry is only a release-audit candidate. Publishing direct models
# requires an explicit successful run of the capability suite for the exact
# upstream ID. Evidence for these routes is recorded in the provider audit.
BUILTIN_RELEASE_VERIFIED_MODELS: dict[str, frozenset[str]] = {
    "openai": frozenset({"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"}),
}


def is_builtin_model_release_verified(provider: str, model: str) -> bool:
    """Return whether an exact built-in route has release-suite evidence."""
    return str(model) in BUILTIN_RELEASE_VERIFIED_MODELS.get(str(provider), frozenset())


def _register_family(family: ProviderFamily) -> None:
    PROVIDER_CATALOG[family.family_id] = family


_register_family(
    ProviderFamily(
        family_id="anthropic",
        label="Anthropic",
        transport="anthropic",
        credential_modes=("ui_secret",),
        models=(
            ModelEntry(
                "claude-opus-5",
                "Claude Opus 5",
                ("chat", "tool_use", "structured_output"),
                official_source_url="https://platform.claude.com/docs/en/about-claude/models/whats-new-opus-5",
            ),
            ModelEntry(
                "claude-fable-5",
                "Claude Fable 5",
                ("chat", "tool_use", "structured_output"),
                official_source_url=_TOKEN_HTTPS_PLATFORM_CLAUDE_COM_DOCS_EN_ABOUT_CLAUDE_MODELS_OVERVIEW,
            ),
            ModelEntry(
                "claude-opus-4-8",
                "Claude Opus 4.8",
                ("chat", "tool_use", "structured_output"),
                official_source_url=_TOKEN_HTTPS_PLATFORM_CLAUDE_COM_DOCS_EN_ABOUT_CLAUDE_MODELS_OVERVIEW,
            ),
            ModelEntry(
                "claude-opus-4-7",
                "Claude Opus 4.7",
                ("chat", "tool_use", "structured_output"),
                official_source_url=_TOKEN_HTTPS_PLATFORM_CLAUDE_COM_DOCS_EN_ABOUT_CLAUDE_MODELS_OVERVIEW,
            ),
            ModelEntry(
                "claude-opus-4-6",
                "Claude Opus 4.6",
                ("chat", "tool_use", "structured_output"),
                official_source_url=_TOKEN_HTTPS_PLATFORM_CLAUDE_COM_DOCS_EN_ABOUT_CLAUDE_MODELS_OVERVIEW,
            ),
            ModelEntry(
                "claude-sonnet-5",
                "Claude Sonnet 5",
                ("chat", "tool_use", "structured_output"),
                official_source_url=_TOKEN_HTTPS_PLATFORM_CLAUDE_COM_DOCS_EN_ABOUT_CLAUDE_MODELS_OVERVIEW,
            ),
            ModelEntry(
                "claude-sonnet-4-6",
                "Claude Sonnet 4.6",
                ("chat", "tool_use", "structured_output"),
                official_source_url=_TOKEN_HTTPS_PLATFORM_CLAUDE_COM_DOCS_EN_ABOUT_CLAUDE_MODELS_OVERVIEW,
            ),
            ModelEntry(
                "claude-haiku-4-5-20251001",
                "Claude Haiku 4.5",
                ("chat", "tool_use", "structured_output"),
                official_source_url=_TOKEN_HTTPS_PLATFORM_CLAUDE_COM_DOCS_EN_ABOUT_CLAUDE_MODELS_OVERVIEW,
            ),
        ),
    )
)

_register_family(
    ProviderFamily(
        family_id="openai",
        label="OpenAI",
        transport="openai",
        credential_modes=("ui_secret",),
        models=(
            ModelEntry(
                "gpt-5.6-sol",
                "GPT-5.6 Sol",
                ("chat", "tool_use", "structured_output"),
                official_source_url=_TOKEN_HTTPS_DEVELOPERS_OPENAI_COM_API_DOCS_MODELS,
            ),
            ModelEntry(
                "gpt-5.6-terra",
                "GPT-5.6 Terra",
                ("chat", "tool_use", "structured_output"),
                official_source_url=_TOKEN_HTTPS_DEVELOPERS_OPENAI_COM_API_DOCS_MODELS,
            ),
            ModelEntry(
                "gpt-5.6-luna",
                "GPT-5.6 Luna",
                ("chat", "tool_use", "structured_output"),
                official_source_url=_TOKEN_HTTPS_DEVELOPERS_OPENAI_COM_API_DOCS_MODELS,
            ),
        ),
    )
)

_register_family(
    ProviderFamily(
        family_id="google",
        label="Google",
        transport="google",
        credential_modes=("ui_secret",),
        models=(
            ModelEntry(
                "gemini-3.6-flash",
                "Gemini 3.6 Flash",
                ("chat", "tool_use", "structured_output"),
                official_source_url="https://ai.google.dev/gemini-api/docs/models/gemini-3.6-flash",
            ),
            ModelEntry(
                "gemini-3.5-flash-lite",
                "Gemini 3.5 Flash-Lite",
                ("chat", "tool_use", "structured_output"),
                official_source_url="https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite",
            ),
        ),
    )
)

# ---------------------------------------------------------------------------
# 2. Provider source detection
# ---------------------------------------------------------------------------


@dataclass
class ProviderSource:
    family_id: str
    source_type: str  # ui_secret | named_provider
    source_ref: str  # provider DB id


def detect_provider_sources(
    account_id: str = "acc_default",
    conn: Any = None,
) -> dict[str, list[ProviderSource]]:
    """Return ``{family_id: [sources]}`` for families that have at least one source."""
    result: dict[str, list[ProviderSource]] = {}

    # A custom provider has a unique stable route key so two account-scoped
    # OpenAI-compatible endpoints cannot collide in selectors or persistence.
    for group in _custom_provider_groups(account_id, conn) if conn is not None else []:
        provider_key = str(group.get("provider_key") or "").strip()
        provider_id = provider_key.removeprefix(CUSTOM_PROVIDER_KEY_PREFIX)
        if provider_key and provider_id:
            result[provider_key] = [ProviderSource(provider_key, "named_provider", provider_id)]

    return result


# ---------------------------------------------------------------------------
# 3. Visibility policy
# ---------------------------------------------------------------------------

VISIBILITY_POLICY_KEY = "model_catalog_visibility_v1"


def _load_visibility_policy(
    conn: Any,
    account_id: str = "acc_default",
) -> dict[str, Any]:
    """Load the account visibility policy or fail closed."""
    _require_catalog_connection(conn)
    try:
        from core.infrastructure.db.system_settings_store import get_system_setting

        row = get_system_setting(
            conn=conn,
            _table_exists=True,
            setting_key=VISIBILITY_POLICY_KEY,
            account_id=account_id,
        )
        if isinstance(row, dict):
            val = row.get("setting_value_jsonb")
            if isinstance(val, dict):
                return val
    except Exception as exc:
        raise ModelCatalogUnavailableError(
            "model catalog visibility policy is unavailable"
        ) from exc
    return {}


def save_visibility_policy(
    conn: Any,
    policy: dict[str, Any],
    *,
    account_id: str = "acc_default",
    actor: str = "system",
) -> None:
    """Save visibility policy to system_settings."""
    from core.infrastructure.db.system_settings_store import save_system_setting

    save_system_setting(
        conn=conn,
        account_id=account_id,
        setting_key=VISIBILITY_POLICY_KEY,
        value=policy,
        updated_by=actor,
    )


def _is_provider_visible(policy: dict, family_id: str) -> bool:
    providers_section = policy.get("providers") or {}
    entry = providers_section.get(family_id)
    if not isinstance(entry, dict):
        return True  # no entry = visible by default
    return bool(entry.get("enabled", True))


def _is_model_visible(policy: dict, family_id: str, model_id: str) -> bool:
    providers_section = policy.get("providers") or {}
    entry = providers_section.get(family_id)
    if not isinstance(entry, dict):
        return True
    if not entry.get("enabled", True):
        return False
    models_section = entry.get("models") or {}
    model_entry = models_section.get(model_id)
    if model_entry is None:
        return True  # no entry = visible
    return bool(model_entry)


# ---------------------------------------------------------------------------
# 4. Effective options resolver
# ---------------------------------------------------------------------------


def _platform_allowlist() -> set[str] | None:
    """Parse FS_PROVIDER_ALLOWLIST env var. None = all allowed."""
    raw = _runtime_env.get("FS_PROVIDER_ALLOWLIST", "").strip()
    if not raw:
        return None
    return {s.strip() for s in raw.split(",") if s.strip()}


@dataclass
class EffectiveModel:
    model_id: str
    label: str


@dataclass
class EffectiveProvider:
    family_id: str
    label: str
    models: list[EffectiveModel]


def get_effective_options(
    account_id: str = "acc_default",
    conn: Any = None,
) -> list[EffectiveProvider]:
    """Compute effective = catalog ∩ sources ∩ platform_allowlist ∩ visibility."""
    _require_catalog_connection(conn)

    result: list[EffectiveProvider] = []
    sources = detect_provider_sources(account_id=account_id, conn=conn)
    allowlist = _platform_allowlist()
    policy = _load_visibility_policy(conn, account_id=account_id)
    for fid, family in PROVIDER_CATALOG.items():
        # Must have at least one source
        if fid not in sources:
            continue
        # Platform allowlist
        if allowlist is not None and fid not in allowlist:
            continue
        # Account visibility
        if not _is_provider_visible(policy, fid):
            continue

        models: list[EffectiveModel] = []
        for m in family.models:
            if (
                m.lifecycle == "active"
                and is_builtin_model_release_verified(fid, m.model_id)
                and _is_model_visible(policy, fid, m.model_id)
            ):
                models.append(EffectiveModel(m.model_id, m.label))

        if models:
            result.append(EffectiveProvider(fid, family.label, models))

    return result


@dataclass
class ProviderOption:
    """Display metadata for one provider entry in the first model selector."""

    provider_id: str
    label: str
    source_kind: str  # "built_in" | "extension_llm_provider"
    supplier_key: str = ""
    supplier_label: str = ""
    upstream_owner_key: str | None = None
    upstream_owner_label: str | None = None


# Stable prefix for extension-contributed provider keys. The runtime never
# parses display labels — it keys off this prefix + the saved provider_key.
EXTENSION_PROVIDER_KEY_PREFIX = "llmext_"
CUSTOM_PROVIDER_KEY_PREFIX = "custom:"


def is_extension_provider_key(provider: str) -> bool:
    return str(provider or "").strip().startswith(EXTENSION_PROVIDER_KEY_PREFIX)


def is_custom_provider_key(provider: str) -> bool:
    """Return whether ``provider`` names one account-scoped custom route."""
    return str(provider or "").strip().startswith(CUSTOM_PROVIDER_KEY_PREFIX)


def _custom_provider_key(provider_id: Any) -> str:
    return f"{CUSTOM_PROVIDER_KEY_PREFIX}{str(provider_id or '').strip()}"


def _custom_provider_groups(account_id: str, conn: Any) -> list[dict[str, Any]]:
    """Project only verified ``default_model`` rows from ``model_providers``.

    ``model_providers`` remains the credential/configuration store.  This
    adapter deliberately creates no model registry: each account-scoped
    provider contributes exactly one selected and capability-verified route.
    """
    _require_catalog_connection(conn)
    try:
        providers = model_provider_catalog_repository.list_routable_model_providers(
            conn, account_id=account_id
        )
    except Exception as exc:  # pragma: no cover - defensive store boundary
        raise ModelCatalogUnavailableError("custom provider catalog is unavailable") from exc

    groups: list[dict[str, Any]] = []
    # The verifier emits the canonical capability name ``tools``.  Built-in
    # catalog entries retain their supplier-facing ``tool_use`` label, but a
    # custom route is only eligible after the same live verifier succeeds.
    required = {"chat", "tools", "structured_output"}
    for provider in providers:
        provider_id = str(provider.get("id") or "").strip()
        model_id = str(provider.get("default_model") or "").strip()
        lifecycle = str(provider.get("lifecycle") or "active").strip().lower()
        status = str(provider.get("verification_status") or "").strip().lower()
        capabilities = provider.get("verified_capabilities_jsonb") or []
        if isinstance(capabilities, dict):
            capabilities = list(capabilities)
        capability_set = {str(value) for value in capabilities if str(value).strip()}
        if (
            not provider_id
            or not model_id
            or lifecycle != "active"
            or status != "passed"
            or not required.issubset(capability_set)
        ):
            continue
        groups.append(
            {
                "provider_key": _custom_provider_key(provider_id),
                "display_label": str(provider.get("name") or provider_id),
                "models": [
                    {
                        "model_id": model_id,
                        "label": model_id,
                        "capabilities": sorted(capability_set),
                    }
                ],
            }
        )
    return groups


def _extension_provider_groups(account_id: str, conn: Any) -> list[dict[str, Any]]:
    """Enabled extension provider groups with their globally-routable models.

    Only sources whose extension install is active/healthy contribute, and only
    enabled models with structured-output support are surfaced (brief: v1 global
    routing requires structured output). Missing or failed account-scoped
    storage raises a typed catalog error instead of exposing built-ins alone.
    """
    _require_catalog_connection(conn)
    try:
        from core.application.settings import llm_provider_extension_repository as store
    except Exception as exc:
        raise ModelCatalogUnavailableError("extension provider catalog is unavailable") from exc
    try:
        sources = store.list_enabled_sources(conn, account_id=account_id)
    except Exception as exc:  # pragma: no cover - defensive
        raise ModelCatalogUnavailableError("extension provider catalog is unavailable") from exc

    source_ids = [str(source.get("id") or "").strip() for source in sources]
    source_ids = [source_id for source_id in source_ids if source_id]
    try:
        all_models = store.list_enabled_structured_models_for_sources(
            conn,
            account_id=account_id,
            provider_source_ids=source_ids,
        )
    except Exception as exc:  # pragma: no cover - defensive
        raise ModelCatalogUnavailableError("extension model catalog is unavailable") from exc
    models_by_source: dict[str, list[dict[str, Any]]] = {}
    for model in all_models:
        source_id = str(model.get("provider_source_id") or "").strip()
        if source_id:
            models_by_source.setdefault(source_id, []).append(model)

    groups: list[dict[str, Any]] = []
    for source in sources:
        source_id = str(source.get("id") or "").strip()
        if not source_id:
            continue
        models = models_by_source.get(source_id, [])
        normalized_models: list[dict[str, Any]] = []
        for model in models:
            declared = model.get("declared_capabilities_jsonb")
            if isinstance(declared, dict):
                declared = list(declared.keys())
            elif not isinstance(declared, list):
                declared = []
            verified = model.get("verified_capabilities_jsonb")
            if isinstance(verified, dict):
                verified = list(verified.keys())
            elif not isinstance(verified, list):
                verified = []
            declared_caps = sorted({str(value) for value in declared if str(value).strip()})
            verified_caps = sorted({str(value) for value in verified if str(value).strip()})
            effective_caps = sorted(
                set(verified_caps)
                | ({"prompt_caching"} if "prompt_caching" in declared_caps else set())
            )
            model_id = str(model.get("model_id") or "").strip()
            if not model_id:
                continue
            normalized_models.append(
                {
                    "model_id": model_id,
                    "label": str(model.get("display_label") or model_id),
                    "capabilities": effective_caps,
                    "declared_capabilities": declared_caps,
                    "verified_capabilities": verified_caps,
                }
            )
        if not normalized_models:
            continue
        groups.append(
            {
                "provider_key": str(source.get("provider_key") or "").strip(),
                "display_label": str(source.get("display_label") or source.get("provider_key")),
                "supplier_key": str(source.get("supplier_key") or ""),
                "supplier_label": str(source.get("supplier_label") or ""),
                "upstream_owner_key": source.get("upstream_owner_key"),
                "upstream_owner_label": source.get("upstream_owner_label"),
                "models": normalized_models,
            }
        )
    return groups


def get_effective_options_structured(
    account_id: str = "acc_default",
    conn: Any = None,
) -> dict[str, Any]:
    """Return structured data for cascading selects.

    Every selector consumes this one provider/model/options shape. Enabled
    extension and custom routes are folded into the same maps with a stable
    provider key and capability metadata.
    """
    _require_catalog_connection(conn)
    effective = get_effective_options(account_id=account_id, conn=conn)
    provider_models: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[str, str]] = set()
    all_models: list[dict[str, Any]] = []
    provider_options: list[dict[str, Any]] = []
    for ep in effective:
        provider_options.append(
            {"provider_id": ep.family_id, "label": ep.label, "source_kind": "built_in"}
        )
        models_list: list[dict[str, Any]] = []
        for m in ep.models:
            models_list.append({"model_id": m.model_id, "label": m.label})
            key = ("", m.model_id)
            if key not in seen:
                seen.add(key)
                all_models.append({"model_id": m.model_id, "label": m.label})
        provider_models[ep.family_id] = models_list

    for group in _extension_provider_groups(account_id, conn):
        provider_id = group["provider_key"]
        if not provider_id:
            continue
        provider_options.append(
            {
                "provider_id": provider_id,
                "label": group["display_label"],
                "source_kind": "extension_llm_provider",
                "supplier_key": group.get("supplier_key", ""),
                "supplier_label": group.get("supplier_label", ""),
                "upstream_owner_key": group.get("upstream_owner_key"),
                "upstream_owner_label": group.get("upstream_owner_label"),
            }
        )
        models_list = []
        for m in group["models"]:
            models_list.append(
                {
                    "model_id": m["model_id"],
                    "label": m["label"],
                    "capabilities": m.get("capabilities", []),
                }
            )
            key = (provider_id, m["model_id"])
            if key not in seen:
                seen.add(key)
                all_models.append(
                    {"model_id": m["model_id"], "label": m["label"], "provider_id": provider_id}
                )
        provider_models[provider_id] = models_list

    for group in _custom_provider_groups(account_id, conn):
        provider_id = group["provider_key"]
        provider_options.append(
            {
                "provider_id": provider_id,
                "label": group["display_label"],
                "source_kind": "custom_model_provider",
            }
        )
        models_list = []
        for model in group["models"]:
            models_list.append(
                {
                    "model_id": model["model_id"],
                    "label": model["label"],
                    "capabilities": model["capabilities"],
                }
            )
            key = (provider_id, model["model_id"])
            if key not in seen:
                seen.add(key)
                all_models.append(
                    {
                        "model_id": model["model_id"],
                        "label": model["label"],
                        "provider_id": provider_id,
                    }
                )
        provider_models[provider_id] = models_list

    return {
        "provider_options": provider_options,
        "provider_models": provider_models,
        "all_models": all_models,
        "provider_labels": _known_provider_labels(provider_options, account_id, conn),
    }


def _known_provider_labels(
    provider_options: list[dict[str, Any]], account_id: str, conn: Any
) -> dict[str, str]:
    """Display labels for every provider a saved route may name.

    ``provider_options`` lists only providers with selectable models right now.
    A project or agent can still name a provider whose models all await
    verification, or a built-in without credentials; it is shown by its label,
    not its stable key, while the page explains that the route is unusable.
    """
    labels = {
        str(option["provider_id"]): str(option.get("label") or option["provider_id"])
        for option in provider_options
        if option.get("provider_id")
    }
    for family_id, family in PROVIDER_CATALOG.items():
        labels.setdefault(family_id, family.label)
    try:
        from core.application.settings import llm_provider_extension_repository as store

        sources = store.list_sources(conn, account_id=account_id)
    except Exception:  # labels are cosmetic; the routable options above stand alone
        sources = []
    for source in sources:
        provider_key = str(source.get("provider_key") or "").strip()
        if provider_key:
            labels.setdefault(provider_key, str(source.get("display_label") or provider_key))
    return labels


def get_effective_options_for_selection_context(
    account_id: str = "acc_default",
    conn: Any = None,
) -> tuple[list[tuple[str, str]], list[str]]:
    """Return compact (providers, models) tuples for model-selection contexts."""
    effective = get_effective_options(account_id=account_id, conn=conn)
    providers = [(ep.family_id, ep.label) for ep in effective]
    seen: set[str] = set()
    models: list[str] = []
    for ep in effective:
        for m in ep.models:
            if m.model_id not in seen:
                seen.add(m.model_id)
                models.append(m.model_id)
    for group in _extension_provider_groups(account_id, conn):
        providers.append((group["provider_key"], group["display_label"]))
        for m in group["models"]:
            if m["model_id"] not in seen:
                seen.add(m["model_id"])
                models.append(m["model_id"])
    for group in _custom_provider_groups(account_id, conn):
        providers.append((group["provider_key"], group["display_label"]))
        for m in group["models"]:
            if m["model_id"] not in seen:
                seen.add(m["model_id"])
                models.append(m["model_id"])
    return providers, models


def get_full_catalog_for_settings(
    account_id: str = "acc_default",
    conn: Any = None,
) -> dict[str, Any]:
    """Return full catalog state for the settings management page."""
    sources = detect_provider_sources(account_id=account_id, conn=conn)
    policy = _load_visibility_policy(conn, account_id=account_id)
    allowlist = _platform_allowlist()

    families: list[dict[str, Any]] = []
    for fid, family in PROVIDER_CATALOG.items():
        family_sources = sources.get(fid, [])
        platform_allowed = allowlist is None or fid in allowlist
        visible = _is_provider_visible(policy, fid)

        models_list: list[dict[str, Any]] = []
        for m in family.models:
            models_list.append(
                {
                    "model_id": m.model_id,
                    "label": m.label,
                    "capabilities": list(m.capabilities),
                    "lifecycle": m.lifecycle,
                    "official_source_url": m.official_source_url,
                    "enabled": _is_model_visible(policy, fid, m.model_id),
                }
            )

        families.append(
            {
                "family_id": fid,
                "label": family.label,
                "transport": family.transport,
                "available": bool(family_sources),
                "platform_allowed": platform_allowed,
                "enabled": visible,
                "sources": [
                    {"source_type": s.source_type, "source_ref": s.source_ref}
                    for s in family_sources
                ],
                "models": models_list,
            }
        )

    return {"families": families}


# ---------------------------------------------------------------------------
# 5. Validation
# ---------------------------------------------------------------------------


def validate_provider_model_pair(
    provider: str,
    model: str,
    account_id: str = "acc_default",
    conn: Any = None,
    *,
    allow_unverified_builtin: bool = False,
) -> tuple[bool, str]:
    """Validate a provider/model pair.

    Rules for Agent Configuration selections:
    - Standard API providers require both provider and model to be non-empty
    - CLI executors require a canonical blank model
    - Model must belong to the specified provider family
    - Provider must be available (have sources)
    - Both must pass visibility checks

    Returns ``(is_valid, error_message)``.
    """
    provider = str(provider or "").strip()
    model = str(model or "").strip()

    # Both empty = "inherit from parent" — valid at agent level
    if not provider and not model:
        return True, ""

    # One empty, one set = invalid state (P1 fix)
    if not provider and model:
        return (
            False,
            f"Model '{model}' specified without a provider. Select a provider or clear both fields.",
        )

    # Extension-contributed provider groups are validated against the
    # account-scoped LLM provider store, not the built-in catalog.
    if is_extension_provider_key(provider):
        _require_catalog_connection(conn)
        return _validate_extension_provider_model_pair(provider, model, account_id, conn)
    if is_custom_provider_key(provider):
        _require_catalog_connection(conn)
        return _validate_custom_provider_model_pair(provider, model, account_id, conn)

    family = PROVIDER_CATALOG.get(provider)
    if family is None:
        return False, f"Unknown provider: '{provider}'"

    if provider and not model:
        return (
            False,
            f"Provider '{provider}' specified without a model. Select a model or clear both fields.",
        )

    # Check catalog membership
    family_model_ids = {m.model_id for m in family.models}
    if model not in family_model_ids:
        return False, (
            f"Model '{model}' is not available for provider '{provider}'. "
            f"Valid models: {', '.join(sorted(family_model_ids))}"
        )

    if not allow_unverified_builtin and not is_builtin_model_release_verified(provider, model):
        return False, f"Model '{model}' has not passed the required release capability suite."

    # Check availability (sources)
    _require_catalog_connection(conn)
    # Built-in provider availability is determined by its direct credential
    # sources.  The database is still mandatory for the visibility policy
    # below, but custom-provider rows are unrelated to this route and may use
    # a different account identity domain.
    sources = detect_provider_sources(account_id=account_id, conn=None)
    if provider not in sources:
        return False, (
            f"Provider '{provider}' ({family.label}) has no configured credentials. "
            f"Set {', '.join(family.env_keys) or 'provider credentials'} or add via Settings."
        )

    # Check visibility
    policy = _load_visibility_policy(conn, account_id=account_id)
    if not _is_provider_visible(policy, provider):
        return False, f"Provider '{provider}' is disabled in account settings."
    if not _is_model_visible(policy, provider, model):
        return False, f"Model '{model}' is disabled for provider '{provider}'."

    return True, ""


def validate_chat_provider_model_pair(
    provider: str,
    model: str,
    account_id: str = "acc_default",
    conn: Any = None,
    *,
    allow_unverified_builtin: bool = False,
) -> tuple[bool, str]:
    """Validate only routes that can construct a chat model."""
    return validate_provider_model_pair(
        str(provider or "").strip(),
        model,
        account_id=account_id,
        conn=conn,
        allow_unverified_builtin=allow_unverified_builtin,
    )


def _validate_extension_provider_model_pair(
    provider: str,
    model: str,
    account_id: str,
    conn: Any,
) -> tuple[bool, str]:
    """Validate an extension provider/model pair against the LLM provider store.

    Valid iff the source is enabled and the model is enabled with structured
    output support. Routing uses the stable ``provider_key`` only; the display
    label is irrelevant here.
    """
    if conn is None:
        return (
            False,
            f"Extension provider '{provider}' cannot be validated without a database connection.",
        )
    try:
        from core.application.settings import llm_provider_extension_repository as store

        # Use the same active/healthy-install gate as catalog options + runtime so a
        # disabled, uninstalled, unhealthy, or stale install cannot be saved.
        source = store.get_enabled_source_by_provider_key(
            conn, account_id=account_id, provider_key=provider
        )
    except Exception as exc:
        return False, f"Extension provider '{provider}' could not be resolved: {exc}"
    if not source:
        return (
            False,
            f"Extension provider '{provider}' is not available "
            "(disabled, uninstalled, or unhealthy install).",
        )
    if not model:
        return False, f"Provider '{provider}' specified without a model. Select a model."
    try:
        models = store.list_enabled_structured_models(
            conn, account_id=account_id, provider_source_id=str(source.get("id") or "")
        )
    except Exception as exc:
        return False, f"Extension provider '{provider}' models are unavailable: {exc}"
    if any(str(m.get("model_id") or "") == model for m in models):
        return True, ""
    return (
        False,
        f"Model '{model}' is not enabled with structured output for extension provider '{provider}'.",
    )


def _validate_custom_provider_model_pair(
    provider: str,
    model: str,
    account_id: str,
    conn: Any,
) -> tuple[bool, str]:
    """Validate one verified selected custom model from ``model_providers``."""
    if conn is None:
        return (
            False,
            f"Custom provider '{provider}' cannot be validated without a database connection.",
        )
    provider_id = str(provider).removeprefix(CUSTOM_PROVIDER_KEY_PREFIX)
    if not provider_id or not model:
        return False, f"Provider '{provider}' specified without a model. Select a model."
    for group in _custom_provider_groups(account_id, conn):
        if group["provider_key"] != provider:
            continue
        if any(entry["model_id"] == model for entry in group["models"]):
            return True, ""
        return (
            False,
            f"Model '{model}' is not the verified selected model for custom provider '{provider}'.",
        )
    return False, f"Custom provider '{provider}' is unavailable or has not passed verification."


def validate_provider_model_pair_catalog_only(
    provider: str,
    model: str,
) -> tuple[bool, str]:
    """Strict static validation for routes that have no account/store context."""
    provider = str(provider or "").strip()
    model = str(model or "").strip()

    if not provider and not model:
        return True, ""
    if not provider and model:
        return False, f"Model '{model}' specified without a provider."

    family = PROVIDER_CATALOG.get(provider)
    if family is None:
        return False, f"Unknown or unavailable provider: '{provider}'."

    if provider and not model:
        return False, f"Provider '{provider}' specified without a model."

    family_model_ids = {m.model_id for m in family.models}
    if model not in family_model_ids:
        return False, f"Model '{model}' does not belong to provider '{provider}'."

    if not is_builtin_model_release_verified(provider, model):
        return False, f"Model '{model}' has not passed the required release capability suite."

    return True, ""


# ---------------------------------------------------------------------------
# 6. High-level resolver for project API integration
# ---------------------------------------------------------------------------


def resolve_model_selection_context(
    default_model_gpt_4_1_nano: str,
    *,
    conn: Any = None,
    account_id: str = "acc_default",
) -> dict[str, Any]:
    """Build the full model-selection context used by project UI/API surfaces.

    Returns dict with keys: default_agent_models, available_providers,
    available_models, providers_with_models.
    """
    from core.application.workflows.serve_workflow_helpers import default_agent_models

    providers, models = get_effective_options_for_selection_context(
        account_id=account_id,
        conn=conn,
    )
    structured = get_effective_options_structured(
        account_id=account_id,
        conn=conn,
    )

    return {
        "default_agent_models": default_agent_models(default_model_gpt_4_1_nano),
        "available_providers": providers,
        "available_models": models,
        "providers_with_models": structured,
    }
