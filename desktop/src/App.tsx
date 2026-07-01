import {
  Avatar,
  ActionIcon,
  Alert,
  Anchor,
  Autocomplete,
  Badge,
  Button,
  Card,
  CopyButton,
  Divider,
  Drawer,
  Group,
  NumberInput,
  Select,
  SegmentedControl,
  Stack,
  Switch,
  Tabs,
  Text,
  Textarea,
  TextInput,
  Title,
  Tooltip,
} from "@mantine/core";
import {
  Activity,
  CheckCircle2,
  Copy,
  ExternalLink,
  FolderOpen,
  History,
  Play,
  RotateCcw,
  Search,
  Settings,
  Square,
  Terminal,
  Ticket,
  TriangleAlert,
  XCircle,
} from "lucide-react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";

import {
  type CommandOption,
  type DiagnosticCheck,
  type MemoryEvent,
  type PromptTraceEntry,
  type RuntimeEvent,
  type RuntimeLog,
  type SchedulerStartRequest,
  type RuntimeUnitStatus,
  type TraceRecord,
  getConfigStatus,
  getCommandOptions,
  getGlobalRecords,
  getMemoryEvents,
  getProjectConfig,
  getPromptTrace,
  getRuntimeDebug,
  getSchedulerStatus,
  getTeam,
  getTraceDetail,
  getTraces,
  runCommand,
  runScenarioDiagnostics,
  startScheduler,
  stopScheduler,
  subscribeEvents,
  subscribeLogs,
  updatePromptTrace,
  updateRuntimeDebug,
  memberAvatarUrl,
} from "./api/client";
import { ActivityHistoryPage } from "./activity/ActivityHistory";
import { type AppLanguage, normalizeLanguage, setAppLanguage } from "./i18n";
import { SetupPage } from "./setup/SetupPage";
import { buildTraceGroups, type PromptTraceGroup } from "./trace";

const PROMPT_TRACE_LIMIT = 500;
const EXECUTION_LIMIT = 200;
const MEMORY_EVENT_LIMIT = 500;
const MEMORY_FILTER_ALL = "__all__";

export function App() {
  const { t, i18n } = useTranslation();
  const appLanguage = normalizeLanguage(i18n.resolvedLanguage ?? i18n.language) ?? "en";
  const config = useQuery({ queryKey: ["config"], queryFn: getConfigStatus });
  const configured = Boolean(config.data?.project_file_exists);
  return (
    <main className="shell">
      <aside className="sidebar">
        <div>
          <h1>GuildBotics</h1>
        </div>
        <nav className="nav">
          {configured ? (
            <NavLink className="nav-item" to="/activity">
              <History size={18} /> {t("app.nav.activity")}
            </NavLink>
          ) : null}
          <NavLink className="nav-item" to="/service">
            <Activity size={18} /> {t("app.nav.service")}
          </NavLink>
          <NavLink className="nav-item" to="/commands">
            <Terminal size={18} /> {t("app.nav.commands")}
          </NavLink>
          <NavLink className="nav-item" to="/diagnostics">
            <TriangleAlert size={18} /> {t("app.nav.diagnostics")}
          </NavLink>
          <NavLink className="nav-item" to="/setup">
            <Settings size={18} /> {t("app.nav.setup")}
          </NavLink>
        </nav>
        <Select
          className="sidebar-language"
          classNames={{
            input: "sidebar-language-input",
            label: "sidebar-language-label",
          }}
          label={t("app.language.label")}
          data={[
            { label: t("app.language.english"), value: "en" },
            { label: t("app.language.japanese"), value: "ja" },
          ]}
          value={appLanguage}
          onChange={(value) => {
            if (value === "en" || value === "ja") {
              void setAppLanguage(value as AppLanguage);
            }
          }}
        />
      </aside>

      <section className="workspace">
        <Routes>
          <Route
            element={<ConfiguredRoute configured={configured} loading={config.isLoading} />}
            path="/activity"
          />
          <Route element={<ServicePage />} path="/service" />
          <Route element={<Navigate replace to="/service" />} path="/overview" />
          <Route element={<CommandsPage />} path="/commands" />
          <Route element={<DiagnosticsPage />} path="/diagnostics" />
          <Route element={<SetupPage />} path="/setup" />
          <Route element={<IndexRedirect />} path="*" />
        </Routes>
      </section>
    </main>
  );
}

function ConfiguredRoute({ configured, loading }: { configured: boolean; loading: boolean }) {
  if (loading) {
    return null;
  }
  return configured ? <ActivityHistoryPage /> : <Navigate replace to="/setup" />;
}

function IndexRedirect() {
  // The landing route picks the first screen based on whether setup is done:
  // a configured workspace opens the service screen, otherwise the setup screen.
  const config = useQuery({ queryKey: ["config"], queryFn: getConfigStatus });
  if (config.isLoading) {
    return null;
  }
  const configured = Boolean(config.data?.project_file_exists);
  return <Navigate replace to={configured ? "/activity" : "/setup"} />;
}

function ServicePage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [initialPreferences] = useState(loadServicePreferences);
  const [scheduledSourceEnabled, setScheduledSourceEnabled] = useState(
    initialPreferences.scheduledSourceEnabled,
  );
  const [routineSourceEnabled, setRoutineSourceEnabled] = useState(
    initialPreferences.routineSourceEnabled,
  );
  const [eventQueueSourceEnabled, setEventQueueSourceEnabled] = useState(
    initialPreferences.eventQueueSourceEnabled,
  );
  const [maxConsecutiveErrors, setMaxConsecutiveErrors] = useState(
    initialPreferences.maxConsecutiveErrors,
  );
  const [routineIntervalMinutes, setRoutineIntervalMinutes] = useState(
    initialPreferences.routineIntervalMinutes,
  );
  const servicePreferences = useMemo<ServicePreferences>(
    () => ({
      scheduledSourceEnabled,
      routineSourceEnabled,
      eventQueueSourceEnabled,
      routineIntervalMinutes,
      maxConsecutiveErrors,
    }),
    [
      scheduledSourceEnabled,
      routineSourceEnabled,
      eventQueueSourceEnabled,
      routineIntervalMinutes,
      maxConsecutiveErrors,
    ],
  );
  // Persist preferences, debounced so rapid edits (e.g. typing in a NumberInput)
  // do not hammer localStorage on every keystroke. A ref holds the latest value
  // so it can be flushed on unmount, ensuring a change followed by an immediate
  // navigation away is never dropped.
  const servicePreferencesRef = useRef(servicePreferences);
  useEffect(() => {
    servicePreferencesRef.current = servicePreferences;
    const handle = window.setTimeout(() => saveServicePreferences(servicePreferences), 400);
    return () => window.clearTimeout(handle);
  }, [servicePreferences]);
  useEffect(() => () => saveServicePreferences(servicePreferencesRef.current), []);
  const config = useQuery({ queryKey: ["config"], queryFn: getConfigStatus });
  const team = useQuery({ queryKey: ["team"], queryFn: getTeam, retry: false });
  const scheduler = useQuery({
    queryKey: ["scheduler"],
    queryFn: getSchedulerStatus,
    refetchInterval: 5000,
  });
  const hasProjectConfig = Boolean(config.data?.project_file_exists);

  const startMutation = useMutation({
    mutationFn: () => {
      const body: SchedulerStartRequest = {
        sources: {
          scheduled: scheduledSourceEnabled,
          routine: routineSourceEnabled,
          event_queue: eventQueueSourceEnabled,
        },
        max_consecutive_errors: maxConsecutiveErrors,
        routine_interval_minutes: routineIntervalMinutes,
      };
      return startScheduler(body);
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["scheduler"] }),
  });
  const stopMutation = useMutation({
    mutationFn: stopScheduler,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["scheduler"] }),
  });
  const activeMembers = team.data?.members.filter((member) => member.is_active) ?? [];
  const runtimeRunning = Boolean(
    scheduler.data?.scheduler.running || scheduler.data?.events.running,
  );
  const runtimeStarting = Boolean(
    startMutation.isPending ||
    scheduler.data?.scheduler.state === "starting" ||
    scheduler.data?.events.state === "starting",
  );
  const runtimeStopping = Boolean(
    stopMutation.isPending ||
    scheduler.data?.scheduler.state === "stopping" ||
    scheduler.data?.events.state === "stopping",
  );
  const runtimeBusy = Boolean(runtimeStarting || runtimeStopping);
  const runtimeActive = Boolean(runtimeRunning || runtimeBusy);
  const showStopAction = Boolean(runtimeRunning || runtimeStopping);
  const noStartTarget =
    !scheduledSourceEnabled && !routineSourceEnabled && !eventQueueSourceEnabled;
  const startDisabled = !hasProjectConfig || runtimeActive || noStartTarget;
  const stopDisabled = !runtimeActive;

  return (
    <Stack gap="lg">
      <Group justify="space-between">
        <div>
          <Title order={2}>{t("service.title")}</Title>
        </div>
        {showStopAction ? (
          <Button
            leftSection={<Square size={16} />}
            loading={runtimeStopping}
            variant="default"
            disabled={stopDisabled}
            onClick={() => stopMutation.mutate()}
          >
            {t("overview.stop")}
          </Button>
        ) : (
          <Button
            leftSection={<Play size={16} />}
            loading={runtimeStarting}
            disabled={startDisabled}
            onClick={() => startMutation.mutate()}
          >
            {t("overview.start")}
          </Button>
        )}
      </Group>

      {!hasProjectConfig ? (
        <Alert color="yellow" title={t("overview.setupRequiredTitle")}>
          <Group justify="space-between" align="center">
            <Text size="sm">{t("overview.setupRequiredBody")}</Text>
            <Button component={NavLink} to="/setup" variant="light">
              {t("overview.openSetup")}
            </Button>
          </Group>
        </Alert>
      ) : null}

      <Card withBorder radius="md" p="lg">
        <Stack>
          {noStartTarget ? (
            <Alert color="yellow" title={t("service.noTargetTitle")}>
              {t("service.noTargetBody")}
            </Alert>
          ) : null}
          <div className="service-unit-grid">
            <ServiceRuntimeSection
              title={t("overview.routineSourceCard.title")}
              description={t("overview.routineSourceCard.description")}
              unit={scheduler.data?.scheduler}
              state={sourceState(
                scheduler.data?.scheduler,
                scheduler.data?.scheduler.routine_source_enabled,
              )}
              enabled={routineSourceEnabled}
              disabled={runtimeActive}
              switchLabel={t("service.sourceTarget")}
              onEnabledChange={setRoutineSourceEnabled}
              rows={[
                [
                  t("overview.routineSourceCard.status"),
                  sourceEnabledLabel(
                    t,
                    scheduler.data?.scheduler.routine_source_enabled ?? routineSourceEnabled,
                  ),
                ],
                [
                  t("overview.routineSourceCard.interval"),
                  String(
                    scheduler.data?.scheduler.routine_interval_minutes ?? routineIntervalMinutes,
                  ),
                ],
              ]}
            >
              <NumberInput
                label={t("overview.routineIntervalMinutes")}
                min={1}
                max={1440}
                step={1}
                allowDecimal={false}
                disabled={!routineSourceEnabled || runtimeActive}
                value={routineIntervalMinutes}
                onChange={(value) => {
                  if (typeof value === "number") {
                    setRoutineIntervalMinutes(value);
                  }
                }}
              />
              <Alert color="blue" title={t("overview.memberPatrolSettings")}>
                <Stack gap="xs">
                  <Text size="sm">{t("overview.memberPatrolSettingsBody")}</Text>
                  <Button
                    component={NavLink}
                    size="xs"
                    to="/setup?section=members&tab=patrol"
                    variant="light"
                  >
                    {t("overview.openMemberPatrolSettings")}
                  </Button>
                </Stack>
              </Alert>
            </ServiceRuntimeSection>
            <ServiceRuntimeSection
              title={t("overview.eventsCard.title")}
              description={t("overview.eventsCard.description")}
              unit={scheduler.data?.events}
              state={eventSourceState(scheduler.data?.scheduler, scheduler.data?.events)}
              enabled={eventQueueSourceEnabled}
              disabled={runtimeActive}
              switchLabel={t("service.sourceTarget")}
              onEnabledChange={setEventQueueSourceEnabled}
              rows={[
                [
                  t("overview.eventsCard.sourceStatus"),
                  sourceEnabledLabel(
                    t,
                    scheduler.data?.scheduler.event_queue_source_enabled ?? eventQueueSourceEnabled,
                  ),
                ],
                [
                  t("overview.eventsCard.supportedEvents"),
                  t("overview.eventsCard.supportedEventsValue"),
                ],
                [t("overview.eventsCard.workflow"), t("overview.eventsCard.workflowValue")],
                [
                  t("overview.eventsCard.listeners"),
                  String(scheduler.data?.events.listener_count ?? 0),
                ],
                [
                  t("overview.eventsCard.subscriptions"),
                  String(scheduler.data?.events.subscription_count ?? 0),
                ],
                [
                  t("overview.eventsCard.processed"),
                  t("overview.eventsCard.processedValue", {
                    drained: scheduler.data?.events.events_drained_count ?? 0,
                    failures: scheduler.data?.events.cycle_failure_count ?? 0,
                  }),
                ],
                [
                  t("overview.eventsCard.workers"),
                  t("overview.eventsCard.workerValue", {
                    workers: scheduler.data?.scheduler.event_queue_source_enabled
                      ? (scheduler.data?.scheduler.worker_count ?? 0)
                      : 0,
                  }),
                ],
              ]}
            />
            <ServiceRuntimeSection
              title={t("overview.scheduledSourceCard.title")}
              description={t("overview.scheduledSourceCard.description")}
              unit={scheduler.data?.scheduler}
              state={sourceState(
                scheduler.data?.scheduler,
                scheduler.data?.scheduler.scheduled_source_enabled,
              )}
              enabled={scheduledSourceEnabled}
              disabled={runtimeActive}
              switchLabel={t("service.sourceTarget")}
              onEnabledChange={setScheduledSourceEnabled}
              rows={[
                [
                  t("overview.scheduledSourceCard.status"),
                  sourceEnabledLabel(
                    t,
                    scheduler.data?.scheduler.scheduled_source_enabled ?? scheduledSourceEnabled,
                  ),
                ],
                [
                  t("overview.scheduledSourceCard.members"),
                  String(scheduler.data?.scheduler.active_member_count ?? activeMembers.length),
                ],
              ]}
            >
              <Alert color="blue" title={t("overview.scheduledSourceCard.settingsTitle")}>
                <Stack gap="xs">
                  <Text size="sm">{t("overview.scheduledSourceCard.settingsBody")}</Text>
                  <Button
                    component={NavLink}
                    size="xs"
                    to="/setup?section=members&tab=patrol"
                    variant="light"
                  >
                    {t("overview.openMemberPatrolSettings")}
                  </Button>
                </Stack>
              </Alert>
            </ServiceRuntimeSection>
            <ServiceRuntimeSection
              title={t("overview.workerCard.title")}
              description={t("overview.workerCard.description")}
              unit={scheduler.data?.scheduler}
              state={scheduler.data?.scheduler.state}
              rows={[
                [
                  t("overview.workerCard.workers"),
                  t("overview.workerCard.workerValue", {
                    workers: scheduler.data?.scheduler.worker_count ?? 0,
                    members: scheduler.data?.scheduler.active_member_count ?? activeMembers.length,
                  }),
                ],
                [
                  t("overview.workerCard.sources"),
                  workerSourceStatusLabel(t, scheduler.data?.scheduler, {
                    scheduled: scheduledSourceEnabled,
                    routine: routineSourceEnabled,
                    eventQueue: eventQueueSourceEnabled,
                  }),
                ],
                [t("overview.workerCard.maxConsecutiveErrors"), String(maxConsecutiveErrors)],
              ]}
            >
              <NumberInput
                label={t("overview.maxConsecutiveErrors")}
                min={1}
                max={20}
                step={1}
                allowDecimal={false}
                disabled={runtimeActive}
                value={maxConsecutiveErrors}
                onChange={(value) => {
                  if (typeof value === "number") {
                    setMaxConsecutiveErrors(value);
                  }
                }}
              />
            </ServiceRuntimeSection>
          </div>
          <PromptTraceOutputSettings />
          <RuntimeDebugSettings />
          {startMutation.error ? (
            <Alert color="red" title={t("overview.startError")}>
              {startMutation.error.message}
            </Alert>
          ) : null}
          {stopMutation.error ? (
            <Alert color="red" title={t("overview.stopError")}>
              {stopMutation.error.message}
            </Alert>
          ) : null}
        </Stack>
      </Card>
    </Stack>
  );
}

