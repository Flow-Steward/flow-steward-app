# ADR 0065: Composite extension actions

- Status: Accepted
- Date: 2026-11-10
- Builds on: [ADR 0013](0013-extension-package-identity-and-dependencies.md),
  [ADR 0038](0038-workflow-containers-for-each-collect-hooks.md)

## Context

First-party extensions expose primitives: one action per provider call. A person
who wants "create the order and its shipment" builds that from three steps and
keeps it working when the provider changes.

Extension authors want to sell exactly that convenience on top of a base
extension: one action that takes the data and does the rest. Two ways to build
it were considered.

- Extension code calls another extension's action through the host. This needs a
  runtime-to-host channel, a new grant kind over another extension's operations
  and a second retry and audit model for nested calls.
- A workflow of the same bundle implements the action. Calling another
  extension's action, branching, looping, retries, external-effect claims, audit
  and Job Details already exist for workflow steps.

Shipping such workflows as ordinary child workflows was rejected by the owner:
hundreds of helper workflows would fill the list a person browses.

Before this decision a bundled workflow could not call another extension at all:
a bundle carries no install id, and the runtime refuses an extension named
without one.

## Decision

A **composite action** is an extension action implemented by a workflow of the
same bundle.

### Declaration

The action definition says `handler: {mode: workflow, workflow_id: <local id>}`.
The workflow carries `settings.action_implementation` with the `action_id` it
implements and its connection slots (`slot`, `extension_id`,
`connection_type_ids`). The two must name each other.

The action stays a workflow-visible action with its Contract V2 operation. The
operation's inputs and outputs must equal the workflow's Interface
(`settings.callable.expected_inputs` / `expected_outputs`), and it declares no
`connection_type_ids`: the slots are the connections.

The body of an implementation is checked when the release is built:

- it is callable and allows only the `parent_workflow` trigger channel;
- it declares no hook;
- an Invoke step is an action Invoke only, of this extension or of a required
  dependency; a plain Invoke of a workflow is refused, because that workflow
  could hold a hook;
- a connector step names this extension or a required dependency by package id,
  never by an install id;
- a connector step reads its connection from a declared slot, never from a fixed
  connection id.

### The step a person adds

In the builder it is an action of the extension. In the definition it is
`step_kind: invoke_workflow` whose config holds:

- `workflow_id`: the implementation, `extension.<install_id>.<local id>`;
- `action`: `extension_id`, `install_id`, `action_id`;
- `installs`: the install of each required dependency;
- `connections`: one `connection_id` per slot;
- the usual `input_mapping`, `output_mapping`, `artifact_mapping`.

`workflow_id` is stored because dependency rows, where-used, the consumer guard,
recursion and depth checks and save-time validation all key on the direct child
id. The host verifies, at save and at execution, that the action resolves to
that workflow. `action`, `hook` and a plain target are mutually exclusive ways
to use an Invoke; an action never stands next to a hook.

### Execution

An action Invoke is an ordinary Invoke of the implementation. When the host
creates the child job it writes the action context into the child's `jobs.meta`:
the installs and the connection of each slot. A connector step of the child
reads its install and its slot's connection from that context. Author input and
workflow state never carry it.

A nested action Invoke inside an implementation passes its caller's slots on.
An action of another extension is named by the action alone; its workflow is
resolved in the install the caller chose.

The version rule is the existing child-workflow rule. The definition is selected
when the child job is created. Resume and redelivery of the same step entry keep
that child and its definition. A new entry selects the current definition, so a
long For each may run two versions. Extension code is never pinned.

### What an installed implementation is not

It is not a workflow a person browses, links to a project, edits, exports on its
own, connects to a hook or launches. It is projected with
`visible_in_catalog: false`, and:

- one gate at job enqueue, which every launch passes, admits a job of an
  implementation only with the action context the Invoke runtime writes; a test
  run, a restart, a webhook or a schedule cannot start one;
- the Invoke runtime refuses to create a child of an implementation unless the
  step's action resolves to it, whatever the saved definition says;
- the action itself cannot be called outside a workflow: the action endpoint, a
  command, a raw invoke and an agent tool are refused with
  `composite_action_direct_call_forbidden`.

### Authoring

Marking a workflow as an implementation requires a Development environment, the
same rule as declaring a hook. Until it ships in a bundle it is the author's own
workflow: editable and testable like any other. A connector step there names the
slot it will read and, for testing, a connection; a bundle never carries the
connection id.

### Trust

`runtime_grants` are unchanged: an extension still gets rights over its own data
only. The add-on never sees the base extension's connection or secrets. The base
extension's own runtime reads them when the host calls its action, as it does
for any connector step. The person chooses which connection each slot holds.

## Consequences

- A bundled workflow can call a required dependency. Its install is chosen by
  the caller, not baked into the bundle.
- An add-on update changes what new entries of the step run without the person
  saving anything. An update that breaks active callers must be refused through
  the existing consumer guard.
- A composite action works only as a workflow step. Direct calls from MCP,
  agents or extension pages are refused.
- Payment for an add-on is not part of this decision.
- The runtime-to-host call channel is not built.
