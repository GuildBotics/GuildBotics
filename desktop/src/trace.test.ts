import { describe, expect, it } from "vitest";

import type { PromptTraceEntry } from "./api/client";
import { buildTraceGroups } from "./trace";

function traceEntry(overrides: Partial<PromptTraceEntry>): PromptTraceEntry {
  return {
    event: "llm.request",
    timestamp: "2026-06-04T01:00:00Z",
    person_id: "alice",
    brain: "brains/default.yml",
    command: "",
    target: "",
    cwd: "/workspace",
    description: "",
    transcript: "",
    prompt: "",
    response: "",
    error: "",
    fields: {},
    ...overrides,
  };
}

describe("buildTraceGroups ordering", () => {
  it("pairs a response with a later matching request entry", () => {
    const response = traceEntry({
      event: "llm.response",
      timestamp: "2026-06-04T01:00:02Z",
      response: "done",
      fields: { model: "gpt" },
    });
    const request = traceEntry({
      event: "llm.request",
      timestamp: "2026-06-04T01:00:01Z",
      prompt: "hello",
      fields: { model: "gpt" },
    });

    const groups = buildTraceGroups([response, request]);

    expect(groups).toHaveLength(1);
    expect(groups[0]).toMatchObject({
      kind: "llm",
      request,
      response,
      single: null,
      personId: "alice",
      brain: "brains/default.yml",
    });
  });

  it("does not pair a request that appears before its response", () => {
    const request = traceEntry({
      event: "llm.request",
      timestamp: "2026-06-04T01:00:01Z",
      prompt: "hello",
      fields: { model: "gpt" },
    });
    const response = traceEntry({
      event: "llm.response",
      timestamp: "2026-06-04T01:00:02Z",
      response: "done",
      fields: { model: "gpt" },
    });

    const groups = buildTraceGroups([request, response]);

    // The request precedes the response, so the response cannot find a later
    // matching request: both remain as separate single/response groups.
    expect(groups).toHaveLength(2);
    expect(groups[0]).toMatchObject({ request, response: null, single: null });
    expect(groups[1]).toMatchObject({ request: null, response, single: null });
  });

  it("pairs multiple mixed request/response entries for the same target", () => {
    const responseB = traceEntry({
      event: "llm.response",
      timestamp: "2026-06-04T01:00:04Z",
      response: "second",
      fields: { model: "gpt" },
    });
    const responseA = traceEntry({
      event: "llm.response",
      timestamp: "2026-06-04T01:00:02Z",
      response: "first",
      fields: { model: "gpt" },
    });
    const requestB = traceEntry({
      event: "llm.request",
      timestamp: "2026-06-04T01:00:03Z",
      prompt: "second?",
      fields: { model: "gpt" },
    });
    const requestA = traceEntry({
      event: "llm.request",
      timestamp: "2026-06-04T01:00:01Z",
      prompt: "first?",
      fields: { model: "gpt" },
    });

    // Newest-first ordering as the UI receives it.
    const groups = buildTraceGroups([responseB, requestB, responseA, requestA]);

    expect(groups).toHaveLength(2);
    expect(groups[0]).toMatchObject({ request: requestB, response: responseB, single: null });
    expect(groups[1]).toMatchObject({ request: requestA, response: responseA, single: null });
  });

  it("keeps responses unpaired when the model field differs", () => {
    const response = traceEntry({
      event: "llm.response",
      timestamp: "2026-06-04T01:00:02Z",
      response: "done",
      fields: { model: "gpt-4" },
    });
    const request = traceEntry({
      event: "llm.request",
      timestamp: "2026-06-04T01:00:01Z",
      prompt: "hello",
      fields: { model: "gpt-3" },
    });

    const groups = buildTraceGroups([response, request]);

    expect(groups).toHaveLength(2);
    expect(groups[0]).toMatchObject({ request: null, response, single: null });
    expect(groups[1]).toMatchObject({ request, response: null, single: null });
  });

  it("pairs entries that both omit model and cli_agent fields", () => {
    const response = traceEntry({
      event: "llm.response",
      timestamp: "2026-06-04T01:00:02Z",
      response: "done",
    });
    const request = traceEntry({
      event: "llm.request",
      timestamp: "2026-06-04T01:00:01Z",
      prompt: "hello",
    });

    const groups = buildTraceGroups([response, request]);

    expect(groups).toHaveLength(1);
    expect(groups[0]).toMatchObject({ request, response, single: null });
  });

  it("pairs cli_agent entries by their cli_agent field", () => {
    const response = traceEntry({
      event: "cli_agent.response",
      timestamp: "2026-06-04T01:00:02Z",
      response: "done",
      fields: { cli_agent: "codex" },
    });
    const request = traceEntry({
      event: "cli_agent.request",
      timestamp: "2026-06-04T01:00:01Z",
      prompt: "hello",
      fields: { cli_agent: "codex" },
    });

    const groups = buildTraceGroups([response, request]);

    expect(groups).toHaveLength(1);
    expect(groups[0]).toMatchObject({ kind: "cli", request, response, single: null });
  });
});

