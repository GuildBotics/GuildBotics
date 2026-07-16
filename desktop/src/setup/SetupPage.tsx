import {
  Avatar,
  FileButton,
  ActionIcon,
  Accordion,
  Alert,
  Anchor,
  Badge,
  Box,
  Button,
  Card,
  Divider,
  Fieldset,
  Group,
  HoverCard,
  Indicator,
  Modal,
  MultiSelect,
  NumberInput,
  PasswordInput,
  Popover,
  Progress,
  Select,
  SegmentedControl,
  Stack,
  Switch,
  TagsInput,
  Tabs,
  Text,
  TextInput,
  Textarea,
  ThemeIcon,
  Title,
  Tooltip,
} from "@mantine/core";
import { useForm, type UseFormReturnType } from "@mantine/form";
import { notifications } from "@mantine/notifications";
import { zodResolver } from "mantine-form-zod-resolver";
import {
  Check,
  CheckCircle2,
  CircleAlert,
  Copy,
  Eraser,
  FileKey,
  Folder,
  FolderOpen,
  Plus,
  Save,
  Trash2,
  TriangleAlert,
  WandSparkles,
  XCircle,
} from "lucide-react";
import type { TFunction } from "i18next";
import type { ReactNode } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { z } from "zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import {
  type CommandOption,
  type DiagnosticCheck,
  type CliAgentDetection,
  type CliAgentDefinition,
  type ConfigStatus,
  type BrainAssignment,
  type IntelligenceConfig,
  type NativeAgentPolicySettings,
  type ModelDefinition,
  type MemberSetupRequest,
  type ChatParticipationPolicy,
  type MemberConfig,
  type LaneMap,
  type MemberConfigUpdateRequest,
  type MemberTaskSchedule,
  type RoleOption,
  type ProjectConfig,
  type ProjectConfigUpdateRequest,
  type ProjectSetupRequest,
  ApiRequestError,
  addMemberConfig,
  type AgentFieldState,
  deleteMemberConfig,
  ensureAgentField,
  getAgentFieldState,
  getCliAgentDetections,
  getCommandOptions,
  getRoutineCommandOptions,
  getConfigStatus,
  getIntelligenceConfig,
  getLlmProviders,
  type LlmProviderInfo,
  getMemberConfig,
  getProjectConfig,
  getProjectStatusOptions,
  type ProjectStatusOptionsRequest,
  getRoleOptions,
  getTeam,
  initConfig,
  resolveMemberIdentity,
  runScenarioDiagnostics,
  stopScheduler,
  updateMemberConfig,
  updateIntelligenceConfig,
  updateProjectConfig,
  memberAvatarUrl,
  uploadMemberAvatar,
  importAvatarFromGithub,
  importAvatarFromSlack,
} from "../api/client";
import {
  type CliAgentSkillState,
  type CliAgentSkillStatusesResponse,
  forceUpdateCliAgentSkill,
  getCliAgentSkillStatuses,
  restartBackend,
} from "../api/backend";
import { cliAgentLabelFromConfig, useMemberCliAgentLabel } from "../cliAgent";
import { normalizeLanguage } from "../i18n";

export function createProjectSchema(t: TFunction | ((key: string) => string)) {
  return z
    .object({
      workspaceDir: z.string().min(1, t("setup.validation.workspaceRequired")),
      envFileOption: z.enum(["skip", "append", "overwrite"]),
      language: z.enum(["en", "ja"]),
      description: z.string().trim().min(1, t("setup.validation.descriptionRequired")),
      llmApiType: z.string(),
      cliAgent: z.string(),
      // provider id -> API key value typed in the form (server maps to env vars)
      providerApiKeys: z.record(z.string(), z.string()),
      githubDecision: z.enum(["", "disabled", "enabled"]),
      githubEnabled: z.boolean(),
      githubProjectUrl: z.string(),
      laneReady: z.string(),
      laneWorking: z.string(),
      laneDone: z.string(),
    })
    .superRefine((values, ctx) => {
      if (!values.githubDecision) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["githubDecision"],
          message: t("setup.validation.githubDecisionRequired"),
        });
        return;
      }
      if (values.githubDecision !== "enabled") {
        return;
      }
      const githubErrors = getGitHubFieldErrors(values, t);
      if (githubErrors.githubProjectUrl) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["githubProjectUrl"],
          message: githubErrors.githubProjectUrl,
        });
      }
      const ready = (values.laneReady || DEFAULT_LANE_READY).trim();
      const done = (values.laneDone || DEFAULT_LANE_DONE).trim();
      if (ready === done) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["laneDone"],
          message: t("setup.validation.laneReadyDoneSame"),
        });
      }
    });
}

export const DEFAULT_LANE_READY = "Todo";
export const DEFAULT_LANE_WORKING = "In Progress";
export const DEFAULT_LANE_DONE = "Done";

type ProjectFormValues = z.infer<ReturnType<typeof createProjectSchema>>;
type ProjectForm = UseFormReturnType<ProjectFormValues>;
type LlmProviderAvailability = Record<string, boolean>;
type IntelligenceDraftState = {
  key: string;
  config: IntelligenceConfig;
  savedSerialized: string;
};
const CORE_SETUP_SECTIONS_INITIAL = ["project", "intelligence", "members", "github"] as const;
const CORE_SETUP_SECTIONS_CONFIGURED = ["project", "intelligence", "members", "github"] as const;
type CoreSection = (typeof CORE_SETUP_SECTIONS_CONFIGURED)[number];
function MemberCliAgentBadge({ personId, enabled }: { personId: string; enabled: boolean }) {
  const label = useMemberCliAgentLabel(personId, enabled);
  if (!label) {
    return null;
  }
  return (
    <Badge variant="light" color="neutral" style={{ flexShrink: 0 }}>
      {label}
    </Badge>
  );
}
const SPEAKING_STYLE_OPTIONS = ["friendly", "professional", "energetic"] as const;
type SpeakingStylePreset = (typeof SPEAKING_STYLE_OPTIONS)[number];
const MASKED_SECRET_PLACEHOLDER = "••••••••••••";

const MEMBER_TYPE_OPTIONS = ["agent", "human"] as const;
type MemberType = (typeof MEMBER_TYPE_OPTIONS)[number];
const GITHUB_ACCOUNT_TYPE_OPTIONS = [
  "none",
  "human",
  "machine_user",
  "github_apps",
  "proxy_agent",
] as const;
const CHAT_PARTICIPATION_OPTIONS = ["social", "strict", "muted"] as const;
type GitHubAccountType = (typeof GITHUB_ACCOUNT_TYPE_OPTIONS)[number];
type GitHubMemberType = Exclude<GitHubAccountType, "none">;
type MemberEditorTab = "basic" | "intelligence" | "patrol" | "github" | "slack" | "diagnostics";
const MEMBER_EDITOR_TABS = new Set<MemberEditorTab>([
  "basic",
  "intelligence",
  "patrol",
  "github",
  "slack",
  "diagnostics",
]);
type CronPreset = "hourly" | "daily" | "weekly" | "custom";
export type ScheduledCommandDraft = {
  id: string;
  commandMode: "catalog" | "custom";
  command: string;
  customCommand: string;
  argValues: Record<string, string>;
  rawArgs: string;
  scheduleMode: CronPreset;
  minute: number;
  hour: number;
  weekday: string;
  cron: string;
};
const WEEKDAY_OPTIONS = ["0", "1", "2", "3", "4", "5", "6"] as const;

export function SetupPage() {
  const { t, i18n } = useTranslation();
  const [searchParams] = useSearchParams();
  const queryClient = useQueryClient();
  const config = useQuery({ queryKey: ["config"], queryFn: getConfigStatus });
  const team = useQuery({
    queryKey: ["team"],
    queryFn: getTeam,
    retry: false,
  });
  const cliDetections = useQuery({
    queryKey: ["cli-agent-detections"],
    queryFn: getCliAgentDetections,
    retry: false,
  });
  const llmProviders = useQuery({
    queryKey: ["llm-providers"],
    queryFn: getLlmProviders,
    retry: false,
  });
  const hasExistingProject = Boolean(config.data?.project_file_exists);
  const projectConfig = useQuery({
    queryKey: ["project-config"],
    queryFn: getProjectConfig,
    enabled: hasExistingProject,
    retry: false,
  });
  const saveMutation = useMutation({
    mutationFn: async (values: ProjectFormValues) => {
      if (hasExistingProject) {
        if (!projectConfig.data) {
          throw new Error("project config has not been loaded yet");
        }
        return updateProjectConfig(toProjectUpdateRequest(values, config.data, projectConfig.data));
      }
      return initConfig(toInitialProjectSetupRequest(values, config.data));
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["config"] });
      queryClient.invalidateQueries({ queryKey: ["team"] });
      queryClient.invalidateQueries({ queryKey: ["project-config"] });
    },
  });

  const appLanguage = normalizeLanguage(i18n.resolvedLanguage ?? i18n.language) ?? "en";
  const persistedTeam = hasExistingProject ? team.data : undefined;
  const projectLanguage = normalizeLanguage(persistedTeam?.project.language_code);
  const activeMemberCount = (persistedTeam?.members ?? []).filter(
    (member) => member.is_active,
  ).length;
  const detectedCliAgentNames = useMemo(
    () =>
      new Set(
        (cliDetections.data?.agents ?? [])
          .filter((agent) => agent.detected)
          .map((agent) => agent.name),
      ),
    [cliDetections.data?.agents],
  );
  const validationSchema = useMemo(() => createProjectSchema(t), [t]);
  const initialValues = useMemo(
    () =>
      initialProjectValues(
        config.data,
        appLanguage,
        projectLanguage,
        hasExistingProject ? projectConfig.data : undefined,
      ),
    [appLanguage, config.data, hasExistingProject, projectConfig.data, projectLanguage],
  );
  const form = useForm<ProjectFormValues>({
    initialValues,
    validate: zodResolver(validationSchema),
  });
  const appliedInitialValues = useRef("");
  const serializedInitialValues = useMemo(() => JSON.stringify(initialValues), [initialValues]);
  const selectedCliAgentDetected = cliDetections.isLoading
    ? true
    : detectedCliAgentNames.has(form.values.cliAgent);
  const [section, setSection] = useState<CoreSection>(
    searchParams.get("section") === "members" ? "members" : "project",
  );
  const requestedMemberTab = searchParams.get("tab");
  const [focusMemberTab] = useState<MemberEditorTab | undefined>(
    requestedMemberTab && MEMBER_EDITOR_TABS.has(requestedMemberTab as MemberEditorTab)
      ? (requestedMemberTab as MemberEditorTab)
      : undefined,
  );
  const [focusMemberId] = useState(searchParams.get("person_id")?.trim() || undefined);
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [draftActiveMemberCount, setDraftActiveMemberCount] = useState(0);
  const [workspaceSwitching, setWorkspaceSwitching] = useState(false);
  const [pendingWorkspaceSwitch, setPendingWorkspaceSwitch] = useState("");
  const [forceWorkspaceSwitching, setForceWorkspaceSwitching] = useState(false);
  const workspaceSwitchId = useRef(0);
  const canAutosave = hasExistingProject && projectConfig.isSuccess;
  const llmProviderAvailability = useMemo(() => {
    const result: Record<string, boolean> = {};
    for (const provider of llmProviders.data ?? []) {
      result[provider.provider] = Boolean(
        (form.values.providerApiKeys[provider.provider] ?? "").trim() ||
        projectConfig.data?.provider_api_keys?.[provider.provider],
      );
    }
    return result;
  }, [llmProviders.data, form.values.providerApiKeys, projectConfig.data?.provider_api_keys]);
  const effectiveActiveMemberCount = hasExistingProject
    ? activeMemberCount
    : draftActiveMemberCount;
  const coreSections: readonly CoreSection[] = hasExistingProject
    ? CORE_SETUP_SECTIONS_CONFIGURED
    : CORE_SETUP_SECTIONS_INITIAL;
  const initialProgress = useMemo(
    () => getInitialCoreStatus(form.values, effectiveActiveMemberCount, selectedCliAgentDetected),
    [effectiveActiveMemberCount, form.values, selectedCliAgentDetected],
  );
  const activeSection = coreSections.includes(section) ? section : coreSections[0];
  const currentCoreSectionIndex = coreSections.indexOf(activeSection);
  const currentCoreSection =
    currentCoreSectionIndex >= 0 ? coreSections[currentCoreSectionIndex] : null;
  const canGoBack = currentCoreSectionIndex > 0;
  const canGoNext =
    currentCoreSectionIndex >= 0 && currentCoreSectionIndex < coreSections.length - 1;
  const goBackSection = () => {
    if (!canGoBack) {
      return;
    }
    setSection(coreSections[currentCoreSectionIndex - 1]);
  };
  const goNextSection = () => {
    if (!canGoNext) {
      return;
    }
    setSection(coreSections[currentCoreSectionIndex + 1]);
  };

  useEffect(() => {
    if (appliedInitialValues.current === serializedInitialValues) {
      return;
    }
    appliedInitialValues.current = serializedInitialValues;
    form.setValues(initialValues);
    form.resetDirty(initialValues);
  }, [form, initialValues, serializedInitialValues]);

  useAutosave(
    form,
    config.data,
    validationSchema,
    saveMutation.mutateAsync,
    setSaveState,
    canAutosave && !workspaceSwitching,
  );

  const setupStatus = useSetupStatus(config.data, effectiveActiveMemberCount, form.values);
  const visibleStatus = hasExistingProject ? setupStatus : initialProgress;
  const currentSectionReady = currentCoreSection
    ? isCoreSectionReady(currentCoreSection, visibleStatus)
    : true;
  const saveNow = async () => {
    if (form.validate().hasErrors) {
      setSaveState("error");
      return;
    }
    const creatingInitialSetup = !hasExistingProject;
    const initialSetupRequest = creatingInitialSetup
      ? toInitialProjectSetupRequest(form.values, config.data)
      : null;
    setSaveState("saving");
    try {
      await saveMutation.mutateAsync(form.values);
      localStorage.setItem("guildbotics.workspace", form.values.workspaceDir);
      if (creatingInitialSetup) {
        await restartBackend(form.values.workspaceDir);
        await Promise.all([
          queryClient.refetchQueries({ queryKey: ["config"] }),
          queryClient.refetchQueries({ queryKey: ["team"] }),
        ]);
      }
      form.resetDirty(form.values);
      setSaveState("saved");
      if (initialSetupRequest) {
        notifications.show({
          autoClose: false,
          color: "success",
          icon: <Check size={18} />,
          title: t("setup.initialCreated.title"),
          message: (
            <Text size="sm" style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
              {t("setup.initialCreated.body", {
                configDir: initialSetupRequest.config_dir,
                envFilePath: initialSetupRequest.env_file_path,
              })}
            </Text>
          ),
        });
      }
    } catch {
      setSaveState("error");
    }
  };
  const applyWorkspaceSwitch = async (workspace: string, switchId: number) => {
    await restartBackend(workspace);
    if (workspaceSwitchId.current !== switchId) {
      return;
    }
    setDraftActiveMemberCount(0);
    queryClient.setQueryData(["team"], undefined);
    queryClient.setQueryData(["project-config"], undefined);
    queryClient.invalidateQueries({ queryKey: ["project-config"] });
    queryClient.invalidateQueries({ queryKey: ["intelligence-config"] });
    queryClient.invalidateQueries({ queryKey: ["command-options"] });
    queryClient.invalidateQueries({ queryKey: ["scheduler"] });
    await Promise.all([
      queryClient.refetchQueries({ queryKey: ["config"] }),
      queryClient.refetchQueries({ queryKey: ["team"] }),
    ]);
    await queryClient.refetchQueries({ queryKey: ["project-config"] });
    setSaveState("saved");
  };
  const startWorkspaceSwitch = (workspace: string) => {
    const switchId = workspaceSwitchId.current + 1;
    workspaceSwitchId.current = switchId;
    setWorkspaceSwitching(true);
    setSaveState("saving");
    void applyWorkspaceSwitch(workspace, switchId)
      .catch((error: unknown) => {
        if (workspaceSwitchId.current === switchId) {
          if (isWorkspaceSwitchBlocked(error)) {
            setPendingWorkspaceSwitch(workspace);
            setSaveState("idle");
          } else {
            setSaveState("error");
          }
        }
      })
      .finally(() => {
        if (workspaceSwitchId.current === switchId) {
          setWorkspaceSwitching(false);
        }
      });
  };
  const changeWorkspace = (value: string) => {
    form.setFieldValue("workspaceDir", value);
    const workspace = value.trim();
    if (workspace) {
      startWorkspaceSwitch(workspace);
    }
  };
  const dismissPendingWorkspaceSwitch = () => {
    setPendingWorkspaceSwitch("");
    const currentWorkspace = config.data?.cwd ?? "";
    if (currentWorkspace) {
      form.setFieldValue("workspaceDir", currentWorkspace);
      localStorage.setItem("guildbotics.workspace", currentWorkspace);
    }
  };
  const forcePendingWorkspaceSwitch = async () => {
    const workspace = pendingWorkspaceSwitch;
    if (!workspace) {
      return;
    }
    const switchId = workspaceSwitchId.current + 1;
    workspaceSwitchId.current = switchId;
    setForceWorkspaceSwitching(true);
    setWorkspaceSwitching(true);
    setSaveState("saving");
    try {
      await stopScheduler({ force: true });
      await applyWorkspaceSwitch(workspace, switchId);
      setPendingWorkspaceSwitch("");
    } catch {
      if (workspaceSwitchId.current === switchId) {
        setSaveState("error");
      }
    } finally {
      if (workspaceSwitchId.current === switchId) {
        setWorkspaceSwitching(false);
      }
      setForceWorkspaceSwitching(false);
    }
  };

  return (
    <Stack gap="lg">
      <Group justify="space-between" align="flex-start">
        <Box>
          <Title order={2}>
            {hasExistingProject ? t("setup.configuredTitle") : t("setup.title")}
          </Title>
          {!hasExistingProject && (
            <Text size="sm" c="dimmed" mt={4}>
              {t("setup.saveMode.manual")}
            </Text>
          )}
        </Box>
      </Group>

      <SetupStatusBanner
        status={visibleStatus}
        hasExistingProject={hasExistingProject}
        initialProgress={initialProgress}
        onCreateInitial={saveNow}
        creating={saveMutation.isPending}
        canGoBack={canGoBack}
        canGoNext={canGoNext}
        currentSectionReady={currentSectionReady}
        onGoBack={goBackSection}
        onGoNext={goNextSection}
      />

      <div className="setup-layout">
        <SetupSectionNav active={activeSection} onChange={setSection} status={visibleStatus} />
        <Stack gap="md">
          {activeSection === "project" ? (
            <ProjectSection
              form={form}
              saveState={saveState}
              autosaveEnabled={canAutosave}
              onWorkspaceChange={changeWorkspace}
            />
          ) : null}
          {activeSection === "intelligence" ? (
            <IntelligenceSection
              form={form}
              saveState={saveState}
              autosaveEnabled={canAutosave}
              detections={cliDetections.data?.agents ?? []}
              detectionLoading={cliDetections.isLoading}
              projectConfig={projectConfig.data}
              providers={llmProviders.data ?? []}
            />
          ) : null}
          {activeSection === "github" ? <GitHubIntegrationSection form={form} /> : null}
          {activeSection === "members" ? (
            <MembersSection
              activeMemberCount={effectiveActiveMemberCount}
              members={persistedTeam?.members ?? []}
              config={config.data}
              workspaceDir={form.values.workspaceDir}
              projectGithubEnabled={form.values.githubDecision === "enabled"}
              cliDetections={cliDetections.data?.agents ?? []}
              llmProviderAvailability={llmProviderAvailability}
              providers={llmProviders.data ?? []}
              initialTab={focusMemberTab}
              initialMemberId={focusMemberId}
              onMemberActiveDelta={(delta) => {
                if (!hasExistingProject && delta !== 0) {
                  setDraftActiveMemberCount((count) => Math.max(0, count + delta));
                }
                queryClient.invalidateQueries({ queryKey: ["team"] });
              }}
            />
          ) : null}
        </Stack>
      </div>

      {saveMutation.error ? (
        <Alert color="danger" title={t("setup.saveErrorTitle")}>
          {saveMutation.error.message}
        </Alert>
      ) : null}
      <Modal
        centered
        opened={Boolean(pendingWorkspaceSwitch)}
        onClose={dismissPendingWorkspaceSwitch}
        title={t("setup.workspaceSwitchBlocked.title")}
      >
        <Stack gap="md">
          <Text size="sm">{t("setup.workspaceSwitchBlocked.body")}</Text>
          <Group justify="flex-end">
            <Button variant="default" onClick={dismissPendingWorkspaceSwitch}>
              {t("setup.workspaceSwitchBlocked.ok")}
            </Button>
            <Button
              color="danger"
              loading={forceWorkspaceSwitching}
              onClick={() => void forcePendingWorkspaceSwitch()}
            >
              {t("setup.workspaceSwitchBlocked.force")}
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Stack>
  );
}

function SetupStatusBanner({
  status,
  hasExistingProject,
  initialProgress,
  onCreateInitial,
  creating,
  canGoBack,
  canGoNext,
  currentSectionReady,
  onGoBack,
  onGoNext,
}: {
  status: SetupStatus;
  hasExistingProject: boolean;
  initialProgress: InitialProgress;
  onCreateInitial: () => Promise<void>;
  creating: boolean;
  canGoBack: boolean;
  canGoNext: boolean;
  currentSectionReady: boolean;
  onGoBack: () => void;
  onGoNext: () => void;
}) {
  const { t } = useTranslation();
  if (!hasExistingProject) {
    return (
      <Card withBorder radius="md" p="md" className="guide-banner">
        <Group justify="space-between" wrap="nowrap">
          <Box>
            <Text fw={700}>{t("setup.status.inputProgressTitle", initialProgress)}</Text>
            <Text size="sm" c="dimmed">
              {t("setup.status.inputProgressMessage")}
            </Text>
          </Box>
          <Progress value={initialProgress.percent} w={220} />
        </Group>
        <Group justify="flex-end" mt="sm">
          {initialProgress.ready ? (
            <Button
              leftSection={<Save size={16} />}
              loading={creating}
              onClick={() => void onCreateInitial()}
            >
              {t("setup.saveInitial")}
            </Button>
          ) : (
            <>
              <Button variant="default" disabled={!canGoBack} onClick={onGoBack}>
                {t("setup.status.back")}
              </Button>
              <Button
                variant="default"
                disabled={!canGoNext || !currentSectionReady}
                onClick={onGoNext}
              >
                {t("setup.status.next")}
              </Button>
            </>
          )}
        </Group>
      </Card>
    );
  }

  if (status.ready) {
    return (
      <Alert color="success" icon={<Check size={18} />} title={t("setup.status.readyTitle")}>
        {t("setup.status.readyMessage")}
      </Alert>
    );
  }
  return (
    <Card withBorder radius="md" p="md" className="guide-banner">
      <Group justify="space-between" wrap="nowrap">
        <Box>
          <Text fw={700}>{t("setup.status.progressTitle", status)}</Text>
          <Text size="sm" c="dimmed">
            {t("setup.status.progressMessage")}
          </Text>
        </Box>
        <Progress value={(status.done / status.total) * 100} w={220} />
      </Group>
    </Card>
  );
}

function isWorkspaceSwitchBlocked(error: unknown): boolean {
  return (
    error instanceof ApiRequestError && error.code === "workspace_switch_blocked_by_active_work"
  );
}

function SetupSectionNav({
  active,
  onChange,
  status,
}: {
  active: string;
  onChange: (value: CoreSection) => void;
  status: SetupStatus;
}) {
  const { t } = useTranslation();
  const items: Array<readonly [CoreSection, string, boolean]> = [
    ["project", t("setup.nav.project"), status.projectReady],
    ["intelligence", t("setup.nav.intelligence"), status.intelligenceReady],
    ["members", t("setup.nav.members"), status.membersReady],
    ["github", t("setup.nav.github"), status.githubReady],
  ];
  return (
    <Card withBorder radius="md" p="xs" className="setup-nav">
      {items.map(([value, label, ok]) => (
        <button
          className={`setup-nav-item ${active === value ? "active" : ""}`}
          key={value}
          type="button"
          onClick={() => onChange(value)}
        >
          <StatusIcon ok={ok} />
          <span>{label}</span>
        </button>
      ))}
    </Card>
  );
}

function StatusIcon({ ok }: { ok: boolean }) {
  return ok ? (
    <ThemeIcon color="success" radius="xl" size={22}>
      <Check size={14} />
    </ThemeIcon>
  ) : (
    <ThemeIcon color="warning" radius="xl" size={22}>
      <CircleAlert size={14} />
    </ThemeIcon>
  );
}

