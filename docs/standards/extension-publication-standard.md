# Extension Publication Standard

This is the canonical branding requirement for publishing Flow Steward extensions
and their Marketplace cards. It applies to first releases and subsequent updates.
The catalog README owns catalog submission mechanics; the
[manifest standard](extension-manifest-standard.yaml) owns the bundle format.

## Official artwork

- An extension integrating a named external product or provider `MUST` ship that
  product's original official logo as `icon.png` next to `extension.yaml` before
  publication. Obtain artwork from the provider's official website, brand kit,
  or provider-owned repository and follow its applicable usage requirements.
- Scaffold package glyphs are development placeholders for branded integrations.
  AI-generated, redrawn, approximate, or third-party icon-library substitutes
  `MUST NOT` be published as the provider's official logo.
- Preserve the artwork's colors, proportions, strokes, details, and required
  clear space. Proportional resizing, transparent padding, and faithful SVG-to-PNG
  conversion are allowed. Prefer a suitable high-resolution official source over
  enlarging a small favicon. The packaged PNG `MUST` meet the manifest standard's
  `x-validator-contract.bundle_icon` requirements.
- For a product suite, use its official suite mark. For an integration covering
  multiple brands without a suite mark, use separate, unaltered official marks
  with their required spacing; do not invent a joint logo.
- Generic protocol, local, and reference extensions without an external brand
  `MAY` use the extension owner's mark. For example, generic IMAP does not imply
  any particular email provider. Do not invent an official protocol logo.
- If an official asset suitable for the intended use cannot be verified, stop
  publication and report the missing asset. Do not silently substitute a placeholder.

## Bundled provenance

The release bundle `MUST` include `BRANDING.md` (or an existing
`BRAND-ASSETS.md`) recording:

- The brand and official source URL, including the exact asset or archive member.
- SHA-256 of the source artwork and the final `icon.png`.
- Any resizing, padding, conversion, or composition performed.
- Applicable attribution and artwork usage requirements, with their source link.

For multiple marks, record each source separately. Repository-only notes may
supplement this record but do not replace bundled provenance or attribution.

## Publication review

Before publishing a release, the publisher `MUST`:

1. Compare the final PNG visually with the official source. Check for lost SVG
   strokes, missing details, distortion, clipping, incorrect colors, and blur.
2. Inspect the icon at catalog-card size and on the card backgrounds. Padding
   and transparency must preserve a legible, recognizable mark.
3. Verify that every target release archive contains the approved, identical
   `icon.png` and its provenance record, with matching recorded hashes.
4. Run the existing bundle and catalog validation. PNG validation checks file
   format and dimensions; it does **not** establish official origin or visual
   fidelity. A passing validator does not replace this review.
5. After catalog generation, verify the published entry's icon hash against the
   approved PNG and inspect the rendered Marketplace card.

Record the source, visual review, archive checks, and final catalog verification
in publication evidence. Branding checks require no paid provider API calls or
changes to a demo installation.

Changing an already published icon `MUST` use a new patch version (or accompany
a higher version release). Never replace immutable release tags or assets.
Use the catalog's generator for index and release records; do not edit generated
catalog data manually.

## Enforcement

Official origin, usage requirements, and visual fidelity are publication review
requirements owned by the publisher and release reviewer. Existing automated
bundle validators enforce the technical PNG contract only. Authors and publishing
agents `MUST` complete this review before treating a branded release as ready.
