"""Read model suggestions from the official API using the provider's own key."""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from functools import lru_cache, partial
from typing import Any

import httpx

from core.application.extensions.url_safety import build_pinned_httpx_transport
from core.application.settings.model_provider_endpoint_policy import official_provider_base_url

DISCOVERABLE_PROVIDER_TYPES = frozenset({"openai", "anthropic"})
_ANTHROPIC_API_VERSION = "2026-10-21"
_MAX_ANTHROPIC_PAGES = 10
_OPENAI_DOCS_ORIGIN = "https://developers.openai.com"
_OPENAI_MODEL_INDEX = "/api/docs/models/all.md"
_OPENAI_DOCS_REFRESH_SECONDS = 300
_MAX_DOC_BYTES = 256 * 1024
_MAX_OPENAI_MODEL_PAGES = 128
_MODEL_LINK = re.compile(r"^- \[.+?\]\((/api/docs/models/[a-z0-9][a-z0-9._-]*\.md)\)", re.M)
_ENDPOINT = re.compile(
    r"^\|[^\n|]+\|\s*`v1/(?:chat/completions|responses)`\s*\|\s*Supported\s*\|", re.M
)
_MODALITIES = re.compile(r"^- (Input|Output) modalities: ([^\r\n]+)$", re.M)
_MODEL_ID = re.compile(r"^Model ID: `([^`\r\n]+)`", re.M)
_DEFAULT_SNAPSHOT = re.compile(r"^- Default snapshot: `([^`\r\n]+)`", re.M)
_LISTED_ID = re.compile(r"^- `([^`\r\n]+)`[ \t]*$", re.M)
_INACTIVE_STATUS = re.compile(r"^Status:[ \t]*(?:deprecated|retired|shut down)", re.M | re.I)


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


def _markdown_sections(document: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    for section in re.split(r"(?m)^## ", document)[1:]:
        title, _, body = section.partition("\n")
        sections[title.strip().casefold()] = body
    return sections


def _read_model_document(client: httpx.Client, path: str) -> str:
    with client.stream("GET", _OPENAI_DOCS_ORIGIN + path) as response:
        response.raise_for_status()
        contents = bytearray()
        for chunk in response.iter_bytes():
            if len(contents) + len(chunk) > _MAX_DOC_BYTES:
                raise ModelDiscoveryError("invalid_response")
            contents.extend(chunk)
    return contents.decode("utf-8")


def _documented_chat_ids(client: httpx.Client, path: str) -> set[str]:
    document = _read_model_document(client, path)
    if _INACTIVE_STATUS.search(document):
        return set()

    sections = _markdown_sections(document)
    details = sections.get("model details", "")
    modalities = {
        direction: {kind.strip().casefold() for kind in kinds.split(",")}
        for direction, kinds in _MODALITIES.findall(details)
    }
    if not (
        "text" in modalities.get("Input", ())
        and "text" in modalities.get("Output", ())
        and _ENDPOINT.search(sections.get("endpoints", ""))
    ):
        return set()

    return {
        *_MODEL_ID.findall(document),
        *_DEFAULT_SNAPSHOT.findall(details),
        *_LISTED_ID.findall(sections.get("model ids", "")),
        *_LISTED_ID.findall(sections.get("snapshots", "")),
    }


@lru_cache(maxsize=1)
def _openai_chat_catalog(_refresh_period: int) -> frozenset[str]:
    # Public compatibility metadata is shared across accounts. This client has
    # no access to provider credentials and only reads fixed-origin Markdown.
    transport = build_pinned_httpx_transport(purpose="OpenAI model documentation")
    with httpx.Client(transport=transport, timeout=5, trust_env=False) as client:
        index = _read_model_document(client, _OPENAI_MODEL_INDEX)
        paths = {
            path
            for section, body in _markdown_sections(index).items()
            if not section.startswith(("deprecated", "retired"))
            for path in _MODEL_LINK.findall(body)
        }
        if not 0 < len(paths) <= _MAX_OPENAI_MODEL_PAGES:
            raise ModelDiscoveryError("invalid_response")

        with ThreadPoolExecutor(max_workers=8) as pool:
            models = frozenset().union(*pool.map(partial(_documented_chat_ids, client), paths))
    if not models:
        raise ModelDiscoveryError("invalid_response")
    return models


def _openai_models(client: httpx.Client, base_url: str, api_key: str) -> list[dict[str, Any]]:
    page = _read_page(client, f"{base_url}/models", headers={"Authorization": f"Bearer {api_key}"})
    if not page["data"]:
        return []
    documented = _openai_chat_catalog(int(time.monotonic() // _OPENAI_DOCS_REFRESH_SECONDS))
    today = datetime.now(UTC).date().isoformat()
    return sorted(
        (
            row
            for row in page["data"]
            if isinstance(row, dict)
            and isinstance(model_id := row.get("id"), str)
            and model_id in documented
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
        # The Claude Models API lists text-generating Claude models for
        # Messages. Keep the requested lifecycle filter on both sides.
        models.extend(
            row
            for row in page["data"]
            if isinstance(row, dict)
            and row.get("type") == "model"
            and row.get("lifecycle") == "active"
        )
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
