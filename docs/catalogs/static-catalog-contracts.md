# Static catalog contracts (V1)

Flow Steward reads three curated, read-only catalogs from Git-hosted static JSON:

| Catalog | Configuration | Surface |
| --- | --- | --- |
| Workflow templates | `FS_WORKFLOW_TEMPLATE_CATALOG_URL` | `Create workflow → From catalog` |
| Extensions | `FS_EXTENSION_CATALOG_URL` | `Extensions → Marketplace` |
| MCP servers | `FS_MCP_CATALOG_URL` | `Registry → MCP → Catalog` |

All values are absolute HTTPS URLs of a JSON index, declared in the canonical
env manifest, bound into the shared app/worker environment of both Compose
profiles, and reached through `CONFIG`. Application code never reads the
environment for them.

By default they name the official catalogs, so a fresh install has something to
install:

- `https://raw.githubusercontent.com/Flow-Steward/workflows/main/index.json`
- `https://raw.githubusercontent.com/Flow-Steward/extensions/main/index.json`
- `https://raw.githubusercontent.com/Flow-Steward/mcps/main/index.json`

Set either to another index to use your own catalog. Set it to an empty value to
leave that catalog unconfigured; every other path in the product keeps working,
including starting a workflow from scratch, importing workflow YAML by hand, and
installing an extension from a file.

The two catalogs are versioned and cached independently. Neither accepts the
other's index, and a failure in one does not affect the other.

## Publishing

Publish three kinds of file to any static HTTPS host:

1. one JSON index per catalog;
2. one immutable workflow bundle YAML per workflow template version; and
3. one immutable release ZIP per extension version.

Artifacts are immutable. A fix to `gmail-ai-triage@1.0.0` is published as
`1.0.1`, never as replacement bytes at the same URL, because the index pins
every artifact by byte size and SHA-256 and a replacement would simply stop
verifying.

Every artifact URL in an index is a **final absolute HTTPS URL**. Flow Steward
does not resolve repository branches or tags, does not call the GitHub REST or
GraphQL APIs, does not search releases, does not accept a mutable branch
archive such as `main.zip`, and does not resolve relative URLs. The browser
never fetches a catalog host directly: it talks only to Flow Steward.

## Workflow template index

```json
{
  "schema_version": "workflow_template_catalog/v1",
  "catalog_revision": "2026-09-18.1",
  "published_at": "2026-09-18T00:00:00Z",
  "items": [
    {
      "item_id": "gmail_ai_triage",
      "version": "1.0.0",
      "name": "Gmail AI triage",
      "summary": "Label and route incoming Gmail threads.",
      "description": "Longer prose shown on the selected template.",
      "categories": ["productivity"],
      "tags": ["gmail", "triage"],
      "integrations": ["gmail"],
      "trigger_types": ["schedule"],
      "setup_requirements": [
        {"kind": "extension", "label": "Gmail extension", "ref": "gmail", "required": true}
      ],
      "compatible_flow_steward": {"min": "0.2.0", "max": ""},
      "bundle_schema": "platform.workflow_bundle.v2",
      "workflow_yaml_url": "https://catalog.example/workflows/gmail_ai_triage-1.0.0.yaml",
      "workflow_yaml_bytes": 4096,
      "workflow_yaml_sha256": "<64 hex characters>",
      "publication_status": "published",
      "verification_status": "verified",
      "added_at": "2026-09-01T08:00:00Z",
      "updated_at": "2026-09-18T00:00:00Z",
      "includes": [{"item_id": "gmail_ai_triage_label", "name": "Label one Gmail thread"}]
    }
  ],
  "integration_labels": {
    "gmail": "Gmail"
  },
  "category_labels": {
    "productivity": "Productivity"
  }
}
```

Rules:

- `schema_version` must be exactly `workflow_template_catalog/v1`.
- `integration_labels` is optional: an object mapping an integration id used in
  `items[].integrations` to the name shown on cards and in the Integration
  filter (at most 120 characters). Ids remain what filters match on; an id with
  no label is shown as the id. A malformed map rejects the whole index.
- `category_labels` is optional: category id → the name to show. Both catalogs
  use one category list (`categories.json` in the extension catalog, copied by
  the workflow catalog), so the names are the same in both. A malformed map
  rejects the whole index.
