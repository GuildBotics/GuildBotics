import { Button, Card, FileButton, Group, Modal, PasswordInput, SegmentedControl, Select, Stack, Tabs, Text, Textarea, TextInput } from "@mantine/core";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { useState } from "react";
import * as api from "../../api/endpoints";
import { PROVIDERS, type AccountCredential, type IssuedToken, type Member, type MemberKind } from "../../api/types";
import { CopyButton, ErrorMessage, Loading, MASKED_SECRET, MemberAvatar, StateBadge, Time } from "../../components/common";
import { PanelHeader } from "../../components/PanelHeader";
import { PeopleToggle, type Person } from "../../components/PeoplePicker";
import { useT } from "../../i18n";
import { useMemberTokens, useMembers, usePeople } from "../../query/hooks";
import { keys } from "../../query/keys";

type NewKind = Exclude<MemberKind, "owner">;

/** The owner, agents, and runners (their kind decides what they may do), as GuildBotics lists its members. */
export function MembersSettings() {
  const t = useT();
  const members = useMembers();
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const people = usePeople();
  return (
    <Card p="lg">
      <PanelHeader
        title={t("settings.members")}
        description={t("settings.membersHelp")}
        action={
          <Button variant="default" leftSection={<Plus size={16} />} onClick={() => setAdding(true)}>
            {t("member.create")}
          </Button>
        }
      />
      {members.isPending ? (
        <Loading />
      ) : members.isError ? (
        <ErrorMessage error={members.error} />
      ) : (
        <Stack gap={6} role="list" aria-label={t("settings.members")}>
          {members.data.items.map((m) => (
            <div key={m.name} role="listitem">
              <div className="setting-row">
                <MemberAvatar name={m.name} kind={m.kind} size={26} />
                <Text span size="sm" className="setting-row-name">
                  {m.display_name ? `${m.display_name} (${m.name})` : m.name}
                </Text>
                <StateBadge>{t(`member.kind.${m.kind}`)}</StateBadge>
                {m.kind !== "runner" &&
                  PROVIDERS.map((p) => {
                    const a = m.accounts.find((x) => x.provider === p.name);
                    return a ? (
                      <StateBadge key={p.name} tone="success">
                        {p.label} {a.login}
                      </StateBadge>
                    ) : (
                      <StateBadge key={p.name}>{t("account.noneOn", { host: p.label })}</StateBadge>
                    );
                  })}
                <Button size="xs" variant="default" onClick={() => setEditing(editing === m.name ? null : m.name)} aria-expanded={editing === m.name}>
                  {t("common.edit")}
                </Button>
              </div>
              {editing === m.name && (
                <MemberEditor member={m} people={people} />
              )}
            </div>
          ))}
        </Stack>
      )}
      <NewMemberModal opened={adding} onClose={() => setAdding(false)} />
    </Card>
  );
}

function NewMemberModal({ opened, onClose }: { opened: boolean; onClose: () => void }) {
  const t = useT();
  const client = useQueryClient();
  const [form, setForm] = useState<{ name: string; display_name: string; kind: NewKind }>({ name: "", display_name: "", kind: "agent" });
  const create = useMutation({
    mutationFn: () => api.members.create({ ...form, name: form.name.trim(), display_name: form.display_name.trim() }),
    onSuccess: () => close(),
    onSettled: () => void client.invalidateQueries({ queryKey: keys.members() }),
  });
  function close() {
    setForm({ name: "", display_name: "", kind: "agent" });
    create.reset();
    onClose();
  }
  return (
    <Modal opened={opened} onClose={close} title={t("member.create")} closeButtonProps={{ "aria-label": t("common.close") }} centered>
      <form
        aria-label={t("member.create")}
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate();
        }}
      >
        <Stack gap="md">
          <TextInput label={t("member.name")} value={form.name} onChange={(e) => setForm({ ...form, name: e.currentTarget.value })} required data-autofocus />
          <TextInput label={t("member.displayName")} value={form.display_name} onChange={(e) => setForm({ ...form, display_name: e.currentTarget.value })} />
          <Select
            label={t("member.kind")}
            value={form.kind}
            allowDeselect={false}
            onChange={(v) => v && setForm({ ...form, kind: v as NewKind })}
            data={[
              { value: "agent", label: t("member.kind.agent") },
              { value: "runner", label: t("member.kind.runner") },
            ]}
          />
          {create.isError && <ErrorMessage error={create.error} />}
          <Group justify="flex-end">
            <Button variant="default" onClick={close}>
              {t("common.cancel")}
            </Button>
            <Button type="submit" disabled={create.isPending || !form.name.trim()}>
              {t("member.createSubmit")}
            </Button>
          </Group>
        </Stack>
      </form>
    </Modal>
  );
}

