import {
  ActionIcon,
  Alert,
  Anchor,
  Autocomplete,
  Avatar,
  Badge,
  Button,
  Card,
  CopyButton,
  Divider,
  Drawer,
  Group,
  Loader,
  Modal,
  NumberInput,
  SegmentedControl,
  Select,
  Stack,
  Switch,
  Tabs,
  Text,
  Textarea,
  TextInput,
  Title,
  Tooltip,
} from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { TFunction } from "i18next";
import {
  Activity,
  CheckCircle2,
  Copy,
  ExternalLink,
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
import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Navigate, NavLink, Route, Routes, useSearchParams } from "react-router-dom";

import { ActivityHistoryPage } from "./activity/ActivityHistory";
import {
  getCommandOptions,
  getConfigStatus,
  getGlobalRecords,
  getMemoryEvents,
  getProjectConfig,
  getRuntimeDebug,
  getSchedulerStatus,
  getSystemAlerts,
  getTeam,
  getTraceDetail,
  getTraces,
  getTranscriptSettings,
  dismissSystemAlert,
  memberAvatarUrl,
  resetChatReceiveState,
  runCommand,
  runScenarioDiagnostics,
  startScheduler,
  stopScheduler,
  subscribeEvents,
  subscribeLogs,
  updateRuntimeDebug,
  updateTranscriptSettings,
  verify as verifyConfiguration,
  type ChatReceiveResetResponse,
  type CommandOption,
  type DiagnosticCheck,
  type MemoryEvent,
  type RuntimeActiveWork,
  type RuntimeEvent,
  type RuntimeLog,
  type RuntimeStatus,
  type RuntimeUnitStatus,
  type SchedulerStartRequest,
  type SystemAlert,
  type TraceDetailResponse,
  type TraceRecord,
  type TraceSummary,
  type TranscriptSettingsStatus,
} from "./api/client";
import { normalizeLanguage, setAppLanguage, type AppLanguage } from "./i18n";
import { SetupPage } from "./setup/SetupPage";
import { isTerminalTraceStatus, traceStatusColor } from "./traceStatus";
const EXECUTION_LIMIT = 200;
const MEMORY_EVENT_LIMIT = 500;
const MEMORY_FILTER_ALL = "__all__";
type DiagnosticsTab = "readiness" | "executions" | "memory" | "settings";
const DIAGNOSTICS_TABS = new Set<DiagnosticsTab>(["readiness", "executions", "memory", "settings"]);
type NavRuntimeState = "running" | "stopping" | "stopped";

type MemoryEventFocus = {
  docId: string;
  traceId: string;
  timestamp: string;
  action: string;
  personId: string;
};
const MEMORY_FOCUS_SEARCH_PARAMS = [
  "memory_trace_id",
  "doc_id",
  "timestamp",
  "action",
  "person_id",
] as const;

