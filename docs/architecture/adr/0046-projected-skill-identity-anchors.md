# ADR 0046: Identity anchors for installed projected skills

- Status: Accepted by project owner for the bounded SURFAGENT001 correction
- Date: 2026-10-28
- Related: ADR 0015, ADR 0016

## Context

An installed extension agent can refer to a skill that exists only in its active
registry projection. A user-created clone needs a durable foreign-key relation
to that skill so the registry can preserve the clone's inherited structured
contract. Copying the skill's executable instructions or prompts into a manual
registry row would duplicate install-owned executable content and could leave it
usable after the extension is disabled.

## Decision

The host may create a disabled `registry_skills` identity anchor for an active
projected skill when a manual agent clone needs a durable reference. The anchor
is owned by the exact install and account, is collision-checked under row lock,
and stores metadata plus declared structured inputs and outputs only. It does
not store instructions, prompts, examples, tools, or executable configuration.
Anchor creation and the referencing agent clone share one database transaction.

An active projection for the same account, install, and skill ID is the runtime
content. Skill lookup resolves the anchor through that active projection; an
inactive or missing projection makes the skill unavailable. Package lifecycle
materialization replaces the anchor's declared I/O, including explicit empty
lists, and refreshes affected manual agents' inherited contracts. Install-owned
anchor rows are hidden from the raw skill catalog. No account or project scope
is inferred from the anchor.

Disabling an install deactivates its projection, clears declarative I/O from
retained anchors, and refreshes affected manual agent contracts. Readiness and
compatibility checks derive the account from the validated agent row or accept
an explicit validated account; an account-A lookup cannot use an account-B
anchor.

Uninstall planning blocks while same-account manual agents retain a relation to
an install-owned skill anchor. After a successful uninstall, unreferenced
anchors are removed; referenced rows remain non-executable and unavailable.
No schema change or copied extension source is introduced.

## Verification boundary

The owning PostgreSQL integration tests exercise active virtual-skill cloning,
I/O preservation and release refresh, collision refusal, rollback after agent
contract materialization, inactive projection resolution, account isolation,
and disabled-to-enabled lifecycle activation with rollback after anchor
materialization. Gate results are reported separately and this ADR does not
claim they have passed.
