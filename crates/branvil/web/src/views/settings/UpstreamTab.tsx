import { Anchor, Button, Group, Select, Stack, Text, TextInput } from "@mantine/core";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { isApiError } from "../../api/client";
import * as api from "../../api/endpoints";
import { PROVIDERS, type Repository } from "../../api/types";
import { ErrorMessage, Refusal, Sha, StateBadge, Time, type Tone } from "../../components/common";
import { useT } from "../../i18n";
import { useMembers } from "../../query/hooks";
import { keys } from "../../query/keys";

const PICK_AGAIN = "\u0000pick";

/** The upstream badge's tone: stopped, failing, or following. */
export function upstreamTone(repo: Repository): Tone {
  const f = repo.upstream?.following;
  if (f?.stopped) return "danger";
  if (f?.error) return "warning";
  return "success";
}

/**
 * The repository's upstream: the host repository its default branch follows
 * and its pull requests are published to, what the last check found, and whose
 * account reads it.
 */
export function UpstreamTab({ repo }: { repo: Repository }) {
  const t = useT();
  const client = useQueryClient();
  const up = repo.upstream;
  const [form, setForm] = useState({ provider: up?.provider ?? PROVIDERS[0].name, repository: up?.repository ?? "" });
  const settle = () => {
    void client.invalidateQueries({ queryKey: keys.repos() });
    void client.invalidateQueries({ queryKey: keys.repo(repo.path) });
  };
  const save = useMutation({
    mutationFn: () => api.repos.setUpstream(repo.path, { provider: form.provider, repository: form.repository.trim() }),
    onSettled: settle,
  });
  const remove = useMutation({ mutationFn: () => api.repos.deleteUpstream(repo.path), onSettled: settle });
  // null is no change; PICK_AGAIN asks the service to pick a reader again.
  const [readerChoice, setReaderChoice] = useState<string | null>(null);
  const changeReader = useMutation({
    mutationFn: () =>
      api.repos.setUpstream(repo.path, {
        provider: up?.provider ?? "",
        repository: up?.repository ?? "",
        reader: readerChoice === PICK_AGAIN ? null : readerChoice,
      }),
    onSuccess: () => setReaderChoice(null),
    onSettled: settle,
  });
  const members = useMembers();
  const withAccount = (members.data?.items ?? []).filter((m) => up && m.accounts.some((a) => a.provider === up.provider));
  const check = useMutation({ mutationFn: () => api.repos.refreshUpstream(repo.path), onSettled: settle });
  const hostLabel = PROVIDERS.find((p) => p.name === form.provider)?.label ?? form.provider;
  const failure = save.error ?? remove.error ?? check.error ?? changeReader.error;

  return (
    <Stack gap="md">
      <Text size="sm" c="var(--color-text-subtle)">
        {t("upstream.explain")}
      </Text>
      {up ? (
        <dl>
          <dt>{t("upstream.title")}</dt>
          <dd>
            <Anchor href={up.url} target="_blank" rel="noopener noreferrer" size="sm">
              {up.host} {up.repository}
            </Anchor>
          </dd>
          <dt>{t("upstream.state")}</dt>
          <dd>
            <Group gap="xs">
              <StateBadge tone={upstreamTone(repo)}>
                {up.following.stopped ? t("upstream.stopped") : up.following.error ? t("upstream.failing") : t("upstream.following", { branch: repo.default_branch })}
              </StateBadge>
              {up.following.tip && <Sha repo={repo.path} value={up.following.tip} />}
              <Text span size="xs" c="dimmed">
                {t("upstream.checked")} {up.following.checked_at ? <Time value={up.following.checked_at} /> : "—"}
              </Text>
            </Group>
            {(up.following.stopped ?? up.following.error) && (
              <Text size="sm" c="var(--color-danger-text)" mt={4}>
                {up.following.stopped ?? up.following.error}
              </Text>
            )}
          </dd>
          <dt>{t("upstream.reader")}</dt>
          <dd>
            {up.reader.member ? (
              <Group gap="xs">
                <strong>{up.reader.member}</strong>
                <Text span size="sm" c="dimmed">
                  ({t(up.reader.chosen ? "upstream.readerChosen" : "upstream.readerPicked")})
                </Text>
              </Group>
            ) : (
              <Text span size="sm" c="var(--color-danger-text)">
                {t("upstream.noReader")}
              </Text>
            )}
          </dd>
        </dl>
      ) : (
        <Text size="sm" c="dimmed">
          {t("upstream.none")}
        </Text>
      )}
      {up && (
        <Group align="flex-end" gap="sm">
          <Select
            label={t("upstream.readerChange")}
            placeholder={t("upstream.readerChoose")}
            style={{ flex: 1 }}
            value={readerChoice}
            onChange={setReaderChoice}
            data={[
              ...withAccount.map((m) => ({ value: m.name, label: `${m.name} (${m.accounts.find((a) => a.provider === up.provider)?.login ?? ""})` })),
              { group: " ", items: [{ value: PICK_AGAIN, label: t("upstream.readerPickAgain") }] },
            ]}
          />
          <Button variant="default" disabled={!readerChoice || changeReader.isPending} onClick={() => changeReader.mutate()}>
            {t("upstream.readerApply")}
          </Button>
        </Group>
      )}
      <form
        aria-label={t("upstream.set")}
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <Group align="flex-end" gap="sm">
          <Select
            label={t("upstream.provider")}
            w={140}
            value={form.provider}
            allowDeselect={false}
            onChange={(v) => v && setForm({ ...form, provider: v })}
            data={PROVIDERS.map((p) => ({ value: p.name, label: p.label }))}
          />
          <TextInput
            label={t("upstream.repository")}
            style={{ flex: 1 }}
            value={form.repository}
            onChange={(e) => setForm({ ...form, repository: e.currentTarget.value })}
            placeholder="owner/name"
            required
          />
          <Button type="submit" variant="default" disabled={save.isPending || !form.repository.trim()}>
            {t("upstream.set")}
          </Button>
        </Group>
      </form>
      {up && (
        <Group gap="sm" pt="md" style={{ borderTop: "1px solid var(--color-border)" }}>
          <Button variant="default" onClick={() => check.mutate()} disabled={check.isPending}>
            {t("upstream.checkNow")}
          </Button>
          <Button
            variant="light"
            color="danger"
            onClick={() => window.confirm(t("upstream.confirmRemove", { repository: up.repository })) && remove.mutate()}
            disabled={remove.isPending}
          >
            {t("upstream.remove")}
          </Button>
        </Group>
      )}
      {failure && (isApiError(failure, "no_account") ? <Refusal>{t("upstream.noAccount", { host: hostLabel })}</Refusal> : <ErrorMessage error={failure} />)}
    </Stack>
  );
}