function ProjectSection({
  form,
  saveState,
  autosaveEnabled,
  onWorkspaceChange,
}: {
  form: ProjectForm;
  saveState: "idle" | "saving" | "saved" | "error";
  autosaveEnabled: boolean;
  onWorkspaceChange: (value: string) => void;
}) {
  const { t } = useTranslation();
  return (
    <Card withBorder radius="md" p="lg">
      <PanelHeader
        title={t("setup.project.title")}
        subtitle={t("setup.project.subtitle")}
        saveState={autosaveEnabled ? saveState : undefined}
      />
      <Stack mt="md">
        <LabeledSegmentedControl
          label={t("setup.project.agentLanguage")}
          description={t("setup.project.agentLanguageDescription")}
          data={[
            { label: t("app.language.english"), value: "en" },
            { label: t("app.language.japanese"), value: "ja" },
          ]}
          value={form.values.language}
          onChange={(value) =>
            form.setFieldValue("language", value as ProjectFormValues["language"])
          }
        />
        <FolderPicker value={form.values.workspaceDir} onChange={onWorkspaceChange} />
        <Textarea
          label={<RequiredLabel text={t("setup.project.description")} />}
          aria-label={t("setup.project.description")}
          aria-required
          description={t("setup.project.descriptionHint")}
          autosize
          minRows={2}
          {...form.getInputProps("description")}
        />
        <Select
          label={<RequiredLabel text={t("setup.github.decision")} />}
          aria-label={t("setup.github.decision")}
          aria-required
          description={t("setup.github.decisionHint")}
          placeholder={t("setup.github.decisionPlaceholder")}
          data={[
            { value: "disabled", label: t("setup.github.disabled") },
            { value: "enabled", label: t("setup.github.enabled") },
          ]}
          value={form.values.githubDecision || null}
          onChange={(value) => {
            const decision = (value ?? "") as ProjectFormValues["githubDecision"];
            form.setFieldValue("githubDecision", decision);
            form.setFieldValue("githubEnabled", decision === "enabled");
          }}
          error={form.errors.githubDecision}
        />
      </Stack>
    </Card>
  );
}

function IntelligenceSection({
  form,
  saveState,
  autosaveEnabled,
  detections,
  detectionLoading,
  projectConfig,
  providers,
}: {
  form: ProjectForm;
  saveState: "idle" | "saving" | "saved" | "error";
  autosaveEnabled: boolean;
  detections: CliAgentDetection[];
  detectionLoading: boolean;
  projectConfig: ProjectConfig | undefined;
  providers: LlmProviderInfo[];
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  const detectedCliAgents = useMemo(
    () => new Set(detections.filter((agent) => agent.detected).map((agent) => agent.name)),
    [detections],
  );
  const skillStatuses = useQuery({
    queryKey: ["cli-agent-skill-statuses"],
    queryFn: getCliAgentSkillStatuses,
  });
  const forceSkillUpdate = useMutation({
    mutationFn: forceUpdateCliAgentSkill,
    onSuccess: (updated) => {
      queryClient.setQueryData<CliAgentSkillStatusesResponse>(
        ["cli-agent-skill-statuses"],
        (current) => ({
          agents: upsertByAgent(current?.agents ?? [], updated),
          error: current?.error,
        }),
      );
      notifications.show({
        color: "success",
        title: t("setup.intelligence.skillUpdatedTitle"),
        message: t("setup.intelligence.skillUpdatedBody"),
      });
    },
    onError: (error) => {
      notifications.show({
        color: "danger",
        title: t("setup.intelligence.skillUpdateFailedTitle"),
        message:
          error instanceof Error ? error.message : t("setup.intelligence.skillUpdateFailedBody"),
      });
    },
  });
  useEffect(() => {
    if (detectionLoading) {
      return;
    }
    if (detectedCliAgents.size === 0) {
      return;
    }
    if (detectedCliAgents.has(form.values.cliAgent)) {
      return;
    }
    const fallback = detections.find((agent) => agent.detected);
    if (fallback) {
      form.setFieldValue("cliAgent", fallback.name);
    }
  }, [detectionLoading, detectedCliAgents, detections, form]);
  return (
    <Card withBorder radius="md" p="lg">
      <PanelHeader
        title={t("setup.intelligence.title")}
        subtitle={t("setup.intelligence.subtitle")}
        saveState={autosaveEnabled ? saveState : undefined}
        badge={t("setup.intelligence.teamDefault")}
      />
      <Stack mt="md" gap="md">
        {/* LLM Settings Section */}
        <Card withBorder radius="sm" p="md">
          <Stack gap="xs">
            <Text size="sm" fw={700}>
              {t("setup.intelligence.defaultProvider")}
            </Text>
            <Text size="sm" c="dimmed">
              {t("setup.intelligence.providerDescription")}
            </Text>
            <div className="option-card-grid">
              {providers.map((option) => {
                const available =
                  (form.values.providerApiKeys[option.provider] ?? "").trim().length > 0 ||
                  Boolean(projectConfig?.provider_api_keys?.[option.provider]);
                const active = form.values.llmApiType === option.provider;

                const cardElement = (
                  <div
                    style={{
                      position: "relative",
                      display: "block",
                      width: "100%",
                    }}
                  >
                    <button
                      type="button"
                      aria-label={option.label}
                      disabled={!available}
                      className={`option-card ${active ? "active" : ""}`}
                      style={{
                        background: available ? "#fff" : "#f7f9f8",
                        color: available ? "#202124" : "#7f8b85",
                        cursor: available ? "pointer" : "default",
                        paddingRight: "40px",
                        width: "100%",
                        textAlign: "left",
                      }}
                      onClick={() => {
                        if (available) {
                          form.setFieldValue("llmApiType", option.provider);
                        }
                      }}
                    >
                      <span className="title" style={{ userSelect: "none" }}>
                        {option.label}
                      </span>
                      <span
                        className={`detection ${available ? "ok" : "ng"}`}
                        style={{ userSelect: "none" }}
                      >
                        <i />
                        {available
                          ? t("setup.intelligence.apiKeyConfigured")
                          : t("setup.intelligence.apiKeyMissing")}
                      </span>
                    </button>
                    <div
                      style={{
                        position: "absolute",
                        top: "10px",
                        right: "10px",
                      }}
                      onClick={(e) => e.stopPropagation()}
                    >
                      <Popover position="bottom" withArrow shadow="md" trapFocus>
                        <Popover.Target>
                          <ActionIcon
                            variant="subtle"
                            color="neutral"
                            size="sm"
                            aria-label={t("setup.intelligence.apiKeyButtonLabel", {
                              provider: option.label,
                            })}
                            style={{ fontSize: "12px" }}
                          >
                            🔑
                          </ActionIcon>
                        </Popover.Target>
                        <Popover.Dropdown p="md" w={340}>
                          <PasswordInput
                            size="sm"
                            label={t("setup.intelligence.apiKeyLabel", { provider: option.label })}
                            description={
                              projectConfig?.provider_api_keys?.[option.provider]
                                ? t("setup.intelligence.keyConfiguredDescription")
                                : undefined
                            }
                            placeholder={
                              projectConfig?.provider_api_keys?.[option.provider]
                                ? MASKED_SECRET_PLACEHOLDER
                                : t("setup.intelligence.keyPlaceholder")
                            }
                            value={form.values.providerApiKeys[option.provider] ?? ""}
                            onChange={(event) =>
                              form.setFieldValue("providerApiKeys", {
                                ...form.values.providerApiKeys,
                                [option.provider]: event.currentTarget.value,
                              })
                            }
                          />
                        </Popover.Dropdown>
                      </Popover>
                    </div>
                  </div>
                );

                return (
                  <Tooltip
                    key={option.provider}
                    label={t("setup.intelligence.apiKeyMissingTooltip")}
                    position="top"
                    withArrow
                    disabled={available}
                  >
                    {cardElement}
                  </Tooltip>
                );
              })}
            </div>
          </Stack>
        </Card>

        {/* CLI Settings Section */}
        <Card withBorder radius="sm" p="md">
          <Stack gap="xs">
            <Text size="sm" fw={700}>
              {t("setup.intelligence.defaultCliAgent")}
            </Text>
            <Text size="sm" c="dimmed">
              {t("setup.intelligence.cliHint")}
            </Text>
            <div className="option-card-grid">
              {detections.map((agent) => {
                const detected = detectionLoading
                  ? form.values.cliAgent === agent.name
                  : agent.detected;
                const active = form.values.cliAgent === agent.name;

                const status = (skillStatuses.data?.agents ?? []).find(
                  (s) => s.agent === agent.name,
                );
                const statusKey = status?.status ?? "agent_home_missing";
                const canForceUpdate = Boolean(status?.can_force_update);

                const cardElement = (
                  <div
                    style={{
                      position: "relative",
                      display: "block",
                      width: "100%",
                    }}
                  >
                    <button
                      type="button"
                      aria-label={agent.label}
                      disabled={!detected}
                      className={`option-card ${active ? "active" : ""}`}
                      style={{
                        background: detected ? "#fff" : "#f7f9f8",
                        color: detected ? "#202124" : "#7f8b85",
                        cursor: detected ? "pointer" : "default",
                        paddingRight: "40px",
                        width: "100%",
                        textAlign: "left",
                      }}
                      onClick={() => {
                        if (detected) {
                          form.setFieldValue("cliAgent", agent.name);
                        }
                      }}
                    >
                      <span className="title" style={{ userSelect: "none" }}>
                        {agent.label}
                      </span>
                      <span
                        className={`detection ${detected ? "ok" : "ng"}`}
                        style={{ userSelect: "none" }}
                      >
                        <i />
                        {detected
                          ? t("setup.intelligence.detected")
                          : t("setup.intelligence.notDetected")}
                      </span>
                    </button>
                    {detected ? (
                      <div
                        style={{
                          position: "absolute",
                          top: "10px",
                          right: "10px",
                        }}
                        onClick={(e) => e.stopPropagation()}
                      >
                        <HoverCard
                          width={340}
                          shadow="md"
                          withArrow
                          openDelay={200}
                          closeDelay={400}
                        >
                          <HoverCard.Target>
                            <Indicator
                              disabled={!canForceUpdate}
                              color="info"
                              size={6}
                              offset={3}
                              processing
                            >
                              <ActionIcon
                                variant="subtle"
                                color="neutral"
                                size="sm"
                                aria-label={t("setup.intelligence.skillStatusButtonLabel", {
                                  agent: agent.label,
                                })}
                                style={{ fontSize: "12px" }}
                              >
                                🪄
                              </ActionIcon>
                            </Indicator>
                          </HoverCard.Target>
                          <HoverCard.Dropdown p="md">
                            <Stack gap="xs">
                              <Group justify="space-between">
                                <Text fw={700} size="sm">
                                  {t("setup.intelligence.skillStatusTitle")}
                                </Text>
                                <Badge
                                  color={skillStatusColor(statusKey)}
                                  variant="light"
                                  size="sm"
                                >
                                  {t(`setup.intelligence.skillStatusLabels.${statusKey}`)}
                                </Badge>
                              </Group>
                              <Text size="sm">
                                {t(`setup.intelligence.skillStatusMessages.${statusKey}`)}
                              </Text>
                              {status?.skill_path ? (
                                <Text
                                  size="xs"
                                  c="dimmed"
                                  className="mono-text"
                                  style={{ wordBreak: "break-all" }}
                                >
                                  {status.skill_path}
                                </Text>
                              ) : null}
                              {status?.error ? (
                                <Text size="xs" c="danger" style={{ wordBreak: "break-all" }}>
                                  {status.error}
                                </Text>
                              ) : null}
                              {canForceUpdate ? (
                                <Button
                                  size="xs"
                                  variant="light"
                                  leftSection={<WandSparkles size={14} />}
                                  loading={
                                    forceSkillUpdate.isPending &&
                                    forceSkillUpdate.variables === agent.name
                                  }
                                  onClick={() => forceSkillUpdate.mutate(agent.name)}
                                  mt="xs"
                                >
                                  {t("setup.intelligence.skillOverwrite")}
                                </Button>
                              ) : null}
                            </Stack>
                          </HoverCard.Dropdown>
                        </HoverCard>
                      </div>
                    ) : null}
                  </div>
                );

                return (
                  <Tooltip
                    key={agent.name}
                    label="このエージェントは PATH 上に検出されていません"
                    position="top"
                    withArrow
                    disabled={detected}
                  >
                    {cardElement}
                  </Tooltip>
                );
              })}
            </div>
          </Stack>
        </Card>

        {autosaveEnabled ? (
          <Accordion variant="contained">
            <Accordion.Item value="advanced-intelligence">
              <Accordion.Control>{t("setup.intelligence.advanced")}</Accordion.Control>
              <Accordion.Panel>
                <IntelligenceEditor
                  enabled={autosaveEnabled}
                  detections={detections}
                  providers={providers}
                  teamLlmApiType={form.values.llmApiType}
                  teamCliAgent={form.values.cliAgent}
                  onTeamLlmApiTypeChange={(val) => form.setFieldValue("llmApiType", val)}
                  onTeamCliAgentChange={(val) => form.setFieldValue("cliAgent", val)}
                />
              </Accordion.Panel>
            </Accordion.Item>
          </Accordion>
        ) : null}
      </Stack>
    </Card>
  );
}

function skillStatusColor(status: CliAgentSkillState["status"]) {
  if (status === "up_to_date") {
    return "success";
  }
  if (status === "user_modified" || status === "unmanaged" || status === "outdated") {
    return "warning";
  }
  if (status === "error") {
    return "danger";
  }
  return "neutral";
}

function upsertByAgent(
  statuses: CliAgentSkillState[],
  updated: CliAgentSkillState,
): CliAgentSkillState[] {
  const exists = statuses.some((status) => status.agent === updated.agent);
  if (!exists) {
    return [...statuses, updated];
  }
  return statuses.map((status) => (status.agent === updated.agent ? updated : status));
}

const DEFAULT_LLM_PROVIDER = "openai";

type ProviderDefaults = Record<string, { model_class: string; model_id: string }>;

// Per-provider default model_class/model_id, sourced from the backend provider
// catalog (`models/<provider>/default.yml`). Keeping it server-side means
// swapping a provider's default model is a data change, not a frontend change.
function providerDefaultsMap(providers: LlmProviderInfo[]): ProviderDefaults {
  return Object.fromEntries(
    providers.map((entry) => [
      entry.provider,
      { model_class: entry.model_class, model_id: entry.model_id },
    ]),
  );
}

// The backend derives the provider from the model file's directory
// (`models/<provider>/<file>.yml`), so the path alone identifies the provider.
function providerFromModelPath(path: string | undefined): string {
  return path ? (path.split("/")[1] ?? "") : "";
}

// Each slot owns a distinct model file keyed by its slot name. Without this,
// slots that resolve to the same path would share a single model definition and
// edits to one would leak into another (e.g. the default + gemini slots).
function slotModelPath(provider: string, slotKey: string): string {
  return `models/${provider}/${slotKey}.yml`;
}

function providerDefaultModel(
  provider: string,
  slotKey: string,
  defaults: ProviderDefaults,
): ModelDefinition {
  const def = defaults[provider];
  return {
    path: slotModelPath(provider, slotKey),
    provider,
    model_class: def?.model_class ?? "",
    model_id: def?.model_id ?? "",
  };
}

// Assigns `model` to `slotKey` and drops any model definition no longer
// referenced by a slot, keeping the slot ↔ model relationship one-to-one.
function withSlotModel(
  current: IntelligenceConfig,
  slotKey: string,
  model: ModelDefinition,
): IntelligenceConfig {
  const model_mapping = { ...current.model_mapping, [slotKey]: model.path };
  const usedPaths = new Set(Object.values(model_mapping));
  const models = [
    ...current.models.filter((m) => m.path !== model.path && usedPaths.has(m.path)),
    model,
  ];
  return { ...current, model_mapping, models };
}

function NativeAgentPolicyEditor({
  policy,
  onChange,
}: {
  policy: NativeAgentPolicySettings;
  onChange: (policy: NativeAgentPolicySettings) => void;
}) {
  const { t } = useTranslation();
  const setCodex = (updates: Partial<NativeAgentPolicySettings["codex"]>) =>
    onChange({ ...policy, codex: { ...policy.codex, ...updates } });
  const filesystemOptions = ["workspace", "host"] as const;

  return (
    <Card withBorder radius="sm" p="md">
      <Stack gap="md">
        <div>
          <Text fw={700} size="sm">
            {t("setup.intelligence.nativePolicy")}
          </Text>
          <Text size="sm" c="dimmed">
            {t("setup.intelligence.nativePolicyDescription")}
          </Text>
        </div>
        <Select
          label={t("setup.intelligence.filesystemAccess")}
          data={filesystemOptions.map((value) => ({
            value,
            label: t(`setup.intelligence.filesystemOptions.${value}`),
          }))}
          value={policy.codex.filesystem_access}
          onChange={(value) =>
            setCodex({
              filesystem_access: (value ?? "workspace") as (typeof filesystemOptions)[number],
            })
          }
        />
        {policy.codex.filesystem_access === "host" ? (
          <Alert color="warning" title={t("setup.intelligence.hostAccessWarningTitle")}>
            {t("setup.intelligence.hostAccessWarningBody")}
          </Alert>
        ) : null}
      </Stack>
    </Card>
  );
}

function IntelligenceEditor({
  personId,
  savePersonId,
  enabled,
  detections,
  llmProviderAvailability,
  providers,
  saveMode = "auto",
  onRegisterSave,
  teamLlmApiType,
  teamCliAgent,
  onTeamLlmApiTypeChange,
  onTeamCliAgentChange,
}: {
  personId?: string;
  savePersonId?: string;
  enabled: boolean;
  detections: CliAgentDetection[];
  llmProviderAvailability?: LlmProviderAvailability;
  providers: LlmProviderInfo[];
  saveMode?: "auto" | "external";
  onRegisterSave?: (save: (() => Promise<void>) | null) => void;
  teamLlmApiType?: string;
  teamCliAgent?: string;
  onTeamLlmApiTypeChange?: (val: string) => void;
  onTeamCliAgentChange?: (val: string) => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const providerDefaults = useMemo(() => providerDefaultsMap(providers), [providers]);
  const isLlmProvider = (provider: string) =>
    providers.some((entry) => entry.provider === provider);
  const query = useQuery({
    queryKey: ["intelligence-config", personId ?? "team"],
    queryFn: () => getIntelligenceConfig(personId),
    enabled,
  });
  const [draftState, setDraftState] = useState<IntelligenceDraftState | null>(null);
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [envErrors, setEnvErrors] = useState<Record<string, string>>({});
  const mutation = useMutation({
    mutationFn: updateIntelligenceConfig,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["intelligence-config", personId ?? "team"] });
      queryClient.invalidateQueries({ queryKey: ["project-config"] });
    },
  });

  const querySerializedPayload = query.data
    ? JSON.stringify(toIntelligenceUpdatePayload(query.data, savePersonId))
    : "";
  const draftKey = `${personId ?? "team"}:${querySerializedPayload}`;
  const activeDraftState = draftState?.key === draftKey ? draftState : null;
  const draft = activeDraftState?.config ?? query.data ?? null;
  const payload = draft ? toIntelligenceUpdatePayload(draft, savePersonId) : null;
  const serializedPayload = payload ? JSON.stringify(payload) : "";
  const savedSerialized = activeDraftState?.savedSerialized ?? querySerializedPayload;
  const dirty = Boolean(serializedPayload && savedSerialized !== serializedPayload);
  const canSave = Boolean(payload && dirty && Object.keys(envErrors).length === 0);

  const saveDraft = useCallback(async () => {
    if (!payload || !serializedPayload || Object.keys(envErrors).length > 0) {
      return;
    }
    setSaveState("saving");
    try {
      await mutation.mutateAsync(payload);
      setDraftState((current) =>
        current?.key === draftKey ? { ...current, savedSerialized: serializedPayload } : current,
      );
      setSaveState("saved");
    } catch (error) {
      setSaveState("error");
      throw error;
    }
  }, [draftKey, envErrors, mutation, payload, serializedPayload]);

  const updateDraft = (recipe: (current: IntelligenceConfig) => IntelligenceConfig) => {
    setDraftState((current) => {
      const currentConfig = current?.key === draftKey ? current.config : query.data;
      if (!currentConfig) {
        return current;
      }
      return {
        key: draftKey,
        config: recipe(currentConfig),
        savedSerialized:
          current?.key === draftKey ? current.savedSerialized : querySerializedPayload,
      };
    });
    setSaveState("idle");
  };

  useEffect(() => {
    if (!enabled || saveMode !== "auto" || !canSave) {
      return;
    }
    const timer = window.setTimeout(() => {
      void saveDraft().catch(() => undefined);
    }, 800);
    return () => window.clearTimeout(timer);
  }, [canSave, enabled, saveDraft, saveMode]);

  useEffect(() => {
    if (saveMode !== "external" || !enabled || !onRegisterSave) {
      return;
    }
    onRegisterSave(saveDraft);
    return () => onRegisterSave(null);
  }, [enabled, onRegisterSave, saveDraft, saveMode]);

  // Sync basic settings (props) -> advanced settings (draftState)
  useEffect(() => {
    if (personId || !teamLlmApiType || !draft) return;

    // Only realign when the provider actually differs, otherwise we would reset
    // the default slot's model on every mount. The guards above keep this from
    // cascading, so reconciling the draft here is intentional.
    if (providerFromModelPath(draft.model_mapping.default) === teamLlmApiType) return;

    // eslint-disable-next-line react-hooks/set-state-in-effect -- guarded prop→draft sync
    updateDraft((current) =>
      withSlotModel(
        current,
        "default",
        providerDefaultModel(teamLlmApiType, "default", providerDefaults),
      ),
    );
  }, [teamLlmApiType, personId]);

  useEffect(() => {
    if (personId || !teamCliAgent || !draft) return;

    const currentPath = draft.cli_agent_mapping.default;
    const matchedAgent = draft.cli_agents.find((a) => a.name === teamCliAgent);
    const matchedDetection = detections.find((agent) => agent.name === teamCliAgent);
    const expectedPath = matchedAgent?.path ?? matchedDetection?.config_reference ?? teamCliAgent;

    if (currentPath !== expectedPath) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- guarded prop→draft sync
      updateDraft((current) => {
        const nextCliAgents = [...current.cli_agents];
        const exists = nextCliAgents.some((a) => a.path === expectedPath);
        if (!exists) {
          nextCliAgents.push({
            path: expectedPath,
            name: teamCliAgent,
            env: {},
            script: "",
            detected: false,
            detected_path: "",
          });
        }
        return {
          ...current,
          cli_agent_mapping: {
            ...current.cli_agent_mapping,
            default: expectedPath,
          },
          cli_agents: nextCliAgents,
        };
      });
    }
  }, [teamCliAgent, personId, detections]);

  // Sync advanced settings (draftState) -> basic settings (props callback)
  const prevDefaultModelPathRef = useRef<string | null>(null);
  useEffect(() => {
    if (personId || !draft || !onTeamLlmApiTypeChange) return;

    const currentPath = draft.model_mapping.default;
    if (prevDefaultModelPathRef.current === null) {
      prevDefaultModelPathRef.current = currentPath;
      return;
    }
    if (prevDefaultModelPathRef.current !== currentPath) {
      prevDefaultModelPathRef.current = currentPath;

      const provider = providerFromModelPath(currentPath);
      if (isLlmProvider(provider) && provider !== teamLlmApiType) {
        onTeamLlmApiTypeChange(provider);
      }
    }
  }, [draft?.model_mapping?.default, teamLlmApiType, onTeamLlmApiTypeChange, personId]);

  const prevDefaultCliPathRef = useRef<string | null>(null);
  useEffect(() => {
    if (personId || !draft || !onTeamCliAgentChange) return;

    const currentPath = draft.cli_agent_mapping.default;
    if (prevDefaultCliPathRef.current === null) {
      prevDefaultCliPathRef.current = currentPath;
      return;
    }
    if (prevDefaultCliPathRef.current !== currentPath) {
      prevDefaultCliPathRef.current = currentPath;

      const matchedAgent = draft.cli_agents.find((agent) => agent.path === currentPath);
      const matchedDetection = detections.find((agent) => agent.config_reference === currentPath);
      const agentName =
        matchedDetection?.name ??
        matchedAgent?.name ??
        currentPath.replace("-cli.yml", "").replace(".yml", "");
      if (agentName && agentName !== teamCliAgent) {
        onTeamCliAgentChange(agentName);
      }
    }
  }, [draft?.cli_agent_mapping?.default, teamCliAgent, onTeamCliAgentChange, personId, detections]);

  if (!enabled) {
    return (
      <Text size="sm" c="dimmed">
        {t("setup.intelligence.createBeforeAdvanced")}
      </Text>
    );
  }
  if (query.isLoading || !draft) {
    return (
      <Text size="sm" c="dimmed">
        {t("setup.intelligence.loadingAdvanced")}
      </Text>
    );
  }
  if (query.error) {
    return (
      <Alert color="danger" title={t("setup.intelligence.loadAdvancedError")}>
        {query.error.message}
      </Alert>
    );
  }

  const modelSlots = Object.keys(draft.model_mapping);
  const cliSlots = Object.keys(draft.cli_agent_mapping);
  // Offer every known AI CLI tool as a choice, not just the ones still mapped to a
  // slot. Deriving the options from `draft.cli_agents` (which the backend builds
  // only from currently-mapped paths) would otherwise leave a single option once
  // the other slots are deleted.
  const cliFileOptions = [
    ...detections.map((agent) => ({
      value: agent.config_reference,
      label: agent.name,
    })),
    ...draft.cli_agents
      .filter((agent) => !detections.some((d) => d.config_reference === agent.path))
      .map((agent) => ({ value: agent.path, label: agent.name })),
  ];
  const detectedByPath = Object.fromEntries(
    detections.map((entry) => [entry.config_reference, entry]),
  ) as Record<string, CliAgentDetection>;

  // --- LLM Slot Helpers ---
  const handleAddLlmSlot = () => {
    updateDraft((current) => {
      let baseName = "new_llm_slot";
      let counter = 1;
      while (current.model_mapping[baseName]) {
        baseName = `new_llm_slot_${counter}`;
        counter++;
      }
      return withSlotModel(
        current,
        baseName,
        providerDefaultModel(DEFAULT_LLM_PROVIDER, baseName, providerDefaults),
      );
    });
  };

  const handleDeleteLlmSlot = (slotKey: string) => {
    if (slotKey === "default") return;
    updateDraft((current) => {
      const nextMapping = { ...current.model_mapping };
      delete nextMapping[slotKey];

      const nextBrainMapping = current.brain_mapping.map((brain) => {
        if (brain.engine === "llm" && brain.target === slotKey) {
          return { ...brain, target: "default" };
        }
        return brain;
      });

      return {
        ...current,
        model_mapping: nextMapping,
        brain_mapping: nextBrainMapping,
      };
    });
  };

  const handleRenameLlmSlot = (oldKey: string, newKey: string) => {
    if (oldKey === "default" || !newKey.trim() || oldKey === newKey) return;
    updateDraft((current) => {
      if (current.model_mapping[newKey]) return current;

      const nextMapping = { ...current.model_mapping };
      const path = nextMapping[oldKey];
      delete nextMapping[oldKey];
      nextMapping[newKey] = path;

      const nextBrainMapping = current.brain_mapping.map((brain) => {
        if (brain.engine === "llm" && brain.target === oldKey) {
          return { ...brain, target: newKey };
        }
        return brain;
      });

      return {
        ...current,
        model_mapping: nextMapping,
        brain_mapping: nextBrainMapping,
      };
    });
  };

  const handleUpdateLlmSlotProvider = (slotKey: string, provider: string) => {
    updateDraft((current) =>
      withSlotModel(current, slotKey, providerDefaultModel(provider, slotKey, providerDefaults)),
    );
  };

  const handleUpdateLlmSlotModelId = (slotKey: string, modelId: string) => {
    updateDraft((current) => {
      const currentPath = current.model_mapping[slotKey];
      const currentDef = current.models.find((m) => m.path === currentPath);
      const provider =
        currentDef?.provider || providerFromModelPath(currentPath) || DEFAULT_LLM_PROVIDER;
      return withSlotModel(current, slotKey, {
        path: slotModelPath(provider, slotKey),
        provider,
        model_class: currentDef?.model_class || providerDefaults[provider]?.model_class || "",
        model_id: modelId,
      });
    });
  };

  // --- CLI Slot Helpers ---
  const handleAddCliSlot = () => {
    updateDraft((current) => {
      let baseName = "new_cli_slot";
      let counter = 1;
      while (current.cli_agent_mapping[baseName]) {
        baseName = `new_cli_slot_${counter}`;
        counter++;
      }
      return {
        ...current,
        cli_agent_mapping: {
          ...current.cli_agent_mapping,
          [baseName]: current.cli_agent_mapping.default,
        },
      };
    });
  };

  const handleDeleteCliSlot = (slotKey: string) => {
    if (slotKey === "default") return;
    updateDraft((current) => {
      const nextMapping = { ...current.cli_agent_mapping };
      delete nextMapping[slotKey];

      const nextBrainMapping = current.brain_mapping.map((brain) => {
        if (brain.engine === "cli" && brain.target === slotKey) {
          return { ...brain, target: "default" };
        }
        return brain;
      });

      return {
        ...current,
        cli_agent_mapping: nextMapping,
        brain_mapping: nextBrainMapping,
      };
    });
  };

  const handleRenameCliSlot = (oldKey: string, newKey: string) => {
    if (oldKey === "default" || !newKey.trim() || oldKey === newKey) return;
    updateDraft((current) => {
      if (current.cli_agent_mapping[newKey]) return current;

      const nextMapping = { ...current.cli_agent_mapping };
      const path = nextMapping[oldKey];
      delete nextMapping[oldKey];
      nextMapping[newKey] = path;

      const nextBrainMapping = current.brain_mapping.map((brain) => {
        if (brain.engine === "cli" && brain.target === oldKey) {
          return { ...brain, target: newKey };
        }
        return brain;
      });

      return {
        ...current,
        cli_agent_mapping: nextMapping,
        brain_mapping: nextBrainMapping,
      };
    });
  };

  const handleUpdateCliSlotAgentPath = (slotKey: string, agentPath: string) => {
    updateDraft((current) => {
      const nextCliAgents = [...current.cli_agents];
      const exists = nextCliAgents.some((a) => a.path === agentPath);
      if (!exists) {
        const name = agentPath.replace(".yml", "");
        nextCliAgents.push({
          path: agentPath,
          name: name,
          env: {},
          script: "",
          detected: false,
          detected_path: "",
        });
      }

      return {
        ...current,
        cli_agent_mapping: {
          ...current.cli_agent_mapping,
          [slotKey]: agentPath,
        },
        cli_agents: nextCliAgents,
      };
    });
  };

  const handleUpdateCliAgentDef = (path: string, updates: Partial<CliAgentDefinition>) => {
    updateDraft((current) => {
      return {
        ...current,
        cli_agents: current.cli_agents.map((agent) => {
          if (agent.path === path) {
            return { ...agent, ...updates };
          }
          return agent;
        }),
      };
    });
  };

  // --- Brain Assignment Helpers ---
  const handleAddBrain = () => {
    updateDraft((current) => {
      let baseName = "new_brain_function";
      let counter = 1;
      const exists = (name: string) => current.brain_mapping.some((b) => b.name === name);
      while (exists(baseName)) {
        baseName = `new_brain_function_${counter}`;
        counter++;
      }
      return {
        ...current,
        brain_mapping: [
          ...current.brain_mapping,
          {
            name: baseName,
            brain_class: "guildbotics.intelligences.agno_agent.AgnoAgentDefaultBrain",
            engine: "llm",
            target: "default",
          },
        ],
      };
    });
  };

  const handleDeleteBrain = (index: number) => {
    const target = draft.brain_mapping[index];
    if (!target || target.name === "default" || target.name === "agent") return;
    updateDraft((current) => {
      return {
        ...current,
        brain_mapping: current.brain_mapping.filter((_, i) => i !== index),
      };
    });
  };

  const handleUpdateBrain = (index: number, updates: Partial<BrainAssignment>) => {
    updateDraft((current) => {
      const updated = [...current.brain_mapping];
      const currentAssignment = updated[index];
      if (!currentAssignment) return current;

      let nextClass = currentAssignment.brain_class;
      let nextTarget = updates.target !== undefined ? updates.target : currentAssignment.target;

      if (updates.engine !== undefined && updates.engine !== currentAssignment.engine) {
        if (updates.engine === "cli") {
          nextClass = "guildbotics.intelligences.brains.cli_agent.CliAgentBrain";
          const firstCliSlot = Object.keys(current.cli_agent_mapping)[0] ?? "default";
          nextTarget = firstCliSlot;
        } else {
          nextClass = "guildbotics.intelligences.brains.agno_agent.AgnoAgentDefaultBrain";
          const firstLlmSlot = Object.keys(current.model_mapping)[0] ?? "default";
          nextTarget = firstLlmSlot;
        }
      }

      updated[index] = {
        ...currentAssignment,
        ...updates,
        brain_class: nextClass,
        target: nextTarget,
      };
      return { ...current, brain_mapping: updated };
    });
  };

  const handleRenameBrain = (index: number, newName: string) => {
    const target = draft.brain_mapping[index];
    if (!target || target.name === "default" || target.name === "agent" || !newName.trim()) return;
    updateDraft((current) => {
      const exists = current.brain_mapping.some((b, i) => i !== index && b.name === newName);
      if (exists) return current;

      const updated = [...current.brain_mapping];
      updated[index] = { ...target, name: newName };
      return { ...current, brain_mapping: updated };
    });
  };

  if (personId) {
    return (
      <Stack gap="md">
        <Group justify="space-between">
          <Text size="sm" c="dimmed">
            {t("setup.intelligence.memberOverrideDescription")}
          </Text>
          {saveMode === "auto" ? <AutosaveIndicator state={saveState} /> : null}
        </Group>
        <Switch
          label={t("setup.intelligence.inheritTeamDefaults")}
          checked={draft.inherited}
          onChange={(event) =>
            updateDraft((current) => ({
              ...current,
              inherited: event.currentTarget.checked,
            }))
          }
        />
        {draft.inherited ? (
          <InfoCallout title={t("setup.intelligence.inheritingTitle")}>
            {t("setup.intelligence.inheritingBody")}
          </InfoCallout>
        ) : (
          <Stack gap="md">
            <Stack gap="xs">
              <Text size="sm" fw={700}>
                {t("setup.intelligence.memberDefaultProvider")}
              </Text>
              <Text size="sm" c="dimmed">
                {t("setup.intelligence.memberDefaultProviderDescription")}
              </Text>
              <div className="option-card-grid">
                {providers.map((option) => {
                  const available = Boolean(llmProviderAvailability?.[option.provider]);
                  const active =
                    providerFromModelPath(draft.model_mapping.default) === option.provider;
                  return (
                    <button
                      key={option.provider}
                      type="button"
                      className={`option-card ${active ? "active" : ""}`}
                      disabled={!available}
                      onClick={() =>
                        updateDraft((current) =>
                          withSlotModel(
                            current,
                            "default",
                            providerDefaultModel(option.provider, "default", providerDefaults),
                          ),
                        )
                      }
                    >
                      <span className="title">{option.label}</span>
                      <span className={`detection ${available ? "ok" : "ng"}`}>
                        <i />
                        {available
                          ? t("setup.intelligence.apiKeyConfigured")
                          : t("setup.intelligence.apiKeyMissing")}
                      </span>
                    </button>
                  );
                })}
              </div>
            </Stack>

            <Stack gap="xs">
              <Text size="sm" fw={700}>
                {t("setup.intelligence.memberDefaultCliAgent")}
              </Text>
              <Text size="sm" c="dimmed">
                {t("setup.intelligence.memberDefaultCliAgentDescription")}
              </Text>
              <div className="option-card-grid">
                {detections.map((agent) => {
                  const agentPath = agent.config_reference;
                  const available = agent.detected;
                  const active = draft.cli_agent_mapping.default === agentPath;
                  return (
                    <button
                      key={agent.name}
                      type="button"
                      className={`option-card ${active ? "active" : ""}`}
                      disabled={!available}
                      onClick={() =>
                        updateDraft((current) => ({
                          ...current,
                          cli_agent_mapping: {
                            ...current.cli_agent_mapping,
                            default: agentPath,
                          },
                        }))
                      }
                    >
                      <span className="title">{agent.label}</span>
                      <span className={`detection ${available ? "ok" : "ng"}`}>
                        <i />
                        {available
                          ? t("setup.intelligence.detected")
                          : t("setup.intelligence.notDetected")}
                      </span>
                    </button>
                  );
                })}
              </div>
            </Stack>

            <NativeAgentPolicyEditor
              policy={draft.native_agent_policy}
              onChange={(policy) =>
                updateDraft((current) => ({ ...current, native_agent_policy: policy }))
              }
            />
          </Stack>
        )}
        {mutation.error ? (
          <Alert color="danger" title={t("setup.intelligence.saveAdvancedError")}>
            {mutation.error.message}
          </Alert>
        ) : null}
      </Stack>
    );
  }

  return (
    <Stack gap="md">
      <Group justify="space-between">
        <Text size="sm" c="dimmed">
          {t("setup.intelligence.teamAdvancedDescription")}
        </Text>
        {saveMode === "auto" ? <AutosaveIndicator state={saveState} /> : null}
      </Group>
      {draft.inherited ? (
        <InfoCallout title={t("setup.intelligence.inheritingTitle")}>
          {t("setup.intelligence.inheritingBody")}
        </InfoCallout>
      ) : (
        <Stack gap="lg">
          {/* Section 1: Brain Assignment */}
          <Card withBorder radius="sm" p="md">
            <Stack gap="sm">
              <Group justify="space-between">
                <Text fw={700} size="sm">
                  {t("setup.intelligence.brainMapping")}
                </Text>
                <Button
                  size="xs"
                  variant="light"
                  leftSection={<Plus size={14} />}
                  onClick={handleAddBrain}
                >
                  {t("setup.intelligence.addBrain")}
                </Button>
              </Group>

              {draft.brain_mapping.map((assignment, index) => {
                const targetOptions =
                  assignment.engine === "cli"
                    ? cliSlots.map((s) => ({ value: s, label: s }))
                    : modelSlots.map((s) => ({ value: s, label: s }));

                return (
                  <Group key={index} align="flex-end" gap="xs" wrap="nowrap">
                    <TextInput
                      label={t("setup.intelligence.feature")}
                      value={assignment.name}
                      readOnly={assignment.name === "default" || assignment.name === "agent"}
                      onChange={(e) => handleRenameBrain(index, e.currentTarget.value)}
                      flex={2}
                    />
                    <Select
                      label={t("setup.intelligence.engine")}
                      data={[
                        { value: "llm", label: "LLM" },
                        { value: "cli", label: "CLI" },
                      ]}
                      value={assignment.engine}
                      onChange={(value) =>
                        handleUpdateBrain(index, { engine: (value as "llm" | "cli") ?? "llm" })
                      }
                      flex={1}
                    />
                    <Select
                      label={t("setup.intelligence.target")}
                      data={targetOptions}
                      value={assignment.target}
                      onChange={(value) => handleUpdateBrain(index, { target: value ?? "default" })}
                      flex={1.5}
                    />
                    {assignment.name !== "default" && assignment.name !== "agent" ? (
                      <ActionIcon
                        color="danger"
                        variant="subtle"
                        onClick={() => handleDeleteBrain(index)}
                        mb="xs"
                      >
                        <Trash2 size={16} />
                      </ActionIcon>
                    ) : (
                      <Box w={28} />
                    )}
                  </Group>
                );
              })}
            </Stack>
          </Card>

          {/* Section 2: LLM Slots Definitions */}
          <Card withBorder radius="sm" p="md">
            <Stack gap="md">
              <Group justify="space-between">
                <Text fw={700} size="sm">
                  {t("setup.intelligence.tabs.models")}
                </Text>
                <Button
                  size="xs"
                  variant="light"
                  leftSection={<Plus size={14} />}
                  onClick={handleAddLlmSlot}
                >
                  {t("setup.intelligence.addLlmSlot")}
                </Button>
              </Group>

              <Stack gap="sm">
                {modelSlots.map((slotKey) => {
                  const path = draft.model_mapping[slotKey];
                  const modelDef = draft.models.find((m) => m.path === path);
                  if (!modelDef) return null;

                  return (
                    <Group key={slotKey} align="flex-end" gap="xs" wrap="nowrap">
                      <TextInput
                        label={t("setup.intelligence.slot")}
                        value={slotKey}
                        readOnly={slotKey === "default"}
                        onChange={(e) => handleRenameLlmSlot(slotKey, e.currentTarget.value)}
                        flex={1.5}
                      />
                      <Select
                        label={t("setup.intelligence.provider")}
                        data={providers.map((provider) => ({
                          value: provider.provider,
                          label: provider.label,
                        }))}
                        value={modelDef.provider}
                        onChange={(val) =>
                          handleUpdateLlmSlotProvider(slotKey, val ?? DEFAULT_LLM_PROVIDER)
                        }
                        flex={1.5}
                      />
                      <TextInput
                        label={t("setup.intelligence.modelId")}
                        value={modelDef.model_id}
                        onChange={(e) => handleUpdateLlmSlotModelId(slotKey, e.currentTarget.value)}
                        flex={2}
                      />
                      {slotKey !== "default" ? (
                        <ActionIcon
                          color="danger"
                          variant="subtle"
                          onClick={() => handleDeleteLlmSlot(slotKey)}
                          mb="xs"
                        >
                          <Trash2 size={16} />
                        </ActionIcon>
                      ) : (
                        <Box w={28} />
                      )}
                    </Group>
                  );
                })}
              </Stack>
            </Stack>
          </Card>

          {/* Section 3: CLI Slots Definitions */}
          <Card withBorder radius="sm" p="md">
            <Stack gap="md">
              <Group justify="space-between">
                <Text fw={700} size="sm">
                  {t("setup.intelligence.tabs.cli")}
                </Text>
                <Button
                  size="xs"
                  variant="light"
                  leftSection={<Plus size={14} />}
                  onClick={handleAddCliSlot}
                >
                  {t("setup.intelligence.addCliSlot")}
                </Button>
              </Group>

              <Accordion variant="separated">
                {cliSlots.map((slotKey) => {
                  const path = draft.cli_agent_mapping[slotKey];
                  const agentDef = draft.cli_agents.find((a) => a.path === path);
                  if (!agentDef) return null;

                  const detection = detectedByPath[agentDef.path];
                  const isDetected = detection?.detected || agentDef.detected;

                  return (
                    <Accordion.Item key={slotKey} value={slotKey}>
                      <Accordion.Control>
                        <Group justify="space-between" pr="md" align="center" wrap="nowrap">
                          <Group gap="xs" align="center">
                            <Text fw={600} size="sm">
                              {slotKey}
                            </Text>
                            <Text size="xs" c="dimmed">
                              ➔ {agentDef.name}
                            </Text>
                          </Group>
                          <Group gap="xs" wrap="nowrap">
                            <Badge color={isDetected ? "success" : "danger"} variant="light">
                              {isDetected
                                ? t("setup.intelligence.detected")
                                : t("setup.intelligence.notDetected")}
                            </Badge>
                            {slotKey !== "default" ? (
                              <ActionIcon
                                color="danger"
                                variant="subtle"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  handleDeleteCliSlot(slotKey);
                                }}
                                size="sm"
                              >
                                <Trash2 size={14} />
                              </ActionIcon>
                            ) : null}
                          </Group>
                        </Group>
                      </Accordion.Control>
                      <Accordion.Panel>
                        <Stack gap="sm" pt="xs">
                          <Group gap="xs" grow>
                            <TextInput
                              label={t("setup.intelligence.slot")}
                              value={slotKey}
                              readOnly={slotKey === "default"}
                              onChange={(e) => handleRenameCliSlot(slotKey, e.currentTarget.value)}
                              size="xs"
                              fw={600}
                            />
                            <Select
                              label={t("setup.intelligence.cliAgent")}
                              data={cliFileOptions}
                              value={path}
                              onChange={(val) => handleUpdateCliSlotAgentPath(slotKey, val ?? path)}
                              size="xs"
                            />
                          </Group>
                          <Textarea
                            label={t("setup.intelligence.envJson")}
                            autosize
                            minRows={2}
                            value={JSON.stringify(agentDef.env ?? {}, null, 2)}
                            error={envErrors[agentDef.path]}
                            onChange={(event) => {
                              const nextText = event.currentTarget.value;
                              try {
                                const parsed = JSON.parse(nextText || "{}") as unknown;
                                if (!isRecord(parsed)) {
                                  throw new Error("env must be an object");
                                }
                                setEnvErrors((current) => {
                                  const next = { ...current };
                                  delete next[agentDef.path];
                                  return next;
                                });
                                handleUpdateCliAgentDef(agentDef.path, { env: parsed });
                              } catch {
                                setEnvErrors((current) => ({
                                  ...current,
                                  [agentDef.path]: t("setup.intelligence.envJsonError"),
                                }));
                              }
                            }}
                          />
                          <Textarea
                            label={t("setup.intelligence.script")}
                            autosize
                            minRows={5}
                            value={agentDef.script}
                            onChange={(event) =>
                              handleUpdateCliAgentDef(agentDef.path, {
                                script: event.currentTarget.value,
                              })
                            }
                          />
                        </Stack>
                      </Accordion.Panel>
                    </Accordion.Item>
                  );
                })}
              </Accordion>
            </Stack>
          </Card>

          <NativeAgentPolicyEditor
            policy={draft.native_agent_policy}
            onChange={(policy) =>
              updateDraft((current) => ({ ...current, native_agent_policy: policy }))
            }
          />
        </Stack>
      )}
      {mutation.error ? (
        <Alert color="danger" title={t("setup.intelligence.saveAdvancedError")}>
          {mutation.error.message}
        </Alert>
      ) : null}
    </Stack>
  );
}

