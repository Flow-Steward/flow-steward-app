# ADR 0075 Project UI sections built on workflow runs and history

Status: Proposed

Date: 2026-11-18

## Decision

The operator section in core uses the existing Jobs list and the standard
modal dialog for the run form. The admin publishes and configures the form
once in Workflow Settings. The section appears automatically in every accessible
project with an active binding of this workflow and shows its jobs in the context
of the selected project. Execution, idempotency, history and file storage
use existing mechanisms.

The admin keeps the current management interface. Operators see the shared
run history of the selected workflow in the project, within their current permissions.
This matches the team trust scope of [ADR 0066](0066-global-roles-and-secret-provider-boundary.md).
A personal-owner policy, an extension, new write routes, new file
storage and a separate durable mechanism for accepting a batch are out of scope for this feature.
Runs, approval decisions and saving settings use existing routes;
only three read routes are added (see [Read routes](#read-routes)).
Existing account, tenant, resource and token limits stay in place.

## Roles

The sections rely on a clear split of global roles, which this ADR
adds as an amendment to [ADR 0066](0066-global-roles-and-secret-provider-boundary.md#amendment-2026-11-18-proposed-in-adr-0075-admin-configures-operator-works-viewer-reads):

| Role | What it does |
|---|---|
| Admin | Manages users, extensions, projects, workflows, triggers, connections, bindings and secret approval. Publishes and configures the operator form in Workflow Settings |
| Operator | Does their work in sections: runs workflows, uploads files, reviews results and decides approvals, views history. Can enter or replace a secret value, for example a mail password, without changing its binding. Does not see the admin interface |
| Viewer | The same sections and history, read-only, with no actions. For example, an analyst or a manager |

Role permissions are the same in the UI, API and MCP. On a run, the server also checks
whether the specific channel is enabled for the workflow. A personal token acts with the role of its
owner. MCP makes it possible to work from any client, including a phone.

Connecting the accounting system, choosing a connection for a workflow and its
business logic are configuration and stay with the admin: they are done by the
specialist who configures the workflow and knows which API methods it calls.

## How the admin publishes a workflow

In Workflow Settings the admin does the following.

1. Selects run channels in `settings.callable.trigger_channels`. For example,
   `api` is enough for an API integration, and `webhook` for an incoming webhook.
   The operator form requires `ui` and an explicit "Show to operators" switch.
   Operator publishing is off by default. The admin confirms the
   workflow's real effects when publishing.
2. Optionally sets the menu item name and configures the input form:
   widget, label, hint, visibility and display conditions of each field
   (see [Form, result and list settings](#form-result-and-list-settings)).
   Without configuration, the form is built from the input contract automatically.
3. Chooses For each or Bulk. For For each, sets the field to iterate over. The other
   form values are repeated in every run.
4. Configures the result display and the run list: columns from run fields,
   inputs and outputs, and filters on those fields.

Human review between steps is not configured here but in the workflow itself
in Builder: an Approval step with fields (see [Human review](#human-review-approval-with-fields)).

Operator publishing, the form and result presentation, the submission mode,
columns and filters are stored in the workflow definition's settings JSON next to
`settings.callable`.
The admin saves them through the existing workflow update. One workflow
has one such configuration. All of its project bindings use
the same form and columns. The source of bindings is `project_workflows`.
Storing settings in the existing JSON avoids a schema migration.

Turning off publishing or the `ui` channel removes the item from all projects. Deleting
or disabling a binding removes it from the corresponding project. Previously accepted
jobs remain available through the existing history, within the user's permissions.
If a published workflow is temporarily disabled or its contract is incompatible
with the form settings, a section with an active binding and the `ui` channel shows
the history and the reason submission is blocked.

## Form, result and list settings

The model is the ACF (Advanced Custom Fields) field editor: a list of fields, each with
expandable settings on the General / Validation / Presentation /
Conditional logic tabs, a type picker dialog and a repeater with rows of subfields. The difference
is that fields and their data types are already defined by the workflow contract. The admin
does not add or remove fields in this editor but chooses how to show each
field. The set of types is smaller than in a CMS: there are no WordPress entities
(posts, taxonomies, users, links) here.

Workflow Settings gets a "For operators" tab with the sub-tabs
"Form", "Result", "List" and "Publication".

### Form: input fields

The list follows `expected_inputs` in contract order; the form order
is changed by drag and drop. Each row shows: label, key and data type from the contract
(read-only), visibility, widget, and use in columns and filters.

Each field is configured as follows:

| Tab | What the admin sets | Where the rest comes from |
|---|---|---|
| General | A widget compatible with the data type, the label for the operator, visibility | Key, type, required flag and `options` come from the contract |
| Validation | Nothing | Required flag, constraints, file types and sizes come from the contract and the current attachment limits |
| Presentation | Hint, placeholder, prepend/append, width, option labels; for a repeater, the Table / Block / Row layout and the add button text | — |
| Conditional logic | "Show the field if [field] [operator] [value]"; conditions are joined with "and" within a group, rule groups with "or" | Operators depend on the field type, as in ACF |

A widget can be chosen only from those compatible with the contract data type:

| Contract type | Widgets |
|---|---|
| `string` | Text, Text Area, WYSIWYG, Email, URL, Date, Date Time, Time |
| `select` (`options`) | Select, Radio, Button Group |
| array of `select` values | Checkbox, multi-value Select |
| `number` | Number |
| `boolean` | True / False |
| `file` | single file upload |
| array of `file` | multiple file upload (for example, 4 pages of one contract) |
| `object` with described properties | Group |
| array of `object` with described properties | Repeater |

A file field is always shown as a file upload, not as a text
field: whether it takes one file or several is decided by the contract, not the form. `secret`
is not shown in the operator form: secret values are entered through the
secret catalog.

Allowed choice values are stored in the contract (`options`), so the server
validates them the same way in the UI, API and MCP. The form only sets option
labels. If the admin shows a string as a Select, the options
are written into that field's contract.

**Hidden field.** The admin makes a technical parameter the operator does not need
to know about hidden. Its value is taken from the field's `default` in the contract.
A hidden required field without a default value is a configuration error;
publishing is blocked while it exists. The operator cannot pass a hidden field
through the form. The API and MCP work by the contract, as they do now.

**Repeater.** Each row is a line of subfield inputs, for example "Name",
"Website", "Email". A button adds a new row; each row has a delete control next to it.
The minimum and maximum number of rows come from the contract.

**Display conditions** apply only in the form. A field hidden by a condition is not
submitted. A field with a condition cannot be required in the contract:
"required when shown" is checked by the form.

### Result: output fields

The same list for `expected_outputs` and returned files, but without validation
and conditions. The admin sets the label, order and display kind (text, number
with a format, date, WYSIWYG as formatted text, files as download
links, repeater as a table). The run page shows the submitted inputs
and the received result according to these settings.

### List: columns and filters

**Columns.** The admin adds a column by choosing a field from three
sources: run fields (date, status, author), inputs and outputs. For each column
the admin sets the header, format and order (by drag and drop). Files are shown as
names with download links.

**Filters.** The admin adds a filter by choosing a field and a filter type
from those compatible with the data type:

| Data | Filter type |
|---|---|
| date, date and time | Date range |
| `number` | Number range |
| `boolean` | Yes / no |
| `select` and array of `select` | Choice of one or more of the field's `options` |
| `string` | Text search |
| job status | Status choice |
| files | not available |

Choice values come from the contract; loading values dynamically from
history is not in the first version.

A filter compares full field values, not the display projection.
`meta.invocation.mapped_inputs` truncates strings longer than 2 KB and arrays longer than
25 items, so a match beyond the truncation limit would be missed. When it
accepts a run, the server writes into the job's `meta` the full values of inputs that
can be filtered: strings, numbers, booleans, `select`, `select` arrays, dates.
It writes all such inputs of the contract, not only the configured filters, so
a filter added later also works on old runs. Secrets and files are not
written. Outputs are already stored in full in `meta.workflow_outputs`.

## Human review: Approval with fields

Work with a document often needs a human check between steps: recognize
an invoice, check and correct the recognized data, export it to the accounting system.
Correcting an amount by hand is faster than waiting for another recognition, which
can be wrong again. The existing Approval step does this, given
typed inputs and outputs like any other step. The whole process is
one workflow and one job: recognize → review → export through the
existing `invoke_workflow`.

Today Approval shows the human `show_outputs` joined into one line of
text and accepts an approve / reject decision with a comment. The rest already
exists: the `approved` / `rejected` branches, decisions in the UI and MCP, notifications, decision
history with the author, refusal of a second decision (`approval_already_resolved`) and
job continuation after the decision.

### Review fields

The admin adds review fields to the Approval step in Builder. A field is the same model
as a workflow contract field: key, label, type (`string`, `paragraph`,
`number`, `boolean`, `select`, date, arrays, `object`), required flag,
options. There is no separate type system.

- **The initial value** is set by an ordinary mapping from the outputs of previous
  steps, for example `Recognize.total → total`. Builder suggests matches
  by name and type; the admin confirms.
- **"Take fields from a workflow"** fills the field list with the inputs of the selected
  workflow — for example, the one called next for the export — together
  with types, required flags and options. Types are not described by hand.
- **Editability** is set per field; the operator only sees the rest.
- **The original** is an optional file input or a file from a previous step,
  shown on the left for comparison.

If the source type is unknown, for example a free-form agent answer, the field
gets the `paragraph` type. Approval without fields works as it does now.

### Display: widget by type

The admin does not have to configure the display. Each type has a default widget:

| Type | Default widget |
|---|---|
| `string` | Text field |
| `paragraph` | Text Area |
| `number` | Number field |
| `boolean` | Toggle |
| `select` | Dropdown |
| date, date and time | Date picker |
| array of `select` | Multi-select |
| array of `object` | Table of rows, like a repeater |
| `file`, array of `file` | Links and preview, view only |

The widgets are the same as in the operator run form (`WorkflowInputFields`).
Choosing another widget, option labels and display conditions for review fields
are not in the first version; later the same field editor as
the run form adds them, without changing the model.

### Decision and step outputs

When a review opens, the server stores the fields' initial values in full
and typed, together with the approval. The `content` text limit
(1000 characters) does not apply to them.

- **Approve.** The approval decision accepts `values` — edits of editable
  fields only. The server applies them to the stored values,
  validates the result by type and required flag and returns it as the step's typed
  outputs: `Approval.total`, `Approval.lines`. Later steps map them
  as usual, for example `Approval.total → Export.total`. The difference from
  the initial values is written to the decision history: what the application
  recognized, what the human corrected, and who.
- **Reject.** The comment is saved and becomes the output of the `rejected` branch.
  If the branch leads to another recognition, the comment is passed there
  as feedback.

The original values stay in the recognition step's outputs. If the accountant
corrects "31,85" to "318,50", 318.50 goes to the export, and the recognized
value is kept in history.

A second decision on the same review gets the existing refusal. Permissions are
the existing approval decision permissions: the operator decides, the viewer only sees.

**Who can submit edits.** Today tokens can decide approvals,
but `override_content` is forbidden to them. For `values` the rule is:

| Who | Decision without edits | Decision with `values` |
|---|---|---|
| A human in the UI (session) | yes | yes |
| A human's personal token (API, MCP) | yes | yes, with the token owner's role |
| Machine token (service account, `is_machine_principal`) | yes | no, 422 refusal |

A personal token acts on behalf of a human, and review from a phone through MCP
must be supported. A machine token is automation: it can approve but
cannot replace data that a human reviews. Both cases are pinned by
tests.

**Snapshot at open time.** Together with `approval_id`, when the review
opens the server stores: the field schema, the list of editable fields and the initial
values. A decision is validated against this stored schema, not against the current
workflow definition: editing the workflow while a review is open does not change
the rules of a review already open. Validated edits go:

- into the stored resume command (`build_approval_resume_value`) — after
  a restart the worker continues with the corrected values;
- into the decision's idempotency fingerprint — a retry with the same key but a different
  amount gets a conflict, not the response to the earlier decision. A decision without
  `values` keeps the previous fingerprint: the `values` key is added to the hash only
  when edits are passed. So a decision made before a Core update and retried by
  the client after it with the same key gets the earlier response, not a conflict.

An Approval opened before a Core update has no stored schema and is decided
by the previous rules: without `values`, as now.

### Compatibility with released workflows

- **New keys are optional and change behavior only where they are declared.**
  Without `review`, Approval works as now. Without `items`, `properties`,
  `format`, `min_items` / `max_items`, a contract field is validated by the previous
  rules: for example, `array` and `object` still accept a JSON string.
  New checks turn on only for explicitly declared constraints.
- **"Take fields from a workflow" is an editor hint.** Execution uses
  the step's stored fields, not the source's fields. On import, the reference to the
  source is rebound the same way as `invoke_workflow` references;
  if the source is not found, the reference is removed and the fields stay.
- **Minimum Core version.** Catalog templates and extension packages with
  a typed Approval or new contract keys declare a minimum
  Core version through the existing compatibility constraints. An old installation
  does not silently import them with fields lost.
- **Existing workflows are not reworked in bulk.** A catalog template
  is imported as a local copy that can be refined in Builder.
  A protected extension graph is changed by a new extension release or
  wrapped in a top-level workflow with `Invoke`, if the contract allows it.

### Review page

While a job is in `awaiting_approval` on an Approval step with fields, the job page
in the operator view shows:

- on the left — the original, through the existing job file preview; several
  files can be paged through; the page renders PDF, spreadsheets and DOCX itself
  ([ADR 0080](0080-in-browser-document-preview.md));
- on the right — review fields as widgets by type; a changed field is marked
  and shows its initial value;
- buttons — the step's labels (`approve_label`, `reject_label`), for example
  "Export to accounting" and "Recognize again"; the comment follows the step's rules.

The viewer sees the page without input fields and buttons. After the decision the page
shows the accepted values, the author of the edits and the job's further status.

### Queue without returning to the list

The operator reviews documents one after another without returning to the list. The job page
opens in the context of the list the user came from: the same project,
workflow, filters and sorting. At the top — "3 of 47" and navigation to the previous
and next job (J / K keys). The main decision can be made as
"Approve and next" (Ctrl+Enter): the decision is made and the next job
opens immediately; the decided job's further status is visible in the list. The next job
and the preview of its first file are preloaded.

"Awaiting review" means a job in `awaiting_approval` and a job in `awaiting_child`
that has an Approval waiting for a decision somewhere down its call chain. Depth is not
limited to one `Invoke`: in the chain `A → B → C` with an Approval in `C`, `A` and `B` have
the `awaiting_child` status, and `A` enters the queue of its section. The chain
is read the same way the approval-waiting notifications already
walk it: through the `meta.invoke_parent_job_id` / `meta.invoke_children` links
in the database, with cycle protection and a depth limit.

A row is a job of the section; the page opens the review of the job where the Approval is
and decides it with that job's permissions. Permissions are split:

- with decide permission — input fields and buttons;
- with read-only permission — fields and the original are visible without input fields and buttons;
- with no read permission — the row is in the queue, but the review's fields, values and files
  are not loaded; the page says the review is awaiting a decision
  by those who have access.

A decided job drops out of the "Awaiting review" filter.
So neighbors are determined relative to the current job by the list sorting,
not by row number, and no job is skipped or shown
twice. The server returns neighbors and the position in the existing `/api/v2/jobs` with the same
filters. Without a list context, for example from a direct link, the queue
is built from the section's default filters.

### Usage examples

| Scenario | Review fields |
|---|---|
| Invoice before export to accounting | Supplier, amount, date, lines (table); original on the left |
| Blog post before publishing | Text (`paragraph`), editable; reject with the comment "rewrite the intro" |
| Lead qualification | Score (`select`) that the manager can change |
| Discount before sending to a customer | Percentage (`number`), editable |
| Moderation | Flags (`boolean`), view only |
| Simple confirmation | No fields, as now |

## What the operator and viewer see

### Run list and form

The section opens directly on the existing Jobs list with the workflow title or
the configured name, for example "Receipts". It uses the same table, filter
panel, search and pagination. `project_id` and `workflow_id` come from the section
and are always passed in the list request. Another project or workflow is
selected through the menu. The existing filters by date, status and call type
and the filters configured by the admin narrow the request within this section.
UI runs have `task_source=manual_ui`.

Columns and filters are set in Workflow Settings on the "List" tab. The project
and workflow names are already given by the section context, so the list can
show the date, files, supplier, amount, status and the author of the run. One row
is one job. A file is shown by name with download available
through the existing job file links. Output values are filled in when
the job finishes. The column projection uses data the user is allowed
to see. A user can hide some columns for themselves; the admin
defines the set of columns.

In the list header, a user with run permission clicks **New**. The form opens
in the standard `DsDialog`, following the agent creation dialog. A long form scrolls
inside the dialog, including on a narrow screen. The viewer opens the same list,
results and files in read mode. The server checks permissions independently of the elements
visible in the interface.

### Menu grouping by project

Operator and viewer navigation depends automatically on the number of projects
with published workflows accessible to the user:

- If there is one project, its sections are shown at the top level of the menu,
  without an extra parent item with the project name.
- If there are several projects, each project's name becomes the parent
  item of a collapsible group, and its sections become nested menu items.
- Projects without visible sections do not create empty groups and do not affect the choice
  between a flat and a grouped menu. If there are no sections, an empty
  state is shown.

The menu is built from active bindings, taking into account publishing, the `ui` channel and the user's
permissions. For each matching `(project_id, workflow_id)` pair, one
item is created with the configured name or the workflow name. Both identifiers
are part of the section address, including when it is shown at the top level of the menu.

## Submission modes

| Mode | Execution | History |
|---|---|---|
| For each | N ordinary runs, one per item; each item has its own stable idempotency key when the same submission is retried | N jobs, each with its own status and result |
| Bulk | One ordinary run with all form values compatible with the workflow contract | One job with a shared status and result |

The mode is determined by what one run means. Four pages of one contract are
Bulk: one job gets all four files. Four separate receipts are For each:
four jobs, one per receipt.

The admin sets the mode. For receipts, For each is chosen: the user
selects a batch, the UI sends separate requests with limited concurrency
and shows which of them were accepted and which failed to submit.
A retry of the same request uses the previous key; an intentional new run uses
a new one. Closing the browser does not guarantee that the remaining items are submitted.
There is no separate server-side batch log, no atomicity of the whole submission and no automatic
recovery of unsubmitted items. Accepted jobs are run by the current queue.

Each run's input goes through the existing validation. If the contract requires
an array, For each passes a single-item array only when its
constraints allow it. A file input gets a real uploaded file.
An array of files in one input (Bulk, for example 4 photos of one contract) needs
a storage change: each file is saved as a separate job artifact by
the existing `_persist_file_refs`, and the input gets their list in upload order.
Raising the byte limit alone does not solve this.

## Read routes

Section writes go through existing routes: runs —
`POST /api/workflows/{id}/run` and `/api/workflow-runs`, decisions —
`POST /api/approvals/{thread_id}`, settings — the workflow update. The section
list and the queue are parameters of the existing `GET /api/v2/jobs`
(`operator_view`, `filters`, `around`).

Three projections are added for reading. They cannot be assembled from existing responses
without a leak or without N requests: the menu and the section need bindings,
publishing, the contract and permissions resolved in one pass on the server, and the review must
walk the Invoke chain and check permissions on the job where the Approval is.

| Route | Purpose | Permissions |
|---|---|---|
| `GET /api/v2/projects/sections` | The operator and viewer menu: (project, workflow) pairs from active bindings of published workflows with the `ui` channel, with blocking reasons | Any role; a token needs `projects.read`. Only projects and workflows visible to the caller, taking into account the token's `allowed_project_ids` / `allowed_workflow_ids` |
| `GET /api/v2/projects/{project_id}/workflows/{workflow_id}/section` | One section: form, list and result settings resolved against the current contract, with issues and the revision for the run | Same as the menu; a section that is not in the caller's menu — 404 |
| `GET /api/v2/jobs/{job_id}/review` | The review the job is waiting for — here or further down the Invoke chain — and the caller's access to it | A token needs `jobs.read`. Access is computed on the job where the Approval is: `decide` (operator, admin) — fields, values, original and buttons; `read` (viewer) — the same without deciding; `none` — only "awaiting decision", without fields, values, files and ids. With the section's `project_id` and `workflow_id`, a job outside that pair — 404 |

All three are thin adapters over query services; the logic is in
`core/application`. They do not change data and do not grant permissions beyond those
checked on the existing write routes.

## Required changes

1. **Operator runs through the existing route.** The section form calls
   the same `POST /api/workflows/{id}/run` as Builder, with revision,
   idempotency, the UI channel and resource checks. Two changes are needed:
   an exception for this route in the middleware (today writes to `/api/workflows/...`
   are allowed only to admin) and a role check in the route itself (today admin only).
   For the operator, the server checks the workflow's publishing and an active binding to
   the given project. The operator confirms real effects when submitting
   the form. The admin run from Builder keeps its checks.
2. **Role split in the middleware.** Writes are allowed only to admin by default;
   the operator gets an explicit list: running workflows, approval decisions,
   cancelling and retrying jobs, secret values and actions on their own account.
   `PUT /api/projects/{id}` becomes admin-only; this also closes
   the current bypass where the operator changes workflow bindings
   and project policy through it, although `PUT .../workflows` and `PUT .../policy` require admin.
3. **Real upload limits.** Connect the existing attachment limits
   configuration to the UI run. Align the decoded byte limit,
   the base64 length limit and the frontend check: today 1 MiB is hard-coded in them.
   `FS_WORKFLOW_TEST_FILE_MAX_BYTES` applies to test files;
   real uploads must respect `FS_ATTACHMENT_MAX_SIZE_BYTES`
   and the current policy for attachment count and types.
4. **Run author when the column needs it.** Write the verified
   identity from scope into `jobs.meta`, without taking it from form fields.
   This is display metadata, not a new ownership policy. No migration is needed;
   old jobs may lack the value.
5. **Connect the UI to existing data.** Save the configuration from Workflow
   Settings through the existing workflow update. Build the menu from
   accessible project bindings. Use the Jobs list in the section context with
   fixed project and workflow filters and the configured columns.
   Allow the supported `task_source` filter values, including `manual_ui`,
   in the API, query service and frontend.
6. **Extend the field contract.** In fields mode, add a description of array
   items (`items`: type, `options`, nested properties) and object properties
   (`properties`), minimum and maximum item counts, and string `format`
   (`date`, `date-time`, `time`, `email`, `uri`). This is needed for repeater,
   group, multiple files, multi-select and dates. Existing
   definitions stay valid: the new keys are optional, and without them
   the previous validation rules apply, including JSON strings for `array`
   and `object`.
7. **Server-side filters.** The existing jobs list in the section context
   accepts the configured filters and applies them to the full values of
   filterable inputs written when the run was accepted, and to outputs
   (`meta.workflow_outputs`). The truncated `mapped_inputs` projection is not used
   for filters. The server accepts only filters from the workflow settings,
   not arbitrary JSON paths.
8. **Editor in Workflow Settings.** A "For operators" tab with the sub-tabs
   "Form", "Result", "List" and "Publication", built from existing
   design-system components, including `DsDialog` for choosing a widget.
   Configuration checks (incompatible widget, hidden required field without
   a value, condition on a non-existent field) are run by the server on
   save and on publish.
9. **Approval with fields.** In the Approval step configuration — review fields
   (the contract field model), initial value mapping, editability,
   the original, "Take fields from a workflow". Storing initial values in full
   when the review opens; `values` in the approval decision (UI, API, MCP) with
   validation of editable fields only and of the result by type; typed
   step outputs for mapping and Builder checks; the reject comment as the output
   of the `rejected` branch; the diff of edits in the decision history. The public approval
   projection returns review fields to those allowed to see the job. Approval without
   fields does not change. The schema, editable fields and initial values
   are pinned together with `approval_id`; edits go into the resume command
   and the idempotency fingerprint; `values` from a machine token are rejected.
10. **Review page and queue.** The operator view of the job page in
   `awaiting_approval`: the original, fields as widgets by type with corrections
   marked, decision buttons. Neighbors and the job's position in `/api/v2/jobs` by
   the list filters, J / K navigation, "Approve and next", preloading of
   the next job.

Files are already saved as job artifacts through `_persist_file_refs`;
no new storage method is introduced. Backend logic stays in application
services, routes stay thin adapters, the UI stays in `assets/frontend/`.
The work is done when a single publish adds the workflow to
the operator and viewer menu in all accessible bound projects, with separate
history and filters in each project. Workflows with `api` or `webhook` without `ui`
are absent from the menu. For each and compatible Bulk create ordinary jobs, a photo
passes the configured limits, and the server rejects an operator run without publishing
and any operator and viewer writes outside the allowed actions. The form shows
only visible fields with the chosen widgets, file inputs as upload of one
or several files per the contract, a repeater as rows of subfields; hidden fields
get their default value. The configured columns and filters work
in every bound project, including matches beyond the projection truncation limit.
Bulk with four contract files creates one job, For each with four receipts —
four jobs. On a job page in `awaiting_approval` the operator sees the original
on the left and review fields on the right as widgets by type; a corrected field is marked.
"Approve and next" makes the decision with edits and opens the next job in
the queue without returning to the list. The next step gets the corrected values
from the Approval outputs; the recognized values and the author of the edits are kept in
history. Reject passes the comment to the `rejected` branch. The "Awaiting
review" filter works in every bound project and finds a review at any
call depth (`A → B → C`) with split permissions: decide, read-only,
and with no read access the fields and files are not loaded. A decision without edits made before
a Core update and retried after it with the same key gets the earlier
response. An already released workflow from an extension and an old catalog template
are installed or imported, opened and run with the previous
data without behavior changes.

In a browser check on a normal and a narrow screen, the list is visible immediately, the
**New** button opens the form in a modal dialog, and filtering by date keeps the project
and workflow context.

## Existing owners

- [UI run service](../../../core/application/management/command_services_project_run_requests_mixin.py).
- [Builder route](../../../core/entrypoints/api/workflows_builder_run.py).
- [Role check in the middleware](../../../core/application/auth/auth_http_runtime.py).
- [Admin-only routes](../../../core/application/auth/config.py).
- [Workflow contract and run channels](../../../core/application/registry/workflow_contract.py).
- [Workflow Settings editor](../../../assets/frontend/src/pages/workflows/detail/detail-workflow-settings-controller.ts).
- [Jobs list and its filters](../../../assets/frontend/src/pages/jobs/list/index.tsx).
- [Create dialog in Registry](../../../assets/frontend/src/pages/registry/shared/catalog-create-dialog.tsx).
- [Project update](../../../core/application/management/command_services_project_crud_mixin.py).
- [File request models](../../../core/application/management/command_models_api_inputs.py).
- [Job file storage](../../../core/application/management/command_services_project_tasks_mixin.py).
- [Public history filter](../../../core/entrypoints/api/contracts_v2.py).
- [Filter validation in the query service](../../../core/application/management/query_services_job_core_mixin.py).
- [Project configuration and workflow binding assembly](../../../core/infrastructure/db/logger_policy_projects_mixin.py).
- [Approval step content and branches](../../../core/application/orchestration/approval_payload.py).
- [Approval node in the Builder graph](../../../core/application/orchestration/graph/builder_approval_gate.py).
- [Approval decision and its conflicts](../../../core/application/management/approval_decision_service.py).
- [Public approval projection](../../../core/application/jobs/public_approval_serializer.py).
- [Step configuration keys](../../../core/application/workflows/workflow_step_config_contracts.py).
- [Job file preview](../../../assets/frontend/src/pages/jobs/detail/job-detail-file-preview.tsx).
- [Approval decision panels on the job page](../../../assets/frontend/src/pages/jobs/detail/job-detail-review-panels.tsx).
