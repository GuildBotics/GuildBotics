import { Badge, Button, Card, Group, Modal, NumberInput, Stack, Switch, Tabs, TagsInput, Text, TextInput } from "@mantine/core";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { FolderGit2, Plus } from "lucide-react";
import { useState } from "react";
import { isApiError } from "../../api/client";
import * as api from "../../api/endpoints";
import type { Repository } from "../../api/types";
import { CopyButton, ErrorMessage, Loading, Notice, Refusal } from "../../components/common";
import { PanelHeader } from "../../components/PanelHeader";
import { useT } from "../../i18n";
import { useRepos } from "../../query/hooks";
import { keys } from "../../query/keys";
import { UpstreamTab } from "./UpstreamTab";

/** The shown workspace: its board's repositories, each with its editor; `open` names one whose editor starts open on its CI tab. */
export function WorkspaceSettings({ workspace, open = null }: { workspace: string; open?: string | null }) {
  const t = useT();
  const repos = useRepos();
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<string | null>(open);
  const items = (repos.data?.items ?? []).filter((r) => r.path.startsWith(`${workspace}/`));
  return (
    <Card p="lg">
      <PanelHeader title={workspace} description={t("settings.workspaceHelp")} />
      <PanelHeader
        order={4}
        title={
          <Group gap="xs">
            {t("settings.repositories")}
            <Badge color="neutral" variant="light" size="sm">
              {items.length}
            </Badge>
          </Group>
        }
        action={
          <Button size="xs" variant="default" leftSection={<Plus size={14} />} onClick={() => setAdding(true)}>
            {t("repo.create")}
          </Button>
        }
      />
      {repos.isPending ? (
        <Loading />
      ) : repos.isError ? (
        <ErrorMessage error={repos.error} />
      ) : items.length === 0 ? (
        <Text size="sm" c="dimmed">
          {t("settings.noRepos")}
        </Text>
      ) : (
        <Stack gap={6} role="list" aria-label={t("settings.repositories")}>
          {items.map((r) => (
            <div key={r.path} role="listitem">
              <div className="setting-row">
                <FolderGit2 size={18} color="var(--color-text-subtle)" />
                <strong className="setting-row-name" style={{ overflowWrap: "anywhere" }}>{r.path}</strong>
                <Button size="xs" variant="default" onClick={() => setEditing(editing === r.path ? null : r.path)} aria-expanded={editing === r.path}>
                  {t("common.edit")}
                </Button>
              </div>
              {editing === r.path && <RepoEditor repo={r} tab={r.path === open ? "ci" : "basic"} />}
            </div>
          ))}
        </Stack>
      )}
      <NewRepoModal workspace={workspace} opened={adding} onClose={() => setAdding(false)} />
    </Card>
  );
}

function NewRepoModal({ workspace, opened, onClose }: { workspace: string; opened: boolean; onClose: () => void }) {
  const t = useT();
  const client = useQueryClient();
  const [form, setForm] = useState({ name: "", branch: "main", labels: [] as string[] });
  const create = useMutation({
    mutationFn: () => api.repos.create({ path: `${workspace}/${form.name.trim()}`, default_branch: form.branch.trim(), labels: form.labels }),
    onSuccess: () => close(),
    onSettled: () => void client.invalidateQueries({ queryKey: keys.repos() }),
  });
  function close() {
    setForm({ name: "", branch: "main", labels: [] });
    create.reset();
    onClose();
  }
  return (
    <Modal opened={opened} onClose={close} title={t("repo.create")} closeButtonProps={{ "aria-label": t("common.close") }} centered>
      <form
        aria-label={t("repo.create")}
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate();
        }}
      >
        <Stack gap="md">
          <TextInput
            label={t("settings.repoName")}
            leftSection={<Text size="sm" c="dimmed">{`${workspace}/`}</Text>}
            leftSectionWidth={workspace.length * 8 + 24}
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.currentTarget.value })}
            placeholder="demo"
            required
            data-autofocus
          />
          <TextInput label={t("repo.defaultBranch")} value={form.branch} onChange={(e) => setForm({ ...form, branch: e.currentTarget.value })} required />
          <TagsInput label={t("repo.labels")} description={t("settings.tagsHelp")} value={form.labels} onChange={(labels) => setForm({ ...form, labels })} clearable />
          {create.isError && <ErrorMessage error={create.error} />}
          <Group justify="flex-end">
            <Button variant="default" onClick={close}>
              {t("common.cancel")}
            </Button>
            <Button type="submit" disabled={create.isPending || !form.name.trim() || !form.branch.trim()}>
              {t("repo.createSubmit")}
            </Button>
          </Group>
        </Stack>
      </form>
    </Modal>
  );
}

type RepoForm = {
  revision: number;
  default_branch: string;
  protected_branches: string[];
  required_checks: string[];
  /** `""` while the field is empty, which cannot be saved. */
  required_approvals: string | number;
  auto_land: boolean;
  ci_network: boolean;
  labels: string[];
};

const seed = (r: Repository): RepoForm => ({
  revision: r.revision,
  default_branch: r.default_branch,
  protected_branches: r.protected_branches,
  required_checks: r.required_checks,
  required_approvals: r.required_approvals,
  auto_land: r.auto_land,
  ci_network: r.ci_network,
  labels: r.labels,
});