function MembersSection({
  activeMemberCount,
  members,
  config,
  workspaceDir,
  projectGithubEnabled,
  cliDetections,
  llmProviderAvailability,
  providers,
  initialTab,
  initialMemberId,
  onMemberActiveDelta,
}: {
  activeMemberCount: number;
  members: Array<{
    person_id: string;
    name: string;
    person_type?: string;
    is_active: boolean;
    roles: string[];
  }>;
  config: ConfigStatus | undefined;
  workspaceDir: string;
  projectGithubEnabled: boolean;
  cliDetections: CliAgentDetection[];
  llmProviderAvailability: LlmProviderAvailability;
  providers: LlmProviderInfo[];
  initialTab?: MemberEditorTab;
  initialMemberId?: string;
  onMemberActiveDelta: (delta: number) => void;
}) {
  const { t, i18n } = useTranslation();
  const queryClient = useQueryClient();
  const hasActiveMember = activeMemberCount > 0;
  const [mode, setMode] = useState<"idle" | "add" | "edit">(initialMemberId ? "edit" : "idle");
  const [editingPersonId, setEditingPersonId] = useState<string | null>(initialMemberId ?? null);
  const [activeTab, setActiveTab] = useState<string | null>(initialTab ?? "basic");
  const [personType, setPersonType] = useState<MemberType>("agent");
  const [githubAccountType, setGithubAccountType] = useState<GitHubAccountType>("none");
  const [identity, setIdentity] = useState("");
  const [personId, setPersonId] = useState("");
  const [personName, setPersonName] = useState("");
  const [githubUsername, setGithubUsername] = useState("");
  const [gitEmail, setGitEmail] = useState("");
  const [roles, setRoles] = useState<string[]>([]);
  const [githubAccessToken, setGithubAccessToken] = useState("");
  const [githubInstallationId, setGithubInstallationId] = useState("");
  const [githubAppId, setGithubAppId] = useState("");
  const [githubPrivateKeyPath, setGithubPrivateKeyPath] = useState("");
  const [speakingStylePreset, setSpeakingStylePreset] = useState<SpeakingStylePreset | "">(
    "energetic",
  );
  const [speakingStyle, setSpeakingStyle] = useState("");
  const [relationships, setRelationships] = useState("");
  const [characterArchetype, setCharacterArchetype] = useState("");
  const [characterTraits, setCharacterTraits] = useState<string[]>([]);
  const [characterInterests, setCharacterInterests] = useState<string[]>([]);
  const [characterJoinWhenText, setCharacterJoinWhenText] = useState("");
  const [characterAvoidWhenText, setCharacterAvoidWhenText] = useState("");
  const [characterContributionText, setCharacterContributionText] = useState("");
  const [characterExtras, setCharacterExtras] = useState<Record<string, unknown>>({});
  const [slackBotToken, setSlackBotToken] = useState("");
  const [slackAppToken, setSlackAppToken] = useState("");
  const [slackUserId, setSlackUserId] = useState("");
  const [slackChannelsText, setSlackChannelsText] = useState("");
  const [slackChannelInput, setSlackChannelInput] = useState("");
  const [slackChannelInputError, setSlackChannelInputError] = useState<string | null>(null);
  const [slackChannelParticipation, setSlackChannelParticipation] = useState<
    Record<string, ChatParticipationPolicy>
  >({});
  const [routineOverrideEnabled, setRoutineOverrideEnabled] = useState(false);
  const [routineCommands, setRoutineCommands] = useState<string[]>([]);
  const [scheduledCommands, setScheduledCommands] = useState<ScheduledCommandDraft[]>([]);
  const [isActive, setIsActive] = useState(true);
  const [storedMemberSecrets, setStoredMemberSecrets] = useState({
    githubInstallationId: false,
    githubAppId: false,
    githubPrivateKeyPath: false,
    githubAccessToken: false,
    slackBotToken: false,
    slackAppToken: false,
  });
  const [identityResolveError, setIdentityResolveError] = useState("");
  const [savingMember, setSavingMember] = useState(false);
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);
  const [draftMembers, setDraftMembers] = useState<MemberConfig[]>([]);
  const memberIntelligenceSaveRef = useRef<(() => Promise<void>) | null>(null);
  const emptyAddDefaultsAppliedRef = useRef(false);
  const routineDefaultDismissedRef = useRef(false);
  const hasPersistedProject = Boolean(config?.project_file_exists);
  const [avatarTimestamp, setAvatarTimestamp] = useState(Date.now());
  const [importingAvatar, setImportingAvatar] = useState(false);
  const [avatarError, setAvatarError] = useState<string | null>(null);
  const isHumanMember = personType === "human";

  // Effective AI CLI tool for the member (member override falls back to the team
  // default automatically), shown as a badge on the avatar header row.
  const cliAgentBadgeQuery = useQuery({
    queryKey: ["intelligence-config", editingPersonId ?? "team"],
    queryFn: () => getIntelligenceConfig(editingPersonId ?? undefined),
    enabled: hasPersistedProject && !isHumanMember,
  });
  const cliAgentLabel = useMemo(
    () => cliAgentLabelFromConfig(cliAgentBadgeQuery.data, cliDetections),
    [cliAgentBadgeQuery.data, cliDetections],
  );

  const getAvatarErrorMessage = (err: unknown, defaultMsg: string): string => {
    if (err instanceof ApiRequestError) {
      if (err.code === "slack_token_missing") {
        return t("setup.members.avatar.errors.slackTokenMissing");
      }
      if (err.code === "slack_missing_scope") {
        return t("setup.members.avatar.errors.slackMissingScope");
      }
      if (err.code === "slack_user_id_missing") {
        return t("setup.members.avatar.errors.slackUserIdMissing");
      }
      if (err.code === "github_username_missing") {
        return t("setup.members.avatar.errors.githubUsernameMissing");
      }
    }
    return err instanceof Error ? err.message : defaultMsg;
  };

  const handleAvatarUpload = async (file: File | null) => {
    if (!file || !editingPersonId) return;
    setImportingAvatar(true);
    setAvatarError(null);
    try {
      const result = await uploadMemberAvatar(editingPersonId, file);
      setAvatarTimestamp(result.avatar_timestamp);
      notifications.show({
        color: "success",
        title: t("setup.members.avatar.uploadSuccessTitle", "Success"),
        message: t("setup.members.avatar.uploadSuccess", "Avatar uploaded successfully."),
      });
      await queryClient.invalidateQueries({ queryKey: ["team"] });
    } catch (err) {
      setAvatarError(getAvatarErrorMessage(err, "Failed to upload avatar"));
    } finally {
      setImportingAvatar(false);
    }
  };

  const handleImportFromGithub = async () => {
    if (!editingPersonId) return;
    setImportingAvatar(true);
    setAvatarError(null);
    try {
      const result = await importAvatarFromGithub(editingPersonId);
      setAvatarTimestamp(result.avatar_timestamp);
      notifications.show({
        color: "success",
        title: t("setup.members.avatar.importSuccessTitle", "Success"),
        message: t("setup.members.avatar.githubSuccess", "Avatar imported from GitHub."),
      });
      await queryClient.invalidateQueries({ queryKey: ["team"] });
    } catch (err) {
      setAvatarError(getAvatarErrorMessage(err, "Failed to import avatar from GitHub"));
    } finally {
      setImportingAvatar(false);
    }
  };

  const handleImportFromSlack = async () => {
    if (!editingPersonId) return;
    setImportingAvatar(true);
    setAvatarError(null);
    try {
      const result = await importAvatarFromSlack(editingPersonId);
      setAvatarTimestamp(result.avatar_timestamp);
      notifications.show({
        color: "success",
        title: t("setup.members.avatar.importSuccessTitle", "Success"),
        message: t("setup.members.avatar.slackSuccess", "Avatar imported from Slack."),
      });
      await queryClient.invalidateQueries({ queryKey: ["team"] });
    } catch (err) {
      setAvatarError(getAvatarErrorMessage(err, "Failed to import avatar from Slack"));
    } finally {
      setImportingAvatar(false);
    }
  };

  const appLanguage = normalizeLanguage(i18n.resolvedLanguage ?? i18n.language) ?? "en";
  const rolesQuery = useQuery({
    queryKey: ["member-role-options", appLanguage],
    queryFn: () => getRoleOptions(appLanguage),
  });
  const commandOptions = useQuery({
    queryKey: ["command-options", editingPersonId ?? "team"],
    queryFn: () => getCommandOptions(editingPersonId ?? undefined),
    enabled: hasPersistedProject && !isHumanMember,
    retry: false,
  });
  const routineCommandOptions = useQuery({
    queryKey: ["routine-command-options", editingPersonId ?? "team"],
    queryFn: () => getRoutineCommandOptions(editingPersonId ?? undefined),
    enabled: hasPersistedProject && !isHumanMember,
    retry: false,
  });
  const roleOptions = useMemo(
    () =>
      (rolesQuery.data?.roles ?? []).map((option: RoleOption) => ({
        value: option.role_id,
        label: option.role_id,
      })),
    [rolesQuery.data?.roles],
  );
  const roleSummaries = useMemo(
    () =>
      Object.fromEntries(
        (rolesQuery.data?.roles ?? []).map((option: RoleOption) => [
          option.role_id,
          option.summary,
        ]),
      ) as Record<string, string>,
    [rolesQuery.data?.roles],
  );
  const commandCatalog = useMemo(
    () => commandOptions.data?.options ?? [],
    [commandOptions.data?.options],
  );
  const commandOptionByValue = useMemo(
    () => new Map(commandCatalog.map((option) => [option.command, option])),
    [commandCatalog],
  );
  const routineCommandCatalog = useMemo(
    () => routineCommandOptions.data?.options ?? [],
    [routineCommandOptions.data?.options],
  );
  const routineCommandOptionByValue = useMemo(
    () => new Map(routineCommandCatalog.map((option) => [option.command, option])),
    [routineCommandCatalog],
  );
  const routineDefaultCommand = routineCommandOptions.data?.default_command ?? "";
  const speakingStyleTemplates = useMemo(() => getSpeakingStyleTemplates(t), [t]);
  const characterPresetExamples = useMemo(
    () => getCharacterPresetExamples(appLanguage),
    [appLanguage],
  );
  const displayedMembers = useMemo(() => {
    const persistedIds = new Set(members.map((member) => member.person_id));
    return [
      ...members,
      ...draftMembers
        .filter((member) => !persistedIds.has(member.person_id))
        .map((member) => ({
          person_id: member.person_id,
          name: member.person_name,
          person_type: member.person_type,
          is_active: member.is_active,
          roles: member.roles,
        })),
    ].sort((a, b) => a.name.localeCompare(b.name));
  }, [draftMembers, members]);
  const formVisible = mode !== "idle" || displayedMembers.length === 0;
  const formMode = mode === "edit" ? "edit" : "add";

  const applyPresetFields = useCallback(
    (preset: SpeakingStylePreset) => {
      const sample = characterPresetExamples[preset];
      setSpeakingStyle(speakingStyleTemplates[preset]);
      setCharacterArchetype(sample.archetype);
      setCharacterTraits(sample.traits);
      setCharacterInterests(sample.interests);
      setCharacterJoinWhenText(sample.joinWhen.join("\n"));
      setCharacterAvoidWhenText(sample.avoidWhen.join("\n"));
      setCharacterContributionText(sample.contributionStyle.join("\n"));
    },
    [characterPresetExamples, speakingStyleTemplates],
  );

  const clearPresetFields = () => {
    setSpeakingStyle("");
    setCharacterArchetype("");
    setCharacterTraits([]);
    setCharacterInterests([]);
    setCharacterJoinWhenText("");
    setCharacterAvoidWhenText("");
    setCharacterContributionText("");
    setSpeakingStylePreset("");
  };

  useEffect(() => {
    if (displayedMembers.length > 0 || mode !== "idle") {
      emptyAddDefaultsAppliedRef.current = false;
      return;
    }
    if (!emptyAddDefaultsAppliedRef.current) {
      setSpeakingStylePreset("energetic");
      setRoles([]);
      applyPresetFields("energetic");
      emptyAddDefaultsAppliedRef.current = true;
    }
  }, [applyPresetFields, displayedMembers.length, mode]);

  useEffect(() => {
    if (
      isHumanMember &&
      (activeTab === "intelligence" || activeTab === "patrol" || activeTab === "diagnostics")
    ) {
      setActiveTab("basic");
    }
  }, [activeTab, isHumanMember]);

  const clearForm = ({ withDefaults = false }: { withDefaults?: boolean } = {}) => {
    setIdentity("");
    setPersonId("");
    setPersonName("");
    setGithubUsername("");
    setGitEmail("");
    setRoles([]);
    setGithubAccessToken("");
    setGithubInstallationId("");
    setGithubAppId("");
    setGithubPrivateKeyPath("");
    if (withDefaults) {
      setSpeakingStylePreset("energetic");
      applyPresetFields("energetic");
    } else {
      setSpeakingStylePreset("");
      clearPresetFields();
    }
    setRelationships("");
    setCharacterExtras({});
    setSlackBotToken("");
    setSlackAppToken("");
    setSlackChannelsText("");
    setSlackChannelInput("");
    setSlackChannelInputError(null);
    setSlackChannelParticipation({});
    setRoutineOverrideEnabled(false);
    setRoutineCommands([]);
    routineDefaultDismissedRef.current = false;
    setScheduledCommands([]);
    setPersonType("agent");
    setGithubAccountType("none");
    setIsActive(true);
    setSlackUserId("");
    setStoredMemberSecrets({
      githubInstallationId: false,
      githubAppId: false,
      githubPrivateKeyPath: false,
      githubAccessToken: false,
      slackBotToken: false,
      slackAppToken: false,
    });
    setIdentityResolveError("");
    setActiveTab(initialTab ?? "basic");
  };

  const fillFormFromMember = (member: MemberConfig) => {
    const nextPersonType = member.person_type === "human" ? "human" : "agent";
    // Seed the cache-buster from the persisted avatar mtime so the URL stays
    // deterministic across reloads (changes only when the avatar changes).
    setAvatarTimestamp(member.avatar_timestamp ?? 0);
    setPersonType(nextPersonType);
    setGithubAccountType(
      nextPersonType === "human"
        ? "human"
        : toGitHubAccountType(member.github_account_type || member.person_type),
    );
    setIdentity("");
    setPersonId(member.person_id);
    setPersonName(member.person_name);
    setGithubUsername(member.github_username);
    setGitEmail(member.git_email);
    setRoles(member.roles);
    const preset = inferSpeakingStylePreset(member.speaking_style, speakingStyleTemplates);
    setSpeakingStylePreset(preset);
    setSpeakingStyle(member.speaking_style);
    setRelationships(member.relationships);
    const characterFields = parseCharacterFields(member.character ?? {});
    setCharacterArchetype(characterFields.archetype);
    setCharacterTraits(characterFields.traits);
    setCharacterInterests(characterFields.interests);
    setCharacterJoinWhenText(characterFields.joinWhen.join("\n"));
    setCharacterAvoidWhenText(characterFields.avoidWhen.join("\n"));
    setCharacterContributionText(characterFields.contributionStyle.join("\n"));
    setCharacterExtras(characterFields.extras);
    setSlackChannelsText(member.slack_channels.join(", "));
    setSlackChannelInput("");
    setSlackChannelInputError(null);
    setSlackChannelParticipation(member.slack_channel_participation ?? {});
    setSlackUserId(member.slack_user_id ?? "");
    setGithubAccessToken("");
    setGithubInstallationId(member.github_installation_id?.toString() ?? "");
    setGithubAppId(member.github_app_id?.toString() ?? "");
    setGithubPrivateKeyPath(member.github_private_key_path);
    setSlackBotToken("");
    setSlackAppToken("");
    setRoutineOverrideEnabled(member.routine_commands.length > 0);
    setRoutineCommands(member.routine_commands);
    routineDefaultDismissedRef.current =
      nextPersonType === "human" ||
      member.routine_commands.length > 0 ||
      toGitHubAccountType(member.github_account_type || member.person_type) === "none";
    setScheduledCommands(
      flattenTaskSchedules(member.task_schedules).map((entry) =>
        scheduledCommandToDraft(entry, commandCatalog),
      ),
    );
    setStoredMemberSecrets({
      githubInstallationId: member.has_github_installation_id,
      githubAppId: member.has_github_app_id,
      githubPrivateKeyPath: member.has_github_private_key_path || member.has_github_private_key,
      githubAccessToken: member.has_github_access_token,
      slackBotToken: member.has_slack_bot_token,
      slackAppToken: member.has_slack_app_token,
    });
    setIsActive(nextPersonType === "human" ? false : member.is_active);
  };

  const memberConfigMutation = useMutation({
    mutationFn: getMemberConfig,
    onSuccess: (snapshot) => {
      fillFormFromMember(snapshot);
      setMode("edit");
    },
  });
  const requestMemberConfig = memberConfigMutation.mutate;
  const initialMemberRequested = useRef(false);
  useEffect(() => {
    if (!initialMemberId || initialMemberRequested.current) {
      return;
    }
    initialMemberRequested.current = true;
    requestMemberConfig(initialMemberId);
  }, [initialMemberId, requestMemberConfig]);
  const resolveMutation = useMutation({
    mutationFn: resolveMemberIdentity,
  });
  const memberDiagnosticsMutation = useMutation({
    mutationFn: (targetPersonId: string) => runScenarioDiagnostics(targetPersonId),
  });
  const addMemberMutation = useMutation({
    mutationFn: addMemberConfig,
    onSuccess: (_, request) => {
      if (!hasPersistedProject) {
        setDraftMembers((current) => [
          ...current.filter((member) => member.person_id !== request.person_id),
          memberRequestToConfig(request),
        ]);
      }
      onMemberActiveDelta(effectiveIsActive ? 1 : 0);
      queryClient.invalidateQueries({ queryKey: ["team"] });
      queryClient.invalidateQueries({ queryKey: ["command-options"] });
      queryClient.invalidateQueries({ queryKey: ["scheduler"] });
      memberDiagnosticsMutation.reset();
      clearForm();
      setMode("idle");
      setEditingPersonId(null);
    },
  });
  const updateMemberMutation = useMutation({
    mutationFn: ({
      originalPersonId,
      body,
    }: {
      originalPersonId: string;
      body: MemberConfigUpdateRequest;
    }) => updateMemberConfig(originalPersonId, body),
    onSuccess: (_, variables) => {
      const previous =
        members.find((member) => member.person_id === variables.originalPersonId) ??
        draftMembers.find((member) => member.person_id === variables.originalPersonId);
      const previousActive = previous?.is_active ?? false;
      const delta = Number(effectiveIsActive) - Number(previousActive);
      if (!hasPersistedProject) {
        setDraftMembers((current) => [
          ...current.filter((member) => member.person_id !== variables.originalPersonId),
          memberRequestToConfig(variables.body),
        ]);
      }
      onMemberActiveDelta(delta);
      queryClient.invalidateQueries({ queryKey: ["team"] });
      queryClient.invalidateQueries({ queryKey: ["command-options"] });
      queryClient.invalidateQueries({ queryKey: ["scheduler"] });
      setEditingPersonId(personId.trim());
    },
  });
  const deleteMemberMutation = useMutation({
    mutationFn: ({
      targetPersonId,
      configDir,
      envFilePath,
    }: {
      targetPersonId: string;
      configDir: string;
      envFilePath: string;
    }) =>
      deleteMemberConfig(targetPersonId, {
        config_dir: configDir,
        env_file_path: envFilePath,
      }),
    onSuccess: (_, variables) => {
      const removed =
        members.find((member) => member.person_id === variables.targetPersonId) ??
        draftMembers.find((member) => member.person_id === variables.targetPersonId);
      if (!hasPersistedProject) {
        setDraftMembers((current) =>
          current.filter((member) => member.person_id !== variables.targetPersonId),
        );
      }
      onMemberActiveDelta(removed?.is_active ? -1 : 0);
      queryClient.invalidateQueries({ queryKey: ["team"] });
      memberDiagnosticsMutation.reset();
      clearForm();
      setMode("idle");
      setEditingPersonId(null);
    },
  });

  const effectiveIsActive = isHumanMember ? false : isActive;
  const slackChannels = useMemo(() => parseSlackChannels(slackChannelsText), [slackChannelsText]);
  const slackChannelsConfigured = slackChannels.length > 0;
  const usesGitHubMember = githubAccountType !== "none";
  const shouldSeedDefaultRoutine = projectGithubEnabled || usesGitHubMember;
  const configDir = resolveConfigDir(workspaceDir);
  const envFilePath = joinPath(workspaceDir, ".env");
  const requiresGitHubAuth =
    githubAccountType === "machine_user" || githubAccountType === "proxy_agent";
  const requiresGitHubAppsAuth = githubAccountType === "github_apps";
  const authReady = requiresGitHubAuth
    ? githubAccessToken.trim().length > 0 || storedMemberSecrets.githubAccessToken
    : requiresGitHubAppsAuth
      ? githubInstallationId.trim().length > 0 &&
        githubAppId.trim().length > 0 &&
        (githubPrivateKeyPath.trim().length > 0 || storedMemberSecrets.githubPrivateKeyPath)
      : true;
  const githubIdentityReady =
    !usesGitHubMember || (githubUsername.trim().length > 0 && gitEmail.trim().length > 0);
  const memberErrors = getMemberFieldErrors(
    {
      personType,
      githubAccountType,
      identity,
      personId,
      personName,
      githubUsername,
      gitEmail,
      githubInstallationId,
      githubAppId,
      githubPrivateKeyPath,
      githubAccessToken,
      slackBotToken,
      slackAppToken,
      slackUserId,
      storedMemberSecrets,
      slackChannelsText,
      roles,
      speakingStyle,
      characterArchetype,
      characterTraits,
      characterInterests,
      characterJoinWhenText,
      characterAvoidWhenText,
      characterContributionText,
      existingPersonIds: displayedMembers.map((member) => member.person_id),
      originalPersonId: formMode === "edit" ? editingPersonId : null,
    },
    t,
  );
  const routineSettingsValid =
    isHumanMember ||
    !routineOverrideEnabled ||
    (routineCommands.length > 0 &&
      routineCommands.every(
        (command) => routineCommandOptionByValue.get(command)?.routine_eligible !== false,
      ));
  const scheduledSettingsValid =
    isHumanMember ||
    scheduledCommands.every(
      (draft) =>
        buildScheduledCommandExpression(draft, commandOptionByValue).trim().length > 0 &&
        isValidCron(draftToCron(draft)),
    );
  const patrolSettingsValid = routineSettingsValid && scheduledSettingsValid;
  const memberProfileValid =
    isHumanMember ||
    (speakingStyle.trim().length > 0 &&
      characterArchetype.trim().length > 0 &&
      characterTraits.length > 0 &&
      characterInterests.length > 0 &&
      characterJoinWhenText.trim().length > 0 &&
      characterAvoidWhenText.trim().length > 0 &&
      characterContributionText.trim().length > 0);
  const seedDefaultRoutineCommands = useCallback(() => {
    setRoutineOverrideEnabled(true);
    if (!routineDefaultCommand) {
      return;
    }
    setRoutineCommands((current) => (current.length > 0 ? current : [routineDefaultCommand]));
  }, [routineDefaultCommand]);
  useEffect(() => {
    if (
      formMode === "add" &&
      !isHumanMember &&
      shouldSeedDefaultRoutine &&
      !routineDefaultDismissedRef.current &&
      (!routineOverrideEnabled || routineCommands.length === 0)
    ) {
      seedDefaultRoutineCommands();
    }
  }, [
    formMode,
    isHumanMember,
    shouldSeedDefaultRoutine,
    routineOverrideEnabled,
    routineCommands.length,
    seedDefaultRoutineCommands,
  ]);
  const canResolveIdentity =
    usesGitHubMember &&
    getGitHubResolveInput(githubAccountType, identity, githubUsername).trim().length > 0 &&
    (githubAccountType === "github_apps"
      ? isGitHubAppsUrl(identity)
      : isGitHubUsername(githubUsername));
  const canSubmit =
    configDir.trim().length > 0 &&
    workspaceDir.trim().length > 0 &&
    personId.trim().length > 0 &&
    personName.trim().length > 0 &&
    roles.length > 0 &&
    memberProfileValid &&
    githubIdentityReady &&
    authReady &&
    patrolSettingsValid &&
    Object.keys(memberErrors).length === 0;
  const activePresetSample = characterPresetExamples[speakingStylePreset || "energetic"];
  const hasMemberError = (keys: Array<keyof MemberFieldErrors>) =>
    keys.some((key) => Boolean(memberErrors[key]));
  const basicErrorKeys: Array<keyof MemberFieldErrors> = isHumanMember
    ? ["personId", "personName", "roles"]
    : [
        "personId",
        "personName",
        "roles",
        "speakingStyle",
        "characterArchetype",
        "characterTraits",
        "characterInterests",
        "characterJoinWhenText",
        "characterAvoidWhenText",
        "characterContributionText",
      ];
  const basicTabHasError = hasMemberError(basicErrorKeys);
  const githubTabHasError = hasMemberError([
    "identity",
    "githubUsername",
    "gitEmail",
    "githubInstallationId",
    "githubAppId",
    "githubPrivateKeyPath",
    "githubAccessToken",
  ]);
  const slackTabHasError = hasMemberError([
    "slackUserId",
    "slackChannelsText",
    "slackBotToken",
    "slackAppToken",
  ]);
  const githubResolveLabel =
    githubAccountType === "github_apps"
      ? t("setup.members.githubAppsUrl")
      : t("setup.members.githubUsername");
  const githubResolveDescription =
    githubAccountType === "github_apps"
      ? t("setup.members.githubAppsUrlHint")
      : t("setup.members.githubUsernameHint");
  const githubResolveValue = githubAccountType === "github_apps" ? identity : githubUsername;
  const githubResolveError =
    githubAccountType === "github_apps"
      ? memberErrors.identity || identityResolveError
      : memberErrors.githubUsername || identityResolveError;

  const setSlackChannels = (channels: string[]) => {
    setSlackChannelsText(channels.join(", "));
  };

  const addSlackChannel = () => {
    const channel = slackChannelInput.trim();
    if (!channel) {
      return;
    }
    if (!isSlackChannelReference(channel)) {
      setSlackChannelInputError(t("setup.validation.slackChannelsInvalid"));
      return;
    }
    const channelRef = normalizeSlackChannelReference(channel);
    const nextChannels = slackChannels.filter(
      (existing) => normalizeSlackChannelReference(existing) !== channelRef,
    );
    setSlackChannels([...nextChannels, channel]);
    setSlackChannelParticipation((current) => ({
      ...current,
      [channelRef]: current[channelRef] ?? "strict",
    }));
    setSlackChannelInput("");
    setSlackChannelInputError(null);
  };

  const updateSlackChannel = (index: number, value: string) => {
    const previousRef = normalizeSlackChannelReference(slackChannels[index] ?? "");
    const nextRef = normalizeSlackChannelReference(value);
    setSlackChannels(
      slackChannels.map((channel, currentIndex) => (currentIndex === index ? value : channel)),
    );
    if (!nextRef || previousRef === nextRef) {
      return;
    }
    setSlackChannelParticipation((current) => {
      const { [previousRef]: previousPolicy, ...rest } = current;
      return {
        ...rest,
        [nextRef]: previousPolicy ?? current[nextRef] ?? "strict",
      };
    });
  };

  const removeSlackChannel = (index: number) => {
    const removedRef = normalizeSlackChannelReference(slackChannels[index] ?? "");
    setSlackChannels(slackChannels.filter((_, currentIndex) => currentIndex !== index));
    setSlackChannelParticipation((current) => {
      const next = { ...current };
      delete next[removedRef];
      return next;
    });
  };

  const handleResolve = async () => {
    if (!canResolveIdentity) {
      return;
    }
    setIdentityResolveError("");
    try {
      const resolved = await resolveMutation.mutateAsync({
        person_type: githubAccountType as GitHubMemberType,
        identity: getGitHubResolveInput(githubAccountType, identity, githubUsername).trim(),
      });
      if (
        !resolved.github_username.trim() ||
        !resolved.git_email.trim() ||
        resolved.github_user_id <= 0
      ) {
        setIdentityResolveError(t("setup.validation.memberGithubIdentityNotFound"));
        return;
      }
      setGithubUsername(resolved.github_username);
      setGitEmail(resolved.git_email);
    } catch (error) {
      setIdentityResolveError(getMemberResolveErrorMessage(error, t));
    }
  };

  const buildMemberRequest = (): MemberSetupRequest => {
    const request: MemberSetupRequest = {
      config_dir: configDir,
      env_file_path: envFilePath,
      append_env_file: Boolean(config?.env_file_exists),
      person_type: personType,
      github_account_type: githubAccountType === "none" ? "" : githubAccountType,
      person_id: personId.trim(),
      person_name: personName.trim(),
      is_active: effectiveIsActive,
      github_username: githubUsername.trim(),
      git_email: gitEmail.trim(),
      roles,
      speaking_style: isHumanMember ? "" : speakingStyle.trim(),
      relationships: isHumanMember ? "" : relationships.trim(),
      character: isHumanMember
        ? {}
        : buildCharacterPayload({
            archetype: characterArchetype,
            traits: characterTraits,
            interests: characterInterests,
            joinWhen: splitLines(characterJoinWhenText),
            avoidWhen: splitLines(characterAvoidWhenText),
            contributionStyle: splitLines(characterContributionText),
            extras: characterExtras,
          }),
      slack_user_id: personType === "human" ? slackUserId.trim() : "",
      slack_bot_token: personType === "agent" ? slackBotToken.trim() : "",
      slack_app_token: personType === "agent" ? slackAppToken.trim() : "",
      slack_channels: personType === "agent" ? slackChannels : [],
      slack_channel_participation:
        personType === "agent"
          ? Object.fromEntries(
              slackChannels.map((channel) => {
                const channelRef = normalizeSlackChannelReference(channel);
                return [channelRef, slackChannelParticipation[channelRef] ?? "strict"];
              }),
            )
          : {},
      routine_commands: isHumanMember || !routineOverrideEnabled ? [] : routineCommands,
      task_schedules: isHumanMember
        ? []
        : buildTaskSchedules(scheduledCommands, commandOptionByValue),
    };
    if (githubAccountType === "github_apps") {
      request.github_installation_id = githubInstallationId
        ? Number(githubInstallationId)
        : undefined;
      request.github_app_id = githubAppId ? Number(githubAppId) : undefined;
      request.github_private_key_path = githubPrivateKeyPath || undefined;
    }
    if (githubAccountType === "machine_user" || githubAccountType === "proxy_agent") {
      request.github_access_token = githubAccessToken || undefined;
    }
    return request;
  };

  const handleSaveMember = async () => {
    if (!canSubmit) {
      return;
    }
    setSavingMember(true);
    try {
      const request = buildMemberRequest();
      if (formMode === "edit" && editingPersonId) {
        if (!hasPersistedProject) {
          const previous =
            draftMembers.find((member) => member.person_id === editingPersonId) ??
            members.find((member) => member.person_id === editingPersonId);
          const delta = Number(effectiveIsActive) - Number(previous?.is_active ?? false);
          setDraftMembers((current) => [
            ...current.filter((member) => member.person_id !== editingPersonId),
            memberRequestToConfig({ ...request, original_person_id: editingPersonId }),
          ]);
          onMemberActiveDelta(delta);
          setEditingPersonId(personId.trim());
          return;
        }
        await updateMemberMutation.mutateAsync({
          originalPersonId: editingPersonId,
          body: {
            ...request,
            original_person_id: editingPersonId,
          },
        });
        if (!isHumanMember) {
          await memberIntelligenceSaveRef.current?.();
        }
        return;
      }
      await addMemberMutation.mutateAsync(request);
    } finally {
      setSavingMember(false);
    }
  };

  const startAddMode = () => {
    memberDiagnosticsMutation.reset();
    setMode("add");
    setEditingPersonId(null);
    clearForm({ withDefaults: true });
  };

  const startEditMode = (memberId: string) => {
    memberDiagnosticsMutation.reset();
    setEditingPersonId(memberId);
    setActiveTab(initialTab ?? "basic");
    const draft = draftMembers.find((member) => member.person_id === memberId);
    if (draft && !hasPersistedProject) {
      fillFormFromMember(draft);
      setMode("edit");
      return;
    }
    memberConfigMutation.mutate(memberId);
  };

  const handleDeleteMember = async () => {
    if (!editingPersonId || !configDir.trim() || !envFilePath.trim()) {
      return;
    }
    await deleteMemberMutation.mutateAsync({
      targetPersonId: editingPersonId,
      configDir,
      envFilePath,
    });
    setDeleteConfirmOpen(false);
  };

  return (
    <Card withBorder radius="md" p="lg">
      <Modal
        opened={deleteConfirmOpen}
        onClose={() => setDeleteConfirmOpen(false)}
        title={t("setup.members.deleteConfirmTitle")}
        centered
      >
        <Stack>
          <Text size="sm">
            {t("setup.members.deleteConfirmBody", {
              name: personName || editingPersonId || "",
            })}
          </Text>
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setDeleteConfirmOpen(false)}>
              {t("setup.members.cancelButton")}
            </Button>
            <Button
              color="danger"
              loading={deleteMemberMutation.isPending}
              onClick={() => void handleDeleteMember()}
            >
              {t("setup.members.deleteButton")}
            </Button>
          </Group>
        </Stack>
      </Modal>
      <PanelHeader title={t("setup.members.title")} subtitle={t("setup.members.subtitle")} />
      <Stack mt="md">
        {!hasActiveMember ? (
          <InfoCallout title={t("setup.members.requiredTitle")}>
            {t("setup.members.requiredBody")}
          </InfoCallout>
        ) : null}
        {displayedMembers.length > 0 ? (
          <Stack gap={6}>
            {displayedMembers.map((member) => (
              <Group key={member.person_id} justify="space-between">
                <Group gap="xs" align="center">
                  <Avatar
                    src={memberAvatarUrl(member.person_id, avatarTimestamp)}
                    size="sm"
                    radius="xl"
                  >
                    {member.name.substring(0, 2).toUpperCase()}
                  </Avatar>
                  <Text size="sm">
                    {member.name} ({member.person_id})
                  </Text>
                </Group>
                <Group gap="xs" wrap="nowrap">
                  <Group w={160} justify="flex-end" gap="xs" wrap="nowrap">
                    {member.person_type === "human" ? (
                      <Badge color="neutral" variant="light" style={{ flexShrink: 0 }}>
                        {t("setup.members.memberHuman")}
                      </Badge>
                    ) : (
                      <MemberCliAgentBadge
                        personId={member.person_id}
                        enabled={hasPersistedProject}
                      />
                    )}
                  </Group>
                  <Group w={56} justify="flex-end" gap="xs" wrap="nowrap">
                    {member.person_type !== "human" && (
                      <Badge
                        color={member.is_active ? "success" : "neutral"}
                        variant="light"
                        style={{ flexShrink: 0 }}
                      >
                        {member.is_active
                          ? t("setup.members.memberActive")
                          : t("setup.members.memberInactive")}
                      </Badge>
                    )}
                  </Group>
                  <Button
                    size="xs"
                    variant="default"
                    onClick={() => startEditMode(member.person_id)}
                  >
                    {t("setup.members.editButton")}
                  </Button>
                </Group>
              </Group>
            ))}
          </Stack>
        ) : null}
        {displayedMembers.length > 0 ? (
          <Group justify="space-between">
            {mode === "edit" && editingPersonId ? (
              <Badge variant="light">
                {t("setup.members.editingBadge", { id: editingPersonId })}
              </Badge>
            ) : (
              <span />
            )}
            <Button variant="default" onClick={startAddMode}>
              {t("setup.members.newButton")}
            </Button>
          </Group>
        ) : null}
        {formVisible ? (
          <>
            <Divider />
            <Text fw={600}>
              {formMode === "edit" ? t("setup.members.editTitle") : t("setup.members.addTitle")}
            </Text>
            <Tabs value={activeTab} onChange={setActiveTab}>
              <Tabs.List>
                <Tabs.Tab
                  value="basic"
                  rightSection={
                    basicTabHasError ? (
                      <TabErrorIcon label={t("setup.members.tabHasError")} />
                    ) : null
                  }
                >
                  {t("setup.members.tabs.basic")}
                </Tabs.Tab>
                {!isHumanMember ? (
                  <Tabs.Tab value="intelligence">{t("setup.members.tabs.intelligence")}</Tabs.Tab>
                ) : null}
                {!isHumanMember ? (
                  <Tabs.Tab value="patrol">{t("setup.members.tabs.patrol")}</Tabs.Tab>
                ) : null}
                <Tabs.Tab
                  value="github"
                  rightSection={
                    githubTabHasError ? (
                      <TabErrorIcon label={t("setup.members.tabHasError")} />
                    ) : null
                  }
                >
                  {t("setup.members.tabs.github")}
                </Tabs.Tab>
                <Tabs.Tab
                  value="slack"
                  rightSection={
                    slackTabHasError ? (
                      <TabErrorIcon label={t("setup.members.tabHasError")} />
                    ) : null
                  }
                >
                  {t("setup.members.tabs.slack")}
                </Tabs.Tab>
                {!isHumanMember ? (
                  <Tabs.Tab value="diagnostics">{t("setup.members.tabs.diagnostics")}</Tabs.Tab>
                ) : null}
              </Tabs.List>

              <Tabs.Panel value="basic" pt="md">
                <Stack>
                  <Group align="stretch" gap="md" mb="sm">
                    <Tooltip
                      label={t(
                        "setup.members.avatar.addFirstTooltip",
                        "You can set the avatar after adding the member.",
                      )}
                      disabled={!!editingPersonId}
                      position="top"
                      withArrow
                    >
                      <Avatar
                        src={
                          editingPersonId
                            ? memberAvatarUrl(editingPersonId, avatarTimestamp)
                            : undefined
                        }
                        size="xl"
                        radius="md"
                      >
                        {personName ? personName.substring(0, 2).toUpperCase() : "MB"}
                      </Avatar>
                    </Tooltip>
                    <Stack gap="xs" justify="space-between" style={{ flex: 1, minWidth: 0 }}>
                      <Group justify="space-between" align="center" wrap="nowrap" gap="xs">
                        <Text size="sm" fw={500}>
                          {personName || "—"} ({personId || "—"})
                        </Text>
                        {personType === "human" ? (
                          <Badge color="neutral" variant="light" style={{ flexShrink: 0 }}>
                            {t("setup.members.memberHuman")}
                          </Badge>
                        ) : (
                          cliAgentLabel && (
                            <Badge variant="light" color="neutral" style={{ flexShrink: 0 }}>
                              {cliAgentLabel}
                            </Badge>
                          )
                        )}
                      </Group>
                      <Stack gap="xs">
                        <Group gap="xs">
                          <FileButton
                            onChange={handleAvatarUpload}
                            accept="image/*"
                            disabled={!editingPersonId}
                          >
                            {(props) => (
                              <Button
                                {...props}
                                size="xs"
                                variant="default"
                                loading={importingAvatar}
                                disabled={!editingPersonId}
                              >
                                {t("setup.members.avatar.upload", "Upload File")}
                              </Button>
                            )}
                          </FileButton>

                          <Tooltip
                            label={t(
                              "setup.members.avatar.githubTooltip",
                              "Configure GitHub username to import avatar",
                            )}
                            disabled={!editingPersonId || !!githubUsername}
                            withArrow
                          >
                            <span>
                              <Button
                                size="xs"
                                variant="default"
                                onClick={handleImportFromGithub}
                                loading={importingAvatar}
                                disabled={!editingPersonId || !githubUsername}
                              >
                                {t("setup.members.avatar.github", "Import from GitHub")}
                              </Button>
                            </span>
                          </Tooltip>

                          <Tooltip
                            label={t(
                              "setup.members.avatar.slackTooltip",
                              "Configure Slack user ID to import avatar",
                            )}
                            disabled={!editingPersonId || personType !== "human" || !!slackUserId}
                            withArrow
                          >
                            <span>
                              <Button
                                size="xs"
                                variant="default"
                                onClick={handleImportFromSlack}
                                loading={importingAvatar}
                                disabled={
                                  !editingPersonId || (personType === "human" && !slackUserId)
                                }
                              >
                                {t("setup.members.avatar.slack", "Import from Slack")}
                              </Button>
                            </span>
                          </Tooltip>
                        </Group>
                        {avatarError && (
                          <Text size="xs" c="danger">
                            {avatarError}
                          </Text>
                        )}
                      </Stack>
                    </Stack>
                  </Group>
                  <TextInput
                    label={<RequiredLabel text={t("setup.members.personId")} />}
                    aria-label={t("setup.members.personId")}
                    aria-required
                    value={personId}
                    onChange={(event) => setPersonId(event.currentTarget.value)}
                    error={memberErrors.personId}
                  />
                  <TextInput
                    label={<RequiredLabel text={t("setup.members.personName")} />}
                    aria-label={t("setup.members.personName")}
                    aria-required
                    value={personName}
                    onChange={(event) => setPersonName(event.currentTarget.value)}
                    error={memberErrors.personName}
                  />
                  <Select
                    label={<RequiredLabel text={t("setup.members.type")} />}
                    aria-label={t("setup.members.type")}
                    aria-required
                    description={t("setup.members.memberTypeHint")}
                    data={MEMBER_TYPE_OPTIONS.map((option) => ({
                      value: option,
                      label: t(`setup.members.memberTypeOptions.${option}`),
                    }))}
                    value={personType}
                    onChange={(value) => {
                      const nextType = value === "human" ? "human" : "agent";
                      setPersonType(nextType);
                      if (nextType === "human") {
                        setIsActive(false);
                        setGithubAccountType("human");
                      } else if (githubAccountType === "human") {
                        setGithubAccountType("none");
                      }
                    }}
                  />
                  <MultiSelect
                    label={<RequiredLabel text={t("setup.members.roles")} />}
                    aria-label={t("setup.members.roles")}
                    aria-required
                    placeholder={t("setup.members.rolesPlaceholder")}
                    data={roleOptions}
                    value={roles}
                    onChange={setRoles}
                    searchable
                    clearable
                    nothingFoundMessage={t("setup.members.rolesEmpty")}
                    error={
                      rolesQuery.error ? t("setup.members.rolesLoadError") : memberErrors.roles
                    }
                    renderOption={({ option }) => {
                      const summary = roleSummaries[option.value];
                      return (
                        <Stack gap={2}>
                          <Text size="sm">{option.label}</Text>
                          {summary ? (
                            <Text size="xs" c="dimmed">
                              {summary}
                            </Text>
                          ) : null}
                        </Stack>
                      );
                    }}
                  />
                  {!isHumanMember ? (
                    <>
                      <SegmentedControl
                        fullWidth
                        data={SPEAKING_STYLE_OPTIONS.map((value) => ({
                          value,
                          label: t(`setup.members.speakingStyleOptions.${value}`),
                        }))}
                        value={speakingStylePreset}
                        onChange={(value) => {
                          const preset = (value as SpeakingStylePreset) ?? "energetic";
                          setSpeakingStylePreset(preset);
                          applyPresetFields(preset);
                        }}
                      />
                      <Group justify="flex-end" mt={-4}>
                        <Button
                          size="xs"
                          variant="default"
                          leftSection={<Eraser size={14} />}
                          onClick={clearPresetFields}
                        >
                          {t("setup.members.clearDefaults")}
                        </Button>
                      </Group>
                      <TagsInput
                        label={
                          <DefaultableLabel
                            text={t("setup.members.characterTraits")}
                            tooltip={t("setup.members.applyDefaultTooltip")}
                            onApply={() => setCharacterTraits(activePresetSample.traits)}
                            required
                          />
                        }
                        aria-required
                        value={characterTraits}
                        onChange={setCharacterTraits}
                        placeholder={activePresetSample.traits.join(", ")}
                        error={memberErrors.characterTraits}
                      />
                      <TagsInput
                        label={
                          <DefaultableLabel
                            text={t("setup.members.characterInterests")}
                            tooltip={t("setup.members.applyDefaultTooltip")}
                            onApply={() => setCharacterInterests(activePresetSample.interests)}
                            required
                          />
                        }
                        aria-required
                        value={characterInterests}
                        onChange={setCharacterInterests}
                        placeholder={activePresetSample.interests.join(", ")}
                        error={memberErrors.characterInterests}
                      />
                      <Textarea
                        label={
                          <DefaultableLabel
                            text={t("setup.members.speakingStyle")}
                            tooltip={t("setup.members.applyDefaultTooltip")}
                            onApply={() =>
                              setSpeakingStyle(
                                speakingStyleTemplates[speakingStylePreset || "energetic"],
                              )
                            }
                            required
                          />
                        }
                        aria-required
                        autosize
                        minRows={3}
                        value={speakingStyle}
                        onChange={(event) => setSpeakingStyle(event.currentTarget.value)}
                        placeholder={speakingStyleTemplates[speakingStylePreset || "energetic"]}
                        error={memberErrors.speakingStyle}
                      />
                      <TextInput
                        label={
                          <DefaultableLabel
                            text={t("setup.members.characterArchetype")}
                            tooltip={t("setup.members.applyDefaultTooltip")}
                            onApply={() => setCharacterArchetype(activePresetSample.archetype)}
                            required
                          />
                        }
                        aria-required
                        value={characterArchetype}
                        onChange={(event) => setCharacterArchetype(event.currentTarget.value)}
                        description={t("setup.members.characterArchetypeHint")}
                        placeholder={activePresetSample.archetype}
                        error={memberErrors.characterArchetype}
                      />
                      <Textarea
                        label={
                          <DefaultableLabel
                            text={t("setup.members.characterJoinWhen")}
                            tooltip={t("setup.members.applyDefaultTooltip")}
                            onApply={() =>
                              setCharacterJoinWhenText(activePresetSample.joinWhen.join("\n"))
                            }
                            required
                          />
                        }
                        aria-required
                        autosize
                        minRows={3}
                        value={characterJoinWhenText}
                        onChange={(event) => setCharacterJoinWhenText(event.currentTarget.value)}
                        description={t("setup.members.characterListHint")}
                        placeholder={activePresetSample.joinWhen.join("\n")}
                        error={memberErrors.characterJoinWhenText}
                      />
                      <Textarea
                        label={
                          <DefaultableLabel
                            text={t("setup.members.characterAvoidWhen")}
                            tooltip={t("setup.members.applyDefaultTooltip")}
                            onApply={() =>
                              setCharacterAvoidWhenText(activePresetSample.avoidWhen.join("\n"))
                            }
                            required
                          />
                        }
                        aria-required
                        autosize
                        minRows={3}
                        value={characterAvoidWhenText}
                        onChange={(event) => setCharacterAvoidWhenText(event.currentTarget.value)}
                        description={t("setup.members.characterListHint")}
                        placeholder={activePresetSample.avoidWhen.join("\n")}
                        error={memberErrors.characterAvoidWhenText}
                      />
                      <Textarea
                        label={
                          <DefaultableLabel
                            text={t("setup.members.characterContributionStyle")}
                            tooltip={t("setup.members.applyDefaultTooltip")}
                            onApply={() =>
                              setCharacterContributionText(
                                activePresetSample.contributionStyle.join("\n"),
                              )
                            }
                            required
                          />
                        }
                        aria-required
                        autosize
                        minRows={3}
                        value={characterContributionText}
                        onChange={(event) =>
                          setCharacterContributionText(event.currentTarget.value)
                        }
                        description={t("setup.members.characterListHint")}
                        placeholder={activePresetSample.contributionStyle.join("\n")}
                        error={memberErrors.characterContributionText}
                      />
                      <Textarea
                        label={t("setup.members.relationships")}
                        autosize
                        minRows={2}
                        value={relationships}
                        onChange={(event) => setRelationships(event.currentTarget.value)}
                        placeholder={activePresetSample.relationships}
                      />
                    </>
                  ) : null}
                  <Switch
                    label={t("setup.members.activeSwitch")}
                    description={
                      personType === "human" ? t("setup.members.activeHumanHint") : undefined
                    }
                    checked={effectiveIsActive}
                    disabled={personType === "human"}
                    onChange={(event) => setIsActive(event.currentTarget.checked)}
                  />
                </Stack>
              </Tabs.Panel>

              {!isHumanMember ? (
                <Tabs.Panel value="intelligence" pt="md">
                  {formMode === "edit" && editingPersonId ? (
                    <IntelligenceEditor
                      personId={editingPersonId}
                      savePersonId={personId.trim()}
                      enabled={Boolean(configDir)}
                      detections={cliDetections}
                      llmProviderAvailability={llmProviderAvailability}
                      providers={providers}
                      saveMode="external"
                      onRegisterSave={(save) => {
                        memberIntelligenceSaveRef.current = save;
                      }}
                    />
                  ) : (
                    <Text size="sm" c="dimmed">
                      {t("setup.members.saveBeforeIntelligence")}
                    </Text>
                  )}
                </Tabs.Panel>
              ) : null}

              {!isHumanMember ? (
                <Tabs.Panel value="patrol" pt="md">
                  <PatrolSettingsEditor
                    commandCatalog={commandCatalog}
                    commandOptionByValue={commandOptionByValue}
                    routineCommandCatalog={routineCommandCatalog}
                    commandOptionsLoading={commandOptions.isLoading}
                    routineCommandOptionsLoading={routineCommandOptions.isLoading}
                    routineCommandOptionsError={Boolean(routineCommandOptions.error)}
                    routineOverrideEnabled={routineOverrideEnabled}
                    routineCommands={routineCommands}
                    scheduledCommands={scheduledCommands}
                    onRoutineOverrideChange={(enabled) => {
                      routineDefaultDismissedRef.current = !enabled;
                      if (enabled) {
                        seedDefaultRoutineCommands();
                      } else {
                        setRoutineOverrideEnabled(enabled);
                      }
                    }}
                    onRoutineCommandsChange={setRoutineCommands}
                    onScheduledCommandsChange={setScheduledCommands}
                  />
                </Tabs.Panel>
              ) : null}

              <Tabs.Panel value="github" pt="md">
                <Stack>
                  <Select
                    label={t("setup.members.githubAccountType")}
                    description={t("setup.members.githubAccountTypeHint")}
                    data={GITHUB_ACCOUNT_TYPE_OPTIONS.map((option) => ({
                      value: option,
                      label: t(`setup.members.githubAccountTypeOptions.${option}`),
                    }))}
                    value={githubAccountType}
                    disabled={personType === "human"}
                    onChange={(value) => {
                      const nextAccountType = toGitHubAccountType(value ?? "none");
                      setGithubAccountType(nextAccountType);
                      if (
                        formMode === "add" &&
                        personType !== "human" &&
                        nextAccountType !== "none" &&
                        !routineDefaultDismissedRef.current &&
                        routineCommands.length === 0
                      ) {
                        routineDefaultDismissedRef.current = false;
                        seedDefaultRoutineCommands();
                      }
                      setIdentityResolveError("");
                    }}
                  />
                  {!usesGitHubMember ? (
                    <Text size="sm" c="dimmed">
                      {t("setup.members.githubDisabledMemberHint")}
                    </Text>
                  ) : (
                    <>
                      <Stack gap={4}>
                        <div>
                          <Text fw={500} size="sm">
                            {githubResolveLabel}
                            <Text span c="danger" inherit aria-hidden="true">
                              {" *"}
                            </Text>
                          </Text>
                          <Text c="dimmed" size="xs">
                            {githubResolveDescription}
                          </Text>
                        </div>
                        <div className="field-action-row">
                          <TextInput
                            aria-label={githubResolveLabel}
                            aria-required
                            value={githubResolveValue}
                            onChange={(event) => {
                              if (githubAccountType === "github_apps") {
                                setIdentity(event.currentTarget.value);
                              } else {
                                setGithubUsername(event.currentTarget.value);
                              }
                              setIdentityResolveError("");
                            }}
                            error={Boolean(githubResolveError)}
                            flex={1}
                          />
                          <Button
                            variant="default"
                            loading={resolveMutation.isPending}
                            disabled={!canResolveIdentity}
                            onClick={() => void handleResolve()}
                          >
                            {t("setup.members.resolve")}
                          </Button>
                        </div>
                        {githubResolveError ? (
                          <Text c="danger" size="xs">
                            {githubResolveError}
                          </Text>
                        ) : null}
                      </Stack>
                      {githubAccountType === "github_apps" ? (
                        <TextInput
                          label={<RequiredLabel text={t("setup.members.githubResolvedIdentity")} />}
                          aria-label={t("setup.members.githubResolvedIdentity")}
                          aria-required
                          value={githubUsername}
                          onChange={(event) => setGithubUsername(event.currentTarget.value)}
                          error={memberErrors.githubUsername}
                        />
                      ) : null}
                      <TextInput
                        label={
                          usesGitHubMember ? (
                            <RequiredLabel text={t("setup.members.gitEmail")} />
                          ) : (
                            t("setup.members.gitEmail")
                          )
                        }
                        aria-label={t("setup.members.gitEmail")}
                        aria-required={usesGitHubMember}
                        value={gitEmail}
                        onChange={(event) => setGitEmail(event.currentTarget.value)}
                        error={memberErrors.gitEmail}
                      />
                    </>
                  )}
                  {githubAccountType === "github_apps" ? (
                    <>
                      <TextInput
                        label={<RequiredLabel text={t("setup.members.installationId")} />}
                        aria-label={t("setup.members.installationId")}
                        aria-required
                        value={githubInstallationId}
                        onChange={(event) => setGithubInstallationId(event.currentTarget.value)}
                        error={memberErrors.githubInstallationId}
                      />
                      <TextInput
                        label={<RequiredLabel text={t("setup.members.appId")} />}
                        aria-label={t("setup.members.appId")}
                        aria-required
                        value={githubAppId}
                        onChange={(event) => setGithubAppId(event.currentTarget.value)}
                        error={memberErrors.githubAppId}
                      />
                      <FilePicker
                        label={t("setup.members.privateKeyPath")}
                        withAsterisk={!storedMemberSecrets.githubPrivateKeyPath}
                        value={githubPrivateKeyPath}
                        onChange={setGithubPrivateKeyPath}
                        error={memberErrors.githubPrivateKeyPath}
                      />
                    </>
                  ) : null}
                  {githubAccountType === "machine_user" || githubAccountType === "proxy_agent" ? (
                    <PasswordInput
                      label={
                        storedMemberSecrets.githubAccessToken ? (
                          t("setup.members.accessToken")
                        ) : (
                          <RequiredLabel text={t("setup.members.accessToken")} />
                        )
                      }
                      aria-label={t("setup.members.accessToken")}
                      aria-required={!storedMemberSecrets.githubAccessToken}
                      placeholder={
                        storedMemberSecrets.githubAccessToken
                          ? MASKED_SECRET_PLACEHOLDER
                          : t("setup.members.accessTokenPlaceholder")
                      }
                      value={githubAccessToken}
                      onChange={(event) => setGithubAccessToken(event.currentTarget.value)}
                      error={memberErrors.githubAccessToken}
                    />
                  ) : null}
                  {githubAccountType === "human" ? (
                    <Text size="sm" c="dimmed">
                      {t("setup.members.githubAuthNotRequired")}
                    </Text>
                  ) : null}
                </Stack>
              </Tabs.Panel>

              <Tabs.Panel value="slack" pt="md">
                <Stack>
                  {personType === "human" ? (
                    <TextInput
                      label={<RequiredLabel text={t("setup.members.slackUserId")} />}
                      aria-label={t("setup.members.slackUserId")}
                      aria-required
                      value={slackUserId}
                      onChange={(event) => setSlackUserId(event.currentTarget.value)}
                      description={t("setup.members.slackUserIdHint")}
                      error={memberErrors.slackUserId}
                    />
                  ) : (
                    <>
                      <Stack gap="xs">
                        <Group align="end">
                          <TextInput
                            className="member-slack-channel-input"
                            label={t("setup.members.slackChannelAdd")}
                            description={t("setup.members.slackChannelAddHint")}
                            value={slackChannelInput}
                            onChange={(event) => {
                              setSlackChannelInput(event.currentTarget.value);
                              setSlackChannelInputError(null);
                            }}
                            onKeyDown={(event) => {
                              if (event.key === "Enter" && !event.nativeEvent.isComposing) {
                                event.preventDefault();
                                addSlackChannel();
                              }
                            }}
                            error={slackChannelInputError ?? memberErrors.slackChannelsText}
                          />
                          <Button leftSection={<Plus size={16} />} onClick={addSlackChannel}>
                            {t("setup.members.slackChannelAddButton")}
                          </Button>
                        </Group>
                        {slackChannels.length === 0 ? (
                          <div className="empty-row">{t("setup.members.slackChannelsEmpty")}</div>
                        ) : (
                          <Stack gap="sm">
                            {slackChannels.map((channel, index) => {
                              const channelRef = normalizeSlackChannelReference(channel);
                              const selectedPolicy =
                                slackChannelParticipation[channelRef] ?? "strict";
                              return (
                                <Group key={`${channelRef}-${index}`} align="start">
                                  <TextInput
                                    className="member-slack-channel-input"
                                    label={t("setup.members.slackParticipationChannel")}
                                    value={channel}
                                    onChange={(event) =>
                                      updateSlackChannel(index, event.currentTarget.value)
                                    }
                                  />
                                  <Select
                                    className="member-slack-policy-select"
                                    label={t("setup.members.slackParticipationPolicy")}
                                    data={CHAT_PARTICIPATION_OPTIONS.map((option) => ({
                                      value: option,
                                      label: t(`setup.members.slackParticipationOptions.${option}`),
                                    }))}
                                    value={selectedPolicy}
                                    onChange={(value) =>
                                      setSlackChannelParticipation((current) => ({
                                        ...current,
                                        [channelRef]: toChatParticipationPolicy(value),
                                      }))
                                    }
                                    renderOption={({ option }) => (
                                      <Stack gap={2}>
                                        <Text size="sm">{option.label}</Text>
                                        <Text size="xs" c="dimmed">
                                          {t(
                                            `setup.members.slackParticipationDescriptions.${option.value}`,
                                          )}
                                        </Text>
                                      </Stack>
                                    )}
                                  />
                                  <ActionIcon
                                    aria-label={t("setup.members.slackChannelRemove")}
                                    color="danger"
                                    mt={25}
                                    variant="subtle"
                                    onClick={() => removeSlackChannel(index)}
                                  >
                                    <Trash2 size={16} />
                                  </ActionIcon>
                                </Group>
                              );
                            })}
                          </Stack>
                        )}
                      </Stack>
                      <PasswordInput
                        label={
                          slackChannelsConfigured && !storedMemberSecrets.slackBotToken ? (
                            <RequiredLabel text={t("setup.members.slackBotToken")} />
                          ) : (
                            t("setup.members.slackBotToken")
                          )
                        }
                        aria-label={t("setup.members.slackBotToken")}
                        aria-required={
                          slackChannelsConfigured && !storedMemberSecrets.slackBotToken
                        }
                        placeholder={
                          storedMemberSecrets.slackBotToken
                            ? MASKED_SECRET_PLACEHOLDER
                            : t("setup.members.slackBotTokenPlaceholder")
                        }
                        value={slackBotToken}
                        onChange={(event) => setSlackBotToken(event.currentTarget.value)}
                        error={memberErrors.slackBotToken}
                      />
                      <PasswordInput
                        label={
                          slackChannelsConfigured && !storedMemberSecrets.slackAppToken ? (
                            <RequiredLabel text={t("setup.members.slackAppToken")} />
                          ) : (
                            t("setup.members.slackAppToken")
                          )
                        }
                        aria-label={t("setup.members.slackAppToken")}
                        aria-required={
                          slackChannelsConfigured && !storedMemberSecrets.slackAppToken
                        }
                        placeholder={
                          storedMemberSecrets.slackAppToken
                            ? MASKED_SECRET_PLACEHOLDER
                            : t("setup.members.slackAppTokenPlaceholder")
                        }
                        value={slackAppToken}
                        onChange={(event) => setSlackAppToken(event.currentTarget.value)}
                        error={memberErrors.slackAppToken}
                      />
                    </>
                  )}
                </Stack>
              </Tabs.Panel>
              {!isHumanMember ? (
                <Tabs.Panel value="diagnostics" pt="md">
                  <MemberDiagnosticsPanel
                    personId={editingPersonId}
                    formMode={formMode}
                    loading={memberDiagnosticsMutation.isPending}
                    error={memberDiagnosticsMutation.error}
                    checks={memberDiagnosticsMutation.data?.checks ?? []}
                    onRun={() => {
                      if (editingPersonId) {
                        memberDiagnosticsMutation.mutate(editingPersonId);
                      }
                    }}
                  />
                </Tabs.Panel>
              ) : null}
            </Tabs>
            <Divider />
            <Group justify="space-between" className="form-footer">
              <Box>
                {formMode === "edit" ? (
                  <Button
                    color="danger"
                    variant="default"
                    loading={deleteMemberMutation.isPending}
                    onClick={() => setDeleteConfirmOpen(true)}
                  >
                    {t("setup.members.deleteButton")}
                  </Button>
                ) : null}
              </Box>
              <Button
                loading={
                  savingMember || addMemberMutation.isPending || updateMemberMutation.isPending
                }
                disabled={!canSubmit}
                onClick={() => void handleSaveMember()}
              >
                {formMode === "edit" ? t("setup.members.saveButton") : t("setup.members.addButton")}
              </Button>
            </Group>
          </>
        ) : null}
        {memberConfigMutation.error ? (
          <Alert color="danger" title={t("setup.members.loadError")}>
            {memberConfigMutation.error.message}
          </Alert>
        ) : null}
        {resolveMutation.error ? (
          <Alert color="danger" title={t("setup.members.resolveError")}>
            {resolveMutation.error.message}
          </Alert>
        ) : null}
        {addMemberMutation.error ? (
          <Alert color="danger" title={t("setup.members.addError")}>
            {addMemberMutation.error.message}
          </Alert>
        ) : null}
        {updateMemberMutation.error ? (
          <Alert color="danger" title={t("setup.members.updateError")}>
            {updateMemberMutation.error.message}
          </Alert>
        ) : null}
        {deleteMemberMutation.error ? (
          <Alert color="danger" title={t("setup.members.deleteError")}>
            {deleteMemberMutation.error.message}
          </Alert>
        ) : null}
      </Stack>
    </Card>
  );
}