export function App() {
  const { t, i18n } = useTranslation();
  const appLanguage = normalizeLanguage(i18n.resolvedLanguage ?? i18n.language) ?? "en";
  const config = useQuery({ queryKey: ["config"], queryFn: getConfigStatus });
  const configured = Boolean(config.data?.project_file_exists);
  const runtimeStatus = useQuery({
    queryKey: ["scheduler"],
    queryFn: getSchedulerStatus,
    enabled: configured,
    refetchInterval: 5000,
  });
  const serviceNavState = serviceRuntimeNavState(runtimeStatus.data);
  const commandNavState = commandRuntimeNavState(runtimeStatus.data);
  const closeGuard = useAppCloseGuard();
  return (
    <main className="shell">
      <AppCloseBlockedModal
        error={closeGuard.forceQuitError}
        forceQuitting={closeGuard.forceQuitting}
        opened={closeGuard.blocked}
        onCancel={closeGuard.cancel}
        onForceQuit={() => void closeGuard.forceStopAndQuit()}
      />
      <aside className="sidebar" style={{ position: "relative" }}>
        <div
          data-tauri-drag-region
          style={{
            position: "absolute",
            top: 0,
            left: 0,
            width: "100%",
            height: "44px",
            zIndex: 10,
            cursor: "default",
          }}
        />

        <nav className="nav">
          {configured ? (
            <NavLink className="nav-item" to="/activity">
              <History size={18} /> {t("app.nav.activity")}
            </NavLink>
          ) : null}
          <NavLink className="nav-item" to="/service">
            <Activity size={18} />
            <span className="nav-item-label">{t("app.nav.service")}</span>
            <NavStatusIndicator
              label={t(`app.navStatus.service.${serviceNavState}`)}
              state={serviceNavState}
            />
          </NavLink>
          <NavLink className="nav-item" to="/commands">
            <Terminal size={18} />
            <span className="nav-item-label">{t("app.nav.commands")}</span>
            <NavStatusIndicator
              label={t(`app.navStatus.commands.${commandNavState}`)}
              state={commandNavState}
            />
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

      <section className="workspace" style={{ position: "relative" }}>
        <div
          data-tauri-drag-region
          style={{
            position: "absolute",
            top: 0,
            left: 0,
            width: "100%",
            height: "24px",
            zIndex: 10,
            cursor: "default",
          }}
        />
        {configured ? <SystemAlertBand /> : null}
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

function SystemAlertBand() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const alerts = useQuery({
    queryKey: ["system-alerts"],
    queryFn: getSystemAlerts,
    refetchInterval: 5000,
  });
  const diagnostics = useMutation({
    mutationFn: async () => {
      await verifyConfiguration();
      return runScenarioDiagnostics();
    },
    onSettled: async () => {
      await queryClient.invalidateQueries({ queryKey: ["system-alerts"] });
    },
  });
  const dismiss = useMutation({
    mutationFn: (alertId: string) => dismissSystemAlert(alertId),
    onSuccess: (response) => {
      queryClient.setQueryData(["system-alerts"], response);
    },
  });
  if (!alerts.data?.alerts.length) {
    return null;
  }
  return (
    <Stack
      aria-label={t("systemAlerts.region")}
      className="system-alert-band"
      gap="xs"
      role="region"
    >
      {alerts.data.alerts.map((alert) => (
        <Alert
          color={alert.severity === "critical" ? "danger" : "warning"}
          icon={<TriangleAlert size={18} />}
          key={alert.id}
          title={t(`systemAlerts.severity.${alert.severity}`)}
          withCloseButton
          onClose={() => {
            if (dismiss.isPending && dismiss.variables === alert.id) {
              return;
            }
            dismiss.mutate(alert.id);
          }}
          closeButtonLabel={t("systemAlerts.actions.dismiss")}
        >
          <Group align="center" gap="md" wrap="wrap">
            <Text size="sm">{systemAlertMessage(t, alert)}</Text>
            {alert.actions.length > 0 && (
              <Group gap="xs">
                {alert.actions.includes("diagnostics") ? (
                  <Group gap="xs" align="center" style={{ display: "inline-flex" }}>
                    <Anchor
                      component="button"
                      type="button"
                      disabled={diagnostics.isPending}
                      onClick={() => diagnostics.mutate()}
                      size="sm"
                      underline="hover"
                      style={{
                        border: "none",
                        background: "none",
                        padding: 0,
                        cursor: diagnostics.isPending ? "not-allowed" : "pointer",
                        opacity: diagnostics.isPending ? 0.6 : 1,
                        color: "inherit",
                        font: "inherit",
                      }}
                    >
                      {t("systemAlerts.actions.diagnostics")}
                    </Anchor>
                    {diagnostics.isPending && <Loader size="xs" color="currentColor" />}
                  </Group>
                ) : null}
                {alert.actions.includes("setup") ? (
                  <Anchor
                    component={NavLink}
                    size="sm"
                    to={systemAlertSetupTarget(alert)}
                    underline="hover"
                  >
                    {t("systemAlerts.actions.setup")}
                  </Anchor>
                ) : null}
                {alert.actions.includes("service") ? (
                  <Anchor component={NavLink} size="sm" to="/service" underline="hover">
                    {t("systemAlerts.actions.service")}
                  </Anchor>
                ) : null}
                {alert.actions.includes("trace") && alert.trace_id ? (
                  <Anchor
                    component={NavLink}
                    size="sm"
                    to={`/diagnostics?tab=executions&trace_id=${encodeURIComponent(alert.trace_id)}`}
                    underline="hover"
                  >
                    {t("systemAlerts.actions.trace")}
                  </Anchor>
                ) : null}
              </Group>
            )}
          </Group>
        </Alert>
      ))}
    </Stack>
  );
}

function systemAlertMessage(t: TFunction, alert: SystemAlert) {
  return t(`systemAlerts.codes.${alert.code}`, {
    person: alert.person_id || t("systemAlerts.unknownMember"),
    command: alert.command || t("systemAlerts.unknownCommand"),
    count: alert.occurrence_count,
  });
}

export function systemAlertSetupTarget(alert: SystemAlert): string {
  if (alert.code !== "credential_github" || !alert.person_id) {
    return "/setup";
  }
  const search = new URLSearchParams({
    section: "members",
    person_id: alert.person_id,
    tab: "github",
  });
  return `/setup?${search.toString()}`;
}

function NavStatusIndicator({ state, label }: { state: NavRuntimeState; label: string }) {
  if (state === "stopped") {
    return null;
  }
  return (
    <span
      aria-label={label}
      className={`nav-status-indicator ${state}`}
      role="status"
      title={label}
    />
  );
}

function serviceRuntimeNavState(status: RuntimeStatus | undefined): NavRuntimeState {
  if (!status) {
    return "stopped";
  }
  if (runtimeUnitsStopping(status)) {
    return "stopping";
  }
  const nonManualWorkRunning = (status.active_works ?? []).some((work) => work.source !== "manual");
  if (status.scheduler.running || status.events.running || nonManualWorkRunning) {
    return "running";
  }
  return "stopped";
}

function commandRuntimeNavState(status: RuntimeStatus | undefined): NavRuntimeState {
  const manualWorkRunning = (status?.active_works ?? []).some((work) => work.source === "manual");
  if (!manualWorkRunning) {
    return "stopped";
  }
  return status && runtimeUnitsStopping(status) ? "stopping" : "running";
}

function runtimeUnitsStopping(status: RuntimeStatus) {
  return (
    status.scheduler.state === "stopping" ||
    status.events.state === "stopping" ||
    isStopTimeoutPending(status.scheduler) ||
    isStopTimeoutPending(status.events)
  );
}

function runtimeHasActiveWork(status: RuntimeStatus | undefined): boolean {
  if (!status) {
    return false;
  }
  return (
    status.scheduler.running || status.events.running || (status.active_works ?? []).length > 0
  );
}

/**
 * Block the Tauri window close while service or command work is running, so a
 * quit never orphans a running agent (the Rust host SIGKILLs the backend
 * sidecar on exit, skipping its graceful shutdown). Registering an
 * onCloseRequested listener defers the close decision to this handler; the
 * user can force stop the runtime and quit from the modal.
 */
function useAppCloseGuard() {
  const [blocked, setBlocked] = useState(false);
  const [forceQuitting, setForceQuitting] = useState(false);
  const [forceQuitError, setForceQuitError] = useState<string | null>(null);

  useEffect(() => {
    if (!isTauriRuntime()) {
      return;
    }
    let cancelled = false;
    let unlisten: (() => void) | undefined;
    void (async () => {
      try {
        const { getCurrentWindow } = await import("@tauri-apps/api/window");
        const stop = await getCurrentWindow().onCloseRequested(async (event) => {
          let busy = false;
          try {
            busy = runtimeHasActiveWork(await getSchedulerStatus());
          } catch {
            // The backend is unreachable, so there is no work to protect.
          }
          if (busy) {
            event.preventDefault();
            setBlocked(true);
          }
        });
        if (cancelled) {
          stop();
        } else {
          unlisten = stop;
        }
      } catch {
        // The window API is unavailable (e.g. a harness that only stubs
        // __TAURI_INTERNALS__ without the full metadata): skip the guard
        // rather than leaking an unhandled rejection.
      }
    })();
    return () => {
      cancelled = true;
      unlisten?.();
    };
  }, []);

  const cancel = () => {
    setBlocked(false);
    setForceQuitError(null);
  };
  const forceStopAndQuit = async () => {
    setForceQuitting(true);
    setForceQuitError(null);
    try {
      try {
        await stopScheduler({ force: true });
      } catch {
        // Quit regardless; the force stop is best-effort cleanup.
      }
      const { getCurrentWindow } = await import("@tauri-apps/api/window");
      await getCurrentWindow().destroy();
    } catch (error) {
      // destroy() failed (e.g. a missing window capability), so the window is
      // staying open: surface the error instead of leaving the button spinning.
      setForceQuitError(error instanceof Error ? error.message : String(error));
      setForceQuitting(false);
    }
  };

  return { blocked, forceQuitting, forceQuitError, cancel, forceStopAndQuit };
}

function AppCloseBlockedModal({
  opened,
  forceQuitting,
  error,
  onCancel,
  onForceQuit,
}: {
  opened: boolean;
  forceQuitting: boolean;
  error: string | null;
  onCancel: () => void;
  onForceQuit: () => void;
}) {
  const { t } = useTranslation();
  return (
    <Modal centered opened={opened} onClose={onCancel} title={t("app.closeBlocked.title")}>
      <Stack gap="md">
        <Text size="sm">{t("app.closeBlocked.body")}</Text>
        {error ? (
          <Alert color="danger" title={t("app.closeBlocked.error")}>
            {error}
          </Alert>
        ) : null}
        <Group justify="flex-end">
          <Button variant="default" onClick={onCancel}>
            {t("app.closeBlocked.cancel")}
          </Button>
          <Button color="danger" loading={forceQuitting} onClick={onForceQuit}>
            {t("app.closeBlocked.force")}
          </Button>
        </Group>
      </Stack>
    </Modal>
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
    mutationFn: () => stopScheduler(),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["scheduler"] }),
  });
  const forceStopMutation = useMutation({
    mutationFn: () => stopScheduler({ force: true }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["scheduler"] }),
  });
  const activeWorks = scheduler.data?.active_works ?? [];
  const activeMembers = team.data?.members.filter((member) => member.is_active) ?? [];
  const runtimeRunning = runtimeHasActiveWork(scheduler.data);
  const runtimeStarting = Boolean(
    startMutation.isPending ||
    scheduler.data?.scheduler.state === "starting" ||
    scheduler.data?.events.state === "starting",
  );
  const runtimeStopPending = Boolean(
    isStopTimeoutPending(scheduler.data?.scheduler) || isStopTimeoutPending(scheduler.data?.events),
  );
  const runtimeStopping = Boolean(
    stopMutation.isPending ||
    forceStopMutation.isPending ||
    runtimeStopPending ||
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
          <Group gap="xs">
            <Button
              leftSection={<Square size={16} />}
              loading={stopMutation.isPending}
              variant="default"
              disabled={stopDisabled || stopMutation.isPending}
              onClick={() => stopMutation.mutate()}
            >
              {t("overview.stop")}
            </Button>
            {runtimeStopping ? (
              <Button
                color="danger"
                leftSection={<TriangleAlert size={16} />}
                loading={forceStopMutation.isPending}
                variant="light"
                disabled={stopDisabled || forceStopMutation.isPending}
                onClick={() => forceStopMutation.mutate()}
              >
                {t("overview.forceStop")}
              </Button>
            ) : null}
          </Group>
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
        <Alert color="warning" title={t("overview.setupRequiredTitle")}>
          <Group justify="space-between" align="center">
            <Text size="sm">{t("overview.setupRequiredBody")}</Text>
            <Button component={NavLink} to="/setup" variant="light">
              {t("overview.openSetup")}
            </Button>
          </Group>
        </Alert>
      ) : null}

      {noStartTarget ? (
        <Alert color="warning" title={t("service.noTargetTitle")}>
          {t("service.noTargetBody")}
        </Alert>
      ) : null}

      {activeWorks.length > 0 ? <ActiveWorkNotice works={activeWorks} /> : null}

      <Card withBorder radius="md" p="lg">
        <Stack>
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
              ]}
            >
              <Stack gap="xs">
                <Group>
                  <Button
                    component={NavLink}
                    to="/setup?section=members&tab=patrol"
                    variant="light"
                  >
                    {t("overview.openMemberPatrolSettings")}
                  </Button>
                </Group>
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
              </Stack>
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
            >
              <ChatReceiveResetControl disabled={runtimeActive} />
            </ServiceRuntimeSection>
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
              ]}
            >
              <Group>
                <Button component={NavLink} to="/setup?section=members&tab=patrol" variant="light">
                  {t("overview.openMemberPatrolSettings")}
                </Button>
              </Group>
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
          {startMutation.error ? (
            <Alert color="danger" title={t("overview.startError")}>
              {startMutation.error.message}
            </Alert>
          ) : null}
          {stopMutation.error || forceStopMutation.error ? (
            <Alert color="danger" title={t("overview.stopError")}>
              {(stopMutation.error ?? forceStopMutation.error)?.message}
            </Alert>
          ) : null}
        </Stack>
      </Card>
    </Stack>
  );
}

