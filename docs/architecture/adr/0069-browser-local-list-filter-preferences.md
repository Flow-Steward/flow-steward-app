# ADR 0069: Browser-Local List Filter Preferences

- Status: Accepted
- Date: 2026-11-14

## Context

Returning to a list resets its filters, forcing users with many projects,
workflows or jobs to repeat the same selection. The same behavior affects other
product lists. The requested preference belongs in this browser and does not
require a server-side settings store.

## Decision

Shared React hooks under `assets/frontend/src/lib/filters/` restore validated
filter preferences from `localStorage` before the first filtered request. Keys
include a format version, authenticated account, user and page/entity scope.
Unauthenticated views do not share a storage bucket. Each owner declares its
schema or query-parameter allowlist.

Explicit filter parameters in a URL specify the complete selection and take
precedence over stored preferences. Other URL parameters, history state and
hashes are preserved. Clearing filters saves the cleared selection; browser
Back does not reapply an earlier initial restore. A live account/scope change
loads that identity's preferences and ignores late updates from an old scope.

Remember list searches, facets and relative period presets. Do not persist
pagination, row selections, dialogs, actions, credentials or editor drafts.
Relative presets remain relative; custom date intervals retain their dates.

## Consequences

- Preferences survive navigation and reload in the same browser, independently
  for each user, account and entity scope; they do not synchronize across devices.
- Missing, corrupt, incompatible or blocked storage falls back to normal live
  filtering without preventing the page from working.
- Separate tabs keep their current filters independently. Their last completed
  preference write determines the selection restored on the next mount.
- Hook and owning-page tests cover restoration before requests, URL precedence,
  reset, identity isolation and storage failure.
