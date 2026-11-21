# ADR 0078: Exact private origins for provider endpoints

- Status: Accepted
- Date: 2026-11-21
- Related: ADR 0018 (verified LLM routing and extension boundaries), issue #31

## Context

A self-hosted model server (an Ollama, LM Studio or vLLM on the Docker host or the
local network) is a private `http://` endpoint. Until now the only way to let an LLM
provider extension reach one was `FS_ALLOW_PRIVATE_REMOTE_URLS=1`, which also relaxes
the private-address protection of extension runtime URLs, MCP connectors, callbacks
and other outbound requests for the whole installation. That is a heavy trade-off to
make for one known server, and hard to justify to a security-minded operator.

Core is provider-neutral: it cannot special-case a provider by name. The decision has
to be a generic, narrow knob of the existing URL-safety policy.

## Decision

1. A new host setting, `FS_ALLOWED_PRIVATE_ORIGINS`, lists exact origins,
   `scheme://host:port`, comma-separated, at most 16. It is empty by default, which
   allows none. It is operator-settable through the environment manifest, so
   `flow-steward config set` / `config apply` (and Compact's `./install.sh --setting`)
   set it like any other host setting.
2. Values are validated strictly when set and again when read. An entry needs
   `http` or `https`, a host and an explicit port; a path, query, fragment, user
   name, wildcard, a numeric-looking host name, or an IP literal that is not
   loopback / RFC 1918 / ULA (public, link-local, metadata, CGNAT, mapped,
   unspecified) is refused by `config set`. A stored value that does not parse
   allows nothing (fail closed) and is logged.
3. The allowlist applies to LLM provider endpoints, the only place where Core hands
   a network decision to an extension:
   - saving a provider Base URL (`assert_safe_provider_url`) treats a URL whose
     canonical origin (default port filled in) equals an entry as if private
     addresses were allowed, for that URL only;
   - the provider subprocess receives the list as
     `runtime_context.network_policy.allowed_private_origins`, next to the existing
     `allow_private_addresses`. The extension's own guard admits private addresses
     only for a base URL whose origin is listed.
4. What stays blocked for an allowed origin is exactly what stays blocked with
   `FS_ALLOW_PRIVATE_REMOTE_URLS=1`: link-local and metadata addresses (including
   any DNS answer among several), CGNAT, multicast, reserved and unspecified
   addresses; cleartext `http` unless every DNS answer is private. The origin is
   compared as written: another port, the other scheme (no https-to-http downgrade
   and no implicit upgrade), another host name for the same address, or a redirect
   target elsewhere is not covered. The check resolves once; extensions pin the
   connection to the checked address and do not follow redirects, as before.
5. A refused private provider URL now names the narrow setting with the refused
   origin (`... adding http://10.0.0.5:11434 to FS_ALLOWED_PRIVATE_ORIGINS`) instead of
   the installation-wide switch. Other refusals keep their existing hint.
6. `FS_ALLOW_PRIVATE_REMOTE_URLS` keeps its meaning. Core's own model-provider
   records (the OpenAI-compatible provider type), MCP connectors, extension runtime
   URLs and the extension SDK HTTP helper are not covered by this ADR; extending the
   allowlist to them is a separate decision.

## Consequences

- An operator can connect one local model server without switching off the
  private-address protection for anything else.
- An extension that does not read `allowed_private_origins` keeps refusing private
  hosts; a provider has to ship support for it (the Ollama provider does from 1.2.5).
- The allowlist is installation-wide, not per account: every LLM provider extension
  may reach the listed origins, which is the operator's explicit choice.
