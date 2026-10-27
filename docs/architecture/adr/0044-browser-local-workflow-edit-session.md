# ADR 0044: Browser-Local Workflow Edit Session and One Whole-Content Writer

- Status: Accepted
- Date: 2026-10-22
- Amends: [ADR 0033](0033-workflow-authoring-application-boundary.md) (one definition writer)

## Context

The builder kept an owner-local Undo journal of unapplied graph intents. It
ended at every successful Apply and at every reload, only canvas intents were
undoable (a form, palette or hook staging was not), and the keys reached the
journal only while the canvas region had the focus. A newer server revision was
noticed only when a dry run or Apply answered `409`, and the only way on was
"Reload", which replays the unapplied set on top of the newer definition.

The product decisions: no Undo/Redo buttons; Ctrl/⌘+Z, Ctrl/⌘+Y and
Ctrl/⌘+Shift+Z by physical key; text fields keep their own Undo; Undo never
writes; own Apply and reload keep the history; nothing is merged automatically;
a conflict is resolved by an explicit, reviewed choice.

## Decision

**The session lives in the browser.** One edit session per tab, account, user
and workflow: the Undo/Redo history (actions with their staged operations and,
once on the server, their reverse; the cursor; the unapplied set), the last
confirmed server checkpoint (revision and the author's content) and unsaved form
drafts. It is written to IndexedDB as one transaction after every completed
action; sessionStorage keeps only this tab's reference to its session id. A
duplicated tab forks the record before its first write (Web Locks tell it the
session already has an owner). The server stores no session.

**The content stored is public authoring content only**: version, display name,
description, tags, builder metadata, trigger, settings without canvas notes and
without input secret and file authority, and the whole definition. A credential
typed into a form is never stored; the draft keeps only the fact that it must be
entered again. Sign-out deletes the database and the references.

**History survives Apply.** Apply still sends the unapplied set through
`POST /changes`. After it, each applied action learns its reverse (computed from
the definitions before and after it), so a later Undo stages that reverse locally
and the next explicit Apply writes it. Dirty means the copy differs from the
checkpoint, not that the history is non-empty. An own one-step save of a step's
or group's settings is one action of the history too.

**A newer revision this page did not write holds the copy.** The canvas keeps
showing the copy on its checkpoint; the conflict dialog compares it three ways
(base → mine, base → server, mine ↔ server) and offers *Keep my version*,
*Accept the server version* and *Decide later*. No replay, no automatic retry.
A revision that changed nothing of the definition (enable, a setting) moves a
clean checkpoint and keeps the history. A successful history restore and an
accepted server version start a new local history.

**One whole-content writer.** `replace_workflow_content` and
`restore_workflow_snapshot` share `_replace_definition` in
`workflow_definition_service`: the locked row with CAS, the snapshot's author
fields, settings and definition over the current row (`restore_candidate`, which
keeps enabled, provenance, availability, notes and input authority), reference
access, the caller's ownership re-check and `_judge_and_write`. The answer is read
back inside the write's transaction. The stateless routes
`POST /api/workflows/{id}/edit-session/compare` (read policy, read-only) and
`POST /api/workflows/{id}/edit-session/apply` (write policy, structure-change
policy, `expected_revision`, optional `dry_run`) validate the content strictly
(`workflow_edit_session_contract`: unknown, service-owned and authority fields
are refused) and project every value with the same authority and credential
redaction as both history diff routes.

## Consequences

- Recovery after a crash or reload depends on the browser keeping IndexedDB; a
  failed or unavailable write is shown, never reported as saved, and no server
  write replaces it.
- The browser's `beforeunload` prompt is a warning only; nothing is written or
  removed on unload. Leaving the workflow normally ends the session; *Leave and
  discard* ends a dirty one only when the navigation happens.
- `Reload workflow` (replaying the unapplied set onto the newer revision) remains
  for a `409` without a held copy; it is not offered while the copy is held.
- A real browser run on the deployed candidate is the evidence for reload,
  duplicate tabs and native ⌘Z; jsdom tests cover the logic only.
