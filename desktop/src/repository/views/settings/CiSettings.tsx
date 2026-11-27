import {
  Anchor,
  Button,
  Card,
  Group,
  NumberInput,
  Radio,
  SimpleGrid,
  Stack,
  Switch,
  Text,
  TextInput,
} from "@mantine/core";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { FolderGit2 } from "lucide-react";
import { Fragment, useState, type ReactNode } from "react";
import * as api from "../../api/endpoints";
import type { CiLimits, CiRunner, CiSettings as Settings } from "../../api/types";
import {
  ErrorMessage,
  Loading,
  Notice,
  StateBadge,
  Time,
  type Tone,
} from "../../components/common";
import { Header } from "../../../ui/Header";
import { useT, type MessageKey } from "../../i18n";
import { resourceShortfallText } from "../../i18n/resourceShortfall";
import { useCiChange, useCiSettings, useMembers, useRunnerLabel } from "../../query/hooks";
import { keys } from "../../query/keys";
import { Link } from "../../router/Router";
import { href, workspacePath } from "../../router/routes";
import { IMAGE_SECTION_ID } from "../../../setup/EnvironmentImages";
import { Tokens } from "./MembersSettings";

const CONNECTION_TONE: Record<CiRunner["connection"], Tone> = {
  online: "success",
  offline: "warning",
  never_seen: "neutral",
};
const RUNNER_TONE: Record<Settings["runner"]["state"], Tone> = {
  ready: "success",
  no_runner: "warning",
  executor_unavailable: "danger",
  stopping: "warning",
};

/** The owner's CI runner settings and network access for this workspace's jobs. */
export function CiSettings({ workspace }: { workspace: string | null }) {
  const t = useT();
  const ci = useCiSettings();
  const [selected, setSelected] = useState<string | null>(null);
  const runners = ci.data?.runner.runners ?? [];
  const runner = runners.find((r) => r.name === selected) ?? runners[0];
  // The runner first shown stays selected: a runner joining at the top of the
  // polled list must not move the name, tokens, and limits being edited to it.
  if (runner && runner.name !== selected) setSelected(runner.name);
  const repositories = (ci.data?.repositories ?? []).filter(
    ({ path }) => workspace === null || path.startsWith(`${workspace}/`),
  );
  const environment = `${workspace ? workspacePath(workspace) : ""}/setup?section=environment#${IMAGE_SECTION_ID}`;
  return (
    <Card p="lg">
      <Header order={3} title={t("ci.settings.title")} description={t("ci.settings.help")} />
      {ci.isPending ? (
        <Loading />
      ) : ci.isError ? (
        <ErrorMessage error={ci.error} />
      ) : (
        <Stack gap="md">
          <Runner settings={ci.data} selected={runner?.name ?? ""} onSelect={setSelected} />
          <ResourceQueue runners={runners} />
          <Section title={t("ci.settings.executor")}>
            <dl>
              <dt>{t("ci.settings.msb")}</dt>
              <dd>
                {ci.data.executor.msb ? (
                  <code>{ci.data.executor.msb}</code>
                ) : (
                  <Text span size="sm" c="var(--color-danger-text)">
                    {ci.data.executor.problem}
                  </Text>
                )}
              </dd>
            </dl>
            <Anchor component={Link} to={environment} size="sm">
              {t("ci.settings.manageImages")}
            </Anchor>
          </Section>
          {runner && (
            // One key for the selected runner's panels: they are replaced
            // together when another runner is selected or this one is deleted.
            <Fragment key={runner.name}>
              <RunnerName name={runner.name} />
              <Section title={t("token.title")} description={t("ci.settings.tokensHelp")}>
                <RunnerHeading name={runner.name} />
                <Tokens name={runner.name} />
              </Section>
              <Limits runner={runner} />
            </Fragment>
          )}
          <Section title={t("ci.settings.network")} description={t("ci.settings.networkHelp")}>
            {repositories.length === 0 ? (
              <Text size="sm" c="dimmed">
                {t("settings.noRepos")}
              </Text>
            ) : (
              <Stack gap={6} role="list" aria-label={t("ci.settings.network")}>
                {repositories.map((r) => (
                  <div key={r.path} role="listitem" className="setting-row">
                    <FolderGit2 size={18} color="var(--color-text-subtle)" />
                    <span className="setting-row-name">
                      <Anchor component={Link} to={href.repoSettings(r.path)} size="sm">
                        {r.path}
                      </Anchor>
                    </span>
                    <StateBadge tone={r.ci_network ? "warning" : "neutral"}>
                      {t(r.ci_network ? "ci.settings.networkAllowed" : "ci.settings.networkDenied")}
                    </StateBadge>
                  </div>
                ))}
              </Stack>
            )}
          </Section>
        </Stack>
      )}
    </Card>
  );
}

