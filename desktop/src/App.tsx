import {
  ActionIcon,
  Alert,
  Anchor,
  Badge,
  Button,
  Card,
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
  FolderOpen,
  Play,
  RotateCcw,
  Settings,
  Square,
  Terminal,
  TriangleAlert,
  XCircle,
} from "lucide-react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";

import {
  type CommandOption,
  type DiagnosticCheck,
  type PromptTraceEntry,
  type RoutineOption,
  type RuntimeEvent,
  type RuntimeLog,
  type RuntimeUnitStatus,
  type StreamStatus,
  getConfigStatus,
  getCommandOptions,
  getProjectConfig,
  getPromptTrace,
  getSchedulerRoutines,
  getSchedulerStatus,
  getTeam,
  runCommand,
  runScenarioDiagnostics,
  startScheduler,
  stopScheduler,
  subscribeEvents,
  subscribeLogs,
  updatePromptTrace,
} from "./api/client";
import { type AppLanguage, normalizeLanguage, setAppLanguage } from "./i18n";
import { SetupPage } from "./setup/SetupPage";

const TICKET_ROUTINE = "workflows/ticket_driven_workflow";
const PROMPT_TRACE_LIMIT = 500;

type PromptTraceGroup = {
  id: string;
  kind: string;
  request: PromptTraceEntry | null;
  response: PromptTraceEntry | null;
  single: PromptTraceEntry | null;
  timestamp: string;
  personId: string;
  brain: string;
};

export function App() {
  const { t, i18n } = useTranslation();
  const appLanguage = normalizeLanguage(i18n.resolvedLanguage ?? i18n.language) ?? "en";
  return (
    <main className="shell">
      <aside className="sidebar">
        <div>
          <h1>GuildBotics</h1>
        </div>
        <nav className="nav">
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
          <Route element={<ServicePage />} path="/service" />
          <Route element={<Navigate replace to="/service" />} path="/overview" />
          <Route element={<CommandsPage />} path="/commands" />
          <Route element={<DiagnosticsPage />} path="/diagnostics" />
          <Route element={<SetupPage />} path="/setup" />
          <Route element={<Navigate replace to="/service" />} path="*" />
        </Routes>
      </section>
    </main>
  );
}