function PatrolSettingsEditor({
  commandCatalog,
  commandOptionByValue,
  routineCommandCatalog,
  commandOptionsLoading,
  routineCommandOptionsLoading,
  routineCommandOptionsError,
  routineOverrideEnabled,
  routineCommands,
  scheduledCommands,
  onRoutineOverrideChange,
  onRoutineCommandsChange,
  onScheduledCommandsChange,
}: {
  commandCatalog: CommandOption[];
  commandOptionByValue: Map<string, CommandOption>;
  routineCommandCatalog: CommandOption[];
  commandOptionsLoading: boolean;
  routineCommandOptionsLoading: boolean;
  routineCommandOptionsError: boolean;
  routineOverrideEnabled: boolean;
  routineCommands: string[];
  scheduledCommands: ScheduledCommandDraft[];
  onRoutineOverrideChange: (enabled: boolean) => void;
  onRoutineCommandsChange: (commands: string[]) => void;
  onScheduledCommandsChange: (commands: ScheduledCommandDraft[]) => void;
}) {
  const { t } = useTranslation();
  const routineOptionByValue = new Map(
    routineCommandCatalog.map((option) => [option.command, option]),
  );
  const routineCommandOptions = [
    ...routineCommandCatalog.map((option) => ({
      value: option.command,
      label: `${option.label} (${option.command})`,
      disabled: option.routine_eligible === false,
    })),
    ...routineCommands
      .filter((command) => command.trim() && !routineOptionByValue.has(command))
      .map((command) => ({ value: command, label: command })),
  ];
  const scheduledCommandOptions = commandCatalog.map((option) => ({
    value: option.command,
    label: `${option.label} (${option.command})`,
  }));
  const routineSelectionError =
    routineCommands.length === 0
      ? t("setup.members.patrol.routineRequired")
      : routineCommands.some(
            (command) => routineOptionByValue.get(command)?.routine_eligible === false,
          )
        ? t("setup.members.patrol.routineIneligible")
        : undefined;
  const updateScheduled = (
    id: string,
    recipe: (current: ScheduledCommandDraft) => ScheduledCommandDraft,
  ) => {
    onScheduledCommandsChange(
      scheduledCommands.map((command) => (command.id === id ? recipe(command) : command)),
    );
  };

  return (
    <Stack>
      <InfoCallout title={t("setup.members.patrol.title")}>
        {t("setup.members.patrol.description")}
      </InfoCallout>
      <Switch
        checked={routineOverrideEnabled}
        label={t("setup.members.patrol.overrideRoutine")}
        description={t("setup.members.patrol.overrideRoutineHint")}
        disabled={routineCommandOptionsLoading || routineCommandOptionsError}
        onChange={(event) => onRoutineOverrideChange(event.currentTarget.checked)}
      />
      {routineCommandOptionsLoading ? (
        <Text size="sm" c="dimmed">
          {t("setup.members.patrol.loadingCommands")}
        </Text>
      ) : null}
      {routineCommandOptionsError ? (
        <Alert color="danger" title={t("setup.members.patrol.routineLoadError")} />
      ) : null}
      {routineOverrideEnabled ? (
        <MultiSelect
          label={t("setup.members.patrol.routineCommands")}
          data={routineCommandOptions}
          value={routineCommands}
          onChange={onRoutineCommandsChange}
          disabled={routineCommandOptionsLoading || routineCommandOptionsError}
          searchable
          clearable
          error={routineSelectionError}
          nothingFoundMessage={t("commands.noCommandOptions")}
          renderOption={({ option }) => {
            const commandOption = routineOptionByValue.get(option.value);
            return (
              <CommandOptionRow
                label={option.label}
                option={commandOption}
                note={
                  commandOption?.routine_eligible === false
                    ? t("setup.members.patrol.routineIneligible")
                    : undefined
                }
              />
            );
          }}
        />
      ) : (
        <Text size="sm" c="dimmed">
          {t("setup.members.patrol.noRoutineCommands")}
        </Text>
      )}

      <Divider />
      <Group justify="space-between" align="center">
        <Box>
          <Text fw={700}>{t("setup.members.patrol.scheduledCommands")}</Text>
          <Text size="sm" c="dimmed">
            {t("setup.members.patrol.scheduledCommandsHint")}
          </Text>
        </Box>
        <Button
          size="xs"
          variant="default"
          leftSection={<Plus size={14} />}
          onClick={() =>
            onScheduledCommandsChange([
              ...scheduledCommands,
              createScheduledCommandDraft(commandCatalog[0]?.command ?? ""),
            ])
          }
        >
          {t("setup.members.patrol.addSchedule")}
        </Button>
      </Group>

      {commandOptionsLoading ? (
        <Text size="sm" c="dimmed">
          {t("setup.members.patrol.loadingCommands")}
        </Text>
      ) : null}

      {scheduledCommands.length === 0 ? (
        <div className="empty-row">{t("setup.members.patrol.noSchedules")}</div>
      ) : (
        <Stack>
          {scheduledCommands.map((draft) => {
            const option =
              draft.commandMode === "catalog"
                ? (commandOptionByValue.get(draft.command) ?? null)
                : null;
            const cron = draftToCron(draft);
            const cronError = isValidCron(cron) ? "" : t("setup.members.patrol.cronInvalid");
            return (
              <Card withBorder radius="sm" p="md" key={draft.id}>
                <Stack>
                  <Group justify="space-between" align="center">
                    <SegmentedControl
                      size="xs"
                      value={draft.commandMode}
                      onChange={(value) =>
                        updateScheduled(draft.id, (current) => ({
                          ...current,
                          commandMode: value as ScheduledCommandDraft["commandMode"],
                        }))
                      }
                      data={[
                        { value: "catalog", label: t("commands.modeCatalog") },
                        { value: "custom", label: t("commands.modeCustom") },
                      ]}
                    />
                    <ActionIcon
                      aria-label={t("setup.members.patrol.removeSchedule")}
                      color="danger"
                      variant="subtle"
                      onClick={() =>
                        onScheduledCommandsChange(
                          scheduledCommands.filter((command) => command.id !== draft.id),
                        )
                      }
                    >
                      <Trash2 size={16} />
                    </ActionIcon>
                  </Group>

                  {draft.commandMode === "catalog" ? (
                    <Select
                      label={t("commands.command")}
                      searchable
                      nothingFoundMessage={t("commands.noCommandOptions")}
                      value={draft.command}
                      data={scheduledCommandOptions}
                      onChange={(value) =>
                        updateScheduled(draft.id, (current) => ({
                          ...current,
                          command: value ?? "",
                          argValues: {},
                          rawArgs: "",
                        }))
                      }
                      renderOption={({ option: selectOption }) => (
                        <CommandOptionRow
                          label={selectOption.label}
                          option={commandOptionByValue.get(selectOption.value)}
                        />
                      )}
                    />
                  ) : (
                    <TextInput
                      label={t("commands.command")}
                      value={draft.customCommand}
                      onChange={(event) =>
                        updateScheduled(draft.id, (current) => ({
                          ...current,
                          customCommand: event.currentTarget.value,
                        }))
                      }
                    />
                  )}

                  {option ? <CommandOptionSummary option={option} /> : null}
                  {draft.commandMode === "catalog" && option?.arguments.length ? (
                    <div className="command-args-grid">
                      {option.arguments.map((argument) => (
                        <TextInput
                          key={`${draft.id}-${argument.kind}-${argument.name}`}
                          label={`${argument.name}${argument.required ? " *" : ""}`}
                          placeholder={argument.default || argument.kind}
                          value={draft.argValues[argument.name] ?? ""}
                          onChange={(event) =>
                            updateScheduled(draft.id, (current) => ({
                              ...current,
                              argValues: {
                                ...current.argValues,
                                [argument.name]: event.currentTarget.value,
                              },
                            }))
                          }
                        />
                      ))}
                    </div>
                  ) : null}
                  {draft.commandMode === "custom" || !option?.arguments.length ? (
                    <TextInput
                      label={t("commands.rawArgs")}
                      placeholder={t("commands.rawArgsPlaceholder")}
                      value={draft.rawArgs}
                      onChange={(event) =>
                        updateScheduled(draft.id, (current) => ({
                          ...current,
                          rawArgs: event.currentTarget.value,
                        }))
                      }
                    />
                  ) : null}

                  <SegmentedControl
                    value={draft.scheduleMode}
                    onChange={(value) =>
                      updateScheduled(draft.id, (current) => ({
                        ...current,
                        scheduleMode: value as CronPreset,
                      }))
                    }
                    data={[
                      { value: "weekly", label: t("setup.members.patrol.cronPresets.weekly") },
                      { value: "daily", label: t("setup.members.patrol.cronPresets.daily") },
                      { value: "hourly", label: t("setup.members.patrol.cronPresets.hourly") },
                      { value: "custom", label: t("setup.members.patrol.cronPresets.custom") },
                    ]}
                  />
                  {draft.scheduleMode === "custom" ? (
                    <TextInput
                      label={t("setup.members.patrol.cron")}
                      description={t("setup.members.patrol.cronHint")}
                      value={draft.cron}
                      error={cronError}
                      onChange={(event) =>
                        updateScheduled(draft.id, (current) => ({
                          ...current,
                          cron: event.currentTarget.value,
                        }))
                      }
                    />
                  ) : (
                    <div className="schedule-grid">
                      {draft.scheduleMode === "weekly" ? (
                        <Select
                          label={t("setup.members.patrol.weekday")}
                          value={draft.weekday}
                          data={WEEKDAY_OPTIONS.map((day) => ({
                            value: day,
                            label: t(`setup.members.patrol.weekdays.${day}`),
                          }))}
                          onChange={(value) =>
                            updateScheduled(draft.id, (current) => ({
                              ...current,
                              weekday: value ?? "1",
                            }))
                          }
                        />
                      ) : (
                        <div className="schedule-empty-cell" />
                      )}
                      {draft.scheduleMode !== "hourly" ? (
                        <NumberInput
                          label={t("setup.members.patrol.hour")}
                          min={0}
                          max={23}
                          allowDecimal={false}
                          value={draft.hour}
                          onChange={(value) =>
                            updateScheduled(draft.id, (current) => ({
                              ...current,
                              hour: typeof value === "number" ? value : 9,
                            }))
                          }
                        />
                      ) : (
                        <div className="schedule-empty-cell" />
                      )}
                      <NumberInput
                        label={t("setup.members.patrol.minute")}
                        min={0}
                        max={59}
                        allowDecimal={false}
                        value={draft.minute}
                        onChange={(value) =>
                          updateScheduled(draft.id, (current) => ({
                            ...current,
                            minute: typeof value === "number" ? value : 0,
                          }))
                        }
                      />
                      <TextInput
                        classNames={{ input: "readonly-cron-input" }}
                        label={t("setup.members.patrol.generatedCron")}
                        value={cron}
                        readOnly
                        error={cronError}
                      />
                    </div>
                  )}
                </Stack>
              </Card>
            );
          })}
        </Stack>
      )}
    </Stack>
  );
}

