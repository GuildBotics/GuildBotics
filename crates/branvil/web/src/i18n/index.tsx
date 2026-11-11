// A small i18n layer: two dictionaries with identical keys (checked by the
// type of `ja` and by i18n.test.ts) and `{name}` interpolation.

import { useCallback, useMemo, useState, type ReactNode } from "react";
import { I18nextProvider, useTranslation } from "react-i18next";
import { createAppI18n } from "@guildbotics/ui/i18n";
import { en, type MessageKey } from "./en";
import { ja } from "./ja";

export type Language = "en" | "ja";
export const dictionaries: Record<Language, Record<MessageKey, string>> = { en, ja };
export type { MessageKey };

export type Translate = (key: MessageKey, params?: Record<string, string | number>) => string;

export function detectLanguage(languages: readonly string[] = navigator.languages ?? [navigator.language]): Language {
  for (const l of languages) {
    const base = l.toLowerCase().split("-")[0];
    if (base === "ja") return "ja";
    if (base === "en") return "en";
  }
  return "en";
}

export function format(template: string, params?: Record<string, string | number>): string {
  if (!params) return template;
  return template.replace(/\{(\w+)\}/g, (m, name: string) => (name in params ? String(params[name]) : m));
}

export function I18nProvider({ children, initial }: { children: ReactNode; initial?: Language }) {
  const [{ i18n }] = useState(() => createAppI18n({
    resources: { en: { translation: en }, ja: { translation: ja } },
    initialLanguage: initial,
    flat: true,
  }));
  return <I18nextProvider i18n={i18n}>{children}</I18nextProvider>;
}

export function useI18n() {
  const { i18n, t: translate } = useTranslation();
  const lang = i18n.language as Language;
  const setLang = useCallback((next: Language) => {
    void i18n.changeLanguage(next);
  }, [i18n]);
  const t = useCallback<Translate>((key, params) => translate(key, params), [translate]);
  return useMemo(() => ({ lang, setLang, t }), [lang, setLang, t]);
}

export function useT(): Translate {
  return useI18n().t;
}