function DiagnosticsPage() {
  const { t } = useTranslation();
  const [readTracePath, setReadTracePath] = useState("");
  const [readTracePathEdited, setReadTracePathEdited] = useState(false);
  const [loadedTracePath, setLoadedTracePath] = useState("");
  const canPickFile = isTauriRuntime();
  const config = useQuery({ queryKey: ["config"], queryFn: getConfigStatus });
  const team = useQuery({ queryKey: ["team"], queryFn: getTeam, retry: false });
  const promptTrace = useQuery({
    queryKey: ["prompt-trace", loadedTracePath],
    queryFn: () => getPromptTrace(PROMPT_TRACE_LIMIT, loadedTracePath.trim() || undefined),
    refetchInterval: 5000,
  });
  const effectiveReadTracePath = readTracePathEdited
    ? readTracePath
    : (promptTrace.data?.trace_file ?? loadedTracePath);
  const hasProjectConfig = Boolean(config.data?.project_file_exists);
  const projectConfig = useQuery({
    queryKey: ["project-config"],
    queryFn: getProjectConfig,
    enabled: hasProjectConfig,
    retry: false,
  });
  const githubEnabled = projectConfig.data?.github_enabled ?? false;
  const activeMembers = team.data?.members.filter((member) => member.is_active) ?? [];
  const diagnosticsMutation = useMutation({
    mutationFn: () => runScenarioDiagnostics(),
  });
  const applyReadTracePath = (tracePath: string = effectiveReadTracePath) => {
    const normalizedPath = tracePath.trim();
    if (!readTracePathEdited && normalizedPath === loadedTracePath) {
      return;
    }
    setLoadedTracePath(normalizedPath);
    setReadTracePathEdited(false);
  };
  const resetReadTracePath = () => {
    const defaultPath = promptTrace.data?.default_trace_file ?? "";
    if (!defaultPath) {
      return;
    }
    setReadTracePath(defaultPath);
    setLoadedTracePath(defaultPath);
    setReadTracePathEdited(false);
  };
  const pickReadTracePath = async () => {
    const selected = await selectTraceFile(
      "open",
      effectiveReadTracePath || promptTrace.data?.default_trace_file || "",
    );
    if (!selected) {
      return;
    }
    setReadTracePath(selected);
    setLoadedTracePath(selected);
    setReadTracePathEdited(false);
  };
  return (
    <Stack className="diagnostics-page" gap="lg">
      <Group justify="space-between">
        <div>
          <Title order={2}>{t("diagnostics.title")}</Title>
        </div>
      </Group>
      <Tabs className="diagnostics-tabs" defaultValue="readiness">
        <Tabs.List>
          <Tabs.Tab value="readiness">{t("diagnostics.tabs.readiness")}</Tabs.Tab>
          <Tabs.Tab value="executions">{t("diagnostics.tabs.executions")}</Tabs.Tab>
          <Tabs.Tab value="memory">{t("diagnostics.tabs.memory")}</Tabs.Tab>
          <Tabs.Tab value="promptTrace">{t("diagnostics.tabs.promptTrace")}</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="readiness" pt="md">
          <Card withBorder radius="md" p="lg">
            <Group justify="space-between">
              <Title order={3}>{t("overview.configuration")}</Title>
              <Button
                loading={diagnosticsMutation.isPending}
                variant="default"
                onClick={() => diagnosticsMutation.mutate()}
              >
                {t("overview.scenarioDiagnostics.run")}
              </Button>
            </Group>
            <dl className="status-list">
              <dt>{t("overview.config")}</dt>
              <dd>
                <Badge color={hasProjectConfig ? "teal" : "orange"} variant="light">
                  {hasProjectConfig ? t("overview.ready") : t("overview.missing")}
                </Badge>
              </dd>
              <dt>{t("overview.env")}</dt>
              <dd>
                <Badge color={config.data?.env_file_exists ? "teal" : "gray"} variant="light">
                  {config.data?.env_file_exists ? t("overview.found") : t("overview.notFound")}
                </Badge>
              </dd>
              <dt>{t("overview.activeMembers")}</dt>
              <dd>{activeMembers.length}</dd>
              <dt>{t("overview.github")}</dt>
              <dd>
                <Badge color={githubEnabled ? "teal" : "gray"} variant="light">
                  {githubEnabled ? t("overview.enabled") : t("overview.disabled")}
                </Badge>
              </dd>
            </dl>
            <ScenarioDiagnosticsSummary
              checks={diagnosticsMutation.data?.checks ?? []}
              error={diagnosticsMutation.error}
              loading={diagnosticsMutation.isPending}
            />
          </Card>
        </Tabs.Panel>
        <Tabs.Panel className="diagnostics-fill-panel" value="promptTrace" pt="md">
          <Card className="diagnostics-fill-card trace-fill-card" withBorder radius="md" p="lg">
            <Group justify="space-between" align="flex-start">
              <div>
                <Title order={3}>{t("overview.promptTrace.title")}</Title>
                <Text c="dimmed" size="sm">
                  {t("overview.promptTrace.description")}
                </Text>
              </div>
            </Group>
            <div className="trace-settings">
              <TracePathField
                label={t("overview.promptTrace.readPath")}
                value={effectiveReadTracePath}
                edited={readTracePathEdited}
                applying={promptTrace.isFetching}
                canPickFile={canPickFile}
                onChange={(value) => {
                  setReadTracePath(value);
                  setReadTracePathEdited(true);
                }}
                onApply={applyReadTracePath}
                onPick={pickReadTracePath}
                onResetDefault={resetReadTracePath}
                defaultDisabled={!promptTrace.data?.default_trace_file}
                pickLabel={t("overview.promptTrace.chooseReadPath")}
              />
            </div>
            <div className="diagnostics-count">
              <Text size="sm">
                <b>{t("overview.promptTrace.displayedCount")}</b>{" "}
                {t("overview.promptTrace.displayedCountValue", {
                  count: promptTrace.data?.events.length ?? 0,
                  limit: PROMPT_TRACE_LIMIT,
                })}
              </Text>
            </div>
            <PromptTraceList entries={promptTrace.data?.events ?? []} />
          </Card>
        </Tabs.Panel>
        <Tabs.Panel className="diagnostics-fill-panel" value="executions" pt="md">
          <TraceExplorer />
        </Tabs.Panel>
        <Tabs.Panel className="diagnostics-fill-panel" value="memory" pt="md">
          <MemoryEventsPanel members={team.data?.members ?? []} />
        </Tabs.Panel>
      </Tabs>
    </Stack>
  );
}

const TRACE_SOURCES = ["all", "manual", "routine", "scheduled", "event_listener"] as const;

const RECORD_FILTERS = ["all", "error", "llm", "cli_agent", "event", "log", "memory"] as const;

export type RecordScopeFilter = {
  kind: "span" | "call" | "subtree";
  value: string;
  label: string;
};

// Sentinel selection for the pinned "Global / system" view, which shows
// unscoped records (service lifecycle events + global logs) that do not belong
// to any trace.
const GLOBAL_TRACE_ID = "__global__";

type MemoryEventFilters = {
  personId: string;
  action: string;
  query: string;
};

function MemoryEventsPanel({
  members,
}: {
  members: Array<{ person_id: string; name: string; person_type?: string }>;
}) {
  const { t } = useTranslation();
  const [selectedId, setSelectedId] = useState("");
  const [searchInput, setSearchInput] = useState("");
  const [filters, setFilters] = useState<MemoryEventFilters>({
    personId: MEMORY_FILTER_ALL,
    action: MEMORY_FILTER_ALL,
    query: "",
  });
  const memoryEvents = useQuery({
    queryKey: ["diagnostics-memory-events", filters],
    queryFn: () =>
      getMemoryEvents({
        personId: filters.personId === MEMORY_FILTER_ALL ? undefined : filters.personId,
        action: filters.action === MEMORY_FILTER_ALL ? undefined : filters.action,
        query: filters.query.trim() || undefined,
        limit: MEMORY_EVENT_LIMIT,
      }),
    refetchInterval: 5000,
  });
  const events = sortMemoryEventsDescending(memoryEvents.data?.events ?? []);
  const selectableMembers = members.filter((member) => member.person_type !== "human");
  const selectedEvent =
    events.find((event) => memoryEventKey(event) === selectedId) ?? events[0] ?? null;
  const selectedKey = selectedEvent ? memoryEventKey(selectedEvent) : "";
  const updateFilters = (next: MemoryEventFilters) => {
    setFilters(next);
    setSelectedId("");
  };
  const applySearch = () => {
    updateFilters({ ...filters, query: searchInput });
  };
  const clearSearch = () => {
    setSearchInput("");
    updateFilters({ ...filters, query: "" });
  };
  const changePerson = (value: string | null) => {
    updateFilters({ ...filters, personId: value ?? MEMORY_FILTER_ALL });
  };
  const changeAction = (value: string | null) => {
    updateFilters({ ...filters, action: value ?? MEMORY_FILTER_ALL });
  };
  const submitOnEnter = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter") {
      applySearch();
    }
  };
  const hasSearch = Boolean(searchInput || filters.query);
  return (
    <Card className="diagnostics-fill-card memory-fill-card" withBorder radius="md" p="lg">
      <div className="memory-header">
        <div>
          <Title order={3}>{t("diagnostics.memory.title")}</Title>
          <Text c="dimmed" size="sm">
            {t("diagnostics.memory.description")}
          </Text>
        </div>
      </div>
      <div className="memory-toolbar">
        <Select
          aria-label={t("diagnostics.memory.person")}
          data={[
            { value: MEMORY_FILTER_ALL, label: t("diagnostics.memory.allPeople") },
            ...selectableMembers.map((member) => ({
              value: member.person_id,
              label: `${member.name} (${member.person_id})`,
            })),
          ]}
          value={filters.personId}
          onChange={changePerson}
        />
        <Select
          aria-label={t("diagnostics.memory.action")}
          data={[
            { value: MEMORY_FILTER_ALL, label: t("diagnostics.memory.allActions") },
            ...["record", "recall", "get", "touch", "update", "archive", "promote"].map(
              (action) => ({
                value: action,
                label: memoryActionLabel(t, action),
              }),
            ),
          ]}
          value={filters.action}
          onChange={changeAction}
        />
        <TextInput
          className="exec-search"
          aria-label={t("diagnostics.memory.search")}
          placeholder={t("diagnostics.memory.searchPlaceholder")}
          leftSection={<Search size={15} />}
          rightSection={
            hasSearch ? (
              <ActionIcon
                size="sm"
                variant="transparent"
                color="gray"
                aria-label={t("diagnostics.memory.searchClear")}
                onClick={clearSearch}
              >
                <XCircle size={16} />
              </ActionIcon>
            ) : null
          }
          rightSectionPointerEvents="auto"
          value={searchInput}
          onChange={(event) => setSearchInput(event.currentTarget.value)}
          onKeyDown={submitOnEnter}
        />
      </div>
      <div className="diagnostics-count">
        <Text size="sm">
          <b>{t("diagnostics.memory.displayed")}</b>{" "}
          {t("diagnostics.memory.displayedValue", {
            count: events.length,
            limit: MEMORY_EVENT_LIMIT,
          })}
        </Text>
      </div>
      <div className="memory-grid">
        <div className="memory-list">
          {memoryEvents.error ? (
            <Alert color="red" title={t("diagnostics.memory.loadError")}>
              {memoryEvents.error.message}
            </Alert>
          ) : events.length === 0 ? (
            <div className="empty-row">{t("diagnostics.memory.empty")}</div>
          ) : (
            events.map((event) => (
              <button
                type="button"
                className={
                  memoryEventKey(event) === selectedKey
                    ? "memory-row memory-row-active"
                    : "memory-row"
                }
                key={memoryEventKey(event)}
                onClick={() => setSelectedId(memoryEventKey(event))}
              >
                <div className="memory-row-top">
                  <Badge color={memoryActionColor(event.action)} variant="light">
                    {memoryActionLabel(t, event.action)}
                  </Badge>
                  <Badge variant="outline">{event.scope || "memory"}</Badge>
                  <span className="memory-row-time">{formatDateTime(event.timestamp)}</span>
                </div>
                <Text className="memory-row-title" fw={600} size="sm" lineClamp={1}>
                  {event.title || event.doc_id || memoryActionLabel(t, event.action)}
                </Text>
                <div className="memory-row-meta">
                  <Group gap={4} align="center" style={{ display: "inline-flex" }}>
                    <Avatar
                      src={event.person_id ? memberAvatarUrl(event.person_id) : undefined}
                      size={16}
                      radius="xl"
                    >
                      {event.person_id ? event.person_id.substring(0, 2).toUpperCase() : "—"}
                    </Avatar>
                    <span>{event.person_id || "—"}</span>
                  </Group>
                  <span>
                    {event.doc_id ||
                      (event.result_count !== null
                        ? t("diagnostics.memory.searchHits", { count: event.result_count })
                        : "—")}
                  </span>
                </div>
                {event.summary ? (
                  <Text c="dimmed" size="xs" lineClamp={2}>
                    {event.summary}
                  </Text>
                ) : null}
              </button>
            ))
          )}
        </div>
        <div className="memory-detail">
          {selectedEvent ? <MemoryEventDetail event={selectedEvent} /> : null}
        </div>
      </div>
    </Card>
  );
}