- `added_at` and `updated_at` are optional ISO 8601 date-times: when the
  template first appeared in the catalog, and when the listed version did. They
  let the UI offer "Recently added" (with A–Z, the catalog's only sorts) and
  mark a template New for its first 30 days. A malformed value rejects the whole
  index.
- `item_id` is unique within the index; `version` is SemVer.
- `compatible_flow_steward` is optional; both bounds are inclusive, and an
  omitted bound means unbounded. They are Flow Steward product versions, the
  `VERSION` file a deployment reports as `build_version`; an extension's own
  `platform_min` is a separate host contract version.
- `bundle_schema` names the portability format of the template file. Flow
  Steward imports only `platform.workflow_bundle.v2`; a template in any other
  format, or one whose entry has no `bundle_schema` (unknown), is listed as
  unavailable with the reason in its card, "Use template" is off, and the
  catalog create and preflight routes answer 409
  `workflow_template_bundle_schema_unsupported` without downloading the file.
- `publication_status` is `published` or `deprecated`; `verification_status` is
  `verified` or `unverified`.
- `workflow_yaml_bytes` is a positive integer and `workflow_yaml_sha256` is a
  lowercase hex SHA-256 of the exact published bytes.

A template's bundle YAML is an ordinary workflow portability bundle in
`platform.workflow_bundle.v2` (steps, groups and an execution graph); a
phase-based v1 template is refused at import before anything is written. It may
reference a connector by a stable **logical `extension_id`** in place of a local
install id, and an ordinary export records that `extension_id` beside the
exporting machine's install id. When exactly one active install of that
extension in this account satisfies the bundle, it is mapped through the
existing portability resolution and rewrite path. Catalog create and manual YAML
import share this resolver, so the same file maps the same way on both paths;
an ambiguous or missing install is left for the operator to resolve.

## Extension index

```json
{
  "schema_version": "extension_catalog/v1",
  "catalog_revision": "2026-09-18.1",
  "published_at": "2026-09-18T00:00:00Z",
  "items": [
    {
      "extension_id": "acme_crm",
      "version": "1.10.0",
      "name": "Acme CRM",
      "summary": "Sync contacts with Acme.",
      "description": "Longer prose shown on the card.",
      "categories": ["sales"],
      "capabilities": ["queries", "actions"],
      "repository_url": "https://github.example/flow-steward/acme_crm",
      "release_zip_url": "https://catalog.example/extensions/acme_crm-1.10.0.zip",
      "release_zip_bytes": 65536,
      "release_zip_sha256": "<64 hex characters>",
      "compatible_flow_steward": {"min": "0.2.0", "max": ""},
      "permissions_summary": ["outbound_http"],
      "setup_summary": "Configure one project connection before enabling.",
      "publication_status": "published",
      "verification_status": "verified",
      "icon": {"url": "https://raw.githubusercontent.com/...", "sha256": "<64 hex characters>", "bytes": 3172, "mime_type": "image/png", "size": "128x128"}
    }
  ]
}
```

`icon` is required and has the MCP index's icon shape: an absolute `https`
`url`, the file's `sha256`, its `bytes` (1 to 102400), `mime_type`
`image/png` and `size` `WxH` of a square 32-512 px icon. It is the bundle's own
`icon.png`, which validation, packaging and install already require. The icon
is fetched only through Flow Steward
(`GET /api/extensions/catalog/{extension_id}/icon`), after its size and SHA-256
verify and only when it is a PNG; the listing carries `has_icon` and an
`icon_url` on Core's origin, so the browser never contacts the icon host. An
installed extension's logo is served from its bundle by
`GET /api/extensions/{extension_id}/icon`, to whoever may read the extension.

The extension index takes the same optional `category_labels` map and the same
per-item `added_at` / `updated_at` (when the extension entered the catalog, and
when the listed release did). An item may also carry optional `tags`, a list of
strings that Marketplace search matches; they are never a filter.

Capability ids (`items[].capabilities`) and permission scopes
(`items[].permissions_summary`) are the extension contract's own, so the index
does not name them: `GET /api/extensions/catalog` adds Core's
`capability_labels` and `permission_labels` (id → name) next to
`category_labels`, and an id without a name is shown as the id.

`extension_id` is the stable logical id that joins a catalog entry to this
account's install records. `repository_url` is documentation, not a download
source.

### Per-target releases

An extension that ships a native executable, or Python wheels built for one
architecture, is packaged once per runtime target
(`flow-steward extensions bundle --runtime-target linux-amd64`), because the
installer accepts exactly one architecture per archive. The bundle keeps only
that target's `bin/<target>/` and the wheels that install on it (pure wheels and
`manylinux`/`musllinux` wheels for its architecture); `.fs-package.yaml` names the
target and, when there is one, the native executable and its SHA-256. A target
package that declares no native executable may not carry one. Such an entry adds
`release_targets`, one archive per target:

