import type { TFunction } from "i18next";
import { describe, expect, it } from "vitest";

import type { TracePresentation } from "./api/client";
import i18n from "./i18n";
import {
  tracePresentationLabel,
  tracePresentationMessage,
  tracePresentationTone,
} from "./tracePresentation";

function t(): TFunction {
  return i18n.getFixedT("en") as TFunction;
}

function presentation(overrides: Partial<TracePresentation> = {}): TracePresentation {
  return {
    label_key: "diagnostics.executions.eventTypes.command_finished",
    label_fallback: "command.finished",
    message_key: "",
    message: "guildbotics member memory record",
    message_params: {},
    tone: "success",
    ...overrides,
  };
}

describe("trace presentation", () => {
  it("renders the translated label and literal command message independently", () => {
    const value = presentation();

    expect(tracePresentationLabel(t(), value)).toBe("Finished");
    expect(tracePresentationMessage(t(), value)).toBe("guildbotics member memory record");
    expect(tracePresentationMessage(t(), value)).not.toBe(tracePresentationLabel(t(), value));
  });

  it("renders parameterized messages supplied by the API contract", () => {
    const value = presentation({
      label_key: "diagnostics.executions.eventTypes.chat_dispatch_retry_scheduled",
      message_key: "diagnostics.executions.messages.chat_dispatch_retry_scheduled",
      message: "chat_dispatch.retry_scheduled",
      message_params: {
        run: "run-1",
        retry_at: "15:00",
        attempt: 2,
        max_attempts: 3,
      },
      tone: "warning",
    });

    expect(tracePresentationMessage(t(), value)).toBe(
      "Run run-1 will retry at 15:00 (attempt 2/3).",
    );
    expect(tracePresentationTone(value)).toBe("warning");
  });

  it("uses API fallbacks for unknown or untranslated records", () => {
    const value = presentation({
      label_key: "",
      label_fallback: "CUSTOM",
      message: "custom detail",
      tone: "",
    });

    expect(tracePresentationLabel(t(), value)).toBe("CUSTOM");
    expect(tracePresentationMessage(t(), value)).toBe("custom detail");
    expect(tracePresentationTone(value)).toBe("neutral");
  });
});
