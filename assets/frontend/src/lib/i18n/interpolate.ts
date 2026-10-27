import type { TranslateParams } from "@/lib/i18n/context";
import { replaceDelimitedTokens } from "@/lib/utils/template-tokens";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";

export function interpolate(template: string, params: TranslateParams | undefined): string {
  if (!params || Object.keys(params).length === 0) {
    return template;
  }
  return replaceDelimitedTokens(template, "{", "}", (key) => {
    const value = params[key];
    return value === undefined || value === null ? `{${key}}` : stringFromUnknownValue(value);
  });
}