function CommandOptionRow({
  label,
  option,
  note,
}: {
  label: string;
  option: CommandOption | undefined;
  note?: string;
}) {
  return (
    <Stack gap={2}>
      <Text size="sm">{label}</Text>
      {option?.description ? (
        <Text size="xs" c="dimmed">
          {option.description}
        </Text>
      ) : null}
      {note ? (
        <Text size="xs" c="danger">
          {note}
        </Text>
      ) : null}
    </Stack>
  );
}

function CommandOptionSummary({ option }: { option: CommandOption }) {
  const { t } = useTranslation();
  return (
    <div className="command-option-summary">
      <Group gap="xs">
        <Badge variant="outline">{t(`commands.sources.${option.source}`)}</Badge>
        {option.requirements.map((requirement) => (
          <Badge
            key={requirement.kind}
            color={requirement.satisfied ? "success" : "warning"}
            variant="light"
          >
            {t(`commands.requirements.${requirement.kind}`)}
          </Badge>
        ))}
      </Group>
      {option.description ? (
        <Text c="dimmed" size="sm">
          {option.description}
        </Text>
      ) : null}
      <div className="command-script-path">
        <Anchor
          href={localFileHref(option.path)}
          size="sm"
          title={option.path}
          onClick={(event) => {
            if (!isTauriRuntime()) {
              return;
            }
            event.preventDefault();
            void openLocalFile(option.path).catch(console.error);
          }}
        >
          {option.path}
        </Anchor>
        <Tooltip label={t("commands.copyScriptPath")}>
          <ActionIcon
            aria-label={t("commands.copyScriptPath")}
            size="sm"
            variant="subtle"
            onClick={() => void navigator.clipboard?.writeText(option.path).catch(console.error)}
          >
            <Copy size={14} />
          </ActionIcon>
        </Tooltip>
      </div>
    </div>
  );
}

