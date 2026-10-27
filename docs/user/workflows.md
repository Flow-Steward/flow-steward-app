# Workflows

Flow Steward workflows are reusable execution plans made from steps. A step
belongs to its workflow directly; loops and parallel work are real groups, and
one execution graph decides what runs next. Workflows can be launched from the
UI, API, MCP, schedules, incoming webhooks, or a parent workflow. A workflow
carries a public interface, runs as a job, records messages/artifacts, and can
pause for a human decision (Approval) or for a set time (Wait).

## Core Concepts

| Concept | Meaning |
|---|---|
| Workflow | A reusable orchestration definition: its steps, its groups, its For each loops and one execution graph. |
| Run/job | One execution of a workflow for a task. It runs the definition revision it pinned before its first step. |
| Step | One executable unit. It belongs to the workflow, and optionally to one group or one For each body. |
| Group | A group that owns a body of steps: one sequence, or parallel lanes. |
| For each | A loop that runs its body once per item of a list. |
| Execution graph | The only routing source: an entry and the edges `source → outcome → target`. |
| Outcome | The named port a step or group leaves through: `success`, `true`/`false`, `case`, `default`, `approved`/`rejected` or `error`. |
| Task envelope | Structured run input used by UI/API/MCP/webhook/schedule/test-run paths. |
| State | Runtime data available to steps through selectors such as `state.task_summary` or `state.workflow_data.customer`. |
| Contract | Declared inputs and outputs (variables, and files returned from steps), and trigger channels. |
| Artifact | A durable file or handle produced by a run or uploaded into a run. |

There are no phases. Nothing synthesizes a start, end or container step, and no
step or group carries a hidden gate. The rules below are summarized from
[ADR 0035](../architecture/adr/0035-workflow-steps-without-phases.md).

## Public Workflow Interface

Callable workflows expose one interface shared across trigger channels:

- `trigger_channels`: `ui`, `api`, `mcp`, `webhook`, `schedule`, `parent_workflow`.
- `input_mode`: `fields`, `json_schema`, or `passthrough`.
- `strict_inputs`: whether unexpected inputs are rejected.
- `expected_inputs`: named input fields.
- `expected_outputs`: named variable outputs.
- `returned_files`: file outputs. They are not declared here: each one is a
  step's file result ticked **Return from workflow** (see below). The contract
  lists them, read only, with their source step and result.
- `output_mode`: derived from the two lists (`none`, `variables`, `artifacts`,
  `variables_and_artifacts`); it is not a setting.
- `input_schema` and `output_schema`: JSON schema contracts where used.

There is no declaration of external changes. What a workflow changes outside
itself is configured in its steps; notifications, approvals and error handling
keep working through their own settings.

Use contracts whenever a workflow will be triggered externally or invoked by
another workflow. They make UI forms, webhook mapping, schedules, MCP calls, and
parent workflow mappings predictable.

In the builder, **Interface** opens the contract editor with two tabs:
**Inputs** and **Outputs**. Selecting the visual **Start** opens Inputs;
selecting **Stop · success** opens Outputs. Start represents the graph entry,
not an executable step. Every successful branch shares the workflow's output
contract.

### Returning files from a workflow

A step that saves a file — **Save file from base64**, an **HTTP request** whose
response is saved as a file, an extension action whose output is a file, or an
**Invoke workflow** step's returned-file mapping — shows **Return from
workflow** under its setup: *Make the result available to the calling workflow
and in the run results*. It is off for a new result.

Ticking it:

- adds the file to **Outputs › Returned files**, with its type and its source
  (step → result). Nothing else has to be declared;
- gives it a key made once from the result's variable (`state.invoice_pdf` →
  `invoice_pdf`), never from the file name, a timestamp or one run's file. A
  run that saves `report-2026-10-22T10-00.pdf` and the next one that saves
  `report-2026-10-23T09-12.pdf` return both through the same output, so a
  parent's mapping never has to change. Renaming the step keeps the key;
- returns a collection of files whole when the result is a list;
- keeps each step's file under its own key: two steps that write the same
  variable, two lanes or two branches never replace each other's returned file.

Unticking it removes the output from the contract; the file stays usable by the
workflow's own steps. A parent mapping that still names it is reported by the
Invoke step's validation. Ticking does not make the output required: a result on
a branch that did not run is simply absent.

A parent workflow maps a child's returned file in the **Invoke workflow** step's
**Returned files mapping** to its own variable. The parent run takes ownership
of the file and receives its handle (`artifact:<id>`); the Invoke step's result
in Job Details lists its file name, content type, size and SHA-256.

Workflows saved before this setting declared file outputs by hand under
`settings.callable.expected_artifacts`. Each such declaration keeps working when
it can only mean one step result (the result saved to `state.<name>`, or the
step output named `<name>`): it is returned under its old name, so existing
parent mappings keep working, and ticking or unticking that step's result
replaces it. A declaration that matches no result, or more than one, is a setup
issue on the workflow; nothing is returned for it until the right result is
ticked. See [ADR 0039](../architecture/adr/0039-workflow-files-returned-from-steps.md).

**Workflow settings** contains the workflow configuration separately from these
contracts. Workflow **Test Run** opens its own drawer from the bottom toolbar;
the step editor retains its separate step Test Run.

In **Schedules**, array inputs under **Inputs for scheduled runs** use **Add
item**. Each item's **Type** offers **Text** (the default) and **JSON**. Choose
JSON for an object or another JSON value; Text preserves strings such as
`true` and `false` as text. Correct invalid JSON before creating or saving the
schedule: the form refuses submission rather than sending an earlier valid
value. Saved structured items retain their values and types when reopened.

Executable nonterminal steps and groups expose an **On error** port below the
card. Connecting it creates an explicit error route, shown as a red dashed
Failure Path. Removing it restores the ordinary fail behavior when the current
policy is route-to-step. Existing retry or other authored policies require an
explicit decision instead of being silently replaced. The builder does not add
a retry mechanism. An unhandled step error fails the run; it does not follow
the success connection. Stop nodes have no outgoing ports.

## Steps, Groups And The Execution Graph

A workflow definition has three parts, and nothing else decides what runs:

- `steps`: every step of the workflow. A step with neither `group_id` nor
  `for_each_id` runs at the root. A step names at most one owner: the Group or
  the For each whose body it belongs to; in a lanes body it also names its
  `lane_id`.
- `groups`: Groups. A Group runs its body once.
- `for_each`: For each containers. A For each runs its body once per item.
- `execution_graph`: `{schema_version: 1, entry, edges}`.

The order of steps in a list and positions on the canvas are display only. An
`if` step keeps its condition and a `switch` step its cases, never a target: the
targets are edges of the graph.

### Groups

A Group runs its body **once**. Its body is `{kind: sequence, entry}` or
`{kind: lanes, lanes: [{lane_id, entry}], concurrency_limit}`; a lanes body runs
every lane in parallel up to `concurrency_limit` (the project's concurrency cap
when it is not set) and then joins. A Group has no `kind` and no `loop`: those
pre-release fields are refused (`workflow.group.loop_removed`), because
iteration is a For each.

- The root enters a Group through its node (`{kind: group, id}`); the body or
  each lane starts at its entry and ends at its `group_exit`
  (`{kind: group_exit, group_id, lane_id}`; `lane_id` is null for a sequence
  body). The Group then leaves on `success`.
- A sequence Group body may contain a For each; a Group never contains another
  Group.
- An edge stays in its scope: a body step cannot jump to the root, into another
  container or into another lane.
- A container routes `success`, and `error` only when its own error policy is
  `route_to_step`.
- A parallel lane runs `agent`, `connector`, `invoke_workflow`, `http_request`,
  `dataset_query`, `transform`, `tabular_feed`, `file_from_base64`,
  `template`, `set_variable` and `batch` steps. `if`, `switch`, Stop, Approval
  and Wait steps are refused in a lane, and a lane has no rework edges.
- A rework edge back into a container starts a new execution of it: every lane
  starts again, and a For each reads its source again from the first item. A
  technical retry or a restart keeps the execution and its position.

### For each

A For each is its own container:

```yaml
for_each:
  - for_each_id: each_order
    group_id: orders_batch      # optional: the sequence Group it sits in
    over: state.orders          # a ready collection in state
    item_name: order            # the current item, read as state.order
    index_name: order_index     # optional: its position, from 0
    body: {kind: sequence, entry: {kind: step, id: check}}
```

- The source is read once per execution. A list is walked as it is; null or a
  missing value is an empty pass; any other value (a scalar or an object) is one
  item. A dataset cursor is refused (`for_each.source_cursor_unsupported`): read
  the rows first, one item is one row, never a page.
- One operator limit applies to every pass: the first
  `FS_DATASET_QUERY_MAX_RUNTIME_ROWS` items (1000 by default) are walked. A
  longer source is not an error: the run reports `truncated` with the source
  count and the applied limit. An invalid limit setting fails the For each
  (`for_each.limit_invalid`).
- Items run one at a time. With a lanes body the lanes of one item run in
  parallel; items never do. An empty source runs no item and the For each leaves
  on `success` at once (its exit contract still applies).
- A For each may sit at the root or in a sequence Group; it never contains
  another For each or a Group.
- The root (or the Group) enters it through `{kind: for_each, id}`; its body
  ends at `{kind: for_each_exit, for_each_id, lane_id}`.
- Errors go from the step to its For each, then to the Group around it, then to
  the workflow: the nearest container whose policy handles the error decides,
  and an unhandled error fails the run. A failed item is never replayed by
  itself.

### Break, Continue and Collect

These steps belong to a sequence For each body:

| Step | Effect |
|---|---|
| `continue` | Ends the current item; the next item starts. |
| `break` | Ends the For each now; it leaves on `success` with what was collected so far. |
| `collect` | Adds one value to a variable that already exists. |

`break` and `continue` have no outgoing edges. Neither undoes a `collect` that
already ran.

`collect` is `{target, source, mode, key}`:

- `mode: append` (the default) adds the `source` value as one element of the
  array `target`. An array value is one element; nothing is flattened, and
  `""`, `0`, `false` and `null` are values.
- `mode: set_key` writes the value under the literal `key` of the object
  `target` (a key such as `a.b@example.test` is one key, never a path); an
  existing key is replaced, the others stay. `key` is a text or
  `{source: state.…}`.
