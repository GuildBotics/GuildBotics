import { ActionIcon, Button, Checkbox, Group, Select, Stack, Text } from "@mantine/core";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { X } from "lucide-react";
import { useState } from "react";
import { isApiError } from "../../api/client";
import * as api from "../../api/endpoints";
import { ticketRef, type PullRequest } from "../../api/types";
import { ErrorMessage, Loading, Refusal } from "../../components/common";
import { useT } from "../../i18n";
import { useCards } from "../../query/hooks";
import { keys } from "../../query/keys";
import { ServiceLink } from "../../router/Router";
import { routeOfUrl } from "../../router/routes";

/** Ticket links are saved together with the revision the editor opened on. */
export function Tickets({ pull }: { pull: PullRequest }) {
  const t = useT();
  const [editing, setEditing] = useState(false);
  if (pull.state !== "open" && pull.tickets.length === 0) return null;
  const label = (url: string) => {
    const route = routeOfUrl(url, new URL(pull.url).origin);
    return route?.name === "ticket" ? ticketRef({ repository: route.repo, key: route.key }) : url;
  };
  return (
    <Stack gap="xs" mt="md" component="section" aria-label={t("pull.tickets")}>
      <Group justify="space-between">
        <Text size="sm" c="dimmed">{t("pull.tickets")}</Text>
        {pull.state === "open" && !editing && (
          <Button size="compact-xs" variant="subtle" onClick={() => setEditing(true)}>{t("pull.editTickets")}</Button>
        )}
      </Group>
      {editing && pull.state === "open" ? (
        <TicketsEditor pull={pull} label={label} onDone={() => setEditing(false)} />
      ) : (
        <Text size="sm" component="div">
          {pull.tickets.length === 0 ? t("pull.noTickets") : pull.tickets.map((tk, i) => (
            <span key={tk.url}>
              {i > 0 && ", "}
              <ServiceLink url={tk.url}>{label(tk.url)}</ServiceLink>
              {tk.closes && <Text span c="dimmed"> {t("pull.closesTicket")}</Text>}
            </span>
          ))}
        </Text>
      )}
    </Stack>
  );
}

function TicketsEditor({ pull, label, onDone }: { pull: PullRequest; label: (url: string) => string; onDone: () => void }) {
  const t = useT();
  const client = useQueryClient();
  const [form, setForm] = useState({ tickets: pull.tickets, revision: pull.revision });
  const workspace = pull.repository.split("/")[0] as string;
  const cards = useCards(workspace, "all");
  const choices = (cards.data?.items ?? []).filter((c) => !form.tickets.some((tk) => tk.url === c.url));
  const save = useMutation({
    mutationFn: () => api.pulls.update(pull.repository, pull.number, form),
    onSuccess: (p) => {
      client.setQueryData(keys.pull(pull.repository, pull.number), p);
      // Ticket pages and board cards also show which PRs refer to each ticket.
      for (const tk of [...pull.tickets, ...p.tickets]) {
        const route = routeOfUrl(tk.url, new URL(pull.url).origin);
        if (route?.name === "ticket") void client.invalidateQueries({ queryKey: keys.ticketPart(route.repo, route.key, "pulls") });
      }
      void client.invalidateQueries({ queryKey: keys.board(workspace) });
      onDone();
    },
    onSettled: () => client.invalidateQueries({ queryKey: keys.pull(pull.repository, pull.number) }),
  });
  const conflict = save.isError && isApiError(save.error, "conflict");
  return (
    <form aria-label={t("pull.editTickets")} onSubmit={(e) => { e.preventDefault(); if (!save.isPending && !conflict) save.mutate(); }}>
      <Stack gap="sm">
        {form.tickets.map((tk) => {
          const title = cards.data?.items.find((c) => c.url === tk.url)?.title;
          const route = routeOfUrl(tk.url, new URL(pull.url).origin);
          // Only canonical service URLs name local tickets to the API; browser
          // loopback aliases are useful for navigation but remain references.
          const prefix = pull.url.slice(0, pull.url.lastIndexOf("/-/pulls/")) + "/-/tickets/";
          const canClose = tk.url.startsWith(prefix) && route?.name === "ticket" && route.repo === pull.repository;
          return (
            <Group key={tk.url} role="group" aria-label={label(tk.url)} justify="space-between">
              <ServiceLink url={tk.url}>{label(tk.url)}{title && ` ${title}`}</ServiceLink>
              <Group gap="xs">
                <Checkbox
                  label={t("pull.closeOnMerge")}
                  checked={tk.closes}
                  disabled={!canClose || save.isPending}
                  onChange={(e) => {
                    const closes = e.currentTarget.checked;
                    setForm({ ...form, tickets: form.tickets.map((x) => x.url === tk.url ? { ...x, closes } : x) });
                  }}
                />
                {!canClose && <Text size="xs" c="dimmed">{t("pull.referenceOnly")}</Text>}
                <ActionIcon variant="subtle" color="gray" disabled={save.isPending} aria-label={t("pull.removeTicket", { ticket: label(tk.url) })}
                  onClick={() => setForm({ ...form, tickets: form.tickets.filter((x) => x.url !== tk.url) })}>
                  <X size={14} />
                </ActionIcon>
              </Group>
            </Group>
          );
        })}
        {cards.isPending ? <Loading /> : cards.isError ? <ErrorMessage error={cards.error} /> : (
          <Select
            label={t("pull.addTicket")}
            searchable
            value={null}
            data={choices.map((c) => ({ value: c.url, label: `${ticketRef(c)} ${c.title}${c.state === "closed" ? ` (${t("ticket.closed")})` : ""}` }))}
            disabled={save.isPending}
            nothingFoundMessage={t("pull.noTicketChoices")}
            onChange={(url) => { if (url) setForm({ ...form, tickets: [...form.tickets, { url, closes: false }] }); }}
          />
        )}
        {save.isError && (conflict ? <Refusal>{t("pull.ticketsConflict")}</Refusal> : <ErrorMessage error={save.error} />)}
        <Group justify="flex-end" gap="xs">
          <Button variant="default" disabled={save.isPending} onClick={onDone}>{t("common.cancel")}</Button>
          <Button type="submit" disabled={save.isPending || conflict}>{t("common.save")}</Button>
        </Group>
      </Stack>
    </form>
  );
}
