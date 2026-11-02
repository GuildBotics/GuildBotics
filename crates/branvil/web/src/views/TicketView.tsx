import { ActionIcon, Anchor, Button, Card, Chip, Group, Menu, Select, Stack, Switch, Text, TextInput } from "@mantine/core";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, Pencil } from "lucide-react";
import { useState } from "react";
import { isApiError } from "../api/client";
import * as api from "../api/endpoints";
import { LANES, type Lane, type Reaction, type Ticket, type TicketRef } from "../api/types";
import { ComposeForm, CopyButton, ErrorMessage, Loading, Post, PostHead, Refusal, StateBadge } from "../components/common";
import { BodyEditor, EditableTitle, EditPencil } from "../components/Editable";
import { Markdown } from "../components/Markdown";
import { PageHeader } from "../components/PageHeader";
import { AssigneePicker } from "../components/PeoplePicker";
import { useT } from "../i18n";
import { useMemberKinds, useRepo, useTicket, useTicketComments, useTicketPulls, usePeople } from "../query/hooks";
import { keys } from "../query/keys";
import { ServiceLink } from "../router/Router";
import { labelOf, Relations, relationRefusal } from "./ticket/Relations";
import { TicketComment } from "./ticket/TicketComment";
import { TicketLink } from "./ticket/TicketLink";

