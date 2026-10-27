"""
LangChain ChatModel factory.
Returns configured ChatModel instances for agent nodes.

Supported providers: openai, anthropic, google (Gemini),
openai_compatible (custom base_url), azure_openai.

Resolution order (first match wins):
  1. Per-agent project override:  project_config["agents_config"]["agents"]["config"][agent_name]
  2. Project default:             project_config["agents_config"]["default_provider/model"]
  3. Registry agent config:       registry_agents.config (set via admin UI)

A selected account-scoped ``model_provider_id`` takes precedence and contributes
its single verified ``default_model``. Missing or incompatible routes fail closed.
"""

from dataclasses import dataclass
from functools import cache
from typing import Any, Self

from core.application.settings.model_catalog import (
    PROVIDER_CATALOG,
    is_custom_provider_key,
    is_extension_provider_key,
)
from core.application.settings.model_provider_endpoint_policy import (
    OFFICIAL_OPENAI_BASE_URL,
    official_provider_base_url,
    validate_provider_endpoint_fields,
)
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

# Test-friendly lazy class handles (patched in unit tests).
ChatAnthropic: Any = None
ChatOpenAI: Any = None
ChatGoogleGenerativeAI: Any = None
AzureChatOpenAI: Any = None


@cache
def _pinned_google_chat_model_class(base_class: type[Any]) -> type[Any]:
    """Override the wrapper validator that rebuilds an injected GenAI client."""

    from pydantic import model_validator

    class _PinnedGoogleChatModel(base_class):
        @model_validator(mode="after")
        def validate_environment(self) -> Self:
            # Preserve the wrapper's non-client validation while retaining the
            # explicitly injected client. The upstream validator unconditionally
            # replaces ``client`` and omits ``vertexai=False`` in its SDK call.
            if self.temperature is not None and not 0 <= self.temperature <= 2.0:
                raise ValueError("temperature must be in the range [0.0, 2.0]")
            if self.top_p is not None and not 0 <= self.top_p <= 1:
                raise ValueError("top_p must be in the range [0.0, 1.0]")
            if self.top_k is not None and self.top_k <= 0:
                raise ValueError("top_k must be positive")
            if self.client is None:
                raise ValueError("an explicitly pinned Google GenAI client is required")
            self.default_metadata = tuple((self.additional_headers or {}).items())
            return self

    return _PinnedGoogleChatModel


def _build_pinned_google_chat_model(chat_model_class: Any, **kwargs: Any) -> Any:
    """Build a Google chat facade around one explicitly non-Vertex SDK client."""

    # Keep the lazy constructor seam used by unit tests and embedders.
    if not isinstance(chat_model_class, type):
        return chat_model_class(**kwargs)

    from google import genai
    from google.genai.types import HttpOptions
    from langchain_google_genai._common import get_user_agent

    api_key = str(kwargs.get("google_api_key") or "").strip()
    if not api_key:
        raise ValueError("Google API key is not configured")

    official_base_url = official_provider_base_url("google")
    additional_headers = kwargs.get("additional_headers") or {}
    _, user_agent = get_user_agent("ChatGoogleGenerativeAI")
    headers = {"User-Agent": user_agent, **additional_headers}
    client_args = kwargs.get("client_args")
    pinned_client = genai.Client(
        api_key=api_key,
        vertexai=False,
        http_options=HttpOptions(
            base_url=official_base_url,
            headers=headers,
            client_args=client_args,
            async_client_args=client_args,
        ),
    )
    pinned_class = _pinned_google_chat_model_class(chat_model_class)
    try:
        pinned_kwargs = {
            **kwargs,
            "google_api_key": api_key,
            "base_url": official_base_url,
            "vertexai": False,
            "client": pinned_client,
        }
        return pinned_class(**pinned_kwargs)
    except Exception:
        pinned_client.close()
        raise


class ModelProviderResolutionError(RuntimeError):
    """Raised when an explicit DB-backed model provider cannot be used safely."""