function ResourceQueue({ runners }: { runners: CiRunner[] }) {
  const t = useT();
  const label = useRunnerLabel();
  const waiting = runners.flatMap((r) =>
    r.resource_wait ? [{ name: r.name, ...r.resource_wait }] : [],
  );
  if (!waiting.length) return null;
  return (
    <Section title={t("ci.settings.resourceQueue")}>
      <Stack gap="xs">
        {waiting.map(({ name, repo, job, attempt_id, ...shortfall }) => {
          const message = resourceShortfallText(t, label(name), shortfall);
          return (
            <Text size="sm" key={name}>
              <Link to={href.job(repo, attempt_id)}>
                {repo} · {job}
              </Link>
              {message && ` — ${message}`}
            </Text>
          );
        })}
      </Stack>
    </Section>
  );
}

/** One part of the page, a card inside the page's card as the other settings pages nest theirs; what it holds (a Save button) belongs to it alone. */
function Section({
  title,
  description,
  action,
  children,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <Card p="md" withBorder component="section" aria-label={title}>
      <Header order={4} title={title} description={description} action={action} />
      {children}
    </Card>
  );
}

function Runner({
  settings,
  selected,
  onSelect,
}: {
  settings: Settings;
  selected: string;
  onSelect: (name: string) => void;
}) {
  const t = useT();
  const label = useRunnerLabel();
  const { runner } = settings;
  const setMode = useCiChange(api.admin.setRunner);
  const remove = useCiChange(api.admin.deleteRunner);
  return (
    <Section title={t("ci.settings.runner")}>
      <Stack gap="md">
        <Group gap="xs">
          <StateBadge tone={RUNNER_TONE[runner.state]}>
            {t(`ci.settings.state.${runner.state}`)}
          </StateBadge>
          <Text span size="sm">
            {runner.version_mismatch
              ? t("ci.runnerVersionMismatch", {
                  ...runner.version_mismatch,
                  name: label(runner.version_mismatch.name),
                })
              : runner.detail}
          </Text>
        </Group>
        <Switch
          label={t("ci.settings.runHere")}
          description={t("ci.settings.runHereHelp")}
          checked={runner.mode === "auto"}
          disabled={setMode.isPending}
          onChange={(e) => setMode.mutate(e.currentTarget.checked ? "auto" : "off")}
        />
        {setMode.isError && <ErrorMessage error={setMode.error} />}
        {runner.runners.length === 0 ? (
          <Text size="sm" c="dimmed">
            {t("ci.settings.noRunners")}
          </Text>
        ) : (
          <Radio.Group aria-label={t("ci.settings.runners")} value={selected} onChange={onSelect}>
            <Stack gap={6} role="list" aria-label={t("ci.settings.runners")}>
              {runner.runners.map((r) => (
                <RunnerRow
                  key={r.name}
                  runner={r}
                  deleting={remove.isPending}
                  onDelete={() => {
                    if (
                      window.confirm(
                        t("ci.settings.confirmDeleteRunner", { name: label(r.name, true) }),
                      )
                    )
                      remove.mutate(r.name);
                  }}
                />
              ))}
            </Stack>
          </Radio.Group>
        )}
        {remove.isError && <ErrorMessage error={remove.error} />}
        <Text size="sm">
          <Anchor href={t("ci.settings.forwardingUrl")} target="_blank" rel="noreferrer">
            {t("ci.settings.forwarding")}
          </Anchor>
          {" · "}
          <code>guildbotics runner join</code>
        </Text>
      </Stack>
    </Section>
  );
}

