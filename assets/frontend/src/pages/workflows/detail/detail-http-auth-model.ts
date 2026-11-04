/**
 * The auth reference of an HTTP step that names workflow secret inputs:
 * `{{ state.inputs.<name> }}` for bearer, `<username>:<password>` of two such
 * templates for basic. The server resolves each one as an opaque secret input.
 */

const INPUT_TEMPLATE = /^\{\{\s*state\.inputs\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}$/;

export function secretInputAuthRef(name: string): string {
  return name ? `{{ state.inputs.${name} }}` : "";
}

export function inputNameFromAuthPart(part: string): string {
  return INPUT_TEMPLATE.exec(part.trim())?.[1] ?? "";
}

/** `username:password`, split on the first colon outside `{{ }}`, as the server does. */
export function splitBasicAuthRef(ref: string): [string, string] {
  let depth = 0;
  for (let index = 0; index < ref.length; index += 1) {
    if (ref.startsWith("{{", index)) depth += 1;
    else if (ref.startsWith("}}", index) && depth) depth -= 1;
    else if (ref[index] === ":" && depth === 0) return [ref.slice(0, index).trim(), ref.slice(index + 1).trim()];
  }
  return [ref.trim(), ""];
}

export function basicAuthRef(username: string, password: string): string {
  return password ? `${username}:${password}` : username;
}

/** A free input name for a stored secret, derived from its name: `Shop API key` → `shop_api_key`. */
export function storedSecretInputName(secretName: string, taken: string[]): string {
  const base = secretName.toLowerCase().normalize("NFKD").replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "") || "secret";
  const stem = (/^[a-z_]/.test(base) ? base : `secret_${base}`).slice(0, 56);
  const used = new Set(taken);
  if (!used.has(stem)) return stem;
  let suffix = 2;
  while (used.has(`${stem}_${suffix}`)) suffix += 1;
  return `${stem}_${suffix}`;
}
