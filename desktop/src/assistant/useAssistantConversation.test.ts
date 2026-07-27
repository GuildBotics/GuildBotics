import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { useAssistantConversation } from "./useAssistantConversation";

describe("useAssistantConversation", () => {
  it("appends a turn to the live conversation", () => {
    const { result } = renderHook(() => useAssistantConversation("person:file-1"));
    const { conversationId } = result.current;

    act(() => {
      result.current.appendUser(conversationId, "person:file-1", "why?");
    });
    act(() => {
      result.current.appendAssistant(conversationId, "person:file-1", {
        content: "the token expired",
        traceId: "trace-1",
      });
    });

    expect(result.current.messages).toEqual([
      { role: "user", content: "why?" },
      { role: "assistant", content: "the token expired", traceId: "trace-1" },
    ]);
  });

  it("starts a new conversation when the target changes", () => {
    const { result, rerender } = renderHook(({ key }) => useAssistantConversation(key), {
      initialProps: { key: "person:file-1" },
    });
    const first = result.current.conversationId;
    act(() => {
      result.current.appendUser(first, "person:file-1", "why?");
    });

    rerender({ key: "person:file-2" });

    expect(result.current.conversationId).not.toBe(first);
    expect(result.current.messages).toEqual([]);
  });

  it("drops a reply that arrives after the target changed", () => {
    const { result, rerender } = renderHook(({ key }) => useAssistantConversation(key), {
      initialProps: { key: "person:file-1" },
    });
    const inFlight = result.current.conversationId;

    rerender({ key: "person:file-2" });
    act(() => {
      // A turn is slow enough that the user can move on before it answers, and
      // the answer would be about the execution they left.
      result.current.appendAssistant(inFlight, "person:file-1", { content: "stale" });
    });

    expect(result.current.messages).toEqual([]);
    expect(result.current.isCurrent(inFlight, "person:file-1")).toBe(false);
  });

  it("drops a reply from a superseded conversation on the same target", () => {
    const { result } = renderHook(() => useAssistantConversation("person:file-1"));

    act(() => {
      result.current.adopt("conv-new", [], "person:file-1");
    });
    act(() => {
      result.current.appendAssistant("conv-old", "person:file-1", { content: "stale" });
    });

    expect(result.current.messages).toEqual([]);
  });

  it("adopts a conversation started elsewhere", () => {
    const { result } = renderHook(() => useAssistantConversation("person:__new-draft__"));

    act(() => {
      result.current.adopt(
        "conv-created",
        [
          { role: "user", content: "make a weekly report" },
          { role: "assistant", content: "here is a draft" },
        ],
        "person:__new-draft__",
      );
    });

    expect(result.current.conversationId).toBe("conv-created");
    expect(result.current.messages).toHaveLength(2);
    expect(result.current.isCurrent("conv-created", "person:__new-draft__")).toBe(true);
  });

  it("keeps an adopted conversation when the target key catches up", () => {
    const { result, rerender } = renderHook(({ key }) => useAssistantConversation(key), {
      initialProps: { key: "person:__new-draft__" },
    });

    act(() => {
      result.current.adopt(
        "conv-1",
        [{ role: "assistant", content: "here is a draft" }],
        "person:file-9",
      );
    });
    // Saving the draft renames the target; the conversation must survive it.
    rerender({ key: "person:file-9" });

    expect(result.current.conversationId).toBe("conv-1");
    expect(result.current.messages).toHaveLength(1);
  });
});