function DiagnosticsPage() {
  const { t } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();
  const activeTab = diagnosticsTabFromSearch(searchParams.get("tab"));
  const memoryFocus = memoryFocusFromSearch(searchParams);
  const config = useQuery({ queryKey: ["config"], queryFn: getConfigStatus });
  const team = useQuery({ queryKey: ["team"], queryFn: getTeam, retry: false });
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
  const changeDiagnosticsTab = (value: string | null) => {
    const next = new URLSearchParams(searchParams);
    const tab = diagnosticsTabFromSearch(value);
    if (tab === "memory") {
      clearMemoryFocusSearchParams(next);
    }
    if (tab === "readiness") {
      next.delete("tab");
    } else {
      next.set("tab", tab);
    }
    setSearchParams(next);
  };
  return (
    <Stack className="diagnostics-page" gap="lg">
      <Group justify="space-between">
        <div>
          <Title order={2}>{t("diagnostics.title")}</Title>
        </div>
      </Group>
      <Tabs className="diagnostics-tabs" value={activeTab} onChange={changeDiagnosticsTab}>
        <Tabs.List>
          <Tabs.Tab value="readiness">{t("diagnostics.tabs.readiness")}</Tabs.Tab>
          <Tabs.Tab value="executions">{t("diagnostics.tabs.executions")}</Tabs.Tab>
          <Tabs.Tab value="memory">{t("diagnostics.tabs.memory")}</Tabs.Tab>
          <Tabs.Tab value="settings">{t("diagnostics.tabs.settings")}</Tabs.Tab>
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
                <Badge color={hasProjectConfig ? "success" : "warning"} variant="light">
                  {hasProjectConfig ? t("overview.ready") : t("overview.missing")}
                </Badge>
              </dd>
              <dt>{t("overview.env")}</dt>
              <dd>
                <Badge color={config.data?.env_file_exists ? "success" : "neutral"} variant="light">
                  {config.data?.env_file_exists ? t("overview.found") : t("overview.notFound")}
                </Badge>
              </dd>
              <dt>{t("overview.activeMembers")}</dt>
              <dd>{activeMembers.length}</dd>
              <dt>{t("overview.github")}</dt>
              <dd>
                <Badge color={githubEnabled ? "success" : "neutral"} variant="light">
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
        <Tabs.Panel className="diagnostics-fill-panel" value="executions" pt="md">
          <TraceExplorer />
        </Tabs.Panel>
        <Tabs.Panel className="diagnostics-fill-panel" value="memory" pt="md">
          <MemoryEventsPanel
            key={memoryFocusKey(memoryFocus)}
            members={team.data?.members ?? []}
            focus={memoryFocus}
          />
        </Tabs.Panel>
        <Tabs.Panel value="settings" pt="md">
          <Card withBorder radius="md" p="lg">
            <Stack gap="md">
              <RuntimeDebugSettings />
              <TranscriptSettingsPanel />
            </Stack>
          </Card>
        </Tabs.Panel>
      </Tabs>
    </Stack>
  );
}

