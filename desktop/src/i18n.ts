import { createInstance } from "i18next";
import { initReactI18next } from "react-i18next";
import { readStored, writeStored } from "./ui/storage";
import { shared } from "./sharedLocales";
import { en } from "./repository/i18n/en";
import { ja } from "./repository/i18n/ja";
import { setApiLanguage } from "./api/client";
import { resources } from "./locales";
import { providerResources } from "./setup/providers/resources";

export type AppLanguage = "en" | "ja";

export function normalizeLanguage(value: string | null | undefined): AppLanguage | null {
  const locale = value?.toLowerCase();
  if (locale?.startsWith("ja")) return "ja";
  if (locale?.startsWith("en")) return "en";
  return null;
}

const LANGUAGE_KEY = "guildbotics.appLanguage";

export function getInitialAppLanguage(): AppLanguage {
  return (
    normalizeLanguage(readStored(LANGUAGE_KEY)) ?? normalizeLanguage(navigator.language) ?? "en"
  );
}

for (const provider of providerResources) {
  for (const language of ["en", "ja"] as const) {
    mergeInto(resources[language].desktop, provider[language]);
  }
}

function mergeInto(target: Record<string, unknown>, source: Record<string, unknown>) {
  for (const [key, value] of Object.entries(source)) {
    const current = target[key];
    if (typeof value === "object" && typeof current === "object" && current) {
      mergeInto(current as Record<string, unknown>, value as Record<string, unknown>);
    } else {
      target[key] = value;
    }
  }
}

export const i18n = createInstance();
void i18n.use(initReactI18next).init({
  resources: {
    en: { ...resources.en, shared: shared.en, repository: en },
    ja: { ...resources.ja, shared: shared.ja, repository: ja },
  },
  lng: getInitialAppLanguage(),
  ns: ["desktop", "repository", "shared"],
  defaultNS: "desktop",
  fallbackNS: "shared",
  fallbackLng: "en",
  interpolation: { escapeValue: false },
});

document.documentElement.lang = i18n.language;
setApiLanguage(i18n.language);
i18n.on("languageChanged", (language) => {
  writeStored(LANGUAGE_KEY, language);
  document.documentElement.lang = language;
  setApiLanguage(language);
});

export async function setAppLanguage(language: AppLanguage) {
  await i18n.changeLanguage(language);
}

export default i18n;