class ModelUnavailableError(RuntimeError):
    """Raised when routing cannot resolve one valid provider/model pair."""


@dataclass(frozen=True)
class ResolvedModelRoute:
    """The complete provider/model route selected by configuration precedence."""

    provider: str
    model: str
    route_config: dict[str, Any]
    source: str


def _assert_safe_provider_base_url(base_url: Any) -> str:
    """Re-validate a stored model-provider base URL at LLM-client build time.

    Store-time validation (command_services_model_provider) checks the URL before
    persistence, but the hostname can become unsafe afterwards (DNS rebinding to
    loopback/metadata/private peers). We re-run the SSRF safety check here, just
    before the LangChain/OpenAI client dials the endpoint with a decrypted API
    key. This re-resolves DNS and rejects blocked answers at use time.

    This is the validation-only step. For DB-backed custom base URLs the caller
    uses :func:`_safe_pinned_base_url`, which additionally attaches connection-
    pinned httpx clients so the third-party client connects to the exact IP
    validated here (closing the use-time DNS-rebinding TOCTOU entirely).
    """
    url = str(base_url or "").strip()
    if not url:
        return url
    from core.application.extensions.url_safety import (
        assert_safe_remote_https_url,
    )

    try:
        return assert_safe_remote_https_url(url, purpose="model_provider_base_url")
    except ValueError as exc:
        raise ModelProviderResolutionError(
            f"model provider base_url failed use-time safety validation: {exc}"
        ) from exc


def _safe_pinned_base_url(base_url: Any, kwargs: dict) -> str:
    """Validate a DB-backed custom base URL and attach DNS-rebinding-pinned httpx
    clients so the OpenAI/Azure client connects to the exact IP validated here.

    This closes the use-time TOCTOU residual: the connection is pinned to a
    validated IP (preserving Host/SNI) rather than letting the third-party client
    re-resolve the hostname at connect time. Fails closed on unsafe URLs.
    """
    safe = _assert_safe_provider_base_url(base_url)
    if not safe:
        return safe
    from core.application.extensions.url_safety import (
        build_pinned_async_httpx_client,
        build_pinned_httpx_client,
    )

    try:
        kwargs["http_client"] = build_pinned_httpx_client(safe, purpose="model_provider_base_url")
        kwargs["http_async_client"] = build_pinned_async_httpx_client(
            safe, purpose="model_provider_base_url"
        )
    except ValueError as exc:
        raise ModelProviderResolutionError(
            f"model provider base_url failed use-time connection pinning: {exc}"
        ) from exc
    return safe


def _model_provider_account_id(config: dict, agents_cfg: dict) -> str:
    """Return the runtime account scope for credential-bearing provider lookups."""

    project_policy = config.get("project_policy") if isinstance(config, dict) else {}
    config_scope = config.get("scope") if isinstance(config, dict) else {}
    for candidate in (
        config.get("account_id") if isinstance(config, dict) else "",
        agents_cfg.get("account_id") if isinstance(agents_cfg, dict) else "",
        project_policy.get("account_id") if isinstance(project_policy, dict) else "",
        config_scope.get("account_id") if isinstance(config_scope, dict) else "",
    ):
        normalized = str(candidate or "").strip()
        if normalized:
            return normalized
    return ""