/** A ticket: its page, or, in a `drawer` over a page, whose URL it does not take: then it offers its own to copy. */
export function TicketView({ repo, ticketKey, drawer }: { repo: string; ticketKey: string; drawer?: boolean }) {
  const t = useT();
  const client = useQueryClient();
  const ticket = useTicket(repo, ticketKey);
  // The repository defines the labels its tickets may carry.
  const repoQuery = useRepo(repo);
  const comments = useTicketComments(repo, ticketKey);
  const pulls = useTicketPulls(repo, ticketKey);
  const kindOf = useMemberKinds();
  const [editingBody, setEditingBody] = useState(false);
  const refresh = () => void client.invalidateQueries({ queryKey: keys.ticket(repo, ticketKey) });
  const refreshBoards = () => void client.invalidateQueries({ queryKey: ["board"] });
  const people = usePeople();

  const addComment = useMutation({
    mutationFn: (body: string) => api.tickets.addComment(repo, ticketKey, body),
    onSettled: refresh,
  });
  const react = useMutation({
    mutationFn: ({ id, reaction, on }: { id: string; reaction: Reaction; on: boolean }) => api.tickets.react(repo, ticketKey, id, reaction, on),
    onSettled: refresh,
  });
  const update = useMutation({
    mutationFn: (patch: api.TicketPatch) => api.tickets.update(repo, ticketKey, patch),
    onSuccess: (res) => client.setQueryData(keys.ticket(repo, ticketKey), res.ticket),
    onError: (err) => {
      if (isApiError(err, "conflict")) refresh();
    },
    onSettled: () => {
      refresh();
      refreshBoards();
    },
  });
  const assignee = useMutation({
    mutationFn: (name: string | null) => (name ? api.tickets.assign(repo, ticketKey, name) : api.tickets.unassign(repo, ticketKey)),
    onSettled: () => {
      refresh();
      refreshBoards();
    },
  });
  const place = useMutation({
    mutationFn: (lane: Lane) => api.tickets.place(repo, ticketKey, { lane }),
    onSettled: () => {
      refresh();
      refreshBoards();
    },
  });

  if (ticket.isPending) return <Loading />;
  if (ticket.isError) return <ErrorMessage error={ticket.error} />;
  const tk = ticket.data;
  // The revision read when editing began makes a stale edit fail with 409.
  const save = (patch: api.TicketPatch) => update.mutateAsync({ ...patch, revision: tk.revision });

  return (
    <Stack gap="lg">
      <PageHeader
        title={
          <Group gap={4} wrap="nowrap" align="center">
            <EditableTitle prefix={`#${tk.key}`} value={tk.title} pending={update.isPending} onSave={(title) => save({ title })} />
            {drawer && <CopyButton value={tk.url} label={t("ticket.copyUrl")} />}
          </Group>
        }
        meta={
          <>
            <StateBadge tone={tk.state === "open" ? "success" : "neutral"}>{t(tk.state === "open" ? "ticket.open" : "ticket.closed")}</StateBadge>
            {tk.state_reason && <Text span size="sm" c="dimmed">{t(`ticket.reason.${tk.state_reason}`)}</Text>}
            <span>{repo}</span>
          </>
        }
        right={
          <AssigneePicker current={tk.assignee?.name ?? null} people={people} disabled={assignee.isPending} onChoose={(name) => assignee.mutate(name)} />
        }
      />
      {update.isError && <TicketError error={update.error} />}
      {assignee.isError && <ErrorMessage error={assignee.error} />}

      <div className="detail">
        <Stack gap="md">
          <Post
            head={
              <PostHead author={tk.author} kind={kindOf(tk.author)} at={tk.created_at}>
                {!editingBody && <EditPencil label={t("ticket.editBody")} onClick={() => setEditingBody(true)} />}
              </PostHead>
            }
          >
            {editingBody ? (
              <BodyEditor
                value={tk.body}
                pending={update.isPending}
                attachTo={repo}
                onCancel={() => setEditingBody(false)}
                onSave={(body) => void save({ body }).then(() => setEditingBody(false), () => undefined)}
              />
            ) : tk.body.trim() ? (
              <Markdown repo={repo} source={tk.body} />
            ) : (
              <Text size="sm" c="dimmed">
                {t("common.notProvided")}
              </Text>
            )}
          </Post>

          <Card component="section" aria-label={t("ticket.comments")} p="lg">
            <div className="timeline">
              {comments.isPending ? (
                <Loading />
              ) : comments.isError ? (
                <ErrorMessage error={comments.error} />
              ) : (
                comments.data.items.map((c) => (
                  <TicketComment key={c.id} repo={repo} ticketKey={ticketKey} comment={c} kind={kindOf(c.author)} onReact={(reaction, on) => react.mutate({ id: c.id, reaction, on })} />
                ))
              )}
              {react.isError && <ErrorMessage error={react.error} />}
              <ComposeForm
                label={t("comment.new")}
                submitLabel={t("comment.submit")}
                pending={addComment.isPending}
                error={addComment.error}
                onSubmit={(body) => addComment.mutateAsync(body)}
                attachTo={repo}
              />
            </div>
          </Card>
        </Stack>

        <aside className="detail-side">
          <Card p="md">
            <Stack gap="md">
              <Select
                label={t("ticket.lane")}
                value={tk.lane}
                disabled={place.isPending}
                allowDeselect={false}
                onChange={(v) => v && place.mutate(v as Lane)}
                data={LANES.map((l) => ({ value: l, label: t(`lane.${l}`) }))}
              />
              {place.isError && <TicketError error={place.error} />}
              <Stack gap={6}>
                <Text size="sm" fw={500}>
                  {t("ticket.labels")}
                </Text>
                {repoQuery.data && repoQuery.data.labels.length === 0 ? (
                  <Text size="xs" c="dimmed">
                    {t("ticket.noLabelsDefined")}
                  </Text>
                ) : (
                  <Group gap={6} role="group" aria-label={t("ticket.labels")}>
                    {(repoQuery.data?.labels ?? tk.labels).map((l) => (
                      <Chip
                        key={l}
                        size="xs"
                        checked={tk.labels.includes(l)}
                        disabled={update.isPending}
                        onChange={(on) => update.mutate(on ? { add_labels: [l] } : { remove_labels: [l] })}
                      >
                        {l}
                      </Chip>
                    ))}
                  </Group>
                )}
              </Stack>
              <Switch
                label={t("ticket.interactive")}
                description={tk.interactive_ancestor ? <InheritedInteractive ancestor={tk.interactive_ancestor} ticket={tk} /> : t("ticket.interactiveHelp")}
                checked={tk.interactive || tk.interactive_ancestor !== null}
                disabled={update.isPending || tk.interactive_ancestor !== null}
                onChange={(e) => update.mutate({ interactive: e.currentTarget.checked })}
              />
              {repoQuery.data?.upstream && (
                <UpstreamIssueField
                  host={repoQuery.data.upstream.host}
                  ticket={tk}
                  pending={update.isPending}
                  onSave={(url) => update.mutateAsync({ upstream_issue: url })}
                />
              )}
            </Stack>
          </Card>

          <Relations ticket={tk} />

          <Card p="md" component="section" aria-label={t("ticket.linkedPulls")}>
            <Text fw={700} mb="xs">
              {t("ticket.linkedPulls")}
            </Text>
            {pulls.isPending ? (
              <Loading />
            ) : pulls.isError ? (
              <ErrorMessage error={pulls.error} />
            ) : pulls.data.items.length === 0 ? (
              <Text size="sm" c="dimmed">
                {t("ticket.noPulls")}
              </Text>
            ) : (
              <Stack gap="xs">
                {pulls.data.items.map((p) => (
                  <div key={p.url}>
                    <Anchor component={ServiceLink} url={p.url} size="sm">
                      {p.label} {p.title}
                    </Anchor>
                    <Group gap={6} mt={4}>
                      <StateBadge tone={p.merged ? "success" : p.state === "open" ? "success" : "neutral"}>{t(`pull.state.${p.merged ? "merged" : p.state}`)}</StateBadge>
                      {p.closes && (
                        <Text span size="xs" c="dimmed">
                          {t("ticket.closesThis")}
                        </Text>
                      )}
                    </Group>
                  </div>
                ))}
              </Stack>
            )}
          </Card>

          <CloseButton ticket={tk} pending={update.isPending} onChange={(patch) => update.mutate(patch)} />
        </aside>
      </div>
    </Stack>
  );
}