function ServicePage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [selectedRoutine, setSelectedRoutine] = useState("");
  const [schedulerEnabled, setSchedulerEnabled] = useState(true);
  const [eventsEnabled, setEventsEnabled] = useState(true);
  const [maxConsecutiveErrors, setMaxConsecutiveErrors] = useState(3);
  const [routineIntervalMinutes, setRoutineIntervalMinutes] = useState(10);
  const config = useQuery({ queryKey: ["config"], queryFn: getConfigStatus });
  const team = useQuery({ queryKey: ["team"], queryFn: getTeam, retry: false });
  const routines = useQuery({
    queryKey: ["scheduler-routines"],
    queryFn: getSchedulerRoutines,
    retry: false,
  });
  const scheduler = useQuery({
    queryKey: ["scheduler"],
    queryFn: getSchedulerStatus,
    refetchInterval: 5000,
  });
  const hasProjectConfig = Boolean(
    config.data?.primary_project_file_exists || config.data?.home_project_file_exists,
  );
  const projectConfig = useQuery({
    queryKey: ["project-config"],
    queryFn: getProjectConfig,
    enabled: hasProjectConfig,
    retry: false,
  });
  const githubEnabled = projectConfig.data?.github_enabled ?? false;

  useEffect(() => {
    if (!routines.data?.routines.length) {
      return;
    }
    if (!selectedRoutine) {
      const preferred =
        routines.data.routines.find((routine) => !routine.requires_github) ??
        routines.data.routines[0];
      setSelectedRoutine(preferred.command);
      return;
    }
    const exists = routines.data.routines.some((routine) => routine.command === selectedRoutine);
    if (!exists) {
      setSelectedRoutine(routines.data.routines[0].command);
    }
  }, [routines.data?.routines, selectedRoutine]);

  const selectedRoutineOption = useMemo(
    () =>
      routines.data?.routines.find((routine) => routine.command === selectedRoutine) ?? null,
    [routines.data?.routines, selectedRoutine],
  );
  const routineRequiresGithub = selectedRoutineOption?.requires_github ?? false;
  const canStartRoutine = !routineRequiresGithub || githubEnabled;

  const startMutation = useMutation({
    mutationFn: () => {
      const body: Record<string, unknown> = {
        max_consecutive_errors: maxConsecutiveErrors,
        routine_interval_minutes: routineIntervalMinutes,
      };
      if (schedulerEnabled && !eventsEnabled) {
        body.only = "scheduler";
      }
      if (!schedulerEnabled && eventsEnabled) {
        body.only = "events";
      }
      if (schedulerEnabled && selectedRoutine) {
        body.routine_commands = [selectedRoutine];
      }
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
  const noStartTarget = !schedulerEnabled && !eventsEnabled;
  const startNeedsRoutine = schedulerEnabled;
  const startBlockedByGithub = startNeedsRoutine && !canStartRoutine;
  const startDisabled =
    !hasProjectConfig || runtimeActive || startBlockedByGithub || noStartTarget;
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
              title={t("overview.schedulerCard.title")}
              description={t("overview.schedulerCard.description")}
              unit={scheduler.data?.scheduler}
              enabled={schedulerEnabled}
              disabled={runtimeActive}
              onEnabledChange={setSchedulerEnabled}
              rows={[
                [
                  t("overview.schedulerCard.workers"),
                  t("overview.schedulerCard.workerValue", {
                    workers: scheduler.data?.scheduler.worker_count ?? 0,
                    members: scheduler.data?.scheduler.active_member_count ?? activeMembers.length,
                  }),
                ],
              ]}
            >
              <Select
                label={t("overview.routine")}
                disabled={!schedulerEnabled || runtimeActive}
                value={selectedRoutine}
                onChange={(value) => setSelectedRoutine(value ?? "")}
                data={(routines.data?.routines ?? []).map((routine: RoutineOption) => ({
                  value: routine.command,
                  label: routine.requires_github
                    ? `${routineLabel(t, routine.command)} (${t("overview.requiresGithub")})`
                    : routineLabel(t, routine.command),
                }))}
              />
              <NumberInput
                label={t("overview.routineIntervalMinutes")}
                min={1}
                max={1440}
                step={1}
                allowDecimal={false}
                disabled={!schedulerEnabled || runtimeActive}
                value={routineIntervalMinutes}
                onChange={(value) => {
                  if (typeof value === "number") {
                    setRoutineIntervalMinutes(value);
                  }
                }}
              />
              <NumberInput
                label={t("overview.maxConsecutiveErrors")}
                min={1}
                max={20}
                step={1}
                allowDecimal={false}
                disabled={!schedulerEnabled || runtimeActive}
                value={maxConsecutiveErrors}
                onChange={(value) => {
                  if (typeof value === "number") {
                    setMaxConsecutiveErrors(value);
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
              enabled={eventsEnabled}
              disabled={runtimeActive}
              onEnabledChange={setEventsEnabled}
              rows={[
                [
                  t("overview.eventsCard.workflow"),
                  workflowLabel(t, scheduler.data?.events.workflow_command),
                ],
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
                    delivered: scheduler.data?.events.events_delivered_count ?? 0,
                    drained: scheduler.data?.events.events_drained_count ?? 0,
                    skipped: scheduler.data?.events.events_skipped_processed_count ?? 0,
                    failures: scheduler.data?.events.cycle_failure_count ?? 0,
                  }),
                ],
              ]}
            />
          </div>
          <PromptTraceOutputSettings />
          {startBlockedByGithub ? (
            <Alert color="yellow" title={t("overview.startGuardTitle")}>
              <Group justify="space-between" align="center">
                <Text size="sm">{t("overview.startGuardBody")}</Text>
                <Button component={NavLink} to="/setup" variant="light">
                  {t("overview.openSetup")}
                </Button>
              </Group>
            </Alert>
          ) : null}
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
  const [feedFilter, setFeedFilter] = useState("all");
  const [streamView, setStreamView] = useState<"events" | "logs">("events");
  const [eventStreamStatus, setEventStreamStatus] = useState<StreamStatus>("connecting");
  const [logStreamStatus, setLogStreamStatus] = useState<StreamStatus>("connecting");
  const [runtimeEvents, setRuntimeEvents] = useState<RuntimeEvent[]>([]);
  const [runtimeLogs, setRuntimeLogs] = useState<RuntimeLog[]>([]);
  const [readTracePath, setReadTracePath] = useState("");
  const [readTracePathEdited, setReadTracePathEdited] = useState(false);
  const [loadedTracePath, setLoadedTracePath] = useState("");
  const canPickFile = isTauriRuntime();
  const config = useQuery({ queryKey: ["config"], queryFn: getConfigStatus });
  const team = useQuery({ queryKey: ["team"], queryFn: getTeam, retry: false });
  const promptTrace = useQuery({
    queryKey: ["prompt-trace", loadedTracePath],
    queryFn: () =>
      getPromptTrace(PROMPT_TRACE_LIMIT, loadedTracePath.trim() || undefined),
    refetchInterval: 5000,
  });
  const hasProjectConfig = Boolean(
    config.data?.primary_project_file_exists || config.data?.home_project_file_exists,
  );
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
  const applyReadTracePath = (tracePath: string = readTracePath) => {
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
      readTracePath || promptTrace.data?.default_trace_file || "",
    );
    if (!selected) {
      return;
    }
    setReadTracePath(selected);
    setLoadedTracePath(selected);
    setReadTracePathEdited(false);
  };
  useEffect(() => {
    const stopEvents = subscribeEvents(
      (event) => {
        setRuntimeEvents((current) => [event, ...current].slice(0, 80));
      },
      setEventStreamStatus,
    );
    const stopLogs = subscribeLogs(
      (log) => {
        setRuntimeLogs((current) => [log, ...current].slice(0, 80));
      },
      setLogStreamStatus,
    );
    return () => {
      stopEvents();
      stopLogs();
    };
  }, []);
  useEffect(() => {
    if (!promptTrace.data) {
      return;
    }
    if (!readTracePathEdited) {
      setReadTracePath(promptTrace.data.trace_file);
    }
    if (!loadedTracePath) {
      setLoadedTracePath(promptTrace.data.trace_file);
    }
  }, [loadedTracePath, promptTrace.data, readTracePathEdited]);
  const filteredEvents = useMemo(
    () => runtimeEvents.filter((event) => matchesFeedFilter(event, feedFilter)),
    [feedFilter, runtimeEvents],
  );
  const filteredLogs = useMemo(
    () => runtimeLogs.filter((log) => matchesLogFilter(log, feedFilter)),
    [feedFilter, runtimeLogs],
  );

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
          <Tabs.Tab value="promptTrace">{t("diagnostics.tabs.promptTrace")}</Tabs.Tab>
          <Tabs.Tab value="runtimeStream">{t("diagnostics.tabs.runtimeStream")}</Tabs.Tab>
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
                <Text c="dimmed" size="sm">{t("overview.promptTrace.description")}</Text>
              </div>
            </Group>
            <div className="trace-settings">
              <TracePathField
                label={t("overview.promptTrace.readPath")}
                value={readTracePath}
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
            <Group className="prompt-trace-counts" gap="xs">
              <Text size="sm">
                <b>{t("overview.promptTrace.eventCount")}</b>{" "}
                {promptTrace.data?.event_count ?? 0}
              </Text>
              <Text c="dimmed" size="sm">/</Text>
              <Text size="sm">
                <b>{t("overview.promptTrace.displayedCount")}</b>{" "}
                {t("overview.promptTrace.displayedCountValue", {
                  count: promptTrace.data?.events.length ?? 0,
                  limit: PROMPT_TRACE_LIMIT,
                })}
              </Text>
            </Group>
            <PromptTraceList entries={promptTrace.data?.events ?? []} />
          </Card>
        </Tabs.Panel>
        <Tabs.Panel className="diagnostics-fill-panel" value="runtimeStream" pt="md">
          <Card className="diagnostics-fill-card stream-fill-card" withBorder radius="md" p="lg">
            <Group justify="space-between" align="flex-start">
              <div>
                <Title order={3}>{t("overview.runtimeFeed")}</Title>
                <Text c="dimmed" size="sm">{t("overview.runtimeFeedDescription")}</Text>
              </div>
              <SegmentedControl
                value={feedFilter}
                onChange={setFeedFilter}
                data={[
                  { value: "all", label: t("overview.feedFilters.all") },
                  { value: "error", label: t("overview.feedFilters.error") },
                  { value: "command", label: t("overview.feedFilters.command") },
                  { value: "scheduler", label: t("overview.feedFilters.scheduler") },
                  { value: "events", label: t("overview.feedFilters.events") },
                ]}
              />
            </Group>
            <SegmentedControl
              mt="md"
              value={streamView}
              onChange={(value) => setStreamView(value as "events" | "logs")}
              data={[
                {
                  value: "events",
                  label: (
                    <>
                      {t("overview.events")} <StreamBadge status={eventStreamStatus} />
                    </>
                  ),
                },
                {
                  value: "logs",
                  label: (
                    <>
                      {t("overview.logs")} <StreamBadge status={logStreamStatus} />
                    </>
                  ),
                },
              ]}
            />
            <div className="runtime-stream-list">
              <div className="runtime-stream-header">
                <span>{t("overview.runtimeStreamColumns.time")}</span>
                <span>{t("overview.runtimeStreamColumns.type")}</span>
                <span>{t("overview.runtimeStreamColumns.request")}</span>
                <span>{t("overview.runtimeStreamColumns.message")}</span>
              </div>
              {streamView === "events" ? (
                <>
                  {filteredEvents.length === 0 ? (
                    <div className="empty-row runtime-stream-empty">{t("overview.emptyEvents")}</div>
                  ) : null}
                  {filteredEvents.map((event, index) => (
                    <div className="runtime-stream-row" key={`${event.timestamp}-${event.type}-${index}`}>
                      <span>{formatTime(event.timestamp)}</span>
                      <span>
                        <Badge color={eventBadgeColor(event.type)} variant="light">
                          {eventTypeLabel(t, event.type)}
                        </Badge>
                      </span>
                      <span className="runtime-stream-request">
                        {event.request_id ? event.request_id.slice(0, 10) : "-"}
                      </span>
                      <span title={formatRuntimeEvent(t, event)}>
                        {formatRuntimeEvent(t, event)}
                      </span>
                    </div>
                  ))}
                </>
              ) : (
                <>
                  {filteredLogs.length === 0 ? (
                    <div className="empty-row runtime-stream-empty">{t("overview.emptyLogs")}</div>
                  ) : null}
                  {filteredLogs.map((log, index) => (
                    <div className="runtime-stream-row" key={`${log.timestamp}-${log.level}-${index}`}>
                      <span>{formatTime(log.timestamp)}</span>
                      <span>
                        <Badge color={logBadgeColor(log.level)} variant="light">
                          {log.level}
                        </Badge>
                      </span>
                      <span className="runtime-stream-request">
                        {log.request_id ? log.request_id.slice(0, 10) : "-"}
                      </span>
                      <span title={log.message}>{log.message}</span>
                    </div>
                  ))}
                </>
              )}
            </div>
          </Card>
        </Tabs.Panel>
      </Tabs>
    </Stack>
  );
}