function MemoryEventDetail({ event }: { event: MemoryEvent }) {
  const { t } = useTranslation();
  const sourceText = memorySourceSummary(event.source);
  const rows = (
    [
      [t("diagnostics.memory.fields.person"), event.person_id],
      [t("diagnostics.memory.fields.docId"), event.doc_id],
      [t("diagnostics.memory.fields.scope"), event.scope],
      [t("diagnostics.memory.fields.kind"), event.kind],
      [t("diagnostics.memory.fields.path"), event.path],
      [t("diagnostics.memory.fields.trace"), event.trace_id ?? ""],
      [t("diagnostics.memory.fields.run"), event.run_id],
      [t("diagnostics.memory.fields.taskRun"), event.task_run_id],
      [t("diagnostics.memory.fields.changed"), event.changed_fields.join(", ")],
      [t("diagnostics.memory.fields.queryKeywords"), event.query_keywords.join(", ")],
      [
        t("diagnostics.memory.fields.resultCount"),
        event.result_count === null ? "" : String(event.result_count),
      ],
      [
        t("diagnostics.memory.fields.duration"),
        event.duration_ms === null ? "" : memoryDuration(event.duration_ms),
      ],
      [t("diagnostics.memory.fields.source"), sourceText],
    ] as [string, string][]
  ).filter(([, value]) => value);
  return (
    <Stack gap="sm">
      <Group justify="space-between" align="flex-start" style={{ width: "100%" }}>
        <div
          className="memory-detail-head"
          style={{ borderBottom: "none", flex: 1, minWidth: 0, paddingBottom: 0 }}
        >
          <Group gap="xs">
            <Badge color={memoryActionColor(event.action)} variant="light">
              {memoryActionLabel(t, event.action)}
            </Badge>
            <Text c="dimmed" size="xs">
              {formatDateTime(event.timestamp) || "—"}
            </Text>
          </Group>
          <Title order={4}>
            {event.title || event.doc_id || memoryActionLabel(t, event.action)}
          </Title>
          {event.summary ? (
            <Text c="dimmed" size="sm">
              {event.summary}
            </Text>
          ) : null}
        </div>
        {event.person_id && (
          <Avatar
            src={memberAvatarUrl(event.person_id)}
            size="lg"
            radius="md"
            style={{ marginLeft: "16px", flexShrink: 0 }}
          >
            {event.person_id.substring(0, 2).toUpperCase()}
          </Avatar>
        )}
      </Group>
      <Divider />
      {event.body_preview ? (
        <div className="memory-preview">
          <Text fw={600} size="sm">
            {t("diagnostics.memory.bodyPreview")}
          </Text>
          <pre>{event.body_preview}</pre>
        </div>
      ) : null}
      {rows.length > 0 ? (
        <dl className="exec-record-meta memory-meta">
          {rows.map(([label, value]) => (
            <FragmentRow key={label} label={label} value={value} />
          ))}
        </dl>
      ) : null}
      {event.source.length > 0 ? (
        <details className="exec-record-raw">
          <summary>{t("diagnostics.memory.rawSource")}</summary>
          <pre className="command-output">{JSON.stringify(event.source, null, 2)}</pre>
        </details>
      ) : null}
    </Stack>
  );
}

function TraceExplorer() {
  const { t } = useTranslation();
  const [source, setSource] = useState("all");
  const [searchInput, setSearchInput] = useState("");
  const [query, setQuery] = useState("");
  // Default to the pinned Global / system view so unscoped records (service
  // lifecycle events and global logs such as Slack listener auth failures) are
  // visible the moment the executions tab opens, without the user having to know
  // to click the Global entry.
  const [selectedTraceId, setSelectedTraceId] = useState<string | null>(GLOBAL_TRACE_ID);
  const [recordFilter, setRecordFilter] = useState("all");
  const [recordScopeFilter, setRecordScopeFilter] = useState<RecordScopeFilter | null>(null);
  const [drawerRecord, setDrawerRecord] = useState<TraceRecord | null>(null);
  const [attrFilter, setAttrFilter] = useState<AttrFilter | null>(null);
  const isGlobal = selectedTraceId === GLOBAL_TRACE_ID;

  const traces = useQuery({
    queryKey: ["diagnostics-traces", source, query, attrFilter?.key, attrFilter?.value],
    queryFn: () =>
      getTraces({
        source: source === "all" ? undefined : source,
        query: query.trim() || undefined,
        attrKey: attrFilter?.key,
        attrValue: attrFilter?.value,
        limit: EXECUTION_LIMIT,
      }),
    refetchInterval: 5000,
  });
  const detail = useQuery({
    queryKey: ["diagnostics-trace", selectedTraceId],
    queryFn: () => (isGlobal ? getGlobalRecords() : getTraceDetail(selectedTraceId as string)),
    enabled: Boolean(selectedTraceId),
    refetchInterval: selectedTraceId ? 5000 : false,
  });
  const traceItems = useMemo(() => traces.data?.traces ?? [], [traces.data]);
  const selectedSummary = useMemo(
    () =>
      traceItems.find((trace) => trace.trace_id === selectedTraceId) ??
      detail.data?.summary ??
      null,
    [traceItems, selectedTraceId, detail.data],
  );
  // The API returns records oldest-first; show them newest-first so a live
  // (polling) trace surfaces new records at the top without scrolling, matching
  // the descending order used by the rest of the diagnostics UI.
  const records = (detail.data?.records ?? [])
    .filter((record) => matchesRecordFilter(record, recordFilter))
    .filter((record) => matchesRecordScopeFilter(record, recordScopeFilter))
    .reverse();

  // Selecting a different execution resets its per-trace UI state (record
  // filter) at the event source rather than in an effect.
  const selectTrace = (traceId: string) => {
    setSelectedTraceId(traceId);
    setRecordFilter("all");
    setRecordScopeFilter(null);
  };

  // The pinned Global view only belongs under "all"; narrowing to a specific
  // source filters traces, so drop a Global selection when leaving "all".
  const changeSource = (value: string) => {
    setSource(value);
    if (value !== "all" && selectedTraceId === GLOBAL_TRACE_ID) {
      setSelectedTraceId(null);
    }
  };

  const applySearch = () => {
    const parsed = parseTraceSearch(searchInput);
    setQuery(parsed.query);
    setAttrFilter(parsed.attrFilter);
  };
  const clearSearch = () => {
    setSearchInput("");
    setQuery("");
    setAttrFilter(null);
  };
  const applyAttrFilter = (filter: AttrFilter) => {
    setAttrFilter(filter);
  };
  const clearAttrFilter = () => {
    setAttrFilter(null);
  };
  const showGlobalEntry = source === "all" && !attrFilter;

  return (
    <Card className="diagnostics-fill-card executions-fill-card" withBorder radius="md" p="lg">
      <div className="exec-header">
        <Title order={3}>{t("diagnostics.executions.title")}</Title>
        <Text c="dimmed" size="sm">
          {t("diagnostics.executions.description")}
        </Text>
      </div>
      <div className="exec-toolbar">
        <div className="exec-source-filter">
          <SegmentedControl
            className="exec-source-segmented"
            classNames={{ label: "exec-source-segmented-label" }}
            value={source}
            onChange={changeSource}
            data={TRACE_SOURCES.map((value) => ({
              value,
              label: traceSourceLabel(t, value),
            }))}
          />
        </div>
        <TextInput
          className="exec-search"
          aria-label={t("diagnostics.executions.search")}
          placeholder={t("diagnostics.executions.searchPlaceholder")}
          leftSection={<Search size={15} />}
          rightSection={
            searchInput || query || attrFilter ? (
              <ActionIcon
                size="sm"
                variant="transparent"
                color="gray"
                aria-label={t("diagnostics.executions.searchClear")}
                onClick={clearSearch}
              >
                <XCircle size={16} />
              </ActionIcon>
            ) : null
          }
          rightSectionPointerEvents="auto"
          value={searchInput}
          onChange={(event) => setSearchInput(event.currentTarget.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              applySearch();
            }
          }}
        />
        {attrFilter ? (
          <Badge
            className="exec-filter-pill"
            size="lg"
            variant="light"
            color="grape"
            leftSection={<Ticket size={12} />}
            rightSection={
              <ActionIcon
                size="xs"
                variant="transparent"
                color="grape"
                aria-label={t("diagnostics.executions.ticket.clear")}
                onClick={clearAttrFilter}
              >
                <XCircle size={14} />
              </ActionIcon>
            }
          >
            {attrFilter.label}
          </Badge>
        ) : null}
      </div>
      <div className="diagnostics-count">
        <Text size="sm">
          <b>{t("diagnostics.executions.displayed")}</b>{" "}
          {t("diagnostics.executions.displayedValue", {
            count: traceItems.length,
            limit: EXECUTION_LIMIT,
          })}
        </Text>
      </div>
      <div className="exec-grid">
        <div className="exec-list">
          {showGlobalEntry ? (
            <button
              type="button"
              className={
                isGlobal ? "exec-row exec-row-global exec-row-active" : "exec-row exec-row-global"
              }
              onClick={() => selectTrace(GLOBAL_TRACE_ID)}
            >
              <div className="exec-row-top">
                <Badge size="sm" color="gray" variant="light">
                  {t("diagnostics.executions.global.badge")}
                </Badge>
              </div>
              <Text className="exec-row-command" fw={600} size="sm">
                {t("diagnostics.executions.global.title")}
              </Text>
              <div className="exec-row-meta">
                <span>{t("diagnostics.executions.global.subtitle")}</span>
              </div>
            </button>
          ) : null}
          {traceItems.length === 0 ? (
            <div className="empty-row">{t("diagnostics.executions.empty")}</div>
          ) : (
            traceItems.map((trace) => (
              <button
                type="button"
                key={trace.trace_id}
                className={
                  trace.trace_id === selectedTraceId ? "exec-row exec-row-active" : "exec-row"
                }
                onClick={() => selectTrace(trace.trace_id)}
              >
                <div className="exec-row-top">
                  <Badge size="sm" color={traceStatusColor(trace.status)} variant="light">
                    {t(`diagnostics.executions.status.${trace.status}`, {
                      defaultValue: trace.status,
                    })}
                  </Badge>
                  <Badge size="sm" variant="outline">
                    {traceSourceLabel(t, trace.source || "unknown")}
                  </Badge>
                  {ticketChipInfo(trace.attributes) ? (
                    <Badge size="sm" color="grape" variant="light">
                      {ticketChipInfo(trace.attributes)?.label}
                    </Badge>
                  ) : null}
                  <span className="exec-row-time">{formatTime(trace.updated_at)}</span>
                </div>
                <Tooltip
                  label={trace.command || trace.trace_id}
                  openDelay={400}
                  withArrow
                  multiline
                >
                  <Text className="exec-row-command" fw={600} size="sm" lineClamp={1}>
                    {trace.command || trace.trace_id}
                  </Text>
                </Tooltip>
                <div className="exec-row-meta">
                  <Group gap={4} align="center" style={{ display: "inline-flex" }}>
                    <Avatar
                      src={trace.person_id ? memberAvatarUrl(trace.person_id) : undefined}
                      size={16}
                      radius="xl"
                    >
                      {trace.person_id ? trace.person_id.substring(0, 2).toUpperCase() : "—"}
                    </Avatar>
                    <span className="exec-row-person">{trace.person_id || "—"}</span>
                  </Group>
                  <span className="exec-row-counts">
                    {t("diagnostics.executions.counts", {
                      events: trace.event_count,
                      logs: trace.log_count,
                    })}
                    {trace.error_count > 0 ? (
                      <span className="exec-row-errors">
                        {" · "}
                        {t("diagnostics.executions.errorChip", { count: trace.error_count })}
                      </span>
                    ) : null}
                  </span>
                </div>
              </button>
            ))
          )}
        </div>
        <div className="exec-detail">
          {isGlobal ? (
            <>
              <div className="exec-summary">
                <Text fw={700} size="sm">
                  {t("diagnostics.executions.global.title")}
                </Text>
                <Text c="dimmed" size="xs">
                  {t("diagnostics.executions.global.description")}
                </Text>
              </div>
              <ExecTimeline
                records={records}
                filter={recordFilter}
                scopeFilter={recordScopeFilter}
                onFilter={setRecordFilter}
                onClearScopeFilter={() => setRecordScopeFilter(null)}
                onSelect={setDrawerRecord}
              />
            </>
          ) : selectedTraceId && selectedSummary ? (
            <>
              <div
                className="exec-summary"
                style={{
                  display: "flex",
                  flexDirection: "row",
                  justifyContent: "space-between",
                  alignItems: "flex-start",
                }}
              >
                <div
                  style={{
                    flex: 1,
                    minWidth: 0,
                    display: "flex",
                    flexDirection: "column",
                    gap: "6px",
                  }}
                >
                  <div className="exec-summary-head">
                    <Group gap="xs">
                      <Badge color={traceStatusColor(selectedSummary.status)} variant="light">
                        {t(`diagnostics.executions.status.${selectedSummary.status}`, {
                          defaultValue: selectedSummary.status,
                        })}
                      </Badge>
                      <Badge variant="outline">
                        {traceSourceLabel(t, selectedSummary.source || "unknown")}
                      </Badge>
                      {(() => {
                        const chip = ticketChipInfo(selectedSummary.attributes);
                        if (!chip) {
                          return null;
                        }
                        return (
                          <Tooltip label={t("diagnostics.executions.ticket.filterTo")} withArrow>
                            <Badge
                              color="grape"
                              variant="light"
                              style={{ cursor: "pointer" }}
                              onClick={() =>
                                applyAttrFilter({
                                  key: chip.key,
                                  value: chip.value,
                                  label: chip.label,
                                })
                              }
                              rightSection={
                                chip.url ? (
                                  <ActionIcon
                                    size="xs"
                                    variant="transparent"
                                    color="grape"
                                    aria-label={t("diagnostics.executions.ticket.open")}
                                    onClick={(event) => {
                                      event.stopPropagation();
                                      void openExternal(chip.url);
                                    }}
                                  >
                                    <ExternalLink size={12} />
                                  </ActionIcon>
                                ) : null
                              }
                            >
                              {chip.label}
                            </Badge>
                          </Tooltip>
                        );
                      })()}
                    </Group>
                  </div>
                  <Tooltip
                    label={selectedSummary.command || selectedSummary.trace_id}
                    openDelay={400}
                    withArrow
                    multiline
                  >
                    <Text fw={700} size="sm" lineClamp={1}>
                      {selectedSummary.command || selectedSummary.trace_id}
                    </Text>
                  </Tooltip>
                  <div className="exec-summary-meta">
                    <span className="exec-summary-id">
                      {t("diagnostics.executions.meta.trace")}:{" "}
                      <Tooltip label={selectedSummary.trace_id} withArrow>
                        <code>{shortTraceId(selectedSummary.trace_id)}</code>
                      </Tooltip>
                      <CopyButton value={selectedSummary.trace_id} timeout={1500}>
                        {({ copied, copy }) => (
                          <Tooltip
                            label={
                              copied
                                ? t("diagnostics.executions.copied")
                                : t("diagnostics.executions.copy")
                            }
                            withArrow
                          >
                            <ActionIcon
                              variant="subtle"
                              size="sm"
                              color={copied ? "teal" : "gray"}
                              onClick={copy}
                            >
                              {copied ? <CheckCircle2 size={14} /> : <Copy size={14} />}
                            </ActionIcon>
                          </Tooltip>
                        )}
                      </CopyButton>
                    </span>
                    {selectedSummary.person_id ? (
                      <span>
                        {t("diagnostics.executions.meta.member")}: {selectedSummary.person_id}
                      </span>
                    ) : null}
                    <span>
                      {t("diagnostics.executions.meta.started")}:{" "}
                      {formatDateTime(selectedSummary.started_at) || "—"}
                    </span>
                    <span>
                      {t("diagnostics.executions.meta.duration")}: {traceDuration(selectedSummary)}
                    </span>
                    <span>
                      {t("diagnostics.executions.counts", {
                        events: selectedSummary.event_count,
                        logs: selectedSummary.log_count,
                      })}
                    </span>
                    {selectedSummary.error_count > 0 ? (
                      <span className="exec-row-errors">
                        {t("diagnostics.executions.errorChip", {
                          count: selectedSummary.error_count,
                        })}
                      </span>
                    ) : null}
                  </div>
                </div>
                {selectedSummary.person_id && (
                  <Avatar
                    src={memberAvatarUrl(selectedSummary.person_id)}
                    size="lg"
                    radius="md"
                    style={{ marginLeft: "16px", flexShrink: 0 }}
                  >
                    {selectedSummary.person_id.substring(0, 2).toUpperCase()}
                  </Avatar>
                )}
              </div>
              <ExecTimeline
                records={records}
                filter={recordFilter}
                scopeFilter={recordScopeFilter}
                onFilter={setRecordFilter}
                onClearScopeFilter={() => setRecordScopeFilter(null)}
                onSelect={setDrawerRecord}
              />
            </>
          ) : (
            <div className="empty-row">{t("diagnostics.executions.selectHint")}</div>
          )}
        </div>
      </div>
      <Drawer
        opened={Boolean(drawerRecord)}
        onClose={() => setDrawerRecord(null)}
        position="right"
        size="lg"
        title={drawerRecord ? recordBadgeLabel(t, drawerRecord) : ""}
      >
        {drawerRecord ? (
          <TraceRecordDetail
            record={drawerRecord}
            onScopeFilter={(filter) => {
              setRecordScopeFilter(filter);
              setDrawerRecord(null);
            }}
          />
        ) : null}
      </Drawer>
    </Card>
  );
}