/** A refused change of the ticket, in the GUI's language where its reason is known. */
function TicketError({ error }: { error: unknown }) {
  const t = useT();
  if (isApiError(error, "conflict")) return <Refusal>{t("ticket.conflict")}</Refusal>;
  const refusal = relationRefusal(error, t);
  return refusal ? <Refusal>{refusal}</Refusal> : <ErrorMessage error={error} />;
}

/** Close (as completed, or as not planned from its menu), or reopen. */
function CloseButton({ ticket, pending, onChange }: { ticket: Ticket; pending: boolean; onChange: (patch: api.TicketPatch) => void }) {
  const t = useT();
  if (ticket.state === "closed") {
    return (
      <Button variant="default" fullWidth disabled={pending} onClick={() => onChange({ state: "open", state_reason: null })}>
        {t("ticket.reopen")}
      </Button>
    );
  }
  return (
    <Group gap={0} wrap="nowrap">
      <Button
        variant="default"
        style={{ flex: 1, borderTopRightRadius: 0, borderBottomRightRadius: 0 }}
        disabled={pending}
        onClick={() => onChange({ state: "closed", state_reason: "completed" })}
      >
        {t("ticket.close")}
      </Button>
      <Menu position="bottom-end" withinPortal>
        <Menu.Target>
          <ActionIcon
            variant="default"
            size={36}
            aria-label={t("ticket.closeReason")}
            disabled={pending}
            style={{ borderTopLeftRadius: 0, borderBottomLeftRadius: 0, borderLeft: 0 }}
          >
            <ChevronDown size={16} />
          </ActionIcon>
        </Menu.Target>
        <Menu.Dropdown>
          <Menu.Item onClick={() => onChange({ state: "closed", state_reason: "completed" })}>{t("ticket.closeCompleted")}</Menu.Item>
          <Menu.Item onClick={() => onChange({ state: "closed", state_reason: "not_planned" })}>{t("ticket.closeNotPlanned")}</Menu.Item>
        </Menu.Dropdown>
      </Menu>
    </Group>
  );
}

/** The issue on the upstream host a ticket stands for: closed by its published pull request. */
function UpstreamIssueField({
  host,
  ticket,
  pending,
  onSave,
}: {
  host: string;
  ticket: Ticket;
  pending: boolean;
  onSave: (url: string | null) => Promise<unknown>;
}) {
  const t = useT();
  const [editing, setEditing] = useState<string | null>(null);
  const label = t("ticket.upstreamIssue", { host });
  if (editing === null) {
    return (
      <Stack gap={4}>
        <Text size="sm" fw={500}>
          {label}
        </Text>
        <Group gap={6} wrap="nowrap">
          {ticket.upstream_issue ? (
            <Anchor href={ticket.upstream_issue} target="_blank" rel="noopener noreferrer" size="sm" style={{ overflowWrap: "anywhere" }}>
              {issueLabel(ticket.upstream_issue)}
            </Anchor>
          ) : (
            <Text size="sm" c="dimmed">
              {t("ticket.noUpstreamIssue")}
            </Text>
          )}
          <ActionIcon variant="subtle" color="gray" size="sm" aria-label={t("ticket.editUpstreamIssue")} onClick={() => setEditing(ticket.upstream_issue ?? "")}>
            <Pencil size={14} />
          </ActionIcon>
        </Group>
      </Stack>
    );
  }
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        void onSave(editing.trim() || null).then(() => setEditing(null), () => undefined);
      }}
    >
      <Stack gap={6}>
        <TextInput
          label={label}
          aria-label={t("ticket.upstreamIssueUrl")}
          value={editing}
          onChange={(e) => setEditing(e.currentTarget.value)}
          placeholder="https://github.com/owner/name/issues/1"
          size="xs"
          autoFocus
        />
        <Group gap={6} justify="flex-end">
          <Button size="xs" variant="default" onClick={() => setEditing(null)}>
            {t("common.cancel")}
          </Button>
          <Button size="xs" type="submit" disabled={pending}>
            {t("common.save")}
          </Button>
        </Group>
      </Stack>
    </form>
  );
}

/** `owner/name#7` for an issue URL of the usual shape; the URL itself otherwise. */
function issueLabel(url: string): string {
  const m = /^https?:\/\/[^/]+\/([^/]+\/[^/]+)\/issues\/(\d+)/.exec(url);
  return m ? `${m[1]}#${m[2]}` : url;
}

/** Why a ticket is interactive through its `ancestor`: the message, naming the ancestor with a link to it. */
function InheritedInteractive({ ancestor, ticket }: { ancestor: TicketRef; ticket: Ticket }) {
  const t = useT();
  const [before, after] = t("ticket.interactiveInherited").split("{ticket}");
  return (
    <>
      {before}
      <Anchor component={TicketLink} repo={ancestor.repository} ticketKey={ancestor.key} inherit>
        {labelOf(ancestor, ticket)} {ancestor.title}
      </Anchor>
      {after}
    </>
  );
}
