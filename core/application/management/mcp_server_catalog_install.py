"""Install an MCP server from the catalog: a filled-in connector, nothing more.

The catalog only pre-fills what the manual "Add MCP server" form asks for. An
install is an ordinary ``McpConnectorCommandService.create_connector`` call, so
the same access check, URL safety check, encrypted header storage and OAuth
flow apply. What the catalog adds is that the endpoint, the hosts it may point
at, and the sign-in settings come from the reviewed index — never from the
request — and that a stale form is refused:

* the client sends the ``remote_id`` and the item ``revision`` it showed; a
  changed revision is a 409 with the current item, so the user confirms again;
* URL variables are checked against their declared choices and, after
  substitution, the host against ``allowed_hosts`` unless the catalog says the
  user defines the host (a self-hosted product);
* only headers the server record declares are accepted.

A later catalog change never rewrites or removes a connector made here; the
connector only remembers where it came from (``catalog_item_id``,
``catalog_remote_id``, ``catalog_item_revision``).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import urlsplit

from core.application.management.command_models import McpConnectorCreateInput
from core.application.management.errors import (
    ServiceConflictError,
    ServiceNotFoundError,
    ServiceValidationError,
)
from core.application.management.mcp_connector_common import (
    _require_connector_mutation_access,
)
from core.application.management.mcp_connector_services import (
    McpConnectorCommandService,
    McpConnectorQueryService,
    McpConnectorStore,
)
from core.application.management.mcp_server_catalog import (
    McpCatalogIndex,
    McpCatalogItem,
    McpCatalogRemote,
    host_allowed,
    load_mcp_server_catalog,
)
from core.application.management.scope import ScopeContext

_URL_VARIABLE = re.compile(r"\{(\w+)\}", re.ASCII)
_HOST_VALUE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_PATH_VALUE = re.compile(r"^[A-Za-z0-9._~-]{1,200}$")
_CREDENTIAL_HEADERS = frozenset(
    {"authorization", "proxy-authorization", "x-api-key", "api-key", "apikey"}
)
_MAX_HEADER_VALUE = 4096


def _invalid(
    message: str, *, field: str, error: str = "mcp_catalog_install_invalid"
) -> ServiceValidationError:
    return ServiceValidationError(
        message, payload={"code": error, "field": field, "message": message}
    )


def resolve_remote_url(remote: McpCatalogRemote, variables: dict[str, str]) -> str:
    """Substitute the user's variable values and check where the URL now points."""
    declared = {variable.name: variable for variable in remote.variables}
    unknown = sorted(set(variables) - set(declared))
    if unknown:
        raise _invalid(f"Unknown variables: {', '.join(unknown)}", field="variables")
    host_template = (
        urlsplit(_URL_VARIABLE.sub(lambda m: f"__{m.group(1).lower()}__", remote.url)).hostname
        or ""
    )
    values: dict[str, str] = {}
    for name in _URL_VARIABLE.findall(remote.url):
        variable = declared[name]
        value = str(variables.get(name) or variable.default or "").strip()
        if not value:
            raise _invalid(f"{name} is required", field=f"variables.{name}")
        if variable.choices and value not in variable.choices:
            raise _invalid(
                f"{name} must be one of {', '.join(variable.choices)}", field=f"variables.{name}"
            )
        in_host = f"__{name.lower()}__" in host_template
        if not (_HOST_VALUE if in_host else _PATH_VALUE).match(value):
            raise _invalid(
                f"{name} contains characters a URL cannot take here", field=f"variables.{name}"
            )
        values[name] = value
    url = _URL_VARIABLE.sub(lambda m: values[m.group(1)], remote.url)
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise _invalid("The endpoint must be an https URL", field="variables")
    if not remote.user_defined_host and not host_allowed(parts.hostname, remote.allowed_hosts):
        raise _invalid(
            f"{parts.hostname} is not an address this catalog server may use",
            field="variables",
            error="mcp_catalog_host_not_allowed",
        )
    return url


