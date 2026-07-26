import { Alert, Button, Group, ScrollArea, Stack, Text, Textarea, Title } from "@mantine/core";
import { Send, Sparkles } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";

export type CommandAuthoringMessage = {
  role: "user" | "assistant";
  content: string;
  traceId?: string;
};

export type CommandAuthoringPanelProps = {
  messages: CommandAuthoringMessage[];
  pending: boolean;
  disabled: boolean;
  error: string | null;
  onSubmit: (message: string) => void;
};

export function CommandAuthoringPanel({
  messages,
  pending,
  disabled,
  error,
  onSubmit,
}: CommandAuthoringPanelProps) {
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
    <section className="command-authoring-panel" aria-label={t("commands.authoring.title")}>
      <Group className="command-authoring-heading" gap="xs">
        <Sparkles size={16} />
        <Title order={3}>{t("commands.authoring.title")}</Title>
      </Group>

      <ScrollArea className="command-authoring-messages" type="auto">
        <Stack gap="sm" p="sm">
          {messages.length ? (
            messages.map((message, index) => (
              <div
                className={`command-authoring-message command-authoring-message-${message.role}`}
                key={`${message.role}-${index}`}
              >
                <Text size="sm">{message.content}</Text>
                {message.traceId ? (
                  <Text c="dimmed" size="xs" title={message.traceId}>
                    {t("commands.authoring.trace", { traceId: message.traceId })}
                  </Text>
                ) : null}
              </div>
            ))
          ) : (
            <Text c="dimmed" size="sm">
              {t("commands.authoring.empty")}
            </Text>
          )}
          {pending ? (
            <Text c="dimmed" size="sm">
              {t("commands.authoring.thinking")}
            </Text>
          ) : null}
        </Stack>
      </ScrollArea>

      {error ? (
        <Alert color="warning" title={t("commands.authoring.errorTitle")}>
          {error}
        </Alert>
      ) : null}

      <form onSubmit={submit} className="command-authoring-form">
        <Textarea
          aria-label={t("commands.authoring.inputLabel")}
          autosize
          disabled={disabled || pending}
          maxRows={6}
          minRows={2}
          placeholder={t("commands.authoring.placeholder")}
          value={input}
          onChange={(event) => setInput(event.currentTarget.value)}
        />
        <Button
          type="submit"
          leftSection={<Send size={15} />}
          loading={pending}
          disabled={disabled || !input.trim()}
        >
          {t("commands.authoring.send")}
        </Button>
      </form>
    </section>
  );
}
