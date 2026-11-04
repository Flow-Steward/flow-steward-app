import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";

/**
 * One key/value row. A header row may send a stored secret instead of a typed value:
 * `secretId` names the catalog secret and `prefix` is the literal sent before it
 * (`"Bearer "` for Authorization). Such a row carries no value.
 */
export type KeyValueRow = { id: number; key: string; value: string; secretId?: string; prefix?: string };

const KEY_VALUE_KEY_MAX_LENGTH = 128;
const KEY_VALUE_VALUE_MAX_LENGTH = 512;
export const HEADER_SECRET_PREFIX_MAX_LENGTH = 32;

export type HeaderSecretChoice = { secret_id: string; prefix: string };

/** The prefix a newly chosen stored secret starts with: "Bearer " for Authorization. */
export function defaultHeaderSecretPrefix(headerName: string): string {
  return headerName.trim().toLowerCase() === "authorization" ? "Bearer " : "";
}

/** The row with a stored secret chosen (or, with "", the choice removed). */
export function withHeaderSecretChoice(row: KeyValueRow, secretId: string): KeyValueRow {
  if (!secretId) return { ...row, secretId: "" };
  return { ...row, secretId, value: "", prefix: defaultHeaderSecretPrefix(row.key) };
}

/**
 * The header a stored secret sends, set by the system as the HTTP step's bearer mode
 * does: Authorization gets "Bearer " before the secret, any other header the secret.
 */
export function headerSecretPrefixFor(row: KeyValueRow): string {
  return row.secretId ? defaultHeaderSecretPrefix(row.key) : row.prefix ?? "";
}

export function headerSecretHelper(row: Pick<KeyValueRow, "key">, t: I18nContextValue["t"] = fallbackContext.t): string {
  return defaultHeaderSecretPrefix(row.key)
    ? t("react.registry.mcp_header_secret_bearer_helper", "Sent as “Bearer” and the stored secret: keep only the token in the secret. The value never reaches the browser.")
    : t("react.registry.mcp_header_secret_value_helper", "Sent as the stored secret's value. The value never reaches the browser.");
}

export const RESERVED_OAUTH_PARAMETER_IDENTITIES = new Set([
  "responsetype",
  "clientid",
  "redirecturi",
  "state",
  "codechallenge",
  "codechallengemethod",
  "scope",
  "resource",
  "clientsecret",
  "authorization",
  "apikey",
  "password",
  "secret",
  "token",
  "idtoken",
  "accesstoken",
  "refreshtoken",
  "code",
  "codeverifier",
  "clientassertion",
  "clientassertiontype",
]);

const OAUTH_CREDENTIAL_MARKERS = [
  "bearer",
  "credential",
  "secret",
  "password",
  "passwd",
  "token",
  "apikey",
  "privatekey",
];

function oauthParameterIdentity(value: string): string {
  return value.replaceAll(/[^a-zA-Z0-9]/g, "").toLowerCase();
}

// Vendor parameters whose names contain a credential marker but carry no
// secret: Dropbox's token_access_type=offline asks for a refresh token.
const SAFE_OAUTH_PARAMETER_IDENTITIES = new Set(["tokenaccesstype"]);

function oauthParameterIsReserved(value: string): boolean {
  const identity = oauthParameterIdentity(value);
  if (SAFE_OAUTH_PARAMETER_IDENTITIES.has(identity)) return false;
  return RESERVED_OAUTH_PARAMETER_IDENTITIES.has(identity)
    || OAUTH_CREDENTIAL_MARKERS.some((marker) => identity.includes(marker));
}

export function keyValueRowProblem(
  rows: KeyValueRow[],
  index: number,
  label: string,
  reservedKeys?: ReadonlySet<string>,
  t: I18nContextValue["t"] = fallbackContext.t,
): string {
  const row = rows[index];
  const key = row.key.trim();
  const value = row.value.trim();
  const chosen = Boolean(row.secretId);
  const hasValue = chosen || Boolean(value);
  if (!key && chosen) return t("react.registry.mcp_header_secret_key_required", "Enter the header name for the stored secret.");
  if ((!key && hasValue) || (key && !hasValue)) return t("react.registry.key_value_pair_required", "{label} rows require both a key and value.", { label });
  if (!key && !hasValue) return "";
  const normalized = reservedKeys ? oauthParameterIdentity(key) : key.toLowerCase();
  if (key.length > KEY_VALUE_KEY_MAX_LENGTH) return t("react.registry.key_value_key_limit", "{label} keys must be 128 characters or fewer.", { label });
  if (!chosen && value.length > KEY_VALUE_VALUE_MAX_LENGTH) return t("react.registry.key_value_value_limit", "{label} values must be 512 characters or fewer.", { label });
  if (chosen && (row.prefix ?? "").length > HEADER_SECRET_PREFIX_MAX_LENGTH) return t("react.registry.mcp_header_secret_prefix_limit", "{label} prefixes must be 32 characters or fewer.", { label });
  if (reservedKeys && oauthParameterIsReserved(key)) return t("react.registry.key_value_reserved", "{label} key is reserved and cannot be overridden.", { label });
  if (rows.findIndex((candidate) => {
    const candidateKey = candidate.key.trim();
    return (reservedKeys ? oauthParameterIdentity(candidateKey) : candidateKey.toLowerCase()) === normalized;
  }) !== index) {
    return t("react.registry.key_value_unique", "{label} keys must be unique.", { label });
  }
  return "";
}

export function keyValueObject(
  rows: KeyValueRow[],
  label: string,
  reservedKeys?: ReadonlySet<string>,
  t: I18nContextValue["t"] = fallbackContext.t,
): Record<string, string> {
  const result: Record<string, string> = {};
  for (const [index, row] of rows.entries()) {
    const key = row.key.trim();
    const value = row.value.trim();
    if (!key && !value && !row.secretId) continue;
    const problem = keyValueRowProblem(rows, index, label, reservedKeys, t);
    if (problem) throw new Error(problem);
    if (row.secretId) continue;
    result[key] = value;
  }
  return result;
}

/** `{header: {secret_id, prefix}}` of the rows that send a stored secret; validated like keyValueObject. */
export function keyValueSecretChoices(
  rows: KeyValueRow[],
  label: string,
  t: I18nContextValue["t"] = fallbackContext.t,
): Record<string, HeaderSecretChoice> {
  const result: Record<string, HeaderSecretChoice> = {};
  for (const [index, row] of rows.entries()) {
    const problem = keyValueRowProblem(rows, index, label, undefined, t);
    if (problem) throw new Error(problem);
    if (!row.secretId) continue;
    result[row.key.trim()] = { secret_id: row.secretId, prefix: headerSecretPrefixFor(row) };
  }
  return result;
}