function ExecTimeline({
  records,
  filter,
  scopeFilter,
  onFilter,
  onClearScopeFilter,
  onSelect,
}: {
  records: TraceRecord[];
  filter: string;
  scopeFilter: RecordScopeFilter | null;
  onFilter: (value: string) => void;
  onClearScopeFilter: () => void;
  onSelect: (record: TraceRecord) => void;
}) {
  const { t } = useTranslation();
  return (
    <>
      <div className="exec-timeline-toolbar">
        <SegmentedControl
          className="exec-filter"
          size="xs"
          value={filter}
          onChange={onFilter}
          data={RECORD_FILTERS.map((value) => ({
            value,
            label: t(`diagnostics.executions.recordFilters.${value}`),
          }))}
        />
        {scopeFilter ? (
          <Badge
            className="exec-filter-pill exec-record-scope-pill"
            size="lg"
            variant="light"
            color="blue"
            rightSection={
              <ActionIcon
                size="xs"
                variant="transparent"
                color="blue"
                aria-label={t("diagnostics.executions.recordScope.clear")}
                onClick={onClearScopeFilter}
              >
                <XCircle size={14} />
              </ActionIcon>
            }
          >
            {scopeFilter.label}
          </Badge>
        ) : null}
      </div>
      <div className="exec-timeline">
        {records.length === 0 ? (
          <div className="empty-row">{t("diagnostics.executions.noRecords")}</div>
        ) : (
          records.map((record, index) => (
            <button
              type="button"
              className="exec-timeline-row"
              key={`${record.timestamp}-${record.kind}-${index}`}
              onClick={() => onSelect(record)}
            >
              <span className="exec-timeline-time">{formatTime(record.timestamp)}</span>
              <Badge color={recordBadgeColor(record)} variant="light">
                {recordBadgeLabel(t, record)}
              </Badge>
              <span className="exec-timeline-message">{recordDisplayMessage(record)}</span>
              <span className="exec-timeline-chevron" aria-hidden>
                ›
              </span>
            </button>
          ))
        )}
      </div>
    </>
  );
}

function TraceRecordDetail({
  record,
  onScopeFilter,
}: {
  record: TraceRecord;
  onScopeFilter: (filter: RecordScopeFilter) => void;
}) {
  const { t } = useTranslation();
  const message = recordDisplayMessage(record);
  const payload = record.payload ?? {};
  const metaEntries = (
    [
      [t("diagnostics.executions.developer.traceId"), record.trace_id ?? ""],
      [t("diagnostics.executions.developer.spanId"), record.span_id ?? ""],
      [t("diagnostics.executions.developer.parentId"), record.parent_id ?? ""],
      [t("diagnostics.executions.developer.callId"), record.call_id ?? ""],
      [
        t("diagnostics.executions.developer.source"),
        traceSourceLabel(t, record.source || "unknown"),
      ],
      [t("diagnostics.executions.developer.member"), record.person_id],
      [t("diagnostics.executions.developer.command"), record.command],
      [t("diagnostics.executions.developer.workflow"), record.workflow],
    ] as [string, string][]
  ).filter(([, value]) => value);
  const hasAttributes = Object.keys(record.attributes ?? {}).length > 0;
  const attributeRows = recordAttributeRows(t, record.attributes ?? {});
  const hasPayload = Object.keys(payload).length > 0;
  return (
    <Stack gap="sm">
      <div className="exec-record-head">
        <div className="exec-record-head-top">
          <Group gap="xs">
            <Badge color={recordBadgeColor(record)} variant="light">
              {recordBadgeLabel(t, record)}
            </Badge>
            {record.source ? (
              <Badge variant="outline">{traceSourceLabel(t, record.source || "unknown")}</Badge>
            ) : null}
            <Text c="dimmed" size="xs">
              {formatDateTime(record.timestamp) || "—"}
            </Text>
          </Group>
          <CopyButton value={message} timeout={1500}>
            {({ copied, copy }) => (
              <Button
                size="xs"
                variant="subtle"
                leftSection={copied ? <CheckCircle2 size={14} /> : <Copy size={14} />}
                onClick={copy}
              >
                {copied
                  ? t("diagnostics.executions.messageCopied")
                  : t("diagnostics.executions.copyMessage")}
              </Button>
            )}
          </CopyButton>
        </div>
        <Text className="exec-record-message" size="sm">
          {message || "—"}
        </Text>
      </div>
      <Group gap="xs">
        {record.span_id ? (
          <Button
            radius="xl"
            size="xs"
            variant="light"
            onClick={() =>
              onScopeFilter({
                kind: "span",
                value: record.span_id as string,
                label: t("diagnostics.executions.recordScope.span"),
              })
            }
          >
            {t("diagnostics.executions.recordScope.span")}
          </Button>
        ) : null}
        {record.call_id ? (
          <Button
            radius="xl"
            size="xs"
            variant="light"
            onClick={() =>
              onScopeFilter({
                kind: "call",
                value: record.call_id as string,
                label: t("diagnostics.executions.recordScope.call"),
              })
            }
          >
            {t("diagnostics.executions.recordScope.call")}
          </Button>
        ) : null}
        {record.span_id ? (
          <Button
            radius="xl"
            size="xs"
            variant="light"
            onClick={() =>
              onScopeFilter({
                kind: "subtree",
                value: record.span_id as string,
                label: t("diagnostics.executions.recordScope.subtree"),
              })
            }
          >
            {t("diagnostics.executions.recordScope.subtree")}
          </Button>
        ) : null}
      </Group>
      {hasAttributes ? (
        <div className="exec-record-section">
          <Text fw={600} size="sm">
            {t("diagnostics.executions.attributes")}
          </Text>
          {attributeRows.length > 0 ? (
            <dl className="exec-record-meta exec-record-attribute-list">
              {attributeRows.map(([label, value]) => (
                <FragmentRow key={label} label={label} value={value} />
              ))}
            </dl>
          ) : null}
          <details className="exec-record-raw">
            <summary>{t("diagnostics.executions.rawAttributes")}</summary>
            <pre className="command-output">{JSON.stringify(record.attributes, null, 2)}</pre>
          </details>
        </div>
      ) : null}
      {hasPayload ? (
        <div className="exec-record-section">
          <Text fw={600} size="sm">
            {t("diagnostics.executions.payload")}
          </Text>
          <pre className="command-output">{JSON.stringify(payload, null, 2)}</pre>
        </div>
      ) : null}
      {metaEntries.length > 0 ? (
        <details className="exec-record-developer">
          <summary>{t("diagnostics.executions.developer.title")}</summary>
          <dl className="exec-record-meta">
            {metaEntries.map(([label, value]) => (
              <FragmentRow key={label} label={label} value={value} />
            ))}
          </dl>
        </details>
      ) : null}
    </Stack>
  );
}

export function matchesRecordFilter(record: TraceRecord, filter: string): boolean {
  if (filter === "all") {
    return true;
  }
  if (filter === "error") {
    return (
      ["ERROR", "CRITICAL", "WARNING"].includes(record.level.toUpperCase()) ||
      record.type.endsWith(".failed")
    );
  }
  // LLM / CLI Agent group both the prompt-trace request/response records and the
  // logs emitted during that agent's span (tagged via record.span).
  if (filter === "llm") {
    return (
      (record.kind === "prompt_trace" && record.type.startsWith("llm")) ||
      (record.kind === "log" && record.span === "llm")
    );
  }
  if (filter === "cli_agent") {
    return (
      (record.kind === "prompt_trace" && record.type.startsWith("cli_agent")) ||
      (record.kind === "log" && record.span === "cli_agent")
    );
  }
  return record.kind === filter;
}

export function matchesRecordScopeFilter(
  record: TraceRecord,
  filter: RecordScopeFilter | null,
): boolean {
  if (!filter) {
    return true;
  }
  if (filter.kind === "span") {
    return record.span_id === filter.value;
  }
  if (filter.kind === "call") {
    return record.call_id === filter.value;
  }
  return record.span_id === filter.value || record.parent_id === filter.value;
}

export function recordAttributeRows(
  t: TFunction,
  attributes: Record<string, unknown>,
): Array<[string, string]> {
  return [
    ["event.provider", t("diagnostics.executions.attributeLabels.eventProvider")],
    ["github.repo", t("diagnostics.executions.attributeLabels.githubRepo")],
    ["github.number", t("diagnostics.executions.attributeLabels.githubNumber")],
    ["github.kind", t("diagnostics.executions.attributeLabels.githubKind")],
    ["github.url", t("diagnostics.executions.attributeLabels.githubUrl")],
    ["service_run_id", t("diagnostics.executions.attributeLabels.serviceRun")],
    ["slack.channel", t("diagnostics.executions.attributeLabels.slackChannel")],
    ["slack.thread_ts", t("diagnostics.executions.attributeLabels.slackThread")],
    ["slack.ts", t("diagnostics.executions.attributeLabels.slackMessage")],
  ].flatMap(([key, label]) => {
    const value = attributes[key];
    return typeof value === "string" && value ? [[label, value]] : [];
  });
}

export function traceSourceLabel(t: TFunction, source: string): string {
  return t(`diagnostics.executions.sources.${source}`, { defaultValue: source });
}

function memoryActionLabel(t: TFunction, action: string): string {
  return t(`diagnostics.memory.actions.${action}`, { defaultValue: action || "memory" });
}

function memoryActionColor(action: string): string {
  if (action === "record") {
    return "green";
  }
  if (action === "recall") {
    return "cyan";
  }
  if (action === "get") {
    return "indigo";
  }
  if (action === "update") {
    return "blue";
  }
  if (action === "touch") {
    return "teal";
  }
  if (action === "archive") {
    return "gray";
  }
  if (action === "promote") {
    return "grape";
  }
  return "dark";
}

function memoryEventKey(event: MemoryEvent): string {
  return `${event.timestamp}-${event.action}-${event.person_id}-${event.doc_id}`;
}

function sortMemoryEventsDescending(events: MemoryEvent[]): MemoryEvent[] {
  return [...events].sort(
    (left, right) => memoryEventTimestampValue(right) - memoryEventTimestampValue(left),
  );
}

function memoryEventTimestampValue(event: MemoryEvent): number {
  const value = Date.parse(event.timestamp);
  return Number.isNaN(value) ? Number.NEGATIVE_INFINITY : value;
}