function MemberEditor({ member, people }: { member: Member; people: Person[] }) {
  const t = useT();
  const client = useQueryClient();
  const [displayName, setDisplayName] = useState(member.display_name);
  const update = useMutation({
    mutationFn: () => api.members.update(member.name, { display_name: displayName }),
    onSettled: () => void client.invalidateQueries({ queryKey: keys.members() }),
  });
  return (
    <Card p="md" mt="sm" mb="sm" withBorder>
      <Tabs defaultValue="basic">
        <Tabs.List>
          <Tabs.Tab value="basic">{t("settings.tabBasic")}</Tabs.Tab>
          {member.kind !== "runner" && <Tabs.Tab value="reviewers">{t("member.reviewers")}</Tabs.Tab>}
          {member.kind !== "runner" && <Tabs.Tab value="accounts">{t("account.title")}</Tabs.Tab>}
          <Tabs.Tab value="tokens">{t("token.title")}</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="basic" pt="md">
          <Picture member={member} />
          <form
            onSubmit={(e) => {
              e.preventDefault();
              update.mutate();
            }}
          >
            <Group align="flex-end" gap="sm">
              <TextInput label={t("member.displayName")} value={displayName} onChange={(e) => setDisplayName(e.currentTarget.value)} style={{ flex: 1 }} />
              <Button type="submit" disabled={update.isPending}>
                {t("common.save")}
              </Button>
            </Group>
            {update.isError && <ErrorMessage error={update.error} />}
          </form>
        </Tabs.Panel>
        {member.kind !== "runner" && (
          <Tabs.Panel value="reviewers" pt="md">
            <Reviewers member={member} people={people} />
          </Tabs.Panel>
        )}
        {member.kind !== "runner" && (
          <Tabs.Panel value="accounts" pt="md">
            <Stack gap="md">
              <Text size="sm" c="var(--color-text-subtle)">
                {t(member.kind === "owner" ? "account.explainOwner" : "account.explainAgent")}
              </Text>
              {PROVIDERS.map((p) => (
                <AccountRow key={p.name} member={member} provider={p.name} label={p.label} />
              ))}
            </Stack>
          </Tabs.Panel>
        )}
        <Tabs.Panel value="tokens" pt="md">
          <Tokens name={member.name} />
        </Tabs.Panel>
      </Tabs>
    </Card>
  );
}

/** The member's picture: a PNG, JPEG, GIF, or WebP image of up to 4 MiB. */
function Picture({ member }: { member: Member }) {
  const t = useT();
  const client = useQueryClient();
  const settle = () => void client.invalidateQueries({ queryKey: keys.members() });
  const set = useMutation({ mutationFn: (image: File) => api.members.setAvatar(member.name, image), onSettled: settle });
  const remove = useMutation({ mutationFn: () => api.members.removeAvatar(member.name), onSettled: settle });
  const failed = set.error ?? remove.error;
  return (
    <Stack gap="xs" mb="md">
      <Group gap="sm">
        <MemberAvatar name={member.name} kind={member.kind} size={48} />
        <FileButton onChange={(f) => f && set.mutate(f)} accept="image/png,image/jpeg,image/gif,image/webp">
          {(props) => (
            <Button {...props} size="xs" variant="default" loading={set.isPending}>
              {t("member.avatarChoose")}
            </Button>
          )}
        </FileButton>
        {member.avatar_url && (
          <Button size="xs" variant="subtle" color="gray" loading={remove.isPending} onClick={() => remove.mutate()}>
            {t("member.avatarRemove")}
          </Button>
        )}
      </Group>
      {failed && <ErrorMessage error={failed} />}
    </Stack>
  );
}

