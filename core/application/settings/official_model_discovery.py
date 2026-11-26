"""Read model suggestions from the official API using the provider's own key."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from core.application.extensions.url_safety import build_pinned_httpx_transport
from core.application.settings.model_provider_endpoint_policy import official_provider_base_url

DISCOVERABLE_PROVIDER_TYPES = frozenset({"openai", "anthropic"})
_ANTHROPIC_API_VERSION = "2026-10-21"
_MAX_ANTHROPIC_PAGES = 10


class ModelDiscoveryError(Exception):
    """A safe error code for the operator, independent of upstream response text."""


def _read_page(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = client.get(url, headers=headers, params=params)
    if response.status_code in (401, 403):
        raise ModelDiscoveryError("authentication_error")
    if response.status_code == 429:
        raise ModelDiscoveryError("rate_limited")
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ModelDiscoveryError("invalid_response")
    return payload


def _openai_models(client: httpx.Client, base_url: str, api_key: str) -> list[dict[str, Any]]:
    page = _read_page(client, f"{base_url}/models", headers={"Authorization": f"Bearer {api_key}"})
    today = datetime.now(UTC).date().isoformat()
    return sorted(
        (
            row
            for row in page["data"]
            if isinstance(row, dict)
            and not (
                isinstance(shutdown_date := row.get("shutdown_date"), str)
                and shutdown_date <= today
            )
        ),
        key=lambda row: row["created"] if isinstance(row.get("created"), int) else 0,
        reverse=True,
    )


def _anthropic_models(client: httpx.Client, base_url: str, api_key: str) -> list[dict[str, Any]]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "anthropic-version": _ANTHROPIC_API_VERSION,
    }
    params: dict[str, Any] = {"limit": 1000, "lifecycle[]": "active"}
    models: list[dict[str, Any]] = []
    cursors: set[str] = set()
    for _ in range(_MAX_ANTHROPIC_PAGES):
        page = _read_page(client, f"{base_url}/v1/models", headers=headers, params=params)
        models.extend(row for row in page["data"] if isinstance(row, dict))
        if page.get("has_more") is False:
            return models
        cursor = page.get("last_id")
        if page.get("has_more") is not True or not isinstance(cursor, str) or not cursor:
            break
        if cursor in cursors:
            break
        cursors.add(cursor)
        params["after_id"] = cursor
    raise ModelDiscoveryError("invalid_response")


def discover_official_models(provider_type: str, api_key: str) -> list[dict[str, str]]:
    """Return upstream metadata only. Saving a model still requires the live probes."""
    if provider_type not in DISCOVERABLE_PROVIDER_TYPES:
        raise ValueError("Unsupported official model discovery provider")

    base_url = official_provider_base_url(provider_type)
    read_models = _openai_models if provider_type == "openai" else _anthropic_models
    try:
        transport = build_pinned_httpx_transport(purpose="official model discovery")
        with httpx.Client(transport=transport, timeout=10, trust_env=False) as client:
            rows = read_models(client, base_url, api_key)
    except httpx.TimeoutException:
        raise ModelDiscoveryError("timeout") from None
    except httpx.HTTPError:
        raise ModelDiscoveryError("upstream_error") from None
    except ValueError:
        raise ModelDiscoveryError("invalid_response") from None

    models: dict[str, dict[str, str]] = {}
    for row in rows:
        model_id = row.get("id")
        if isinstance(model_id, str) and model_id.strip():
            label = row.get("display_name")
            models.setdefault(
                model_id,
                {"model_id": model_id, "label": label if isinstance(label, str) else model_id},
            )
    return list(models.values())