function memorySourceSummary(source: Array<Record<string, unknown>>): string {
  return source
    .map((entry) =>
      [entry.type, entry.url, entry.channel_id, entry.thread_ts]
        .filter((value): value is string => typeof value === "string" && Boolean(value))
        .join(": "),
    )
    .filter(Boolean)
    .join(" / ");
}

export type AttrFilter = { key: string; value: string; label: string };

type TraceSearch = { query: string; attrFilter: AttrFilter | null };

// Derive the GitHub ticket/PR chip from a trace's attributes. Prefers the URL
// (globally unique) for exact filtering; falls back to the bare number.
export function ticketChipInfo(
  attributes: Record<string, unknown>,
): { label: string; key: string; value: string; url: string } | null {
  const number = typeof attributes["github.number"] === "string" ? attributes["github.number"] : "";
  const url = typeof attributes["github.url"] === "string" ? attributes["github.url"] : "";
  if (!number && !url) {
    return null;
  }
  const prefix = attributes["github.kind"] === "pull_request" ? "PR #" : "#";
  const label = number ? `${prefix}${number}` : prefix.trim();
  return url
    ? { label, key: "github.url", value: url, url }
    : { label, key: "github.number", value: number, url: "" };
}

// Turn a ticket-lookup input into an exact structured filter. Accepts a GitHub
// issue/PR URL, "owner/repo#42", "#42" or a bare "42" — never a fuzzy match.
export function parseTicketQuery(input: string): AttrFilter | null {
  const trimmed = input.trim();
  if (!trimmed) {
    return null;
  }
  const ticketPrefixMatch = trimmed.match(/^ticket:(\d+)$/i);
  if (ticketPrefixMatch) {
    return {
      key: "github.number",
      value: ticketPrefixMatch[1],
      label: `#${ticketPrefixMatch[1]}`,
    };
  }
  const urlMatch = trimmed.match(/^https?:\/\/\S+\/(?:issues|pull)\/(\d+)\b/);
  if (urlMatch) {
    return {
      key: "github.url",
      value: trimmed.replace(/[#?].*$/, ""),
      label: `#${urlMatch[1]}`,
    };
  }
  const refMatch = trimmed.match(/^(?:[\w.-]+\/[\w.-]+)?#?(\d+)$/);
  if (refMatch) {
    return { key: "github.number", value: refMatch[1], label: `#${refMatch[1]}` };
  }
  return null;
}

export function parseTraceSearch(input: string): TraceSearch {
  const trimmed = input.trim();
  if (!trimmed) {
    return { query: "", attrFilter: null };
  }
  const tokens = trimmed.split(/\s+/);
  const ticketIndex = tokens.findIndex((token) => {
    const parsed = parseTicketQuery(token);
    return parsed && isTicketSearchToken(token, tokens.length === 1);
  });
  if (ticketIndex < 0) {
    return { query: trimmed, attrFilter: null };
  }
  return {
    query: tokens.filter((_, index) => index !== ticketIndex).join(" "),
    attrFilter: parseTicketQuery(tokens[ticketIndex]),
  };
}

function isTicketSearchToken(token: string, singleToken: boolean): boolean {
  return (
    /^https?:\/\//i.test(token) ||
    /^ticket:/i.test(token) ||
    token.includes("#") ||
    (singleToken && /^\d+$/.test(token))
  );
}

async function openExternal(url: string): Promise<void> {
  if (!url) {
    return;
  }
  if (isTauriRuntime()) {
    const { open } = await import("@tauri-apps/plugin-shell");
    await open(url);
    return;
  }
  window.open(url, "_blank", "noopener,noreferrer");
}

// Surface the most useful one-line summary per record: log message, prompt
// description, or — for events — the payload detail (e.g. a failure reason)
// falling back to the raw event type.
export function recordDisplayMessage(record: TraceRecord): string {
  if (record.kind === "log") {
    return record.message;
  }
  if (record.kind === "prompt_trace") {
    return record.message || record.type;
  }
  if (record.kind === "memory") {
    return record.message || record.type;
  }
  const payload = record.payload ?? {};
  for (const key of ["message", "error", "code", "error_type"]) {
    const value = payload[key];
    if (typeof value === "string" && value) {
      return value;
    }
  }
  return record.type;
}

export function shortTraceId(id: string): string {
  return id.length > 16 ? `${id.slice(0, 8)}…${id.slice(-6)}` : id;
}

export function traceDuration(summary: { started_at: string; updated_at: string }): string {
  const start = Date.parse(summary.started_at);
  const end = Date.parse(summary.updated_at);
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) {
    return "—";
  }
  const ms = end - start;
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
}

function memoryDuration(ms: number): string {
  return ms < 1000 ? `${Math.max(0, Math.round(ms))}ms` : `${(ms / 1000).toFixed(1)}s`;
}

export function traceStatusColor(status: string): string {
  if (status === "success") {
    return "green";
  }
  if (status === "failed") {
    return "red";
  }
  if (status === "running") {
    return "blue";
  }
  return "gray";
}

export function recordBadgeColor(record: TraceRecord): string {
  if (record.kind === "prompt_trace") {
    return "violet";
  }
  if (record.kind === "memory") {
    const action =
      typeof record.attributes["memory.action"] === "string"
        ? record.attributes["memory.action"]
        : "";
    return memoryActionColor(action);
  }
  if (record.kind === "log") {
    return logBadgeColor(record.level);
  }
  return eventBadgeColor(record.type);
}

export function recordBadgeLabel(t: TFunction, record: TraceRecord): string {
  if (record.kind === "prompt_trace") {
    return record.type || t("diagnostics.executions.kinds.prompt_trace");
  }
  if (record.kind === "memory") {
    const action =
      typeof record.attributes["memory.action"] === "string"
        ? record.attributes["memory.action"]
        : record.type.replace(/^memory\./, "");
    return memoryActionLabel(t, action);
  }
  if (record.kind === "log") {
    return record.level || "LOG";
  }
  return eventTypeLabel(t, record.type);
}

function ServiceRuntimeSection({
  title,
  description,
  unit,
  state,
  enabled,
  disabled,
  switchLabel,
  onEnabledChange,
  rows,
  children,
}: {
  title: string;
  description: string;
  unit: RuntimeUnitStatus | undefined;
  state?: RuntimeUnitStatus["state"];
  enabled?: boolean;
  disabled?: boolean;
  switchLabel?: string;
  onEnabledChange?: (enabled: boolean) => void;
  rows: Array<[string, string]>;
  children?: ReactNode;
}) {
  const { t } = useTranslation();
  const status = state ?? unit?.state ?? "stopped";
  return (
    <div className="service-unit-panel">
      <div className="service-unit-heading">
        <Group
          className="service-unit-title-row"
          justify="space-between"
          align="center"
          gap="sm"
          wrap="nowrap"
        >
          <Group className="service-unit-title" gap="xs" wrap="nowrap">
            <Text fw={700}>{title}</Text>
            <RuntimeStateBadge state={isStopTimeoutPending(unit) ? "stopping" : status} />
          </Group>
          {onEnabledChange && switchLabel ? (
            <Switch
              className="service-unit-switch"
              checked={enabled ?? false}
              disabled={disabled}
              label={switchLabel}
              onChange={(event) => onEnabledChange(event.currentTarget.checked)}
            />
          ) : null}
        </Group>
        <Text c="dimmed" size="sm">
          {description}
        </Text>
      </div>
      {children ? <Stack gap="sm">{children}</Stack> : null}
      <dl className="status-list compact">
        <dt>{t("overview.runtimeFields.startedAt")}</dt>
        <dd>{formatDateTime(unit?.started_at)}</dd>
        <dt>{t("overview.runtimeFields.stoppedAt")}</dt>
        <dd>{formatDateTime(unit?.stopped_at)}</dd>
        {rows.map(([label, value]) => (
          <FragmentRow key={label} label={label} value={value || t("overview.unknown")} />
        ))}
      </dl>
      {(unit?.events_auth_failed_count ?? 0) > 0 ? (
        <Alert color="red" title={t("overview.eventsCard.authFailedTitle")}>
          {t("overview.eventsCard.authFailedBody", {
            persons: (unit?.events_auth_failed_persons ?? []).join(", ") || t("overview.unknown"),
          })}
        </Alert>
      ) : null}
      {isStopTimeoutPending(unit) ? (
        <Text c="dimmed" size="sm">
          {t("overview.stopDelayHint")}
        </Text>
      ) : unit?.error ? (
        <Alert color="red" title={t("overview.runtimeError")}>
          {unit.error}
        </Alert>
      ) : null}
    </div>
  );
}

function RuntimeDebugSettings() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const runtimeDebug = useQuery({
    queryKey: ["runtime-debug"],
    queryFn: getRuntimeDebug,
    refetchInterval: 5000,
  });
  const runtimeDebugMutation = useMutation({
    mutationFn: (enabled: boolean) => updateRuntimeDebug({ enabled }),
    onSuccess: (data) => {
      queryClient.setQueryData(["runtime-debug"], data);
      queryClient.invalidateQueries({ queryKey: ["runtime-debug"] });
    },
  });
  const enabled = Boolean(runtimeDebug.data?.enabled);
  return (
    <div className="trace-runtime-settings">
      <Group justify="space-between" align="center">
        <div>
          <Text fw={700} size="sm">
            {t("overview.runtimeDebug.title")}
          </Text>
          <Text c="dimmed" size="xs">
            {t("overview.runtimeDebug.status", {
              logLevel: runtimeDebug.data?.log_level ?? "-",
              agnoDebug: runtimeDebug.data?.agno_debug ? "true" : "false",
            })}
          </Text>
        </div>
        <Switch
          checked={enabled}
          disabled={runtimeDebugMutation.isPending || runtimeDebug.isLoading}
          label={enabled ? t("overview.runtimeDebug.enabled") : t("overview.runtimeDebug.disabled")}
          onChange={(event) => runtimeDebugMutation.mutate(event.currentTarget.checked)}
        />
      </Group>
      {runtimeDebugMutation.error ? (
        <Alert color="red" title={t("overview.runtimeDebug.saveError")}>
          {runtimeDebugMutation.error.message}
        </Alert>
      ) : null}
    </div>
  );
}

function PromptTraceOutputSettings() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [outputTracePath, setOutputTracePath] = useState("");
  const [outputTracePathEdited, setOutputTracePathEdited] = useState(false);
  const canPickFile = isTauriRuntime();
  const promptTrace = useQuery({
    queryKey: ["prompt-trace-output"],
    queryFn: () => getPromptTrace(PROMPT_TRACE_LIMIT),
    refetchInterval: 5000,
  });
  const promptTraceMutation = useMutation({
    mutationFn: (enabled: boolean) =>
      updatePromptTrace({ enabled, trace_path: outputTracePath.trim() }, PROMPT_TRACE_LIMIT),
    onSuccess: (data) => {
      setOutputTracePath(data.output_trace_file);
      setOutputTracePathEdited(false);
      queryClient.invalidateQueries({ queryKey: ["prompt-trace"] });
      queryClient.invalidateQueries({ queryKey: ["prompt-trace-output"] });
    },
  });
  const promptTraceOutputPathMutation = useMutation({
    mutationFn: (tracePath: string) =>
      updatePromptTrace(
        {
          enabled: Boolean(promptTrace.data?.enabled),
          trace_path: tracePath,
        },
        PROMPT_TRACE_LIMIT,
      ),
    onSuccess: (data) => {
      setOutputTracePath(data.output_trace_file);
      setOutputTracePathEdited(false);
      queryClient.invalidateQueries({ queryKey: ["prompt-trace"] });
      queryClient.invalidateQueries({ queryKey: ["prompt-trace-output"] });
    },
  });
  const effectiveOutputTracePath = outputTracePathEdited
    ? outputTracePath
    : (promptTrace.data?.output_trace_file ?? "");
  const applyOutputTracePath = (tracePath: string = effectiveOutputTracePath) => {
    const normalizedPath = tracePath.trim();
    if (!outputTracePathEdited && normalizedPath === (promptTrace.data?.output_trace_file ?? "")) {
      return;
    }
    promptTraceOutputPathMutation.mutate(normalizedPath);
  };
  const resetOutputTracePath = () => {
    const defaultPath = promptTrace.data?.default_trace_file ?? "";
    if (!defaultPath) {
      return;
    }
    setOutputTracePath(defaultPath);
    setOutputTracePathEdited(false);
    promptTraceOutputPathMutation.mutate(defaultPath);
  };
  const pickOutputTracePath = async () => {
    const selected = await selectTraceFile(
      "save",
      effectiveOutputTracePath || promptTrace.data?.default_trace_file || "",
    );
    if (!selected) {
      return;
    }
    setOutputTracePath(selected);
    setOutputTracePathEdited(false);
    promptTraceOutputPathMutation.mutate(selected);
  };
  return (
    <div className="trace-runtime-settings">
      <Group justify="space-between" align="center">
        <div>
          <Text fw={700} size="sm">
            {t("overview.promptTrace.runtimeTitle")}
          </Text>
          <Text c="dimmed" size="xs">
            {t("overview.promptTrace.runtimeDescription")}
          </Text>
        </div>
        <Switch
          checked={Boolean(promptTrace.data?.enabled)}
          disabled={promptTraceMutation.isPending}
          label={
            promptTrace.data?.enabled
              ? t("overview.promptTrace.enabled")
              : t("overview.promptTrace.disabled")
          }
          onChange={(event) => promptTraceMutation.mutate(event.currentTarget.checked)}
        />
      </Group>
      <div className="trace-settings">
        <TracePathField
          label={t("overview.promptTrace.outputPath")}
          value={effectiveOutputTracePath}
          edited={outputTracePathEdited}
          applying={promptTraceOutputPathMutation.isPending}
          canPickFile={canPickFile}
          onChange={(value) => {
            setOutputTracePath(value);
            setOutputTracePathEdited(true);
          }}
          onApply={applyOutputTracePath}
          onPick={pickOutputTracePath}
          onResetDefault={resetOutputTracePath}
          defaultDisabled={!promptTrace.data?.default_trace_file}
          pickLabel={t("overview.promptTrace.chooseOutputPath")}
        />
      </div>
      {promptTraceMutation.error || promptTraceOutputPathMutation.error ? (
        <Alert color="red" title={t("overview.promptTrace.saveError")}>
          {(promptTraceMutation.error ?? promptTraceOutputPathMutation.error)?.message}
        </Alert>
      ) : null}
    </div>
  );
}