/** A repository's editor: its basics, its upstream, its CI, and its ticket labels, as tabs. */
function RepoEditor({ repo, tab }: { repo: Repository; tab: "basic" | "ci" }) {
  const t = useT();
  const client = useQueryClient();
  // The form keeps the revision it was filled from, so a stale save is refused (409).
  const [form, setForm] = useState<RepoForm>(() => seed(repo));
  const save = useMutation({
    mutationFn: () =>
      api.repos.update(repo.path, { ...form, default_branch: form.default_branch.trim(), required_approvals: Number(form.required_approvals) }),
    onSuccess: (r) => setForm(seed(r)),
    onSettled: () => {
      void client.invalidateQueries({ queryKey: keys.repos() });
      void client.invalidateQueries({ queryKey: keys.repo(repo.path) });
      void client.invalidateQueries({ queryKey: keys.ci() });
    },
  });
  const stale = form.revision !== repo.revision;
  const footer = (
    <Stack gap="xs" mt="md">
      {save.isError && (isApiError(save.error, "conflict") ? <Refusal>{t("settings.conflict")}</Refusal> : <ErrorMessage error={save.error} />)}
      {stale && (
        <Group gap="xs">
          <Text size="sm">{t("settings.changedElsewhere")}</Text>
          <Button size="compact-sm" variant="subtle" onClick={() => setForm(seed(repo))}>
            {t("settings.loadLatest")}
          </Button>
        </Group>
      )}
      {save.isSuccess && !stale && <Notice>{t("settings.saved")}</Notice>}
      <Group>
        <Button onClick={() => save.mutate()} disabled={save.isPending || form.required_approvals === ""}>
          {t("common.save")}
        </Button>
      </Group>
    </Stack>
  );
  return (
    <Card p="md" mt="sm" mb="sm" withBorder>
      <Tabs defaultValue={tab}>
        <Tabs.List>
          <Tabs.Tab value="basic">{t("settings.tabBasic")}</Tabs.Tab>
          <Tabs.Tab value="upstream">{t("upstream.title")}</Tabs.Tab>
          <Tabs.Tab value="ci">{t("settings.tabCi")}</Tabs.Tab>
          <Tabs.Tab value="labels">{t("ticket.labels")}</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="basic" pt="md">
          <Stack gap="md">
            <Stack gap={4}>
              <Text size="sm" fw={500}>
                {t("repo.cloneUrl")}
              </Text>
              <Group gap="xs" wrap="nowrap">
                <code>{repo.clone_url}</code>
                <CopyButton value={repo.clone_url} label={t("repo.copyCloneUrl")} />
              </Group>
            </Stack>
            <TextInput
              label={t("repo.defaultBranch")}
              description={repo.upstream ? t("upstream.defaultBranchFollows") : undefined}
              value={form.default_branch}
              readOnly={repo.upstream !== null}
              onChange={(e) => setForm({ ...form, default_branch: e.currentTarget.value })}
            />
            <TagsInput
              label={t("repo.protectedBranches")}
              description={t("settings.protectedHelp")}
              value={form.protected_branches}
              onChange={(v) => setForm({ ...form, protected_branches: v })}
              placeholder={t("settings.addBranch")}
              clearable
            />
          </Stack>
          {footer}
        </Tabs.Panel>
        <Tabs.Panel value="upstream" pt="md">
          <UpstreamTab repo={repo} />
        </Tabs.Panel>
        <Tabs.Panel value="ci" pt="md">
          <Stack gap="md">
            <Switch
              label={t("settings.ciNetwork")}
              description={t("settings.ciNetworkHelp")}
              checked={form.ci_network}
              onChange={(e) => setForm({ ...form, ci_network: e.currentTarget.checked })}
            />
            <TagsInput
              label={t("repo.requiredChecks")}
              description={t(repo.upstream ? "settings.requiredHelpUpstream" : "settings.requiredHelp")}
              value={form.required_checks}
              onChange={(v) => setForm({ ...form, required_checks: v })}
              placeholder={t("settings.addCheck")}
              clearable
            />
            <NumberInput
              label={t("repo.requiredApprovals")}
              description={t(repo.upstream ? "settings.approvalsHelpUpstream" : "settings.approvalsHelp")}
              value={form.required_approvals}
              onChange={(v) => setForm({ ...form, required_approvals: v })}
              min={0}
              allowNegative={false}
              allowDecimal={false}
            />
            <Switch
              label={t("settings.autoLand")}
              description={repo.upstream ? t("settings.autoLandHelpUpstream", { host: repo.upstream.host }) : t("settings.autoLandHelp")}
              checked={form.auto_land}
              onChange={(e) => setForm({ ...form, auto_land: e.currentTarget.checked })}
            />
          </Stack>
          {footer}
        </Tabs.Panel>
        <Tabs.Panel value="labels" pt="md">
          <TagsInput
            label={t("repo.labels")}
            description={t("settings.labelsHelp")}
            value={form.labels}
            onChange={(v) => setForm({ ...form, labels: v })}
            placeholder={t("settings.addLabel")}
            clearable
          />
          {footer}
        </Tabs.Panel>
      </Tabs>
    </Card>
  );
}
