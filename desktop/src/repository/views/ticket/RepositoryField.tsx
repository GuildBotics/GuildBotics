import { Select, Stack } from "@mantine/core";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { isApiError } from "../../../api/client";
import * as api from "../../api/endpoints";
import type { Ticket } from "../../api/types";
import { ErrorMessage, Refusal } from "../../components/common";
import { useT } from "../../i18n";
import { useRepos } from "../../query/hooks";

/** Why the service refuses to move a ticket, each with its own message. */
const REFUSALS = ["move_upstream_issue", "move_closing_pull", "move_repair", "conflict"] as const;

/**
 * The repository a ticket is in, among those of its workspace (shown when it
 * has more than one). Choosing another moves the ticket there once the person
 * confirms what changes with it: its number and URL, and the labels the other
 * repository does not define.
 */
export function RepositoryField({
  ticket,
  onMoved,
}: {
  ticket: Ticket;
  onMoved: (moved: Ticket) => void;
}) {
  const t = useT();
  const client = useQueryClient();
  const repos = useRepos();
  const workspace = ticket.repository.split("/")[0];
  const choices = (repos.data?.items ?? []).filter((r) => r.path.startsWith(`${workspace}/`));
  const move = useMutation({
    mutationFn: (repository: string) =>
      api.tickets.move(ticket.repository, ticket.key, { repository, revision: ticket.revision }),
    onSuccess: (res) => onMoved(res.ticket),
    onSettled: () => void client.invalidateQueries({ queryKey: ["board"] }),
  });
  if (choices.length < 2) return null;
  const choose = (path: string | null) => {
    const to = choices.find((r) => r.path === path);
    if (!to || to.path === ticket.repository) return;
    const dropped = ticket.labels.filter((l) => !to.labels.includes(l));
    const message = [
      t("ticket.moveConfirm", { key: `#${ticket.key}`, repository: to.path }),
      ...(dropped.length
        ? [t("ticket.moveDropsLabels", { repository: to.path, labels: dropped.join(", ") })]
        : []),
    ].join("\n\n");
    if (window.confirm(message)) move.mutate(to.path);
  };
  const refusal = REFUSALS.find((code) => isApiError(move.error, code));
  return (
    <Stack gap={6}>
      <Select
        label={t("ticket.repository")}
        value={ticket.repository}
        allowDeselect={false}
        disabled={move.isPending}
        onChange={choose}
        data={choices.map((r) => ({ value: r.path, label: r.path }))}
      />
      {move.isError &&
        (refusal ? (
          <Refusal>{t(`ticket.${refusal}`)}</Refusal>
        ) : (
          <ErrorMessage error={move.error} />
        ))}
    </Stack>
  );
}
