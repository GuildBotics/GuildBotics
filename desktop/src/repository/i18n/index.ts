import { useTranslation } from "react-i18next";
import "../../i18n";
import type { MessageKey as RepositoryKey } from "./en";
import type { shared } from "../../sharedLocales";

export type MessageKey = RepositoryKey | keyof (typeof shared)["en"];
export type Translate = (key: MessageKey, params?: Record<string, string | number>) => string;

export function useT(): Translate {
  return useTranslation("repository").t;
}
