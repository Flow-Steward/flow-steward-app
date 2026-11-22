# ADR (number pending): Per-connection notification capabilities, an explicit system email sender, and borrowed connection credentials

Status: Proposed for 0.9.24. The number is assigned by the release coordinator;
add the file to the ADR index under that number.

Date: 2026-11-22

## Context

A notification provider declared one target kind for the whole extension
(`external_effects[].observability.target_kind`), and the build rejected effects
that disagreed. An extension that emails people and also posts to channels could
not exist. Notification delivery passed an OAuth connection only its stored
config and secrets, never a valid access token, and computed an effect
idempotency key without passing it to the extension.

System email took the first enabled connection, in any project, of any provider
whose extension-level kind was `recipient`. With several providers, connections
or projects the sender was accidental, and with per-connection kinds it could be
a connection that cannot send mail. Nothing stopped a project administrator from
repointing the sender by editing that connection, and the delivery reloaded the
connection after the sender was chosen.

## Decision

1. **Capability per connection type.** A connection type may declare
   `notification: {target_kind, ledger_channel, channel_source}`. The single
   `deliver_notification` effect stays; its `channel` and `target_kind` are the
   defaults. Once one type declares a block, only declared types carry routes or
   send system email. A provider whose types disagree is *mixed*.
   `channel_source: connection` marks a channel type bound to one channel by its
   own configuration (a display-only `channel_label`, no `channel_id` on routes).
2. **Routes name their connection when it matters.** A route of a mixed provider,
   or of one with a connection-bound type, must carry `connection_id`
   (`notification_connection_required`); the platform never falls back to the
   project's runtime connection for it. The connection must be enabled, in the
   route's project and the provider's install, of a declared type, and the
   route's fields must fit the type (`notification_connection_incompatible`).
   Checked on save and before every delivery; a refusal at delivery is not
   retried. The effect claim records the type's ledger channel and target kind,
   and standard email formatting follows the connection's kind.
3. **One credential resolver.** Delivery resolves an OAuth connection's access
   token with the same function workflow operations use
   (`resolve_connection_oauth_secrets`). A revoked grant fails the delivery with
   `authorization_required`, not retried.
4. **The effect key in the request.** The local delivery request carries the
   claimed effect's `idempotency_key` and `external_effect_id` at the top level.
   A future remote provider uses the remote envelope's `idempotency_key` with
   the same value. Workflow operations are unchanged.
5. **An explicit system email sender.** A platform administrator chooses one
   candidate connection in Settings -> Email (`system_email_connection_id` in
   `system_settings`). The setting stores the connection, its install and type,
   and a confirmation: the connection's `sender_revision` and the OAuth
   configuration's `configuration_revision`. Both are counters that only grow,
   moved by database triggers on every change of who sends (enabled in either
   direction, type, install, project, OAuth configuration or account, settings
   outside the OAuth lifecycle block, static secrets; for a connection without
   OAuth every credential revision). A token refresh does not move them. Any
   increment revokes the confirmation; there is no fallback. While a connection
   is the sender, only a platform administrator may change those fields, delete
   it, reconnect it or edit its OAuth configuration (`system_sender_locked`).
6. **Check, snapshot, admission.** A send reads the setting and the connection
   once, builds `target.connection` from that read, and passes it to delivery
   without a reload. At the admission point, after everything is built and before
   the effect claim and the extension call, it re-reads the setting and both
   counters; a difference fails the send with `system_sender_changed` and no
   effect.
7. **Upgrade pin.** An install that never wrote the setting is pinned once, at
   runtime, to what the earlier rule picked, with an audit entry. After that only
   the explicit setting applies.
8. **Sensitive payloads.** An event may carry `payload_sensitive: true`; system
   messages always do. Its subject and body reach only the delivery request;
   every kept copy (invocation log and audit, effect ledger fingerprint, domain
   event ledger, delivery rows, error text, platform logs) holds a placeholder
   with length and hash.

9. **Credentials from an OAuth configuration.** A non-OAuth connection type may
   declare `credentials_from`; its connections store
   `credentials_configuration_id` (a column of its own: `oauth_configuration_id`
   means "holds its own grant") and receive the mapped values from the
   configuration's current revision on every path that builds
   `target.connection`. OAuth credential fields may be `usage: runtime`. A
   deleted or incomplete configuration leaves the connection
   `credentials_unavailable`; a change of it revokes a system sender's
   confirmation.

## Consequences

- Existing single-kind providers behave as before: no block, optional
  connection, no field checks.
- A route stored before the change, for a provider that becomes mixed, fails
  delivery until it names a connection.
- An install that upgrades keeps sending system email through the connection
  the earlier rule chose, now explicit and locked; an administrator can change
  it in Settings -> Email.
- Remote notification providers remain unsupported.
