# ADR 0018: Verified LLM Routing and Extension Boundaries

## Status

Accepted.

## Context

Chat models are selected in project and agent settings, but a
provider listing alone cannot prove the tool-call, tool-result replay, and exact
structured-output behaviours required by workflow agents. Static catalog entries,
custom provider defaults, and extension discovery therefore need one routing
policy without moving built-in provider definitions into a database registry.

Remote extension runtime URLs and their upstream provider URLs also have distinct
trust boundaries. Conflating them risks leaking credentials to an arbitrary
endpoint or weakening the host's public-HTTPS/SSRF policy.

## Decision

1. Built-in allowlists remain core-owned code. A built-in model is selectable
   only when it is active and has passed the reusable release capability suite.
   Qwen, DeepSeek, and xAI direct routes are not exposed unless a concrete
   verified extension route supplies them.
2. `llm_provider_models` holds discovered account-scoped extension models and
   verification evidence, not a replacement universal registry. Only
   `enabled + passed + active` extension models enter `provider_models`,
   `all_models`, validation, and runtime resolution.
3. A custom `model_providers` row publishes only its chosen `default_model`
   after the same live verification. Credential, endpoint, model ID, or upstream
   revision/digest changes mark verification `stale` and remove the route until
   explicit re-verification.
4. The canonical extension wire preserves structured content, tool calls and
   IDs, tool-result context, cache-control blocks, bounded continuation metadata,
   exact JSON Schema, schema name, strict intent, normalized usage, and safe
   error categories. Host-side schema validation is mandatory; semantic repair
   cannot make a capability probe pass.
5. `remote_base_url` is the public HTTPS URL of the extension runtime and is
   required at install time and before every activation. A remote install with
   no configured runtime URL remains `disabled`; there is no implicit
   `active_unconfigured` state. `upstream_base_url` is account-scoped non-secret
   extension configuration and is separately URL-safe validated; upstream
   credentials remain account-scoped encrypted `secret_refs`.
6. Model routing may inherit an explicitly configured project/agent
   route, but never substitutes another provider, environment default, or first
   catalog model. An invalid, stale, or removed route returns typed
   `model_unavailable`.
7. LLM-provider operator requests use closed Pydantic transport schemas. The
   complete synchronous DB/crypto/DNS/remote-runtime use-case unit runs through
   an explicit threadpool boundary so async HTTP handlers never execute blocking
   provider work on the event loop.
8. The first-release schema is a hard clean baseline. Fresh initialization
   loads the complete canonical LLM DDL from `db/schema/`; the 1.0 release
   ships no historical migrations. Pre-release (pre-1.0) development databases
   are not upgradable: they are recreated from the 1.0 baseline, and a ledger
   that records pre-1.0 migrations is refused rather than adopted. Post-1.0
   schema changes are SQL files under `db/migrations/`, applied under the
   upgrade lock by `flow-steward database upgrade` per
   [ADR 0012](0012-post-1.0-sql-migrations.md). There is no legacy schema
   branch or compatibility registry.
9. Custom-provider verification is one-flight. A generation-bound pending
   claim over the exact provider, endpoint, credential reference/ciphertext,
   model, configuration, and upstream revision commits before external probes.
   Publication and cancellation require ownership of the same pending
   generation; a changed route clears the lease and remains stale.
10. Stored custom routing identity is the exact `custom:<provider-id>` plus
    persisted upstream model ID. The provider row supplies credentials only
    after both values match its verified snapshot; changing `default_model`
    never retargets an existing project or agent silently.
11. Public HTTPS safety is enforced both when configuration is saved and each
    time the host constructs a credential-bearing remote runtime client.
    Transport success requires an HTTP 2xx status and literal `ok: true`.
12. The product has no bundled CLI agent executors or executor-option catalog.
    Model options and agent configuration use the verified provider routes above.
    External developer clients may consume the portable skills described in
    [ADR 0033](0033-workflow-authoring-application-boundary.md); they are not
    product chat providers or runtime executors.
13. An account-scoped custom-provider or extension-model pass is a bounded
    snapshot, not permanent capability evidence. It expires after 30 days and
    is excluded from selectors and runtime resolution until the operator invokes
    the explicit verification flow. Routing freshness is evaluated against the
    database clock and rejects future timestamps. No scheduler or background
    monitoring is introduced. Operators may also supply an immutable upstream
    revision/digest; changing it invalidates the snapshot immediately.
14. Custom-provider runtime construction requires the decrypted account-scoped
    credential from its saved `secret_ref`. Ambient process-wide provider keys
    are never a fallback for an account-scoped route.

## Consequences

- Historical provider/model references are retained as data but are visibly
  unavailable and cannot execute until manually replaced. An invalid explicit
  project or agent route terminates before the request is processed; blank values
  remain reserved for intentional inheritance.
- Discovery can retain disabled/unverified snapshots for operator UI. Failed or
  partial refreshes do not delete the last complete successful snapshot.
- A single generic Extensions Hub LLM-provider panel drives credential save,
  discovery, selection, verification, and safe failure display for all manifest
  v2 `llm_provider` bundles.
- First-release LLM schema is part of the canonical `db/schema/` baseline.
  Post-1.0 changes follow ADR 0012; provider verification does not reset or
  reseed performance, embedding, vector, trace, or execution data.
- Safe discovery failures preserve their canonical category without replacing
  the last complete model snapshot. Capability gates require exact `basic-ok`,
  exact schemas, and the required tool replay; no semantic repair is used.