function RunnerRow({
  runner: r,
  deleting,
  onDelete,
}: {
  runner: CiRunner;
  deleting: boolean;
  onDelete: () => void;
}) {
  const t = useT();
  const label = useRunnerLabel();
  const online = r.connection === "online";
  const reportTone = (ok: boolean): Tone => (online ? (ok ? "success" : "warning") : "neutral");
  return (
    <div role="listitem" className="setting-row">
      <Stack gap={4} className="setting-row-name">
        <Radio value={r.name} label={label(r.name, true)} />
        <Group gap="xs">
          <StateBadge tone={CONNECTION_TONE[r.connection]}>
            {t(`ci.settings.connection.${r.connection}`)}
          </StateBadge>
          <Text size="xs" c="dimmed">
            {online && r.max_jobs !== null
              ? t("ci.settings.capacity", { running: r.running_jobs, max: r.max_jobs })
              : t("ci.settings.active", { running: r.running_jobs })}
          </Text>
        </Group>
        {r.last_seen_at && (
          <Stack gap={4}>
            <Text size="xs" c="dimmed">
              {t(online ? "ci.settings.lastSeen" : "ci.settings.lastReport")}{" "}
              <Time value={r.last_seen_at} />
            </Text>
            <Group gap="xs">
              {r.executor_state && (
                <StateBadge tone={reportTone(r.executor_state === "ready")}>
                  {r.executor} ·{" "}
                  {t(
                    r.executor_state === "ready"
                      ? "ci.settings.state.ready"
                      : "ci.settings.state.executor_unavailable",
                  )}
                </StateBadge>
              )}
              {r.version && (
                <StateBadge tone={reportTone(r.version_matches === true)}>
                  {t(
                    r.version_matches
                      ? "ci.settings.versionMatches"
                      : "ci.settings.versionMismatch",
                    { version: r.version },
                  )}
                </StateBadge>
              )}
            </Group>
            {!online && r.max_jobs !== null && (
              <Text size="xs" c="dimmed">
                {t("ci.settings.reportedCapacity", { max: r.max_jobs })}
              </Text>
            )}
            {r.executor_detail && (
              <Text size="xs" c="dimmed">
                {r.executor_detail}
              </Text>
            )}
            <Text size="xs" c="dimmed">
              {r.machine
                ? t("ci.settings.machine", {
                    cpus: r.machine.logical_cpus,
                    total: r.machine.total_memory_mib,
                    available: r.machine.available_memory_mib,
                  })
                : t("ci.settings.machineUnknown")}
            </Text>
            {!!r.missing_images?.length && (
              <Text size="xs">
                {t("ci.settings.runnerMissingImages", {
                  cpu: r.architecture ?? "",
                  images: r.missing_images.join(", "),
                })}
              </Text>
            )}
          </Stack>
        )}
      </Stack>
      <Button
        size="xs"
        variant="light"
        color="danger"
        aria-label={t("ci.settings.deleteRunnerLabel", { name: label(r.name, true) })}
        disabled={deleting}
        onClick={onDelete}
      >
        {t("ci.settings.deleteRunner")}
      </Button>
    </div>
  );
}

type LimitsForm = Record<keyof CiLimits, string | number>;

const LIMIT_FIELDS: { key: keyof CiLimits; label: MessageKey; min: number; max: number }[] = [
  { key: "max_cpus", label: "ci.settings.maxCpus", min: 1, max: 64 },
  { key: "max_memory_mib", label: "ci.settings.maxMemory", min: 128, max: 65536 },
  { key: "work_quota_mib", label: "ci.settings.workQuota", min: 1024, max: 1048576 },
  { key: "max_timeout_secs", label: "ci.settings.maxTimeout", min: 1, max: 86400 },
  { key: "cache_max_mib", label: "ci.settings.cacheMax", min: 0, max: 1048576 },
  { key: "max_jobs", label: "ci.settings.maxJobs", min: 1, max: 64 },
  { key: "memory_reserve_percent", label: "ci.settings.memoryReserve", min: 0, max: 100 },
  { key: "cpu_multiplier_percent", label: "ci.settings.cpuMultiplier", min: 0, max: 6400 },
];