function diagnosticsTabFromSearch(value: string | null): DiagnosticsTab {
  return value && DIAGNOSTICS_TABS.has(value as DiagnosticsTab)
    ? (value as DiagnosticsTab)
    : "readiness";
}

function memoryFocusFromSearch(searchParams: URLSearchParams): MemoryEventFocus {
  return {
    docId: searchParams.get("doc_id") ?? "",
    traceId: searchParams.get("memory_trace_id") ?? "",
    timestamp: searchParams.get("timestamp") ?? "",
    action: searchParams.get("action") ?? "",
    personId: searchParams.get("person_id") ?? "",
  };
}

const TRACE_SOURCES = [
  "all",
  "interactive",
  "routine",
  "scheduled",
  "event_listener",
  "manual",
  "diagnostics",
] as const;

const RECORD_FILTERS = ["all", "error", "ai", "memory", "event", "log"] as const;

// The Global/system view only holds unscoped events and logs: AI io records
// always run inside a trace, and memory records are merged into per-trace
// detail only, so those filters can never match there.
const GLOBAL_RECORD_FILTERS = ["all", "error", "event", "log"] as const;

export type RecordScopeFilter = {
  kind: "span" | "call" | "subtree";
  value: string;
  label: string;
};

// Sentinel selection for the pinned "Global / system" view, which shows
// unscoped records (service lifecycle events + global logs) that do not belong
// to any trace.
const GLOBAL_TRACE_ID = "__global__";

function traceIdsFromSearch(searchParams: URLSearchParams): string[] {
  const rawValues = searchParams.getAll("trace_ids");
  const commaValue = searchParams.get("trace_ids");
  const values = rawValues.length > 0 ? rawValues : commaValue ? [commaValue] : [];
  return Array.from(
    new Set(
      values
        .flatMap((value) => value.split(","))
        .map((value) => value.trim())
        .filter((value) => value.length > 0),
    ),
  );
}

function compositeTraceRecords(details: TraceDetailResponse[]): TraceRecord[] {
  return details
    .flatMap((detail) => detail.records)
    .sort((left, right) => timestampMillis(left.timestamp) - timestampMillis(right.timestamp));
}

function compositeTraceSummary(
  details: TraceDetailResponse[],
  traceIds: string[],
  t: TFunction,
): TraceSummary | null {
  if (traceIds.length === 0) {
    return null;
  }
  const summaries = details.map((detail) => detail.summary).filter(Boolean) as TraceSummary[];
  const records = compositeTraceRecords(details);
  const startedAt =
    minTimestamp([
      ...summaries.map((summary) => summary.started_at),
      ...records.map((record) => record.timestamp),
    ]) ?? "";
  const updatedAt =
    maxTimestamp([
      ...summaries.map((summary) => summary.updated_at),
      ...records.map((record) => record.timestamp),
    ]) ?? startedAt;
  return {
    trace_id: traceIds.join(","),
    source: "",
    person_id: "",
    command: t("diagnostics.executions.compositeTitle"),
    workflow: "",
    started_at: startedAt,
    updated_at: updatedAt,
    status: compositeTraceStatus(summaries),
    event_count: summaries.reduce((total, summary) => total + summary.event_count, 0),
    log_count: summaries.reduce((total, summary) => total + summary.log_count, 0),
    error_count: summaries.reduce((total, summary) => total + summary.error_count, 0),
    span_count: summaries.reduce((total, summary) => total + summary.span_count, 0),
    attributes: {},
  };
}

function compositeTraceStatus(summaries: TraceSummary[]): TraceSummary["status"] {
  if (
    summaries.some(
      (summary) =>
        summary.status === "failed" ||
        summary.status === "abandoned" ||
        summary.status === "incomplete",
    )
  ) {
    return "failed";
  }
  if (summaries.some((summary) => summary.status === "running")) {
    return "running";
  }
  if (summaries.some((summary) => summary.status === "retry_scheduled")) {
    return "retry_scheduled";
  }
  if (summaries.length > 0 && summaries.every((summary) => summary.status === "success")) {
    return "success";
  }
  return "info";
}

function minTimestamp(values: string[]): string | null {
  return extremeTimestamp(values, Math.min);
}

function maxTimestamp(values: string[]): string | null {
  return extremeTimestamp(values, Math.max);
}

function extremeTimestamp(
  values: string[],
  select: (...values: number[]) => number,
): string | null {
  const parsed = values
    .map((value) => ({ value, millis: timestampMillis(value) }))
    .filter((item) => Number.isFinite(item.millis));
  if (parsed.length === 0) {
    return null;
  }
  const target = select(...parsed.map((item) => item.millis));
  return parsed.find((item) => item.millis === target)?.value ?? null;
}

function timestampMillis(value: string): number {
  const millis = Date.parse(value);
  return Number.isNaN(millis) ? Number.POSITIVE_INFINITY : millis;
}

type MemoryEventFilters = {
  personId: string;
  action: string;
  query: string;
  docId: string;
  traceId: string;
};