def _resolve_named_provider(
    provider_id: Any, *, account_id: str, require_verified: bool = True
) -> dict:
    """Look up a named model provider within the runtime account scope."""

    normalized_provider_id = str(provider_id or "").strip()
    normalized_account_id = str(account_id or "").strip()
    if not normalized_provider_id:
        raise ModelProviderResolutionError("model_provider_id is required")
    if not normalized_account_id:
        raise ModelProviderResolutionError(
            f"model_provider_id '{normalized_provider_id}' requires account_id"
        )
    try:
        from core.infrastructure.db.platform_stores import PlatformConnectionStore

        get_shared_instance = getattr(PlatformConnectionStore, "get_shared_instance", None)
        if not callable(get_shared_instance):
            raise ModelProviderResolutionError("model provider store is unavailable")
        bl = get_shared_instance()
        if bl is None or not hasattr(bl, "conn"):
            raise ModelProviderResolutionError("model provider store is unavailable")
        from core.infrastructure.db import psycopg_runtime as psycopg_runtime

        with bl.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            query = """
                SELECT provider.*,
                       secret.encrypted_value AS api_key_ciphertext
                  FROM model_providers AS provider
             LEFT JOIN secret_refs AS secret
                    ON secret.account_id = provider.account_id
                   AND secret.ref_key = provider.api_key_ref
                 WHERE provider.id::text = %s
                   AND provider.account_id = %s
                   AND provider.enabled = true
            """
            if require_verified:
                query += (
                    " AND provider.verification_status = 'passed'"
                    " AND provider.lifecycle = 'active'"
                    " AND provider.verified_at >= now() - interval '30 days'"
                    " AND provider.verified_at <= now()"
                )
            cur.execute(query, (normalized_provider_id, normalized_account_id))
            row = cur.fetchone()
        if not row:
            raise ModelProviderResolutionError(
                f"model_provider_id '{normalized_provider_id}' was not found for account"
            )
        result = dict(row)
        row_account_id = str(result.get("account_id") or "").strip()
        if row_account_id != normalized_account_id:
            raise ModelProviderResolutionError(
                f"model_provider_id '{normalized_provider_id}' account mismatch"
            )

        # Decrypt API key if ref exists
        ref_key = str(result.get("api_key_ref") or "").strip()
        if ref_key:
            from core.application.auth.secrets import decrypt_secret

            ciphertext = result.pop("api_key_ciphertext", None)
            if not ciphertext:
                raise ModelProviderResolutionError(
                    f"model_provider_id '{normalized_provider_id}' secret reference is missing"
                )
            result["_decrypted_api_key"] = decrypt_secret(ciphertext)
        else:
            result.pop("api_key_ciphertext", None)
        return result
    except ModelProviderResolutionError:
        raise
    except Exception as exc:
        raise ModelProviderResolutionError(
            f"model_provider_id '{normalized_provider_id}' could not be resolved"
        ) from exc


def _build_extension_chat_model(
    *,
    provider_key: str,
    model_id: str,
    account_id: str,
    temperature: float,
    request_timeout: float | None,
    max_retries: int | None,
) -> Any:
    """Resolve an extension LLM provider key into an ExtensionChatModel.

    Fail-closed (mirrors ``_resolve_named_provider``): a missing account scope,
    disabled/unhealthy install, disabled source, or model that is not enabled
    with structured output raises ``ModelProviderResolutionError`` — never a
    silent fallback to another provider.
    """
    normalized_account_id = str(account_id or "").strip()
    if not normalized_account_id:
        raise ModelProviderResolutionError(
            f"extension provider '{provider_key}' requires account_id"
        )
    try:
        from core.application.settings import llm_provider_extension_repository as store
        from core.infrastructure.db.platform_stores import PlatformConnectionStore

        get_shared_instance = getattr(PlatformConnectionStore, "get_shared_instance", None)
        if not callable(get_shared_instance):
            raise ModelProviderResolutionError("extension provider store is unavailable")
        bl = get_shared_instance()
        if bl is None or not hasattr(bl, "conn"):
            raise ModelProviderResolutionError("extension provider store is unavailable")

        # list_enabled_sources enforces install active/healthy + source enabled.
        sources = store.list_enabled_sources(bl.conn, account_id=normalized_account_id)
        source = next(
            (s for s in sources if str(s.get("provider_key") or "") == provider_key), None
        )
        if not source:
            raise ModelProviderResolutionError(
                f"extension provider '{provider_key}' is not available for account"
            )
        enabled_models = store.list_enabled_structured_models(
            bl.conn,
            account_id=normalized_account_id,
            provider_source_id=str(source.get("id") or ""),
        )
        selected_extension_model = next(
            (model for model in enabled_models if str(model.get("model_id") or "") == model_id),
            None,
        )
        if model_id and selected_extension_model is None:
            raise ModelProviderResolutionError(
                f"model '{model_id}' is not enabled for extension provider '{provider_key}'"
            )
    except ModelProviderResolutionError:
        raise
    except Exception as exc:
        raise ModelProviderResolutionError(
            f"extension provider '{provider_key}' could not be resolved"
        ) from exc

    from core.application.orchestration.extension_chat_model import ExtensionChatModel

    return ExtensionChatModel(
        account_id=normalized_account_id,
        provider_key=provider_key,
        model_id=model_id,
        chat_action_id=str(source.get("chat_action_id") or "llm.chat"),
        structured_chat_action_id=(source.get("structured_chat_action_id") or None),
        extension_install_id=str(source.get("extension_install_id") or ""),
        extension_id=str(source.get("extension_id") or ""),
        temperature=float(temperature),
        request_timeout=request_timeout,
        max_retries=max_retries,
        supports_prompt_caching=bool(
            selected_extension_model
            and "prompt_caching" in set(selected_extension_model.get("capabilities_jsonb") or [])
        ),
    )


