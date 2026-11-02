// A small i18n layer: two dictionaries with identical keys (checked by the
// type of `ja` and by i18n.test.ts) and `{name}` interpolation.

import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";
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

interface I18nValue {
  lang: Language;
  setLang: (lang: Language) => void;
  t: Translate;
}

const I18nContext = createContext<I18nValue | null>(null);

export function I18nProvider({ children, initial }: { children: ReactNode; initial?: Language }) {
  const [lang, setLangState] = useState<Language>(() => initial ?? detectLanguage());
  const setLang = useCallback((next: Language) => {
    setLangState(next);
    document.documentElement.lang = next;
  }, []);
  const t = useCallback<Translate>((key, params) => format(dictionaries[lang][key], params), [lang]);
  const value = useMemo(() => ({ lang, setLang, t }), [lang, setLang, t]);
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nValue {
  const value = useContext(I18nContext);
  if (!value) throw new Error("useI18n outside I18nProvider");
  return value;
}

export function useT(): Translate {
  return useI18n().t;
}
