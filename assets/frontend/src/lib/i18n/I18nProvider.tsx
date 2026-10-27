import { useCallback, useEffect, useMemo, type PropsWithChildren } from "react";

import { I18nContext, fallbackSupportedLocales, type TranslateParams } from "@/lib/i18n/context";
import { useAuth } from "@/lib/auth/useAuth";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";
import { interpolate } from "@/lib/i18n/interpolate";

const EMPTY_MESSAGES: Record<string, string> = {};

export function I18nProvider({ children }: Readonly<PropsWithChildren>) {
  const auth = useAuth();
  const bootstrap = auth.session?.i18n;
  const locale = firstStringFromUnknownValue([bootstrap?.locale, "en"]);
  const fallbackLocale = firstStringFromUnknownValue([bootstrap?.fallback_locale, "en"]);
  const supportedLocales = Array.isArray(bootstrap?.supported_locales)
    ? bootstrap.supported_locales
    : fallbackSupportedLocales;
  const messages = bootstrap?.messages && typeof bootstrap.messages === "object" ? bootstrap.messages : EMPTY_MESSAGES;

  useEffect(() => {
    document.documentElement.lang = locale;
  }, [locale]);

  const t = useCallback((key: string, defaultText = "", params?: TranslateParams) => {
    const normalizedKey = firstStringFromUnknownValue([key, ""]).trim();
    const template = firstStringFromUnknownValue([messages[normalizedKey], defaultText, normalizedKey]);
    return interpolate(template, params);
  }, [messages]);
  const contextValue = useMemo(
    () => ({
      locale,
      fallbackLocale,
      supportedLocales,
      messages,
      t,
    }),
    [fallbackLocale, locale, messages, supportedLocales, t],
  );

  return (
    <I18nContext.Provider value={contextValue}>
      {children}
    </I18nContext.Provider>
  );
}