def _normalize_timeout_seconds(timeout_seconds) -> float | None:
    """Return a positive timeout value or None when unset/invalid.

    Per-agent timeout_minutes is the primary source of truth for LLM request
    duration. When operators set FS_LLM_REQUEST_TIMEOUT_SECONDS, it also bounds
    callers that do not provide an explicit per-agent timeout.
    """
    try:
        timeout = float(timeout_seconds or 0)
    except (TypeError, ValueError):
        return None
    raw_cap = str(_runtime_env.get("FS_LLM_REQUEST_TIMEOUT_SECONDS", "") or "").strip()
    timeout_cap: float | None = None
    if raw_cap:
        try:
            parsed_cap = float(raw_cap)
        except (TypeError, ValueError):
            parsed_cap = 0.0
        if parsed_cap > 0:
            timeout_cap = parsed_cap
    if timeout <= 0:
        return timeout_cap
    if timeout_cap is None:
        return timeout
    return min(timeout, timeout_cap)


def _get_registry_agent_config(agent_name: str) -> dict:
    """Load an agent's config from the registry (set via admin UI)."""
    candidate = str(agent_name or "").strip()
    if not candidate:
        return {}
    try:
        from core.application.registry.registry_manager import RegistryManager

        reg = RegistryManager.instance()
        manifest = reg.agents.get_agent(candidate)
        if manifest:
            cfg = manifest.config or {}
            if isinstance(cfg, dict):
                return cfg
    except Exception:
        pass
    return {}


def _project_agent_config(agents_cfg: dict, agent_name: str | None) -> dict:
    """Return the project-level agent override for this agent."""

    if not agent_name:
        return {}
    config_by_agent = ((agents_cfg.get("agents") or {}).get("config", {})) or {}
    maybe = config_by_agent.get(str(agent_name or "").strip(), {})
    if isinstance(maybe, dict) and maybe:
        return maybe
    return {}


