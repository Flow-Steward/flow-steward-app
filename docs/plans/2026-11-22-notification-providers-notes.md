# Notification providers: per-connection kinds, system email sender, delivery credentials

Generic Core capability for every `notification_provider` extension. Landed in
release batch 0.9.24. The guide for extension authors is the "Notification
Providers" section of `docs/extension-authoring-agent-playbook.md`; the decision
record is `docs/architecture/adr/draft-notification-connection-capabilities-and-system-email-sender.md`
(number pending).

## What changed

### Per-connection notification capability (C1)

- New `core/application/extensions/notification_capability.py`: a connection
  type's `notification: {target_kind, ledger_channel, channel_source}` block,
  provider-level kind (`recipient`, `channel` or `mixed`),
  `provider_requires_route_connection`, and the route field rules
  (`to` for recipient, `channel_id` for channel with `channel` as an alias,
  nothing for `channel_source: connection`).
- Build check (`compiled_manifest_policies._validate_connection_type_notification_blocks`):
  unknown kind, unknown keys, empty ledger channel, `channel_source: connection`
  on a recipient type, and a block on a non-notification extension are refused.
- New `core/application/notifications/route_connections.py`: the route check run
  on save (`update_notification_routes` -> `notification_connection_required` /
  `notification_connection_incompatible`, HTTP 400 with `code`, `route_key` and
  every refused route) and before each delivery.
- Delivery (`notifications/delivery.py`):
  - a refused route fails with `retryable: false`; dispatch records the attempt
    dead-lettered with the code as reason;
  - a provider that needs a named connection is never delivered through the
    project's runtime connection;
  - the effect claim takes the type's ledger channel and target kind
    (`claim_external_effect_for_action(declaration_overrides=...)`, and
    `source_ref.target_kind`/`ledger_channel`);
  - standard email formatting follows the connection's kind.
- Provider listing (`GET /api/projects/{id}/providers`): per connection
  `connection_type`, `connection_display_name`, `notification_eligible`,
  `target_kind`, `channel_source`, `channel_label`, `requires_connection`, and the
  provider's eligible `connections`; `notification_target_kind` is `mixed` for a
  mixed provider.
- Workflow automations (`reaction_runtime`, `extension_channel`) pass
  `connection_id`; an automation with only a connection (bound channel) is
  delivered. The routes overview no longer flags such a route as without a
  destination.

### Explicit system email sender (C2)

- Schema: `project_connections.sender_revision` and trigger
  `trg_project_connections_sender_revision`; trigger
  `trg_extension_oauth_configurations_revision` moves `configuration_revision`
  on identity edits a write path forgot to count. Migration
  `20261122140000_system_email_sender.sql`, ledger and fingerprint updated,
  schema contract requires the column.
- New `core/application/extensions/system_email_sender.py`: the setting
  (`system_email_connection_id` in `system_settings`), candidates, status,
  `select_sender`/`confirm_sender` (platform admin, audited), the one-time
  upgrade pin, the admission check (`system_sender_changed`), and the locks
  (`system_sender_locked`) used by connection update, secrets save, delete,
  OAuth reconnect and OAuth configuration save/delete.
- `system_email._send` reads once, builds the target from that read
  (`build_notification_connection_target`), and delivers with
  `prepared_target`, `admission_check` and `payload_sensitive=True`.
- API: `GET/PUT/DELETE /api/settings/system-email`,
  `POST /api/settings/system-email/confirm`. Connection views carry
  `system_email_sender` and `system_email_sender_locked`. UI page registry has
  the `email` settings tab.
- New `core/application/notifications/sensitive_payload.py`: placeholders for a
  flagged event's text in the invocation log, the effect fingerprint, the
  domain event ledger, delivery rows and attempts, error text and responses.

### Delivery credentials and the effect key (C3)

- `operation_targets.resolve_connection_oauth_secrets` is the one OAuth
  resolver; operations, background polls and delivery call it. A revoked grant
  fails delivery with `authorization_required`, not retried.
- `delivery_coordinator` puts `idempotency_key` and `external_effect_id` at the
  top level of the local delivery request (prepare and execute phases).
- C3.4: every delivery request carries `event.run_url`
  (`event_email.event_run_url`, the same link the standard email shows; the
  approval's own job for an approval) when the platform knows its public base
  URL and the event has a job. It is left out of the effect fingerprint, so a
  changed base URL never conflicts with an earlier claim.

### Credentials from an OAuth configuration (C5)

- Manifest: a non-OAuth connection type may declare
  `credentials_from: {oauth_provider_id, fields: {<field_id>: {into, as}}}`;
  checked at build time (`extension_contract_references._validate_connection_type_credentials_from`).
  OAuth `credential_fields` accept `usage: runtime` (never sent by the platform's
  sign-in, not needed for authorization; published only when set, so existing
  provider fingerprints do not change).
- Schema: `project_connections.credentials_configuration_id` (no foreign key, so a
  deleted configuration strands the link) with an index; the sender-revision
  trigger counts a link change. Migration
  `20261122150000_connection_credentials_from.sql`.
- New `core/application/extensions/connection_credentials.py`: resolves the mapped
  values from the configuration's current revision (secrets decrypted from their
  configuration refs) and merges them into `target.connection`. Wired into
  `operation_targets._direct_action_connection_target` (operations, resource
  queries, dispatch actions, background polls), notification delivery and
  connector steps (which now also use the shared OAuth resolver).
