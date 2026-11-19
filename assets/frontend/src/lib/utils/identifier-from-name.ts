/**
 * An identifier from a display name: "Order Sync (EU)" reads "order_sync_eu".
 * The result matches `[a-z][a-z0-9_]*`, the rule for workflow, agent, tool and
 * skill IDs. Letters outside a-z are dropped, so a name in another script
 * yields "" and the user types the ID.
 */
export function identifierFromName(name: string): string {
  // NFKD splits "é" into "e" and a combining accent; drop the accent so it
  // does not break the word ("résumé" reads "resume", not "re_sume").
  const plain = name.normalize("NFKD").replace(/\p{M}/gu, "");
  const words = plain.toLowerCase().split(/[^a-z0-9]+/u).filter(Boolean);
  const slug = words.join("_").replace(/^[\d_]+/u, "");
  let end = Math.min(slug.length, 64);
  while (end > 0 && slug[end - 1] === "_") end -= 1;
  return slug.slice(0, end);
}
