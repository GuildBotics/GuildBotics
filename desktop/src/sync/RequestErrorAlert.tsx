import { Alert, Stack, Text } from "@mantine/core";
import { CircleAlert } from "lucide-react";

import { ApiRequestError } from "../api/client";

/**
 * A failed request, as the user reads it.
 *
 * The message arrives from the backend already in the screen's language: the
 * client names its display language on every request, and the API renders
 * each error's sentence from the locale files. Nothing is re-translated or
 * re-worded here -- a second wording would eventually be a different one.
 *
 * Hub failures also carry, in `context.detail`, the OpenSSH or Git text that
 * says why. Showing only the sentence leaves "could not be reached" covering
 * an unregistered device key, a name that does not resolve, and a hub whose
 * command is missing -- three situations with three different fixes, none of
 * which the screen would name. So the detail is shown with it, quieter but
 * present, and stays as the boundary produced it: it is diagnostics, not copy.
 */
export function RequestErrorAlert({ cause, title }: { cause: unknown; title: string }) {
  if (cause === null || cause === undefined) {
    return null;
  }
  const failure = cause instanceof ApiRequestError ? cause : null;
  const detail = typeof failure?.context.detail === "string" ? failure.context.detail.trim() : "";
  return (
    <Alert color="danger" icon={<CircleAlert size={18} />} title={title}>
      <Stack gap={4}>
        <Text size="sm">{failure ? failure.message : String(cause)}</Text>
        {detail === "" ? null : (
          <Text c="dimmed" size="xs" style={{ overflowWrap: "anywhere" }}>
            {detail}
          </Text>
        )}
      </Stack>
    </Alert>
  );
}