def resolve_headers(remote: McpCatalogRemote, headers: dict[str, str]) -> dict[str, str]:
    """Only declared headers; required ones present; none of them for OAuth sign-in."""
    declared = {header.name.lower(): header for header in remote.headers}
    resolved: dict[str, str] = {}
    for name, value in (headers or {}).items():
        header = declared.get(str(name or "").strip().lower())
        if header is None:
            raise _invalid(f"{name} is not a header this server takes", field=f"headers.{name}")
        text = str(value or "").strip()
        if not text:
            continue
        if "\r" in text or "\n" in text or len(text) > _MAX_HEADER_VALUE:
            raise _invalid(f"{header.name} has an invalid value", field=f"headers.{header.name}")
        if remote.auth_mode == "oauth2" and header.name.lower() in _CREDENTIAL_HEADERS:
            raise _invalid(
                f"{header.name} is not used when you sign in with OAuth",
                field=f"headers.{header.name}",
            )
        resolved[header.name] = text
    if remote.auth_mode == "headers":
        for header in remote.headers:
            if header.required and header.name not in resolved:
                raise _invalid(f"{header.name} is required", field=f"headers.{header.name}")
        if not resolved:
            raise _invalid("Enter the credentials this server asks for", field="headers")
    return resolved


def _connector_auth_mode(remote: McpCatalogRemote, headers: dict[str, str]) -> str:
    if remote.auth_mode == "oauth2":
        return "oauth2"
    if remote.auth_mode == "headers":
        return "bearer" if "authorization" in {name.lower() for name in headers} else "api_key"
    return "none"