function ServiceRuntimeSection({
  title,
  description,
  unit,
  enabled,
  disabled,
  onEnabledChange,
  rows,
  children,
}: {
  title: string;
  description: string;
  unit: RuntimeUnitStatus | undefined;
  enabled: boolean;
  disabled: boolean;
  onEnabledChange: (enabled: boolean) => void;
  rows: Array<[string, string]>;
  children?: ReactNode;
}) {
  const { t } = useTranslation();
  const status = unit?.state ?? "stopped";
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
          <Switch
            className="service-unit-switch"
            checked={enabled}
            disabled={disabled}
            label={t("service.startTarget")}
            onChange={(event) => onEnabledChange(event.currentTarget.checked)}
          />
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
      updatePromptTrace(
        { enabled, trace_path: outputTracePath.trim() },
        PROMPT_TRACE_LIMIT,
      ),
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
  useEffect(() => {
    if (promptTrace.data && !outputTracePathEdited) {
      setOutputTracePath(promptTrace.data.output_trace_file);
    }
  }, [outputTracePathEdited, promptTrace.data]);
  const applyOutputTracePath = (tracePath: string = outputTracePath) => {
    const normalizedPath = tracePath.trim();
    if (
      !outputTracePathEdited &&
      normalizedPath === (promptTrace.data?.output_trace_file ?? "")
    ) {
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
      outputTracePath || promptTrace.data?.default_trace_file || "",
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
          onChange={(event) =>
            promptTraceMutation.mutate(event.currentTarget.checked)
          }
        />
      </Group>
      <div className="trace-settings">
        <TracePathField
          label={t("overview.promptTrace.outputPath")}
          value={outputTracePath}
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
      {promptTraceMutation.error ||
      promptTraceOutputPathMutation.error ? (
        <Alert color="red" title={t("overview.promptTrace.saveError")}>
          {(
            promptTraceMutation.error ??
            promptTraceOutputPathMutation.error
          )?.message}
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
              label={
                canPickFile
                  ? pickLabel
                  : t("overview.promptTrace.filePickerUnavailable")
              }
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
          {applying
            ? t("overview.promptTrace.pathApplying")
            : t("overview.promptTrace.pathEdited")}
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
  useEffect(() => {
    if (groups.length === 0) {
      setSelectedId("");
      return;
    }
    if (!groups.some((group) => group.id === selectedId)) {
      setSelectedId(groups[0].id);
    }
  }, [groups, selectedId]);
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
            <span>{group.personId || "-"}</span>
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
      ? decodeTraceText(request?.transcript || response?.transcript || group.single?.transcript || "")
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
      <Group justify="space-between" align="flex-start">
        <div>
          <Text fw={700}>{traceKindLabel(t, group.kind)} / {traceBrainLabel(group.brain)}</Text>
          <Text c="dimmed" size="xs">
            {group.personId || "-"} · {group.timestamp ? formatDateTime(group.timestamp) : "-"}
          </Text>
        </div>
        <Badge color={traceKindColor(group.kind)} variant="light">
          {group.request && group.response
            ? t("overview.promptTrace.requestResponse")
            : t("overview.promptTrace.singleEvent")}
        </Badge>
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
            <Text fw={700} size="xs">{contextLabel}</Text>
            <pre>{contextText}</pre>
          </div>
        ) : null}
        <div className="trace-preview">
          <Text fw={700} size="xs">{t("overview.promptTrace.prompt")}</Text>
          <pre>{requestText || t("overview.promptTrace.noRequest")}</pre>
        </div>
        <div className="trace-preview">
          <Text fw={700} size="xs">{t("overview.promptTrace.response")}</Text>
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
  const color = state === "running" ? "teal" : state === "failed" ? "red" : state === "stopped" ? "gray" : "orange";
  return (
    <Badge color={color} variant="light">
      {t(`overview.runtimeStates.${state}`)}
    </Badge>
  );
}

function isStopTimeoutPending(unit: RuntimeUnitStatus | undefined) {
  return Boolean(
    unit?.running &&
      unit.state === "failed" &&
      unit.error?.includes("did not stop before timeout"),
  );
}

function StreamBadge({ status }: { status: StreamStatus }) {
  const { t } = useTranslation();
  const color = status === "connected" ? "teal" : status === "error" ? "red" : "gray";
  return (
    <Badge color={color} ml={6} size="xs" variant="light">
      {t(`overview.streamStates.${status}`)}
    </Badge>
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
          <Text size="sm">
            {diagnosticDescription(t, check)}
          </Text>
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

function routineLabel(t: TFunction, command: string | undefined) {
  if (!command) {
    return t("overview.none");
  }
  if (command === TICKET_ROUTINE) {
    return t("overview.routines.ticketDriven");
  }
  return command;
}

function workflowLabel(t: TFunction, command: string | null | undefined) {
  if (!command) {
    return t("overview.workflows.chatConversation");
  }
  if (command === "workflows/chat_conversation_workflow") {
    return t("overview.workflows.chatConversation");
  }
  return command;
}

function traceEventLabel(t: TFunction, event: string) {
  return t(`overview.promptTrace.events.${event.replace(/\./g, "_")}`, {
    defaultValue: event,
  });
}

function traceBadgeColor(event: string) {
  if (event.endsWith(".response")) {
    return "teal";
  }
  if (event.includes("parse_error")) {
    return "red";
  }
  if (event.includes("chat")) {
    return "indigo";
  }
  return "blue";
}

function buildTraceGroups(entries: PromptTraceEntry[]): PromptTraceGroup[] {
  const used = new Set<number>();
  const groups: PromptTraceGroup[] = [];
  entries.forEach((entry, index) => {
    if (used.has(index)) {
      return;
    }
    if (isTraceResponse(entry)) {
      const requestIndex = entries.findIndex(
        (candidate, candidateIndex) =>
          candidateIndex > index &&
          !used.has(candidateIndex) &&
          isTraceRequest(candidate) &&
          tracePairKey(candidate) === tracePairKey(entry),
      );
      const request = requestIndex >= 0 ? entries[requestIndex] : null;
      used.add(index);
      if (requestIndex >= 0) {
        used.add(requestIndex);
      }
      groups.push(traceGroup({ request, response: entry, single: null, index }));
      return;
    }
    used.add(index);
    groups.push(
      traceGroup({
        request: isTraceRequest(entry) ? entry : null,
        response: null,
        single: isTraceRequest(entry) ? null : entry,
        index,
      }),
    );
  });
  return groups;
}

function traceGroup({
  request,
  response,
  single,
  index,
}: {
  request: PromptTraceEntry | null;
  response: PromptTraceEntry | null;
  single: PromptTraceEntry | null;
  index: number;
}): PromptTraceGroup {
  const representative = response ?? request ?? single;
  return {
    id: `${representative?.timestamp ?? ""}-${representative?.event ?? ""}-${index}`,
    kind: traceKind(representative),
    request,
    response,
    single,
    timestamp: representative?.timestamp ?? "",
    personId: representative?.person_id ?? "",
    brain: representative?.brain ?? "",
  };
}

function isTraceRequest(entry: PromptTraceEntry) {
  return entry.event.endsWith(".request");
}

function isTraceResponse(entry: PromptTraceEntry) {
  return entry.event.endsWith(".response");
}

function tracePairKey(entry: PromptTraceEntry) {
  return [
    traceKind(entry),
    entry.person_id,
    entry.brain,
    traceFieldValue(entry, "model"),
    traceFieldValue(entry, "cli_agent"),
  ].join("|");
}

function traceKind(entry: PromptTraceEntry | null | undefined) {
  if (!entry) {
    return "trace";
  }
  if (entry.event.startsWith("llm.")) {
    return "llm";
  }
  if (entry.event.startsWith("cli_agent.")) {
    return "cli";
  }
  if (entry.event.startsWith("chat.")) {
    return "chat";
  }
  return "trace";
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

function traceBrainLabel(brain: string) {
  return brain.split("/").pop()?.replace(/\.[^.]+$/, "") || "-";
}

function traceFieldValue(entry: PromptTraceEntry, key: string) {
  const value = entry.fields[key];
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return "";
}

function traceGroupMetadata(group: PromptTraceGroup): Array<[string, string]> {
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

function traceFieldRows(entry: PromptTraceEntry): Array<[string, string]> {
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

function decodeTraceText(value: string) {
  return value
    .replace(/\\u([0-9a-fA-F]{4})/g, (_, hex: string) =>
      String.fromCharCode(Number.parseInt(hex, 16)),
    )
    .replace(/\\n/g, "\n")
    .replace(/\\t/g, "\t");
}

function formatDateTime(value: string | null | undefined) {
  if (!value) {
    return "-";
  }
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "short",
    timeStyle: "medium",
  }).format(new Date(value));
}

function formatTime(value: string) {
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

function matchesFeedFilter(event: RuntimeEvent, filter: string) {
  if (filter === "all") {
    return true;
  }
  if (filter === "error") {
    return event.type.endsWith(".failed") || event.type.includes("error");
  }
  if (filter === "command") {
    return event.type.startsWith("command.");
  }
  if (filter === "scheduler") {
    return event.type.startsWith("scheduler.");
  }
  if (filter === "events") {
    return event.type.startsWith("events.");
  }
  return true;
}

function matchesLogFilter(log: RuntimeLog, filter: string) {
  if (filter === "all") {
    return true;
  }
  if (filter === "error") {
    return ["ERROR", "CRITICAL", "WARNING"].includes(log.level.toUpperCase());
  }
  if (filter === "command") {
    return Boolean(log.request_id);
  }
  return log.message.toLowerCase().includes(filter);
}

function eventTypeLabel(t: TFunction, type: string) {
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

function eventBadgeColor(type: string) {
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

function logBadgeColor(level: string) {
  const upper = level.toUpperCase();
  if (upper === "ERROR" || upper === "CRITICAL") {
    return "red";
  }
  if (upper === "WARNING") {
    return "orange";
  }
  return "gray";
}

function formatRuntimeEvent(t: TFunction, event: RuntimeEvent): string {
  const { payload } = event;
  if (typeof payload.message === "string") {
    return payload.message;
  }
  if (typeof payload.command === "string") {
    return t("overview.eventSummaries.command", { command: payload.command });
  }
  if (typeof payload.error === "string") {
    return payload.error;
  }
  if (event.type === "scheduler.running") {
    return t("overview.eventSummaries.schedulerRunning");
  }
  if (event.type === "scheduler.stopped") {
    return t("overview.eventSummaries.schedulerStopped");
  }
  if (event.type === "events.running") {
    return t("overview.eventSummaries.eventsRunning");
  }
  if (event.type === "events.stopped") {
    return t("overview.eventSummaries.eventsStopped");
  }
  if (event.type.endsWith(".failed")) {
    return t("overview.eventSummaries.failed");
  }
  return event.type;
}

function isTauriRuntime() {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

async function openLocalFile(path: string) {
  if (!isTauriRuntime()) {
    return;
  }
  const { open } = await import("@tauri-apps/plugin-shell");
  await open(path);
}

function localFileHref(path: string) {
  const normalizedPath = path.replace(/\\/g, "/");
  const prefix = normalizedPath.startsWith("/") ? "file://" : "file:///";
  return encodeURI(`${prefix}${normalizedPath}`);
}

async function selectTraceFile(mode: "open" | "save", currentPath: string) {
  if (!isTauriRuntime()) {
    return null;
  }
  const selected =
    mode === "open"
      ? await (await import("@tauri-apps/plugin-dialog")).open({
          defaultPath: currentPath || undefined,
          directory: false,
          multiple: false,
          title: "Prompt trace file",
        })
      : await (await import("@tauri-apps/plugin-dialog")).save({
          defaultPath: currentPath || undefined,
          title: "Prompt trace file",
        });
  return typeof selected === "string" ? selected : null;
}

function CommandsPage() {
  const { t } = useTranslation();
  const config = useQuery({ queryKey: ["config"], queryFn: getConfigStatus });
  const team = useQuery({ queryKey: ["team"], queryFn: getTeam, retry: false });
  const hasProjectConfig = Boolean(
    config.data?.primary_project_file_exists || config.data?.home_project_file_exists,
  );
  const [mode, setMode] = useState("catalog");
  const [selectedCommand, setSelectedCommand] = useState("");
  const [customCommand, setCustomCommand] = useState("");
  const [rawArgs, setRawArgs] = useState("");
  const [argValues, setArgValues] = useState<Record<string, string>>({});
  const [message, setMessage] = useState("");
  const [person, setPerson] = useState<string | null>(null);
  const [cwd, setCwd] = useState("");
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [runtimeEvents, setRuntimeEvents] = useState<RuntimeEvent[]>([]);
  const [runtimeLogs, setRuntimeLogs] = useState<RuntimeLog[]>([]);
  const [history, setHistory] = useState<CommandRunRecord[]>([]);
  const [activeRequestId, setActiveRequestId] = useState<string | null>(null);
  const commandOptions = useQuery({
    queryKey: ["command-options", person],
    queryFn: () => getCommandOptions(person || undefined),
    enabled: hasProjectConfig,
    retry: false,
  });
  const commandCatalog = commandOptions.data?.options ?? [];

  useEffect(() => {
    if (selectedCommand || !commandCatalog.length) {
      return;
    }
    const runnable =
      commandCatalog.find((option) =>
        option.requirements.every((requirement) => requirement.satisfied),
      ) ?? commandCatalog[0];
    setSelectedCommand(runnable.command);
  }, [commandCatalog, selectedCommand]);

  const selectedOption = useMemo(
    () => commandCatalog.find((option) => option.command === selectedCommand) ?? null,
    [commandCatalog, selectedCommand],
  );
  const commandOptionByValue = useMemo(
    () => new Map(commandCatalog.map((option) => [option.command, option])),
    [commandCatalog],
  );
  const command = mode === "catalog" ? selectedCommand : customCommand.trim();
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
  useEffect(() => {
    if (!activeMembers.length) {
      setPerson(null);
      return;
    }
    if (!person || !activeMembers.some((member) => member.person_id === person)) {
      setPerson(activeMembers[0].person_id);
    }
  }, [activeMembers, person]);
  const runDisabled =
    !hasProjectConfig ||
    !command ||
    !person ||
    activeMembers.length === 0 ||
    blockingRequirements.length > 0;

  const runMutation = useMutation({
    mutationFn: () =>
      runCommand({
        command,
        args: commandArgs,
        message,
        person: person ?? undefined,
        cwd: cwd.trim() || undefined,
    }),
    onMutate: () => {
      setActiveRequestId(null);
    },
    onSuccess: (response) => {
      setActiveRequestId(response.request_id);
      setHistory((current) =>
        upsertCommandRecord(current, {
          requestId: response.request_id,
          person: person ?? "",
          command,
          startedAt: new Date().toISOString(),
          status: "success",
          output: response.output,
        }),
      );
    },
    onError: (error) => {
      const requestId = activeRequestId ?? `local-${Date.now()}`;
      setActiveRequestId(requestId);
      setHistory((current) =>
        upsertCommandRecord(current, {
          requestId,
          person: person || "",
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
    const stopEvents = subscribeEvents((event) => {
      if (!event.type.startsWith("command.")) {
        return;
      }
      setRuntimeEvents((current) => [event, ...current].slice(0, 80));
      if (!event.request_id) {
        return;
      }
      if (event.type === "command.started") {
        setActiveRequestId(event.request_id);
        setHistory((current) =>
          upsertCommandRecord(current, {
            requestId: event.request_id ?? "",
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
            requestId: event.request_id ?? "",
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
            requestId: event.request_id ?? "",
            person: stringPayload(event.payload.person),
            command: stringPayload(event.payload.command),
            startedAt: event.timestamp,
            status: "success",
          }),
        );
      }
    });
    const stopLogs = subscribeLogs((log) => {
      setRuntimeLogs((current) => [log, ...current].slice(0, 80));
    });
    return () => {
      stopEvents();
      stopLogs();
    };
  }, [t]);

  const selectedRecord = useMemo(
    () =>
      history.find((record) => record.requestId === activeRequestId) ??
      history[0] ??
      null,
    [activeRequestId, history],
  );
  const visibleRequestId = selectedRecord?.requestId ?? activeRequestId;
  const commandEvents = useMemo(
    () =>
      runtimeEvents.filter(
        (event) => event.type.startsWith("command.") && event.request_id === visibleRequestId,
      ),
    [runtimeEvents, visibleRequestId],
  );
  const commandLogs = useMemo(
    () => runtimeLogs.filter((log) => log.request_id === visibleRequestId),
    [runtimeLogs, visibleRequestId],
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
          {blockingRequirements.map((requirement) => requirementLabel(t, requirement.kind)).join(", ")}
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
                      value={selectedCommand}
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
                    <TextInput
                      aria-label={t("commands.command")}
                      value={customCommand}
                      onChange={(event) => setCustomCommand(event.currentTarget.value)}
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
                  value={person}
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
                        ? t("commands.currentRunBody", { requestId: selectedRecord.requestId })
                        : t("commands.noRunSelected")}
                    </Text>
                  </div>
                  {selectedRecord ? (
                    <CommandRunDetails
                      record={selectedRecord}
                      events={commandEvents}
                      logs={commandLogs}
                    />
                  ) : (
                    <div className="empty-row">{t("commands.noRunsYet")}</div>
                  )}
                </Stack>
              </div>

            </Stack>
          </div>

          <PromptTraceOutputSettings />
        </Stack>
      </Card>
    </Stack>
  );
}

type CommandRunRecord = {
  requestId: string;
  person: string;
  command: string;
  startedAt: string;
  status: "running" | "success" | "failed";
  output?: string;
  error?: string;
};

function CommandRunDetails({
  record,
  events,
  logs,
}: {
  record: CommandRunRecord;
  events: RuntimeEvent[];
  logs: RuntimeLog[];
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
      <Tabs defaultValue="output">
        <Tabs.List>
          <Tabs.Tab value="output">{t("commands.output")}</Tabs.Tab>
          <Tabs.Tab value="events">{t("commands.events")}</Tabs.Tab>
          <Tabs.Tab value="logs">{t("commands.logs")}</Tabs.Tab>
          <Tabs.Tab value="details">{t("commands.details")}</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="output" pt="md">
          <pre className="command-output">{record.output || t("commands.noOutput")}</pre>
        </Tabs.Panel>
        <Tabs.Panel value="events" pt="md">
          <CommandEventList events={events} />
        </Tabs.Panel>
        <Tabs.Panel value="logs" pt="md">
          <CommandLogList logs={logs} />
        </Tabs.Panel>
        <Tabs.Panel value="details" pt="md">
          <pre className="command-output">
            {record.error || JSON.stringify({ request_id: record.requestId }, null, 2)}
          </pre>
        </Tabs.Panel>
      </Tabs>
    </Stack>
  );
}

function CommandEventList({ events }: { events: RuntimeEvent[] }) {
  const { t } = useTranslation();
  if (!events.length) {
    return <div className="empty-row">{t("commands.noRelatedEvents")}</div>;
  }
  return (
    <div className="event-list">
      {events.map((event, index) => (
        <div className="event-row" key={`${event.timestamp}-${event.type}-${index}`}>
          <span>{event.type.replace("command.", "")}</span>
          <p>{formatCommandEvent(event)}</p>
        </div>
      ))}
    </div>
  );
}

function CommandLogList({ logs }: { logs: RuntimeLog[] }) {
  const { t } = useTranslation();
  if (!logs.length) {
    return <div className="empty-row">{t("commands.noRelatedLogs")}</div>;
  }
  return (
    <div className="event-list">
      {logs.map((log, index) => (
        <div className="event-row" key={`${log.timestamp}-${log.level}-${index}`}>
          <span>{log.level}</span>
          <p>{log.message}</p>
        </div>
      ))}
    </div>
  );
}

function buildCommandArgs(
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

function splitCommandLine(value: string): string[] {
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

function upsertCommandRecord(
  records: CommandRunRecord[],
  next: CommandRunRecord,
): CommandRunRecord[] {
  const existing = records.find((record) => record.requestId === next.requestId);
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
  return [merged, ...records.filter((record) => record.requestId !== next.requestId)]
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

function commandFailureDetail(event: RuntimeEvent): string {
  return JSON.stringify(
    {
      request_id: event.request_id,
      type: event.type,
      payload: event.payload,
    },
    null,
    2,
  );
}

function formatCommandEvent(event: RuntimeEvent): string {
  const { payload } = event;
  if (typeof payload.message === "string") {
    return payload.message;
  }
  if (typeof payload.command === "string") {
    return payload.command;
  }
  return event.request_id ?? event.type;
}
