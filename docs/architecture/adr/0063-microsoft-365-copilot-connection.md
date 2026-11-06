# ADR 0063: Connecting Microsoft 365 Copilot to the MCP server

- Status: Accepted for implementation (the owner approved the proposed setup on
  2026-11-06: agent package recommended, admin connector as the alternative,
  platform clients as decision 2; GitHub Copilot is left for later), amended
  the same day after review: the widget-host CORS question is open, not
  settled; the connector is a separately verified option; the setup chain is
  explicit; the `resource` and DCR wording is narrowed. Built from Microsoft's
  documentation; every entry stays "not verified" until it is checked on a
  customer's Microsoft 365 tenant.
- Date: 2026-11-06
- Related: [ADR 0047](0047-inbound-mcp-oauth.md) (inbound OAuth, amended by
  decision 2), [ADR 0059](0059-workflow-report-documents.md) (reports and MCP
  Apps), the AI agents page catalog
  (`core/application/auth/agent_connection_catalog.py`,
  `docs/agent-skills/COMPATIBILITY.md`)

## Context

The owner wants business users to drive workflows from Microsoft 365 Copilot,
on the web and, if it works there, on the phone, with the workflow's response
(including a report) shown correctly. Gemini is not an option for cloud use.

What Microsoft documents today (checked 2026-11-06; sources at the end):

- **Declarative agent with an MCP plugin** (Microsoft 365 Agents Toolkit 6.12+):
  the plugin manifest names the server URL and discovers tools at run time. MCP
  plugins work only as actions inside a declarative agent. The agent can carry
  instructions (8,000 characters) and, in preview, up to eight `SKILL.md`
  skills. **MCP Apps render inline** in Copilot Chat for declarative agents
  (`_meta.ui.resourceUri`); `prefersBorder`, `_meta.ui.domain`, `permissions`
  and `frameDomains` are not supported. Declarative agents with custom actions
  and MCP Apps are listed as available in Copilot Chat without a Microsoft 365
  Copilot licence, with usage limits and tenant restrictions. For MCP Apps,
  Microsoft says the MCP server and the identity provider must allow the widget
  host `{hashed-mcp-domain}.widget-renderer.usercontent.microsoft.com`, where
  the hash is SHA-256 of the MCP server's domain; the page names no endpoints
  and no exact hash input or encoding (it points to a generator tool).
- **Custom federated connector** created by a tenant admin (Microsoft 365 admin
  center → Copilot → Connectors → Connect to MCP server): a base URL and an OAuth
  registration; users turn it on under Sources. Needs a Microsoft 365 Copilot
  licence. Whether MCP Apps render there is not documented. Its OAuth form lists
  a client ID **and a client secret** "from your OAuth provider", with PKCE
  optional; a public client without a secret is not described there.
- **OAuth.** The static path registers a client in the Teams Developer Portal
  with a client id (a secret is optional: PKCE is on by default and a public
  client is allowed), and a base URL that must equal the MCP server URL; the
  registration returns a reference id the plugin manifest names. The redirect
  URI is `https://teams.microsoft.com/api/platform/v1.0/oAuthRedirect` for every
  plugin. Dynamic client registration is supported only if it issues a client
  secret. Client ID Metadata Documents are not mentioned anywhere.
- **Distribution.** A package is uploaded by an admin (Agents → Registry) or
  side-loaded by a developer; it is scoped to users. Each self-hosted Flow
  Steward has its own address, so each customer builds and uploads its own
  package or connector: there is no single store listing.
- **Mobile** is not documented for MCP plugins or connectors, and custom plugins
  are documented as unsupported in Cowork on mobile.

Flow Steward today: inbound OAuth (ADR 0047) accepts only public clients whose
`client_id` is a CIMD URL on `chatgpt.com` or `claude.ai`, requires `resource`,
PKCE S256 and exact redirect URIs. The AI agents page lists local clients
(PAT) and two cloud clients (OAuth). Microsoft cannot sign in today.

