---
name: flow-steward-workflow-authoring
description: Use when a Flow Steward user wants to build, change, enable, run or repair an automation (workflows, agents, skills) through the configured Flow Steward MCP connection, or asks about runs, errors, pending approvals or operational reports.
---

# Flow Steward Automations

You work through the user's configured, authenticated Flow Steward MCP
connection. Everything you need for ordinary work is in this skill, its
`references/` files and the live MCP contracts; no checkout, terminal or
documentation search is required.

- The server owns permissions, contracts and runtime behavior. `tools/list`
  shows the tools this token may call and their current arguments; the schema
  tools (`workflow_authoring_schema`, `agent_authoring_schema`,
  `skill_authoring_schema`) return the current request bodies. Read them
  instead of guessing fields.
- A missing tool means this token lacks the scope or the server lacks the
  operation. Say which operation is unavailable; never ask for another token,
  change credentials, install a server or reach the API another way.
- Failures stay failures. A refused read is not an empty result; a refused
  write did not happen.

## Limits you state instead of working around

| Missing | What to say and do |
| --- | --- |
| No Flow Steward MCP in this client | You cannot read or change anything in Flow Steward. Ask the user to connect the MCP server as their installation's AI-agent setup describes, then continue. |
| Write tools absent from `tools/list` | Creating, changing and enabling agents, skills and workflows needs an administrator user and a token with `admin:all`. Report it; continue with what the token can read. |
| An integration the automation needs | See "Missing integrations" below. A skill is never a substitute. |
| A platform defect | Collect the facts and use the `flow-steward-report-issue` skill. Changing Flow Steward code is not a fix you offer; a workaround inside the automation is. |
| GitHub access | Only issue reports and catalog publishing need it; those skills say what to do without it. |

Never enable, run, approve, cancel, restart, publish or install anything the
user did not ask for. Asking to build an automation is not asking to run it.

## Build an automation

Agree on the outcome first: trigger, inputs, outputs, which systems it reads
or changes, and who approves what. Then work in this order.

### 1. Discover what exists

1. `project_list`; keep the exact `project_name` the tools take.
2. `workflow_examples_search(project_name, query)`: a workflow that already
   does the job is reused, not duplicated. Inspect candidates with
   `capability_describe(project_name, "workflow:<id>")` or
   `workflow_definition(workflow_id)`.
3. `capability_search(project_name, query, kinds)` for tools, agents, skills
   and workflows; `capability_describe` for one component's live operation
   contract and, for a tool, its required connection types and the project's
   connections with their readiness and `setup_url`. Pick an `operation_id`
   for multi-operation tools. Report knowledge freshness when it is returned.
4. `agent_list(search)` and `skill_list(search)` list registry objects,
   disabled ones included; `capability_search` hides disabled objects. Reuse
   a fitting agent or skill instead of creating a near-duplicate.
5. `model_options` lists the providers and models the account may select.
   Leave `config.provider`/`config.model` empty to inherit the project route
   unless the user asked for a specific model.

### 2. Missing integrations

An external system (a shop, a mailbox, a CRM, a file store) is reached through
a tool that an installed extension provides and a ready project connection.

- No matching tool: the integration is missing. Say which system and which
  operation are needed, and that an administrator installs the extension
  (Extensions → Catalog in the UI) and creates its connection. Do not install
  anything, and do not promise that such an extension exists.
- A tool exists but no connection is ready: give the returned `setup_url` and
  what readiness reported. The user sets up the connection; you never handle
  its secrets.
- Never write a skill or an agent prompt that claims to call a system no tool
  reaches. A skill is instructions for an agent; it does not create an
  integration. Never put credentials, tokens or private URLs into a skill,
  prompt, bundle or input.

Stop at the blocker, show what is ready and what is missing, and continue
only with the user's decision.

### 3. Create skills and agents

Only when nothing existing fits. Fields, enablement and the conflict
procedure are in [references/agents-and-skills.md](references/agents-and-skills.md).