function LaneField({
  label,
  placeholder,
  description,
  choices,
  inputProps,
  error,
}: {
  label: string;
  placeholder: string;
  description?: string;
  choices: string[];
  inputProps: ReturnType<ProjectForm["getInputProps"]>;
  error?: ReactNode;
}) {
  // When status options were read from the Project, pick strictly from them: a
  // non-searchable Select shows every option (no filtering) and disallows
  // values that are not real board lanes. Only fall back to free text when no
  // options could be read.
  if (choices.length > 0) {
    return (
      <Select
        label={label}
        aria-label={label}
        placeholder={placeholder}
        description={description}
        data={choices}
        searchable={false}
        allowDeselect={false}
        {...inputProps}
        error={error}
      />
    );
  }
  return (
    <TextInput
      label={label}
      aria-label={label}
      placeholder={placeholder}
      description={description}
      {...inputProps}
      error={error}
    />
  );
}

function buildLaneFetchTarget(values: ProjectFormValues): ProjectStatusOptionsRequest | null {
  const parsed = parseGitHub(values.githubProjectUrl);
  if (!parsed.projectValid) {
    return null;
  }
  return {
    owner: parsed.owner,
    project_id: parsed.projectId,
    github_project_url: parsed.projectUrl,
  };
}

function GitHubIntegrationSection({ form }: { form: ProjectForm }) {
  const { t } = useTranslation();
  const githubErrors = getGitHubFieldErrors(form.values, t);
  const githubEnabled = form.values.githubDecision === "enabled";

  // Lane status options are fetched live for the entered Project URL (not the
  // saved project) so they appear before saving. The fetch target is seeded
  // from the current form values so an already-configured Project URL loads its
  // lanes as soon as the section opens, and is refreshed when the URL changes.
  const [laneFetchTarget, setLaneFetchTarget] = useState<ProjectStatusOptionsRequest | null>(() =>
    buildLaneFetchTarget(form.values),
  );
  const statusOptions = useQuery({
    queryKey: ["projectStatusOptions", laneFetchTarget],
    queryFn: () => getProjectStatusOptions(laneFetchTarget as ProjectStatusOptionsRequest),
    enabled: githubEnabled && laneFetchTarget !== null,
  });
  const laneChoices =
    githubEnabled && laneFetchTarget !== null && statusOptions.data?.available
      ? statusOptions.data.statuses
      : [];

  // Always update the target (to null for an invalid/cleared URL) so the lane
  // Selects never keep showing options fetched for a different project.
  const refreshLaneOptions = () => {
    setLaneFetchTarget(buildLaneFetchTarget(form.values));
  };

  const projectUrlProps = form.getInputProps("githubProjectUrl");

  if (!githubEnabled) {
    return (
      <Card withBorder radius="md" p="lg">
        <PanelHeader title={t("setup.github.title")} subtitle={t("setup.github.subtitle")} />
        <Box mt="md">
          <InfoCallout title={t("setup.github.disabledTitle")}>
            {t("setup.github.disabledHint")}
          </InfoCallout>
        </Box>
      </Card>
    );
  }

  return (
    <Card withBorder radius="md" p="lg">
      <PanelHeader title={t("setup.github.title")} subtitle={t("setup.github.subtitle")} />
      <Stack mt="md">
        <TextInput
          label={<RequiredLabel text={t("setup.github.projectUrl")} />}
          aria-label={t("setup.github.projectUrl")}
          aria-required
          description={t("setup.github.projectUrlHint")}
          {...projectUrlProps}
          onBlur={(event) => {
            projectUrlProps.onBlur?.(event);
            refreshLaneOptions();
          }}
          error={githubErrors.githubProjectUrl || form.errors.githubProjectUrl}
        />
        <Fieldset legend={t("setup.github.laneMapping")} radius="md">
          <Stack>
            <Text size="sm" c="dimmed">
              {laneChoices.length > 0
                ? t("setup.github.laneMappingHint")
                : t("setup.github.laneMappingManualHint")}
            </Text>
            <LaneField
              label={t("setup.github.laneReady")}
              placeholder={DEFAULT_LANE_READY}
              choices={laneChoices}
              inputProps={form.getInputProps("laneReady")}
            />
            <LaneField
              label={t("setup.github.laneWorking")}
              placeholder={DEFAULT_LANE_WORKING}
              description={t("setup.github.laneWorkingHint")}
              choices={laneChoices}
              inputProps={form.getInputProps("laneWorking")}
            />
            <LaneField
              label={t("setup.github.laneDone")}
              placeholder={DEFAULT_LANE_DONE}
              choices={laneChoices}
              inputProps={form.getInputProps("laneDone")}
              error={form.errors.laneDone}
            />
          </Stack>
        </Fieldset>
        <AgentFieldPanel target={laneFetchTarget} />
      </Stack>
    </Card>
  );
}

function AgentFieldMemberBadges({
  options,
  color,
  variant,
}: {
  options: AgentFieldState["options"];
  color: string;
  variant: "light" | "outline";
}) {
  return (
    <Group gap="xs">
      {options.map((option) => (
        <Badge key={option.name} color={color} variant={variant}>
          {option.description || option.name}
        </Badge>
      ))}
    </Group>
  );
}