## Decision

### 1. Microsoft 365 Copilot is one cloud client with two setups

The AI agents page gains **Microsoft 365 Copilot (Cloud)**:

- **Recommended: agent package.** The page builds, in the browser like the other
  packages, a declarative agent package for this installation: app manifest,
  declarative agent (Operator instructions within the 8,000-character limit,
  conversation starters, and the Operator skills while the skills preview
  allows), and an MCP plugin pointing at `{mcp_url}` with the admin's OAuth
  reference id. The admin uploads it in the Microsoft 365 admin center. Reports
  render as MCP Apps there (ADR 0059 viewer).
- **Alternative, verified separately: admin connector.** A custom federated
  connector with this installation's MCP address. Needs a Copilot licence and
  shows Copilot's own text answer; a report widget there is unverified. It is
  **not** promised to work with the public platform client of decision 2:
  Microsoft documents the connector's OAuth form with a client secret. The page
  offers it as something to try, and the tenant check decides. If it needs a
  confidential client, issuing, storing and rotating a client secret is
  separate work under its own decision; this ADR issues no secret.

The setup is a chain across two systems, and the AI agents page shows every
link of it:

1. A Flow Steward administrator creates the Microsoft 365 platform client.
2. A Microsoft 365 administrator registers the OAuth connection in the Teams
   Developer Portal with the values the page shows, and gets back the OAuth
   client registration ID (`reference_id`).
3. That ID is entered on the page, which builds the agent package with it.
4. The Microsoft 365 administrator uploads the package and makes the agent
   available to the people who need it.

Making the agent available in Microsoft 365 grants nothing in Flow Steward.
Each person still signs in to Flow Steward, approves the access on the consent
page, and gets at most what their own Flow Steward role allows (ADR 0047).

### 2. Admin-registered public OAuth clients (amends ADR 0047 decision 5)

A Flow Steward admin can register a **platform client** from a fixed preset.
The first preset is Microsoft 365:

- `client_id`: a random opaque identifier generated by Flow Steward (not a URL),
  shown for copying into the Teams Developer Portal.
- Redirect URIs come only from the preset:
  `https://teams.microsoft.com/api/platform/v1.0/oAuthRedirect`. An admin cannot
  type a redirect URI.
- Public client, `token_endpoint_auth_method=none`, PKCE S256 required; no client
  secret is issued. The consent page, scopes, grants, refresh rotation,
  revocation and audit are exactly ADR 0047's.
- Registrations are per installation (account-scoped where accounts apply),
  listed, and revocable; revoking one revokes its grants.
- Changes to an account's registrations are serialized by the account's
  transaction-scoped advisory lock (the existing `pg_advisory_xact_lock` pattern),
  which needs no settings row to exist. Code exchange and refresh for a platform
  client share that lock, taken before any grant row lock: a revocation either
  finishes before a token is issued, which then fails, or waits for it and
  revokes the new grant with the others.
- CIMD stays the only path for ChatGPT and Claude.ai; dynamic client
  registration stays off.

### 3. A missing `resource` from a platform client binds to this installation

ADR 0047 requires `resource`. As a narrow policy for an admin-registered
platform client only, a missing `resource` in the authorization request binds
the grant to this installation's MCP resource (whether Microsoft omits it is
unverified). A `resource` that is sent must match exactly; anything else is
`invalid_target`. The grant stores this installation's resource, and code
exchange and refresh keep that binding: a different `resource` sent to the
token endpoint is refused, and an omitted one leaves the stored binding as it
is. CIMD clients keep the ADR 0047 rule unchanged.

### 4. Widget-host CORS: verify first, then allow one exact origin if needed

Microsoft asks MCP servers that return MCP Apps to allow the widget host
`{hashed-mcp-domain}.widget-renderer.usercontent.microsoft.com`. Our viewer
(ADR 0059) fetches nothing itself, but that does not show what Microsoft's host
does from that origin, so no CORS change is claimed to be unnecessary.