function TracePathField({
  label,
  value,
  edited,
  applying,
  canPickFile,
  defaultDisabled,
  pickLabel,
  onChange,
  onApply,
  onPick,
  onResetDefault,
}: {
  label: string;
  value: string;
  edited: boolean;
  applying: boolean;
  canPickFile: boolean;
  defaultDisabled: boolean;
  pickLabel: string;
  onChange: (value: string) => void;
  onApply: (value: string) => void;
  onPick: () => void;
  onResetDefault: () => void;
}) {
  const { t } = useTranslation();
  const applyCurrentValue = () => onApply(value);
  return (
    <div className="trace-path-field">
      <TextInput
        label={label}
        value={value}
        rightSectionWidth={76}
        rightSection={
          <Group gap={4} wrap="nowrap">
            <Tooltip
              label={canPickFile ? pickLabel : t("overview.promptTrace.filePickerUnavailable")}
            >
              <ActionIcon
                aria-label={pickLabel}
                disabled={!canPickFile}
                size="sm"
                variant="subtle"
                onMouseDown={(event) => event.preventDefault()}
                onClick={onPick}
              >
                <FolderOpen size={16} />
              </ActionIcon>
            </Tooltip>
            <Tooltip label={t("overview.promptTrace.resetDefaultPath")}>
              <ActionIcon
                aria-label={t("overview.promptTrace.resetDefaultPath")}
                disabled={defaultDisabled}
                size="sm"
                variant="subtle"
                onMouseDown={(event) => event.preventDefault()}
                onClick={onResetDefault}
              >
                <RotateCcw size={16} />
              </ActionIcon>
            </Tooltip>
          </Group>
        }
        rightSectionPointerEvents="auto"
        onBlur={applyCurrentValue}
        onChange={(event) => onChange(event.currentTarget.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.currentTarget.blur();
          }
        }}
      />
      {edited || applying ? (
        <Text c="dimmed" className="trace-path-status" size="xs">
          {applying ? t("overview.promptTrace.pathApplying") : t("overview.promptTrace.pathEdited")}
        </Text>
      ) : null}
    </div>
  );
}

function PromptTraceList({ entries }: { entries: PromptTraceEntry[] }) {
  const { t } = useTranslation();
  const groups = useMemo(() => buildTraceGroups(entries), [entries]);
  const [selectedId, setSelectedId] = useState("");
  const [drawerOpen, setDrawerOpen] = useState(false);
  if (entries.length === 0) {
    return <div className="empty-row">{t("overview.promptTrace.empty")}</div>;
  }
  const selected = groups.find((group) => group.id === selectedId) ?? groups[0];
  return (
    <div className="trace-browser">
      <div className="trace-list">
        <div className="trace-header">
          <span>{t("overview.promptTrace.columns.kind")}</span>
          <span>{t("overview.promptTrace.columns.person")}</span>
          <span>{t("overview.promptTrace.columns.time")}</span>
          <span>{t("overview.promptTrace.columns.brain")}</span>
          <span>{t("overview.promptTrace.columns.io")}</span>
        </div>
        {groups.map((group) => (
          <button
            className={`trace-row ${group.id === selected.id ? "active" : ""}`}
            key={group.id}
            type="button"
            onClick={() => {
              setSelectedId(group.id);
              setDrawerOpen(true);
            }}
          >
            <span>
              <Badge color={traceKindColor(group.kind)} variant="light">
                {traceKindLabel(t, group.kind)}
              </Badge>
            </span>
            <span>
              <Group gap={4} align="center" style={{ display: "inline-flex" }}>
                <Avatar
                  src={group.personId ? memberAvatarUrl(group.personId) : undefined}
                  size={16}
                  radius="xl"
                >
                  {group.personId ? group.personId.substring(0, 2).toUpperCase() : "-"}
                </Avatar>
                {group.personId || "-"}
              </Group>
            </span>
            <span>{group.timestamp ? formatTime(group.timestamp) : "-"}</span>
            <span title={group.brain}>{traceBrainLabel(group.brain)}</span>
            <span className="trace-io">
              {group.request ? <i>{t("overview.promptTrace.requestShort")}</i> : null}
              {group.response ? <i>{t("overview.promptTrace.responseShort")}</i> : null}
              {group.single ? <i>{traceEventLabel(t, group.single.event)}</i> : null}
            </span>
          </button>
        ))}
      </div>
      <Drawer
        opened={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        position="right"
        size="80%"
        title={`${traceKindLabel(t, selected.kind)} / ${traceBrainLabel(selected.brain)}`}
      >
        <PromptTraceDetails group={selected} />
      </Drawer>
    </div>
  );
}

function PromptTraceDetails({ group }: { group: PromptTraceGroup }) {
  const { t } = useTranslation();
  const request = group.request ?? group.single;
  const response = group.response ?? group.single;
  const requestText = request?.prompt ? decodeTraceText(request.prompt) : "";
  const responseText = response?.response ? decodeTraceText(response.response) : "";
  const descriptionText = request?.description ? decodeTraceText(request.description) : "";
  const transcriptText =
    request?.transcript || response?.transcript || group.single?.transcript
      ? decodeTraceText(
          request?.transcript || response?.transcript || group.single?.transcript || "",
        )
      : "";
  const contextText = descriptionText || transcriptText;
  const contextLabel = descriptionText
    ? t("overview.promptTrace.descriptionLabel")
    : t("overview.promptTrace.transcriptLabel");
  const errorText = decodeTraceText(
    group.response?.error || group.request?.error || group.single?.error || "",
  );
  const metadata = traceGroupMetadata(group);
  return (
    <div className="trace-detail">
      <Group justify="space-between" align="flex-start" style={{ width: "100%" }}>
        <div>
          <Text fw={700}>
            {traceKindLabel(t, group.kind)} / {traceBrainLabel(group.brain)}
          </Text>
          <Text c="dimmed" size="xs">
            {group.personId || "-"} · {group.timestamp ? formatDateTime(group.timestamp) : "-"}
          </Text>
          <Badge color={traceKindColor(group.kind)} variant="light" mt={6}>
            {group.request && group.response
              ? t("overview.promptTrace.requestResponse")
              : t("overview.promptTrace.singleEvent")}
          </Badge>
        </div>
        {group.personId && (
          <Avatar src={memberAvatarUrl(group.personId)} size="lg" radius="md">
            {group.personId.substring(0, 2).toUpperCase()}
          </Avatar>
        )}
      </Group>
      <div className="trace-detail-meta">
        {metadata.map(([label, value]) => (
          <span key={label}>
            {label}: <b>{value}</b>
          </span>
        ))}
      </div>
      <div className={`trace-detail-grid ${contextText ? "with-context" : ""}`}>
        {contextText ? (
          <div className="trace-preview">
            <Text fw={700} size="xs">
              {contextLabel}
            </Text>
            <pre>{contextText}</pre>
          </div>
        ) : null}
        <div className="trace-preview">
          <Text fw={700} size="xs">
            {t("overview.promptTrace.prompt")}
          </Text>
          <pre>{requestText || t("overview.promptTrace.noRequest")}</pre>
        </div>
        <div className="trace-preview">
          <Text fw={700} size="xs">
            {t("overview.promptTrace.response")}
          </Text>
          <pre>{responseText || t("overview.promptTrace.noResponse")}</pre>
        </div>
      </div>
      {errorText ? (
        <Text c="red" size="sm">
          {errorText}
        </Text>
      ) : null}
    </div>
  );
}

function FragmentRow({ label, value }: { label: string; value: string }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </>
  );
}

function RuntimeStateBadge({ state }: { state: RuntimeUnitStatus["state"] }) {
  const { t } = useTranslation();
  const color =
    state === "running"
      ? "teal"
      : state === "failed"
        ? "red"
        : state === "stopped"
          ? "gray"
          : "orange";
  return (
    <Badge color={color} variant="light">
      {t(`overview.runtimeStates.${state}`)}
    </Badge>
  );
}

export function isStopTimeoutPending(unit: RuntimeUnitStatus | undefined) {
  return Boolean(
    unit?.running && unit.state === "failed" && unit.error?.includes("did not stop before timeout"),
  );
}

function ScenarioDiagnosticsSummary({
  checks,
  error,
  loading,
}: {
  checks: DiagnosticCheck[];
  error: Error | null;
  loading: boolean;
}) {
  const { t } = useTranslation();
  if (loading) {
    return (
      <Text size="sm" c="dimmed">
        {t("overview.scenarioDiagnostics.running")}
      </Text>
    );
  }
  if (error) {
    return (
      <Alert color="red" title={t("overview.scenarioDiagnostics.failed")}>
        {error.message}
      </Alert>
    );
  }
  if (checks.length === 0) {
    return (
      <Text size="sm" c="dimmed">
        {t("overview.scenarioDiagnostics.notRun")}
      </Text>
    );
  }
  const issues = checks.filter((check) => check.status !== "ok");
  if (issues.length === 0) {
    return (
      <Alert color="green" title={t("overview.scenarioDiagnostics.ok")}>
        {t("overview.scenarioDiagnostics.okDescription", { count: checks.length })}
      </Alert>
    );
  }
  return (
    <Stack gap="xs">
      {issues.map((check, index) => (
        <Alert
          color={diagnosticColor(check.status)}
          icon={diagnosticIcon(check.status)}
          className={`diagnostic-alert ${check.status}`}
          key={`${check.code}-${check.target}-${index}`}
          title={diagnosticTitle(t, check)}
        >
          <Text size="xs" c="dimmed" mb={4}>
            {t(`overview.diagnosticSections.${check.section}`)}
            {check.person_id ? ` / ${check.person_id}` : ""}
          </Text>
          <Text size="sm">{diagnosticDescription(t, check)}</Text>
          {diagnosticDetail(t, check) ? (
            <Text size="xs" c="dimmed" mt={6}>
              {diagnosticDetail(t, check)}
            </Text>
          ) : null}
          {check.target ? (
            <Text size="xs" c="dimmed" mt={4}>
              {t("overview.scenarioDiagnostics.target")}:{" "}
              <Text span ff="monospace">
                {check.target}
              </Text>
            </Text>
          ) : null}
        </Alert>
      ))}
    </Stack>
  );
}

function diagnosticTitle(t: TFunction, check: DiagnosticCheck) {
  const namespace =
    check.status === "ok" ? "overview.diagnosticSuccess" : "overview.diagnosticChecks";
  return t(`${namespace}.${check.code}.title`, { defaultValue: check.message });
}

function diagnosticDescription(t: TFunction, check: DiagnosticCheck) {
  const namespace =
    check.status === "ok" ? "overview.diagnosticSuccess" : "overview.diagnosticChecks";
  return t(`${namespace}.${check.code}.description`, {
    defaultValue: check.status === "ok" ? "" : check.message,
  });
}

function diagnosticDetail(t: TFunction, check: DiagnosticCheck) {
  if (check.status === "ok") {
    return "";
  }
  const description = diagnosticDescription(t, check);
  return description && description !== check.message ? check.message : "";
}

function diagnosticColor(status: DiagnosticCheck["status"]) {
  if (status === "ok") {
    return "teal";
  }
  if (status === "warning") {
    return "orange";
  }
  return "red";
}

function diagnosticIcon(status: DiagnosticCheck["status"]) {
  if (status === "ok") {
    return <CheckCircle2 size={18} />;
  }
  if (status === "warning") {
    return <TriangleAlert size={18} />;
  }
  return <XCircle size={18} />;
}

function sourceState(
  unit: RuntimeUnitStatus | undefined,
  sourceEnabled: boolean | null | undefined,
) {
  if (unit?.running && !sourceEnabled) {
    return "stopped";
  }
  return unit?.state ?? "stopped";
}

function eventSourceState(
  worker: RuntimeUnitStatus | undefined,
  events: RuntimeUnitStatus | undefined,
) {
  if (events?.state === "failed" || events?.state === "starting" || events?.state === "stopping") {
    return events.state;
  }
  return sourceState(worker, worker?.event_queue_source_enabled);
}

function workerSourceStatusLabel(
  t: TFunction,
  unit: RuntimeUnitStatus | undefined,
  fallback: { scheduled: boolean; routine: boolean; eventQueue: boolean },
) {
  const enabled: string[] = [];
  if (unit?.scheduled_source_enabled ?? fallback.scheduled) {
    enabled.push(t("overview.workerCard.scheduledSource"));
  }
  if (unit?.routine_source_enabled ?? fallback.routine) {
    enabled.push(t("overview.workerCard.routineSource"));
  }
  if (unit?.event_queue_source_enabled ?? fallback.eventQueue) {
    enabled.push(t("overview.workerCard.eventQueueSource"));
  }
  return enabled.length ? enabled.join(" / ") : t("overview.none");
}

function sourceEnabledLabel(t: TFunction, enabled: boolean | null | undefined) {
  return enabled ? t("overview.enabled") : t("overview.disabled");
}

function traceEventLabel(t: TFunction, event: string) {
  return t(`overview.promptTrace.events.${event.replace(/\./g, "_")}`, {
    defaultValue: event,
  });
}

function traceKindLabel(t: TFunction, kind: string) {
  return t(`overview.promptTrace.kinds.${kind}`, { defaultValue: kind });
}

function traceKindColor(kind: string) {
  if (kind === "llm") {
    return "blue";
  }
  if (kind === "cli") {
    return "violet";
  }
  if (kind === "chat") {
    return "indigo";
  }
  return "gray";
}

export function traceBrainLabel(brain: string) {
  return (
    brain
      .split("/")
      .pop()
      ?.replace(/\.[^.]+$/, "") || "-"
  );
}

export function traceGroupMetadata(group: PromptTraceGroup): Array<[string, string]> {
  const rows = new Map<string, string>();
  for (const entry of [group.request, group.response, group.single]) {
    if (!entry) {
      continue;
    }
    for (const [label, value] of traceFieldRows(entry)) {
      rows.set(label, value);
    }
  }
  if (group.brain) {
    rows.set("brain", decodeTraceText(group.brain));
  }
  return Array.from(rows.entries()).slice(0, 10);
}

export function traceFieldRows(entry: PromptTraceEntry): Array<[string, string]> {
  const rows: Array<[string, string]> = [];
  for (const [label, value] of [
    ["brain", entry.brain],
    ["command", entry.command],
    ["target", entry.target],
    ["cwd", entry.cwd],
  ]) {
    if (value) {
      rows.push([label, decodeTraceText(value)]);
    }
  }
  for (const [label, value] of Object.entries(entry.fields)) {
    if (rows.some(([existing]) => existing === label)) {
      continue;
    }
    if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
      rows.push([label, decodeTraceText(String(value))]);
    }
  }
  return rows.slice(0, 8);
}