/** Which runner a section acts on, as the list names it. */
function RunnerHeading({ name }: { name: string }) {
  const label = useRunnerLabel();
  return (
    <Text size="sm" fw={600} mb="sm">
      {label(name, true)}
    </Text>
  );
}

/** The selected runner's display name, which the checks, jobs, and this page show in place of its name. */
function RunnerName({ name }: { name: string }) {
  const t = useT();
  const members = useMembers();
  const saved = members.data?.items.find((m) => m.name === name)?.display_name;
  return (
    <Section
      title={t("ci.settings.displayName")}
      description={t("ci.settings.displayNameHelp", { name })}
    >
      <RunnerHeading name={name} />
      {members.isPending ? (
        <Loading />
      ) : members.isError ? (
        <ErrorMessage error={members.error} />
      ) : (
        // A display name saved elsewhere (another tab) replaces the form's.
        <RunnerNameForm key={saved} name={name} saved={saved ?? name} />
      )}
    </Section>
  );
}

function RunnerNameForm({ name, saved }: { name: string; saved: string }) {
  const t = useT();
  const client = useQueryClient();
  const [value, setValue] = useState(saved);
  const save = useMutation({
    mutationFn: () => api.members.update(name, { display_name: value.trim() }),
    onSuccess: (member) => setValue(member.display_name),
    onSettled: () => void client.invalidateQueries({ queryKey: keys.members() }),
  });
  return (
    <form
      aria-label={t("ci.settings.displayName")}
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate();
      }}
    >
      <Stack gap="sm">
        <Group align="flex-end" gap="sm">
          <TextInput
            label={t("ci.settings.displayName")}
            value={value}
            onChange={(e) => setValue(e.currentTarget.value)}
            style={{ flex: 1 }}
          />
          <Button type="submit" disabled={save.isPending}>
            {t("common.save")}
          </Button>
        </Group>
        {save.isError && <ErrorMessage error={save.error} />}
      </Stack>
    </form>
  );
}

function Limits({ runner }: { runner: CiRunner }) {
  const t = useT();
  const label = useRunnerLabel();
  const { limits } = runner;
  const [form, setForm] = useState<LimitsForm>(limits);
  // Saved limits that change (another tab, a restart) replace the form's.
  const [shown, setShown] = useState(limits);
  if (JSON.stringify(shown) !== JSON.stringify(limits)) {
    setShown(limits);
    setForm(limits);
  }
  const save = useCiChange((value: CiLimits) => api.admin.setRunnerLimits(runner.name, value));
  const filled = LIMIT_FIELDS.every((f) => form[f.key] !== "");
  return (
    <Section title={t("ci.settings.limits")} description={t("ci.settings.limitsHelp")}>
      <Group gap="xs" mb="sm">
        <Text size="sm" fw={600}>
          {label(runner.name, true)}
        </Text>
        <Text size="xs" c="dimmed">
          {t(`ci.settings.limitsSource.${runner.limits_source}`)}
        </Text>
      </Group>
      <form
        aria-label={t("ci.settings.limits")}
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate({
            max_cpus: Number(form.max_cpus),
            max_memory_mib: Number(form.max_memory_mib),
            work_quota_mib: Number(form.work_quota_mib),
            max_timeout_secs: Number(form.max_timeout_secs),
            cache_max_mib: Number(form.cache_max_mib),
            max_jobs: Number(form.max_jobs),
            memory_reserve_percent: Number(form.memory_reserve_percent),
            cpu_multiplier_percent: Number(form.cpu_multiplier_percent),
          });
        }}
      >
        <Stack gap="sm">
          <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="sm">
            {LIMIT_FIELDS.map((f) => (
              <NumberInput
                key={f.key}
                label={t(f.label)}
                min={f.min}
                max={f.max}
                allowDecimal={false}
                allowNegative={false}
                value={form[f.key]}
                onChange={(v) => setForm({ ...form, [f.key]: v })}
              />
            ))}
          </SimpleGrid>
          {save.isError && <ErrorMessage error={save.error} />}
          {save.isSuccess && <Notice>{t("settings.saved")}</Notice>}
          <Group>
            <Button type="submit" disabled={save.isPending || !filled}>
              {t("common.save")}
            </Button>
          </Group>
        </Stack>
      </form>
    </Section>
  );
}
