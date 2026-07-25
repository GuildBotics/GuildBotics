import { beforeEach, describe, expect, it } from "vitest";

import type { CommandOption, RuntimeEvent, TraceRecord } from "../api/client";
import {
  canRunUnattended,
  initialCommand,
  latestPresentation,
  loadLastCommand,
  pendingRunTraceId,
  resolveRunner,
  saveLastCommand,
  unmetRequirements,
} from "./quickRunState";

function option(overrides: Partial<CommandOption> = {}): CommandOption {
  return {
    command: "greet",
    label: "Greet",
    description: "",
    category: "custom",
    source: "workspace",
    path: "greet.md",
    arguments: [],
    inputs: { defined_args: "auto", extra_args: "hidden", message: "optional" },
    requirements: [],
    routine_eligible: true,
    ...overrides,
  } as CommandOption;
}

describe("canRunUnattended", () => {
  it("runs when the command asks for nothing", () => {
    expect(canRunUnattended(option(), "", {})).toBe(true);
  });

  it("waits when a required message was not carried over", () => {
    const required = option({
      inputs: { defined_args: "auto", extra_args: "hidden", message: "required" },
    });

    expect(canRunUnattended(required, "   ", {})).toBe(false);
    expect(canRunUnattended(required, "hello", {})).toBe(true);
  });

  it("waits when a required argument has no value", () => {
    const withArg = option({
      arguments: [{ name: "target", kind: "positional", required: true, default: "" }],
    });

    expect(canRunUnattended(withArg, "hello", {})).toBe(false);
    expect(canRunUnattended(withArg, "hello", { target: "main" })).toBe(true);
  });

  it("ignores declared arguments the command hides", () => {
    const hidden = option({
      arguments: [{ name: "target", kind: "positional", required: true, default: "" }],
      inputs: { defined_args: "hidden", extra_args: "hidden", message: "optional" },
    });

    expect(canRunUnattended(hidden, "", {})).toBe(true);
  });

  it("waits when the command is unknown", () => {
    expect(canRunUnattended(undefined, "hello", {})).toBe(false);
  });
});

describe("initialCommand", () => {
  const options = [option(), option({ command: "review", label: "Review" })];

  it("restores the previously run command", () => {
    expect(initialCommand(options, "review")).toBe("review");
  });

  it("falls back to the first command when the remembered one is gone", () => {
    expect(initialCommand(options, "deleted")).toBe("greet");
    expect(initialCommand(options, null)).toBe("greet");
  });

  it("returns null when nothing can be run", () => {
    expect(initialCommand([], "greet")).toBeNull();
  });
});

describe("last command persistence", () => {
  beforeEach(() => window.localStorage.clear());

  it("round trips through storage", () => {
    expect(loadLastCommand()).toBeNull();

    saveLastCommand("review");

    expect(loadLastCommand()).toBe("review");
  });
});

describe("requirements", () => {
  const blocked = option({
    requirements: [
      { kind: "github", satisfied: false, message: "" },
      { kind: "llm", satisfied: true, message: "" },
    ],
  });

  it("names only the requirements that are unmet", () => {
    expect(unmetRequirements(blocked)).toEqual(["github"]);
    expect(unmetRequirements(option())).toEqual([]);
    expect(unmetRequirements(undefined)).toEqual([]);
  });

  it("refuses to run a command whose integrations are not configured", () => {
    expect(canRunUnattended(blocked, "hello", {})).toBe(false);
  });
});

describe("resolveRunner", () => {
  const team = {
    project: { name: "Demo", language_code: "en", language_name: "English" },
    members: [
      { person_id: "bot", name: "Bot", is_active: true, roles: [] },
      { person_id: "aiko", name: "Aiko", is_active: true, roles: [] },
      { person_id: "retired", name: "Retired", is_active: false, roles: [] },
    ],
    default_person_id: "bot",
  };

  it("uses the chosen member", () => {
    expect(resolveRunner(team, "aiko")?.person_id).toBe("aiko");
  });

  it("falls back to the default when the choice is gone or inactive", () => {
    expect(resolveRunner(team, "deleted")?.person_id).toBe("bot");
    expect(resolveRunner(team, "retired")?.person_id).toBe("bot");
    expect(resolveRunner(team, null)?.person_id).toBe("bot");
  });

  it("returns null when the team is not loaded", () => {
    expect(resolveRunner(undefined, "aiko")).toBeNull();
  });
});

function startedEvent(overrides: Partial<RuntimeEvent> = {}): RuntimeEvent {
  return {
    kind: "event",
    type: "command.started",
    trace_id: "trace-1",
    span_id: null,
    parent_id: null,
    source: "manual",
    person_id: "bot",
    command: "greet",
    workflow: "",
    attributes: {},
    payload: { command: "greet", person: "bot" },
    timestamp: "2026-07-26T00:00:00Z",
    ...overrides,
  };
}

describe("pendingRunTraceId", () => {
  const pending = { command: "greet", person: "bot" };

  it("takes the trace of the run this window asked for", () => {
    expect(pendingRunTraceId(startedEvent(), pending)).toBe("trace-1");
  });

  it("ignores runs started for another command or member", () => {
    const otherCommand = startedEvent({ payload: { command: "review", person: "bot" } });
    const otherMember = startedEvent({ payload: { command: "greet", person: "aiko" } });

    expect(pendingRunTraceId(otherCommand, pending)).toBeNull();
    expect(pendingRunTraceId(otherMember, pending)).toBeNull();
  });

  it("ignores the scheduler running the very same command for the same member", () => {
    expect(pendingRunTraceId(startedEvent({ source: "scheduled" }), pending)).toBeNull();
    expect(pendingRunTraceId(startedEvent({ source: "routine" }), pending)).toBeNull();
  });

  it("accepts any member when the window could not name one", () => {
    const event = startedEvent({ payload: { command: "greet", person: "aiko" } });

    expect(pendingRunTraceId(event, { command: "greet", person: null })).toBe("trace-1");
  });

  it("ignores anything that is not a start with a trace", () => {
    expect(pendingRunTraceId(startedEvent({ type: "command.finished" }), pending)).toBeNull();
    expect(pendingRunTraceId(startedEvent({ trace_id: null }), pending)).toBeNull();
    expect(pendingRunTraceId(startedEvent(), null)).toBeNull();
  });
});

describe("latestPresentation", () => {
  function record(message: string): TraceRecord {
    return {
      presentation: {
        label_key: "",
        label_fallback: "Event",
        message_key: "",
        message,
        message_params: {},
        tone: "info",
      },
    } as TraceRecord;
  }

  it("takes the newest record", () => {
    expect(latestPresentation([record("first"), record("last")])?.message).toBe("last");
  });

  it("has nothing to show for a trace without records", () => {
    expect(latestPresentation([])).toBeNull();
  });
});
