import { Alert, Button, Card, CopyButton, Group, Loader, Modal, NumberInput, Radio, SimpleGrid, Stack, Switch, Text, TextInput } from "@mantine/core";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, Copy, FolderGit2, Plus, TriangleAlert } from "lucide-react";
import { useState, type ReactNode } from "react";
import { isApiError } from "../../api/client";
import * as api from "../../api/endpoints";
import type { CachedImage, CiLimits, CiSettings as Settings, ImageOperation, MissingImage } from "../../api/types";
import { ErrorMessage, Loading, Notice, Refusal, StateBadge, Time, type Tone } from "../../components/common";
import { PanelHeader } from "../../components/PanelHeader";
import { useT, type MessageKey } from "../../i18n";
import { useCiSettings } from "../../query/hooks";
import { keys } from "../../query/keys";
import { Link } from "../../router/Router";
import { href } from "../../router/routes";

const RUNNER_TONE: Record<Settings["runner"]["state"], Tone> = { ready: "success", no_runner: "warning", executor_unavailable: "danger", stopping: "warning" };

/** An image's size as people read it (the cache holds images of hundreds of MiB to GiB). */
function size(bytes: number): string {
  const gib = bytes / 2 ** 30;
  return gib >= 1 ? `${gib.toFixed(1)} GiB` : `${Math.max(1, Math.round(bytes / 2 ** 20))} MiB`;
}

/** A digest cut in the middle: `sha256:` and its first 8 hex digits, then the last 9 characters. */
function shorten(digest: string): string {
  return digest.length > 24 ? `${digest.slice(0, 15)}…${digest.slice(-9)}` : digest;
}

/** Answers of the changes are the new settings: they replace the shown ones (and start polling a running operation). */
function useCiChange<V>(change: (value: V) => Promise<Settings>) {
  const client = useQueryClient();
  return useMutation({ mutationFn: change, onSuccess: (s) => client.setQueryData(keys.ci(), s) });
}