/** Who is asked to review every pull request the member opens: one at a time, as on a pull request. */
function Reviewers({ member, people }: { member: Member; people: Person[] }) {
  const t = useT();
  const client = useQueryClient();
  const toggle = useMutation({
    mutationFn: ({ name, on }: { name: string; on: boolean }) => api.members.setReviewer(member.name, name, on),
    onSettled: () => void client.invalidateQueries({ queryKey: keys.members(), exact: true }),
  });
  const names = member.reviewers.join(", ");
  return (
    <Stack gap="sm">
      <Text size="sm" c="var(--color-text-subtle)">
        {t("member.reviewersExplain", { name: member.name })}
      </Text>
      <Group gap="sm">
        <PeopleToggle
          people={people}
          except={member.name}
          shown={member.reviewers}
          on={(name) => member.reviewers.includes(name)}
          label={t("member.reviewersTrigger", { name: member.name, names: names || t("member.reviewersNobody") })}
          help={t("member.reviewersHelp", { name: member.name })}
          onToggle={(name, on) => toggle.mutate({ name, on })}
          disabled={toggle.isPending}
          position="bottom-start"
        />
        <Text size="sm" c={names ? undefined : "dimmed"}>
          {names || t("member.reviewersNone")}
        </Text>
      </Group>
      {toggle.isError && <ErrorMessage error={toggle.error} />}
    </Stack>
  );
}

type AccountForm = { kind: "token" | "app"; token: string; app_id: string; installation_id: string; private_key: string };

/** The credential a filled form sends, or `null` while something is missing. */
function credentialOf(f: AccountForm): AccountCredential | null {
  if (f.kind === "token") return f.token ? { token: f.token } : null;
  const app = { app_id: f.app_id.trim(), installation_id: f.installation_id.trim(), private_key: f.private_key };
  return app.app_id && app.installation_id && app.private_key.trim() ? { app } : null;
}

function AccountRow({ member, provider, label }: { member: Member; provider: string; label: string }) {
  const t = useT();
  const client = useQueryClient();
  const account = member.accounts.find((a) => a.provider === provider);
  // The secrets live only in this form until they are sent; they are never shown again.
  const empty: AccountForm = { kind: account?.kind ?? "token", token: "", app_id: "", installation_id: "", private_key: "" };
  const [form, setForm] = useState(empty);
  const credential = credentialOf(form);
  const settle = () => void client.invalidateQueries({ queryKey: keys.members() });
  const set = useMutation({
    mutationFn: (c: AccountCredential) => api.members.setAccount(member.name, provider, c),
    onSuccess: () => setForm((f) => ({ ...empty, kind: f.kind })),
    onSettled: settle,
  });
  const remove = useMutation({ mutationFn: () => api.members.deleteAccount(member.name, provider), onSettled: settle });
  const field = (k: keyof AccountForm) => (e: { currentTarget: { value: string } }) => setForm({ ...form, [k]: e.currentTarget.value });
  return (
    <Stack gap="xs">
      <Group gap="xs">
        <Text fw={600} size="sm">
          {label}
        </Text>
        {account ? (
          <>
            <StateBadge tone="success">{account.login}</StateBadge>
            <StateBadge>{t(`account.kind.${account.kind}`, { host: label })}</StateBadge>
            <Text span size="xs" c="dimmed">
              {t("account.setAt")} <Time value={account.set_at} />
            </Text>
            <Button
              size="compact-xs"
              variant="light"
              color="danger"
              disabled={remove.isPending}
              onClick={() => window.confirm(t("account.confirmRemove", { host: label, login: account.login })) && remove.mutate()}
            >
              {t("account.remove")}
            </Button>
          </>
        ) : (
          <StateBadge>{t("account.none")}</StateBadge>
        )}
      </Group>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (credential) set.mutate(credential);
        }}
      >
        <Stack gap="xs">
          <SegmentedControl
            aria-label={t("account.form", { host: label })}
            value={form.kind}
            onChange={(v) => setForm({ ...form, kind: v as AccountForm["kind"] })}
            data={(["token", "app"] as const).map((k) => ({ value: k, label: t(`account.kind.${k}`, { host: label }) }))}
            style={{ alignSelf: "flex-start" }}
          />
          {form.kind === "token" ? (
            <PasswordInput
              label={t(account ? "account.replaceToken" : "account.token", { host: label })}
              autoComplete="off"
              placeholder={account?.kind === "token" ? MASKED_SECRET : undefined}
              value={form.token}
              onChange={field("token")}
            />
          ) : (
            <>
              <Text size="xs" c="dimmed">
                {t("account.appHelp", { host: label })}
              </Text>
              <Group grow gap="sm">
                <TextInput label={t("account.appId")} autoComplete="off" value={form.app_id} onChange={field("app_id")} />
                <TextInput label={t("account.installationId")} autoComplete="off" value={form.installation_id} onChange={field("installation_id")} />
              </Group>
              <Textarea
                label={t("account.privateKey")}
                autoComplete="off"
                spellCheck={false}
                rows={4}
                styles={{ input: { fontFamily: "var(--mantine-font-family-monospace)" } }}
                placeholder={account?.kind === "app" ? MASKED_SECRET : undefined}
                value={form.private_key}
                onChange={field("private_key")}
              />
              <FileButton accept=".pem,application/x-pem-file" inputProps={{ "aria-label": t("account.privateKeyFile") }} onChange={(f) => void f?.text().then((private_key) => setForm((cur) => ({ ...cur, private_key })))}>
                {(props) => (
                  <Button {...props} size="xs" variant="default" style={{ alignSelf: "flex-start" }}>
                    {t("account.privateKeyFile")}
                  </Button>
                )}
              </FileButton>
            </>
          )}
          <Group justify="flex-end">
            <Button type="submit" variant="default" disabled={set.isPending || !credential}>
              {t("account.save")}
            </Button>
          </Group>
        </Stack>
      </form>
      {(set.isError || remove.isError) && <ErrorMessage error={set.error ?? remove.error} />}
    </Stack>
  );
}