- Connection create requires the link for such a type
  (`credentials_configuration_required`), update can change it, and both refuse a
  missing, foreign or incomplete configuration (`credentials_unavailable`).
  Views carry `credentials_configuration_id` and `credentials_status`; an
  unavailable link shows `runtime_status: not_ready`.
- Configuration delete impact lists linked connections with
  `link: credentials`; they are kept. The system email sender confirmation
  tracks the borrowed configuration's revision.

### Frontend

- Route dialog and workflow automation editor follow the selected connection
  (`notification-route-target.ts`), Settings -> Email page, and the
  "Used as the system email sender" marker with read-only locked controls.

## Design choices

1. **`channel_id` is the route's channel field**, as every existing route,
   overview and automation already uses it; the task's `channel` is accepted as
   an alias. Extensions read `route_config.channel_id`.
2. **Admission before the claim.** The admission check runs after everything is
   built and right before the external-effect claim and the extension call, so
   a refused send leaves no effect row. Nothing is reloaded after it.
3. **Revisions by trigger.** `sender_revision` moves in the database on every
   write path; for an OAuth connection the token refs (`_oauth_*`) and the
   lifecycle block are excluded so a refresh does not move it; a reconnect is
   seen by the oauth `subject` and `authorization_intent_id`. For a connection
   without OAuth every credential revision counts (a catalog secret rotated
   under the same ref).
4. **Upgrade pin at runtime.** The earlier rule needs the installed manifests,
   so the pin happens on the first sender read (app load), inserted only if
   absent. A fresh install pins "none".
5. **The `payload_sensitive` flag is present only when true**, so fingerprints of
   existing events do not change.
6. **The C5 link has its own column**, not `oauth_configuration_id`: that column
   marks a connection holding its own grant, and the schema's OAuth metadata
   check, token refresh, disconnect and reauthorization paths all act on it.

## Tests (Tier 1, Docker `fs-audit`)

- `tests/integration/test_notification_providers_postgres.py`: a real extension
  process in a temporary bundle records every request; real connections,
  effects, triggers, settings and OAuth configurations.
  - C1: refused routes on save (required, wrong kind, another install, another
    project, undeclared type, channel to a bound connection); the same refusals
    at delivery without retry and without a call, a stored route without
    connection never sent through the runtime connection; one event two routes
    delivered, replay delivers nothing new, standard email on the recipient
    route only, separate keys and ledger channels; a bound-only provider; the
    listing fields; the full request key set.
  - C3: retry after a confirmed pre-effect failure carries the same key and
    effect id; a repeat of a success returns the stored result without a call;
    `event.run_url` for a job event and an approval, absent without a public
    base URL; refreshed OAuth token through the shared resolver; revoked grant
    `authorization_required`; own secrets still work.
  - C2: chosen connection used and audited; only a platform admin may choose;
    ineligible types and disabled installs are no candidates; every change
    (disable, delete, retype, other install, config, secret, reconnect as
    another account) makes system email unavailable with no fallback; project
    admin refused with `system_sender_locked` on config, type, disable, secrets,
    delete, OAuth reconnect and OAuth configuration edit, revision unchanged;
    platform admin change needs re-confirmation; OAuth configuration edited with
    the same id; disable then re-enable; token refresh keeps it available; the
    three check-to-admission races send nothing and record no effect; the
    extension receives the checked snapshot; the upgrade pin matches the old
    rule once; a fresh install sends nothing; no token in invocation logs,
    delivery rows, the ledger or debug logs for reset, welcome and email change.
- `tests/unit/test_notification_capability.py`: build check, capability rules,
  route field rules, listing view, redaction, top-level key, automations
  through a named connection.
- `tests/unit/test_system_email_via_extension.py`: rewritten for the explicit
  sender.
- Frontend tests: route form per target kind, automation editor, Settings ->
  Email, connection marker.

Not run: Tier 2/3 (`ui-i18n`, `frontend-lint` included), the full suite, Sonar.

## Follow-ups

- Assign the ADR number and add it to the ADR index.
- `.mo` catalogs are not recompiled (`ui_i18n_catalog compile` needs pybabel in
  the container); the hosted `ui-i18n` check covers it.
- Remote notification providers: carry the same key in the remote envelope's
  `idempotency_key`.