function AgentFieldActionLabel({ state }: { state: AgentFieldState }) {
  const { t } = useTranslation();
  if (!state.exists) {
    return <>{t("setup.github.agentFieldCreate")}</>;
  }
  if (state.missing.length > 0) {
    return <>{t("setup.github.agentFieldAddMembers", { count: state.missing.length })}</>;
  }
  return <>{t("setup.github.agentFieldUpToDate")}</>;
}

function AgentFieldPanel({ target }: { target: ProjectStatusOptionsRequest | null }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const stateQuery = useQuery({
    queryKey: ["agentFieldState", target],
    queryFn: () => getAgentFieldState(target as ProjectStatusOptionsRequest),
    enabled: target !== null,
  });
  const ensureMutation = useMutation({
    mutationFn: () => ensureAgentField(target as ProjectStatusOptionsRequest),
    onSuccess: (data) => {
      queryClient.setQueryData(["agentFieldState", target], data);
    },
  });
  const state = stateQuery.data;

  return (
    <Fieldset legend={t("setup.github.agentField")} radius="md">
      <Stack>
        <Text size="sm" c="dimmed">
          {t("setup.github.agentFieldHint")}
        </Text>
        {target === null ? (
          <Text size="sm" c="dimmed">
            {t("setup.github.agentFieldNeedsProject")}
          </Text>
        ) : stateQuery.isLoading ? (
          <Text size="sm" c="dimmed">
            {t("setup.github.agentFieldLoading")}
          </Text>
        ) : !state?.available ? (
          <InfoCallout title={t("setup.github.agentFieldUnavailableTitle")}>
            {t("setup.github.agentFieldUnavailableHint")}
          </InfoCallout>
        ) : (
          <>
            <Group gap="xs">
              <Text size="sm" fw={500}>
                {t("setup.github.agentFieldStatus")}
              </Text>
              <Badge color={state.exists ? "success" : "neutral"} variant="light">
                {state.exists
                  ? t("setup.github.agentFieldExists")
                  : t("setup.github.agentFieldMissing")}
              </Badge>
            </Group>
            {state.exists && (
              <div>
                <Text size="sm" fw={500} mb={4}>
                  {t("setup.github.agentFieldRegistered")}
                </Text>
                {state.options.length > 0 ? (
                  <AgentFieldMemberBadges options={state.options} color="neutral" variant="light" />
                ) : (
                  <Text size="sm" c="dimmed">
                    {t("setup.github.agentFieldNoMembers")}
                  </Text>
                )}
              </div>
            )}
            {state.missing.length > 0 && (
              <div>
                <Text size="sm" fw={500} mb={4}>
                  {t("setup.github.agentFieldMissingMembers")}
                </Text>
                <AgentFieldMemberBadges options={state.missing} color="warning" variant="outline" />
              </div>
            )}
            <Group>
              <Button
                onClick={() => ensureMutation.mutate()}
                loading={ensureMutation.isPending}
                disabled={state.exists && state.missing.length === 0}
              >
                <AgentFieldActionLabel state={state} />
              </Button>
            </Group>
            {ensureMutation.isError && (
              <Text size="sm" c="danger">
                {t("setup.github.agentFieldError")}
              </Text>
            )}
          </>
        )}
      </Stack>
    </Fieldset>
  );
}

function MemberDiagnosticsPanel({
  personId,
  formMode,
  loading,
  error,
  checks,
  onRun,
}: {
  personId: string | null;
  formMode: "add" | "edit";
  loading: boolean;
  error: Error | null;
  checks: DiagnosticCheck[];
  onRun: () => void;
}) {
  const { t } = useTranslation();
  if (formMode !== "edit" || !personId) {
    return (
      <InfoCallout title={t("setup.members.diagnostics.saveFirstTitle")}>
        {t("setup.members.diagnostics.saveFirstBody")}
      </InfoCallout>
    );
  }

  const issues = checks.filter((check) => check.status !== "ok");
  const errorCount = checks.filter((check) => check.status === "error").length;
  const warningCount = checks.filter((check) => check.status === "warning").length;
  return (
    <Stack>
      <Group justify="space-between">
        <Box>
          <Text fw={700}>{t("setup.members.diagnostics.title")}</Text>
          <Text size="sm" c="dimmed">
            {t("setup.members.diagnostics.description")}
          </Text>
        </Box>
        <Button loading={loading} onClick={onRun}>
          {t("setup.members.diagnostics.run")}
        </Button>
      </Group>
      {error ? (
        <Alert color="danger" title={t("setup.members.diagnostics.failed")}>
          {error.message}
        </Alert>
      ) : null}
      {!loading && checks.length === 0 && !error ? (
        <Text size="sm" c="dimmed">
          {t("setup.members.diagnostics.notRun")}
        </Text>
      ) : null}
      {checks.length > 0 && issues.length === 0 ? (
        <Alert color="success" title={t("setup.members.diagnostics.ok")}>
          {t("setup.members.diagnostics.okDescription", { count: checks.length })}
        </Alert>
      ) : null}
      {issues.length > 0 ? (
        <Alert
          color={errorCount > 0 ? "danger" : "warning"}
          icon={diagnosticIcon(errorCount > 0 ? "error" : "warning")}
          title={t("setup.members.diagnostics.issuesTitle")}
        >
          {t("setup.members.diagnostics.issuesDescription", {
            errors: errorCount,
            warnings: warningCount,
          })}
        </Alert>
      ) : null}
      {checks.length > 0 ? (
        <Stack gap="xs">
          {checks.map((check, index) => (
            <Alert
              color={diagnosticColor(check.status)}
              icon={diagnosticIcon(check.status)}
              className={`diagnostic-alert ${check.status}`}
              key={`${check.section}-${check.code}-${check.target}-${index}`}
              title={diagnosticTitle(t, check)}
            >
              <Text size="xs" c="dimmed" mb={4}>
                {t(`overview.diagnosticSections.${check.section}`)}
                {check.target ? ` / ${check.target}` : ""}
              </Text>
              {diagnosticDescription(t, check) ? (
                <Text size="sm">{diagnosticDescription(t, check)}</Text>
              ) : null}
              {diagnosticDetail(t, check) ? (
                <Text size="xs" c="dimmed" mt={6}>
                  {diagnosticDetail(t, check)}
                </Text>
              ) : null}
            </Alert>
          ))}
        </Stack>
      ) : null}
    </Stack>
  );
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

function PanelHeader({
  title,
  subtitle,
  badge,
  saveState,
}: {
  title: string;
  subtitle: string;
  badge?: string;
  saveState?: "idle" | "saving" | "saved" | "error";
}) {
  return (
    <Group justify="space-between" align="flex-start">
      <Box>
        <Group gap="xs">
          <Title order={3}>{title}</Title>
          {badge ? <Badge>{badge}</Badge> : null}
        </Group>
        <Text size="sm" c="dimmed">
          {subtitle}
        </Text>
      </Box>
      {saveState ? <AutosaveIndicator state={saveState} /> : null}
    </Group>
  );
}

function AutosaveIndicator({ state }: { state: "idle" | "saving" | "saved" | "error" }) {
  const { t } = useTranslation();
  const label = {
    idle: t("setup.autosave.idle"),
    saving: t("setup.autosave.saving"),
    saved: t("setup.autosave.saved"),
    error: t("setup.autosave.error"),
  }[state];
  const color = state === "error" ? "danger" : state === "saved" ? "success" : "neutral";
  return (
    <Badge color={color} variant="light" leftSection={<Save size={12} />}>
      {label}
    </Badge>
  );
}

function FolderPicker({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const { t } = useTranslation();
  const [picking, setPicking] = useState(false);
  const pickDirectory = async () => {
    setPicking(true);
    try {
      if (!isTauriRuntime()) {
        return;
      }
      const { open } = await import("@tauri-apps/plugin-dialog");
      const selected = await open({ directory: true, multiple: false });
      if (typeof selected === "string") {
        onChange(selected);
      }
    } finally {
      setPicking(false);
    }
  };
  return (
    <Group align="flex-end" wrap="nowrap">
      <TextInput
        label={<RequiredLabel text={t("setup.project.workspace")} />}
        aria-label={t("setup.project.workspace")}
        aria-required
        leftSection={<Folder size={16} />}
        value={value}
        onChange={(event) => onChange(event.currentTarget.value)}
        description={t("setup.project.workspaceDescription")}
        flex={1}
      />
      <Button
        leftSection={<FolderOpen size={16} />}
        loading={picking}
        onClick={pickDirectory}
        variant="default"
      >
        {t("setup.project.choose")}
      </Button>
    </Group>
  );
}

function FilePicker({
  label,
  withAsterisk,
  value,
  onChange,
  error,
}: {
  label: string;
  withAsterisk?: boolean;
  value: string;
  onChange: (value: string) => void;
  error?: string;
}) {
  const { t } = useTranslation();
  const [picking, setPicking] = useState(false);
  const pickFile = async () => {
    setPicking(true);
    try {
      if (!isTauriRuntime()) {
        return;
      }
      const { open } = await import("@tauri-apps/plugin-dialog");
      const selected = await open({ directory: false, multiple: false });
      if (typeof selected === "string") {
        onChange(selected);
      }
    } finally {
      setPicking(false);
    }
  };
  return (
    <div className="field-action-row">
      <TextInput
        label={withAsterisk ? <RequiredLabel text={label} /> : label}
        aria-label={label}
        aria-required={withAsterisk}
        leftSection={<FileKey size={16} />}
        value={value}
        onChange={(event) => onChange(event.currentTarget.value)}
        error={error}
        flex={1}
      />
      <Button
        className="field-action-button"
        leftSection={<FolderOpen size={16} />}
        loading={picking}
        onClick={pickFile}
        variant="default"
      >
        {t("setup.project.choose")}
      </Button>
    </div>
  );
}

function LabeledSegmentedControl({
  label,
  description,
  data,
  value,
  onChange,
}: {
  label: string;
  description?: string;
  data: { label: string; value: string }[];
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <Stack gap={6}>
      <Text size="sm" fw={500}>
        {label}
      </Text>
      {description ? (
        <Text size="xs" c="dimmed">
          {description}
        </Text>
      ) : null}
      <SegmentedControl data={data} value={value} onChange={onChange} />
    </Stack>
  );
}

function RequiredLabel({ text }: { text: string }) {
  return (
    <>
      {text}
      <Text span c="danger" inherit aria-hidden="true">
        {" *"}
      </Text>
    </>
  );
}

function DefaultableLabel({
  text,
  tooltip,
  onApply,
  required,
}: {
  text: string;
  tooltip: string;
  onApply: () => void;
  required?: boolean;
}) {
  return (
    <Group justify="space-between" wrap="nowrap" gap="xs">
      <Text size="sm" fw={500}>
        {text}
        {required ? (
          <Text span c="danger" inherit aria-hidden="true">
            {" *"}
          </Text>
        ) : null}
      </Text>
      <Tooltip label={tooltip} withArrow>
        <ActionIcon variant="subtle" size="sm" onClick={onApply} aria-label={tooltip}>
          <WandSparkles size={14} />
        </ActionIcon>
      </Tooltip>
    </Group>
  );
}

function TabErrorIcon({ label }: { label: string }) {
  return (
    <Tooltip label={label} withArrow>
      <ThemeIcon color="warning" variant="light" size="sm" radius="xl">
        <CircleAlert size={12} />
      </ThemeIcon>
    </Tooltip>
  );
}

function InfoCallout({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Alert color="warning" icon={<CircleAlert size={18} />} title={title}>
      {children}
    </Alert>
  );
}

function isTauriRuntime() {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

function sameFormErrors(current: ProjectForm["errors"], next: Record<string, string>): boolean {
  const currentKeys = Object.keys(current);
  const nextKeys = Object.keys(next);
  return (
    currentKeys.length === nextKeys.length && nextKeys.every((key) => current[key] === next[key])
  );
}

function useAutosave(
  form: ProjectForm,
  config: ConfigStatus | undefined,
  schema: ReturnType<typeof createProjectSchema>,
  save: (values: ProjectFormValues) => Promise<unknown>,
  setSaveState: (state: "idle" | "saving" | "saved" | "error") => void,
  enabled: boolean,
) {
  const previous = useRef("");
  const timerRef = useRef<number | null>(null);

  useEffect(() => {
    if (!enabled) {
      return;
    }
    const serialized = JSON.stringify(form.values);
    if (!previous.current) {
      previous.current = serialized;
      return;
    }

    const isValueChanged = previous.current !== serialized;

    if (isValueChanged) {
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      previous.current = serialized;
    }

    if (!form.isDirty()) {
      setSaveState("idle");
      return;
    }
    if (!config?.project_file_exists) {
      setSaveState("idle");
      return;
    }

    const validation = schema.safeParse(form.values);
    if (!validation.success) {
      const errors: Record<string, string> = {};
      for (const [key, messages] of Object.entries(validation.error.flatten().fieldErrors)) {
        if (messages && messages.length > 0) {
          errors[key] = messages[0];
        }
      }
      // Only write when the errors actually change. `form` is a fresh object on
      // every render, so this effect re-runs each render; setting a new errors
      // object unconditionally would re-render → re-run → loop indefinitely.
      if (!sameFormErrors(form.errors, errors)) {
        form.setErrors(errors);
      }
      setSaveState("idle");
      return;
    }

    if (Object.keys(form.errors).length > 0) {
      form.clearErrors();
    }

    if (isValueChanged && timerRef.current === null) {
      timerRef.current = window.setTimeout(async () => {
        setSaveState("saving");
        try {
          await save(validation.data);
          setSaveState("saved");
        } catch {
          setSaveState("error");
        } finally {
          timerRef.current = null;
        }
      }, 700);
    }
  }, [enabled, form, form.values, config, save, schema, setSaveState]);

  useEffect(() => {
    return () => {
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current);
      }
    };
  }, []);
}

function useSetupStatus(
  config: ConfigStatus | undefined,
  activeMemberCount: number,
  values: ProjectFormValues,
): SetupStatus {
  const projectReady = Boolean(config?.project_file_exists);
  const githubReady = isGitHubDecisionComplete(values);
  const intelligenceReady = projectReady;
  const membersReady = activeMemberCount > 0;
  const done = [projectReady, intelligenceReady, githubReady, membersReady].filter(Boolean).length;
  return {
    projectReady,
    intelligenceReady,
    githubReady,
    membersReady,
    done,
    total: 4,
    ready: projectReady && intelligenceReady && githubReady && membersReady,
  };
}

type SetupStatus = {
  projectReady: boolean;
  intelligenceReady: boolean;
  githubReady: boolean;
  membersReady: boolean;
  done: number;
  total: number;
  ready: boolean;
};

type InitialProgress = {
  projectReady: boolean;
  intelligenceReady: boolean;
  githubReady: boolean;
  membersReady: boolean;
  done: number;
  total: number;
  percent: number;
  ready: boolean;
};

function isCoreSectionReady(section: CoreSection, status: SetupStatus | InitialProgress): boolean {
  if (section === "project") {
    return status.projectReady;
  }
  if (section === "intelligence") {
    return status.intelligenceReady;
  }
  if (section === "github") {
    return status.githubReady;
  }
  if (section === "members") {
    return status.membersReady;
  }
  return false;
}

function getInitialCoreStatus(
  values: ProjectFormValues,
  activeMemberCount: number,
  selectedCliAgentDetected: boolean,
): InitialProgress {
  const projectReady =
    values.workspaceDir.trim().length > 0 &&
    values.description.trim().length > 0 &&
    Boolean(values.githubDecision);
  const intelligenceReady =
    Boolean(values.llmApiType) &&
    Boolean(values.cliAgent) &&
    selectedCliAgentDetected &&
    isProviderKeyProvided(values);
  const githubReady = isGitHubDecisionComplete(values);
  const membersReady = activeMemberCount > 0;
  const checks = [projectReady, intelligenceReady, githubReady, membersReady];
  const done = checks.filter(Boolean).length;
  const total = checks.length;
  return {
    projectReady,
    intelligenceReady,
    githubReady,
    membersReady,
    done,
    total,
    percent: Math.round((done / total) * 100),
    ready: done === total,
  };
}

function isGitHubDecisionComplete(values: ProjectFormValues): boolean {
  if (values.githubDecision === "disabled") {
    return true;
  }
  if (values.githubDecision !== "enabled") {
    return false;
  }
  const parsed = parseGitHub(values.githubProjectUrl);
  return parsed.projectValid;
}

function getGitHubFieldErrors(
  values: ProjectFormValues,
  t: TFunction | ((key: string) => string),
): { githubProjectUrl?: string } {
  if (values.githubDecision !== "enabled") {
    return {};
  }
  const parsed = parseGitHub(values.githubProjectUrl);
  const errors: { githubProjectUrl?: string } = {};
  if (!values.githubProjectUrl.trim()) {
    errors.githubProjectUrl = t("setup.validation.githubProjectRequired");
  } else if (!parsed.projectValid) {
    errors.githubProjectUrl = t("setup.validation.githubProjectInvalid");
  }
  return errors;
}

type MemberFieldErrors = Partial<
  Record<
    | "personId"
    | "personName"
    | "roles"
    | "speakingStyle"
    | "characterArchetype"
    | "characterTraits"
    | "characterInterests"
    | "characterJoinWhenText"
    | "characterAvoidWhenText"
    | "characterContributionText"
    | "identity"
    | "githubUsername"
    | "gitEmail"
    | "githubInstallationId"
    | "githubAppId"
    | "githubPrivateKeyPath"
    | "githubAccessToken"
    | "slackUserId"
    | "slackBotToken"
    | "slackAppToken"
    | "slackChannelsText",
    string
  >
>;

export function getMemberFieldErrors(
  values: {
    personType: MemberType;
    githubAccountType: GitHubAccountType;
    identity: string;
    personId: string;
    personName: string;
    githubUsername: string;
    gitEmail: string;
    githubInstallationId: string;
    githubAppId: string;
    githubPrivateKeyPath: string;
    githubAccessToken: string;
    slackUserId: string;
    slackBotToken: string;
    slackAppToken: string;
    slackChannelsText: string;
    storedMemberSecrets: {
      githubInstallationId: boolean;
      githubAppId: boolean;
      githubPrivateKeyPath: boolean;
      githubAccessToken: boolean;
      slackBotToken: boolean;
      slackAppToken: boolean;
    };
    roles: string[];
    speakingStyle: string;
    characterArchetype: string;
    characterTraits: string[];
    characterInterests: string[];
    characterJoinWhenText: string;
    characterAvoidWhenText: string;
    characterContributionText: string;
    existingPersonIds: string[];
    originalPersonId: string | null;
  },
  t: TFunction | ((key: string) => string),
): MemberFieldErrors {
  const errors: MemberFieldErrors = {};
  const personId = values.personId.trim();
  const originalPersonId = values.originalPersonId?.trim() ?? "";
  const duplicatedPersonId = values.existingPersonIds.some(
    (existingPersonId) =>
      existingPersonId.trim() === personId && existingPersonId.trim() !== originalPersonId,
  );
  if (!personId) {
    errors.personId = t("setup.validation.memberIdRequired");
  } else if (!/^[a-z0-9_-]+$/.test(personId)) {
    errors.personId = t("setup.validation.memberIdInvalid");
  } else if (duplicatedPersonId) {
    errors.personId = t("setup.validation.memberIdDuplicate");
  }
  if (!values.personName.trim()) {
    errors.personName = t("setup.validation.memberNameRequired");
  }
  if (values.roles.length === 0) {
    errors.roles = t("setup.validation.memberRolesRequired");
  }
  if (values.personType !== "human") {
    if (!values.speakingStyle.trim()) {
      errors.speakingStyle = t("setup.validation.memberSpeakingStyleRequired");
    }
    if (!values.characterArchetype.trim()) {
      errors.characterArchetype = t("setup.validation.memberCharacterArchetypeRequired");
    }
    if (values.characterTraits.length === 0) {
      errors.characterTraits = t("setup.validation.memberCharacterTraitsRequired");
    }
    if (values.characterInterests.length === 0) {
      errors.characterInterests = t("setup.validation.memberCharacterInterestsRequired");
    }
    if (!values.characterJoinWhenText.trim()) {
      errors.characterJoinWhenText = t("setup.validation.memberCharacterJoinWhenRequired");
    }
    if (!values.characterAvoidWhenText.trim()) {
      errors.characterAvoidWhenText = t("setup.validation.memberCharacterAvoidWhenRequired");
    }
    if (!values.characterContributionText.trim()) {
      errors.characterContributionText = t("setup.validation.memberCharacterContributionRequired");
    }
  }

  const usesGitHubMember = values.githubAccountType !== "none";
  if (values.githubAccountType === "github_apps") {
    const missingGitHubReference = !values.githubUsername.trim() || !values.gitEmail.trim();
    if (missingGitHubReference && !values.identity.trim()) {
      errors.identity = t("setup.validation.memberGithubAppsUrlRequired");
    } else if (values.identity.trim() && !isGitHubAppsUrl(values.identity)) {
      errors.identity = t("setup.validation.memberGithubAppsUrlInvalid");
    }
    if (!values.githubUsername.trim()) {
      errors.githubUsername = t("setup.validation.memberGithubUsernameRequired");
    }
  } else if (usesGitHubMember) {
    if (!values.githubUsername.trim()) {
      errors.githubUsername = t("setup.validation.memberGithubUsernameRequired");
    } else if (!isGitHubUsername(values.githubUsername)) {
      errors.githubUsername = t("setup.validation.memberGithubUsernameInvalid");
    }
  }
  if (usesGitHubMember && !values.gitEmail.trim()) {
    errors.gitEmail = t("setup.validation.memberGitEmailRequired");
  } else if (values.gitEmail.trim() && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(values.gitEmail.trim())) {
    errors.gitEmail = t("setup.validation.memberGitEmailInvalid");
  }

  if (values.githubAccountType === "github_apps") {
    if (!values.githubInstallationId.trim()) {
      errors.githubInstallationId = t("setup.validation.githubInstallationIdRequired");
    } else if (
      values.githubInstallationId.trim() &&
      !/^\d+$/.test(values.githubInstallationId.trim())
    ) {
      errors.githubInstallationId = t("setup.validation.githubInstallationIdInvalid");
    }
    if (!values.githubAppId.trim()) {
      errors.githubAppId = t("setup.validation.githubAppIdRequired");
    } else if (values.githubAppId.trim() && !/^\d+$/.test(values.githubAppId.trim())) {
      errors.githubAppId = t("setup.validation.githubAppIdInvalid");
    }
    if (!values.githubPrivateKeyPath.trim() && !values.storedMemberSecrets.githubPrivateKeyPath) {
      errors.githubPrivateKeyPath = t("setup.validation.githubPrivateKeyPathRequired");
    }
  }

  if (
    (values.githubAccountType === "machine_user" || values.githubAccountType === "proxy_agent") &&
    !values.githubAccessToken.trim() &&
    !values.storedMemberSecrets.githubAccessToken
  ) {
    errors.githubAccessToken = t("setup.validation.githubAccessTokenRequired");
  } else if (values.githubAccessToken.trim() && !isGitHubAccessToken(values.githubAccessToken)) {
    errors.githubAccessToken = t("setup.validation.githubAccessTokenInvalid");
  }

  if (values.personType === "human") {
    if (!values.slackUserId.trim()) {
      errors.slackUserId = t("setup.validation.slackUserIdRequired");
    } else if (!isSlackUserId(values.slackUserId)) {
      errors.slackUserId = t("setup.validation.slackUserIdInvalid");
    }
    return errors;
  }

  if (values.slackBotToken.trim() && !isSlackBotToken(values.slackBotToken)) {
    errors.slackBotToken = t("setup.validation.slackBotTokenInvalid");
  }
  if (values.slackAppToken.trim() && !isSlackAppToken(values.slackAppToken)) {
    errors.slackAppToken = t("setup.validation.slackAppTokenInvalid");
  }

  const slackChannels = parseSlackChannels(values.slackChannelsText);
  if (
    slackChannels.length > 0 &&
    !values.slackBotToken.trim() &&
    !values.storedMemberSecrets.slackBotToken
  ) {
    errors.slackBotToken = t("setup.validation.slackBotTokenRequired");
  }
  if (
    slackChannels.length > 0 &&
    !values.slackAppToken.trim() &&
    !values.storedMemberSecrets.slackAppToken
  ) {
    errors.slackAppToken = t("setup.validation.slackAppTokenRequired");
  }

  const invalidSlackChannels = slackChannels.filter((channel) => !isSlackChannelReference(channel));
  if (invalidSlackChannels.length > 0) {
    errors.slackChannelsText = t("setup.validation.slackChannelsInvalid");
  }

  return errors;
}

export function getMemberResolveErrorMessage(
  error: unknown,
  t: TFunction | ((key: string) => string),
): string {
  if (error instanceof ApiRequestError) {
    if (error.code === "invalid_github_username" || error.code === "invalid_github_apps_url") {
      return t("setup.validation.memberGithubIdentityNotFound");
    }
  }
  return t("setup.validation.memberGithubIdentityResolveFailed");
}

function isGitHubAppsUrl(value: string): boolean {
  const parts = value.trim().split("/");
  return Boolean(
    value.trim().startsWith("https://github.com/") &&
    parts[3] === "organizations" &&
    parts[4] &&
    parts[5] === "settings" &&
    parts[6] === "apps" &&
    parts[7],
  );
}

function getGitHubResolveInput(
  githubAccountType: GitHubAccountType,
  identity: string,
  githubUsername: string,
): string {
  return githubAccountType === "github_apps" ? identity : githubUsername;
}

function toGitHubAccountType(value: string): GitHubAccountType {
  return GITHUB_ACCOUNT_TYPE_OPTIONS.includes(value as GitHubAccountType)
    ? (value as GitHubAccountType)
    : "none";
}

function isGitHubUsername(value: string): boolean {
  const username = value.trim();
  return (
    /^[A-Za-z0-9-]{1,39}$/.test(username) && !username.startsWith("-") && !username.endsWith("-")
  );
}

function isSlackChannelReference(value: string): boolean {
  const channel = value.trim();
  return (
    /^[CGD][A-Z0-9]{8,}$/.test(channel) ||
    /^#?[\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Han}a-z0-9][\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Han}a-z0-9_-]{0,79}$/u.test(
      channel,
    )
  );
}