class McpServerCatalogService:
    """List the catalog for one project and install entries as connectors."""

    def __init__(
        self,
        *,
        get_logger: Callable[[], Any],
        load_catalog: Callable[[], McpCatalogIndex] = load_mcp_server_catalog,
        save_oauth_credentials: Callable[..., Any] | None = None,
    ) -> None:
        self._get_logger = get_logger
        self._load_catalog = load_catalog
        self._save_oauth_credentials = save_oauth_credentials

    def _installed(self, scopes: Sequence[ScopeContext]) -> dict[str, list[dict[str, Any]]]:
        """Connectors made from catalog entries in these projects, by entry id."""
        connectors = McpConnectorQueryService(
            get_logger=self._get_logger
        ).list_connectors_in_projects(scopes=scopes)
        installed: dict[str, list[dict[str, Any]]] = {}
        for connector in connectors.connectors:
            if not connector.catalog_item_id:
                continue
            installed.setdefault(connector.catalog_item_id, []).append(
                {
                    "connector_id": connector.connector_id,
                    "display_name": connector.display_name,
                    "project_id": connector.project_id,
                    "remote_id": connector.catalog_remote_id,
                    "catalog_item_revision": connector.catalog_item_revision,
                }
            )
        return installed

    def list_catalog(self, *, scopes: Sequence[ScopeContext]) -> dict[str, Any]:
        """The reviewed servers, each with the connectors these projects made from it."""
        index = self._load_catalog()
        installed = self._installed(scopes)
        return {
            "kind": "mcp_server_catalog",
            "schema_version": index.schema_version,
            "catalog_revision": index.catalog_revision,
            "published_at": index.published_at,
            "category_labels": dict(index.category_labels),
            # Cards only: a browser needs the full entry only for the one it opens.
            "items": [
                {**item.dump_card(), "installed": installed.get(item.item_id, [])}
                for item in index.items
            ],
        }

    def get_item(self, item_id: str, *, scopes: Sequence[ScopeContext]) -> dict[str, Any]:
        """One server's full entry, with the connectors these projects made from it."""
        item = self._load_catalog().item(item_id)
        if item is None:
            message = "This server is not in the MCP catalog."
            raise ServiceNotFoundError(
                message,
                payload={
                    "code": "mcp_catalog_item_not_found",
                    "message": message,
                    "item_id": item_id,
                },
            )
        return {**item.dump(), "installed": self._installed(scopes).get(item.item_id, [])}

    def _current_item(self, item_id: str, item_revision: int) -> McpCatalogItem:
        item = self._load_catalog().item(item_id)
        if item is None:
            message = "This server is no longer in the MCP catalog."
            raise ServiceNotFoundError(
                message,
                payload={
                    "code": "mcp_catalog_item_not_found",
                    "message": message,
                    "item_id": item_id,
                },
            )
        if item.revision != int(item_revision):
            message = "This catalog entry changed since you opened it. Review it and install again."
            raise ServiceConflictError(
                message,
                payload={
                    "code": "mcp_catalog_item_changed",
                    "message": message,
                    "item_id": item.item_id,
                    "current_revision": item.revision,
                    "item": item.dump(),
                },
            )
        return item

    def install(
        self,
        item_id: str,
        *,
        remote_id: str,
        item_revision: int,
        scope: ScopeContext,
        display_name: str = "",
        variables: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        client_id: str = "",
        client_secret: str = "",
        enabled: bool = True,
        requested_scopes: list[str] | None = None,
        authorization_parameters: dict[str, str] | None = None,
        refresh_token_required: bool | None = None,
    ) -> dict[str, Any]:
        """Create a connector from one catalog endpoint.

        The catalog fills the OAuth settings; the form shows them and the user
        may change scopes, authorization parameters and the refresh-token rule.
        ``None`` keeps the catalog's value. The endpoint, its hosts and how the
        client registers always come from the catalog.
        """
        _require_connector_mutation_access(scope)
        item = self._current_item(item_id, item_revision)
        remote = item.remote(remote_id)
        if remote is None:
            raise _invalid(f"{item.title} has no endpoint {remote_id!r}", field="remote_id")
        url = resolve_remote_url(remote, dict(variables or {}))
        resolved_headers = resolve_headers(remote, dict(headers or {}))
        wants_client = bool(str(client_id or "").strip() or str(client_secret or "").strip())
        if wants_client and remote.auth_mode != "oauth2":
            raise _invalid("Client credentials are only used for OAuth sign-in", field="client_id")
        if wants_client and not str(client_id or "").strip():
            raise _invalid("Client ID is required with a client secret", field="client_id")
        edited = (requested_scopes, authorization_parameters, refresh_token_required)
        if remote.auth_mode != "oauth2" and any(value is not None for value in edited):
            raise _invalid(
                "OAuth settings are only used for OAuth sign-in", field="requested_scopes"
            )
        oauth_config: dict[str, Any] = {}
        if remote.auth_mode == "oauth2":
            oauth_config = {
                "requested_scopes": list(remote.requested_scopes)
                if requested_scopes is None
                else [str(s).strip() for s in requested_scopes if str(s).strip()],
                "authorization_parameters": dict(remote.authorization_parameters)
                if authorization_parameters is None
                else {
                    str(k).strip(): str(v)
                    for k, v in authorization_parameters.items()
                    if str(k).strip()
                },
                "refresh_token_required": remote.refresh_token_required
                if refresh_token_required is None
                else bool(refresh_token_required),
            }
        title = str(display_name or "").strip() or item.title
        if len(item.remotes) > 1 and not str(display_name or "").strip():
            title = f"{item.title} ({remote.label})"
        connector = McpConnectorCommandService(get_logger=self._get_logger).create_connector(
            McpConnectorCreateInput(
                display_name=title[:200],
                description=item.summary,
                transport="streamable_http",
                base_url=url,
                auth_mode=_connector_auth_mode(remote, resolved_headers),
                headers=resolved_headers,
                oauth_config=oauth_config,
                enabled=enabled,
                catalog_item_id=item.item_id,
                catalog_remote_id=remote.remote_id,
                catalog_item_revision=item.revision,
            ),
            scope=scope,
        )
        client_saved = False
        client_error = ""
        if wants_client:
            # The connector exists either way; a refused client is entered again
            # on its page instead of failing an install that already happened.
            try:
                self._save_client(connector.connector_id, scope, client_id, client_secret)
                client_saved = True
            except Exception as exc:
                client_error = str(exc) or "The OAuth client could not be saved."
        needs_client = (
            remote.auth_mode == "oauth2"
            and remote.client_registration == "user_supplied"
            and not client_saved
        )
        next_step = "sync"
        if remote.auth_mode == "oauth2":
            next_step = "oauth_client" if needs_client else "oauth_connect"
        return {
            "connector": connector.dump(),
            "next_step": next_step,
            "setup_url": remote.setup_url or item.help_url,
            "client_error": client_error,
        }

    def _save_client(
        self, connector_id: str, scope: ScopeContext, client_id: str, client_secret: str
    ) -> None:
        save = self._save_oauth_credentials
        if save is None:
            from core.application.auth.mcp_connector_oauth import (
                save_mcp_connector_oauth_credentials as save,
            )
        bl = self._get_logger()
        row = McpConnectorStore().get_connector_row(
            bl,
            connector_id,
            account_id=str(scope.account_id or "").strip(),
            project_id=str(scope.project_id or "").strip(),
            tenant_id=str(scope.tenant_id or "").strip(),
        )
        if not row:
            raise ServiceNotFoundError(f"MCP connector '{connector_id}' not found")
        save(
            bl,
            connector_row=row,
            client_id=str(client_id).strip(),
            client_secret=str(client_secret or ""),
        )


__all__ = ["McpServerCatalogService", "resolve_headers", "resolve_remote_url"]