function MemoryEventsPanel({
  members,
  focus,
}: {
  members: Array<{ person_id: string; name: string; person_type?: string }>;
  focus: MemoryEventFocus;
}) {
  const { t } = useTranslation();
  const [selectedId, setSelectedId] = useState("");
  const [searchInput, setSearchInput] = useState("");
  const [filters, setFilters] = useState<MemoryEventFilters>({
    personId: focus.personId || MEMORY_FILTER_ALL,
    action: focus.action || MEMORY_FILTER_ALL,
    query: "",
    docId: focus.docId,
    traceId: focus.traceId,
  });
  const memoryEvents = useQuery({
    queryKey: ["diagnostics-memory-events", filters],
    queryFn: () =>
      getMemoryEvents({
        personId: filters.personId === MEMORY_FILTER_ALL ? undefined : filters.personId,
        action: filters.action === MEMORY_FILTER_ALL ? undefined : filters.action,
        docId: filters.docId || undefined,
        traceId: filters.traceId || undefined,
        query: filters.query.trim() || undefined,
        limit: MEMORY_EVENT_LIMIT,
      }),
    refetchInterval: 5000,
  });
  const events = sortMemoryEventsDescending(memoryEvents.data?.events ?? []);
  const selectableMembers = members.filter((member) => member.person_type !== "human");
  const selectedEvent =
    events.find((event) => memoryEventKey(event) === selectedId) ??
    events.find((event) => memoryEventMatchesFocus(event, focus)) ??
    events[0] ??
    null;
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
                color="neutral"
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
            <Alert color="danger" title={t("diagnostics.memory.loadError")}>
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
        event.duration_ms === null ? "" : formatDuration(event.duration_ms),
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
  const [searchParams, setSearchParams] = useSearchParams();
  const focusedTraceId = searchParams.get("trace_id") ?? "";
  const compositeTraceIds = useMemo(() => traceIdsFromSearch(searchParams), [searchParams]);
  const isComposite = compositeTraceIds.length > 0;
  const [source, setSource] = useState("all");
  const [searchInput, setSearchInput] = useState("");
  const [query, setQuery] = useState("");
  // Default to the pinned Global / system view so unscoped records (service
  // lifecycle events and global logs such as Slack listener auth failures) are
  // visible the moment the executions tab opens, without the user having to know
  // to click the Global entry.
  const [selectedTraceId, setSelectedTraceId] = useState<string | null>(
    focusedTraceId || (isComposite ? null : GLOBAL_TRACE_ID),
  );
  const [recordFilter, setRecordFilter] = useState("all");
  const [recordScopeFilter, setRecordScopeFilter] = useState<RecordScopeFilter | null>(null);
  const [drawerRecord, setDrawerRecord] = useState<TraceRecord | null>(null);
  const [attrFilter, setAttrFilter] = useState<AttrFilter | null>(null);
  const isGlobal = !isComposite && selectedTraceId === GLOBAL_TRACE_ID;

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
    enabled: Boolean(selectedTraceId) && !isComposite,
    refetchInterval: selectedTraceId ? 5000 : false,
  });
  const compositeDetail = useQuery({
    queryKey: ["diagnostics-trace-composite", compositeTraceIds],
    queryFn: async () => Promise.all(compositeTraceIds.map((traceId) => getTraceDetail(traceId))),
    enabled: isComposite,
    refetchInterval: isComposite ? 5000 : false,
  });
  const traceItems = useMemo(() => traces.data?.traces ?? [], [traces.data]);
  const compositeSummary = useMemo(
    () => compositeTraceSummary(compositeDetail.data ?? [], compositeTraceIds, t),
    [compositeDetail.data, compositeTraceIds, t],
  );
  const selectedSummary = useMemo(() => {
    if (isComposite) {
      return compositeSummary;
    }
    return (
      traceItems.find((trace) => trace.trace_id === selectedTraceId) ?? detail.data?.summary ?? null
    );
  }, [isComposite, compositeSummary, traceItems, selectedTraceId, detail.data]);
  // The API returns records oldest-first; show them newest-first so a live
  // (polling) trace surfaces new records at the top without scrolling, matching
  // the descending order used by the rest of the diagnostics UI.
  const timelineRecords = isComposite
    ? compositeTraceRecords(compositeDetail.data ?? [])
    : (detail.data?.records ?? []);
  const hasMemoryRecords =
    !isComposite && timelineRecords.some((record) => record.kind === "memory");
  const records = timelineRecords
    .filter((record) => matchesRecordFilter(record, recordFilter))
    .filter((record) => matchesRecordScopeFilter(record, recordScopeFilter))
    .reverse();

  // Selecting a different execution resets its per-trace UI state (record
  // filter) at the event source rather than in an effect.
  const selectTrace = (traceId: string) => {
    const next = new URLSearchParams(searchParams);
    next.set("tab", "executions");
    next.delete("trace_ids");
    if (traceId === GLOBAL_TRACE_ID) {
      next.delete("trace_id");
    } else {
      next.set("trace_id", traceId);
    }
    setSearchParams(next);
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
                color="neutral"
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
            color="neutral"
            leftSection={<Ticket size={12} />}
            rightSection={
              <ActionIcon
                size="xs"
                variant="transparent"
                color="neutral"
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
          {isComposite ? (
            <button
              type="button"
              className="exec-row exec-row-active exec-row-composite"
              onClick={() => {
                setRecordFilter("all");
                setRecordScopeFilter(null);
              }}
            >
              <div className="exec-row-top">
                <Badge size="sm" color="neutral" variant="light">
                  {t("diagnostics.executions.compositeBadge")}
                </Badge>
                <span className="exec-row-time">{compositeTraceIds.length}</span>
              </div>
              <Text className="exec-row-command" fw={600} size="sm">
                {t("diagnostics.executions.compositeTitle")}
              </Text>
              <div className="exec-row-meta">
                <span>
                  {t("diagnostics.executions.compositeSubtitle", {
                    count: compositeTraceIds.length,
                  })}
                </span>
              </div>
            </button>
          ) : null}
          {showGlobalEntry ? (
            <button
              type="button"
              className={
                isGlobal ? "exec-row exec-row-global exec-row-active" : "exec-row exec-row-global"
              }
              onClick={() => selectTrace(GLOBAL_TRACE_ID)}
            >
              <div className="exec-row-top">
                <Badge size="sm" color="neutral" variant="light">
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
                    <Badge size="sm" color="neutral" variant="light">
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
                    {isTerminalTraceStatus(trace.status)
                      ? t("diagnostics.executions.counts", {
                          events: trace.event_count,
                          logs: trace.log_count,
                        })
                      : null}
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
                filters={GLOBAL_RECORD_FILTERS}
                filter={recordFilter}
                scopeFilter={recordScopeFilter}
                onFilter={setRecordFilter}
                onClearScopeFilter={() => setRecordScopeFilter(null)}
                onSelect={setDrawerRecord}
              />
            </>
          ) : selectedSummary ? (
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
                      {isComposite ? (
                        <Badge color="success" variant="outline">
                          {t("diagnostics.executions.compositeBadge")}
                        </Badge>
                      ) : (
                        <Badge variant="outline">
                          {traceSourceLabel(t, selectedSummary.source || "unknown")}
                        </Badge>
                      )}
                      {(() => {
                        const chip = ticketChipInfo(selectedSummary.attributes);
                        if (!chip) {
                          return null;
                        }
                        return (
                          <Tooltip label={t("diagnostics.executions.ticket.filterTo")} withArrow>
                            <Badge
                              color="info"
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
                                    color="info"
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
                      {hasMemoryRecords ? (
                        <Badge
                          component={NavLink}
                          color="info"
                          variant="light"
                          style={{ cursor: "pointer", textDecoration: "none" }}
                          to={traceMemoryUrl(searchParams, selectedSummary.trace_id)}
                        >
                          {t("diagnostics.tabs.memory")}
                        </Badge>
                      ) : null}
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
                      {isComposite ? (
                        <span>{compositeTraceIds.length}</span>
                      ) : (
                        <>
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
                                  color={copied ? "success" : "neutral"}
                                  onClick={copy}
                                >
                                  {copied ? <CheckCircle2 size={14} /> : <Copy size={14} />}
                                </ActionIcon>
                              </Tooltip>
                            )}
                          </CopyButton>
                        </>
                      )}
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
                    {isTerminalTraceStatus(selectedSummary.status) ? (
                      <span>
                        {t("diagnostics.executions.counts", {
                          events: selectedSummary.event_count,
                          logs: selectedSummary.log_count,
                        })}
                      </span>
                    ) : null}
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
              {detail.data?.transcript_available === false ? (
                <Alert color="neutral" title={t("diagnostics.executions.transcriptDeleted")} />
              ) : null}
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
  filters = RECORD_FILTERS,
  filter,
  scopeFilter,
  onFilter,
  onClearScopeFilter,
  onSelect,
}: {
  records: TraceRecord[];
  filters?: readonly string[];
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
          data={filters.map((value) => ({
            value,
            label: t(`diagnostics.executions.recordFilters.${value}`),
          }))}
        />
        {scopeFilter ? (
          <Badge
            className="exec-filter-pill exec-record-scope-pill"
            size="lg"
            variant="light"
            color="info"
            rightSection={
              <ActionIcon
                size="xs"
                variant="transparent"
                color="info"
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
              <span className="exec-timeline-message">{recordDisplayMessage(t, record)}</span>
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
  const message = recordDisplayMessage(t, record);
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
  // "AI" groups everything produced during a brain call — the LLM / AI CLI
  // request-response transcripts plus every record tagged with that call's
  // span (agent activity events, streamed responses, logs).
  const agentSpan = record.span === "llm" || record.span === "cli_agent";
  if (filter === "ai") {
    return (
      agentSpan ||
      (record.kind === "io" &&
        (record.type.startsWith("llm") || record.type.startsWith("cli_agent")))
    );
  }
  // Records grouped under "ai" are excluded from the kind filters so "event"
  // and "log" stay the non-AI execution milestones and plain service logs.
  if (filter === "event" || filter === "log") {
    return record.kind === filter && !agentSpan;
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
    return "success";
  }
  if (action === "recall") {
    return "info";
  }
  if (action === "get") {
    return "info";
  }
  if (action === "update") {
    return "info";
  }
  if (action === "touch") {
    return "success";
  }
  if (action === "archive") {
    return "neutral";
  }
  if (action === "promote") {
    return "info";
  }
  return "neutral";
}

function memoryEventKey(event: MemoryEvent): string {
  return `${event.timestamp}-${event.action}-${event.person_id}-${event.doc_id}`;
}

function clearMemoryFocusSearchParams(searchParams: URLSearchParams): void {
  for (const key of MEMORY_FOCUS_SEARCH_PARAMS) {
    searchParams.delete(key);
  }
}

function traceMemoryUrl(searchParams: URLSearchParams, traceId: string): string {
  const next = new URLSearchParams(searchParams);
  clearMemoryFocusSearchParams(next);
  next.set("tab", "memory");
  next.set("memory_trace_id", traceId);
  return `/diagnostics?${next.toString()}`;
}

function memoryFocusKey(focus: MemoryEventFocus): string {
  return [focus.docId, focus.traceId, focus.timestamp, focus.action, focus.personId].join("\0");
}

function memoryEventMatchesFocus(event: MemoryEvent, focus: MemoryEventFocus): boolean {
  if (!focus.docId && !focus.traceId && !focus.timestamp && !focus.action && !focus.personId) {
    return false;
  }
  if (focus.docId && event.doc_id !== focus.docId) {
    return false;
  }
  if (focus.traceId && event.trace_id !== focus.traceId) {
    return false;
  }
  if (focus.timestamp && event.timestamp !== focus.timestamp) {
    return false;
  }
  if (focus.action && event.action !== focus.action) {
    return false;
  }
  if (focus.personId && event.person_id !== focus.personId) {
    return false;
  }
  return true;
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

// Surface the most useful one-line summary per record: log message, I/O
// payload excerpt, or — for events — the payload detail (e.g. a failure
// reason) falling back to the translated event label.
export function recordDisplayMessage(t: TFunction, record: TraceRecord): string {
  if (record.kind === "log") {
    return record.message;
  }
  const payload = record.payload ?? {};
  if (record.kind === "io") {
    return (
      record.message || firstPayloadString(payload, ["message", "prompt", "stdout"]) || record.type
    );
  }
  if (record.kind === "memory") {
    return record.message || record.type;
  }
  if (record.type.startsWith("span.")) {
    const model = typeof payload.model === "string" ? payload.model : "";
    const duration =
      typeof payload.duration_ms === "number" ? formatDuration(payload.duration_ms) : "";
    const summary = [model, duration].filter(Boolean).join(" · ");
    if (summary) {
      return summary;
    }
  }
  return (
    firstPayloadString(payload, ["message", "error", "code", "error_type"]) ||
    eventNameLabel(t, payload) ||
    eventTypeLabel(t, record.type)
  );
}

function firstPayloadString(payload: Record<string, unknown>, keys: string[]): string {
  for (const key of keys) {
    const value = payload[key];
    if (typeof value === "string" && value) {
      return value;
    }
  }
  return "";
}

// Message-less events (turn started, process initialized, ...) still carry a
// provider-neutral phase name in payload.name; show its translation so the
// row says "Started"/"Completed" instead of repeating the badge label.
function eventNameLabel(t: TFunction, payload: Record<string, unknown>): string {
  const name = typeof payload.name === "string" ? payload.name : "";
  return name ? t(`diagnostics.executions.eventNames.${name}`, { defaultValue: name }) : "";
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

function formatDuration(ms: number): string {
  return ms < 1000 ? `${Math.max(0, Math.round(ms))}ms` : `${(ms / 1000).toFixed(1)}s`;
}

export { traceStatusColor, isTerminalTraceStatus };

export function recordBadgeColor(record: TraceRecord): string {
  if (record.kind === "io") {
    return "info";
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
  if (record.kind === "io") {
    return t(`diagnostics.executions.ioTypes.${record.type.replace(".", "_")}`, {
      defaultValue: record.type || t("diagnostics.executions.kinds.io"),
    });
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
  if (record.type === "agent_runtime.assistant" && record.payload?.partial === true) {
    return t("diagnostics.executions.agentRuntime.assistant_partial");
  }
  // Approval events announce either the session policy or a decision; the
  // provider-neutral payload.name tells which, and the badge should too.
  if (record.type === "agent_runtime.approval" && record.payload?.name === "policy") {
    return t("diagnostics.executions.agentRuntime.approval_policy");
  }
  return eventTypeLabel(t, record.type);
}

function ChatReceiveResetControl({ disabled }: { disabled: boolean }) {
  const { t } = useTranslation();
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [result, setResult] = useState<ChatReceiveResetResponse | null>(null);
  const resetMutation = useMutation({
    mutationFn: resetChatReceiveState,
    onSuccess: (data) => {
      setResult(data);
      setConfirmOpen(false);
    },
  });

  const tooltipLabel = disabled
    ? `${t("overview.eventsCard.chatReset.description")} (${t("overview.eventsCard.chatReset.stoppedOnlyHint")})`
    : t("overview.eventsCard.chatReset.description");

  return (
    <Stack gap="xs">
      <Group gap="sm" align="center">
        <Tooltip label={tooltipLabel} withArrow multiline w={300}>
          <span style={{ display: "inline-block" }}>
            <Button
              color="danger"
              variant="light"
              disabled={disabled}
              loading={resetMutation.isPending}
              leftSection={<RotateCcw size={14} />}
              onClick={() => {
                setResult(null);
                resetMutation.reset();
                setConfirmOpen(true);
              }}
            >
              {t("overview.eventsCard.chatReset.action")}
            </Button>
          </span>
        </Tooltip>
      </Group>
      {result ? (
        <Text size="sm" c="success">
          {t("overview.eventsCard.chatReset.successBody", {
            members: result.members_reset,
            channels: result.channels_reset,
          })}
        </Text>
      ) : null}
      {resetMutation.error ? (
        <Text size="sm" c="danger">
          {resetMutation.error.message}
        </Text>
      ) : null}
      <Modal
        opened={confirmOpen}
        onClose={() => setConfirmOpen(false)}
        title={t("overview.eventsCard.chatReset.confirmTitle")}
        centered
      >
        <Stack gap="md">
          <Text size="sm">{t("overview.eventsCard.chatReset.confirmBody")}</Text>
          <Group justify="flex-end" gap="sm">
            <Button variant="default" onClick={() => setConfirmOpen(false)}>
              {t("overview.eventsCard.chatReset.cancel")}
            </Button>
            <Button
              color="danger"
              loading={resetMutation.isPending}
              onClick={() => resetMutation.mutate()}
            >
              {t("overview.eventsCard.chatReset.confirm")}
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Stack>
  );
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
        {rows.map(([label, value]) => (
          <FragmentRow key={label} label={label} value={value || t("overview.unknown")} />
        ))}
      </dl>
      {(unit?.events_auth_failed_count ?? 0) > 0 ? (
        <Alert color="danger" title={t("overview.eventsCard.authFailedTitle")}>
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
        <Alert color="danger" title={t("overview.runtimeError")}>
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
    <div className="service-unit-panel">
      <Group justify="space-between" align="center">
        <div>
          <Text fw={700} size="sm">
            {t("diagnostics.runtimeDebug.title")}
          </Text>
          <Text c="dimmed" size="xs">
            {t("diagnostics.runtimeDebug.status", {
              logLevel: runtimeDebug.data?.log_level ?? "-",
              agnoDebug: runtimeDebug.data?.agno_debug ? "true" : "false",
            })}
          </Text>
        </div>
        <Switch
          checked={enabled}
          disabled={runtimeDebugMutation.isPending || runtimeDebug.isLoading}
          label={
            enabled ? t("diagnostics.runtimeDebug.enabled") : t("diagnostics.runtimeDebug.disabled")
          }
          onChange={(event) => runtimeDebugMutation.mutate(event.currentTarget.checked)}
        />
      </Group>
      {runtimeDebugMutation.error ? (
        <Alert color="danger" title={t("diagnostics.runtimeDebug.saveError")}>
          {runtimeDebugMutation.error.message}
        </Alert>
      ) : null}
    </div>
  );
}

function TranscriptSettingsPanel() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const settings = useQuery({
    queryKey: ["transcript-settings"],
    queryFn: getTranscriptSettings,
    refetchInterval: 5000,
  });
  const [retentionDraft, setRetentionDraft] = useState<number | null>(null);
  const detail = settings.data?.detail ?? "standard";
  const retentionDays = retentionDraft ?? settings.data?.retention_days ?? 30;
  const mutation = useMutation({
    mutationFn: updateTranscriptSettings,
    onSuccess: (data) => {
      queryClient.setQueryData<TranscriptSettingsStatus>(["transcript-settings"], data);
      setRetentionDraft(null);
    },
  });
  const saveRetention = () => {
    mutation.mutate({
      detail,
      retention_days: retentionDays,
    });
  };
  return (
    <div className="service-unit-panel">
      <Stack gap="sm">
        <div>
          <Text fw={700} size="sm">
            {t("diagnostics.transcripts.title")}
          </Text>
          <Text c="dimmed" size="xs">
            {t("diagnostics.transcripts.description")}
          </Text>
        </div>
        <Group align="flex-end" grow>
          <Select
            label={t("diagnostics.transcripts.detail")}
            description={t(
              detail === "full"
                ? "diagnostics.transcripts.fullDescription"
                : "diagnostics.transcripts.standardDescription",
            )}
            data={[
              { value: "standard", label: t("diagnostics.transcripts.standard") },
              { value: "full", label: t("diagnostics.transcripts.full") },
            ]}
            disabled={mutation.isPending || settings.isLoading}
            value={detail}
            onChange={(value) => {
              if (value === "standard" || value === "full") {
                mutation.mutate({
                  detail: value,
                  retention_days: settings.data?.retention_days ?? retentionDays,
                });
              }
            }}
          />
          <NumberInput
            label={t("diagnostics.transcripts.retentionDays")}
            min={1}
            max={3650}
            allowDecimal={false}
            disabled={mutation.isPending || settings.isLoading}
            value={retentionDays}
            onChange={(value) => {
              if (typeof value === "number") {
                setRetentionDraft(value);
              }
            }}
            onBlur={saveRetention}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.currentTarget.blur();
              }
            }}
          />
        </Group>
        <dl className="status-list">
          <dt>{t("diagnostics.transcripts.sessionsTotal")}</dt>
          <dd>{formatBytes(settings.data?.total_size_bytes ?? 0)}</dd>
          <dt>{t("diagnostics.transcripts.indexSize")}</dt>
          <dd>
            {t("diagnostics.transcripts.indexUsage", {
              current: formatBytes(settings.data?.index_size_bytes ?? 0),
              threshold: formatBytes(settings.data?.index_rewrite_threshold_bytes ?? 0),
            })}
          </dd>
          <dt>{t("diagnostics.transcripts.memorySize")}</dt>
          <dd>
            {t("diagnostics.transcripts.memoryUsage", {
              current: formatBytes(settings.data?.memory_size_bytes ?? 0),
              maximum: formatBytes(settings.data?.memory_max_size_bytes ?? 0),
            })}
          </dd>
        </dl>
      </Stack>
      {mutation.error ? (
        <Alert color="danger" title={t("diagnostics.transcripts.saveError")}>
          {mutation.error.message}
        </Alert>
      ) : null}
    </div>
  );
}

function formatBytes(value: number): string {
  if (value < 1024) {
    return `${value} B`;
  }
  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)} KiB`;
  }
  return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
}
function FragmentRow({ label, value }: { label: string; value: string }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </>
  );
}

function ActiveWorkNotice({ works }: { works: RuntimeActiveWork[] }) {
  const { t } = useTranslation();
  return (
    <Alert color="info" title={t("overview.activeWork.title")}>
      <Stack gap="xs">
        {works.map((work) => (
          <Group key={work.id} gap="xs" justify="space-between">
            <Text size="sm">
              {t("overview.activeWork.item", {
                person: work.person_id || t("overview.unknown"),
                source: activeWorkSourceLabel(t, work.source),
                command: work.command,
              })}
            </Text>
            <Text c="dimmed" size="xs">
              {formatDateTime(work.started_at)}
            </Text>
          </Group>
        ))}
      </Stack>
    </Alert>
  );
}

function RuntimeStateBadge({ state }: { state: RuntimeUnitStatus["state"] }) {
  const { t } = useTranslation();
  const color =
    state === "running"
      ? "success"
      : state === "failed"
        ? "danger"
        : state === "stopped"
          ? "neutral"
          : "warning";
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

function activeWorkSourceLabel(t: TFunction, source: RuntimeActiveWork["source"]) {
  return t(`overview.activeWork.sources.${source}`);
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
      <Alert color="danger" title={t("overview.scenarioDiagnostics.failed")}>
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
      <Alert color="success" title={t("overview.scenarioDiagnostics.ok")}>
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
    return "success";
  }
  if (status === "warning") {
    return "warning";
  }
  return "danger";
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
  // member.command.* mirrors the command.* lifecycle and shares its labels.
  if (type.startsWith("command.") || type.startsWith("member.command.")) {
    const phase = type.slice(type.lastIndexOf(".") + 1);
    return t(`overview.eventTypes.command_${phase}`, { defaultValue: phase });
  }
  if (type.startsWith("scheduler.")) {
    return t("overview.eventTypes.scheduler");
  }
  if (type.startsWith("events.")) {
    return t("overview.eventTypes.events");
  }
  if (type.startsWith("agent_runtime.")) {
    return t(`diagnostics.executions.agentRuntime.${type.replace("agent_runtime.", "")}`, {
      defaultValue: type,
    });
  }
  if (type.startsWith("span.")) {
    return t(`diagnostics.executions.spanEvents.${type.replace("span.", "")}`, {
      defaultValue: type,
    });
  }
  return type;
}

export function eventBadgeColor(type: string) {
  if (type.endsWith(".failed")) {
    return "danger";
  }
  if (type.includes("running") || type.includes("started") || type.includes("finished")) {
    return "success";
  }
  if (type.includes("stopping")) {
    return "warning";
  }
  return "neutral";
}

export function logBadgeColor(level: string) {
  const upper = level.toUpperCase();
  if (upper === "ERROR" || upper === "CRITICAL") {
    return "danger";
  }
  if (upper === "WARNING") {
    return "warning";
  }
  return "neutral";
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
        <Alert color="warning" title={t("overview.setupRequiredTitle")}>
          <Group justify="space-between" align="center">
            <Text size="sm">{t("overview.setupRequiredBody")}</Text>
            <Button component={NavLink} to="/setup" variant="light">
              {t("overview.openSetup")}
            </Button>
          </Group>
        </Alert>
      ) : null}

      {activeMembers.length === 0 && hasProjectConfig ? (
        <Alert color="warning" title={t("commands.noMembersTitle")}>
          {t("commands.noMembersBody")}
        </Alert>
      ) : null}

      {commandBlocked ? (
        <Alert color="warning" title={t("commands.requirementsBlockedTitle")}>
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
                            color={requirement.satisfied ? "success" : "warning"}
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
    return "success";
  }
  if (status === "failed") {
    return "danger";
  }
  return "info";
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
