import { Button, Chip, Group, Modal, Select, Stack, Text, TextInput } from "@mantine/core";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState, type ReactNode } from "react";
import * as api from "../../api/endpoints";
import type { Lane, Repository, Ticket } from "../../api/types";
import { ErrorMessage } from "../../components/common";
import { hasAttachment, MarkdownField } from "../../components/MarkdownField";
import { useT } from "../../i18n";
import { useRepos } from "../../query/hooks";
import { keys } from "../../query/keys";
import { useStored } from "../../storage";

/** The workspace's repositories, and the one new tickets go to by default: the last one chosen in this workspace. */
export function useTicketRepos(workspace: string) {
  const query = useRepos();
  const repos = useMemo(() => (query.data?.items ?? []).filter((r) => r.path.startsWith(`${workspace}/`)), [query.data, workspace]);
  const [chosen, choose] = useStored(`branvil.repo.${workspace}`);
  return { query, repos, repo: repos.find((r) => r.path === chosen)?.path ?? repos[0]?.path ?? "", chooseRepo: choose };
}

interface Draft {
  repo: string;
  title: string;
  body: string;
  labels: string[];
}

function useCreate(workspace: string, onCreated: (t: Ticket) => void) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ draft, lane }: { draft: Draft; lane: Lane }) =>
      api.tickets.create(draft.repo, { title: draft.title.trim(), body: draft.body, labels: draft.labels, lane }),
    onSuccess: onCreated,
    onSettled: () => void client.invalidateQueries({ queryKey: keys.board(workspace) }),
  });
}

/** Chooses among the workspace's repositories; shown only when there is more than one. */
function RepoChoice({ repos, value, onChange, fixed }: { repos: Repository[]; value: string; onChange: (r: string) => void; fixed?: string }) {
  const t = useT();
  if (repos.length < 2) return null;
  return (
    <Select
      label={t("board.repository")}
      data={repos.map((r) => r.path)}
      value={value}
      onChange={(v) => v && onChange(v)}
      allowDeselect={false}
      disabled={fixed !== undefined}
      description={fixed}
    />
  );
}

/** A new ticket with its body and labels, in the lane it was asked for. */
export function NewTicketModal({
  workspace,
  lanes,
  initialLane,
  repos,
  repo,
  onRepo,
  onClose,
  note,
  onCreated,
}: {
  workspace: string;
  lanes: Lane[];
  /** The lane it opened with, or null while closed. */
  initialLane: Lane | null;
  repos: Repository[];
  repo: string;
  onRepo: (repo: string) => void;
  onClose: () => void;
  /** What the new ticket is made as, said above the form. */
  note?: ReactNode;
  onCreated?: (ticket: Ticket) => void;
}) {
  const t = useT();
  const [lane, setLane] = useState<Lane | null>(null);
  const [draft, setDraft] = useState<Omit<Draft, "repo"> | null>(null);
  const [uploading, setUploading] = useState(false);
  const current = draft ?? { title: "", body: "", labels: [] };
  const chosenLane = lane ?? initialLane ?? lanes[0] ?? "new";
  const repository = repos.find((r) => r.path === repo);
  const create = useCreate(workspace, (ticket) => {
    onCreated?.(ticket);
    close();
  });
  function close() {
    setLane(null);
    setDraft(null);
    create.reset();
    onClose();
  }
  return (
    <Modal
      opened={initialLane !== null}
      onClose={close}
      title={t("board.newTicketIn", { lane: t(`lane.${chosenLane}`) })}
      closeButtonProps={{ "aria-label": t("common.close") }}
      centered
      size="lg"
    >
      <form
        aria-label={t("board.newTicket")}
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate({ draft: { ...current, repo }, lane: chosenLane });
        }}
      >
        <Stack gap="md">
          {note && <Text size="sm">{note}</Text>}
          <Group grow align="flex-start">
            {/* An attached image belongs to the repository it was uploaded to. */}
            {repos.length > 1 && <RepoChoice repos={repos} value={repo} fixed={uploading || hasAttachment(current.body) ? t("attach.repoFixed") : undefined} onChange={(r) => {
              onRepo(r);
              setDraft({ ...current, labels: [] });
            }} />}
            <Select
              label={t("ticket.lane")}
              data={lanes.map((l) => ({ value: l, label: t(`lane.${l}`) }))}
              value={chosenLane}
              onChange={(v) => v && setLane(v as Lane)}
              allowDeselect={false}
            />
          </Group>
          <TextInput label={t("ticket.title")} value={current.title} onChange={(e) => setDraft({ ...current, title: e.currentTarget.value })} required data-autofocus />
          <MarkdownField label={t("ticket.body")} value={current.body} onChange={(body) => setDraft({ ...current, body })} rows={6} attachTo={repo} onUploading={setUploading} />
          {repository && repository.labels.length > 0 && (
            <Stack gap={6}>
              <Text size="sm" fw={500}>
                {t("ticket.labels")}
              </Text>
              <Chip.Group multiple value={current.labels} onChange={(labels) => setDraft({ ...current, labels })}>
                <Group gap={6}>
                  {repository.labels.map((l) => (
                    <Chip key={l} value={l} size="xs">
                      {l}
                    </Chip>
                  ))}
                </Group>
              </Chip.Group>
            </Stack>
          )}
          {create.isError && <ErrorMessage error={create.error} />}
          <Group justify="flex-end">
            <Button variant="default" onClick={close}>
              {t("common.cancel")}
            </Button>
            <Button type="submit" disabled={create.isPending || uploading || !current.title.trim() || !repo}>
              {t("board.create")}
            </Button>
          </Group>
        </Stack>
      </form>
    </Modal>
  );
}