function Tokens({ name }: { name: string }) {
  const t = useT();
  const client = useQueryClient();
  const tokens = useMemberTokens(name);
  const [label, setLabel] = useState("");
  // The issued token lives only in this component's state and is shown once.
  const [issued, setIssued] = useState<IssuedToken | null>(null);
  const settle = () => void client.invalidateQueries({ queryKey: keys.memberTokens(name) });
  const issue = useMutation({
    mutationFn: () => api.members.issueToken(name, label.trim()),
    onSuccess: (tok) => {
      setIssued(tok);
      setLabel("");
    },
    onSettled: settle,
  });
  const revoke = useMutation({ mutationFn: (id: string) => api.members.revokeToken(name, id), onSettled: settle });

  return (
    <Stack gap="md">
      {issued && (
        <Card p="sm" role="alert" style={{ borderColor: "var(--color-warning-border)", background: "var(--color-warning-bg)" }}>
          <Stack gap="xs">
            <Text size="sm" c="var(--color-warning-text)">
              {t("token.showOnce")}
            </Text>
            <Group gap="xs" wrap="nowrap">
              <code className="secret">{issued.token}</code>
              <CopyButton value={issued.token} />
              <Button size="compact-sm" variant="default" ml="auto" onClick={() => setIssued(null)}>
                {t("token.done")}
              </Button>
            </Group>
          </Stack>
        </Card>
      )}
      {tokens.isPending ? (
        <Loading />
      ) : tokens.isError ? (
        <ErrorMessage error={tokens.error} />
      ) : tokens.data.items.length === 0 ? (
        <Text size="sm" c="dimmed">
          {t("token.none")}
        </Text>
      ) : (
        <Stack gap={6}>
          {tokens.data.items.map((tk) => (
            <div className="setting-row" key={tk.id}>
              <Text span size="sm" className="setting-row-name">
                <strong>{tk.label || tk.id}</strong>{" "}
                <Text span size="xs" c="dimmed">
                  {t("token.created")} <Time value={tk.created_at} /> · {t("token.lastUsed")} {tk.last_used_at ? <Time value={tk.last_used_at} /> : "—"}
                </Text>
              </Text>
              <Button
                size="xs"
                variant="light"
                color="danger"
                disabled={revoke.isPending}
                onClick={() => window.confirm(t("token.confirmRevoke", { label: tk.label || tk.id })) && revoke.mutate(tk.id)}
              >
                {t("token.revoke")}
              </Button>
            </div>
          ))}
        </Stack>
      )}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          issue.mutate();
        }}
      >
        <Group align="flex-end" gap="sm">
          <TextInput label={t("token.label")} value={label} onChange={(e) => setLabel(e.currentTarget.value)} style={{ flex: 1 }} />
          <Button type="submit" variant="default" disabled={issue.isPending}>
            {t("token.issue")}
          </Button>
        </Group>
      </form>
      {(issue.isError || revoke.isError) && <ErrorMessage error={issue.error ?? revoke.error} />}
    </Stack>
  );
}
