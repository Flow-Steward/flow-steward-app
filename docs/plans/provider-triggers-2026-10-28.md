# Provider triggers, finished (2026-10-28)

Goal: an extension (Telegram first, WhatsApp next) declares events such as "New
Telegram message"; the workflow author binds one under Triggers; a real
incoming event starts that workflow with the event as input. Origin: item 13 of
`audit-backlog-2026-10-21.md`.

Owner decisions: do what n8n does, no reinvention; keep the change small and do
not rework existing code; **closed by default**: a message starts a workflow
only from a chat the operator allowed.

## What already existed (read from code)

- `workflow_triggers` in the extension manifest -> catalog
  (`workflow_trigger_catalog.py`); create/update/delete of a trigger row
  (`WorkflowProviderTriggerService`); the UI behind one hiding flag.
- An extension's callback (`/webhook/extensions/{id}/{callback}/{token}`) runs its
  `ingress_event` action and records the returned `events[]` through
  `ingest_provider_events` (project and provider key known from the registration).
- Starting a workflow from a trigger row existed only on the task-provider route
  (`/webhook/providers/{key}`).

## What this change does

1. **Core bridge** (`core/application/workflows/provider_event_trigger_dispatch.py`,
   one call in `ingest_provider_events`): after an event is recorded, start every
   enabled trigger of that provider and project whose `event_name` equals the
   event type exactly and whose scalar `trigger_config` values equal the payload
   fields. Payload fields become the workflow inputs. Same admission, input
   validation, replay protection (`provider_webhook_deliveries`) and
   `enqueue_workflow_start` as the task-provider route. A failing trigger never
   fails the event. Replay key = the id the provider gave the event
   (`source_id` or `correlation_id`); no id -> hash of the payload.
2. **UI**: the Provider triggers section on Triggers is shown when an installed
   extension declares events or a trigger is already bound; otherwise the tab
   shows only incoming webhooks, as before.
3. **Telegram extension** (separate repo, next): declare `workflow_triggers`,
   return `telegram.message.received` from `ingress_event` only for allowed
   chats (new channel capability `workflow_trigger`, default off), then the
   steps below.

## Telegram steps (n8n parity, trimmed)

Extend `send_message` (reply, buttons, silent); one `send_file` with `kind`
(photo, document, video, audio, voice, animation; 10 MB photo, 50 MB other);
`edit_message`; `delete_message` (messages younger than 48 h); pin/unpin;
`get_file` (20 MB). Not now: stickers, location, polls, media groups, chat
administration, automatic attachment download in the trigger.

## Left out on purpose

- Creating a trigger does not register the webhook at Telegram; the operator
  turns incoming messages on at the extension's Inbound page, as today. Doing it
  automatically needs a callback registration (secret token, public URL), a
  choice of connection and removal on delete: a separate task.
- No new manifest action, no schema change, no change to the task-provider route.
- A shared layer for WhatsApp is extracted after Telegram works, not before.

## Checks done

Core: `tests/unit/test_provider_event_trigger_dispatch.py` plus the existing
command-service, callback, webhook and event-ingress suites (594 passed);
frontend: the three trigger/settings suites (78 passed). Not yet: a live run with
a real bot, and the full gate.

## Open

- Bump `VERSION`; the extension requires that Core as its minimum.
- Two Telegram connections in one project: the event payload carries the
  connection fields so a trigger can filter on them.