export function decodeTraceText(value: string) {
  return value
    .replace(/\\u([0-9a-fA-F]{4})/g, (_, hex: string) =>
      String.fromCharCode(Number.parseInt(hex, 16)),
    )
    .replace(/\\n/g, "\n")
    .replace(/\\t/g, "\t");
}

function formatDateTime(value: string | null | undefined) {
  const date = value ? new Date(value) : null;
  if (!date || Number.isNaN(date.getTime())) {
    return "-";
  }
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "short",
    timeStyle: "medium",
  }).format(date);
}

function formatTime(value: string | null | undefined) {
  // Guard invalid/empty values: Intl.DateTimeFormat.format(new Date("")) throws
  // "Invalid time value", which would crash the whole React tree.
  const date = value ? new Date(value) : null;
  if (!date || Number.isNaN(date.getTime())) {
    return "";
  }
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}

export function eventTypeLabel(t: TFunction, type: string) {
  if (type.startsWith("command.")) {
    return t(`overview.eventTypes.${type.replace("command.", "command_")}`, {
      defaultValue: type.replace("command.", ""),
    });
  }
  if (type.startsWith("scheduler.")) {
    return t("overview.eventTypes.scheduler");
  }
  if (type.startsWith("events.")) {
    return t("overview.eventTypes.events");
  }
  return type;
}

export function eventBadgeColor(type: string) {
  if (type.endsWith(".failed")) {
    return "red";
  }
  if (type.includes("running") || type.includes("started") || type.includes("finished")) {
    return "teal";
  }
  if (type.includes("stopping")) {
    return "orange";
  }
  return "gray";
}

export function logBadgeColor(level: string) {
  const upper = level.toUpperCase();
  if (upper === "ERROR" || upper === "CRITICAL") {
    return "red";
  }
  if (upper === "WARNING") {
    return "orange";
  }
  return "gray";
}

function isTauriRuntime() {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

export async function openLocalFile(path: string) {
  if (!isTauriRuntime()) {
    return;
  }
  const { open } = await import("@tauri-apps/plugin-shell");
  await open(path);
}

export function localFileHref(path: string) {
  const normalizedPath = path.replace(/\\/g, "/");
  const prefix = normalizedPath.startsWith("/") ? "file://" : "file:///";
  return encodeURI(`${prefix}${normalizedPath}`);
}

export async function selectTraceFile(mode: "open" | "save", currentPath: string) {
  if (!isTauriRuntime()) {
    return null;
  }
  const selected =
    mode === "open"
      ? await (
          await import("@tauri-apps/plugin-dialog")
        ).open({
          defaultPath: currentPath || undefined,
          directory: false,
          multiple: false,
          title: "Prompt trace file",
        })
      : await (
          await import("@tauri-apps/plugin-dialog")
        ).save({
          defaultPath: currentPath || undefined,
          title: "Prompt trace file",
        });
  return typeof selected === "string" ? selected : null;
}

function CommandsPage() {
  const { t } = useTranslation();
  const config = useQuery({ queryKey: ["config"], queryFn: getConfigStatus });
  const team = useQuery({ queryKey: ["team"], queryFn: getTeam, retry: false });
  const hasProjectConfig = Boolean(config.data?.project_file_exists);
  const [initialHistory] = useState(loadCustomCommandHistory);
  const restoreCustom = initialHistory.lastRunWasCustom && initialHistory.commands.length > 0;
  const [mode, setMode] = useState(restoreCustom ? "custom" : "catalog");
  const [selectedCommand, setSelectedCommand] = useState("");
  const [customCommand, setCustomCommand] = useState(
    restoreCustom ? initialHistory.commands[0] : "",
  );
  const [customHistory, setCustomHistory] = useState<string[]>(initialHistory.commands);
  const [lastRunWasCustom, setLastRunWasCustom] = useState(initialHistory.lastRunWasCustom);
  const [rawArgs, setRawArgs] = useState("");
  const [argValues, setArgValues] = useState<Record<string, string>>({});
  const [message, setMessage] = useState("");
  const [person, setPerson] = useState<string | null>(null);
  const [cwd, setCwd] = useState("");
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [runtimeEvents, setRuntimeEvents] = useState<RuntimeEvent[]>([]);
  const [runtimeLogs, setRuntimeLogs] = useState<RuntimeLog[]>([]);
  const [history, setHistory] = useState<CommandRunRecord[]>([]);
  const [activeTraceId, setActiveTraceId] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<string | null>("events");
  const commandOptions = useQuery({
    queryKey: ["command-options", person],
    queryFn: () => getCommandOptions(person || undefined),
    enabled: hasProjectConfig,
    retry: false,
  });
  const commandCatalog = useMemo(
    () => commandOptions.data?.options ?? [],
    [commandOptions.data?.options],
  );

  const selectedOption = useMemo(
    () =>
      commandCatalog.find((option) => option.command === selectedCommand) ??
      commandCatalog.find((option) =>
        option.requirements.every((requirement) => requirement.satisfied),
      ) ??
      commandCatalog[0] ??
      null,
    [commandCatalog, selectedCommand],
  );
  const effectiveSelectedCommand = selectedOption?.command ?? "";
  const commandOptionByValue = useMemo(
    () => new Map(commandCatalog.map((option) => [option.command, option])),
    [commandCatalog],
  );
  const command = mode === "catalog" ? effectiveSelectedCommand : customCommand.trim();
  const commandArgs = useMemo(
    () => buildCommandArgs(mode === "catalog" ? selectedOption : null, argValues, rawArgs),
    [argValues, mode, rawArgs, selectedOption],
  );
  const blockingRequirements =
    mode === "catalog"
      ? (selectedOption?.requirements.filter((requirement) => !requirement.satisfied) ?? [])
      : [];
  const activeMembers = useMemo(
    () => (team.data?.members ?? []).filter((member) => member.is_active),
    [team.data?.members],
  );
  const effectivePerson =
    activeMembers.find((member) => member.person_id === person)?.person_id ??
    activeMembers[0]?.person_id ??
    null;
  const runDisabled =
    !hasProjectConfig ||
    !command ||
    !effectivePerson ||
    activeMembers.length === 0 ||
    blockingRequirements.length > 0;

  const runMutation = useMutation({
    mutationFn: () =>
      runCommand({
        command,
        args: commandArgs,
        message,
        person: effectivePerson ?? undefined,
        cwd: cwd.trim() || undefined,
      }),
    onMutate: () => {
      setActiveTraceId(null);
      setActiveTab("events");
      const ranCustom = mode === "custom";
      setLastRunWasCustom(ranCustom);
      if (ranCustom) {
        setCustomHistory((current) => pushCustomCommand(current, command));
      }
    },
    onSuccess: (response) => {
      setActiveTraceId(response.trace_id);
      setActiveTab("output");
      setHistory((current) =>
        upsertCommandRecord(current, {
          traceId: response.trace_id,
          person: effectivePerson ?? "",
          command,
          startedAt: new Date().toISOString(),
          status: "success",
          output: response.output,
        }),
      );
    },
    onError: (error) => {
      const traceId = activeTraceId ?? `local-${Date.now()}`;
      setActiveTraceId(traceId);
      setActiveTab("output");
      setHistory((current) =>
        upsertCommandRecord(current, {
          traceId,
          person: effectivePerson || "",
          command,
          startedAt: new Date().toISOString(),
          status: "failed",
          error: error instanceof Error ? error.message : String(error),
        }),
      );
    },
  });
  const runBusy = runMutation.isPending;
  const commandBlocked = blockingRequirements.length > 0;
  const canRun = !runBusy && !runDisabled && !commandBlocked;

  useEffect(() => {
    saveCustomCommandHistory({ commands: customHistory, lastRunWasCustom });
  }, [customHistory, lastRunWasCustom]);

  useEffect(() => {
    const stopEvents = subscribeEvents((event) => {
      if (!event.type.startsWith("command.")) {
        return;
      }
      setRuntimeEvents((current) => [event, ...current].slice(0, 80));
      if (!event.trace_id) {
        return;
      }
      if (event.type === "command.started") {
        setActiveTraceId(event.trace_id);
        setHistory((current) =>
          upsertCommandRecord(current, {
            traceId: event.trace_id ?? "",
            person: stringPayload(event.payload.person),
            command: stringPayload(event.payload.command),
            startedAt: event.timestamp,
            status: "running",
          }),
        );
      }
      if (event.type === "command.failed") {
        setHistory((current) =>
          upsertCommandRecord(current, {
            traceId: event.trace_id ?? "",
            person: stringPayload(event.payload.person),
            command: stringPayload(event.payload.command),
            startedAt: event.timestamp,
            status: "failed",
            error: commandFailureDetail(event),
          }),
        );
      }
      if (event.type === "command.finished") {
        setHistory((current) =>
          upsertCommandRecord(current, {
            traceId: event.trace_id ?? "",
            person: stringPayload(event.payload.person),
            command: stringPayload(event.payload.command),
            startedAt: event.timestamp,
            status: "success",
          }),
        );
      }
    });
    // Logs flow on a single path now (no command.log events); collect them so
    // the events tab can show the run's logs inline, scoped by trace id.
    const stopLogs = subscribeLogs((log) => {
      if (!log.trace_id) {
        return;
      }
      setRuntimeLogs((current) => [log, ...current].slice(0, 200));
    });
    return () => {
      stopEvents();
      stopLogs();
    };
  }, [t]);

  const selectedRecord = useMemo(
    () => history.find((record) => record.traceId === activeTraceId) ?? history[0] ?? null,
    [activeTraceId, history],
  );
  const visibleTraceId = selectedRecord?.traceId ?? activeTraceId;
  const commandTimeline = useMemo(
    () => buildCommandTimeline(runtimeEvents, runtimeLogs, visibleTraceId),
    [runtimeEvents, runtimeLogs, visibleTraceId],
  );

  return (
    <Stack gap="lg">
      <Group justify="space-between">
        <div>
          <Title order={2}>{t("commands.title")}</Title>
        </div>
        <Button
          leftSection={<Play size={16} />}
          loading={runBusy}
          disabled={!canRun}
          onClick={() => runMutation.mutate()}
        >
          {t("commands.run")}
        </Button>
      </Group>

      {!hasProjectConfig ? (
        <Alert color="yellow" title={t("overview.setupRequiredTitle")}>
          <Group justify="space-between" align="center">
            <Text size="sm">{t("overview.setupRequiredBody")}</Text>
            <Button component={NavLink} to="/setup" variant="light">
              {t("overview.openSetup")}
            </Button>
          </Group>
        </Alert>
      ) : null}

      {activeMembers.length === 0 && hasProjectConfig ? (
        <Alert color="yellow" title={t("commands.noMembersTitle")}>
          {t("commands.noMembersBody")}
        </Alert>
      ) : null}

      {commandBlocked ? (
        <Alert color="yellow" title={t("commands.requirementsBlockedTitle")}>
          {blockingRequirements
            .map((requirement) => requirementLabel(t, requirement.kind))
            .join(", ")}
        </Alert>
      ) : null}

      <Card withBorder radius="md" p="lg">
        <Stack>
          <div className="commands-layout">
            <div className="command-panel">
              <Stack className="command-form-stack">
                <div>
                  <Text fw={700}>{t("commands.formTitle")}</Text>
                  <Text c="dimmed" size="sm">
                    {t("commands.formBody")}
                  </Text>
                </div>
                <Stack className="command-field-stack" gap="xs">
                  <Group justify="space-between" align="center">
                    <Text className="field-label">{t("commands.command")}</Text>
                    <SegmentedControl
                      size="xs"
                      value={mode}
                      onChange={setMode}
                      data={[
                        { value: "catalog", label: t("commands.modeCatalog") },
                        { value: "custom", label: t("commands.modeCustom") },
                      ]}
                    />
                  </Group>

                  {mode === "catalog" ? (
                    <Select
                      aria-label={t("commands.command")}
                      searchable
                      nothingFoundMessage={t("commands.noCommandOptions")}
                      value={effectiveSelectedCommand}
                      onChange={(value) => {
                        setSelectedCommand(value ?? "");
                        setArgValues({});
                        setRawArgs("");
                      }}
                      data={commandCatalog.map((option) => ({
                        value: option.command,
                        label: `${option.label} (${option.command})`,
                      }))}
                      renderOption={({ option }) => {
                        const commandOption = commandOptionByValue.get(option.value);
                        return (
                          <Stack gap={2}>
                            <Text size="sm">{option.label}</Text>
                            {commandOption?.description ? (
                              <Text size="xs" c="dimmed">
                                {commandOption.description}
                              </Text>
                            ) : null}
                          </Stack>
                        );
                      }}
                    />
                  ) : (
                    <Autocomplete
                      aria-label={t("commands.command")}
                      placeholder={t("commands.customCommandPlaceholder")}
                      description={
                        customHistory.length ? t("commands.customCommandHistoryHint") : undefined
                      }
                      data={customHistory}
                      value={customCommand}
                      onChange={setCustomCommand}
                      // Always show the full history (newest first) instead of
                      // narrowing it to entries matching the current input.
                      filter={({ options }) => options}
                    />
                  )}
                  {selectedOption && mode === "catalog" ? (
                    <div className="command-option-summary">
                      <Group gap="xs">
                        <Badge variant="outline">
                          {t(`commands.sources.${selectedOption.source}`)}
                        </Badge>
                        {selectedOption.requirements.map((requirement) => (
                          <Badge
                            key={requirement.kind}
                            color={requirement.satisfied ? "green" : "yellow"}
                            variant="light"
                          >
                            {requirementLabel(t, requirement.kind)}
                          </Badge>
                        ))}
                      </Group>
                      {selectedOption.description ? (
                        <Text c="dimmed" size="sm">
                          {selectedOption.description}
                        </Text>
                      ) : null}
                    </div>
                  ) : null}
                  {selectedOption && mode === "catalog" ? (
                    <div className="command-script-path">
                      <Anchor
                        href={localFileHref(selectedOption.path)}
                        size="sm"
                        title={selectedOption.path}
                        onClick={(event) => {
                          if (!isTauriRuntime()) {
                            return;
                          }
                          event.preventDefault();
                          void openLocalFile(selectedOption.path).catch(console.error);
                        }}
                      >
                        {selectedOption.path}
                      </Anchor>
                      <Tooltip label={t("commands.copyScriptPath")}>
                        <ActionIcon
                          aria-label={t("commands.copyScriptPath")}
                          size="sm"
                          variant="subtle"
                          onClick={() =>
                            void navigator.clipboard
                              ?.writeText(selectedOption.path)
                              .catch(console.error)
                          }
                        >
                          <Copy size={14} />
                        </ActionIcon>
                      </Tooltip>
                    </div>
                  ) : null}
                </Stack>

                <Select
                  label={t("commands.member")}
                  placeholder={t("commands.memberPlaceholder")}
                  value={effectivePerson}
                  onChange={(value) => {
                    if (value) {
                      setPerson(value);
                    }
                  }}
                  data={activeMembers.map((member) => ({
                    value: member.person_id,
                    label: `${member.name} (${member.person_id})`,
                  }))}
                />

                {mode === "catalog" && selectedOption?.arguments.length ? (
                  <div className="command-args-grid">
                    {selectedOption.arguments.map((argument) => (
                      <TextInput
                        key={`${argument.kind}-${argument.name}`}
                        label={`${argument.name}${argument.required ? " *" : ""}`}
                        placeholder={argument.default || argument.kind}
                        value={argValues[argument.name] ?? ""}
                        onChange={(event) =>
                          setArgValues((current) => ({
                            ...current,
                            [argument.name]: event.currentTarget.value,
                          }))
                        }
                      />
                    ))}
                  </div>
                ) : null}

                {mode === "custom" || !selectedOption?.arguments.length ? (
                  <TextInput
                    label={t("commands.rawArgs")}
                    placeholder={t("commands.rawArgsPlaceholder")}
                    value={rawArgs}
                    onChange={(event) => setRawArgs(event.currentTarget.value)}
                  />
                ) : null}

                <Textarea
                  label={t("commands.message")}
                  description={t("commands.messageDescription")}
                  minRows={5}
                  value={message}
                  onChange={(event) => setMessage(event.currentTarget.value)}
                />

                <Switch
                  checked={showAdvanced}
                  label={t("commands.advanced")}
                  onChange={(event) => setShowAdvanced(event.currentTarget.checked)}
                />
                {showAdvanced ? (
                  <TextInput
                    label={t("commands.cwd")}
                    description={t("commands.cwdDescription", { cwd: config.data?.cwd ?? "" })}
                    value={cwd}
                    onChange={(event) => setCwd(event.currentTarget.value)}
                  />
                ) : null}
              </Stack>
            </div>

            <Stack>
              <div className="command-panel">
                <Stack>
                  <div>
                    <Text fw={700}>{t("commands.currentRun")}</Text>
                    <Text c="dimmed" size="sm">
                      {selectedRecord
                        ? t("commands.currentRunBody", { traceId: selectedRecord.traceId })
                        : t("commands.noRunSelected")}
                    </Text>
                  </div>
                  {selectedRecord ? (
                    <CommandRunDetails
                      record={selectedRecord}
                      items={commandTimeline}
                      activeTab={activeTab}
                      onTabChange={setActiveTab}
                    />
                  ) : (
                    <div className="empty-row">{t("commands.noRunsYet")}</div>
                  )}
                </Stack>
              </div>
            </Stack>
          </div>

          <PromptTraceOutputSettings />
          <RuntimeDebugSettings />
        </Stack>
      </Card>
    </Stack>
  );
}

export type CommandRunRecord = {
  traceId: string;
  person: string;
  command: string;
  startedAt: string;
  status: "running" | "success" | "failed";
  output?: string;
  error?: string;
};

export function commandOutputText(record: CommandRunRecord): string {
  if (record.status === "failed") {
    const detail = record.error || JSON.stringify({ trace_id: record.traceId }, null, 2);
    return record.output?.trim() ? `${detail}\n---\n${record.output}` : detail;
  }
  return record.output ?? "";
}

export const SERVICE_PREFERENCES_KEY = "guildbotics.service.preferences";

// The Service screen remembers the run targets and tuning the user last chose so
// the next launch starts from the same configuration instead of resetting to the
// built-in defaults.
export type ServicePreferences = {
  scheduledSourceEnabled: boolean;
  routineSourceEnabled: boolean;
  eventQueueSourceEnabled: boolean;
  routineIntervalMinutes: number;
  maxConsecutiveErrors: number;
};

export const DEFAULT_SERVICE_PREFERENCES: ServicePreferences = {
  scheduledSourceEnabled: true,
  routineSourceEnabled: true,
  eventQueueSourceEnabled: true,
  routineIntervalMinutes: 10,
  maxConsecutiveErrors: 3,
};

function clampStoredInteger(value: unknown, min: number, max: number, fallback: number): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return fallback;
  }
  return Math.min(max, Math.max(min, Math.round(value)));
}

