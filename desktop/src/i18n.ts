import { createAppI18n } from "@guildbotics/ui/i18n";
import { setApiLanguage } from "./api/client";
import { resources } from "./locales";
import { providerResources } from "./setup/providers/resources";

export { normalizeLanguage, type AppLanguage } from "@guildbotics/ui/i18n";

for (const provider of providerResources) {
  for (const language of ["en", "ja"] as const) {
    mergeInto(resources[language].translation, provider[language]);
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

export const { i18n, getInitialAppLanguage, setAppLanguage } = createAppI18n({
  resources,
  storageKey: "guildbotics.appLanguage",
});
// The backend renders API error sentences in the language named on each
// request, so the client's header follows this instance's language -- through
// init and the settings screen alike.
i18n.on("languageChanged", (language) => setApiLanguage(language));
setApiLanguage(i18n.language);

export default i18n;
