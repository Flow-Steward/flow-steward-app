# ADR 0039: Files Returned From Steps, Not Declared in the Interface

- Status: Accepted
- Date: 2026-10-22
- Amends: the workflow interface contract (`settings.callable`), ADR 0035 (Invoke results)

## Context

The workflow interface had four parts: Inputs, Outputs, Artifacts and External
Changes. Artifacts were declared by hand (`settings.callable.expected_artifacts`)
and filled at run end from whatever state, message or terminal payload carried a
key with the same name. External Changes (`side_effects`) were descriptive rows
that changed nothing at runtime, but an `output_mode` of `side_effect` and its
validation warning depended on them.

Both duplicated what the steps already configure. An artifact declaration could
not name the step whose file it meant, so a file name or a stray message key
decided what a parent received, and a file whose name carried a timestamp could
not be mapped at all. The owner's request is R2-18 (a planning note under the
untracked `logs/` directory, not part of the repository).

## Decision

**The setting lives on the step result.** A step's file result carries "Return
from workflow" in `override.returned_files`: one entry per ticked result,
`{"result": <result id>, "key": <stable key>}` (`"required": true` only for a
result linked from an old declaration). Result ids: `file` (Save file from
base64), `response` (HTTP request saved as a file), the output name (connector
or agent output mapping), the child's returned-file key (Invoke returned-file
mapping). No schema column is added; `override` already holds per-step settings.

**The key is made once.** The editor creates it when the result is ticked, from
the result's variable (`state.invoice_pdf` → `invoice_pdf`), unique among the
workflow's outputs. It never depends on the file name, a timestamp or one run's
file, and survives a step rename or a changed variable.

**The contract is derived, never edited.** `WorkflowInterface.returned_files` is
computed from the definition's steps (`core/application/registry/workflow_returned_files.py`)
wherever the interface is read with its definition (`interface_from_definition`):
the editor's contract, readiness and Invoke validation, the callable list, the
canvas graph, the archived job summary, the pinned run definition and the child
an Invoke step calls. `settings.callable` no longer stores file outputs or side
effects, and `output_mode` is derived from the two lists. Outputs shows returned
files read only with their source; there is no second editable list.

**The file is captured from the step that produced it.** After a step executes
(`execute_step_node`, the Invoke node, and so every parallel lane), its own state
update is read at each ticked result's variable and copied into
`state.artifacts[<key>]` (`returned_files_runtime.with_returned_files`). Two steps
writing one variable, two lanes or two branches never overwrite each other's
returned file; parallel lanes merge `artifacts` by key. `state.artifacts` is a
reserved root no author step can write. At run end the boundary validation reads
returned files only from `state.artifacts` — never from a message or a variable
that shares the name. A list is returned whole.

**Child → parent uses the existing mechanism.** The child's final contract lists
its returned files; the parent's Invoke `artifact_mapping` maps a key to a parent
variable. The parent adopts every file (each element of a collection) and gets
the handle as the child produced it; the Invoke result records each file's id,
name, type, size and digest. A missing returned file follows the existing
required rules; ticking alone does not make it required. A stale parent mapping
is reported by the existing Invoke validation (`invoke.artifact_mapping_unknown_key`
/ `invoke.artifact_mapping_unused`).

**Existing workflows.** A stored `expected_artifacts` declaration is linked, at
read time, to the one result it can only mean: among steps that never had the
setting, the result saved to `state.<name>` or the output named `<name>`. It is
returned under its old name, so parent mappings keep working. No match or more
than one match is a setup issue (`workflow.interface.legacy_artifact_unlinked` —
an error when the declaration was required — or
`workflow.interface.legacy_artifact_ambiguous`), never a guess. The first change
to that step's setting writes the linked result into the step. Stored
`side_effects` and `output_mode` are ignored. This is the only compatibility
path, as the request asks; there is no alias for the removed fields.

## Consequences

- The interface has two tabs, Inputs and Outputs; the builder's Stop card lists
  returned files with the outputs. Notifications, approvals and error handling
  are unaffected: they never depended on the declarations.
- A ticked result whose source no longer saves a file is a setup issue
  (`workflow.interface.returned_file_source_missing`) until it is unticked.
- Connector and agent outputs are offered for "Return from workflow" only when
  the extension catalog declares a file type; the server accepts any mapped
  output that is ticked.
- Converting a Group to a child workflow refuses while a member step returns a
  file from the parent.
