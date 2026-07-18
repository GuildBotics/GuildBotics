import type { TFunction } from "i18next";

import type { TracePresentation } from "./api/client";

export function tracePresentationLabel(t: TFunction, presentation: TracePresentation): string {
  return presentation.label_key
    ? t(presentation.label_key, { defaultValue: presentation.label_fallback })
    : presentation.label_fallback;
}

export function tracePresentationMessage(t: TFunction, presentation: TracePresentation): string {
  return presentation.message_key
    ? t(presentation.message_key, {
        ...presentation.message_params,
        defaultValue: presentation.message,
      })
    : presentation.message;
}

export function tracePresentationTone(presentation: TracePresentation): string {
  return presentation.tone || "neutral";
}
