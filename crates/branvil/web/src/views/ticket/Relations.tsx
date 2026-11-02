import { ActionIcon, Anchor, Button, Card, Group, Stack, Text, TextInput } from "@mantine/core";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Plus, X } from "lucide-react";
import { useState } from "react";
import { isApiError } from "../../api/client";
import * as api from "../../api/endpoints";
import type { Ticket, TicketRef } from "../../api/types";
import { ErrorMessage, Refusal, StateBadge } from "../../components/common";
import { type Translate, useT } from "../../i18n";
import { keys } from "../../query/keys";
import { routeOfUrl } from "../../router/routes";
import { TicketLink } from "./TicketLink";

/** A ticket by its repository and number, with the URL the service knows it by. */
interface Named {
  repo: string;
  key: string;
  url: string;
}

/**
 * The ticket `text` names: `#12` (of `ticket`'s repository), `acme/demo#12`,
 * or its URL; null when it names none.
 */
export function ticketNamed(text: string, ticket: Ticket): Named | null {
  const s = text.trim();
  const base = ticket.url.slice(0, ticket.url.lastIndexOf(`/${ticket.repository}/-/tickets/`));
  const named = (repo: string, key: string) => ({ repo, key, url: `${base}/${repo}/-/tickets/${key}` });
  if (/^https?:\/\//.test(s)) {
    // The browser's address, or the URL the service gives (another origin behind a dev proxy).
    const route = routeOfUrl(s) ?? routeOfUrl(s, base);
    return route?.name === "ticket" ? named(route.repo, route.key) : null;
  }
  const m = /^(?:(\S+)#|#)?([1-9][0-9]*)$/.exec(s);
  return m ? named(m[1] ?? ticket.repository, m[2] as string) : null;
}

/** The refusal an API error of a relation's rule stands for, in the GUI's language; null for other errors. */
export function relationRefusal(err: unknown, t: Translate): string | null {
  if (isApiError(err, "children_open")) return t("ticket.childrenOpen");
  if (isApiError(err, "blocked")) return t("ticket.blocked");
  return null;
}

/** What one change of a relation writes: a patch of one of the two tickets. */
interface Change {
  ticket: { repo: string; key: string };
  patch: api.TicketPatch;
}

/**
 * A ticket's parent, children, the tickets it is blocked by, and those it
 * blocks. Each relation is stored on one side (the child names its parent,
 * the blocked ticket its blockers), so a change from the other side writes
 * the other ticket.
 */
export function Relations({ ticket }: { ticket: Ticket }) {
  const t = useT();
  const client = useQueryClient();
  const relate = useMutation({
    mutationFn: ({ ticket: { repo, key }, patch }: Change) => api.tickets.update(repo, key, patch),
    onSettled: () => {
      void client.invalidateQueries({ queryKey: keys.ticket(ticket.repository, ticket.key) });
      void client.invalidateQueries({ queryKey: ["board"] });
    },
  });
  const change = (c: Change) => relate.mutateAsync(c);
  const self = { repo: ticket.repository, key: ticket.key };
  const of = (r: TicketRef) => ({ repo: r.repository, key: r.key });
  const closedChildren = ticket.children.filter((c) => c.state === "closed").length;

  return (
    <Card p="md" component="section" aria-label={t("ticket.relations")}>
      <Text fw={700} mb="xs">
        {t("ticket.relations")}
      </Text>
      <Stack gap="md">
        <RelationList
          label={t("ticket.parent")}
          refs={ticket.parent ? [ticket.parent] : []}
          ticket={ticket}
          pending={relate.isPending}
          onAdd={ticket.parent ? undefined : (n) => change({ ticket: self, patch: { parent: n.url } })}
          onRemove={() => change({ ticket: self, patch: { parent: null } })}
        />
        <RelationList
          label={t("ticket.children")}
          note={ticket.children.length > 0 ? t("ticket.childrenClosed", { closed: closedChildren, total: ticket.children.length }) : undefined}
          refs={ticket.children}
          ticket={ticket}
          pending={relate.isPending}
          onAdd={(n) => change({ ticket: n, patch: { parent: ticket.url } })}
          onRemove={(r) => change({ ticket: of(r), patch: { parent: null } })}
        />
        <RelationList
          label={t("ticket.blockedBy")}
          note={ticket.blocked_by.some((b) => b.state === "open") ? t("ticket.blocked") : undefined}
          refs={ticket.blocked_by}
          ticket={ticket}
          pending={relate.isPending}
          onAdd={(n) => change({ ticket: self, patch: { add_blocked_by: [n.url] } })}
          onRemove={(r) => change({ ticket: self, patch: { remove_blocked_by: [r.url] } })}
        />
        <RelationList
          label={t("ticket.blocks")}
          refs={ticket.blocks}
          ticket={ticket}
          pending={relate.isPending}
          onAdd={(n) => change({ ticket: n, patch: { add_blocked_by: [ticket.url] } })}
          onRemove={(r) => change({ ticket: of(r), patch: { remove_blocked_by: [ticket.url] } })}
        />
        {relate.isError && <ErrorMessage error={relate.error} />}
      </Stack>
    </Card>
  );
}

/** How a related ticket is named next to `ticket`: `#12` in the same repository. */
export const labelOf = (r: TicketRef, ticket: Ticket) => (r.repository === ticket.repository ? `#${r.key}` : `${r.repository}#${r.key}`);

/** One relation's tickets, each with its state and a button that takes it off, and a form that adds one. */
function RelationList({
  label,
  note,
  refs,
  ticket,
  pending,
  onAdd,
  onRemove,
}: {
  label: string;
  /** Shown under the label. */
  note?: string;
  refs: TicketRef[];
  ticket: Ticket;
  pending: boolean;
  /** Absent when no ticket can be added (a parent is already set). */
  onAdd?: (named: Named) => Promise<unknown>;
  onRemove: (r: TicketRef) => Promise<unknown>;
}) {
  const t = useT();
  const [adding, setAdding] = useState<string | null>(null);
  const [invalid, setInvalid] = useState(false);
  const close = () => {
    setAdding(null);
    setInvalid(false);
  };
  return (
    <Stack gap={4} role="group" aria-label={label}>
      <Group gap={6} justify="space-between" wrap="nowrap">
        <Text size="sm" fw={500}>
          {label}
        </Text>
        {onAdd && adding === null && (
          <ActionIcon variant="subtle" color="gray" size="sm" aria-label={t("ticket.addRelation", { relation: label })} onClick={() => setAdding("")}>
            <Plus size={14} />
          </ActionIcon>
        )}
      </Group>
      {note && (
        <Text size="xs" c="dimmed">
          {note}
        </Text>
      )}
      {refs.length === 0 && adding === null && (
        <Text size="sm" c="dimmed">
          {t("ticket.noRelation")}
        </Text>
      )}
      {refs.map((r) => (
        <Group key={r.url} gap={6} wrap="nowrap" align="flex-start">
          <Anchor component={TicketLink} repo={r.repository} ticketKey={r.key} size="sm" style={{ flex: 1, minWidth: 0, overflowWrap: "anywhere" }}>
            {labelOf(r, ticket)} {r.title}
          </Anchor>
          <StateBadge tone={r.state === "open" ? "success" : "neutral"}>{t(r.state === "open" ? "ticket.open" : "ticket.closed")}</StateBadge>
          <ActionIcon
            variant="subtle"
            color="gray"
            size="sm"
            aria-label={t("ticket.removeRelation", { ticket: labelOf(r, ticket), relation: label })}
            disabled={pending}
            onClick={() => void onRemove(r).catch(() => undefined)}
          >
            <X size={14} />
          </ActionIcon>
        </Group>
      ))}
      {adding !== null && onAdd && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            const named = ticketNamed(adding, ticket);
            setInvalid(!named);
            if (named) void onAdd(named).then(close, () => undefined);
          }}
        >
          <Stack gap={6}>
            <TextInput
              aria-label={t("ticket.relationInput", { relation: label })}
              value={adding}
              onChange={(e) => setAdding(e.currentTarget.value)}
              placeholder={t("ticket.relationPlaceholder")}
              size="xs"
              autoFocus
            />
            {invalid && <Refusal>{t("ticket.relationInvalid")}</Refusal>}
            <Group gap={6} justify="flex-end">
              <Button size="xs" variant="default" onClick={close}>
                {t("common.cancel")}
              </Button>
              <Button size="xs" type="submit" disabled={pending}>
                {t("ticket.addRelationSubmit")}
              </Button>
            </Group>
          </Stack>
        </form>
      )}
    </Stack>
  );
}