/** The owner's CI settings, a card each: the runner, its executor, the job limits, the images, and which repositories' jobs reach the network. */
export function CiSettings() {
  const t = useT();
  const ci = useCiSettings();
  return (
    <Card p="lg">
      <PanelHeader title={t("ci.settings.title")} description={t("ci.settings.help")} />
      {ci.isPending ? (
        <Loading />
      ) : ci.isError ? (
        <ErrorMessage error={ci.error} />
      ) : (
        <Stack gap="md">
          <Runner settings={ci.data} />
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
              <dt>{t("ci.settings.imageCache")}</dt>
              <dd>
                <code>{ci.data.executor.home}</code>
              </dd>
            </dl>
          </Section>
          <Limits limits={ci.data.limits} />
          <Images settings={ci.data} />
          <Section title={t("ci.settings.network")} description={t("ci.settings.networkHelp")}>
            {ci.data.repositories.length === 0 ? (
              <Text size="sm" c="dimmed">
                {t("settings.noRepos")}
              </Text>
            ) : (
              <Stack gap={6} role="list" aria-label={t("ci.settings.network")}>
                {ci.data.repositories.map((r) => (
                  <div key={r.path} role="listitem" className="setting-row">
                    <FolderGit2 size={18} color="var(--color-text-subtle)" />
                    <span className="setting-row-name">
                      <Link to={href.repoSettings(r.path)}>{r.path}</Link>
                    </span>
                    <StateBadge tone={r.ci_network ? "warning" : "neutral"}>{t(r.ci_network ? "ci.settings.networkAllowed" : "ci.settings.networkDenied")}</StateBadge>
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

/** One part of the page, a card inside the page's card as the other settings pages nest theirs; what it holds (a Save button) belongs to it alone. */
function Section({ title, description, action, children }: { title: string; description?: string; action?: ReactNode; children: ReactNode }) {
  return (
    <Card p="md" withBorder component="section" aria-label={title}>
      <PanelHeader order={4} title={title} description={description} action={action} />
      {children}
    </Card>
  );
}

function Runner({ settings }: { settings: Settings }) {
  const t = useT();
  const { runner } = settings;
  const setMode = useCiChange(api.admin.setRunner);
  return (
    <Section title={t("ci.settings.runner")}>
      <Stack gap="md">
        <Group gap="xs">
          <StateBadge tone={RUNNER_TONE[runner.state]}>{t(`ci.settings.state.${runner.state}`)}</StateBadge>
          <Text span size="sm">
            {runner.detail}
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
          <Stack gap={6} role="list" aria-label={t("ci.settings.runners")}>
            {runner.runners.map((r) => (
              <div key={r.name} role="listitem" className="setting-row">
                <Text span size="sm" className="setting-row-name">
                  <strong>{r.name}</strong>{" "}
                  <Text span size="xs" c="dimmed">
                    {t("ci.settings.lastSeen")} <Time value={r.last_seen_at} />
                  </Text>
                  {r.executor_detail && (
                    <Text size="xs" c="dimmed">
                      {r.executor_detail}
                    </Text>
                  )}
                </Text>
                <StateBadge tone={r.executor_state === "ready" ? "success" : "warning"}>
                  {r.executor} {r.executor_state}
                </StateBadge>
              </div>
            ))}
          </Stack>
        )}
      </Stack>
    </Section>
  );
}

type LimitsForm = Record<keyof CiLimits, string | number>;

const LIMIT_FIELDS: { key: keyof CiLimits; label: MessageKey; min: number; max: number }[] = [
  { key: "max_cpus", label: "ci.settings.maxCpus", min: 1, max: 64 },
  { key: "max_memory_mib", label: "ci.settings.maxMemory", min: 128, max: 65536 },
  { key: "max_timeout_secs", label: "ci.settings.maxTimeout", min: 1, max: 86400 },
  { key: "cache_max_mib", label: "ci.settings.cacheMax", min: 0, max: 1048576 },
  { key: "max_jobs", label: "ci.settings.maxJobs", min: 1, max: 64 },
];

function Limits({ limits }: { limits: CiLimits }) {
  const t = useT();
  const [form, setForm] = useState<LimitsForm>(limits);
  // Saved limits that change (another tab, a restart) replace the form's.
  const [shown, setShown] = useState(limits);
  if (JSON.stringify(shown) !== JSON.stringify(limits)) {
    setShown(limits);
    setForm(limits);
  }
  const save = useCiChange(api.admin.setLimits);
  const filled = LIMIT_FIELDS.every((f) => form[f.key] !== "");
  return (
    <Section title={t("ci.settings.limits")} description={t("ci.settings.limitsHelp")}>
      <form
        aria-label={t("ci.settings.limits")}
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate({
            max_cpus: Number(form.max_cpus),
            max_memory_mib: Number(form.max_memory_mib),
            max_timeout_secs: Number(form.max_timeout_secs),
            cache_max_mib: Number(form.cache_max_mib),
            max_jobs: Number(form.max_jobs),
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


function Images({ settings }: { settings: Settings }) {
  const t = useT();
  const { images, operation } = settings;
  const [adding, setAdding] = useState(false);
  // The images the latest operation brought, marked in the list.
  const added = new Set(operation?.state === "succeeded" ? operation.loaded.map((l) => l.reference) : []);
  return (
    <Section
      title={t("ci.settings.images")}
      action={
        <Button size="xs" variant="default" leftSection={<Plus size={14} />} onClick={() => setAdding(true)}>
          {t("ci.settings.addImage")}
        </Button>
      }
    >
      <Stack gap="md">
        {images.error && <Refusal>{images.error}</Refusal>}
        {operation?.state === "running" && (
          <Text size="sm" c="dimmed" role="status">
            {t("ci.settings.adding", { target: operation.target })}
          </Text>
        )}
        {images.missing.map((m) => (
          <Missing key={m.image} image={m} />
        ))}
        {images.cached.length > 0 ? (
          <Stack gap="sm" role="list" aria-label={t("ci.settings.cached")}>
            {images.cached.map((c) => (
              <Cached key={c.reference} image={c} added={added.has(c.reference)} />
            ))}
          </Stack>
        ) : (
          !images.error && (
            <Stack gap={4} align="center" ta="center" py="lg">
              <Text fw={600}>{t("ci.settings.emptyTitle")}</Text>
              <Text size="sm" c="dimmed">
                {t("ci.settings.emptyHelp")}
              </Text>
            </Stack>
          )
        )}
      </Stack>
      <AddImage operation={operation} opened={adding} onClose={() => setAdding(false)} />
    </Section>
  );
}

/** An image pipelines name that the cache does not hold. */
function Missing({ image: m }: { image: MissingImage }) {
  const t = useT();
  const users = m.repositories.map((r) => t("ci.settings.jobsOf", { jobs: r.jobs.join(", "), repository: r.repository })).join(" · ");
  return (
    <Alert color="warning" icon={<TriangleAlert size={18} />} title={t("ci.settings.missingTitle", { users })}>
      <Stack gap={4}>
        <code className="secret">{m.image}</code>
        <Text size="sm">{t("ci.settings.missingAbsent")}</Text>
      </Stack>
    </Alert>
  );
}

function Cached({ image: c, added }: { image: CachedImage; added: boolean }) {
  const t = useT();
  const users = c.used_by.map((u) => `${u.repository} (${u.jobs.join(", ")})`).join(" · ");
  return (
    <div role="listitem" className="setting-row">
      <div className="setting-row-name">
        <Group gap="xs" wrap="nowrap">
          <Text size="sm" fw={600} title={c.reference} truncate style={{ minWidth: 0 }}>
            {c.reference}
          </Text>
          {added && <StateBadge tone="success">{t("ci.settings.added")}</StateBadge>}
        </Group>
        <Text size="xs" c="dimmed">
          {size(c.size_bytes)} · <Time value={c.created_at} date />
          {users && ` · ${t("ci.settings.usedBy", { repositories: users })}`}
        </Text>
        <Text size="xs" c="dimmed" className="mono" title={c.digest} truncate>
          {shorten(c.digest)}
        </Text>
      </div>
      <CopyReference value={c.reference} />
    </div>
  );
}

/** Copies an image's tag: the value to write as `image` in `.branvil/ci.toml`. */
function CopyReference({ value }: { value: string }) {
  const t = useT();
  return (
    <CopyButton value={value} timeout={2000}>
      {({ copied, copy }) => (
        <Button size="compact-xs" variant="default" leftSection={copied ? <Check size={12} /> : <Copy size={12} />} onClick={copy}>
          {t(copied ? "common.copied" : "ci.settings.copyReference")}
        </Button>
      )}
    </CopyButton>
  );
}

type Source = "pull" | "load";

/** Where an image comes from: the one field the dialog asks for, and the operation that adds it. */
const SOURCES: Record<Source, { label: MessageKey; field: MessageKey; description?: MessageKey; placeholder: string; add: (value: string) => Promise<Settings> }> = {
  pull: { label: "ci.settings.pull", field: "ci.settings.pullReference", placeholder: "debian:bookworm", add: api.admin.pullImage },
  load: { label: "ci.settings.load", field: "ci.settings.loadPath", description: "ci.settings.loadHelp", placeholder: "/path/to/image.tar", add: api.admin.loadImage },
};

/** The "Add image" dialog: it starts a pull or a load and follows it (the page polls a running operation) to its outcome. */
function AddImage({ operation, opened, onClose }: { operation: ImageOperation | null; opened: boolean; onClose: () => void }) {
  const t = useT();
  const [source, setSource] = useState<Source>("pull");
  const [value, setValue] = useState("");
  // Whether this opening of the dialog started an operation: its outcome stays shown until the dialog closes.
  const [started, setStarted] = useState(false);
  const add = useCiChange((s: { source: Source; value: string }) => SOURCES[s.source].add(s.value));
  const running = operation?.state === "running";
  const shown = operation && (running || started) ? operation : null;
  const { field, description, placeholder } = SOURCES[source];
  function close() {
    setSource("pull");
    setValue("");
    setStarted(false);
    add.reset();
    onClose();
  }
  return (
    <Modal opened={opened} onClose={close} title={t("ci.settings.addImage")} closeButtonProps={{ "aria-label": t("common.close") }} centered>
      <form
        aria-label={t("ci.settings.addImage")}
        onSubmit={(e) => {
          e.preventDefault();
          add.mutate(
            { source, value: value.trim() },
            {
              onSuccess: () => {
                setStarted(true);
                setValue("");
              },
            },
          );
        }}
      >
        <Stack gap="md">
          <Radio.Group label={t("ci.settings.source")} value={source} onChange={(v) => setSource(v as Source)}>
            <Stack gap="xs" mt={6}>
              {(["pull", "load"] as const).map((s) => (
                <Radio key={s} value={s} label={t(SOURCES[s].label)} />
              ))}
            </Stack>
          </Radio.Group>
          <TextInput
            label={t(field)}
            description={description && t(description)}
            placeholder={placeholder}
            value={value}
            onChange={(e) => setValue(e.currentTarget.value)}
            data-autofocus
          />
          {shown && <Outcome operation={shown} />}
          {add.isError && (isApiError(add.error, "conflict") ? <Refusal>{t("ci.settings.busy")}</Refusal> : <ErrorMessage error={add.error} />)}
          <Group justify="flex-end">
            <Button type="submit" disabled={running || add.isPending || !value.trim()}>
              {t("ci.settings.addSubmit")}
            </Button>
          </Group>
        </Stack>
      </form>
    </Modal>
  );
}

/** What the operation is doing, or what it brought or why it failed. */
function Outcome({ operation: op }: { operation: ImageOperation }) {
  const t = useT();
  if (op.state === "running") {
    return (
      <Group gap="xs" role="status" wrap="nowrap">
        <Loader size="xs" />
        <Text size="sm">{t("ci.settings.adding", { target: op.target })}</Text>
      </Group>
    );
  }
  if (op.state === "failed") return op.detail ? <Refusal>{op.detail}</Refusal> : null;
  return (
    <Stack gap={6} role="list" aria-label={t("ci.settings.loaded")}>
      {op.loaded.map((l) => (
        <div key={l.reference} role="listitem" className="setting-row">
          <span className="setting-row-name">
            <Notice>{t("ci.settings.addedImage", { reference: l.reference })}</Notice>
          </span>
          <CopyReference value={l.reference} />
        </div>
      ))}
    </Stack>
  );
}