```json
"release_zip_url": "https://catalog.example/acme_db-1.0.0-linux-amd64.zip",
"release_zip_bytes": 37748736,
"release_zip_sha256": "<the linux-amd64 digest>",
"release_targets": {
  "linux-amd64": {"release_zip_url": "…-linux-amd64.zip", "release_zip_bytes": 37748736, "release_zip_sha256": "…"},
  "linux-arm64": {"release_zip_url": "…-linux-arm64.zip", "release_zip_bytes": 33554432, "release_zip_sha256": "…"}
}
```

Flow Steward resolves the entry to the archive for the host it runs on, so the
listing, the pinned digest and the download all name that one archive. A host
with no archive in `release_targets` shows the entry as **Incompatible** and
refuses the install before downloading anything. The top-level `release_zip_*`
fields stay required and should name the `linux-amd64` archive: a Flow Steward
that predates `release_targets` reads only those.

## MCP server index

The MCP server catalog (`github.com/Flow-Steward/mcps`) publishes the shape of
the official MCP Registry's `GET /v0.1/servers` response, so the same reader
accepts a company's private MCP registry:

```json
{
  "schema_version": "mcp_server_catalog/v1",
  "catalog_revision": "2026-10-10.fdb609ff",
  "published_at": "2026-10-10T12:00:00Z",
  "category_labels": {"productivity": "Productivity"},
  "servers": [
    {
      "server": {
        "name": "com.notion/mcp",
        "version": "1.0.1",
        "remotes": [{"type": "streamable-http", "url": "https://mcp.notion.com/mcp"}],
        "_meta": {
          "io.github.flow-steward/catalog": {
            "id": "notion",
            "revision": 1,
            "title": "Notion",
            "summary": "...",
            "category": "productivity",
            "official": {"vendor": "Notion", "evidence_url": "https://developers.notion.com/..."},
            "icon": {"url": "https://raw.githubusercontent.com/...", "sha256": "...", "bytes": 3172, "mime_type": "image/png", "size": "128x128"},
            "remotes": [
              {
                "remote_id": "default",
                "label": "Notion",
                "url": "https://mcp.notion.com/mcp",
                "allowed_hosts": ["mcp.notion.com"],
                "user_defined_host": false,
                "auth": {"mode": "oauth2", "client_registration": "dcr",
                         "requested_scopes": [], "authorization_parameters": {},
                         "refresh_token_required": false}
              }
            ]
          }
        }
      },
      "_meta": {"io.modelcontextprotocol.registry/official": {"status": "active"}}
    }
  ],
  "metadata": {"count": 1}
}
```

- `schema_version` is optional, so a plain Registry response also parses. A
  record without the `io.github.flow-steward/catalog` block was never reviewed:
  it is skipped (and counted as `unreviewed_count` in availability), never
  offered for install.
- `auth.mode` is `none`, `headers` or `oauth2`; `client_registration` is `dcr`,
  `client_metadata` or `user_supplied` (the user enters their own OAuth client).
  A reviewer sets it from the vendor's documentation; a probe is never trusted.
- Headers and URL variables the user fills in come from the server record's
  `remotes[].headers` and `remotes[].variables`. Every overlay `url` must be a
  Streamable HTTP remote of that record.
- A server the Registry marks `deleted` is never listed; `deprecated` is listed
  and labelled.
- A remote may carry `field_labels`, a map from a URL variable or header name to
  the label the install form shows ("subdomain": "Freshdesk subdomain"). The
  name stays the key the URL and the request use; without a label the form shows
  the variable name made readable.
- `added_at` and `updated_at` in the catalog block are optional date-times: when
  the server first appeared in the catalog and when its entry last changed. The
  card carries both. With `added_at` the Catalog tab offers "Recently added"
  and marks a server New for its first 30 days (a catalog's first batch is not
  New); without it the tab has neither.
- An optional `keywords` list (at most 20 terms) holds the other names people
  search for: the products one vendor entry covers ("Jira", "Confluence" for
  Atlassian), a former name ("Sendinblue"). Catalog search matches them along
  with the title and summary; they are not shown.
- Every listed record was reviewed by a person; a malformed one fails the whole
  index. The list endpoint returns cards only; `GET /api/mcp/catalog/{item_id}`
  returns one server's full entry for its page and install form.