function parseSlackChannels(value: string): string[] {
  return value
    .split(",")
    .map((channel) => channel.trim())
    .filter(Boolean);
}

function normalizeSlackChannelReference(value: string): string {
  return value.trim().replace(/^#/, "");
}

function toChatParticipationPolicy(value: string | null): ChatParticipationPolicy {
  if (value === "social" || value === "muted") {
    return value;
  }
  return "strict";
}

function isSlackUserId(value: string): boolean {
  return /^U[A-Z0-9]{8,}$/.test(value.trim());
}

function isSlackBotToken(value: string): boolean {
  return /^xoxb-[A-Za-z0-9-]{8,}$/.test(value.trim());
}

function isSlackAppToken(value: string): boolean {
  return /^xapp-[A-Za-z0-9-]{8,}$/.test(value.trim());
}

function isGitHubAccessToken(value: string): boolean {
  return /^(?:gh[pousr]_[A-Za-z0-9_]{8,}|github_pat_[A-Za-z0-9_]{8,})$/.test(value.trim());
}

function isProviderKeyProvided(values: ProjectFormValues): boolean {
  return (values.providerApiKeys[values.llmApiType] ?? "").trim().length > 0;
}

function getInitialEnvFileOption(
  config: ConfigStatus | undefined,
): ProjectFormValues["envFileOption"] {
  return config?.env_file_exists ? "append" : "overwrite";
}

function toInitialProjectSetupRequest(
  values: ProjectFormValues,
  config: ConfigStatus | undefined,
): ProjectSetupRequest {
  return toProjectSetupRequest(values, config, {
    envFileOption: getInitialEnvFileOption(config),
  });
}

export function initialProjectValues(
  config: ConfigStatus | undefined,
  appLanguage: ProjectFormValues["language"],
  projectLanguage: ProjectFormValues["language"] | null,
  projectConfig: ProjectConfig | undefined,
): ProjectFormValues {
  if (projectConfig) {
    return {
      workspaceDir: config?.cwd ?? localStorage.getItem("guildbotics.workspace") ?? "",
      envFileOption: getInitialEnvFileOption(config),
      language: projectConfig.language,
      description: projectConfig.description ?? "",
      llmApiType: projectConfig.llm_api_type,
      cliAgent: projectConfig.cli_agent,
      providerApiKeys: {},
      githubDecision: projectConfig.github_enabled ? "enabled" : "disabled",
      githubEnabled: projectConfig.github_enabled,
      githubProjectUrl: projectConfig.github_project_url ?? "",
      laneReady: projectConfig.lane_map?.ready ?? DEFAULT_LANE_READY,
      laneWorking: projectConfig.lane_map?.working ?? DEFAULT_LANE_WORKING,
      laneDone: projectConfig.lane_map?.done ?? DEFAULT_LANE_DONE,
    };
  }
  const cwd = config?.cwd ?? localStorage.getItem("guildbotics.workspace") ?? "";
  return {
    workspaceDir: cwd,
    envFileOption: getInitialEnvFileOption(config),
    language: config?.project_file_exists ? (projectLanguage ?? appLanguage) : appLanguage,
    description: "",
    llmApiType: "openai",
    cliAgent: "codex",
    providerApiKeys: {},
    githubDecision: "",
    githubEnabled: false,
    githubProjectUrl: "",
    laneReady: DEFAULT_LANE_READY,
    laneWorking: DEFAULT_LANE_WORKING,
    laneDone: DEFAULT_LANE_DONE,
  };
}

export function toProjectSetupRequest(
  values: ProjectFormValues,
  _config: ConfigStatus | undefined,
  options: { envFileOption?: ProjectFormValues["envFileOption"] } = {},
): ProjectSetupRequest {
  const github = values.githubDecision === "enabled" ? parseGitHub(values.githubProjectUrl) : null;
  return {
    config_dir: resolveConfigDir(values.workspaceDir),
    env_file_path: joinPath(values.workspaceDir, ".env"),
    env_file_option: options.envFileOption ?? values.envFileOption,
    language: values.language,
    description: values.description,
    owner: github?.owner ?? "",
    project_id: github?.projectId ?? "",
    github_project_url: github?.projectUrl ?? "",
    lane_map: github ? toLaneMap(values) : undefined,
    llm_api_type: values.llmApiType,
    cli_agent: values.cliAgent,
    provider_api_keys: values.providerApiKeys,
  };
}

function toLaneMap(values: ProjectFormValues): LaneMap {
  return {
    ready: values.laneReady.trim() || DEFAULT_LANE_READY,
    working: values.laneWorking.trim() || DEFAULT_LANE_WORKING,
    done: values.laneDone.trim() || DEFAULT_LANE_DONE,
  };
}

export function toProjectUpdateRequest(
  values: ProjectFormValues,
  config: ConfigStatus | undefined,
  snapshot: ProjectConfig,
): ProjectConfigUpdateRequest {
  const github = values.githubDecision === "enabled" ? parseGitHub(values.githubProjectUrl) : null;
  return {
    config_dir: snapshot.config_dir || config?.config_dir || resolveConfigDir(values.workspaceDir),
    env_file_path: snapshot.env_file_path || joinPath(values.workspaceDir, ".env"),
    language: values.language,
    description: values.description,
    llm_api_type: values.llmApiType,
    cli_agent: values.cliAgent,
    github_enabled: values.githubDecision === "enabled",
    owner: github?.owner ?? "",
    project_id: github?.projectId ?? "",
    github_project_url: github?.projectUrl ?? "",
    lane_map: github ? toLaneMap(values) : undefined,
    // Only send providers the user actually typed a key for; empty = leave the
    // existing .env value unchanged.
    provider_api_keys: Object.fromEntries(
      Object.entries(values.providerApiKeys).filter(([, value]) => value.trim().length > 0),
    ),
  };
}

function getSpeakingStyleTemplates(t: TFunction): Record<SpeakingStylePreset, string> {
  return {
    friendly: t("setup.members.speakingStyleDescriptions.friendly"),
    professional: t("setup.members.speakingStyleDescriptions.professional"),
    energetic: t("setup.members.speakingStyleDescriptions.energetic"),
  };
}

function inferSpeakingStylePreset(
  speakingStyle: string,
  templates: Record<SpeakingStylePreset, string>,
): SpeakingStylePreset | "" {
  const normalized = speakingStyle.trim();
  if (!normalized) {
    return "";
  }
  for (const preset of SPEAKING_STYLE_OPTIONS) {
    if (templates[preset] === normalized) {
      return preset;
    }
  }
  return "";
}

type CharacterPresetExample = {
  archetype: string;
  traits: string[];
  interests: string[];
  joinWhen: string[];
  avoidWhen: string[];
  contributionStyle: string[];
  relationships: string;
};

function getCharacterPresetExamples(
  language: "en" | "ja",
): Record<SpeakingStylePreset, CharacterPresetExample> {
  if (language === "ja") {
    return {
      friendly: {
        archetype: "親しみやすいアイデアメーカー",
        traits: ["明るい", "共感的", "社交的"],
        interests: ["UX", "コンテンツ企画", "コミュニティ"],
        joinWhen: [
          "会話が固くなっていて、ユーザー目線を足したいとき",
          "アイデア出しや発散が必要なとき",
        ],
        avoidWhen: [
          "厳密な技術詳細だけで、自分の観点を足しにくいとき",
          "すでに結論が固まっていて、脱線しそうなとき",
        ],
        contributionStyle: ["ユーザーの気持ちや体験を言語化する", "具体案をやわらかく提案する"],
        relationships:
          "例:\n他メンバーAに対して: 分析力を尊敬しつつ、必要ならユーザー視点を補う。\n他メンバーBに対して: 発想を歓迎し、実現に向けた具体化を一緒に進める。",
      },
      professional: {
        archetype: "戦略的プロジェクトマネージャー",
        traits: ["戦略的", "整理上手", "責任感が強い"],
        interests: ["アーキテクチャ", "計画立案", "品質管理"],
        joinWhen: [
          "設計方針、品質、リスク、優先順位が論点のとき",
          "会話が発散していて整理が必要なとき",
        ],
        avoidWhen: [
          "雑談だけでプロジェクト判断に繋がらないとき",
          "既に十分整理されていて過剰介入になるとき",
        ],
        contributionStyle: [
          "論点・制約・次アクションを明確にする",
          "抜け漏れやリスクを短く指摘する",
        ],
        relationships:
          "例:\n他メンバーAに対して: 意思決定を尊重し、議論を計画と実行可能性に接続する。\n他メンバーBに対して: 創造性を活かしつつ、実現可能な形に落とし込む支援を行う。\nチームに対して: 品質・設計整合性・リスク管理の観点を担う。",
      },
      energetic: {
        archetype: "爆速プロトタイパー / 行動重視の推進役",
        traits: ["行動的", "情熱的", "スピード重視"],
        interests: ["プロトタイピング", "新規機能開発", "実験的実装"],
        joinWhen: [
          "議論を実際のコードや動くプロトタイプ（PoC）で具体化したいとき",
          "新しい技術の導入や、実験的な試みを検討しているとき",
          "プロジェクトの進行スピードを上げ、開発の勢いを生み出したいとき",
        ],
        avoidWhen: [
          "長期的な保守性やライセンス・規約など、慎重な検討が最優先されるとき",
          "仮説検証よりも、極めて高いセキュリティや安定性が求められるデリケートな作業のとき",
        ],
        contributionStyle: [
          "「まずはやってみる」ための最初のアクションを具体的に提示する",
          "最小限の動くプロトタイプ（PoC）をすばやく作成して議論を前に進める",
        ],
        relationships:
          "例:\n他メンバーAに対して: 長期的な設計や品質管理をリスペクトしつつ、検証を加速するためのPoC提案で支援する。\n他メンバーBに対して: アイデアに共鳴し、それをすぐに動くプロトタイプへ落とし込む実装アプローチを一緒に考える。",
      },
    };
  }
  return {
    friendly: {
      archetype: "friendly_idea_contributor",
      traits: ["friendly", "empathetic", "social"],
      interests: ["ux", "content", "community"],
      joinWhen: ["When the discussion needs user perspective", "When brainstorming is needed"],
      avoidWhen: [
        "When only deep technical details are discussed",
        "When adding comments would derail a settled decision",
      ],
      contributionStyle: [
        "Translate user feelings into actionable insights",
        "Suggest concrete ideas in a lightweight tone",
      ],
      relationships:
        "Example:\nWith member A: respect analysis and add user perspective when needed.\nWith member B: amplify ideas and help make them executable.",
    },
    professional: {
      archetype: "strategic_project_manager",
      traits: ["strategic", "organized", "responsible"],
      interests: ["architecture", "planning", "quality"],
      joinWhen: [
        "When architecture, risk, quality, or priorities are discussed",
        "When the conversation needs structure",
      ],
      avoidWhen: [
        "When discussion is pure small talk",
        "When the plan is already clear and intervention adds noise",
      ],
      contributionStyle: [
        "Clarify constraints and next actions",
        "Highlight risks and gaps concisely",
      ],
      relationships:
        "Example:\nWith member A: connect leadership decisions to executable plans.\nWith member B: keep creativity while shaping feasible implementation.",
    },
    energetic: {
      archetype: "rapid_prototyper_and_accelerator",
      traits: ["active", "passionate", "speed-oriented"],
      interests: ["prototyping", "feature development", "experimental implementation"],
      joinWhen: [
        "When the discussion needs to transition to concrete code or a working prototype (PoC)",
        "When exploring new technologies or experimental approaches",
        "When the team needs to accelerate development speed and build momentum",
      ],
      avoidWhen: [
        "When long-term maintenance, licensing, or legal compliance is the primary concern",
        "When high security and absolute stability are required, leaving no room for rapid experimentation",
      ],
      contributionStyle: [
        "Propose immediate actions to get things started without hesitation",
        "Build and share minimal working prototypes quickly to make ideas tangible",
      ],
      relationships:
        "Example:\nWith member A: respect long-term planning and support by providing quick proof-of-concepts.\nWith member B: align with creative ideas and collaborate to build a working prototype instantly.",
    },
  };
}

export function parseCharacterFields(character: Record<string, unknown>): {
  archetype: string;
  traits: string[];
  interests: string[];
  joinWhen: string[];
  avoidWhen: string[];
  contributionStyle: string[];
  extras: Record<string, unknown>;
} {
  const source = isRecord(character) ? character : {};
  const conversation = isRecord(source.conversation_preferences)
    ? source.conversation_preferences
    : {};
  const conversationExtras: Record<string, unknown> = { ...conversation };
  delete conversationExtras.join_when;
  delete conversationExtras.avoid_when;
  delete conversationExtras.contribution_style;
  const extras: Record<string, unknown> = { ...source };
  delete extras.archetype;
  delete extras.traits;
  delete extras.interests;
  delete extras.conversation_preferences;
  if (Object.keys(conversationExtras).length > 0) {
    extras.conversation_preferences = conversationExtras;
  }

  return {
    archetype: stringOrEmpty(source.archetype),
    traits: toStringList(source.traits),
    interests: toStringList(source.interests),
    joinWhen: toStringList(conversation.join_when),
    avoidWhen: toStringList(conversation.avoid_when),
    contributionStyle: toStringList(conversation.contribution_style),
    extras,
  };
}

export function buildCharacterPayload({
  archetype,
  traits,
  interests,
  joinWhen,
  avoidWhen,
  contributionStyle,
  extras,
}: {
  archetype: string;
  traits: string[];
  interests: string[];
  joinWhen: string[];
  avoidWhen: string[];
  contributionStyle: string[];
  extras: Record<string, unknown>;
}): Record<string, unknown> {
  const payload: Record<string, unknown> = { ...extras };
  if (archetype.trim()) {
    payload.archetype = archetype.trim();
  }
  if (traits.length > 0) {
    payload.traits = traits;
  }
  if (interests.length > 0) {
    payload.interests = interests;
  }
  const existingConversation = isRecord(payload.conversation_preferences)
    ? payload.conversation_preferences
    : {};
  const conversation: Record<string, unknown> = { ...existingConversation };
  if (joinWhen.length > 0) {
    conversation.join_when = joinWhen;
  } else {
    delete conversation.join_when;
  }
  if (avoidWhen.length > 0) {
    conversation.avoid_when = avoidWhen;
  } else {
    delete conversation.avoid_when;
  }
  if (contributionStyle.length > 0) {
    conversation.contribution_style = contributionStyle;
  } else {
    delete conversation.contribution_style;
  }
  if (Object.keys(conversation).length > 0) {
    payload.conversation_preferences = conversation;
  } else {
    delete payload.conversation_preferences;
  }
  return payload;
}

function splitLines(value: string): string[] {
  return value
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

function toStringList(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.map((item) => String(item).trim()).filter((item) => item.length > 0);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function toIntelligenceUpdatePayload(config: IntelligenceConfig, savePersonId?: string) {
  const personId = savePersonId || config.person_id;
  if (config.person_id && config.inherited) {
    return {
      config_dir: config.config_dir,
      person_id: personId,
      inherit_team_defaults: true,
    };
  }
  if (config.person_id) {
    return {
      config_dir: config.config_dir,
      person_id: personId,
      inherit_team_defaults: false,
      model_mapping: config.model_mapping,
      cli_agent_mapping: config.cli_agent_mapping,
      native_agent_policy: config.native_agent_policy,
    };
  }
  return {
    config_dir: config.config_dir,
    person_id: personId,
    inherit_team_defaults: false,
    model_mapping: config.model_mapping,
    models: config.models,
    cli_agent_mapping: config.cli_agent_mapping,
    cli_agents: config.cli_agents,
    brain_mapping: config.brain_mapping,
    native_agent_policy: config.native_agent_policy,
  };
}

function stringOrEmpty(value: unknown): string {
  return typeof value === "string" ? value : "";
}

export function parseGitHub(projectUrl: string) {
  const normalizedProjectUrl = projectUrl.trim();
  const projectParts = normalizedProjectUrl.split("/");
  const projectType = projectParts[3] ?? "";
  const owner = projectParts[4] ?? "";
  const projectId = projectParts[6]?.split("?")[0] ?? "";
  const projectValid = Boolean(
    normalizedProjectUrl.startsWith("https://github.com/") &&
    ["orgs", "users"].includes(projectType) &&
    owner &&
    projectParts[5] === "projects" &&
    projectId,
  );
  return {
    owner: projectValid ? owner : "",
    projectId: projectValid ? projectId : "",
    projectUrl: projectValid
      ? `https://github.com/${projectType}/${owner}/projects/${projectId}`
      : normalizedProjectUrl,
    projectValid,
  };
}

function joinPath(base: string, suffix: string) {
  return `${base.replace(/\/$/, "")}/${suffix}`;
}

function memberRequestToConfig(
  request: MemberSetupRequest | MemberConfigUpdateRequest,
): MemberConfig {
  return {
    person_id: request.person_id,
    person_name: request.person_name,
    person_type: request.person_type,
    github_account_type: request.github_account_type,
    is_active: request.is_active,
    github_username: request.github_username,
    git_email: request.git_email,
    roles: request.roles ?? [],
    speaking_style: request.speaking_style ?? "",
    relationships: request.relationships ?? "",
    character: request.character ?? {},
    github_installation_id: request.github_installation_id ?? null,
    github_app_id: request.github_app_id ?? null,
    github_private_key_path: request.github_private_key_path ?? "",
    has_github_installation_id: Boolean(request.github_installation_id),
    has_github_app_id: Boolean(request.github_app_id),
    has_github_private_key_path: Boolean(request.github_private_key_path),
    has_github_private_key: Boolean(request.github_private_key_path),
    has_github_access_token: Boolean(request.github_access_token),
    slack_user_id: request.slack_user_id ?? "",
    has_slack_bot_token: Boolean(request.slack_bot_token),
    has_slack_app_token: Boolean(request.slack_app_token),
    slack_channels: request.slack_channels ?? [],
    slack_channel_participation: request.slack_channel_participation ?? {},
    routine_commands: request.routine_commands ?? [],
    task_schedules: request.task_schedules ?? [],
  };
}

function flattenTaskSchedules(taskSchedules: MemberTaskSchedule[]) {
  return taskSchedules.flatMap((schedule) =>
    schedule.schedules.map((cron) => ({
      command: schedule.command,
      schedule: cron,
    })),
  );
}

function scheduledCommandToDraft(
  entry: { command: string; schedule: string },
  commandCatalog: CommandOption[],
): ScheduledCommandDraft {
  const parsedCommand = parseCommandExpression(entry.command, commandCatalog);
  const parsedCron = parseCron(entry.schedule);
  return {
    id: newDraftId(),
    commandMode: parsedCommand.option ? "catalog" : "custom",
    command: parsedCommand.option?.command ?? commandCatalog[0]?.command ?? "",
    customCommand: parsedCommand.option ? "" : parsedCommand.command,
    argValues: {},
    rawArgs: parsedCommand.args,
    scheduleMode: parsedCron.mode,
    minute: parsedCron.minute,
    hour: parsedCron.hour,
    weekday: parsedCron.weekday,
    cron: entry.schedule,
  };
}

export function createScheduledCommandDraft(command = ""): ScheduledCommandDraft {
  return {
    id: newDraftId(),
    commandMode: command ? "catalog" : "custom",
    command,
    customCommand: "",
    argValues: {},
    rawArgs: "",
    scheduleMode: "daily",
    minute: 0,
    hour: 9,
    weekday: "1",
    cron: "0 9 * * *",
  };
}

export function buildTaskSchedules(
  drafts: ScheduledCommandDraft[],
  commandOptionByValue: Map<string, CommandOption>,
): MemberTaskSchedule[] {
  const grouped = new Map<string, string[]>();
  for (const draft of drafts) {
    const command = buildScheduledCommandExpression(draft, commandOptionByValue);
    const schedule = draftToCron(draft);
    if (!command || !isValidCron(schedule)) {
      continue;
    }
    grouped.set(command, [...(grouped.get(command) ?? []), schedule]);
  }
  return Array.from(grouped.entries()).map(([command, schedules]) => ({
    command,
    schedules,
  }));
}

export function buildScheduledCommandExpression(
  draft: ScheduledCommandDraft,
  commandOptionByValue: Map<string, CommandOption>,
): string {
  const option =
    draft.commandMode === "catalog" ? (commandOptionByValue.get(draft.command) ?? null) : null;
  const command = draft.commandMode === "catalog" ? draft.command : draft.customCommand.trim();
  if (!command) {
    return "";
  }
  const args = buildSetupCommandArgs(option, draft.argValues, draft.rawArgs);
  return [command, ...args.map(quoteCommandArg)].join(" ");
}

function buildSetupCommandArgs(
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
      args.push(argument.kind === "positional" ? value : `${argument.name}=${value}`);
    }
  }
  return [...args, ...splitCommandLine(rawArgs)];
}

export function parseCommandExpression(expression: string, commandCatalog: CommandOption[]) {
  const command = expression.trim();
  const option = [...commandCatalog]
    .sort((a, b) => b.command.length - a.command.length)
    .find(
      (candidate) => command === candidate.command || command.startsWith(`${candidate.command} `),
    );
  if (option) {
    return {
      option,
      command: option.command,
      args: command.slice(option.command.length).trim(),
    };
  }
  const [first = "", ...rest] = splitCommandLine(command);
  return { option: null, command: first, args: rest.join(" ") };
}

export function draftToCron(draft: ScheduledCommandDraft): string {
  const minute = clampInteger(draft.minute, 0, 59);
  const hour = clampInteger(draft.hour, 0, 23);
  if (draft.scheduleMode === "hourly") {
    return `${minute} * * * *`;
  }
  if (draft.scheduleMode === "daily") {
    return `${minute} ${hour} * * *`;
  }
  if (draft.scheduleMode === "weekly") {
    return `${minute} ${hour} * * ${draft.weekday || "1"}`;
  }
  return draft.cron.trim();
}

export function parseCron(schedule: string): {
  mode: CronPreset;
  minute: number;
  hour: number;
  weekday: string;
} {
  const parts = schedule.trim().split(/\s+/);
  if (parts.length !== 5) {
    return { mode: "custom", minute: 0, hour: 9, weekday: "1" };
  }
  const [minute, hour, dayOfMonth, month, dayOfWeek] = parts;
  if (hour === "*" && dayOfMonth === "*" && month === "*" && dayOfWeek === "*") {
    return { mode: "hourly", minute: toCronNumber(minute, 0), hour: 9, weekday: "1" };
  }
  if (dayOfMonth === "*" && month === "*" && dayOfWeek === "*") {
    return {
      mode: "daily",
      minute: toCronNumber(minute, 0),
      hour: toCronNumber(hour, 9),
      weekday: "1",
    };
  }
  if (
    dayOfMonth === "*" &&
    month === "*" &&
    (WEEKDAY_OPTIONS as readonly string[]).includes(dayOfWeek)
  ) {
    return {
      mode: "weekly",
      minute: toCronNumber(minute, 0),
      hour: toCronNumber(hour, 9),
      weekday: dayOfWeek,
    };
  }
  return { mode: "custom", minute: toCronNumber(minute, 0), hour: 9, weekday: "1" };
}

export function isValidCron(schedule: string): boolean {
  return schedule.trim().split(/\s+/).length === 5;
}

function toCronNumber(value: string, fallback: number): number {
  return /^\d+$/.test(value) ? Number(value) : fallback;
}

function clampInteger(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, Math.trunc(value)));
}

export function splitCommandLine(value: string): string[] {
  const args: string[] = [];
  const pattern = /"([^"]*)"|'([^']*)'|(\S+)/g;
  for (const match of value.matchAll(pattern)) {
    args.push(match[1] ?? match[2] ?? match[3] ?? "");
  }
  return args.filter(Boolean);
}

export function quoteCommandArg(value: string): string {
  if (!/\s/.test(value)) {
    return value;
  }
  return `"${value.replace(/(["\\])/g, "\\$1")}"`;
}

function newDraftId(): string {
  return crypto.randomUUID?.() ?? `schedule-${Date.now()}-${Math.random()}`;
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

function resolveConfigDir(workspaceDir: string): string {
  return joinPath(workspaceDir, ".guildbotics/config");
}
