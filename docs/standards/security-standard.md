# Security Standard

This document defines the secure-by-default baseline for the Flow Steward repository.

## Purpose

The goal of this standard is to protect users, operators, projects, credentials,
and runtime infrastructure across all Flow Steward surfaces.

## Security Surfaces

The canonical security surfaces are:

- web UI
- REST and JSON API
- WebSocket endpoints
- webhook and external ingress endpoints
- CLI commands
- MCP endpoints and tool execution
- extensions, connectors, and external providers

Rules:

- `MUST` treat every external input path as untrusted.
- `MUST` apply security controls at the boundary where data enters the system.
- `SHOULD` keep trust boundaries explicit in code and documentation.

## Authentication and Authorization

- `MUST` require authentication by default.
- `MUST` make public routes, health routes, bootstrap flows, and webhook entrypoints explicit and documented.
- `MUST` enforce authorization at the boundary and in sensitive service flows where applicable.
- `MUST` keep admin-only and write-capable paths clearly separated from read-only paths.
- `SHOULD` keep role and scope checks centralized and reusable.

### Shared Team Trust and Policy Boundaries

Per [ADR 0066](../architecture/adr/0066-global-roles-and-secret-provider-boundary.md):

- `MUST` use global human roles (`admin`, `operator`, `viewer`) for the current
  open-source deployment; projects and workspaces do not assign human roles.
- `MUST` preserve action permissions, account/resource ownership checks, token
  scopes, machine grants and execution-reference authority. A shared team trust
  domain does not grant unrestricted resource access or relax ADR 0020.
- `MUST` keep sensitive authorization application-owned, with authenticated actor,
  explicit operation and server-resolved resource context, so additional policies
  can narrow access without rewriting transports or workflow definitions.
- `MUST NOT` introduce per-secret human ACLs, project/workspace membership roles,
  commercial-edition terminology or licensing/upgrade placeholders in the current
  product. Separately supplied policies must preserve baseline security checks.

## Rate Limiting and Abuse Controls

- `MUST` apply rate limiting or equivalent abuse controls to login and password-reset flows.
- `MUST` review bootstrap, token-issuing, webhook, MCP, and other public or auth-sensitive ingress endpoints for abuse exposure when adding or changing them.
- `SHOULD` add explicit abuse controls to bootstrap, token-issuing, webhook, MCP, and similar ingress surfaces as those paths are hardened.
- `SHOULD` return `429` with a meaningful `Retry-After` contract where throttling is enforced.
- `SHOULD` keep counters scoped by the right unit for the surface, such as IP, principal, route, action, or project.
- `SHOULD` keep operator-tunable thresholds in config modules rather than embedding magic numbers in handlers.

## Input Validation

- `MUST` validate external inputs at transport boundaries.
- `MUST` use structured schemas or explicit validation paths instead of trusting raw payloads.
- `MUST` normalize and sanitize externally sourced identifiers, paths, and user-controlled text when required by the surface.
- `SHOULD` reject malformed or unexpected input early.
- `SHOULD` keep validation logic close to the boundary or in shared validation modules.

## Output Safety

- `MUST` avoid leaking raw exception details, stack traces, secrets, or internal-only metadata in user-facing responses.
- `MUST` preserve safe HTML rendering and escaping by default.
- `MUST` redact secrets and sensitive fields from logs, debug payloads, and diagnostics.
- `SHOULD` expose only the minimum output needed by the caller.
- `SHOULD` keep error responses stable and non-sensitive.

## Security Headers for HTML Surfaces

- `MUST` set baseline protection headers on HTML responses, including clickjacking, MIME-sniffing, and referrer-leakage controls appropriate to the surface.
- `SHOULD` ensure HTML pages include `X-Frame-Options` or an equivalent CSP framing rule.
- `SHOULD` ensure HTML pages include `X-Content-Type-Options: nosniff` and an explicit `Referrer-Policy`.
- `SHOULD` introduce `Content-Security-Policy`, `Strict-Transport-Security`, and related headers only with deliberate rollout planning when the current frontend behavior can support them safely.

## Secrets and Credentials

- `MUST` keep secrets out of source code, templates, fixtures, and committed examples.
- `MUST` load secrets from environment variables or approved secret storage paths.
- `MUST` treat tokens, DSNs, credentials, and signing material as sensitive.
- `SHOULD` separate secret values from non-secret feature toggles.
- `SHOULD` rotate or replace credentials rather than expanding workaround hacks around compromised naming or storage.
- `MUST` keep saved values write-only in UI and public APIs; resolve references
  only at authorized server-side execution boundaries.
- `MUST` put secret-provider selection and remote locators behind a host-owned
  storage adapter boundary. Workflows retain opaque local references; remote
  values must not be disguised as local ciphertext. See ADR 0066 for the
  integration contract and current implementation limits.
- `MUST` authorize before provider I/O and fail explicitly for unavailable or
  forbidden secrets, without implicit name-based or cross-provider fallback.
- `MUST NOT` let deletion of a local reference implicitly delete an externally
  owned secret or let step parameters select provider authentication/endpoints.

## Dependency and Supply-Chain Hygiene

- `MUST` run dependency and static analysis checks as part of the quality gate.
- `MUST` treat vulnerable dependencies and insecure defaults as release risks.
- `MUST` pin or constrain dependencies where operationally appropriate.
- `SHOULD` review new dependencies for maintenance, security posture, and blast radius before adoption.
- `SHOULD` keep optional tooling and overlays out of production defaults unless explicitly required.

## MCP, Tools, Extensions, and External Integrations

- `MUST` apply least privilege to tools, MCP connectors, extensions, and service accounts.
- `MUST` keep allowlists, auth, and scope checks explicit for external integrations.
- `MUST` treat extension and webhook payloads as untrusted input.
- `MUST` treat every extension the same way: no system / official / third-party tier and no host policy file naming extensions. Runtime grants come only from the extension's manifest `runtime_grants`, accepted by `own_runtime_grants` (scope `owning_extension`, provider key equal to the extension's own id); a declaration over another extension's namespace fails validation (2026-10-01).
- `MUST` validate or constrain tool execution inputs before execution on sensitive paths.
- `SHOULD` design new integrations as deny-by-default.
- `SHOULD` keep security-relevant integration behavior documented in standards or ADRs.

## Agent and LLM Safety

- `MUST` treat prompt-derived instructions and retrieved external content as untrusted until validated in context.
- `MUST` require explicit approval paths for destructive or high-impact actions.
- `MUST` avoid granting tool access broader than the task requires.
- `SHOULD` validate model outputs before they trigger privileged operations.
- `SHOULD` document trust boundaries when a model can influence execution, routing, or external side effects.

## Logging, Auditability, and Incident Support

- `MUST` log security-relevant failures, access decisions, and sensitive state transitions at the appropriate layer.
- `MUST` keep logs useful for incident review without exposing secrets.
- `SHOULD` preserve correlation identifiers for security investigations.
- `SHOULD` record operator-visible security decisions where that improves reviewability.

## Security Verification

- `MUST` add security-oriented verification for security-sensitive changes.
- `MUST` verify auth, authorization, validation, and output handling when changing those areas.
- `MUST` treat excluded critical security tests as debt that requires closure.
- `SHOULD` use the cheapest verification that proves the security contract reliably.
- `SHOULD` record significant security design decisions in ADRs.

## Review Expectations

- `MUST` reject hardcoded secrets, missing boundary validation, and raw exception leakage.
- `MUST` reject new privileged paths without explicit auth and authorization design.
- `SHOULD` flag overly broad tool or integration access as a design issue.
