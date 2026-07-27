import { useCallback, useState } from "react";
import type { AssistantMessage } from "./AssistantChatPanel";

export type AssistantConversation = {
  conversationId: string;
  messages: AssistantMessage[];
  /** Whether a reply still belongs to the live conversation and target. */
  isCurrent: (conversationId: string, targetKey: string) => boolean;
  appendUser: (conversationId: string, targetKey: string, content: string) => void;
  appendAssistant: (
    conversationId: string,
    targetKey: string,
    reply: Omit<AssistantMessage, "role">,
  ) => void;
  /** Take over a conversation that was started outside this panel. */
  adopt: (conversationId: string, messages: AssistantMessage[], targetKey: string) => void;
};

type ConversationState = {
  conversationId: string;
  messages: AssistantMessage[];
  /** Target this conversation belongs to; `adopt` may move it ahead. */
  targetKey: string;
  /** Last target the caller rendered with, so only real moves reset. */
  observedKey: string;
};

function startConversation(targetKey: string): ConversationState {
  return {
    conversationId: crypto.randomUUID(),
    messages: [],
    targetKey,
    observedKey: targetKey,
  };
}

/**
 * Own one assistant conversation's client-side state.
 *
 * The conversation restarts whenever `targetKey` changes, and replies that
 * arrive after a restart are dropped: a turn is slow enough that the user can
 * switch targets while it runs, and its answer would be about the old one.
 */
export function useAssistantConversation(targetKey: string): AssistantConversation {
  const [state, setState] = useState(() => startConversation(targetKey));

  // Adjusting state during render is how React resets state on a prop change;
  // it re-renders before anything is shown, so no stale conversation is drawn.
  // Only a move the caller actually made resets: when `adopt` has already
  // carried the conversation to this target, the caller is merely catching up.
  if (state.observedKey !== targetKey) {
    setState(
      state.targetKey === targetKey
        ? { ...state, observedKey: targetKey }
        : startConversation(targetKey),
    );
  }

  const isCurrent = useCallback(
    (candidateConversationId: string, candidateTargetKey: string) =>
      candidateConversationId === state.conversationId && candidateTargetKey === state.targetKey,
    [state.conversationId, state.targetKey],
  );

  const append = useCallback(
    (candidateConversationId: string, candidateTargetKey: string, message: AssistantMessage) => {
      setState((current) =>
        candidateConversationId === current.conversationId &&
        candidateTargetKey === current.targetKey
          ? { ...current, messages: [...current.messages, message] }
          : current,
      );
    },
    [],
  );

  const appendUser = useCallback(
    (candidateConversationId: string, candidateTargetKey: string, content: string) => {
      append(candidateConversationId, candidateTargetKey, { role: "user", content });
    },
    [append],
  );

  const appendAssistant = useCallback(
    (
      candidateConversationId: string,
      candidateTargetKey: string,
      reply: Omit<AssistantMessage, "role">,
    ) => {
      append(candidateConversationId, candidateTargetKey, { role: "assistant", ...reply });
    },
    [append],
  );

  const adopt = useCallback(
    (
      adoptedConversationId: string,
      adoptedMessages: AssistantMessage[],
      adoptedTargetKey: string,
    ) => {
      setState((current) => ({
        conversationId: adoptedConversationId,
        messages: adoptedMessages,
        targetKey: adoptedTargetKey,
        observedKey: current.observedKey,
      }));
    },
    [],
  );

  return {
    conversationId: state.conversationId,
    messages: state.messages,
    isCurrent,
    appendUser,
    appendAssistant,
    adopt,
  };
}
