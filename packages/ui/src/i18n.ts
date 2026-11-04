import { createInstance, type Resource } from "i18next";
import { initReactI18next } from "react-i18next";

export type AppLanguage = "en" | "ja";
export function normalizeLanguage(value: string | null | undefined): AppLanguage | null {
  if (!value) {
    return null;
  }
  const normalized = value.toLowerCase();
  if (normalized.startsWith("ja")) {
    return "ja";
  }
  if (normalized.startsWith("en")) {
    return "en";
  }
  return null;
}

export function createAppI18n({
  resources,
  storageKey,
}: {
  resources: Resource;
  storageKey: string;
}) {
  const i18n = createInstance();

  function getInitialAppLanguage(): AppLanguage {
    const stored = normalizeLanguage(localStorage.getItem(storageKey));
    if (stored) {
      return stored;
    }
    return normalizeLanguage(navigator.language) ?? "en";
  }

  async function setAppLanguage(language: AppLanguage) {
    localStorage.setItem(storageKey, language);
    await i18n.changeLanguage(language);
  }

  i18n.use(initReactI18next).init({
    resources,
    lng: getInitialAppLanguage(),
    fallbackLng: "en",
    interpolation: { escapeValue: false },
  });
  return { i18n, getInitialAppLanguage, setAppLanguage };
}