describe("buildTraceGroups kind classification", () => {
  it("classifies llm events as llm", () => {
    const groups = buildTraceGroups([traceEntry({ event: "llm.request" })]);
    expect(groups[0]?.kind).toBe("llm");
  });

  it("classifies cli_agent events as cli", () => {
    const groups = buildTraceGroups([traceEntry({ event: "cli_agent.request" })]);
    expect(groups[0]?.kind).toBe("cli");
  });

  it("classifies chat events as chat", () => {
    const groups = buildTraceGroups([traceEntry({ event: "chat.reply_input" })]);
    expect(groups[0]?.kind).toBe("chat");
  });

  it("classifies unknown events as trace", () => {
    const groups = buildTraceGroups([traceEntry({ event: "something.else" })]);
    expect(groups[0]?.kind).toBe("trace");
  });
});

describe("buildTraceGroups single and parse-error entries", () => {
  it("keeps a chat single event as its own group", () => {
    const entry = traceEntry({ event: "chat.message", timestamp: "2026-06-04T01:00:03Z" });

    const groups = buildTraceGroups([entry]);

    expect(groups).toHaveLength(1);
    expect(groups[0]).toMatchObject({ request: null, response: null, single: entry });
  });

  it("keeps a parse-error single event separate from a request/response pair", () => {
    const parseError = traceEntry({
      event: "prompt_trace.parse_error",
      timestamp: "2026-06-04T01:00:05Z",
      error: "bad json",
    });
    const response = traceEntry({
      event: "llm.response",
      timestamp: "2026-06-04T01:00:02Z",
      response: "done",
      fields: { model: "gpt" },
    });
    const request = traceEntry({
      event: "llm.request",
      timestamp: "2026-06-04T01:00:01Z",
      prompt: "hello",
      fields: { model: "gpt" },
    });

    const groups = buildTraceGroups([parseError, response, request]);

    expect(groups).toHaveLength(2);
    expect(groups[0]).toMatchObject({ kind: "trace", single: parseError });
    expect(groups[1]).toMatchObject({ kind: "llm", request, response, single: null });
  });

  it("does not pair a response with a request of a different person", () => {
    const response = traceEntry({
      event: "llm.response",
      timestamp: "2026-06-04T01:00:02Z",
      person_id: "alice",
      response: "done",
      fields: { model: "gpt" },
    });
    const request = traceEntry({
      event: "llm.request",
      timestamp: "2026-06-04T01:00:01Z",
      person_id: "bob",
      prompt: "hello",
      fields: { model: "gpt" },
    });

    const groups = buildTraceGroups([response, request]);

    expect(groups).toHaveLength(2);
    expect(groups[0]).toMatchObject({ response, request: null });
    expect(groups[1]).toMatchObject({ request, response: null });
  });
});