export function loadServicePreferences(): ServicePreferences {
  try {
    const raw = window.localStorage.getItem(SERVICE_PREFERENCES_KEY);
    if (!raw) {
      return { ...DEFAULT_SERVICE_PREFERENCES };
    }
    const parsed = JSON.parse(raw) as Partial<ServicePreferences>;
    const legacySchedulerEnabled =
      "schedulerEnabled" in parsed
        ? (parsed as { schedulerEnabled?: unknown }).schedulerEnabled
        : undefined;
    const legacyEventsEnabled =
      "eventsEnabled" in parsed ? (parsed as { eventsEnabled?: unknown }).eventsEnabled : undefined;
    return {
      scheduledSourceEnabled:
        typeof parsed.scheduledSourceEnabled === "boolean"
          ? parsed.scheduledSourceEnabled
          : typeof legacySchedulerEnabled === "boolean"
            ? legacySchedulerEnabled
            : DEFAULT_SERVICE_PREFERENCES.scheduledSourceEnabled,
      routineSourceEnabled:
        typeof parsed.routineSourceEnabled === "boolean"
          ? parsed.routineSourceEnabled
          : typeof legacySchedulerEnabled === "boolean"
            ? legacySchedulerEnabled
            : DEFAULT_SERVICE_PREFERENCES.routineSourceEnabled,
      eventQueueSourceEnabled:
        typeof parsed.eventQueueSourceEnabled === "boolean"
          ? parsed.eventQueueSourceEnabled
          : typeof legacyEventsEnabled === "boolean"
            ? legacyEventsEnabled
            : DEFAULT_SERVICE_PREFERENCES.eventQueueSourceEnabled,
      // Clamp to the same bounds the NumberInput controls enforce so a tampered
      // or outdated value cannot push the inputs out of range.
      routineIntervalMinutes: clampStoredInteger(
        parsed.routineIntervalMinutes,
        1,
        1440,
        DEFAULT_SERVICE_PREFERENCES.routineIntervalMinutes,
      ),
      maxConsecutiveErrors: clampStoredInteger(
        parsed.maxConsecutiveErrors,
        1,
        20,
        DEFAULT_SERVICE_PREFERENCES.maxConsecutiveErrors,
      ),
    };
  } catch {
    return { ...DEFAULT_SERVICE_PREFERENCES };
  }
}

export function saveServicePreferences(value: ServicePreferences): void {
  try {
    window.localStorage.setItem(SERVICE_PREFERENCES_KEY, JSON.stringify(value));
  } catch {
    // Ignore persistence failures (e.g. storage disabled or full).
  }
}

export const CUSTOM_COMMAND_HISTORY_KEY = "guildbotics.commands.customHistory";
const CUSTOM_COMMAND_HISTORY_LIMIT = 30;

export type CustomCommandHistory = {
  commands: string[];
  lastRunWasCustom: boolean;
};

// Keep a newest-first command list well-formed: drop non-strings/blanks, trim,
// de-duplicate (first occurrence wins), and cap at the history limit. Shared by
// both the in-memory push and the persisted load so stored values cannot grow
// unbounded or contain blank entries.
function normalizeCustomCommands(values: unknown, limit = CUSTOM_COMMAND_HISTORY_LIMIT): string[] {
  if (!Array.isArray(values)) {
    return [];
  }
  const seen = new Set<string>();
  const result: string[] = [];
  for (const entry of values) {
    if (typeof entry !== "string") {
      continue;
    }
    const trimmed = entry.trim();
    if (!trimmed || seen.has(trimmed)) {
      continue;
    }
    seen.add(trimmed);
    result.push(trimmed);
    if (result.length >= limit) {
      break;
    }
  }
  return result;
}

// Newest-first history of free-input commands: a re-run moves the existing entry
// to the top instead of duplicating it.
export function pushCustomCommand(
  commands: string[],
  command: string,
  limit = CUSTOM_COMMAND_HISTORY_LIMIT,
): string[] {
  return normalizeCustomCommands([command, ...commands], limit);
}

export function loadCustomCommandHistory(): CustomCommandHistory {
  const empty: CustomCommandHistory = { commands: [], lastRunWasCustom: false };
  try {
    const raw = window.localStorage.getItem(CUSTOM_COMMAND_HISTORY_KEY);
    if (!raw) {
      return empty;
    }
    const parsed = JSON.parse(raw) as Partial<CustomCommandHistory>;
    return {
      commands: normalizeCustomCommands(parsed.commands),
      lastRunWasCustom: Boolean(parsed.lastRunWasCustom),
    };
  } catch {
    return empty;
  }
}

export function saveCustomCommandHistory(value: CustomCommandHistory): void {
  try {
    window.localStorage.setItem(CUSTOM_COMMAND_HISTORY_KEY, JSON.stringify(value));
  } catch {
    // Ignore persistence failures (e.g. storage disabled or full).
  }
}

function CommandRunDetails({
  record,
  items,
  activeTab,
  onTabChange,
}: {
  record: CommandRunRecord;
  items: CommandTimelineItem[];
  activeTab: string | null;
  onTabChange: (value: string | null) => void;
}) {
  const { t } = useTranslation();
  return (
    <Stack>
      <div className="command-run-meta">
        <Badge color={statusColor(record.status)} variant="light">
          {t(`commands.status.${record.status}`)}
        </Badge>
        <span>{record.command}</span>
        <span>{record.person || t("commands.defaultPerson")}</span>
      </div>
      <Tabs value={activeTab} onChange={onTabChange}>
        <Tabs.List>
          <Tabs.Tab value="events">{t("commands.events")}</Tabs.Tab>
          <Tabs.Tab value="output">{t("commands.output")}</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="events" pt="md">
          <CommandEventList items={items} />
        </Tabs.Panel>
        <Tabs.Panel value="output" pt="md">
          <pre className="command-output">
            {commandOutputText(record) || t("commands.noOutput")}
          </pre>
        </Tabs.Panel>
      </Tabs>
    </Stack>
  );
}

function CommandEventList({ items }: { items: CommandTimelineItem[] }) {
  const { t } = useTranslation();
  if (!items.length) {
    return <div className="empty-row">{t("commands.noRelatedEvents")}</div>;
  }
  return (
    <div className="event-list">
      {items.map((item, index) => (
        <div className="event-row" key={`${item.timestamp}-${item.label}-${index}`}>
          <span>{item.label}</span>
          <p>{item.message}</p>
        </div>
      ))}
    </div>
  );
}

export type CommandTimelineItem = {
  timestamp: string;
  label: string;
  message: string;
};

// Merge a run's command.* state-change events with its logs (both scoped by
// trace id) into one newest-first timeline for the Commands "events" tab.
export function buildCommandTimeline(
  events: RuntimeEvent[],
  logs: RuntimeLog[],
  traceId: string | null,
): CommandTimelineItem[] {
  if (!traceId) {
    return [];
  }
  const eventItems: CommandTimelineItem[] = events
    .filter((event) => event.type.startsWith("command.") && event.trace_id === traceId)
    .map((event) => ({
      timestamp: event.timestamp,
      label: event.type.replace("command.", ""),
      message: formatCommandEvent(event),
    }));
  const logItems: CommandTimelineItem[] = logs
    .filter((log) => log.trace_id === traceId)
    .map((log) => ({
      timestamp: log.timestamp,
      label: log.level,
      message: log.message,
    }));
  return [...eventItems, ...logItems].sort((a, b) => b.timestamp.localeCompare(a.timestamp));
}

export function buildCommandArgs(
  option: CommandOption | null,
  values: Record<string, string>,
  rawArgs: string,
): string[] {
  const args: string[] = [];
  if (option) {
    for (const argument of option.arguments) {
      const value = values[argument.name]?.trim();
      if (!value) {
        continue;
      }
      if (argument.kind === "positional") {
        args.push(value);
      } else {
        args.push(`${argument.name}=${value}`);
      }
    }
  }
  return [...args, ...splitCommandLine(rawArgs)];
}

export function splitCommandLine(value: string): string[] {
  const args: string[] = [];
  const pattern = /"([^"]*)"|'([^']*)'|(\S+)/g;
  for (const match of value.matchAll(pattern)) {
    args.push(match[1] ?? match[2] ?? match[3] ?? "");
  }
  return args.filter(Boolean);
}

function requirementLabel(
  t: TFunction,
  kind: CommandOption["requirements"][number]["kind"],
): string {
  return t(`commands.requirements.${kind}`);
}

export function upsertCommandRecord(
  records: CommandRunRecord[],
  next: CommandRunRecord,
): CommandRunRecord[] {
  const existing = records.find((record) => record.traceId === next.traceId);
  const merged = existing
    ? {
        ...existing,
        ...next,
        output: next.output ?? existing.output,
        error: next.error ?? existing.error,
        command: next.command || existing.command,
        person: next.person || existing.person,
      }
    : next;
  return [merged, ...records.filter((record) => record.traceId !== next.traceId)]
    .sort((a, b) => b.startedAt.localeCompare(a.startedAt))
    .slice(0, 20);
}

function statusColor(status: CommandRunRecord["status"]): string {
  if (status === "success") {
    return "green";
  }
  if (status === "failed") {
    return "red";
  }
  return "blue";
}

function stringPayload(value: unknown): string {
  return typeof value === "string" ? value : "";
}

export function commandFailureDetail(event: RuntimeEvent): string {
  return JSON.stringify(
    {
      trace_id: event.trace_id,
      type: event.type,
      payload: event.payload,
    },
    null,
    2,
  );
}

export function formatCommandEvent(event: RuntimeEvent): string {
  const { payload } = event;
  if (typeof payload.message === "string") {
    return payload.message;
  }
  if (typeof payload.command === "string") {
    return payload.command;
  }
  return event.trace_id ?? event.type;
}