The tenant check records which requests reach the MCP endpoint and the OAuth
endpoints with that `Origin`, and whether any fails for CORS. If one does, the
fix is a single allowed origin, computed from this installation's own MCP
domain the way Microsoft's generator computes it and confirmed against it,
on exactly the endpoints that need it. No wildcard
`*.widget-renderer.usercontent.microsoft.com`, no general CORS opening, and
no allowance before the tenant shows it is needed.

### 5. GitHub Copilot is a separate local client

For developers, **GitHub Copilot (On your computer)** covers VS Code agent mode
and Copilot CLI with the existing personal-token setup: an `mcp.json` entry
(VS Code) or `copilot mcp add` (CLI), and the Operator skills as an agent plugin
(both accept the Claude plugin format). No server change. MCP Apps in VS Code
are experimental and not claimed.

## Verification before any entry is marked verified

- A Microsoft 365 business tenant with admin rights, Teams custom app upload,
  and, for the connector path, a Copilot licence.
- Sign-in through the platform client, a workflow run from chat, a report
  rendered inline, revocation, and the text-only answer for a client without
  the widget. Mobile is tested separately and stays unverified until then.
- Whether Microsoft sends `resource`; which requests carry the widget-host
  `Origin` and whether any fails for CORS (decision 4).
- Whether the connector accepts the public platform client without a secret
  (decision 1); until then the connector is not a supported path.
- Code, OAuth regressions and the package structure are tested without a
  tenant. A free E5 sandbox is only for eligible Developer Program members;
  Microsoft names Business Basic as a possible Copilot Chat development
  environment with limited capabilities. No paid Copilot licence is bought for
  this check.

## Consequences

- One new kind of OAuth client record and its admin UI; the token, grant and
  consent machinery of ADR 0047 is reused unchanged.
- A possible later CORS allowance for one computed origin (decision 4) and a
  possible later confidential client for the connector (decision 1), each only
  if the tenant check shows it is needed.
- Each customer's admin does a one-time setup in two places (Flow Steward and
  Microsoft); the page guides both and fills every value it can.
- A Microsoft change to the redirect URI or auth options needs a preset update.

## Alternatives considered

- **Dynamic client registration** (Microsoft's DCR path, which needs it to
  issue a client secret): not added in this version, because a static
  registration covers the scenario and is simpler to operate: no registration
  endpoint, no secret issuance and no client lifecycle to maintain. RFC 7591
  allows protected registration (an initial access token), so DCR is not
  inherently open to the internet; it may be revisited if static registration
  stops being enough.
- **Typing any redirect URI in the admin UI:** makes the admin a phishing target;
  presets only.
- **Federated connector only:** loses the report widget and needs a Copilot
  licence for every user.
- **A single store listing:** impossible while each installation has its own
  address.

## Owner answers (2026-11-06)

1. Agent package recommended, connector as the alternative: yes.
2. Platform clients as an admin feature rather than DCR: yes.
3. Verification: a customer's Microsoft 365 tenant, the week after; until then
   the entry is marked not verified.
4. GitHub Copilot: not in this change.

## Sources

- https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/build-mcp-plugins
- https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/overview-plugins
- https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/plugin-mcp-apps
- https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/plugin-authentication-oauth
- https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/plugin-authentication-dynamic-client-registration
- https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/declarative-agent-skills
- https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/prerequisites
- https://learn.microsoft.com/en-us/microsoft-365/copilot/connectors/set-up-custom-federated-connectors
- https://learn.microsoft.com/en-us/microsoft-365/admin/manage/manage-plugins-skills-mcp-servers
- https://learn.microsoft.com/en-us/microsoft-365/copilot/cowork/cowork-plugin-development
- https://learn.microsoft.com/en-us/office/developer-program/microsoft-365-developer-program
- https://www.rfc-editor.org/rfc/rfc7591.html#appendix-A.1.2
- https://code.visualstudio.com/docs/copilot/customization/agent-plugins
- https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers
