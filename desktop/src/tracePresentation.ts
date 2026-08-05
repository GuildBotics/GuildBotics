import type { TFunction } from "i18next";

import type { TracePresentation } from "./api/client";

/** Newest record of a trace — what a one-line status shows. Records arrive oldest first. */
export function latestPresentation(
  records: { presentation: TracePresentation }[],
): TracePresentation | null {
  return records.length > 0 ? records[records.length - 1].presentation : null;
}

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
