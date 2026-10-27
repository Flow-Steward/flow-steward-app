import { createContext } from "react";

import type { I18nBootstrap } from "@/lib/auth/AuthProvider";
import { interpolate } from "@/lib/i18n/interpolate";

export type TranslateParams = Record<string, string | number | boolean | null | undefined>;

export type I18nContextValue = {
  locale: string;
  fallbackLocale: string;
  supportedLocales: I18nBootstrap["supported_locales"];
  messages: Record<string, string>;
  t: (key: string, defaultText?: string, params?: TranslateParams) => string;
};

export const fallbackSupportedLocales = [{ code: "en", label: "English", native_label: "English" }];

export const fallbackContext: I18nContextValue = {
  locale: "en",
  fallbackLocale: "en",
  supportedLocales: fallbackSupportedLocales,
  messages: {},
  t: (key, defaultText, params) => interpolate(defaultText || key, params),
};

export const I18nContext = createContext<I18nContextValue>(fallbackContext);
