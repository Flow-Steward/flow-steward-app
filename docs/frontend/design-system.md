# Design-system reference

The vendored design-system snapshot lives under
`assets/frontend/src/design-system/`. Its upstream repository, immutable tag,
commit and copied paths are recorded in [the source lock](design-system-source.json).
That lock is provenance, not a second stylesheet or a task/evidence archive.
Implementation rules remain in the [frontend standard](../standards/frontend-standard.md).

Before porting Workflow Builder design, read the
[Builder design contract](workflow-builder-design-contract.md) for shared domain
terms, composition, action flows and design-source rulings. Scoped task ownership
and product acceptance evidence remain separate from this reference.

## Shared semantic patterns

- A badge represents state; `DsValueChip`/`.fs-value-chip` represents a reference
  value such as an identifier, transport or authentication scheme. Do not restore
  the withdrawn `ink` badge tone for values.
- Inline machine text uses `code.fs-code-value`, not the proportional data face
  used for URLs/paths. Keep this shared role instead of copying declarations per page.
- Disabled action roles use a neutral surface, border and tertiary text treatment;
  pages must not invent separate disabled primary/accent/danger colors.
- `DsSelect` opens a value list with a chevron; comboboxes filter it. The Base skill
  selector deliberately uses `DsSelect`. Revisit the choice when enabled choices
  exceed about 30 or duplicate display names make selection ambiguous; do not
  silently combine unrelated controls per page.
- A bordered surface around a checkbox does not automatically extend its hit
  target beyond the label. Make interaction affordance match the actual target.
- Multi-line selectable rows are local compositions unless the shared component
  contract publishes them. Reuse an existing composition rather than overriding
  canonical select-option borders with equal-specificity utility classes.

## Verification cautions

For focus traps and other positional multi-selector DOM queries, normalize to
document order when the test DOM does not preserve it. Browser behavior is the
acceptance reference; do not weaken focus assertions to accommodate a test artifact.

Contrast measurement must handle `color-mix(...)` and composited alpha, not just
parse an `rgb()` string. A canvas-normalized color probe avoids silently skipping
these tokens. Check success badges on their actual dark/inset backgrounds rather
than assume semantic color names guarantee readable contrast.

Verification screenshots belong under ignored `logs/` or `audit-artifacts/`.
Open defects and upstream promotion tasks belong in the issue tracker; neither
dated measurements nor completed transfer reports are product documentation.