def _normalize_provider_model_pair(
    provider: str,
    model: str,
    *,
    account_scoped: bool = False,
    allow_unverified_builtin: bool = False,
) -> tuple[str, str]:
    """Require one explicit, compatible route; never substitute a fallback."""
    clean_provider = str(provider or "").strip()
    clean_model = str(model or "").strip()
    if not clean_provider or not clean_model:
        raise ModelUnavailableError("model_unavailable: provider and model are both required")
    if not account_scoped and clean_provider not in PROVIDER_CATALOG:
        raise ModelUnavailableError(
            f"model_unavailable: provider '{clean_provider}' is not routable"
        )

    if not account_scoped:
        family_models = {entry.model_id for entry in PROVIDER_CATALOG[clean_provider].models}
        if clean_model not in family_models:
            raise ModelUnavailableError(
                f"model_unavailable: model '{clean_model}' does not belong to provider '{clean_provider}'"
            )
        from core.application.settings.model_catalog import is_builtin_model_release_verified

        if not allow_unverified_builtin and not is_builtin_model_release_verified(
            clean_provider, clean_model
        ):
            raise ModelUnavailableError(
                f"model_unavailable: model '{clean_model}' has not passed the release capability suite"
            )
    return clean_provider, clean_model


def _configured_route(config: dict, *, source: str) -> tuple[str, str] | None:
    """Return a complete route from one configuration level or fail closed.

    Provider and model form one stable routing identity.  They must therefore
    be supplied together by the same configuration level; filling a blank model
    from a parent would silently create a cross-provider route.
    """
    provider = str(config.get("provider") or "").strip()
    model = str(config.get("model") or "").strip()
    if not provider and not model:
        return None
    if not provider or not model:
        raise ModelUnavailableError(
            f"model_unavailable: {source} must configure provider and model together"
        )
    return provider, model


#: Where a route came from, in precedence order.
_ROUTE_SOURCE_AGENT_OVERRIDE = "agent override"
_ROUTE_SOURCE_PROJECT_DEFAULTS = "project defaults"
_ROUTE_SOURCE_ASSISTANT = "assistant configuration"
#: Route sources a project sets: they win over the agent's own model, so a
#: project pointing at a removed provider breaks every agent in it.
_PROJECT_ROUTE_SOURCES = frozenset({_ROUTE_SOURCE_PROJECT_DEFAULTS, _ROUTE_SOURCE_AGENT_OVERRIDE})


def project_model_unavailable_message(
    config: dict, route: ResolvedModelRoute, *, agent_name: str | None = None
) -> str:
    """Say which project points at a model that no longer resolves, and what to do."""
    project = str(config.get("project_name") or config.get("project_id") or "").strip()
    named = f"Project '{project}'" if project else "This project"
    if route.source == _ROUTE_SOURCE_AGENT_OVERRIDE:
        what = f"sets the model {route.model} for the agent '{agent_name or 'this agent'}'"
    else:
        what = f"has the default model {route.model}"
    return (
        f"{named} {what}, which is no longer available: its LLM provider was removed, "
        "reinstalled or turned off, or the model is no longer verified. The project's "
        "setting takes precedence over the agent's own model. Choose another model in the "
        "project's settings, or clear it so each agent uses its own model."
    )


def _resolve_model_route(
    config: dict,
    agent_name: str | None,
    *,
    registry_cfg: dict | None = None,
) -> ResolvedModelRoute:
    """Select one complete route using the same precedence as model construction."""

    agents_cfg = config.get("agents_config") or {}
    agent_cfg = _project_agent_config(agents_cfg, agent_name)
    if registry_cfg is None:
        registry_cfg = _get_registry_agent_config(agent_name) if agent_name else {}
    candidates = (
        (agent_cfg, _ROUTE_SOURCE_AGENT_OVERRIDE),
        (
            {
                "provider": agents_cfg.get("default_provider"),
                "model": agents_cfg.get("default_model"),
                "model_provider_id": agents_cfg.get("model_provider_id"),
            },
            _ROUTE_SOURCE_PROJECT_DEFAULTS,
        ),
        (registry_cfg, _ROUTE_SOURCE_ASSISTANT),
    )
    for route_config, source in candidates:
        route = _configured_route(route_config, source=source)
        if route is not None:
            return ResolvedModelRoute(*route, route_config=route_config, source=source)
    raise ModelUnavailableError("model_unavailable: no configured provider/model route")


