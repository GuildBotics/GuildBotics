import {
  Alert,
  Badge,
  Button,
  Card,
  Group,
  Select,
  Stack,
  Tabs,
  Text,
  Textarea,
  TextInput,
  Title,
} from "@mantine/core";
import {
  Activity,
  CheckCircle2,
  Play,
  RefreshCcw,
  Settings,
  Square,
  Terminal,
  TriangleAlert,
  XCircle,
} from "lucide-react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";

import {
  type DiagnosticCheck,
  type RoutineOption,
  type RuntimeEvent,
  type RuntimeLog,
  getConfigStatus,
  getProjectConfig,
  getSchedulerRoutines,
  getSchedulerStatus,
  getTeam,
  runCommand,
  runScenarioDiagnostics,
  startScheduler,
  stopScheduler,
  subscribeEvents,
  subscribeLogs,
} from "./api/client";
import { type AppLanguage, normalizeLanguage, setAppLanguage } from "./i18n";
import { SetupPage } from "./setup/SetupPage";

const TICKET_ROUTINE = "workflows/ticket_driven_workflow";

export function App() {
  const { t, i18n } = useTranslation();
  const appLanguage = normalizeLanguage(i18n.resolvedLanguage ?? i18n.language) ?? "en";
  return (
    <main className="shell">
      <aside className="sidebar">
        <div>
          <h1>GuildBotics</h1>
          <p>{t("app.localWorkspace")}</p>
        </div>
        <nav className="nav">
          <NavLink className="nav-item" to="/overview">
            <Activity size={18} /> {t("app.nav.overview")}
          </NavLink>
          <NavLink className="nav-item" to="/commands">
            <Terminal size={18} /> {t("app.nav.commands")}
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
          <Route element={<OverviewPage />} path="/overview" />
          <Route element={<CommandsPage />} path="/commands" />
          <Route element={<SetupPage />} path="/setup" />
          <Route element={<Navigate replace to="/setup" />} path="*" />
        </Routes>
      </section>
    </main>
  );
}

function OverviewPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [selectedRoutine, setSelectedRoutine] = useState("");
  const [runtimeEvents, setRuntimeEvents] = useState<RuntimeEvent[]>([]);
  const [runtimeLogs, setRuntimeLogs] = useState<RuntimeLog[]>([]);
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
    const stopEvents = subscribeEvents((event) => {
      setRuntimeEvents((current) => [event, ...current].slice(0, 40));
    });
    const stopLogs = subscribeLogs((log) => {
      setRuntimeLogs((current) => [log, ...current].slice(0, 40));
    });
    return () => {
      stopEvents();
      stopLogs();
    };
  }, []);

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

  const diagnosticsMutation = useMutation({
    mutationFn: () => runScenarioDiagnostics(),
  });
  const startMutation = useMutation({
    mutationFn: () => {
      if (!selectedRoutine) {
        return startScheduler({});
      }
      return startScheduler({ routine_commands: [selectedRoutine] });
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

  return (
    <Stack gap="lg">
      <Group justify="space-between">
        <div>
          <Text className="eyebrow">{t("overview.eyebrow")}</Text>
          <Title order={2}>{t("overview.title")}</Title>
        </div>
        <Button
          aria-label={t("overview.refresh")}
          leftSection={<RefreshCcw size={18} />}
          variant="default"
          onClick={() => queryClient.invalidateQueries()}
        >
          {t("overview.refresh")}
        </Button>
      </Group>

      <div className="grid">
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
          <dl>
            <dt>{t("overview.config")}</dt>
            <dd>{hasProjectConfig ? t("overview.ready") : t("overview.missing")}</dd>
            <dt>{t("overview.env")}</dt>
            <dd>{config.data?.env_file_exists ? t("overview.found") : t("overview.notFound")}</dd>
            <dt>{t("overview.activeMembers")}</dt>
            <dd>{activeMembers.length}</dd>
            <dt>{t("overview.github")}</dt>
            <dd>{githubEnabled ? t("overview.enabled") : t("overview.disabled")}</dd>
          </dl>
          <ScenarioDiagnosticsSummary
            checks={diagnosticsMutation.data?.checks ?? []}
            error={diagnosticsMutation.error}
            loading={diagnosticsMutation.isPending}
          />
        </Card>

        <Card withBorder radius="md" p="lg">
          <Group justify="space-between">
            <Title order={3}>{t("overview.scheduler")}</Title>
            <span className={runtimeRunning ? "status on" : "status"}>
              {runtimeRunning ? t("overview.running") : t("overview.stopped")}
            </span>
          </Group>
          <Stack mt="md">
            <Select
              label={t("overview.routine")}
              value={selectedRoutine}
              onChange={(value) => setSelectedRoutine(value ?? "")}
              data={(routines.data?.routines ?? []).map((routine: RoutineOption) => ({
                value: routine.command,
                label: routine.requires_github
                  ? `${routine.command} (${t("overview.requiresGithub")})`
                  : routine.command,
              }))}
            />
            {!canStartRoutine ? (
              <Alert color="yellow" title={t("overview.startGuardTitle")}>
                {t("overview.startGuardBody")}
              </Alert>
            ) : null}
            <Group>
              <Button
                leftSection={<Play size={16} />}
                loading={startMutation.isPending}
                disabled={!canStartRoutine}
                onClick={() => startMutation.mutate()}
              >
                {t("overview.start")}
              </Button>
              <Button
                leftSection={<Square size={16} />}
                loading={stopMutation.isPending}
                variant="default"
                onClick={() => stopMutation.mutate()}
              >
                {t("overview.stop")}
              </Button>
            </Group>
            {startMutation.error ? (
              <Alert color="red" title={t("overview.startError")}>
                {startMutation.error.message}
              </Alert>
            ) : null}
            <pre>{JSON.stringify(scheduler.data, null, 2)}</pre>
          </Stack>
        </Card>
      </div>

      <Card withBorder radius="md" p="lg">
        <Title order={3}>{t("overview.runtimeFeed")}</Title>
        <Tabs defaultValue="events" mt="md">
          <Tabs.List>
            <Tabs.Tab value="events">{t("overview.events")}</Tabs.Tab>
            <Tabs.Tab value="logs">{t("overview.logs")}</Tabs.Tab>
          </Tabs.List>
          <Tabs.Panel value="events" pt="md">
            <div className="event-list">
              {runtimeEvents.map((event, index) => (
                <div className="event-row" key={`${event.timestamp}-${event.type}-${index}`}>
                  <span>{event.type}</span>
                  <p>{formatCommandEvent(event)}</p>
                </div>
              ))}
            </div>
          </Tabs.Panel>
          <Tabs.Panel value="logs" pt="md">
            <div className="event-list">
              {runtimeLogs.map((log, index) => (
                <div className="event-row" key={`${log.timestamp}-${log.level}-${index}`}>
                  <span>{log.level}</span>
                  <p>{log.message}</p>
                </div>
              ))}
            </div>
          </Tabs.Panel>
        </Tabs>
      </Card>
    </Stack>
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

function CommandsPage() {
  const { t } = useTranslation();
  const config = useQuery({ queryKey: ["config"], queryFn: getConfigStatus });
  const team = useQuery({ queryKey: ["team"], queryFn: getTeam, retry: false });
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
  const [command, setCommand] = useState(TICKET_ROUTINE);
  const [message, setMessage] = useState("");
  const [person, setPerson] = useState<string | null>(null);
  const [runtimeEvents, setRuntimeEvents] = useState<RuntimeEvent[]>([]);
  const [runtimeLogs, setRuntimeLogs] = useState<RuntimeLog[]>([]);
  const commandRequiresGithub = command.trim() === TICKET_ROUTINE;
  const commandBlockedByGithub = commandRequiresGithub && !githubEnabled;
  const runMutation = useMutation({
    mutationFn: () =>
      runCommand({
        command,
        message,
        person: person || undefined,
      }),
  });

  useEffect(() => {
    const stopEvents = subscribeEvents((event) => {
      if (!event.type.startsWith("command.")) {
        return;
      }
      setRuntimeEvents((current) => [event, ...current].slice(0, 80));
    });
    const stopLogs = subscribeLogs((log) => {
      setRuntimeLogs((current) => [log, ...current].slice(0, 80));
    });
    return () => {
      stopEvents();
      stopLogs();
    };
  }, []);

  const activeMembers = useMemo(
    () => (team.data?.members ?? []).filter((member) => member.is_active),
    [team.data?.members],
  );
  const commandEvents = useMemo(
    () => runtimeEvents.filter((event) => event.type.startsWith("command.")),
    [runtimeEvents],
  );

  return (
    <Stack gap="lg">
      <Group justify="space-between">
        <div>
          <Text className="eyebrow">{t("commands.eyebrow")}</Text>
          <Title order={2}>{t("commands.title")}</Title>
        </div>
        <Button
          leftSection={<Play size={16} />}
          loading={runMutation.isPending}
          disabled={commandBlockedByGithub}
          onClick={() => runMutation.mutate()}
        >
          {t("commands.run")}
        </Button>
      </Group>

      {commandBlockedByGithub ? (
        <Alert color="yellow" title={t("commands.githubRequiredTitle")}>
          {t("commands.githubRequiredBody")}
        </Alert>
      ) : null}

      <Card withBorder radius="md" p="lg">
        <Stack>
          <Select
            label={t("commands.member")}
            placeholder={t("commands.memberPlaceholder")}
            clearable
            value={person}
            onChange={setPerson}
            data={activeMembers.map((member) => ({
              value: member.person_id,
              label: `${member.name} (${member.person_id})`,
            }))}
          />
          <TextInput
            label={t("commands.command")}
            value={command}
            onChange={(event) => setCommand(event.currentTarget.value)}
          />
          <Textarea
            label={t("commands.message")}
            minRows={5}
            value={message}
            onChange={(event) => setMessage(event.currentTarget.value)}
          />
          <pre>
            {runMutation.data
              ? `${runMutation.data.request_id}\n${runMutation.data.output}`
              : runMutation.error?.message ?? ""}
          </pre>
          <Tabs defaultValue="events">
            <Tabs.List>
              <Tabs.Tab value="events">{t("commands.events")}</Tabs.Tab>
              <Tabs.Tab value="logs">{t("commands.logs")}</Tabs.Tab>
            </Tabs.List>
            <Tabs.Panel value="events" pt="md">
              <div className="event-list">
                {commandEvents.map((event, index) => (
                  <div className="event-row" key={`${event.timestamp}-${event.type}-${index}`}>
                    <span>{event.type.replace("command.", "")}</span>
                    <p>{formatCommandEvent(event)}</p>
                  </div>
                ))}
              </div>
            </Tabs.Panel>
            <Tabs.Panel value="logs" pt="md">
              <div className="event-list">
                {runtimeLogs.map((log, index) => (
                  <div className="event-row" key={`${log.timestamp}-${log.level}-${index}`}>
                    <span>{log.level}</span>
                    <p>{log.message}</p>
                  </div>
                ))}
              </div>
            </Tabs.Panel>
          </Tabs>
        </Stack>
      </Card>
    </Stack>
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
