import { Alert, Button, Group, ScrollArea, Stack, Text, Textarea, Title } from "@mantine/core";
import { Send, Sparkles } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

/** i18n key prefix. Every namespace defines the same leaf keys. */
export type AssistantChatNamespace = "commands.authoring" | "diagnostics.troubleshooting";

export type AssistantReference = {
  traceId: string;
  label: string;
  to: string;
};

export type AssistantMessage = {
  role: "user" | "assistant";
  content: string;
  traceId?: string;
  references?: AssistantReference[];
};

export type AssistantChatPanelProps = {
  namespace: AssistantChatNamespace;
  messages: AssistantMessage[];
  pending: boolean;
  disabled: boolean;
  error: string | null;
  onSubmit: (message: string) => void;
  /** Rendered above the transcript, for target badges and disclosures. */
  header?: ReactNode;
  /** Rendered while pending, for live progress from the running turn. */
  progress?: ReactNode;
};

export function AssistantChatPanel({
  namespace,
  messages,
  pending,
  disabled,
  error,
  onSubmit,
  header,
  progress,
}: AssistantChatPanelProps) {
  const { t } = useTranslation();
  const [input, setInput] = useState("");
  const submittedInputRef = useRef("");
  const previousErrorRef = useRef<string | null>(null);

  useEffect(() => {
    if (error && !previousErrorRef.current) {
      setInput((current) => current || submittedInputRef.current);
    }
    previousErrorRef.current = error;
  }, [error]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const message = input.trim();
    if (!message || disabled || pending) {
      return;
    }
    submittedInputRef.current = message;
    setInput("");
    onSubmit(message);
  };

  return (
    <section className="assistant-chat-panel" aria-label={t(`${namespace}.title`)}>
      <Group className="assistant-chat-heading" gap="xs">
        <Sparkles size={16} />
        <Title order={3}>{t(`${namespace}.title`)}</Title>
      </Group>

      {header ? <div className="assistant-chat-header">{header}</div> : null}

      <ScrollArea className="assistant-chat-messages" type="auto">
        <Stack gap="sm" p="sm">
          {messages.length ? (
            messages.map((message, index) => (
              <div
                className={`assistant-chat-message assistant-chat-message-${message.role}`}
                key={`${message.role}-${index}`}
              >
                <Text size="sm">{message.content}</Text>
                {message.references?.length ? (
                  <Group className="assistant-chat-references" gap="xs">
                    {message.references.map((reference) => (
                      <Link
                        className="assistant-chat-reference"
                        key={reference.traceId}
                        title={reference.traceId}
                        to={reference.to}
                      >
                        {reference.label}
                      </Link>
                    ))}
                  </Group>
                ) : null}
                {message.traceId ? (
                  <Text c="dimmed" size="xs" title={message.traceId}>
                    {t(`${namespace}.trace`, { traceId: message.traceId })}
                  </Text>
                ) : null}
              </div>
            ))
          ) : (
            <Text c="dimmed" size="sm">
              {t(`${namespace}.empty`)}
            </Text>
          )}
          {pending ? (
            <div className="assistant-chat-progress">
              <Text c="dimmed" size="sm">
                {t(`${namespace}.thinking`)}
              </Text>
              {progress}
            </div>
          ) : null}
        </Stack>
      </ScrollArea>

      {error ? (
        <Alert color="warning" title={t(`${namespace}.errorTitle`)}>
          {error}
        </Alert>
      ) : null}

      <form onSubmit={submit} className="assistant-chat-form">
        <Textarea
          aria-label={t(`${namespace}.inputLabel`)}
          autosize
          disabled={disabled || pending}
          maxRows={6}
          minRows={2}
          placeholder={t(`${namespace}.placeholder`)}
          value={input}
          onChange={(event) => setInput(event.currentTarget.value)}
        />
        <Button
          type="submit"
          leftSection={<Send size={15} />}
          loading={pending}
          disabled={disabled || !input.trim()}
        >
          {t(`${namespace}.send`)}
        </Button>
      </form>
    </section>
  );
}
