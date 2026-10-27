import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";

export type KeyValueRow = { id: number; key: string; value: string };

const KEY_VALUE_KEY_MAX_LENGTH = 128;
const KEY_VALUE_VALUE_MAX_LENGTH = 512;

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
  if ((!key && value) || (key && !value)) return t("react.registry.key_value_pair_required", "{label} rows require both a key and value.", { label });
  if (!key && !value) return "";
  const normalized = reservedKeys ? oauthParameterIdentity(key) : key.toLowerCase();
  if (key.length > KEY_VALUE_KEY_MAX_LENGTH) return t("react.registry.key_value_key_limit", "{label} keys must be 128 characters or fewer.", { label });
  if (value.length > KEY_VALUE_VALUE_MAX_LENGTH) return t("react.registry.key_value_value_limit", "{label} values must be 512 characters or fewer.", { label });
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
    if (!key && !value) continue;
    const problem = keyValueRowProblem(rows, index, label, reservedKeys, t);
    if (problem) throw new Error(problem);
    result[key] = value;
  }
  return result;
}
