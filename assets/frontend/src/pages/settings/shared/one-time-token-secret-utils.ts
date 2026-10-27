import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";
type TokenPayload = Record<string, unknown>;

function objectOrEmpty(value: unknown): TokenPayload {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return value as TokenPayload;
  }
  return {};
}

export function extractOneTimeTokenSecret(payload: unknown): string {
  const root = objectOrEmpty(payload);
  const token = objectOrEmpty(root.token);
  return firstStringFromUnknownValue([token.raw_token, token.token, token.secret, token.value, root.raw_token, root.token_value, root.secret, ""]).trim();
}

export function extractOneTimeTokenLabel(payload: unknown, fallback: string, t: I18nContextValue["t"] = fallbackContext.t): string {
  const root = objectOrEmpty(payload);
  const token = objectOrEmpty(root.token);
  return firstStringFromUnknownValue([token.label, root.label, fallback, t("react.settings.token", "Token")]).trim();
}

export function extractOneTimeTokenId(payload: unknown): string {
  const root = objectOrEmpty(payload);
  const token = objectOrEmpty(root.token);
  return firstStringFromUnknownValue([root.id, root.token_id, token.id, token.token_id, ""]).trim();
}