- The target must exist before the For each, created by a Set Variable (`[]` or
  `{}`) or a workflow input. Collect never creates, resets or flattens it:
  `collect.target_missing`, `collect.target_type_invalid` and
  `collect.key_invalid` are precise errors.

### Placement

| Placement | Supported |
|---|---|
| Root sequence, `if`/`switch` branches, rework | Yes |
| Group with a sequence or lanes body (runs once) | Yes |
| For each at the root or in a sequence Group, with a sequence or lanes body | Yes |
| Break, Continue and Collect in a sequence For each body | Yes |
| Approval/Wait at the root, in a sequence Group or in a sequence For each body | Yes |
| Approval/Wait in a child workflow invoked serially, including child → child chains | Yes; the parent waits durably as `awaiting_child` |
| Approval/Wait/Stop in a parallel lane, or Approval/Wait in a child invoked from a lane | No; refused by validation before any run |
| A Group in a Group, a For each in a For each, parallel items | No |

### The graph: entry, edges and outcomes

The workflow `entry` is one root step or group. The first root node you add
becomes the entry; you can change it explicitly. The runtime never guesses an
entry from incoming edges, and dragging, reordering or deleting a node never
picks a new one.

An edge is `source → outcome → target`:

```json
{
  "source": {"kind": "step", "id": "check_stock"},
  "outcome": {"name": "case", "case_id": "low"},
  "target": {"kind": "step", "id": "reorder"},
  "rework": false
}
```

| Source | Outcomes it must route |
|---|---|
| Ordinary step, including `wait` | `success` |
| `if` | `true` and `false` |
| `switch` | one `case` edge per case, addressed by the case's stable `case_id`, plus `default` |
| `approval` | `approved` and `rejected` |
| `stop_success`, `stop_fail` | none; a Stop step has no outgoing edge |
| A step or group whose error policy is `route_to_step` | additionally `error` |

One outcome leads to exactly one target, and an unsupported outcome is refused.
Every executable node must be reachable from the entry of its scope. A root path
ends at a `stop_success` or `stop_fail` step; a step with no continuation is a
definition issue, never an implicit success. Branches of an `if` or `switch`
that meet again simply share their next step; a join exists only at the end of
a parallel group.

### Rework edges

Without rework edges, every sequential area must be acyclic
(`workflow.graph.cycle_without_rework`). A path that returns to an earlier node
is an edge marked `rework: true`:

- it leaves from a `true`, `false`, `case`, `default`, `rejected` or `error`
  outcome — a `success` loop is never allowed;
- it targets a step or a group in the same scope, never a `group_exit`, and a
  parallel lane has none;
- every run has a durable budget of 50 rework transitions, which restarts,
  retries and group re-entries do not reset; finite steps and loop iterations
  do not count against it. An edge may add its own `rework_limit` from 1 to 50.

When the next rework transition would exceed a budget, the run pauses for a
recovery approval before the target runs. It offers only `approve` and
`abort`: approve adds another 50 to the run allowance when that one is
exhausted, and another `rework_limit` to the edge allowance when that one is,
then takes the blocked transition once; abort cancels the run. A reject is
refused. Counters are never reset.

### Boundary contracts

The expected inputs and required outputs that used to belong to a phase are an
optional `boundary_contract` on the step or real group where the boundary is:

| Key | Meaning |
|---|---|
| `entry` | Expected input rows, resolved before the executor runs. A missing input fails the step. |
| `exit` | Required output keys, checked after an accepted successful result and its output mapping, before the continuation (including a continuation into `stop_success`). |
| `exit_on` | Steps only: the outcomes the `exit` check applies to. Without it, every non-error outcome is checked. `exit_on` without `exit` is refused. |

A group contract takes `entry` and `exit`. Error, `stop_fail` and cancel need no
outputs. A contract never changes the route, and a contract failure is never
bypassed by an approval. When a step leaves a former phase on more than one
path, put the `exit` check on every step from which a successful path leaves.

### What a step can read

A step reads the outputs of the steps upstream of it along non-rework edges in
its own scope, plus everything upstream of its group. Parallel sibling lanes are
never upstream of each other. A step in a For each body also reads the current
item under the For each's `item_name` and, when set, its position (from 0) under
`index_name`.

