import { Alert, Stack, Text } from "@mantine/core";
import { CircleAlert } from "lucide-react";
import { useTranslation } from "react-i18next";

import { ApiRequestError } from "../api/client";

/**
 * A failed request, as the user reads it.
 *
 * Hub failures answer with a sentence naming what could not be done and, in
 * `context.detail`, the OpenSSH or Git text that says why. Showing only the
 * first leaves "could not be reached" covering an unregistered device key, a
 * name that does not resolve, and a hub whose command is missing -- three
 * situations with three different fixes, none of which the screen would name.
 * So the detail is shown with it, quieter but present.
 *
 * The sentence is localized by the error's `code` when `apiErrors` has an
 * entry for it, and is otherwise the backend's own English text -- so each
 * code gains a translation by adding a key, and an unknown one still says
 * something true. The detail stays as the boundary produced it: it is OS or
 * Git diagnostics, not copy.
 */
export function RequestErrorAlert({ cause, title }: { cause: unknown; title: string }) {
  const { t } = useTranslation();
  if (cause === null || cause === undefined) {
    return null;
  }
  const failure = cause instanceof ApiRequestError ? cause : null;
  const detail = typeof failure?.context.detail === "string" ? failure.context.detail.trim() : "";
  return (
    <Alert color="danger" icon={<CircleAlert size={18} />} title={title}>
      <Stack gap={4}>
        <Text size="sm">
          {failure
            ? t(`apiErrors.${failure.code}`, { defaultValue: failure.message })
            : String(cause)}
        </Text>
        {detail === "" ? null : (
          <Text c="dimmed" size="xs" style={{ overflowWrap: "anywhere" }}>
            {detail}
          </Text>
        )}
      </Stack>
    </Alert>
  );
}