- The icon is fetched only through Flow Steward
  (`GET /api/mcp/catalog/{item_id}/icon`), after its size and SHA-256 verify
  and only when it is a PNG, and served from the product's own origin, so the
  browser never contacts the icon host.

An install (`POST /api/mcp/catalog/{item_id}/install`) carries the `remote_id`
and the item `revision` the form showed; a different current revision is a
`409 mcp_catalog_item_changed` with the current item. The endpoint, its allowed
hosts and the sign-in settings come from the index, never from the request.
URL variables are checked against their choices and, after substitution, the
host against `allowed_hosts` unless the entry declares `user_defined_host`.
The install is an ordinary connector create: same role check, same SSRF check,
same encrypted header storage, same OAuth flow.

The connector records where it came from (`catalog_item_id`,
`catalog_remote_id`, `catalog_item_revision`) so the catalog can show what a
project already connected. That is the only link: a catalog change never
rewrites, disconnects or deletes a connector made from it.

## Cache and failure behaviour

Each catalog has its own process-local, in-memory cache
(`core/infrastructure/static_catalog/index_cache.py`, one implementation for
all three). It is a fallback for outages, never a freshness window:

- there is no TTL. Every catalog read (the listing, an entry's page, an
  install, the availability probe) asks the host again, so an approved
  extension, template or server shows on the next page load;
- the read is conditional: if the previous `200` carried an `ETag` it sends
  `If-None-Match`, and if it carried `Last-Modified` it sends
  `If-Modified-Since`. A `304` reuses the already parsed index; an unchanged
  index costs a response with no body. A host that always answers `200` is
  equally correct and needs no validators at all;
- the in-memory index is replaced only after the **entire** new document
  validates. One malformed item rejects the read and the previous index keeps
  serving;
- if a read fails (network error, HTTP error, invalid document) and an index is
  cached, that index is served and availability reports `stale: true` with
  `last_refresh_error`; `fetched_at` is when the host last confirmed the index;
- a failed read starts a 10-second cooldown in which reads serve the last good
  index (or report the catalog unavailable) without dialling the host, so a
  dead host costs one outbound round trip per cooldown rather than one per
  request. A successful read is never cached this way;
- one read at a time goes to the host: a request that arrives while a read is
  in flight waits for it and takes its result;
- there is no background worker, scheduler, control loop, queue, or
  distributed lock;
- if the first read fails, the API answers catalog-unavailable and the UI
  points back at the paths that do not need a catalog;
- nothing is persisted. After a restart the first request fetches again; there
  is no database table, migration, or filesystem snapshot;
- changing the configured URL drops the cached index rather than attributing
  one publisher's entries to another;
- icons are content-addressed: verified icon bytes stay cached by their
  SHA-256, and an icon request is answered from the index the listing just
  read.

The official catalogs are served from `raw.githubusercontent.com`, whose CDN
caches a file for about five minutes. A merged catalog change can therefore
take up to that long to reach the host's answer; Core adds no delay of its own.

Every outbound request is HTTPS-only, SSRF-checked with connected-peer pinning,
timeout-bounded, redirect-bounded (each hop re-validated), and size-bounded. An
index refresh downloads **no** referenced YAML or ZIP: artifacts are fetched one
at a time, only when a user selects that item, and only after the declared byte
size and SHA-256 both verify.

## Behaviour in the product

A template is refused before anything is downloaded when its
`compatible_flow_steward` range excludes this deployment.

A template is one workflow and the child workflows it calls, in one file, so it
installs complete and nobody has to find a child among the other templates. The
author exports it with **Export YAML with child workflows**
(`/api/workflows/{id}/export.yaml?include_children=true`), which writes the
workflow and every workflow it invokes, transitively, from the same account. The
index lists the children under `includes` (`item_id`, `name`); a card shows them,
and they never appear as templates of their own. A template that carries a
workflow nothing in it calls is refused before anything is written, because the
importer would create it without the card saying so.

Workflow templates are **copy-only**. Creating from a template produces disabled
independent drafts through the existing portability apply path: the template's
workflow and each child it carries, children first. Nothing records where they
came from, nothing links them to the catalog, and a catalog refresh never
queries, modifies, merges, migrates, disables, or flags a local workflow. If the
template's workflow id already exists, the existing workflow is preserved and
the normal import conflict behaviour asks for a different id. A child whose id
already exists can be renamed the same way, or the existing workflow reused; the
calling workflow is pointed at whichever the child became.