The builder's variable picker offers exactly that, plus the global variables,
which never depend on the position. A step that exists only in the unapplied
change set is offered the variables of where it will run: after the step it is
connected to (that step's outputs included), inside its Group or For each (at the
container's first saved step, with the For each's item names), or, while it is
not connected, every saved step's outputs, marked as such in the picker. The
same answers come from `GET /api/workflows/{workflow_id}/steps/{step_id}/available-variables`
(`?position=after` for a step placed right after that one) and
`GET /api/workflows/{workflow_id}/available-variables` (no position;
`?for_each_id=` adds that For each's item names).

Message selectors address a step or a group:

- `message.step.<step_id>.<field>` — the latest result of that step (step ids
  are unique per workflow);
- `message.group.<group_id>.<field>` — the latest result of any step of that
  real group, so a loop never accumulates per-item results.

A `message.phase.*` selector is refused with
`workflow.selector.phase_scope_removed`, which names both replacements.

## Errors, Retry And Recovery

A step or a group may carry
`error_policy: {on_error, max_retries, backoff_seconds, retry_on}`:

| `on_error` | Effect |
|---|---|
| `fail` | Terminal failure. The default when neither the step nor a container declares a policy. |
| `continue` | Records a continued error (the original status is kept) and follows the `success` edge without inventing an output. |
| `retry` | Runs the same step execution with the next attempt, within its saved `max_retries`, `backoff_seconds` and `retry_on`. It cannot reset a host transport retry refusal, spent budget or expired deadline, or resend an unknown mutation. Other existing exhausted/unmatched step-retry recovery behavior is unchanged. |
| `route_to_step` | Follows the `error` edge. The target lives only in the graph, never in the policy. |
| `await_approval` | Explicitly asks for a recovery approval of that exact step execution, where supported. |

The effective policy is the step's whole declaration, then the nearest For each
or Group declaration, then `fail`. Policies are not merged. Drawing a step's
existing **On error** connection selects its error route. Connect that route to
an ordinary Approval to ask a person, and use the Approval's existing outcomes
to continue, stop or return. No additional Automation setting is needed.
**Send notification → Human decision** selects a notification channel and does
not pause execution; **Save rules** saves reaction rules separately.

When a step inherits `route_to_step` from a container, that container lets its
in-flight siblings finish, records a failure and leaves through its own `error`
edge. Existing local policies and container graph edges keep their meaning;
connecting a different handler must not silently replace another authored
policy or delete an unrelated edge.

Execution, attempt, cancellation and run safety guards retain priority. For a
validated host transport failure, admissible operation retries run first with
the same identity, count and absolute deadline
([ADR 0026](../architecture/adr/0026-provider-neutral-operation-retry.md)). Once
technical retry is forbidden, exhausted or expired, apply the effective authored
policy before missing-response contract regeneration. An unknown mutation is
never automatically redispatched. Default fail, error route, continued error and
refused authored retry create no implicit Approval. An authored error route may
reach an ordinary Approval; the existing `await_approval` recovery policy remains
a separate choice, not an authorization to resend an unknown effect. The same
rule applies when the deadline expires during a persisted retry wait or
immediately before dispatch. A model-written artifact cannot
supply the host provenance that selects this path.

Nontransport validation, guardrail, rework-budget and autonomy behavior is
unchanged; this is not a global reordering of every failure. Only the result of
the exact dispatched execution and attempt is accepted. A Stop step and a cancel
are never treated as a recoverable failure.

A **recovery approval** belongs to one step execution. For an ordinary failed
execution, approve continues past the error on the success edge, recorded as an
operator-continued error, and reject runs that execution again with the comment
as feedback. Existing nontransport retry exhaustion or an unmatched `retry_on`
can still produce its ordinary recovery. In a parallel lane, an ordinary
transport failure with explicit `await_approval` stops its lane; once in-flight
siblings finish, the group opens one recovery approval without repeating
successful lanes. Ordinary authored Approval/rework and the exact unknown retry
cycle below are unsupported in a lane. A pre-step autonomy pause is a separate
policy keyed by the execution it guards.

## Approval Steps

An `approval` step asks a person to decide. When the run reaches it, the job
status becomes `awaiting_approval`, the worker is released, and the approval
panel, API and notification routes show the request.

| Config key (`override.approval`) | Meaning |
|---|---|
| `message` | Optional text for the reviewer. Without it a standard request is shown. |
| `show_outputs` | Output selectors whose values are shown for review; default none. |
| `output_mapping` | Maps the decision result into state with the ordinary step output mapper. |
| `response_mapping` | The existing reviewer write-back (override content targets) and its authorization. |

- The step leaves on `approved` or `rejected`; both edges are required. A reject
  is not terminal by itself: route it to rework, to another path or to
  `stop_fail`. A return cycle must contain an eligible marked rework edge and
  spends the rework budget.
- The result is `decision` (`approved` or `rejected`) and `feedback` (the
  reviewer's comment, possibly empty). It is kept in the step's message whether
  or not you map it. Existing decision paths that accept a blank rejection use
  the default request to revise; browser and token rejection still require
  feedback. Agent rework receives the feedback as before.
- There is no condition, no timeout and no automatic approval inside an
  Approval step. Check a condition with an `if` or `switch` before it; the run
  waits for a decision or a cancel.
- Each new entry into the step is a new `approval_id`. A repeated decision
  creates no second continuation, a stale or conflicting decision is refused,
  and a late approve cannot revive a cancelled run.
- After a For each, one approval covers the whole result; inside a sequence
  For each body it covers that item.

**Design consequence.** Put the Approval step *before* the irreversible action
— publishing, deleting, merging, paying or sending a customer-facing message —
and give the reviewer the evidence with `show_outputs`:

```text
gather_evidence → approval ─approved→ publish → stop_success
                           └rejected→ stop_fail
```

Approve and reject a waiting approval with the same API, MCP tools and
notification-card actions as any approval (see the
[User Guide](../GUIDE.md#step-5--human-approval)).

### Unknown external operation outcomes

Unknown outcomes follow the configured error handling without creating a
mandatory human gate. Use the existing direct error connection from the failing
step to an ordinary authored Approval when a person should decide. Both
`approved` and `rejected` keep their normal graph meanings. Approve may omit a
comment; ordinary browser Reject requires feedback. Token rejection still
requires a reason except for an allowed `abort`. Only the outcome actually connected back to the original step can
authorize that attempt. An outcome going elsewhere creates no retry permission.

A return cycle needs an eligible marked rework edge and the existing budget
([Rework edges](#rework-edges)). Rejected can be the marked return edge. Approved
cannot itself be marked rework; an Approved-return cycle must mark another
eligible edge, such as the Error connection. Graph validation and same-scope
rules still apply. No new step kind, Automation selector, required reason,
setting or system-ID input is added.

The supported authorization is one ledger-declared serial connector with one
trusted unknown observation, its own direct error edge to the current Approval,
and a chosen immediate direct dispatch back to the original step in the same
job/source/loop item. The host validates the job-pinned definition and current
gate/execution under the decision lock, then records the original full intent
and a neutral retry authorization with the effect fence and durable resume
intent. This grants one attempt **despite uncertainty**; it does not mark success
or assert that no effect occurred. The normal claim checks and consumes the
exact persisted grant before spawning. Duplicate/stale decisions and competing
consumers cannot call the provider twice from the same grant.

If this exact bound return is blocked because the existing rework budget is
already exhausted, refuse the decision
and leave the same ordinary Approval pending. No decision or retry permission
is recorded and the run does not resume or create another budget pause. No limit
is bypassed; unrelated ordinary budget handling is unchanged. If the request,
connection, installation, operation declaration or source/item differs, or its ledger declaration disappears, fail
closed without consuming permission or falling back to a new provider call.
Missing/stale provenance or a lost original checkpoint is not reconstructed.
The private association is cleared on other outcomes, steps/items or cancel.

An indirect route through another node, inherited container exit/re-entry,
multiple effects or a parallel lane is outside this direct binding. Group/For
each cases qualify only with preserved and verified same-scope source/frame
context. Raw HTTP without a ledger keeps its ordinary graph behavior: a return
may send again, but this mechanism supplies no durable one-use ledger guarantee.
A fence stops stale local confirmation; it cannot cancel the original remote
request, which may still finish.

A separate read Lookup may use the exact business correlation, return a real ID
and feed downstream mappings. That leaves the original ledger entry unknown;
neither a found ID nor an absent query settles it or independently authorizes a
resend. A Lookup inserted before Approval is an indirect path for this bounded
retry mechanism. No failed write response, ID, artifact or dataset handle is
invented. The unknown producer's partial result is not promoted as canonical
success output; independent real data remains available. A missing required ID
still fails before its consumer is called.

The browser, API, MCP, CLI and public WebSocket job events address the same
ordinary pending Approval. Use its current identity, allowed actions and caller
permissions; viewers remain read-only. Closing the panel records no decision.
Existing comment and decision rules do not become factual no-effect
certifications: Approve may omit a comment; ordinary browser Reject requires
feedback, and token rejection retains its required reason except for an allowed
`abort`. See
[Human Approval](../GUIDE.md#when-an-external-operations-outcome-is-unknown)
and [ADR 0026](../architecture/adr/0026-provider-neutral-operation-retry.md).

## Wait Steps

A `wait` step postpones the rest of the run. It has one `success` edge, no
outputs, and does not change business state.

| Config key (`override.wait`) | Meaning |
|---|---|
| `duration_ms` | A literal integer from 0 to 31,536,000,000 (365 days). |
| `input_mapping.duration_ms` | The same value read from state with the ordinary mapping dialect. |

Exactly one source is allowed. The duration is resolved once, when the step is
entered; later state changes never move the deadline. A mapped value that is not
an integer in range fails the Wait step under its error policy, and that failure
is not retried. A zero duration completes at once without pausing.

While it waits, the job status is `awaiting_timer` and the run holds no worker.
The control process checks for due timers every second and wakes at most 100
per pass, so a due run continues on the next pass; queue and worker load add to
that. This is not a real-time guarantee. After downtime, an overdue timer is
eligible as soon as the control process runs again. A Wait never approves
anything on a person's behalf.

## Step Kinds

Supported step kinds:

| Step kind | Use it for | Key configuration |
|---|---|---|
| `agent` | Run an agent/skill pair. | `agent_id`, `skill_id`, input/output mappings, error policy. |
| `connector` | Call an extension or MCP connector operation. | `connector_id`, `connector_install_id`, `connection_binding_jsonb`, operation override. |
| `invoke_workflow` | Run a child workflow and map its results back. | Child `workflow_id`, input/output/artifact mapping. See [Nested Workflows](#nested-workflows). |
| `http_request` | Make an outbound HTTP/HTTPS API call or bounded download. | Method, URL, headers, query params, body template, auth, timeout, expected statuses, response mode. |
| `dataset_query` | Execute a saved dataset query template. | Query template, params, row limit, timeout, result target. |
| `transform` | Map and reshape JSON with deterministic typed actions. | `override.transform.input.source`, `fields[]`, action pipelines, `result_target`. |
| `tabular_feed` | Process artifact- and dataset-backed tabular feeds without putting full rows in workflow state. | `override.tabular_feed.operation`, `input`, `actions`/`relational`, `result_target`. |
| `template` | Assemble one text (prompt, email, post, HTML fragment) from declared inputs with Jinja. | `override.template.text`, `format`, `inputs[]` (local name, type, required, source), `result_target`. See [Template Steps](#template-steps). |
| `if` | Branch on a condition. | `condition`; routes `true`/`false` edges. |
| `switch` | Branch on a selector. | `selector`, `cases[]` with a stable `case_id` and `when`; routes one `case` edge per case and `default`. |
| `approval` | Wait for a human decision. | `message`, `show_outputs`, `output_mapping`, `response_mapping`; routes `approved`/`rejected`. See [Approval Steps](#approval-steps). |
| `wait` | Postpone the run for a duration. | `duration_ms` or `input_mapping.duration_ms`. See [Wait Steps](#wait-steps). |
| `set_variable` | Write values into workflow state. | Assignments, source selectors, target paths. |
| `collect` | Add one value to an existing array or object variable. | `target`, `source`, `mode` (`append` or `set_key`), `key`. Sequence For each body only. |
| `continue` | End the current For each item. | None. Sequence For each body only. |
| `break` | End the For each now. | None. Sequence For each body only. |
| `batch` | Split an array into batches. | Source path, batch size, partial-batch behavior, result target. |
| `stop_success` | End workflow successfully. | Optional `message`/`reason`. |
| `stop_fail` | End workflow as failed. | Optional `message`/`reason`. |

## Loop Patterns

Iterate with a For each container over a ready collection, and put ordinary
steps in its body: agents, connectors, child workflows and controls.

### Splitting orders into two lists

```yaml
steps:
  - step_id: prepare
    step_kind: set_variable
    override:
      set_variable:
        assignments: {state.new_orders: [], state.existing_orders: []}
  - {step_id: check, step_kind: if, for_each_id: each_order,
     override: {if: {condition: "state.order.is_new == true"}}}
  - {step_id: keep_new, step_kind: collect, for_each_id: each_order,
     override: {collect: {target: state.new_orders, source: state.order}}}
  - {step_id: keep_existing, step_kind: collect, for_each_id: each_order,
     override: {collect: {target: state.existing_orders, source: state.order}}}
  - {step_id: done, step_kind: stop_success}
for_each:
  - for_each_id: each_order
    over: state.orders
    item_name: order
    body: {kind: sequence, entry: {kind: step, id: check}}
execution_graph:
  schema_version: 1
  entry: {kind: step, id: prepare}
  edges:
    - {source: {kind: step, id: prepare}, outcome: {name: success},
       target: {kind: for_each, id: each_order}}
    - {source: {kind: step, id: check}, outcome: {name: "true"},
       target: {kind: step, id: keep_new}}
    - {source: {kind: step, id: check}, outcome: {name: "false"},
       target: {kind: step, id: keep_existing}}
    - {source: {kind: step, id: keep_new}, outcome: {name: success},
       target: {kind: for_each_exit, for_each_id: each_order, lane_id: null}}
    - {source: {kind: step, id: keep_existing}, outcome: {name: success},
       target: {kind: for_each_exit, for_each_id: each_order, lane_id: null}}
    - {source: {kind: for_each, id: each_order}, outcome: {name: success},
       target: {kind: step, id: done}}
```

### Pushing an array to a connector in chunks

`batch` splits an array; it does not execute anything per chunk. Follow it with
a For each over the chunk array and put the connector step in its body:

```yaml
steps:
  - step_id: chunk_products
    step_kind: batch
    override:
      batch:
        source_path: state.items_update
        result_target: state.item_chunks
        batch_size: 50
  - {step_id: push, step_kind: connector, for_each_id: push_chunks}  # reads state.current_chunk
  - {step_id: done, step_kind: stop_success}
for_each:
  - for_each_id: push_chunks
    over: state.item_chunks
    item_name: current_chunk
    body: {kind: sequence, entry: {kind: step, id: push}}
execution_graph:
  schema_version: 1
  entry: {kind: step, id: chunk_products}
  edges:
    - {source: {kind: step, id: chunk_products}, outcome: {name: success},
       target: {kind: for_each, id: push_chunks}}
    - {source: {kind: step, id: push}, outcome: {name: success},
       target: {kind: for_each_exit, for_each_id: push_chunks, lane_id: null}}
    - {source: {kind: for_each, id: push_chunks}, outcome: {name: success},
       target: {kind: step, id: done}}
```

`batch_size` is yours to choose, from 1 through 1,000, and Core uses exactly the
number you write. Core does not know any provider's smaller per-call maximum,
does not clamp or discover one, and does not retry with a smaller size. A
provider that refuses the size it was handed reports that through the ordinary
step-failure path, naming its own error. A value outside the bounded range is
refused at authoring time rather than silently changed.

Order is preserved, the last partial chunk is kept unless
`emit_partial_batch: false`, and an empty source array produces no chunks — the
For each over it then runs no item, which is the correct result for a sync with
nothing to do this run. A For each walks ready collections only: read the rows
of a large dataset in bounded pages before iterating over them.

## Hooks

A hook is an Invoke step whose author lets each project add its own steps
at a defined place, for example "before the invoice is created". The
author declares the contract under `override.invoke_workflow.hook` instead of a
`workflow_id`:

| Field | Meaning |
|---|---|
| `hook_id` | Stable `[a-z][a-z0-9_]*` id, unique in the workflow. |
| `contract_version` | Positive revision of the contract. |
| `display_name`, `description` | The business place shown to the client. |
| `input_schema`, `input_mapping` | The published inputs (a JSON Schema object) and their sources in the author's state. |
| `mutable_fields` | Inputs the hook's steps may replace. |
| `additional_outputs` | Output-only fields: `{schema, optional, default}`; a required one needs a default. |
| `stage_contract` | `{summary, effects_before, effects_after}`: where the hook runs. |

Unknown fields are refused, as is an Invoke with both a `workflow_id` and a
hook.

A workflow an extension provides is protected: nobody changes its steps, but an
administrator reads it. Its Overview header offers **View workflow
(read-only)** where an ordinary workflow has **Open builder**, and its row menu
in the Workflows list offers **View workflow**; both open the builder
read-only, which is also where each project adds the steps of its hooks.

A hook is a place on the canvas where each project adds steps of its own. It is
drawn like a Group, in its own tone: its header says **Hook**, the hook's name,
and **Empty** while it holds no steps. Its steps are edited exactly like a
Group's: **Add step** (any kind), the ordinary step drawer, connections,
deletion, **Apply changes** and the step Test Run. A step can finish the hook
the way a step finishes a Group: its outcome leads to **End of the hook**. The
first step added to an empty hook starts it and ends the hook; to run a reusable
workflow of your own here, add an **Invoke workflow** step inside the hook. On a
protected workflow every step outside the frame stays read-only. When both the
hook and the workflow hold unapplied changes, Apply applies the hook first.

Each project keeps its own steps in a hook, so the builder of a workflow with
hooks shows a project picker in its header with the projects the workflow is
linked to (the choice sets `?project_id=`). With no linked project it says
**Link this workflow to a project to connect its hooks.** and opens the
workflow's Projects tab; until a project is chosen a hook's **Add step** is
disabled and says why.

The frame's header opens the hook's drawer. **Smart setup** explains the hook:
what it receives, what its steps may change or add, and that an empty hook
changes nothing; **Clear the hook** removes all of its steps at once (after a
confirmation, as one unapplied change that Undo or Discard brings back).
**Test Run** runs the hook's steps on typed hook inputs and shows what the hook
would publish; **Automation** works as for any step. Declared hooks belong only
in the workflow root, before or after a Group or For each. Placing one inside
either container, including a parallel lane, is rejected by validation and
cannot be saved or imported. Ordinary Invoke steps remain available inside
containers, and a hook's steps can contain their own Group and For each. A
workflow's Overview lists its hooks with the number of steps each holds in the
project.

Behind the canvas, a hook's steps are stored in a hidden per-project workflow,
created empty by the first step you add (`hook-create-and-bind`). Automation can
still bind a hook to any existing callable workflow with `workflows hook-bind`
(CLI), `workflow_hook_bind` (MCP) or
`POST /api/projects/{project_id}/workflows/{workflow_id}/hooks/{hook_id}/bind`:
a binding maps hook inputs to the child's inputs and the child's outputs to the
hook's mutable or output-only fields; each save is the next binding revision.

At run time:

- An empty hook calls nothing: its output is its inputs plus the declared
  defaults, and the step succeeds.
- A hook with steps runs them as an ordinary Invoke. At the end of the hook each
  result is read by its name: a result no step set keeps the hook input of that
  name (an output-only field its declared default). The values are checked by
  the hook contract (a wrong type fails the hook) and the whole output is
  published at `state.hooks.<hook_id>.output`. The author's own variables never
  change; read the output, or an output-only field such as `skip_request`, in
  later steps.
- A run uses the binding it pinned when it started; editing the binding changes
  new runs only. A connected workflow that no longer fits the binding fails the
  step before it does anything.

`workflows where-used` (CLI), `workflow_where_used` (MCP) or
`GET /api/projects/{project_id}/workflows/{workflow_id}/where-used` lists every
Invoke step and hook binding that calls a workflow. A change that would break
one of them is refused with the caller and the reason.

## Tabular Feed Steps

`tabular_feed` is for bulk tabular workloads backed by artifact and dataset
handles. It stores full row sets as Parquet-backed datasets and writes only a
bounded result envelope to workflow state: handles, row counts, schema, warnings,
checksums, and preview rows.

Supported operations are `inspect`, `read`, `transform`, `concat`, `write`,
`validate`, `diff`, `checksum`, `commit_snapshot`, `read_rows`, `merge_rows`,
and `create_dataset`. Inputs are explicit per operation; for example `read`
uses `input.artifact_handle`, `transform` uses `input.dataset_handle`, and
`concat` uses `input.dataset_handles`.

Supported file formats are CSV, TSV, XLSX, JSON array, NDJSON, and Parquet for
input and CSV, TSV, JSON, NDJSON, Parquet, and XLSX for export. Use `read_rows`
for bounded pages only; full feeds must not be passed through `for_each`.

The step is core-owned. Extensions and transport steps can fetch or upload files,
but generic artifact/dataset-backed tabular processing runs in the core workflow
runtime through the Artifact/Dataset Plane. The Builder inspect control calls a
bounded authenticated core API and returns only format metadata, schema,
warnings, row estimate/count, and at most ten preview rows.

Example:

```yaml
step_kind: tabular_feed
override:
  tabular_feed:
    operation: transform
    input:
      dataset_handle: state.supplier_dataset
    result_target: state.normalized_dataset
    actions:
      - target: stock_qty
        action: number.multiply
        params: {factor: 0.1}
      - target: stock_qty
        action: number.floor
      - target: price
        when:
          column: supplier
          not_equals: protected_supplier
        action: money.multiply
        params: {factor: "0.90"}
      - target: price
        action: money.round
        params: {decimals: 2, mode: half_up}
    relational:
      - op: dedupe_by
        keys: [part_number, company, condition]
        keep: first
```

### Supplier feed and snapshot sequence

Keep one stable `profile_key` for the feed's checksum, diff, and commit. Snapshot
heads belong to the normalized account/project/workflow/profile scope, so later
runs of that workflow compare against the same committed feed.

1. Obtain the original source artifact and its actual raw-byte SHA-256. For a
   core source `write`, select its `sha256` member from the existing upstream
   variable picker. Other source steps must expose their real hash through their
   output contract.
2. Map that original value into the checksum step's required **Raw SHA-256**
   variable input. If `unchanged` is true, the workflow can short-circuit the
   already committed feed.
3. Read the source and normalize the full current dataset. If checksum returns
   a committed snapshot artifact, read exactly that artifact without a column
   map or subsequent transformation for the canonical baseline. When there is
   no committed artifact, create an actually empty initial baseline dataset.
4. Choose **Prepare feed for publication** in **Comparison purpose**. Use the
   rendered controls for the current and canonical baseline datasets, key and
   profile. Map the same original source hash into the optional **Raw SHA-256**
   variable input on diff. The Builder uses the engine's comparison and
   enrichment-invalidation defaults; it has no dedicated controls for those
   column lists. A small feed without selective enrichment can go directly to
   the final diff and export below.
5. For selective enrichment in the Builder, run a preliminary diff and select
   its `diff_dataset_handle` for **read_rows**. Select the returned **Rows** for
   the extension's enrichment action, then use **merge_rows** to merge its
   updates into the **full current dataset**. A small feed that fits the bounded
   row result uses these normal fields and upstream selectors. The delta can
   include removed rows; only send rows the enrichment action can handle and
   merge updates for keys still present in the current dataset. The dedicated
   Builder does not render paging or filter controls for larger selections.
6. Run the **final diff** after any enrichment, in publication mode with the
   same original canonical baseline, profile and source hash. Select that step's
   `snapshot_artifact_id` for subsequent operations. The server saves the
   baseline's historical reference automatically; users do not type revision
   numbers or reference JSON.
7. Always read **that same final candidate artifact**, then write its returned
   dataset for the full export. This ensures carry-forward and any merged
   enrichment are exported from exactly the candidate that will be committed.
   The delta dataset is not the full replacement feed.
8. Send this export through the required destination step and wait for confirmed
   success. An unknown destination outcome follows its configured
   [error handling](#unknown-external-operation-outcomes); it does not force an
   Approval or permit sending again or advancing the snapshot.
9. Only after destination success, commit **that same final candidate** using
   the same profile. A repeat checksum with the original source hash then sees
   the committed feed as unchanged.

The engine accepts `compare_columns` and `enrichment_invalidate_on` in advanced
machine configurations. With no explicit comparison list it compares available
non-key columns except internal `__*` columns; with no invalidation list, no
fields invalidate carried enrichment. Bounded `read_rows` settings such as
filter, selected columns, offset and limit are also machine configuration, not
currently dedicated Builder controls.

This advanced configuration example uses ordinary upstream selectors and
explicit comparison/invalidation lists:

```yaml
step_kind: tabular_feed
override:
  tabular_feed:
    operation: diff
    snapshot_mode: publish
    input:
      dataset_handle: state.enriched_current.dataset_handle
      baseline_dataset_handle: state.baseline.dataset_handle
    profile_key: supplier_feed
    key_column: sku
    compare_columns: [price, stock_qty]
    enrichment_invalidate_on: [price, stock_qty]
    raw_sha256: state.source.sha256
    result_target: state.final_diff
```

The picker retains each whole result object and exposes successful result members
with their declared types. Tabular dataset-producing operations offer
`dataset_handle`; `write` offers `artifact_handle` and `sha256`; `diff` offers
`diff_dataset_handle` and `snapshot_artifact_id`; `read_rows` offers `rows`; and
`checksum` offers `unchanged` and `committed_snapshot_artifact_id`. These are
selectable upstream values, not guarantees that a failed step produced data.
The same picker infrastructure supports other typed workflow outputs.

**Compare datasets** is the default purpose. It accepts any accessible dataset
pair without a snapshot database and produces a noncommittable candidate;
commit returns `snapshot_baseline_required`. Publication instead requires the
untouched canonical read and its exact durable receipt. A mapped read (even an
identity map), transformed/concatenated/merged dataset, or uncommitted snapshot
candidate is not a canonical publication baseline. Keep the original baseline
handle separate from enrichment and comparison datasets.

The server uses revision zero only for a verified empty initial baseline. An
empty committed snapshot still has its positive historical receipt; it is not
relabelled as a first publication. A stale canonical baseline never borrows the
new head's reference, and its commit fails with `snapshot_conflict`.

Existing machine configurations can still pass a whole
`input.expected_snapshot_ref` object or state selector. The supplied reference
must match that original baseline's receipt (or the verified initial empty
baseline), and implies publication when purpose is absent. The Builder hides it
and clears it when switching to comparison; it is not a required user field.
Explicit comparison with a hidden publication reference is refused.

#### Enrichment carried into the full candidate

`diff` carries baseline columns named `enriched_*` forward for matching keys
when none of the configured `enrichment_invalidate_on` fields changed. An absent
current enrichment column, null, empty string, or whitespace-only string can
receive the previous value. An explicit current value wins, including numeric
zero and boolean false. A changed invalidation field prevents copying the old
value; it does not erase an explicitly supplied current value. Added rows have
no previous enrichment to copy.

The carried values are written into the full candidate Parquet, including
unchanged rows, and become part of the next baseline after commit. `merge_rows`
creates a new dataset; it cannot modify an already created candidate. Rebuild
the final candidate after merges. Exporting an earlier current handle can omit
carried values, while exporting the preliminary candidate can omit new
updates. The `diff_dataset_handle` is a delta that can include removed rows;
use it deliberately for incremental destinations, rather than as a full-feed
replacement.

#### Commit conflicts, retries, and development data

`commit_snapshot` checks current access, ownership, scope, candidate metadata,
and artifact availability, then atomically compares **both** the saved revision
and artifact ID with the database head. A different candidate that wins first
causes `snapshot_conflict`; the stale candidate is not rebound to the new head.
Prepare a new baseline and candidate deliberately before another publication.

A successful commit stores a durable receipt. Repeating candidate A after A and
then B have committed returns A's original result while leaving B current;
revoked grants or a deleted candidate can still prevent replay. The receipt and
head advance share one transaction. Database errors or an unknown commit outcome
are not confirmed success; a safe retry of the same candidate can consult its
receipt. Destination effects are outside that transaction: a commit failure does
not undo them and must not automatically resend them. `unchanged` compares
committed data; it does not independently prove destination success.

`checksum`, publication `diff`, and `commit_snapshot` require database authority.
Missing database
configuration or connectivity returns `snapshot_storage_unavailable`, without
an in-memory or object-pointer fallback. A legacy `committed.json` with no
committed database head returns `snapshot_dev_data_transition_required`. Inspect
and preserve the existing pointer and artifacts, then explicitly decide the
development-data transition before rerunning; the runtime does not import the
pointer or reset/delete the data automatically. Candidates remain persistent
artifacts. See [ADR 0014](../architecture/adr/0014-core-owned-tabular-feed-step-kind.md)
for the publication contract.

### Rounding modes

`number.round` and `money.round` take a `mode`, and it is applied by both the
`tabular_feed` engine and the `transform` step:

| Mode | Behaviour |
|---|---|
| `half_up` (default) | Ties go away from zero: `1.235` -> `1.24`, `-1.235` -> `-1.24`. |
| `half_even` | Ties go to the even last digit: `1.245` -> `1.24`. |
| `ceil` | Towards positive infinity: `4.801` -> `4.81`, `-4.801` -> `-4.80`. |
| `floor` | Towards negative infinity: `4.809` -> `4.80`, `-4.801` -> `-4.81`. |

An unsupported mode is rejected when the step is saved rather than silently
downgraded.

Round money with `money.round`, not by composing float arithmetic. The money
family is exact decimal throughout, so "cost x 1.5 rounded up to cents" is
`money.multiply {factor: "1.5"}` followed by `money.round {decimals: 2, mode:
ceil}`. Doing the same with `number.multiply` and `number.ceil` goes through
binary floating point, where `3.20 x 1.5` is `4.800000000000001` and the ceiling
returns 4.81.

## Data Flow And Variables

Workflow data moves through explicit mappings and runtime state:

- `input_mapping_jsonb`: maps workflow/state values into a step input.
- `output_mapping_jsonb`: maps step outputs into state or later artifacts.
- `workflow_data`: durable workflow-scoped data for later steps.
- `state.*`: selector syntax used by mappings and templates.
- `outputs`: named workflow variable outputs.
- `artifacts`: durable file outputs or artifact handles.
- `agent_messages`: runtime evidence emitted by agents and deterministic steps.

The builder uses contracts from agents, skills, tools, connectors, workflows, and
global state variables to populate pickers and validation. If a step output is
not visible to a later step, inspect the step contract, its output mapping,
whether the producing step is upstream of the reader along non-rework edges
(see [What a step can read](#what-a-step-can-read)), and any boundary contract
`exit` outputs.

### `literal:` values are parsed, not passed through

A `literal:` input is read as JSON, so it carries a type as well as a value:

| Written | Reaches the step as |
|---|---|
| `literal:12.00` | the number `12.0` |
| `literal:"12.00"` | the string `"12.00"` |
| `literal:true` | the boolean `true` |
| `literal:day travelers` | the string `day travelers` (not valid JSON, so kept as text) |

A field that must be a string — an Acme CRM `unit_price`, for example —
needs the quoted form. Writing `literal:12.00` for it sends a number, and the
extension refuses it.

### Reading a nested field out of a connector result

`output_mapping_jsonb` matches the keys of the result envelope. A connector that
returns `{"data": {"id": 1150}}` has one key to map — `data` — so map that and
read the field from state:

```yaml
output_mapping_jsonb:
  data: state.created_product
# a later step then reads state.created_product.id
```

Mapping `id` directly finds nothing. That used to be silent, and the run broke a
step later on a value nobody had written; the step that owns the mapping now
fails instead, naming the outputs it could not resolve and the keys the result did
provide.

## Editing A Workflow

Steps, groups and the graph have one writer: a **change set**, applied as one
transaction and one new revision, or not at all. The builder, the API and MCP
all use it. From the deployment host, read the current revision and then apply
one change set (add `--dry-run` to check it without saving):

```bash
./flow-steward workflows definition --project-id {project_id} {workflow_id}

./flow-steward workflows apply-changes --project-id {project_id} {workflow_id} \
  --expected-revision 12 --operations '[
    {"op": "create_step", "step": {"step_id": "notify", "step_kind": "http_request"}},
    {"op": "connect", "edge": {
      "source": {"kind": "step", "id": "notify"},
      "outcome": {"name": "success"},
      "target": {"kind": "step", "id": "done"}}}
  ]'
```

The API takes the same `expected_revision` and `operations` as a JSON body on
`POST /api/workflows/{workflow_id}/changes`.

MCP clients read the current definition and revision with
`workflow_definition(workflow_id)` and, with `admin:all`, call
`workflow_apply_changes(workflow_id, expected_revision, operations)` with the
same operations. A `409 workflow_revision_conflict` names the current `revision`.

| Operation | What it does |
|---|---|
| `create_step`, `create_group` | Add a node with the stable id you assign, so later operations in the same set can name it. The first root node becomes the entry. |
| `update_step_config`, `update_group_config` | Patch the configuration of an existing node. Structure fields (`step_id`, `step_kind`, `group_id`, `lane_id`; a group's `group_id`, `kind`) cannot be patched. |
| `set_entry` | Set the workflow entry, or with `group_id` (and `lane_id`) the entry step of a sequence body or lane. |
| `connect`, `disconnect` | Add or remove the edge of one outcome. An outcome that already has an edge is replaced only with `replace: true`. |
| `insert_step_between` | Replace `A.outcome → B` with `A.outcome → N` and `N.success → B`. Only for a step with the single `success` outcome. |
| `change_step_kind` | Give a step another kind (`step: {step_kind, …new kind's configuration}`). It keeps its id, placement, name, error policy, boundary contract and optional flag; the old kind's configuration is replaced, and edges on outcomes the new kind does not have are dropped (an error edge stays unless it becomes a Stop). Connect the new ports in the same set. |
| `move_step` | Change a step's group and lane, or move it to the root. Its edges and entry references are dropped; reconnect it in the same set. |
| `delete_step`, `delete_group` | Remove a node and its edges; no bypass is built around it. Deleting a group needs `with_contents: true` and removes its steps too. |

- The resulting definition is validated as a whole: entry, outcomes, scopes,
  reachability, rework, placement, Approval/Wait configuration, boundary
  contracts and child chains under lanes.
- A stale `expected_revision` returns `409` with `workflow_revision_conflict`
  and the current `revision`. Nothing is merged or overwritten; reload and
  apply again.
- `"dry_run": true` judges the set exactly as Apply would and saves nothing:
  the response has `applied: false`, the unchanged `revision`, all `issues` of
  the resulting definition and the `blocking_issues` that would refuse it. The
  builder uses it to show the result of unapplied changes.
- An **enabled** workflow refuses a change set that leaves any blocking structural
  or readiness issue (agents, connections, extensions, secrets), including an
  issue that already existed. Nothing is saved and the current revision is kept.
  Repair all blocking issues in one atomic set, or explicitly disable the workflow
  before incomplete edits. The workflow is never disabled automatically.
- A **disabled** workflow stores an incomplete definition together with its
  issues. Disable a workflow explicitly before a series of incomplete edits;
  enabling it requires a valid definition.
- Each operation is authorized on its own: a new step must be of a supported
  kind, and every agent, child workflow, connection and extension install an
  operation names must be usable by the workflow's account. A reference that
  does not exist and one that belongs to another account are refused alike with
  `workflow.change.reference_unavailable`. A configuration patch that does not
  name a reference does not re-check it.
- Runs started before the change commits use the previous revision; runs
  started after it use the new one. No trigger ever sees a half-applied graph.
- Saving workflow settings cannot carry steps, groups or `execution_graph`; the
  graph is a service-owned setting.

**In the builder**, structural edits — adding or deleting steps and groups,
edges, the entry, group membership and changes of kind or ports (including the
cases of a switch) — stay local until you **Apply** them as one change set, or
**Discard** the whole local set. Discard does not undo settings that were
already saved or revisions already applied. The settings of an existing step
(prompts, mappings, parameters) save with that step's panel as one
`update_step_config` change; a step that the unapplied structural set touches
cannot be saved separately until you apply or discard. After a revision
conflict the local set is kept so you can resolve it. Closing and reopening an
editor retains its unsaved parameters. If a JSON field contains invalid input,
closing or switching its editor first asks whether to discard that input; Cancel
keeps the text available to correct. Switching to another step or group asks
before discarding its unsaved parameters. Leaving the builder, including browser Back,
asks before losing unapplied structure or unsaved forms.

### Undo, Redo and the edit session

**Keys.** In the builder, **Ctrl+Z** (⌘Z on a Mac) undoes your latest action and
**Ctrl+Y**, **Ctrl+Shift+Z** (⌘Y, ⌘⇧Z) redo it. The keys follow the physical
key, so they work with any keyboard layout, and they work after the ordinary
paths — a click on the canvas, a drawer's Save or Add button, the palette —
without selecting a node first. A text field, a code editor and an open dialog
keep their own keys: ⌘Z there undoes typing, not the graph. There are no Undo
or Redo buttons.

Every change you make is one step of the history, in the order you made it:
adding a step from the canvas, the palette or a drawer, changing its
parameters, connecting, moving, deleting, grouping and For each. One command
that changes several things (add and connect, delete a Group with its steps, a
drag of several cards) is undone with one press. A new change after Undo ends
Redo. The history follows the latest action across the workflow and an open
hook.

**Undo never writes to the server.** The history does not end at Apply:
undoing an action that is already applied puts its reverse into the unapplied
set, and the server changes only on your next **Apply**. Saving a saved step's or
group's settings is an action of the history too. "Unapplied" means your copy
differs from the last saved version, not that there is something to undo: after
Apply the builder is clean even with an Undo history.

**The edit session survives a reload.** The builder keeps your copy of the
workflow, its Undo/Redo history (with the Redo branch) and unsaved form values
in this browser tab, written after every action. A reload or a restored tab
continues the same session; a second tab, including a duplicated one, has its own.
The copy holds only what you author: never secrets typed into a form (the form
asks for them again), notes, or whether the workflow is enabled. Signing out
removes it. If the browser cannot keep the copy, the builder says so — apply
your work or keep the tab open — and nothing is saved on the server for you.

**Someone else saved meanwhile.** When the workflow on the server changed and
you have local work or an Undo history, the builder keeps your copy on screen
and opens **Compare your copy with the server**: your changes, the changes on
the server, and how the two versions differ. Nothing is merged automatically.

- **Keep my version** saves your copy as one new revision after the server
  revision you reviewed (after a confirmation). Whether the workflow is enabled,
  its notes and input secrets stay as they are. If another revision was saved
  in the meantime, nothing is written and you get a new comparison.
- **Accept the server version** discards your copy and its history and continues
  from the server version. Nothing is written.
- **Decide later** keeps your copy; **Compare versions** in the conflict bar
  opens the comparison again.

A revision that changed nothing you edit (for example, enabling the workflow)
just moves the base and keeps your history. A successful history Restore also
starts a new history.

**Leaving.** Reloading or closing the tab asks first only while something is not
saved on the server (unapplied changes, unsaved forms, an Apply in progress).
Leaving the builder for another page asks **Stay** or **Leave and discard**;
switching panels or this workflow's pages does not. A normal leave ends the
session, so the next visit starts from the server.

The same comparison and save are available to API clients:
`POST /api/workflows/{workflow_id}/edit-session/compare` with
`{base_content, local_content}` answers `current_revision`, `current_content`,
`base_to_local`, `base_to_server` and `local_to_server` without writing;
`POST /api/workflows/{workflow_id}/edit-session/apply` with
`{content, expected_revision, dry_run}` writes the whole authoring content
(version, display name, description, tags, builder metadata, trigger, settings
and the full steps, Groups and For each lists) as one revision through the same
rules as Apply and Restore. Unknown or server-owned fields are refused; a stale
`expected_revision` is a `409 workflow_revision_conflict`.

## Workflow History

Every save of a workflow records the workflow as stored: its steps, Groups,
For each containers, routing graph and settings. Open the workflow settings
drawer and choose **History** to see the saved revisions with their author and
time. Selecting a revision compares it with the one before it. Steps, Groups and
For each containers are matched by their IDs, so inserting a step shows one
added step, not a change of every step after it; a new order of the same nodes
shows as one order change.

**Restore** saves the selected revision's content as a new revision; history is
never rewritten and the revision number only grows. Restore takes back the
steps, Groups, For each containers, routing graph, workflow settings, name,
description, version, tags and the stored `trigger` field. It keeps as they are
now: whether the workflow is enabled, its provenance and availability, canvas
notes, the configured secrets and test files of its inputs, and everything kept
outside the workflow definition — webhook entrypoints, provider triggers,
schedules, reactions and hook bindings. A secret or test file comes back only
when the same input still has it now; an older stored value never returns.

Restore follows the same rules as Apply. On an enabled workflow the restored
definition must be ready to run, or nothing is saved. A disabled workflow can
take back an unfinished definition; the response lists its issues. A revision
in a removed pre-release format, one that names an agent, child workflow,
connection or extension install the workflow may no longer use, and one that
would break an enabled caller are refused without any change. Restore uses the
revision shown in the builder: if someone saved since, it answers `409
workflow_revision_conflict`. Apply or discard unapplied canvas changes first. A
successful Restore starts a new Undo history in the builder.

Revision numbers are stable: number 3 always names the same entry, even after
older entries are cleaned up. Entries written before full snapshots were kept
show no number; one that does not record the definition can be viewed but not
restored. A workflow provided by an installed extension keeps a reference to the
extension's definition in its history and cannot be restored from it. History
retention keeps the newest entries of each workflow; every save — including a
settings, secret or test-file save — takes one place, so frequent settings saves
shorten how far back structural changes go.

## Convert A Group To A Child Workflow

Select a saved group and choose **Convert to child workflow**.
Apply or discard pending structure and parameter edits first. Enter a unique
lowercase child identifier and a display name, then preview the typed inputs,
outputs and mappings. Preview writes nothing. Apply creates a callable child
and replaces the parent group with an ordinary `invoke_workflow` step in one
transaction. A stale revision requires a new preview; a failed apply creates
neither a partial child nor a partial parent edit.

The child keeps the original group, its member steps, internal connections and
loop/parallel settings. Parent entry, incoming/outgoing connections and rework
markers are preserved. The generated child has explicit terminal steps and uses
the existing durable child execution. Parent triggers and canvas notes are not
copied; the child is made available in the same active projects as its parent.

Automatic conversion requires a provable, typed state boundary. The preview
lists exact blockers for implicit agent or message history, message-only
results, conflicting producer types, mutable loop state, partial object writes,
dynamic runtime context, group index dependencies, exit contracts, unsupported
artifact ownership, reactions requiring scope changes, and Stop steps that would change which workflow terminates. A read after an operation
that can continue or recover without writing its result is also ambiguous.
These cases require authoring an explicit callable contract rather than an
automatic conversion. No blocked preview changes either workflow.

MCP uses `workflow_convert_group(workflow_id, group_id, child_workflow_id,
display_name, expected_revision, dry_run=true)`. Apply repeats the same
arguments with `dry_run=false` and the returned `preview_digest`. The API is
`POST /api/workflows/{workflow_id}/groups/{group_id}/convert-to-child`; both
surfaces use the existing administrator and account/project access checks.

The deployment-admin CLI uses the same service:

```bash
./flow-steward workflows convert-group parent_workflow \
  --project-id PROJECT_ID --group-id process_items \
  --child-workflow-id process_child --display-name "Process child" \
  --expected-revision 3 --json --confirm
```

That command previews only. Add `--apply --preview-digest DIGEST` with the
unchanged arguments to apply the reviewed preview. Read the current revision
through the workflow definition API or MCP before starting.

## Canvas Notes

In the builder, choose **Add → Note**, enter plain text or Markdown, and select
**Save note**. Notes are canvas documentation: they have no execution ports and
do not participate in routing, retries or test runs. Drag the note by its header;
select it to resize it. Position and size are saved when the gesture ends and
are available after reloading or opening the workflow on another device.

After saving the text, attach a PNG, JPEG, GIF or WebP image in the same editor.
Click its canvas thumbnail for the existing full-size file preview. An image
belongs to the workflow and does not require a job or an input parameter.
**Remove image** retains the text; **Delete note** removes the note and its image.
Uploads use the existing file preview size limit, `FS_FILE_PREVIEW_MAX_BYTES`.

Notes save separately from the structural **Apply changes** set and use the
same workflow revision guard. If another author saves first, the editor keeps
your draft. Select **Reload saved notes**, review the saved version and your
draft, then save again. This does not silently rebase pending structural edits.

The workflow's `settings.canvas_annotations` contains the text, coordinates,
dimensions and image references. The dedicated `/api/workflows/{workflow_id}/annotations`
routes own writes to it; a general settings save preserves the latest notes atomically.
Note writes use two guards: `expected_revision` identifies the executable workflow,
and `expected_annotations_revision` identifies its presentation. Read responses
return `revision`, `annotations_revision` and `annotations`. Both guards are required
for note and image mutations, including requests made by API/MCP clients. A missing
guard returns 428; a stale presentation returns 409 with
`workflow_annotations_revision_conflict`; a changed executable definition retains
`workflow_revision_conflict`. Reload before retrying; note saves never rebase a
pending structural change set.

`settings.canvas_annotations_revision` starts at zero and increases on each note or
image mutation. These writes leave the execution revision, runtime snapshots and
definition changelog unchanged. Notes and their presentation revision are excluded
from executable snapshots and their digest. Import and clone initialize an independent
presentation revision; portable bundles omit this local CAS value. SDK-owned notes
continue to update through the extension release projection and are read-only when
the workflow is protected. Cloning preserves note text and layout if an image is
missing, omitting the unavailable attachment.

The existing workflow-definition reads also include notes. YAML bundles retain
text and layout; image files stay in their original installation and must be
reattached after import. Import refuses local artifact references.

## Workflow Test Runs

The builder supports two test-run modes, both executed as durable worker jobs:

- Workflow Test Run: executes the workflow against a project binding.
- Step Test Run: executes one selected step with a task envelope and project
  context. It starts at that step and never follows its edges.

Step Test Run labels such as **Required by step** describe the runtime input
contract. The isolated test's sample values are optional: earlier steps and loop
context are not loaded, and a saved mapping alone does not preload a value.
Leaving a sample blank can therefore make execution fail on a missing required
input. This does not make required workflow-launch inputs optional.

Test Run is not available while the builder has unapplied structural changes,
or unsaved, saving or failed settings edits of the step being tested (for a
Workflow Test Run, of any workflow, group or step form); apply or discard and save first. A step that
exists only in the local set cannot be tested. The request carries the revision
the builder shows as `expected_revision`; if the workflow moved on, the server
answers `409` (`workflow_revision_conflict`) before any job is created, rather
than testing another revision. Workflow and Step Test Run can test a saved disabled
draft; readiness, project binding and caller permissions still apply. Normal
launches, including MCP `workflow_trigger`, require the workflow to be enabled.

The POST request validates the workflow, project binding, inputs, and caller scope,
then returns `202 Accepted` with the job and thread identity. The builder polls the
test-run GET endpoint until the worker reaches a terminal or waiting state
(`awaiting_approval`, `awaiting_timer` or `awaiting_child`). A
workflow dry run remains synchronous and returns `200` without creating a job.
Execution failures are reported as durable terminal state by the GET endpoint;
cancel and approval actions use the same job/thread identity as normal jobs.
Every test-run job, including a Step Test Run, pins the definition revision it
runs, like any other job; test inputs and test-mode overrides stay invocation
data.

Test runs are meant for debugging:

- input mappings;
- missing required inputs;
- expected output names;
- HTTP request templates;
- dataset query scope;
- child workflow contracts;
- branch outcomes, Approval and Wait steps;
- boundary contracts;
- artifact output handling.

Secrets and sensitive keys are redacted from test-run payloads before returning
diagnostics.

## Run Inspector

Every workflow run executes the definition it pinned before its first step: a
deduplicated, immutable snapshot of its steps, groups, graph, configuration,
contracts and mappings at one revision. Editing or deleting the workflow later
never changes a run. A run whose definition is not valid is not dispatched.

Job Details shows that executed definition, not the current workflow:

- **Executed definition: revision … · digest**, with a link to open the
  current workflow separately. The same data comes from
  `GET /api/jobs/{job_id}/definition` (`snapshot_id`, `revision`, `digest`,
  `steps`, `groups`, `execution_graph`). It stays available after the workflow
  is edited or deleted and after the run's checkpoint is cleaned up; a snapshot
  is kept while any job references it.
- A job without a snapshot shows **Definition snapshot unavailable** with its
  surviving logs; the API answers `available: false` with
  `unavailable_reason: definition_snapshot_unavailable`. The current definition
  is never shown as the one that ran.
- A run parked on a Wait step shows the time it waits until; a run waiting for
  a child workflow links to the child job; a parallel group lists its active
  lanes with their step and status.

The job projection names the run position with `current_step_id` (null while a
parallel group runs), `current_group_id`, `active_steps[]` (`step_id`,
`step_execution_id`, `attempt`, `group_id`, `for_each_id`, `lane_id`, `iteration`, `status`)
and, while the run waits, `workflow_wait`. A snapshot does not keep runtime
inputs, files or secrets, and does not extend their retention.

Archived run definitions retain the source's visibility policy when pinned.
Extension-protected definitions remain hidden even after their source workflow
is deleted. Older snapshots without a recorded visibility policy report
`definition_visibility_unknown`; they are not assumed public. Public snapshot
views also remove secret defaults and secret-store reference authority.

## Restore a Content Revision

Content history restoration applies to an existing active workflow and requires
its current `expected_revision`. A concurrent edit returns `409` without changing
either definition. To restore a deleted workflow, use Trash restore first, with
the usual access checks; restoring a content revision does not recreate or
un-delete a workflow.

## Workflow Bundles

Export and import use `platform.workflow_bundle.v2`. Each workflow in a bundle
carries `steps`, `groups` and `execution_graph` (the graph is a definition key
of the bundle, not a setting). Import creates through the same validation as a
change set: a definition issue that no valid workflow can have blocks the
import, while an unfinished draft (a missing graph, entry or outcome edge, an
unreachable node) imports disabled with its issues reported. See
[Which topology findings block a workflow import](../decisions/workflow-import-topology-blocking.md).

A phase-based `platform.workflow_bundle.v1` bundle is refused before anything is
written, with `bundle_schema_unsupported` and the `required_schema_version`.
There is no converter: recreate such a workflow in the builder, following the
[manual recreation runbook](../operations/workflow-steps-rollout.md).

## Incoming Webhooks

Incoming webhooks are workflow-owned public entrypoints. Use them for external
systems that need to start a workflow through HTTP.

Configurable webhook settings:

| Setting | Purpose |
|---|---|
| `auth_mode` | `shared_secret_header`, `bearer_token`, `basic_auth`, or `hmac_sha256`. |
| `allowed_content_types` | Accepted request body types, such as JSON, form, multipart, or text. |
| `source_restrictions` | Exact IPs and CIDR allowlists. |
| `max_body_bytes` | Maximum request body size. |
| `max_file_bytes` | Maximum size for each uploaded file. |
| `max_files` | Maximum uploaded file count. |
| `idempotency_mode` | Duplicate request behavior. |
| `input_mapping` | Maps webhook payload fields to workflow expected inputs. |
| `response_mode` | Controls the accepted-response behavior. |

Webhook file uploads are stored through the artifact plane with `run_window`
retention. Zip uploads are inspected before storage.

## Template Steps

A Template step builds one text from values produced earlier in the workflow: a
prompt for an agent, an email, a product description, a blog post or an HTML
fragment. It writes the text to one variable and never calls a model.

**Inputs.** Declare every value the template reads. Each input has a local name
(`header`), a type (`text`, `number`, `boolean`, `object`, `array` or `json`), a
required flag, and a source picked from the workflow variables
(`state.outputs.title`). The template uses the local name only: `{{ header }}`,
`{{ product.name }}`, `{% for image in images %}`. It sees nothing else of the
workflow state. Values keep their types; a list stays a list.

**Template text.** Standard [Jinja](https://jinja.palletsprojects.com/en/stable/templates/):
variables, `if`, `for`, and the built-in filters. Values inserted with `{{ … }}`
are coloured in the editor; *Insert variable* puts `{{ name }}` at the cursor.
To print literal `{{` or `{%`, wrap them in `{% raw %}…{% endraw %}`. Spaces,
blank lines and the final newline are kept exactly; use Jinja's `{%-`/`-%}` to
trim them. A value that itself contains `{{ … }}` is inserted as text, never
rendered again.

**Missing values.** A required input with no value fails the step before
rendering. A missing optional input stays undefined: `{{ footer | default("") }}`
and `{% if footer is defined %}` handle it, and any other use fails the step.
An empty string, `0`, `false`, `[]` and `{}` are values, not missing ones. A
`null` is accepted only by a `json` input. A failed step writes nothing.

**Format.** `text` (default), `markdown` or `html`. The format never converts the
result: Markdown stays Markdown. In `html` every inserted value is escaped (`<`,
`>`, `&`, quotes); `{{ fragment | safe }}` inserts trusted markup unchanged, so
use it only for HTML you control. `text` and `markdown` insert values as they are.

**Preparing data.** Shape data with a [Transform](#step-kinds) step first and
point the template input at its result; the template only lays the data out.
Images are inserted as text: a URL in `![alt](https://…)` or `<img src="https://…">`.

**Limits.** Template text up to 100,000 characters, inputs up to 2,000,000 bytes of
JSON, a result up to 1,000,000 characters, and 5 seconds per render. The render
runs in a separate sandboxed process: no file, network or application access,
and `include`, `import` and `extends` are not available. Inputs whose source path
names a credential (`token`, `password`, `secret`, …) are refused.

**Testing.** Open *Test Run* on the step, enter the input values (JSON for
`object`, `array` and `json`), and run it; earlier steps do not run. The result
shows the exact text and, for Markdown and HTML, a *Preview* in an isolated frame
that runs no scripts. After you change the template or the test values, the old
result is marked as out of date. In Job Details the step shows its inputs, the
text and its format; *Show full text* opens texts up to 64,000 characters in full.

Example: a Markdown post.

```yaml
step_kind: template
override:
  template:
    format: markdown
    inputs:
      - {name: header, type: text, required: true, source: state.outputs.title}
      - {name: body, type: text, required: true, source: state.outputs.draft}
      - {name: footer, type: text, required: false, source: state.outputs.footer}
      - {name: images, type: array, required: true, source: state.outputs.images}
    text: |
      # {{ header }}

      {{ body }}

      {% for image in images %}
      ![{{ image.alt }}]({{ image.url }})
      {% endfor %}

      ---
      {{ footer | default("") }}
    result_target: state.outputs.post_markdown
```

Example: an HTML product table from a list prepared by a Transform step.

```jinja
<h2>{{ product.name }}</h2>
<table>
{% for item in parameters %}
  <tr><td>{{ item.name }}</td><td>{{ item.value }}</td></tr>
{% endfor %}
</table>
```

A later step reads the result like any other variable: map
`state.outputs.post_markdown` into an agent input, an HTTP request body, or an
email action.

## HTTP Request Steps

HTTP request steps are built-in outbound HTTP/HTTPS calls. Use them for APIs and
HTTP/HTTPS downloads. FTP, FTPS, and SFTP transfers need a file-transfer
extension installed from the extension catalog.

Configurable fields:

- `method`: defaults to `GET`.
- `url`: required, supports templates from state.
- `headers`: rendered from state.
- `query_params`: rendered from state.
- `body_template`: string, object, or array body template.
- `auth_mode`: `none`, `bearer`, or `basic`.
- `auth_ref`: project secret reference. `env:` references are rejected.
- `timeout_seconds`: positive number, defaults to 30 seconds.
- `expected_statuses`: defaults to `[200]`.
- `response_mode`: `inline` or `artifact`; defaults to `inline` when omitted.
- `response_target`: required state path where the response result is stored.
- `max_response_bytes`: inline defaults to and cannot exceed 5 MiB. Artifact mode
  defaults to 100 MiB and cannot exceed 200 MiB.
- `artifact_filename`: optional artifact-mode filename template.

Responses are streamed and counted after HTTPX content decoding. A compressed
response can therefore fail if the decoded entity bytes exceed the configured
limit. Raw `Content-Length` is preserved as response metadata, but only identity
encoded lengths are trusted for early rejection. Redirects are not followed.
When Basic or Bearer auth is used, the URL scheme and authority must be a static
literal HTTPS origin with no userinfo; templates may still be used in path,
query, headers, and body.

The result includes:

- `ok`;
- `status_code`;
- `retryable`;
- `failure_reason`;
- `response_body` in inline mode;
- `artifact_handle`, `artifact_id`, `filename`, `content_type`, `size_bytes`, and
  `sha256` in artifact mode;
- `response_excerpt`;
- selected response headers;
- rendered request metadata.

## Nested Workflows

`invoke_workflow` lets a parent workflow run a callable child workflow.

To fan one normalized input out to several destinations, put each
`invoke_workflow` step in its own lane of a parallel group. The parallel
executor starts the lanes up to the group's `concurrency_limit` (or the
project's concurrency cap), waits for every lane to reach a terminal result, and
then merges each child's declared output and artifact mappings into the parent,
in lane order.
Use distinct parent mapping targets for each child so destination reports do
not overwrite one another. A following parent step can aggregate those mapped
results and deliver one report through email, a chat notifier, MCP, or another
configured channel.

The runtime:

- validates that the child workflow is callable;
- maps parent state into child inputs;
- validates child input contract;
- creates the child job of this invocation, or finds the one it already has
  (one child per step execution, linked to the parent in the same transaction);
- in a serial position (root, a serial loop, a child called serially) parks the
  parent as `awaiting_child`, releasing its worker, until the child job has
  finished — however long the child waits on its own Wait or Approval steps;
- from a parallel lane (and for every descendant of such a child) keeps the
  finite path: runs or waits for the child within the timeout below, and refuses
  a child whose definition contains Wait or Approval steps;
- rejects direct and indirect workflow cycles during contract validation;
- blocks any child that already appears in the invocation ancestry at runtime, so
  stale or concurrently changed definitions cannot recurse forever;
- enforces `FS_INVOKE_WORKFLOW_MAX_DEPTH` (default `5`) as an additional nesting
  bound even when no repeated workflow ID is present;
- collects declared child outputs and the files the child returns;
- maps outputs and returned files back into the parent, which takes ownership
  of each file;
- records child job metadata for recovery and observability;
- cancels unfinished children durably when the parent is cancelled.

Deployment knobs of the finite (lane) path — a serial parent ignores them:

- `FS_INVOKE_CHILD_TIMEOUT_SECONDS`.
- `FS_INVOKE_CHILD_TIMEOUT_CAP_SECONDS`.
- `FS_INVOKE_CHILD_POLL_SECONDS`.
- `FS_INVOKE_CHILD_CANCEL_ON_TIMEOUT`.
- `FS_INVOKE_INLINE_EXECUTION`.
- `FS_INVOKE_WORKFLOW_MAX_DEPTH`.

## Dataset Query Steps

`dataset_query` steps execute saved query templates through a dataset connector.
They do not accept raw SQL in the workflow step. The step must reference a saved
query template and run with canonical project/account/tenant/workspace scope.

Configurable fields:

- `query_template_id`;
- `params` or `input_params`;
- `row_limit`;
- `timeout_seconds`;
- `result_target`.

### How many rows a query may return

A `dataset_query` step materialises its whole result set into workflow state and
keeps it there for the rest of the run, so the row count is bounded by one
deployment-wide maximum: `FS_DATASET_QUERY_MAX_RUNTIME_ROWS`, default `1000`.

- `row_limit` on the step is your request; it is approved only if it fits under
  the deployment maximum. A larger `row_limit` is rejected before the query runs,
  and the message names both the requested and the allowed value.
- Raising the maximum raises load on the source database, worker memory and
  checkpoint storage together, because the rows stay in state.
- The connector reports `truncated` when the source had more rows than the
  approved limit, so a clipped result is visible rather than silent.
- For volumes that do not fit comfortably, have the source system prepare an
  export — a URL, FTP/SFTP, an upload or an email attachment — and read the file
  with `file-transfer` and `tabular_feed` instead of pulling it through a query.

Preview limits in the dataset editor are separate and are not affected by this
setting.

### Data connections

Core stores abstract connections and dispatches one universal extension action;
it holds no database drivers and runs no SQL of its own. A connection can only
run a saved query when it is bound to an installed Data Connector extension,
whose configuration selects the concrete driver. The platform's own database is
infrastructure and is not offered as a query connection.

A workflow whose saved query points at an unbound or disabled connection is
rejected when you save it, not when it runs.

For extension datasets that resolve connections per project, **Preview** and
**Validate** ask you to select an assigned project. The choice starts empty even
when only one project is available. Assign the dataset from the project's
Datasets page and select exactly one compatible data connection in its
configuration. These registry actions use that project's selected connections;
missing or ambiguous connections are reported before the query runs.

Common failures:

- missing query template;
- disabled query template;
- dataset not assigned to project;
- missing data connection;
- data connection not bound to a Data Connector extension;
- `row_limit` above the deployment maximum;
- connector runtime unavailable;
- missing canonical scope.

## Artifacts

Workflows can receive and produce artifacts. Users should think in terms of
artifact handles, not raw S3 bucket paths.

Artifact metadata includes:

- `artifact_id`;
- `filename`;
- `content_type`;
- `size_bytes`;
- `sha256`;
- `download_url` or presigned access when available;
- `retention_class`;
- internal storage reference.

Supported storage backends:

- `memory`: safe in-process local default, but not a cross-process extension
  artifact grant transport.
- `s3`: S3-compatible storage such as internal object storage or an external S3-compatible service.
- `local_fs`: filesystem-backed storage for trusted local/dev use. Extension
  artifact grants do not expose `file://` URLs unless
  `FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS=1`; use S3-compatible storage
  when read/write capabilities must be enforced for lower-trust extensions.

`tabular_feed` uses the same canonical Artifact/Dataset Plane storage.

Retention classes:

- `ephemeral`;
- `run_window`;
- `audit`;
- `persistent`.

### Job invocation evidence and retry

Job Details shows a bounded, recursively redacted projection of the effective
launch inputs. It includes source keys, canonical byte size and SHA-256 digest,
truncation state, and compact file summaries. This projection is operator
evidence only: it is capped at 16 KiB and is never used to execute or retry a
job.

Each new workflow job also receives an encrypted, audit-retained replay
snapshot. That immutable snapshot contains the exact validated inputs,
workflow selection, host-authorized test mode, bounded host-minted secret-reference
fingerprints, and immutable artifact/dataset handles with digests, sizes, and owner
scope. It is the only retry authority. A retry fails closed when the snapshot is
missing, partial, expired, belongs to another job/project, or fails an integrity
check. Jobs created before this contract do not fall back to current defaults or
legacy job metadata and must be launched again instead.

Raw ingress capture and an original-ingress view/download endpoint are not part of
this release. Job Details retains only bounded source-key and request-context summaries.

## Notifications And Automation

A workflow's **Automation** reacts to the events of its own runs. A rule has a
scope — the whole workflow, one real group, or one step — and reacts only to
events of that scope. A step/group scope must exist in the same workflow.
Remove or retarget its automation rules before deleting the step or group;
both full definition saves and builder change sets reject orphaned rules,
including on disabled workflows. Preview reports the same blocking issue.
Rule replacement and definition writes serialize on the workflow row, so a
concurrent rule save cannot race a deletion.

| Event | When |
|---|---|
| `job.completed`, `job.failed` | The run ends. |
| `job.step_started`, `job.step_completed`, `job.step_failed`, `job.step_blocked` | A step execution starts, completes, fails or is blocked, at the root or in a group. |
| `job.group_started`, `job.group_completed`, `job.group_failed` | A real group execution starts, completes after its accepted success, or fails after the decision to leave with a group failure (not on each transient retry). |
| `job.approval_waiting` | An Approval step, a recovery approval or an autonomy pause waits for a decision; the payload carries `wait_id`, `approval_id` and `reason` (`authored`, `recovery` or `autonomy`). |
| `job.timer_waiting` | A Wait step has paused; the payload carries `wait_id` and `due_at`. |

Each wait raises one event: a restart or a refresh of the run never sends the
same notification again. The retired `job.phase_started`,
`job.phase_completed`, `job.phase_failed` and `job.gate_waiting` events and the
`phase` scope are refused everywhere; recreate such rules with a group event for
a group or a For each, or a step or approval event for an ordinary former
phase.

When a child workflow invoked serially waits for approval, its own reactions
handle `job.approval_waiting`, and each ancestor that waits on it up to the root
also receives a `job.approval_waiting` event for its own invoke step, with
`propagated: true`, the `approval_job_id` and `approval_id` of the actual
approval, the immediate `child_job_id`, and `origin_job_id`,
`origin_workflow_id` and `origin_step_execution_id`. A parent rule that notifies
about approvals therefore fires without per-child setup. Links and decisions
always address the child's approval, and are authorized against the child; the
parent's own rights do not let anyone see or decide it. The projection carries
no child outputs or secrets and does not change the parent, which stays
`awaiting_child`. Delivery stops once the approval is no longer pending or the
ancestor has finished or been cancelled. The forwarded payload preserves the
actual approval's `allowed_actions`; a recovery gate does not gain a Reject
button when it only allows Approve and Abort. The reaction scope and
notification event identity still name the subscribing parent, while the
notification's actionable `job_id` names the child. The host verifies that
pending approval and invocation lineage again before dispatch.

Child approval notification catch-up uses the current durable public approval
projection for authored Approval, recovery and autonomy gates. The stored
`approval_id`, `wait_id`, `reason` and `allowed_actions` identify that exact pause.
A delayed event from an earlier pause cannot notify or address a newer decision.
If an older run has no complete durable approval identity, ancestor forwarding
fails closed; it never creates a synthetic wait or guesses an interrupt ID.

Workflow automation rules can take actions such as:

- `resume_job`;
- `approve_job`;
- `reject_job`;
- `cancel_job`;
- `notify`.

Notification actions can target a specific route key or let the runtime choose
matching project routes. Route matching can use event type, severity, project,
workflow, provider, and enabled state.

Every **Send notification** rule of a linked workflow appears, with its scope,
event, destination and state, on the project's **Notifications** tab
(**Automatic notifications**), next to any existing project event
subscriptions (listed read-only, with no editor). The tab is read-only; the
**Open settings** link of a rule opens it in the builder at its workflow,
group or step (`?drawer=…&rule=<key>`). Message-sending steps are
not listed there.

Document notification setup together with human approval, because approval
requests often need an external human-decision channel.

## Troubleshooting Checklist

When a workflow does not behave as expected:

1. Run Workflow Test Run or Step Test Run.
2. Check workflow validation: entry, outcome edges, reachability, placement and
   boundary contracts.
3. Confirm expected inputs match the trigger payload.
4. Confirm step input and output mappings.
5. Confirm the producing step is upstream of the reader and actually emitted the
   required outputs.
6. Open the run in Job Details and compare it with its executed definition, not
   with the current workflow.
7. Check artifact retention and upload limits.
8. Check security policy events for blocked/redacted payloads.
9. Check job logs, runtime journal, agent messages, and notification deliveries.
10. For child workflows, inspect the child job and declared output/artifact mapping.
