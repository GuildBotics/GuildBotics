import { en } from "../i18n/en";
import { ja } from "../i18n/ja";
import { shared } from "../../sharedLocales";

export const dictionaries = { en: { ...shared.en, ...en }, ja: { ...shared.ja, ...ja } };