def get_chat_model(
    config: dict,
    agent_name: str | None = None,
    timeout_seconds: float | None = None,
    *,
    allow_unverified_custom: bool = False,
    allow_unverified_builtin: bool = False,
    claimed_custom_provider: dict[str, Any] | None = None,
):
    """Build a LangChain ChatModel from project/agent config.

    `config` is the full project_config dict (as returned by
    get_project_run_config).  LLM settings live under the `agents_config`
    sub-key which is where the UI and DB store them.

    Resolution order:
      1. Per-agent project override  (agents_config.agents.config.<name>)
      2. Project default             (agents_config.default_provider / .default_model)
      3. Registry agent config       (registry_agents.config — admin UI)

    An account-scoped provider supplies credentials only after the selected
    route exactly matches its verified provider key and default_model.

    There is deliberately no environment, cross-provider, or first-catalog-model
    fallback. An incomplete or unavailable route raises ``ModelUnavailableError``.
    """
    from core.infrastructure.config.runtime_config import (
        CONFIG,  # import here to avoid circular import at module load
    )

    def _to_float(value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    # LLM settings are stored inside the `agents_config` sub-key of project_config
    agents_cfg = config.get("agents_config") or {}
    account_id = _model_provider_account_id(config, agents_cfg)

    agent_cfg = _project_agent_config(agents_cfg, agent_name)
    registry_cfg = _get_registry_agent_config(agent_name) if agent_name else {}
    route = _resolve_model_route(config, agent_name, registry_cfg=registry_cfg)

    provider, model = route.provider, route.model
    provider_identity = provider
    named_provider: dict | None = None
    named_provider_id = str(route.route_config.get("model_provider_id") or "").strip()
    if named_provider_id:
        expected_provider = f"custom:{named_provider_id}"
        if provider != expected_provider:
            raise ModelUnavailableError(
                f"model_unavailable: {route.source} provider does not match model_provider_id"
            )
    elif is_custom_provider_key(provider):
        named_provider_id = str(provider).removeprefix("custom:").strip()

    if named_provider_id:
        if claimed_custom_provider is not None:
            claimed_id = str(claimed_custom_provider.get("id") or "").strip()
            claimed_account = str(claimed_custom_provider.get("account_id") or "").strip()
            if claimed_id != named_provider_id or (
                claimed_account and claimed_account != account_id
            ):
                raise ModelUnavailableError(
                    "model_unavailable: claimed custom provider snapshot does not match route"
                )
            named_provider = dict(claimed_custom_provider)
        else:
            named_provider = _resolve_named_provider(
                named_provider_id,
                account_id=account_id,
                require_verified=not allow_unverified_custom,
            )
        selected_model = str(named_provider.get("default_model") or "").strip()
        if model != selected_model:
            raise ModelUnavailableError(
                "model_unavailable: configured custom model is no longer the verified selected model"
            )
        if not str(named_provider.get("_decrypted_api_key") or "").strip():
            raise ModelUnavailableError(
                "authentication_error: account-scoped custom provider credential is required"
            )
        try:
            endpoint = validate_provider_endpoint_fields(
                str(named_provider.get("provider_type") or ""),
                base_url=named_provider.get("base_url"),
                api_version=named_provider.get("api_version"),
                deployment_name=named_provider.get("deployment_name"),
            )
        except ValueError as exc:
            raise ModelProviderResolutionError(
                f"model provider endpoint contract is invalid: {exc}"
            ) from exc
        named_provider["base_url"] = endpoint.base_url
        named_provider["api_version"] = endpoint.api_version
        named_provider["deployment_name"] = endpoint.deployment_name
        provider_identity = f"custom:{named_provider_id}"
        provider, model = _normalize_provider_model_pair(
            str(named_provider.get("provider_type") or ""), selected_model, account_scoped=True
        )
    elif not is_extension_provider_key(provider):
        provider, model = _normalize_provider_model_pair(
            provider,
            model,
            allow_unverified_builtin=allow_unverified_builtin,
        )
        from core.application.settings.model_catalog import (
            ModelCatalogUnavailableError,
            validate_chat_provider_model_pair,
        )

        catalog_conn = None
        try:
            from core.infrastructure.db.platform_stores import PlatformConnectionStore

            shared_logger = PlatformConnectionStore.get_shared_instance()
            catalog_conn = getattr(shared_logger, "conn", None)
        except Exception:
            catalog_conn = None
        try:
            route_available, _route_error = validate_chat_provider_model_pair(
                provider,
                model,
                account_id=account_id,
                conn=catalog_conn,
                allow_unverified_builtin=allow_unverified_builtin,
            )
        except ModelCatalogUnavailableError as exc:
            raise ModelUnavailableError("model_unavailable: model catalog is unavailable") from exc
        if not route_available:
            raise ModelUnavailableError(
                f"model_unavailable: provider/model route '{provider}/{model}' is unavailable"
            )
    temperature_raw = next(
        (
            value
            for value in (
                agent_cfg.get("temperature"),
                agents_cfg.get("default_temperature"),
                registry_cfg.get("temperature"),
                CONFIG.get("default_temperature"),
            )
            if value is not None
        ),
        0.1,
    )
    temperature = _to_float(temperature_raw, 0.1)
    request_timeout = _normalize_timeout_seconds(timeout_seconds)
    max_retries_raw = None
    for candidate in (
        agent_cfg.get("max_retries"),
        agents_cfg.get("max_retries"),
        registry_cfg.get("max_retries"),
    ):
        if candidate is not None:
            max_retries_raw = candidate
            break
    max_retries: int | None = None
    try:
        if max_retries_raw is not None:
            max_retries = max(0, int(max_retries_raw))
    except (TypeError, ValueError):
        max_retries = None
    if request_timeout is not None and max_retries is None:
        # Provider SDK defaults retry timed-out HTTP calls internally, which can
        # multiply a configured request deadline several times before control
        # returns to Flow Steward. Keep the operator timeout authoritative by
        # default; the orchestration retry policy handles retryable responses.
        max_retries = 0

    if is_extension_provider_key(provider):
        try:
            return _build_extension_chat_model(
                provider_key=provider,
                model_id=model,
                account_id=account_id,
                temperature=temperature,
                request_timeout=request_timeout,
                max_retries=max_retries,
            )
        except ModelProviderResolutionError as exc:
            if route.source in _PROJECT_ROUTE_SOURCES:
                raise ModelProviderResolutionError(
                    project_model_unavailable_message(config, route, agent_name=agent_name)
                ) from exc
            raise

    if provider not in {"openai", "openai_compatible", "anthropic", "google", "azure_openai"}:
        raise ModelUnavailableError(f"model_unavailable: unsupported provider '{provider}'")
    if named_provider is None:
        raise ModelUnavailableError(
            "model_unavailable: API-backed models require a verified saved provider"
        )
    provider_api_key = str(named_provider.get("_decrypted_api_key") or "").strip()
    if not provider_api_key:
        raise ModelUnavailableError(
            "model_unavailable: API-backed models require a verified saved provider"
        )

    global ChatAnthropic, ChatGoogleGenerativeAI, ChatOpenAI, AzureChatOpenAI

    if provider == "anthropic":
        if ChatAnthropic is None:
            from langchain_anthropic import ChatAnthropic as _ChatAnthropic

            ChatAnthropic = _ChatAnthropic
        kwargs = {
            "model": model,
            "base_url": official_provider_base_url("anthropic"),
            "metadata": {
                "flow_steward_provider": provider_identity,
                "flow_steward_provider_policy": provider,
                "flow_steward_model": model,
            },
        }
        # Anthropic rejects non-default sampling parameters for the current
        # adaptive-thinking Sonnet/Opus releases; omission is the accepted
        # default and preserves their native behavior.
        if model not in {"claude-fable-5", "claude-opus-5", "claude-opus-4-8", "claude-sonnet-5"}:
            kwargs["temperature"] = temperature
        kwargs["api_key"] = provider_api_key
        if request_timeout is not None:
            kwargs["timeout"] = request_timeout
        if max_retries is not None:
            kwargs["max_retries"] = max_retries
        return ChatAnthropic(**kwargs)

    if provider == "google":
        if ChatGoogleGenerativeAI is None:
            from langchain_google_genai import ChatGoogleGenerativeAI as _ChatGoogleGenerativeAI

            ChatGoogleGenerativeAI = _ChatGoogleGenerativeAI
        kwargs = {
            "model": model,
            "temperature": temperature,
            "base_url": official_provider_base_url("google"),
            "vertexai": False,
            "metadata": {
                "flow_steward_provider": provider_identity,
                "flow_steward_provider_policy": provider,
                "flow_steward_model": model,
            },
        }
        kwargs["google_api_key"] = provider_api_key
        if request_timeout is not None:
            kwargs["request_timeout"] = request_timeout
        if max_retries is not None:
            kwargs["max_retries"] = max_retries
        return _build_pinned_google_chat_model(ChatGoogleGenerativeAI, **kwargs)

    if provider == "azure_openai":
        if AzureChatOpenAI is None:
            from langchain_openai import AzureChatOpenAI as _AzureChatOpenAI

            AzureChatOpenAI = _AzureChatOpenAI
        np = named_provider or {}
        kwargs = {
            "model": model,
            "temperature": temperature,
            "azure_deployment": np["deployment_name"],
            "api_version": np["api_version"],
            "metadata": {
                "flow_steward_provider": provider_identity,
                "flow_steward_provider_policy": provider,
                "flow_steward_model": model,
            },
        }
        kwargs["azure_endpoint"] = _safe_pinned_base_url(np["base_url"], kwargs)
        if np.get("_decrypted_api_key"):
            kwargs["api_key"] = np["_decrypted_api_key"]
        if request_timeout is not None:
            kwargs["timeout"] = request_timeout
        if max_retries is not None:
            kwargs["max_retries"] = max_retries
        return AzureChatOpenAI(**kwargs)

    # openai or openai_compatible
    if ChatOpenAI is None:
        from langchain_openai import ChatOpenAI as _ChatOpenAI

        ChatOpenAI = _ChatOpenAI
    direct_gpt_5_6 = provider == "openai" and model in {
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
    }
    kwargs = {
        "model": model,
        "metadata": {
            "flow_steward_provider": provider_identity,
            "flow_steward_provider_policy": provider,
            "flow_steward_model": model,
        },
    }
    if direct_gpt_5_6:
        # GPT-5.6 reasoning plus function tools is a Responses API contract.
        # Keep the model's documented medium baseline explicit; Chat Completions
        # only supports function tools at effective reasoning ``none``.
        kwargs["use_responses_api"] = True
        kwargs["reasoning"] = {"effort": "medium"}
    else:
        kwargs["temperature"] = temperature
    if provider == "openai":
        kwargs["base_url"] = OFFICIAL_OPENAI_BASE_URL
    else:
        kwargs["base_url"] = _safe_pinned_base_url(named_provider["base_url"], kwargs)
    kwargs["api_key"] = provider_api_key
    if request_timeout is not None:
        kwargs["timeout"] = request_timeout
    if max_retries is not None:
        kwargs["max_retries"] = max_retries
    return ChatOpenAI(**kwargs)


def get_builtin_audit_chat_model(provider: str, model: str):
    """Create an unpublished built-in candidate exclusively for a live audit.

    This bypasses only the release-publication gate; it never adds a route to
    selectors, defaults, or production fallback resolution.
    """
    return get_chat_model(
        {"agents_config": {"default_provider": str(provider), "default_model": str(model)}},
        allow_unverified_builtin=True,
    )