1. `skill_authoring_schema`, then `skill_create(skill)` with a stable
   `skill_id` and a readable `display_name`; it is saved disabled.
   `skill_inputs_set`/`skill_outputs_set` replace its whole contract lists;
   `skill_validate` reports readiness.
2. `agent_authoring_schema`, then `agent_create(agent)`; it is saved disabled.
   Assign skills in the create body or with `agent_skill_assign(agent_id,
   skill_id, skill_prompt)`; `agent_skill_set_instructions` replaces one
   assignment's instructions. `agent_validate` reports readiness.
3. A `registry_id_conflict` or `registry_name_conflict` changes nothing:
   follow the conflict procedure in the reference. Never rename or overwrite
   on your own.

A valid readiness result is a static check; it does not prove that a run
works.

### 4. Compose the workflow

1. `workflow_authoring_schema(step_kind)` returns the current bundle sample
   (`minimal_bundle_yaml`) and persisted step fields. Start from it or from a
   permitted export.
2. Author exactly one workflow in `platform.workflow_bundle.v2` with an
   explicit stable `workflow_id` and a readable `display_name`: its `steps` (a
   step in a loop or parallel group names its `group_id`, in a lanes body its
   `lane_id`), its `groups`, and one `execution_graph` with an `entry` and
   edges `source → outcome → target`. There are no phases. Route every outcome
   (`success`, `true`/`false`, each switch `case_id` plus `default`,
   `approved`/`rejected`) to exactly one target; end root paths at
   `stop_success` or `stop_fail`. A phase-based v1 bundle is refused, not
   converted.
3. Reference agents, skills, tools and child workflows by the exact ids you
   discovered or created. Only `use_existing`/`map_existing` resolutions are
   supported.

### 5. Validate: validation is not execution

`workflow_draft_validate(project_name, bundle_yaml, resolutions)` writes
nothing. Repair real structural, reference, mapping and contract issues and
validate again, keeping the same id. Read `can_save`, the issues and the
performed and unperformed checks. Tell the user plainly that validation does
not run the workflow, call providers or prove that external systems accept the
data. Missing dependencies are reported as such, never replaced with
placeholders.

### 6. Save a disabled draft

When the user wants it created and validation allows it, call
`workflow_draft_save` with the same bundle and resolutions. It creates one
disabled workflow and never overwrites one. Report the saved id,
`enabled=false`, the returned `editor_url` and the remaining issues, exactly as
returned. A timeout or unclear answer is handled under "Uncertain results".

### 7. Enable only on request

Enabling is a separate decision. When the user asks for it:

1. `workflow_definition(workflow_id)` for the current `revision`, then
   `workflow_set_enabled(workflow_id, true, expected_revision)`. A 400 lists
   the readiness issues; a 409 means the workflow changed, so read it again.
2. If readiness names a disabled agent or skill the workflow uses, enable it
   (`skill_set_enabled(skill_id, true)` before `agent_set_enabled(agent_id,
   true)`) and try again. Each is refused with its own readiness issues when
   it is not ready.

Enabling does not run anything.

### 8. Run and read the result

Only when the user asks to run it.

1. `workflow_contract(workflow_id, project_name)` gives the inputs. Ask for
   every missing required input; do not invent values.
2. `workflow_trigger(project_name, workflow_id, payload, files=...)`. File
   inputs carry `input_name`, `filename`, `mime_type` and `content_base64`,
   never server paths or URLs. `test_mode=true` asks extensions to suppress
   the external effects they declare as suppressible; it does not guarantee
   that nothing outside changes, so say so before a test run that touches
   real systems. The workflow must be enabled.
3. Follow the returned `job_id` with `job_status`; read `job_result` for the
   outcome and `job_errors`/`job_incident` on failure. Judge the outcome by the
   job's status, not by a generic completion flag. A pending approval waits for
   the human: see [references/runs-approvals-reports.md](references/runs-approvals-reports.md).

Report what actually ran: job id, status, outputs or errors, and what was not
verified.

## Change an existing workflow

