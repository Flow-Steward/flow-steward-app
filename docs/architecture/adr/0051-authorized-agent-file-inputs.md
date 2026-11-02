# 0051: Authorized file inputs for agent steps

Status: Proposed

## Context

An upstream connector can return a durable artifact handle, while an ordinary
agent input mapping supplies only a string. Ordinary prompt input summaries are
bounded and therefore cannot represent a complete parsed document. Media also
requires a processing tool or a model transport that understands its content.
A handle in authored state does not establish authority to read an object.

## Decision

Agent steps select files with `override.agent.file_bindings`, a list of named
`state.<path>` selectors. Existing ordinary input mappings retain their behavior.
The nested override uses the existing persistence and portable workflow envelope;
it does not introduce a database column or migration.

The host resolves each selection against canonical artifact metadata and an
invocation-local owner restored independently from the durable job and project.
Account, project and job must match. Runtime objects and file bytes are never
stored in checkpointed workflow state. A child invocation establishes its own
owner; a string naming a parent job does not grant access to parent artifacts.
Returned child files already adopted by the parent use the parent's ownership.

Model-facing file operations address selected names, never arbitrary artifact
identifiers or storage URLs. Authorization, availability, expiry, declared size
and checksum are checked before content is exposed. Text, JSON and Markdown use
bounded UTF-8 pages with explicit continuation and EOF rather than ordinary
clipped prompt summaries. Count, file size, selected total size, page size and
cumulative read budgets are finite and documented in the user guide. Model
context and iteration limits still constrain how much a run can process.

Native media messages require both an explicitly true SDK model profile flag
and a provider adapter whose content-block transport supports that modality.
Unknown profiles fail closed. Current native paths target image/audio inputs for
compatible OpenAI models, image inputs for compatible Anthropic models, and
image/audio/video inputs for compatible built-in pinned Google Gemini models.
The separate Vertex provider extension remains text-only. Model names are not used as capability
heuristics, and provider extensions do not inherit SDK adapter capabilities.
Bounded bytes are authorized before base64 message construction. File bodies
must not be persisted by prompt traces or model callbacks.

Existing media operations in OpenAI Tools are workflow connector actions. They
require their existing connection, grant and durable external-effect lifecycle;
the generic agent-tool wire is a different protocol. Unsupported media must be
processed in an appropriate preceding connector step, then its text result may
be mapped into the agent. This decision does not add action-to-agent-tool
projection or claim metadata alone constitutes media understanding.

## Consequences

The same input mechanism serves parsed documents, images, audio and video without
connector-specific Core branches. Existing text-only provider extensions remain
text-only. Large-file processing can need dedicated tools even when a file fits
storage limits. Foreign-job handles fail closed unless a separate durable
adoption or ownership mechanism has established access.

## References

- [Workflow user guide](../../user/workflows.md)
- [Files returned from steps](0039-workflow-files-returned-from-steps.md)
- [Atomic extension artifact lifecycle](0015-atomic-extension-package-and-artifact-lifecycle.md)
