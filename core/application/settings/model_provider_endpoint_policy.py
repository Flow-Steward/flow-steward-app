"""Endpoint contracts for account-scoped model-provider records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

OFFICIAL_OPENAI_BASE_URL = "https://api.openai.com/v1"


@dataclass(frozen=True)
class ProviderEndpointContract:
    """Declare whether a provider uses a fixed or account-scoped endpoint."""

    endpoint_mode: Literal["fixed", "gateway"]
    official_base_url: str | None = None
    requires_api_version: bool = False
    requires_deployment_name: bool = False


@dataclass(frozen=True)
class ProviderEndpointValues:
    """Normalized typed endpoint fields accepted by a provider contract."""

    base_url: str | None
    api_version: str | None
    deployment_name: str | None


PROVIDER_ENDPOINT_CONTRACTS: dict[str, ProviderEndpointContract] = {
    "openai": ProviderEndpointContract(
        endpoint_mode="fixed",
        official_base_url=OFFICIAL_OPENAI_BASE_URL,
    ),
    "openai_compatible": ProviderEndpointContract(endpoint_mode="gateway"),
    "azure_openai": ProviderEndpointContract(
        endpoint_mode="gateway",
        requires_api_version=True,
        requires_deployment_name=True,
    ),
    "anthropic": ProviderEndpointContract(
        endpoint_mode="fixed",
        official_base_url="https://api.anthropic.com",
    ),
    "google": ProviderEndpointContract(
        endpoint_mode="fixed",
        official_base_url="https://generativelanguage.googleapis.com/",
    ),
}


def validate_provider_endpoint_fields(
    provider_type: str,
    *,
    base_url: object = None,
    api_version: object = None,
    deployment_name: object = None,
) -> ProviderEndpointValues:
    """Normalize and validate the endpoint fields for one provider type."""

    normalized_provider_type = str(provider_type or "").strip()
    contract = PROVIDER_ENDPOINT_CONTRACTS.get(normalized_provider_type)
    if contract is None:
        supported = ", ".join(PROVIDER_ENDPOINT_CONTRACTS)
        raise ValueError(
            f"Unsupported provider_type: {normalized_provider_type}. Supported: {supported}"
        )

    normalized_base_url = str(base_url or "").strip() or None
    normalized_api_version = str(api_version or "").strip() or None
    normalized_deployment_name = str(deployment_name or "").strip() or None

    if contract.endpoint_mode == "fixed":
        if normalized_base_url:
            raise ValueError(f"provider_type '{normalized_provider_type}' does not accept base_url")
    elif not normalized_base_url:
        raise ValueError(f"provider_type '{normalized_provider_type}' base_url is required")

    if contract.requires_api_version:
        if not normalized_api_version:
            raise ValueError(f"provider_type '{normalized_provider_type}' api_version is required")
    elif normalized_api_version:
        raise ValueError(f"provider_type '{normalized_provider_type}' does not accept api_version")

    if contract.requires_deployment_name:
        if not normalized_deployment_name:
            raise ValueError(
                f"provider_type '{normalized_provider_type}' deployment_name is required"
            )
    elif normalized_deployment_name:
        raise ValueError(
            f"provider_type '{normalized_provider_type}' does not accept deployment_name"
        )

    return ProviderEndpointValues(
        base_url=normalized_base_url,
        api_version=normalized_api_version,
        deployment_name=normalized_deployment_name,
    )


def official_provider_base_url(provider_type: str) -> str:
    """Return the explicit official endpoint for a fixed-endpoint provider."""

    contract = PROVIDER_ENDPOINT_CONTRACTS.get(str(provider_type or "").strip())
    if contract is None or contract.endpoint_mode != "fixed" or not contract.official_base_url:
        raise ValueError(f"provider_type '{provider_type}' does not use a fixed official endpoint")
    return contract.official_base_url