`workflow_draft_save` never edits an existing workflow.

1. `workflow_definition(workflow_id)` (disabled workflows included): steps,
   groups, execution graph and `revision`.
2. `workflow_apply_changes(workflow_id, expected_revision, operations,
   dry_run=true)` with one atomic set of `create_step`, `create_group`,
   `update_step_config`, `change_step_kind`, `update_group_config`,
   `set_entry`, `connect`, `disconnect`, `insert_step_between`, `move_step`,
   `delete_step` and `delete_group`, using stable ids so later operations can
   name new nodes. The dry run shows the resulting issues and which of them
   would refuse the set.
3. Apply the same set without `dry_run`. It becomes one new revision or
   nothing is saved. `workflow_revision_conflict` (409) means someone changed
   the workflow: read the definition again and rebuild the set; never resend a
   stale set.
4. A moved step loses its edges and a deleted step leaves no bypass: reconnect
   in the same set. An enabled workflow refuses a result with blocking issues
   and keeps running its current revision; do not disable it to force a
   change unless the user asked. `workflow.change.reference_unavailable` means
   a named agent, child workflow, connection or install is not usable by the
   workflow's account; it does not say whether a foreign id exists.

Related changes:

- `workflow_convert_group` moves a group into a new child workflow. It
  previews by default; apply only with the current `preview_digest` and
  revision.
- Hooks: `workflow_hooks_list` and `workflow_hook_get` read a workflow's hooks
  and bindings; `workflow_hook_preview` runs every binding check without
  saving; `workflow_hook_bind`/`workflow_hook_unbind` save the next binding
  revision at `expected_revision`; `workflow_hook_create_and_bind` creates the
  empty workflow that holds a hook's steps.
- Before changing a workflow's inputs or outputs, `workflow_where_used` lists
  the Invoke steps and hook bindings that call it.

Agents and skills have no revision: the last write wins. Read the object right
before `agent_update`/`skill_update` and send only the fields you change.

## Read and repair disabled objects

- List them: `workflow_list(project_name, enabled_only=false,
  include_validation=true)`, `agent_list`, `skill_list` (disabled included by
  default).
- Find out why they are not ready: `workflow_validate(workflow_id,
  project_name)` and `workflow_suggestions` for a workflow, `agent_validate`,
  `skill_validate`.
- `workflow_definition`, `agent_get` and `skill_get` read disabled objects.
  Discovery tools (`capability_search`, `capability_describe`) may not show
  them; that is not proof that they do not exist.
- Repair with the change tools, validate again, and enable only when the user
  asks. Disabling is refused while enabled workflows depend on the object; the
  error names them.

## Uncertain results

A timeout, dropped connection or unreadable answer after a write means the
outcome is unknown.

- Read before retrying: `workflow_definition`/`workflow_list` for a
  workflow, `agent_get`/`skill_get` for registry objects.
- Retry a draft save only with the same id and an unchanged bundle. A
  `workflow_id_conflict` or a name conflict after an uncertain save may be your
  own earlier call: compare what you read with what you sent. If it matches,
  say that its origin is unconfirmed (you cannot prove that your call created
  it) and continue with it. If it differs, treat it as someone else's object.
- Seeing an object proves that it exists, not who created it or that its
  content matches. Only a confirmed result supports "created".
- Never switch to a new id automatically to get past a conflict. A new id is
  used only when the user asks for an additional object.

## Export and import

`workflow_export(project_name, workflow_id, include_children)` returns a
`platform.workflow_bundle.v2` YAML (`bundle_yaml`); use `include_children=true`
when it calls other workflows. `workflow_import(project_name, bundle_yaml,
target_workflow_id, resolutions)` creates disabled drafts and needs draft-save
authority. Exports omit secret values; still read one before it leaves the
installation. To share a workflow in the public catalog, use the
`flow-steward-publish-workflow` skill.

## Operational questions

Runs, failures, stuck jobs, approvals, cancel and restart, and reports:
[references/runs-approvals-reports.md](references/runs-approvals-reports.md).
