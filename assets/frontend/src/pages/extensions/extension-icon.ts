/**
 * Where Flow Steward serves an installed extension's logo. Projections carry a
 * versioned `icon_url`; this is for places that only know the extension id
 * (a workflow step). A missing logo answers 404 and the tile shows its glyph.
 */
export function extensionIconUrl(extensionId: string): string {
  return `/api/extensions/${encodeURIComponent(extensionId)}/icon`;
}