Extension cards show one state, computed by joining the catalog with existing
account-scoped install records using SemVer (not string order, so `1.9.0` is
older than `1.10.0`):

| Condition | State |
| --- | --- |
| No local install | `Install` |
| Same version installed | `Installed` |
| Older version installed (active or disabled) | `Update available` |
| Newer version installed | `Installed · Newer version` |
| Same or newer version installed but disabled | `Installed · Disabled` |
| Catalog release outside the compatible range | `Incompatible` |

`Incompatible` replaces only the two states that assert the catalog release
could be installed (`Install`, `Update available`). The other three describe the
local install and stay accurate whatever the catalog offers.

Cards and rows show `Update available` as a badge only. The extension's own
page turns it into the main action, **Update to X**, with "Open installed
extension" kept beside it. Update is the same catalog install call: when the
catalog release is newer than the install, the backend hands the verified
archive to the archive installer, whose update path is the one a newer ZIP
uploaded through "Install extension from file" takes — same pinned digest,
manifest and `platform_min` checks, data migrations, browser-authority
approval and rollback on failure. A disabled install can be updated and stays
disabled. Only administrators see the button enabled, under the same
archive-lifecycle condition as Install.

When the newer release asks for permissions the installed release did not,
the listing carries them as `added_permissions` and the page lists them above
the button as "This version also asks for: …". The installed set is read from
the installed release's descriptor (`required_scopes`); when it cannot be read
the list is empty.

The install endpoint refuses, before downloading anything, a same-version
reinstall (`extension_catalog_already_installed`), a downgrade
(`extension_catalog_downgrade_refused`) and an install whose recorded version
cannot be read (`extension_catalog_installed_version_unreadable`), all `409`.

Choosing `Install` sends the `extension_id` **and the version and SHA-256 the
card showed**. The listing and the install may be served by two processes with
independent caches, and a republished version keeps its number while changing
its bytes, so both are pinned and the backend refuses rather than substituting
a release the user never saw. It then fetches that entry's ZIP, verifies its size and SHA-256,
checks that the archive's own `extension.yaml` declares the same
`extension_id` and `version` the catalog advertised — the digest proves the
bytes are the published ones, not that the metadata describes them honestly —
and hands the verified archive to the existing extension installer exactly
once. All existing manifest, compatibility, dependency, permission, health,
and lifecycle checks run unchanged, including the browser-authority approval
handshake: a `412` reaches the same approval modal the file-upload install
uses, and the approved hash is replayed on the retry.

Under the writable-installer deployment profile
(`FS_EXTENSION_INSTALLER_PROXY_ENABLED=1`), `POST /api/extensions/catalog/install`
is proxied to the installer process exactly like the file upload route, so that
process needs outbound HTTPS access to the catalog artifact host.

Every catalog HTTP handler is a synchronous `def`. The work behind it — the
outbound fetch, the install-store read, the archive install — is blocking and
can take tens of seconds; declared `async` it would run on the event loop and
stall every other request in the process.

### Where the code lives

`core/infrastructure/static_catalog/` holds the configuration projection, the
numeric bounds, and the bounded fetch. The fetch reaches the SSRF guard through
an injected `CatalogUrlGuard` rather than importing it: the repository's
canonical guard currently lives at `core/application/extensions/url_safety`,
and infrastructure must not depend on the application layer. Each business area
binds its own guard and exposes an area-owned artifact fetcher.

## V1 non-goals

Explicitly out of scope, and not to be added by accretion:

- persistent catalog storage: database tables, columns, migrations, or
  filesystem snapshots (an MCP connector's three provenance columns describe the
  connector, not the catalog, and nothing reads the catalog from them);
- background synchronization: workers, schedulers, control loops, queues, or
  distributed locks;
- a deployable catalog or marketplace service, a generic marketplace item
  model, a plugin framework, or a source-adapter hierarchy;
- catalog create, update, delete, publish, moderation, or administration
  endpoints or UI;
- third-party self-service publishing, ratings, reviews, comments, publisher
  profiles, paid listings, or entitlements;
- managed workflow updates: provenance, linking, update notifications, merging,
  or migrations;
- extension rollbacks, downgrades, or release switching (an update to the
  newer catalog release is supported, through the existing archive update
  path);
- signatures, attestations, PKI, or key management;
- GitHub REST or GraphQL calls, branch/tag resolution, release search, or
  mutable branch archives;
- browser-side fetching of catalog files or artifacts;
- telemetry or analytics.
