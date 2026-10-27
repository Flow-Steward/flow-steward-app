/**
 * The workspace mark's letter (canon navigation/sidebar.md, "The workspace
 * mark"): one character, the first of the name that has a letter form, upper
 * case. A name with no letter at all falls back to its first character as
 * written rather than to an invented placeholder.
 */
export function workspaceInitial(name: string): string {
  const trimmed = name.trim();
  const letter = /\p{L}/u.exec(trimmed)?.[0];
  if (letter) return letter.toLocaleUpperCase();
  return Array.from(trimmed)[0] ?? "";
}
