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
  Paper,
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
import { schemaResolver } from "@mantine/form";
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
import { useSearchParams } from "react-router";

import {
  type CommandOption,
  type DiagnosticCheck,
  type CliAgentDetection,
  type CliAgentDefinition,
  type EffortFieldSpec,
  type EffortOverlay,
  type ConfigRevisions,
  type ConfigStatus,
  type BrainAssignment,
  type IntelligenceConfig,
  NATIVE_POLICY_ADAPTERS,
  type NativeAgentFilesystemAccess,
  type NativeAgentPolicyAdapter,
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
  updateDefaultPerson,
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
import { announceWorkspaceChange } from "../appEvents";
import { cliAgentLabelFromConfig, useMemberCliAgentLabel } from "../cliAgent";
import { GitHubAppRegistrationPanel } from "./GitHubAppRegistration";
import { SlackAppRegistrationPanel } from "./SlackAppRegistration";
import { SlackTokenVerificationPanel } from "./SlackTokenVerification";
import { ShortcutsSection } from "./ShortcutsSection";
import { CloneFromHubButton } from "../sync/CloneFromHub";
import { DeviceSettings } from "../sync/DeviceSettings";
import { SecretStatusHint } from "../sync/SecretStatusHint";
import { SyncSettings } from "../sync/SyncSettings";
import { isBusyConfigSave, isStaleConfigSave } from "./configRevisions";
import { EffortSettingsField, ToolSettingsField } from "./EffortSettingsField";
import { normalizeLanguage } from "../i18n";

export function createProjectSchema(t: TFunction | ((key: string) => string)) {
  return z
    .object({
      workspaceDir: z.string().min(1, t("setup.validation.workspaceRequired")),
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
const CORE_SETUP_SECTIONS_INITIAL = [
  "project",
  "intelligence",
  "members",
  "github",
  "verification",
  // Hosting a hub and this device's key belong to the machine rather than to a
  // workspace, so the section has to be reachable before there is one: a
  // machine that only hosts the hub never gets a project of its own.
  "device",
] as const;
const CORE_SETUP_SECTIONS_CONFIGURED = [
  "project",
  "intelligence",
  "members",
  "github",
  // Hotkeys are an everyday convenience rather than part of getting running,
  // and sharing needs a workspace to share, so both are offered only once the
  // workspace is configured.
  "shortcuts",
  "sync",
  "verification",
  "device",
] as const;
type CoreSection = (typeof CORE_SETUP_SECTIONS_CONFIGURED)[number];
// Sections that configure this machine rather than the selected workspace.
// The nav draws them under their own heading so the boundary is visible.
const MACHINE_SECTIONS: ReadonlySet<CoreSection> = new Set(["device"]);
const CORE_SECTION_LABEL_KEYS = {
  project: "setup.nav.project",
  intelligence: "setup.nav.intelligence",
  members: "setup.nav.members",
  github: "setup.nav.github",
  shortcuts: "setup.nav.shortcuts",
  sync: "setup.nav.sync",
  verification: "setup.nav.verification",
  device: "setup.nav.device",
} as const satisfies Record<CoreSection, string>;
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
  extraArgs: string;
  scheduleMode: CronPreset;
  minute: number;
  hour: number;
  weekday: string;
  cron: string;
};
const WEEKDAY_OPTIONS = ["0", "1", "2", "3", "4", "5", "6"] as const;

interface SlotNameInputProps {
  label: string;
  value: string;
  readOnly?: boolean;
  onRename: (oldKey: string, newKey: string) => void;
  flex?: number;
  size?: string;
  fw?: number | string;
}

function SlotNameInput({ label, value, readOnly, onRename, flex, size, fw }: SlotNameInputProps) {
  const [localValue, setLocalValue] = useState(value);

  const commitRename = () => {
    const trimmed = localValue.trim();
    if (trimmed && trimmed !== value) {
      onRename(value, trimmed);
    } else {
      setLocalValue(value);
    }
  };

  return (
    <TextInput
      label={label}
      value={localValue}
      // Locked names (built-in default/agent or team-owned inherited slots) are
      // disabled, not just read-only, so they read as clearly non-editable.
      disabled={readOnly}
      onChange={(e) => setLocalValue(e.currentTarget.value)}
      onBlur={commitRename}
      onKeyDown={(e) => {
        if (e.key === "Enter") {
          commitRename();
        }
      }}
      flex={flex}
      size={size}
      fw={fw}
    />
  );
}

export function SetupPage() {
  const { t, i18n } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();
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
      return initConfig(toInitialProjectSetupRequest(values));
    },
    onSuccess: (written) => {
      // Take the revisions from the reply rather than waiting for the refetch
      // below: the screen stays open and can be saved again immediately, and a
      // save that overtakes the refetch would carry the revision it replaced.
      if ("revisions" in written) {
        queryClient.setQueryData<ProjectConfig>(["project-config"], (current) =>
          current ? { ...current, revisions: written.revisions } : current,
        );
      }
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
    validate: schemaResolver(validationSchema, { sync: true }),
  });
  const appliedInitialValues = useRef("");
  const serializedInitialValues = useMemo(() => JSON.stringify(initialValues), [initialValues]);
  const selectedCliAgentDetected = cliDetections.isLoading
    ? true
    : detectedCliAgentNames.has(form.values.cliAgent);
  // The URL is the single source of truth for which section is open. Holding
  // it in state instead would go stale: reaching Settings again through a link
  // (the sidebar's "Sync settings", a hash URL) changes only the search params
  // and never remounts this page, so a mount-time copy would keep showing the
  // section the page was first opened at.
  const sectionParam = searchParams.get("section");
  const section =
    sectionParam && (CORE_SETUP_SECTIONS_CONFIGURED as readonly string[]).includes(sectionParam)
      ? (sectionParam as CoreSection)
      : "project";
  const setSection = (value: CoreSection) => setSearchParams({ section: value }, { replace: true });
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
  // A saved project whose content is loaded: only then is there something
  // to compare a save against, and something to save into.
  const canSaveProject = hasExistingProject && projectConfig.isSuccess;
  // Keys already stored in the workspace secret store. Only a saved project has
  // them; during first setup the snapshot belongs to no workspace yet (and may
  // still be cached from a previously opened one), so it must not be consulted.
  const storedProviderKeys = hasExistingProject ? projectConfig.data?.provider_api_keys : undefined;
  // Single source of truth for "this provider can be used": either the key was
  // typed into the form now, or it is already stored in the workspace. Both the
  // provider cards and the section readiness rule read this.
  const llmProviderAvailability = useMemo(() => {
    const result: Record<string, boolean> = {};
    for (const provider of llmProviders.data ?? []) {
      result[provider.provider] = isProviderKeyAvailable(
        provider.provider,
        form.values,
        storedProviderKeys,
      );
    }
    return result;
  }, [llmProviders.data, form.values, storedProviderKeys]);
  const effectiveActiveMemberCount = hasExistingProject
    ? activeMemberCount
    : draftActiveMemberCount;
  const coreSections: readonly CoreSection[] = hasExistingProject
    ? CORE_SETUP_SECTIONS_CONFIGURED
    : CORE_SETUP_SECTIONS_INITIAL;
  const initialProgress = useMemo(
    () =>
      getInitialCoreStatus(
        form.values,
        effectiveActiveMemberCount,
        selectedCliAgentDetected,
        storedProviderKeys,
      ),
    [effectiveActiveMemberCount, form.values, selectedCliAgentDetected, storedProviderKeys],
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

  const setupStatus = useSetupStatus(config.data, effectiveActiveMemberCount, form.values);
  const visibleStatus = hasExistingProject ? setupStatus : initialProgress;
  const currentSectionReady = currentCoreSection
    ? isCoreSectionReady(currentCoreSection, visibleStatus)
    : true;
  /**
   * Save the basic settings.
   *
   * Returns where the files it wrote now stand, so a save that continues into
   * the advanced editor is composed against this result rather than against
   * what was on screen before it ran — or null when nothing was written, so
   * that the second half does not run on its own. One button applying half of
   * itself is worse than applying neither half, and it contradicts what the
   * refusal just told the user.
   */
  const saveNow = async (): Promise<ConfigRevisions | null> => {
    if (form.validate().hasErrors) {
      setSaveState("error");
      return null;
    }
    const creatingInitialSetup = !hasExistingProject;
    const initialSetupRequest = creatingInitialSetup
      ? toInitialProjectSetupRequest(form.values)
      : null;
    let revisionsWritten: ConfigRevisions | null = null;
    setSaveState("saving");
    try {
      const written = await saveMutation.mutateAsync(form.values);
      if (creatingInitialSetup) {
        await restartBackend(form.values.workspaceDir);
        await Promise.all([
          queryClient.refetchQueries({ queryKey: ["config"] }),
          queryClient.refetchQueries({ queryKey: ["team"] }),
        ]);
      }
      form.resetDirty(form.values);
      setSaveState("saved");
      revisionsWritten = "revisions" in written ? written.revisions : {};
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
              })}
            </Text>
          ),
        });
      }
    } catch (error) {
      setSaveState("error");
      if (isStaleConfigSave(error)) {
        // Reload rather than retry: the reply carries the current revisions,
        // and re-sending this form would be the overwrite that was refused.
        await queryClient.refetchQueries({ queryKey: ["project-config"] });
        notifications.show({
          color: "warning",
          title: t("setup.staleSave.title"),
          message: t("setup.staleSave.body"),
        });
      } else if (isBusyConfigSave(error)) {
        // The form is still good; it was simply never compared. Keep it as it
        // is so the same click can be repeated.
        notifications.show({
          color: "warning",
          title: t("setup.busySave.title"),
          message: t("setup.busySave.body"),
        });
      }
    }
    return revisionsWritten;
  };
  const applyWorkspaceSwitch = async (
    workspace: string,
    switchId: number,
    alreadySwitched = false,
  ) => {
    if (!alreadySwitched) {
      await restartBackend(workspace);
    }
    if (workspaceSwitchId.current !== switchId) {
      return;
    }
    setDraftActiveMemberCount(0);
    // Drop the previous workspace's snapshots outright: `setQueryData(key,
    // undefined)` bails out instead of clearing, and a query disabled by the
    // new workspace state is never refetched, so stale data would survive and
    // be shown as if it belonged to the workspace just opened.
    queryClient.removeQueries({ queryKey: ["team"] });
    queryClient.removeQueries({ queryKey: ["project-config"] });
    queryClient.invalidateQueries({ queryKey: ["intelligence-config"] });
    queryClient.invalidateQueries({ queryKey: ["command-options"] });
    // Hotkey assignments live in the workspace config, so the previous
    // workspace's combinations must not stay registered with the OS.
    queryClient.invalidateQueries({ queryKey: ["hotkeys"] });
    queryClient.invalidateQueries({ queryKey: ["scheduler"] });
    await Promise.all([
      queryClient.refetchQueries({ queryKey: ["config"] }),
      queryClient.refetchQueries({ queryKey: ["team"] }),
    ]);
    await queryClient.refetchQueries({ queryKey: ["project-config"] });
    // The quick run window has its own cache and never reloads.
    await announceWorkspaceChange();
    setSaveState("saved");
  };
  const startWorkspaceSwitch = (workspace: string, alreadySwitched = false) => {
    const switchId = workspaceSwitchId.current + 1;
    workspaceSwitchId.current = switchId;
    setWorkspaceSwitching(true);
    setSaveState("saving");
    void applyWorkspaceSwitch(workspace, switchId, alreadySwitched)
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
  /**
   * Take up a workspace the backend has already switched to.
   *
   * Taking a copy from a hub selects it on the server and answers with the new
   * configuration, so switching again here would stop the synchronization queue
   * that had just been started — and a queue that does not stop within the
   * timeout reports the copy as blocked although it was taken.
   */
  const adoptWorkspace = (workspace: string) => {
    form.setFieldValue("workspaceDir", workspace);
    if (workspace) {
      startWorkspaceSwitch(workspace, true);
    }
  };
  const dismissPendingWorkspaceSwitch = () => {
    setPendingWorkspaceSwitch("");
    const currentWorkspace = config.data?.workspace ?? "";
    if (currentWorkspace) {
      form.setFieldValue("workspaceDir", currentWorkspace);
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
        onCreateInitial={async () => {
          await saveNow();
        }}
        creating={saveMutation.isPending}
        canGoBack={canGoBack}
        canGoNext={canGoNext}
        currentSectionReady={currentSectionReady}
        onGoBack={goBackSection}
        onGoNext={goNextSection}
      />

      <div className="setup-layout">
        <SetupSectionNav
          active={activeSection}
          onChange={setSection}
          sections={coreSections}
          status={visibleStatus}
        />
        <Stack gap="md">
          {activeSection === "project" ? (
            <ProjectSection
              form={form}
              saveState={saveState}
              persisted={canSaveProject && !workspaceSwitching}
              saving={saveMutation.isPending}
              onSave={saveNow}
              onWorkspaceChange={changeWorkspace}
              onWorkspaceCloned={adoptWorkspace}
            />
          ) : null}
          {activeSection === "intelligence" ? (
            <IntelligenceSection
              form={form}
              saveState={saveState}
              persisted={canSaveProject && !workspaceSwitching}
              saving={saveMutation.isPending}
              onSave={saveNow}
              detections={cliDetections.data?.agents ?? []}
              detectionLoading={cliDetections.isLoading}
              storedProviderKeys={storedProviderKeys}
              providers={llmProviders.data ?? []}
            />
          ) : null}
          {activeSection === "github" ? <GitHubIntegrationSection form={form} /> : null}
          {activeSection === "shortcuts" ? <ShortcutsSection /> : null}
          {activeSection === "sync" ? <SyncSettings /> : null}
          {activeSection === "device" ? <DeviceSettings /> : null}
          {activeSection === "verification" ? (
            <VerificationSection
              config={config.data}
              projectConfig={projectConfig.data}
              activeMemberCount={effectiveActiveMemberCount}
            />
          ) : null}
          {activeSection === "members" ? (
            <MembersSection
              activeMemberCount={effectiveActiveMemberCount}
              members={persistedTeam?.members ?? []}
              defaultPersonId={persistedTeam?.default_person_id ?? ""}
              config={config.data}
              workspaceDir={form.values.workspaceDir}
              projectGithubEnabled={form.values.githubDecision === "enabled"}
              githubOrganizationDefault={parseGitHub(form.values.githubProjectUrl).organization}
              agentFieldTarget={
                form.values.githubDecision === "enabled" ? buildLaneFetchTarget(form.values) : null
              }
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

/**
 * The sections of Setup, as the only way to move between them.
 *
 * Only the sections that can actually be opened are listed: a button that
 * silently leaves the screen where it was reads as a broken app, so which
 * sections exist is decided in one place rather than drawn here and filtered
 * where the section is rendered.
 */
function SetupSectionNav({
  active,
  onChange,
  sections,
  status,
}: {
  active: string;
  onChange: (value: CoreSection) => void;
  sections: readonly CoreSection[];
  status: SetupStatus;
}) {
  const { t } = useTranslation();
  const groups = [
    { labelKey: "setup.nav.groupWorkspace", machine: false },
    { labelKey: "setup.nav.groupMachine", machine: true },
  ].map((group) => ({
    ...group,
    sections: sections.filter((value) => MACHINE_SECTIONS.has(value) === group.machine),
  }));
  return (
    <Card withBorder radius="md" p="xs" className="setup-nav">
      {groups.map((group) =>
        group.sections.length === 0 ? null : (
          <div className="setup-nav-group" key={group.labelKey}>
            <div className="nav-label">{t(group.labelKey)}</div>
            {group.sections.map((value) => (
              <button
                className={`setup-nav-item ${active === value ? "active" : ""}`}
                key={value}
                type="button"
                onClick={() => onChange(value)}
              >
                <StatusIcon ok={isCoreSectionReady(value, status)} />
                <span>{t(CORE_SECTION_LABEL_KEYS[value])}</span>
              </button>
            ))}
          </div>
        ),
      )}
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
  persisted,
  saving,
  onSave,
  onWorkspaceChange,
  onWorkspaceCloned,
}: {
  form: ProjectForm;
  saveState: "idle" | "saving" | "saved" | "error";
  persisted: boolean;
  saving: boolean;
  onSave: () => Promise<ConfigRevisions | null>;
  onWorkspaceChange: (value: string) => void;
  onWorkspaceCloned: (workspace: string) => void;
}) {
  const { t } = useTranslation();
  return (
    <Card withBorder radius="md" p="lg">
      <PanelHeader
        title={t("setup.project.title")}
        subtitle={t("setup.project.subtitle")}
        save={persisted ? { state: saveState, saving, onSave: () => void onSave() } : undefined}
      />
      <Stack mt="md">
        <Stack gap="xs">
          <FolderPicker value={form.values.workspaceDir} onChange={onWorkspaceChange} />
          <Group>
            <CloneFromHubButton
              destination={form.values.workspaceDir}
              onCloned={(status) => onWorkspaceCloned(status.workspace ?? "")}
            />
          </Group>
        </Stack>
        <Textarea
          label={<RequiredLabel text={t("setup.project.description")} />}
          aria-label={t("setup.project.description")}
          aria-required
          description={t("setup.project.descriptionHint")}
          autosize
          minRows={2}
          {...form.getInputProps("description")}
        />
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
        {form.values.githubDecision === "enabled" ? (
          <TextInput
            label={<RequiredLabel text={t("setup.github.projectUrl")} />}
            aria-label={t("setup.github.projectUrl")}
            aria-required
            description={t("setup.github.projectUrlHint")}
            {...form.getInputProps("githubProjectUrl")}
            error={
              getGitHubFieldErrors(form.values, t).githubProjectUrl || form.errors.githubProjectUrl
            }
          />
        ) : null}
      </Stack>
    </Card>
  );
}

function IntelligenceSection({
  form,
  saveState,
  persisted,
  saving,
  onSave,
  detections,
  detectionLoading,
  storedProviderKeys,
  providers,
}: {
  form: ProjectForm;
  saveState: "idle" | "saving" | "saved" | "error";
  persisted: boolean;
  saving: boolean;
  onSave: () => Promise<ConfigRevisions | null>;
  detections: CliAgentDetection[];
  detectionLoading: boolean;
  storedProviderKeys: Record<string, boolean> | undefined;
  providers: LlmProviderInfo[];
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  // The advanced editor writes different files from the basic settings above
  // it, but they are one screen to the user, so one button saves both.
  const saveAdvanced = useRef<((written: ConfigRevisions) => Promise<void>) | null>(null);
  const [savingSection, setSavingSection] = useState(false);
  const saveSection = async () => {
    setSavingSection(true);
    try {
      // The basic settings write two of the files the advanced editor guards,
      // so the advanced save is composed against what this one just left --
      // otherwise one button would reliably collide with itself.
      const written = await onSave();
      if (written === null) {
        // The basic half wrote nothing. Going on would apply the advanced half
        // alone, which is not what one button means, and not what the message
        // the user just saw says happened.
        return;
      }
      await saveAdvanced.current?.(written);
    } catch {
      // Both halves report their own failure: the basic settings through the
      // section's save state, the advanced editor through its own alert.
    } finally {
      setSavingSection(false);
    }
  };

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
        save={
          persisted
            ? {
                state: saveState,
                saving: saving || savingSection,
                onSave: () => void saveSection(),
              }
            : undefined
        }
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
            {isProviderKeyAvailable(
              form.values.llmApiType,
              form.values,
              storedProviderKeys,
            ) ? null : (
              <Alert color="warning" title={t("setup.intelligence.apiKeyRequiredTitle")}>
                {t("setup.intelligence.apiKeyRequiredBody")}
              </Alert>
            )}
            <DefaultProviderCards
              providers={providers}
              isActive={(provider) => form.values.llmApiType === provider}
              isAvailable={(provider) =>
                isProviderKeyAvailable(provider, form.values, storedProviderKeys)
              }
              onSelect={(provider) => form.setFieldValue("llmApiType", provider)}
              renderExtra={(option) => (
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
                        storedProviderKeys?.[option.provider]
                          ? t("setup.intelligence.keyConfiguredDescription")
                          : undefined
                      }
                      placeholder={
                        storedProviderKeys?.[option.provider]
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
                    <SecretStatusHint envKey={option.api_key_env} />
                  </Popover.Dropdown>
                </Popover>
              )}
            />
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
            <DefaultCliAgentCards
              detections={detections}
              isActive={(agent) => form.values.cliAgent === agent.name}
              isDetected={(agent) =>
                detectionLoading ? form.values.cliAgent === agent.name : agent.detected
              }
              onSelect={(agent) => form.setFieldValue("cliAgent", agent.name)}
              renderExtra={(agent) => {
                const detected = detectionLoading
                  ? form.values.cliAgent === agent.name
                  : agent.detected;
                if (!detected) {
                  return null;
                }
                const status = (skillStatuses.data?.agents ?? []).find(
                  (s) => s.agent === agent.name,
                );
                const statusKey = status?.status ?? "agent_home_missing";
                const canForceUpdate = Boolean(status?.can_force_update);
                return (
                  <HoverCard width={340} shadow="md" withArrow openDelay={200} closeDelay={400}>
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
                          <Badge color={skillStatusColor(statusKey)} variant="light" size="sm">
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
                );
              }}
            />
          </Stack>
        </Card>

        {persisted ? (
          <Accordion variant="contained">
            <Accordion.Item value="advanced-intelligence">
              <Accordion.Control>{t("setup.intelligence.advanced")}</Accordion.Control>
              <Accordion.Panel>
                <IntelligenceEditor
                  enabled={persisted}
                  onRegisterSave={(save) => {
                    saveAdvanced.current = save;
                  }}
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

type ProviderDefaults = Record<
  string,
  {
    model_class: string;
    model_id: string;
    effort: EffortOverlay;
    effort_fields: EffortFieldSpec[];
  }
>;

// Per-provider default model_class/model_id, sourced from the backend provider
// catalog (`models/<provider>/default.yml`). Keeping it server-side means
// swapping a provider's default model is a data change, not a frontend change.
function providerDefaultsMap(providers: LlmProviderInfo[]): ProviderDefaults {
  return Object.fromEntries(
    providers.map((entry) => [
      entry.provider,
      {
        model_class: entry.model_class,
        model_id: entry.model_id,
        effort: entry.effort ?? {},
        effort_fields: entry.effort_fields ?? [],
      },
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

// AI CLI tool definitions use the same two-level shape as model definitions, so
// a slot owns its own file and two slots on one tool can differ.
function slotCliPath(tool: string, slotKey: string): string {
  return `cli_agents/${tool}/${slotKey}.yml`;
}

function cliToolFromPath(path: string | undefined): string {
  const parts = path?.split("/") ?? [];
  return parts.length >= CLI_PATH_PARTS && parts[0] === "cli_agents" ? parts[1] : "";
}

// The tool picker lists one entry per tool (its `default.yml`), so a slot with
// its own definition still has to resolve to that entry to show as selected.
function cliToolDefaultPath(path: string | undefined): string {
  const tool = cliToolFromPath(path);
  return tool ? slotCliPath(tool, "default") : (path ?? "");
}

const CLI_PATH_PARTS = 3;

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
    parameters: def?.model_id ? { id: def.model_id } : {},
    // Left empty on purpose: the slot inherits its provider's mapping (the
    // runtime resolves the same fallback), so switching provider adopts the new
    // provider's defaults instead of materializing a copy of the old ones.
    effort: {},
    inherited_effort: def?.effort ?? {},
    effort_fields: def?.effort_fields ?? [],
  };
}

// Assigns a definition to `slotKey`, creating it when new and dropping any
// definition no slot references -- the AI CLI tool twin of `withSlotModel`.
function withSlotCliAgent(
  current: IntelligenceConfig,
  slotKey: string,
  path: string,
  tool: string,
): IntelligenceConfig {
  const mapping = { ...current.cli_agent_mapping, [slotKey]: path };
  const referenced = new Set(Object.values(mapping));
  const agents = current.cli_agents.filter((agent) => referenced.has(agent.path));
  if (!agents.some((agent) => agent.path === path)) {
    const toolDefault = current.cli_agents.find((agent) => agent.path === cliToolDefaultPath(path));
    agents.push({
      path,
      name: tool,
      detected: toolDefault?.detected ?? false,
      detected_path: toolDefault?.detected_path ?? "",
      effort: {},
      inherited_effort: toolDefault?.inherited_effort ?? {},
      effort_fields: toolDefault?.effort_fields ?? [],
      effort_supported: toolDefault?.effort_supported ?? true,
    });
  }
  return { ...current, cli_agent_mapping: mapping, cli_agents: agents };
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

// A single selectable option card. `extra` is an optional top-right control
// (e.g. the team's API-key popover or CLI skill-status button) that only the
// team scope supplies; the member scope renders the same card without it.
function OptionCard({
  label,
  active,
  enabled,
  statusOk,
  statusText,
  disabledTooltip,
  onSelect,
  extra,
}: {
  label: string;
  active: boolean;
  enabled: boolean;
  statusOk: boolean;
  statusText: string;
  disabledTooltip: string;
  onSelect: () => void;
  extra?: ReactNode;
}) {
  const card = (
    <div style={{ position: "relative", display: "block", width: "100%" }}>
      <button
        type="button"
        aria-label={label}
        disabled={!enabled}
        className={`option-card ${active ? "active" : ""}`}
        style={{ paddingRight: extra ? "40px" : undefined, width: "100%", textAlign: "left" }}
        onClick={() => {
          if (enabled) onSelect();
        }}
      >
        <span className="title" style={{ userSelect: "none" }}>
          {label}
        </span>
        <span className={`detection ${statusOk ? "ok" : "ng"}`} style={{ userSelect: "none" }}>
          <i />
          {statusText}
        </span>
      </button>
      {extra ? (
        <div
          style={{ position: "absolute", top: "10px", right: "10px" }}
          onClick={(event) => event.stopPropagation()}
        >
          {extra}
        </div>
      ) : null}
    </div>
  );
  return (
    <Tooltip label={disabledTooltip} position="top" withArrow disabled={enabled}>
      {card}
    </Tooltip>
  );
}

// The "default LLM provider" card grid, shared by the team and member scopes.
// Selection/availability bindings differ per scope (team writes the project
// config, a member writes their default model slot), so they are passed in.
function DefaultProviderCards({
  providers,
  isActive,
  isAvailable,
  onSelect,
  renderExtra,
}: {
  providers: LlmProviderInfo[];
  isActive: (provider: string) => boolean;
  isAvailable: (provider: string) => boolean;
  onSelect: (provider: string) => void;
  renderExtra?: (option: LlmProviderInfo) => ReactNode;
}) {
  const { t } = useTranslation();
  return (
    <div className="option-card-grid">
      {providers.map((option) => {
        const available = isAvailable(option.provider);
        return (
          <OptionCard
            key={option.provider}
            label={option.label}
            active={isActive(option.provider)}
            enabled={available}
            statusOk={available}
            statusText={
              available
                ? t("setup.intelligence.apiKeyConfigured")
                : t("setup.intelligence.apiKeyMissing")
            }
            disabledTooltip={t("setup.intelligence.apiKeyMissingTooltip")}
            onSelect={() => onSelect(option.provider)}
            extra={renderExtra?.(option)}
          />
        );
      })}
    </div>
  );
}

// The "default AI CLI tool" card grid, shared by the team and member scopes.
function DefaultCliAgentCards({
  detections,
  isActive,
  isDetected,
  onSelect,
  renderExtra,
}: {
  detections: CliAgentDetection[];
  isActive: (agent: CliAgentDetection) => boolean;
  isDetected: (agent: CliAgentDetection) => boolean;
  onSelect: (agent: CliAgentDetection) => void;
  renderExtra?: (agent: CliAgentDetection) => ReactNode;
}) {
  const { t } = useTranslation();
  return (
    <div className="option-card-grid">
      {detections.map((agent) => {
        const detected = isDetected(agent);
        return (
          <OptionCard
            key={agent.name}
            label={agent.label}
            active={isActive(agent)}
            enabled={detected}
            statusOk={detected}
            statusText={
              detected ? t("setup.intelligence.detected") : t("setup.intelligence.notDetected")
            }
            disabledTooltip={t("setup.intelligence.notDetectedOnPath")}
            onSelect={() => onSelect(agent)}
            extra={renderExtra?.(agent)}
          />
        );
      })}
    </div>
  );
}

function NativeAgentPolicyEditor({
  policy,
  onChange,
}: {
  policy: NativeAgentPolicySettings;
  onChange: (policy: NativeAgentPolicySettings) => void;
}) {
  const { t } = useTranslation();
  const filesystemOptions = ["workspace", "host"] as const;
  const setAdapter = (
    adapter: NativeAgentPolicyAdapter,
    filesystem_access: NativeAgentFilesystemAccess,
  ) => onChange({ ...policy, [adapter]: { filesystem_access } });

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
        {NATIVE_POLICY_ADAPTERS.map((adapter) => {
          const agent = t(`setup.intelligence.nativeAgents.${adapter}`);
          return (
            <Stack gap="xs" key={adapter}>
              <Select
                label={t("setup.intelligence.filesystemAccessFor", { agent })}
                description={t(`setup.intelligence.sandboxMapping.${adapter}`)}
                data={filesystemOptions.map((value) => ({
                  value,
                  label: t(`setup.intelligence.filesystemOptions.${value}`),
                }))}
                value={policy[adapter].filesystem_access}
                onChange={(value) =>
                  setAdapter(adapter, (value ?? "workspace") as NativeAgentFilesystemAccess)
                }
              />
              {policy[adapter].filesystem_access === "host" ? (
                <Alert color="warning" title={t("setup.intelligence.hostAccessWarningTitle")}>
                  {t("setup.intelligence.hostAccessWarningBody", { agent })}
                </Alert>
              ) : null}
            </Stack>
          );
        })}
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
  /** The enclosing section's save button drives this editor too. */
  onRegisterSave?: (save: ((written?: ConfigRevisions) => Promise<void>) | null) => void;
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
  // Field id -> the JSON in that editor does not parse. A malformed object must
  // block saving, not be silently dropped on the way to the backend.
  const [jsonErrors, setJsonErrors] = useState<Record<string, boolean>>({});
  const setJsonValidity = useCallback((fieldId: string, valid: boolean) => {
    setJsonErrors((current) => {
      if (valid === !current[fieldId]) return current;
      const next = { ...current };
      if (valid) delete next[fieldId];
      else next[fieldId] = true;
      return next;
    });
  }, []);
  const hasJsonError = Object.keys(jsonErrors).length > 0;
  const mutation = useMutation({
    mutationFn: updateIntelligenceConfig,
    onSuccess: (written) => {
      // Take the revisions from the reply rather than waiting for the refetch
      // below: this editor stays open and can be saved again immediately, and a
      // save that overtakes the refetch would carry the revision it replaced.
      queryClient.setQueryData<IntelligenceConfig>(
        ["intelligence-config", personId ?? "team"],
        (current) => (current ? { ...current, revisions: written.revisions } : current),
      );
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
  const canSave = Boolean(payload && dirty && !hasJsonError);

  const saveDraft = useCallback(
    async (written?: ConfigRevisions) => {
      if (!payload || !serializedPayload || hasJsonError) {
        return;
      }
      await mutation.mutateAsync({
        ...payload,
        // `written` describes files a save that just ran left behind, and takes
        // precedence over what this editor read before that save.
        expected_revisions: { ...(query.data?.revisions ?? {}), ...(written ?? {}) },
      });
      setDraftState((current) =>
        current?.key === draftKey ? { ...current, savedSerialized: serializedPayload } : current,
      );
    },
    [draftKey, hasJsonError, mutation, payload, query.data, serializedPayload],
  );

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
  };

  useEffect(() => {
    if (!enabled || !onRegisterSave) {
      return;
    }
    onRegisterSave(canSave ? saveDraft : null);
    return () => onRegisterSave(null);
  }, [canSave, enabled, onRegisterSave, saveDraft]);

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
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reacts to the basic setting only; a draft dep would clobber advanced edits before the draft->prop sync runs
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
            detected: false,
            detected_path: "",
            effort: {},
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
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reacts to the basic setting only; a draft dep would clobber advanced edits before the draft->prop sync runs
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
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reacts to the default slot path only; the ref guard makes broader deps pure re-run noise
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
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reacts to the default slot path only; the ref guard makes broader deps pure re-run noise
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
  // A member may override a team-owned slot's value but not delete or rename it
  // (the runtime merge would only revive it). Lock those names, alongside the
  // always-fixed "default"/"agent". For the team scope the inherited sets are
  // empty, so only the built-ins are locked.
  const inheritedModelSlots = new Set(draft.inherited_model_slots ?? []);
  const inheritedCliSlots = new Set(draft.inherited_cli_slots ?? []);
  const inheritedBrainFeatures = new Set(draft.inherited_brain_features ?? []);
  const isModelSlotLocked = (slotKey: string) =>
    slotKey === "default" || inheritedModelSlots.has(slotKey);
  const isCliSlotLocked = (slotKey: string) =>
    slotKey === "default" || inheritedCliSlots.has(slotKey);
  const isBrainFeatureLocked = (name: string) =>
    name === "default" || name === "agent" || inheritedBrainFeatures.has(name);
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
      .filter(
        (agent) =>
          agent.path === cliToolDefaultPath(agent.path) &&
          !detections.some((d) => d.config_reference === agent.path),
      )
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
    if (isModelSlotLocked(slotKey)) return;
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
    if (isModelSlotLocked(oldKey) || !newKey.trim() || oldKey === newKey) return;
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

  const handleUpdateLlmSlotParameters = (slotKey: string, parameters: Record<string, unknown>) => {
    updateDraft((current) => ({
      ...current,
      models: current.models.map((model) =>
        model.path === current.model_mapping[slotKey] ? { ...model, parameters } : model,
      ),
    }));
  };

  // The effort overlay is provider-shaped, so it is edited (and replaced) as a
  // whole object rather than merged key by key.
  const handleUpdateLlmSlotEffort = (slotKey: string, effort: ModelDefinition["effort"]) => {
    updateDraft((current) => ({
      ...current,
      models: current.models.map((model) =>
        model.path === current.model_mapping[slotKey] ? { ...model, effort } : model,
      ),
    }));
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
      const tool = cliToolFromPath(current.cli_agent_mapping.default);
      return withSlotCliAgent(current, baseName, slotCliPath(tool, baseName), tool);
    });
  };

  const handleDeleteCliSlot = (slotKey: string) => {
    if (isCliSlotLocked(slotKey)) return;
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
    if (isCliSlotLocked(oldKey) || !newKey.trim() || oldKey === newKey) return;
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
      // The picker offers a tool (its `default.yml`); the slot gets a
      // definition of its own on that tool.
      const tool = cliToolFromPath(agentPath);
      return withSlotCliAgent(current, slotKey, slotCliPath(tool, slotKey), tool);
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
    if (!target || isBrainFeatureLocked(target.name)) return;
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
    if (!target || isBrainFeatureLocked(target.name) || !newName.trim()) return;
    updateDraft((current) => {
      const exists = current.brain_mapping.some((b, i) => i !== index && b.name === newName);
      if (exists) return current;

      const updated = [...current.brain_mapping];
      updated[index] = { ...target, name: newName };
      return { ...current, brain_mapping: updated };
    });
  };

  // Team and member scopes share one advanced editor. A member only adds the
  // "inherit team defaults" toggle on top; when inheriting is off they get the
  // exact same full editor (feature assignments, model slots, CLI slots, native
  // policy) as the team, so member-scoped slots like a translation model are
  // always visible and editable rather than silently missing.
  return (
    <Stack gap="md">
      <Group justify="space-between">
        <Text size="sm" c="dimmed">
          {personId
            ? t("setup.intelligence.memberOverrideDescription")
            : t("setup.intelligence.teamAdvancedDescription")}
        </Text>
      </Group>
      {personId ? (
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
      ) : null}
      {(() => {
        if (draft.inherited) {
          return (
            <InfoCallout title={t("setup.intelligence.inheritingTitle")}>
              {t("setup.intelligence.inheritingBody")}
            </InfoCallout>
          );
        }
        // The full editor (feature assignments, model/CLI slots, native policy)
        // is identical for both scopes. The team renders it directly; a member
        // gets the same friendly default-provider/CLI cards as the team on top,
        // with this editor tucked into a "詳細設定" accordion below.
        const advancedBody = (
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
                        disabled={isBrainFeatureLocked(assignment.name)}
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
                        onChange={(value) =>
                          handleUpdateBrain(index, { target: value ?? "default" })
                        }
                        flex={1.5}
                      />
                      {!isBrainFeatureLocked(assignment.name) ? (
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
                      // One bounded box per slot: without it the rows of two
                      // slots run together, and the AI CLI tool card (whose
                      // slots are accordion items) already reads as grouped.
                      <Paper key={slotKey} withBorder radius="sm" p="sm">
                        <Stack gap="xs">
                          <Group align="flex-end" gap="xs" wrap="nowrap">
                            <SlotNameInput
                              key={slotKey}
                              label={t("setup.intelligence.slot")}
                              value={slotKey}
                              readOnly={isModelSlotLocked(slotKey)}
                              onRename={handleRenameLlmSlot}
                              flex={1}
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
                              flex={1}
                            />
                            {!isModelSlotLocked(slotKey) ? (
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
                          <Group align="flex-end" gap="xs" wrap="nowrap">
                            <Box flex={1}>
                              <ToolSettingsField
                                value={modelDef.parameters ?? {}}
                                fields={modelDef.effort_fields ?? []}
                                onChange={(parameters) =>
                                  handleUpdateLlmSlotParameters(slotKey, parameters)
                                }
                              />
                            </Box>
                            {/* Keeps this row's columns aligned with the one above,
                              which reserves the same width for the delete icon. */}
                            <Box w={28} />
                          </Group>
                          {/* Keyed on the path so switching provider reseeds the
                            editor: the draft's overlay is replaced with the new
                            provider's defaults, and stale state must not linger. */}
                          <EffortSettingsField
                            key={`model-effort:${modelDef.path}`}
                            onValidityChange={(valid) =>
                              setJsonValidity(`model-effort:${modelDef.path}`, valid)
                            }
                            value={modelDef.effort ?? {}}
                            inherited={modelDef.inherited_effort ?? {}}
                            fields={modelDef.effort_fields ?? []}
                            onChange={(effort) =>
                              handleUpdateLlmSlotEffort(
                                slotKey,
                                effort as ModelDefinition["effort"],
                              )
                            }
                          />
                        </Stack>
                      </Paper>
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

                    const detection = detectedByPath[cliToolDefaultPath(agentDef.path)];
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
                              {!isCliSlotLocked(slotKey) ? (
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
                              <SlotNameInput
                                key={slotKey}
                                label={t("setup.intelligence.slot")}
                                value={slotKey}
                                readOnly={isCliSlotLocked(slotKey)}
                                onRename={handleRenameCliSlot}
                                size="xs"
                                fw={600}
                              />
                              <Select
                                label={t("setup.intelligence.cliAgent")}
                                data={cliFileOptions}
                                // The picker lists tools, so a slot with its
                                // own definition selects its tool's entry.
                                value={cliToolDefaultPath(path)}
                                onChange={(val) =>
                                  handleUpdateCliSlotAgentPath(slotKey, val ?? path)
                                }
                                size="xs"
                              />
                            </Group>
                            <ToolSettingsField
                              value={agentDef.parameters ?? {}}
                              fields={agentDef.effort_fields ?? []}
                              onChange={(parameters) =>
                                handleUpdateCliAgentDef(agentDef.path, { parameters })
                              }
                            />
                            <EffortSettingsField
                              key={`cli-effort:${agentDef.path}`}
                              onValidityChange={(valid) =>
                                setJsonValidity(`cli-effort:${agentDef.path}`, valid)
                              }
                              value={agentDef.effort ?? {}}
                              inherited={agentDef.inherited_effort ?? {}}
                              fields={agentDef.effort_fields ?? []}
                              supported={agentDef.effort_supported ?? true}
                              onChange={(effort) =>
                                handleUpdateCliAgentDef(agentDef.path, {
                                  effort: effort as CliAgentDefinition["effort"],
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
        );
        if (!personId) {
          return advancedBody;
        }
        return (
          <Stack gap="md">
            <Card withBorder radius="sm" p="md">
              <Stack gap="xs">
                <Text size="sm" fw={700}>
                  {t("setup.intelligence.defaultProvider")}
                </Text>
                <Text size="sm" c="dimmed">
                  {t("setup.intelligence.providerDescription")}
                </Text>
                <DefaultProviderCards
                  providers={providers}
                  isActive={(provider) =>
                    providerFromModelPath(draft.model_mapping.default) === provider
                  }
                  isAvailable={(provider) => Boolean(llmProviderAvailability?.[provider])}
                  onSelect={(provider) =>
                    updateDraft((current) =>
                      withSlotModel(
                        current,
                        "default",
                        providerDefaultModel(provider, "default", providerDefaults),
                      ),
                    )
                  }
                />
              </Stack>
            </Card>
            <Card withBorder radius="sm" p="md">
              <Stack gap="xs">
                <Text size="sm" fw={700}>
                  {t("setup.intelligence.defaultCliAgent")}
                </Text>
                <Text size="sm" c="dimmed">
                  {t("setup.intelligence.cliHint")}
                </Text>
                <DefaultCliAgentCards
                  detections={detections}
                  isActive={(agent) => draft.cli_agent_mapping.default === agent.config_reference}
                  isDetected={(agent) => agent.detected}
                  // Reuse the advanced-slot handler so the picked tool is also
                  // registered in draft.cli_agents; otherwise the default slot
                  // would vanish when the 詳細設定 accordion is opened.
                  onSelect={(agent) =>
                    handleUpdateCliSlotAgentPath("default", agent.config_reference)
                  }
                />
              </Stack>
            </Card>
            <Accordion variant="contained">
              <Accordion.Item value="advanced-intelligence">
                <Accordion.Control>{t("setup.intelligence.advanced")}</Accordion.Control>
                <Accordion.Panel>{advancedBody}</Accordion.Panel>
              </Accordion.Item>
            </Accordion>
          </Stack>
        );
      })()}
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
  defaultPersonId,
  config,
  workspaceDir,
  projectGithubEnabled,
  githubOrganizationDefault,
  agentFieldTarget,
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
  defaultPersonId: string;
  config: ConfigStatus | undefined;
  workspaceDir: string;
  projectGithubEnabled: boolean;
  githubOrganizationDefault: string;
  agentFieldTarget: ProjectStatusOptionsRequest | null;
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
  // GitHub Apps entry mode: register a new app on GitHub, or reference an
  // already-registered one via its settings URL.
  const [githubAppsSetupMode, setGithubAppsSetupMode] = useState<"create" | "existing">("create");
  // Slack App entry mode, mirroring GitHub Apps: create a new app from a
  // manifest deep link, or paste tokens from an app that already exists.
  const [slackAppsSetupMode, setSlackAppsSetupMode] = useState<"create" | "existing">("create");
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
  // Which environment key each of this member's credentials is stored under.
  // Only a saved member has them: the naming is the backend's, and a member
  // being created has nothing stored to have a state yet.
  const [memberSecretEnvKeys, setMemberSecretEnvKeys] = useState<Record<string, string>>({});
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
  // Only saved agent members can execute commands, so drafts, humans and
  // inactive members are never default-executor candidates. A member that was
  // deactivated while configured stays listed, otherwise the stored setting
  // would be unreachable from this screen.
  const defaultPersonOptions = useMemo(
    () =>
      members
        .filter(
          (member) =>
            member.person_type !== "human" &&
            (member.is_active || member.person_id === defaultPersonId),
        )
        .map((member) => ({
          value: member.person_id,
          label: `${member.name} (${member.person_id})`,
        })),
    [defaultPersonId, members],
  );
  const formVisible = mode !== "idle" || displayedMembers.length === 0;
  const formMode = mode === "edit" ? "edit" : "add";
  // The member form is reused across members, so the app registration panels
  // would carry one member's edited name and started registration over to the
  // next. They reset their own state whenever this edit-session identity
  // changes.
  const memberFormKey = `${formMode}:${editingPersonId ?? ""}`;

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
    setGithubAppsSetupMode("create");
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
    setMemberSecretEnvKeys({});
    setSlackAppsSetupMode("create");
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
    setGithubPrivateKeyPath("");
    setGithubAppsSetupMode(
      member.github_app_id != null || member.has_github_app_id ? "existing" : "create",
    );
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
      githubPrivateKeyPath: member.has_github_private_key,
      githubAccessToken: member.has_github_access_token,
      slackBotToken: member.has_slack_bot_token,
      slackAppToken: member.has_slack_app_token,
    });
    setMemberSecretEnvKeys(member.secret_env_keys ?? {});
    // A member that already carries Slack credentials is being edited, not
    // set up, so default to the mode that does not push them into Slack.
    setSlackAppsSetupMode(
      member.has_slack_bot_token || member.has_slack_app_token ? "existing" : "create",
    );
    setIsActive(nextPersonType === "human" ? false : member.is_active);
  };

  // What the member form was composed against, sent back with the save so an
  // edit that arrived from another machine meanwhile is not replaced unseen.
  const [memberRevisions, setMemberRevisions] = useState<ConfigRevisions>({});
  const memberConfigMutation = useMutation({
    mutationFn: getMemberConfig,
    onSuccess: (snapshot) => {
      setMemberRevisions(snapshot.revisions);
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
  const defaultPersonMutation = useMutation({
    mutationFn: (personId: string) => updateDefaultPerson(personId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["team"] });
    },
  });
  const memberDiagnosticsMutation = useMutation({
    mutationFn: (targetPersonId: string) => runScenarioDiagnostics(targetPersonId),
  });
  const addMemberMutation = useMutation({
    mutationFn: addMemberConfig,
    onSuccess: (written, request) => {
      if (!hasPersistedProject) {
        setDraftMembers((current) => [
          ...current.filter((member) => member.person_id !== request.person_id),
          memberRequestToConfig(request, written.revisions),
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
    onSuccess: (written, variables) => {
      // The screen stays open on the member that was just saved, so without
      // this its next save would be composed against the revision it replaced.
      setMemberRevisions(written.revisions);
      const previous =
        members.find((member) => member.person_id === variables.originalPersonId) ??
        draftMembers.find((member) => member.person_id === variables.originalPersonId);
      const previousActive = previous?.is_active ?? false;
      const delta = Number(effectiveIsActive) - Number(previousActive);
      if (!hasPersistedProject) {
        setDraftMembers((current) => [
          ...current.filter((member) => member.person_id !== variables.originalPersonId),
          memberRequestToConfig(variables.body, written.revisions),
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
    mutationFn: ({ targetPersonId, configDir }: { targetPersonId: string; configDir: string }) =>
      deleteMemberConfig(targetPersonId, {
        config_dir: configDir,
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

  // A member that cannot be a GitHub assignee is assigned through the
  // project's Agent field, so keep the field's options in sync whenever a
  // GitHub-linked non-human member is saved. Best-effort: the member save
  // itself already succeeded, so a failure only warns.
  const syncAgentFieldAfterSave = (request: MemberSetupRequest) => {
    if (!agentFieldTarget || request.person_type === "human" || !request.github_account_type) {
      return;
    }
    ensureAgentField(agentFieldTarget)
      .then((state) => {
        queryClient.setQueryData(["agentFieldState", agentFieldTarget], state);
      })
      .catch(() => {
        notifications.show({
          color: "warning",
          title: t("setup.members.agentFieldSyncFailedTitle"),
          message: t("setup.members.agentFieldSyncFailedBody"),
        });
      });
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
            memberRequestToConfig(
              { ...request, original_person_id: editingPersonId },
              memberRevisions,
            ),
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
            expected_revisions: memberRevisions,
          },
        });
        syncAgentFieldAfterSave(request);
        if (!isHumanMember) {
          await memberIntelligenceSaveRef.current?.();
        }
        return;
      }
      await addMemberMutation.mutateAsync(request);
      syncAgentFieldAfterSave(request);
    } catch (error) {
      if (isStaleConfigSave(error) && editingPersonId) {
        // Reload the member rather than resend this form: the input it holds
        // is what would have overwritten the change that arrived.
        memberConfigMutation.mutate(editingPersonId);
        notifications.show({
          color: "warning",
          title: t("setup.staleSave.title"),
          message: t("setup.staleSave.body"),
        });
        return;
      }
      if (isBusyConfigSave(error)) {
        notifications.show({
          color: "warning",
          title: t("setup.busySave.title"),
          message: t("setup.busySave.body"),
        });
        return;
      }
      throw error;
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
    setMemberRevisions({});
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
    if (!editingPersonId || !configDir.trim()) {
      return;
    }
    await deleteMemberMutation.mutateAsync({
      targetPersonId: editingPersonId,
      configDir,
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
                  <Group w={72} justify="flex-end" gap="xs" wrap="nowrap">
                    {member.person_id === defaultPersonId ? (
                      <Badge color="success" variant="light" style={{ flexShrink: 0 }}>
                        {t("setup.members.defaultPersonBadge")}
                      </Badge>
                    ) : null}
                  </Group>
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
        {defaultPersonOptions.length > 1 ? (
          <Select
            label={t("setup.members.defaultPersonLabel")}
            description={t("setup.members.defaultPersonDescription")}
            placeholder={t("setup.members.defaultPersonPlaceholder")}
            data={defaultPersonOptions}
            value={defaultPersonId || null}
            disabled={defaultPersonMutation.isPending}
            onChange={(value) => value && defaultPersonMutation.mutate(value)}
            maw={360}
          />
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
                  {githubAccountType === "github_apps" ? (
                    <SegmentedControl
                      aria-label={t("setup.members.githubAppsSetupMode.label")}
                      value={githubAppsSetupMode}
                      onChange={(value) => {
                        setGithubAppsSetupMode(value === "existing" ? "existing" : "create");
                        setIdentityResolveError("");
                      }}
                      data={[
                        {
                          value: "create",
                          label: t("setup.members.githubAppsSetupMode.create"),
                        },
                        {
                          value: "existing",
                          label: t("setup.members.githubAppsSetupMode.existing"),
                        },
                      ]}
                    />
                  ) : null}
                  {githubAccountType === "github_apps" && githubAppsSetupMode === "create" ? (
                    <GitHubAppRegistrationPanel
                      memberKey={memberFormKey}
                      defaultAppName={personId}
                      defaultOrganization={githubOrganizationDefault}
                      onApplied={(fields) => {
                        if (fields.githubUsername) {
                          setGithubUsername(fields.githubUsername);
                        }
                        if (fields.gitEmail) {
                          setGitEmail(fields.gitEmail);
                        }
                        if (fields.appId) {
                          setGithubAppId(fields.appId);
                        }
                        if (fields.privateKeyPath) {
                          setGithubPrivateKeyPath(fields.privateKeyPath);
                        }
                        if (fields.installationId) {
                          setGithubInstallationId(fields.installationId);
                        }
                        setIdentityResolveError("");
                      }}
                    />
                  ) : null}
                  {!usesGitHubMember ? (
                    <Text size="sm" c="dimmed">
                      {t("setup.members.githubDisabledMemberHint")}
                    </Text>
                  ) : (
                    <>
                      {githubAccountType !== "github_apps" || githubAppsSetupMode === "existing" ? (
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
                      ) : null}
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
                      description={
                        <SecretStatusHint envKey={memberSecretEnvKeys.github_access_token} />
                      }
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
                      <SegmentedControl
                        aria-label={t("setup.members.slackAppsSetupMode.label")}
                        value={slackAppsSetupMode}
                        onChange={(value) =>
                          setSlackAppsSetupMode(value === "existing" ? "existing" : "create")
                        }
                        data={[
                          {
                            value: "create",
                            label: t("setup.members.slackAppsSetupMode.create"),
                          },
                          {
                            value: "existing",
                            label: t("setup.members.slackAppsSetupMode.existing"),
                          },
                        ]}
                      />
                      {slackAppsSetupMode === "create" ? (
                        <SlackAppRegistrationPanel
                          memberKey={memberFormKey}
                          defaultAppName={personId}
                        />
                      ) : null}
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
                        description={
                          <SecretStatusHint envKey={memberSecretEnvKeys.slack_bot_token} />
                        }
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
                        description={
                          <SecretStatusHint envKey={memberSecretEnvKeys.slack_app_token} />
                        }
                      />
                      <SlackTokenVerificationPanel
                        botToken={slackBotToken}
                        appToken={slackAppToken}
                        // Empty fields keep the saved tokens, so verification
                        // needs to know whose saved tokens those are.
                        personId={formMode === "edit" ? (editingPersonId ?? "") : ""}
                        channels={slackChannels}
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
        {defaultPersonMutation.error ? (
          <Alert color="danger" title={t("setup.members.defaultPersonError")}>
            {defaultPersonMutation.error.message}
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
                          extraArgs: "",
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
                          label={argument.name}
                          required={argument.required}
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
                      label={t("commands.extraArgs")}
                      placeholder={t("commands.extraArgsPlaceholder")}
                      value={draft.extraArgs}
                      onChange={(event) =>
                        updateScheduled(draft.id, (current) => ({
                          ...current,
                          extraArgs: event.currentTarget.value,
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
  const githubEnabled = form.values.githubDecision === "enabled";

  // Lane status options are fetched live for the Project URL entered in the
  // Project section (not the saved project) so they appear before saving. The
  // section remounts on navigation, so the target reflects the current URL.
  const laneFetchTarget = buildLaneFetchTarget(form.values);
  const statusOptions = useQuery({
    queryKey: ["projectStatusOptions", laneFetchTarget],
    queryFn: () => getProjectStatusOptions(laneFetchTarget as ProjectStatusOptionsRequest),
    enabled: githubEnabled && laneFetchTarget !== null,
  });
  const laneChoices =
    githubEnabled && laneFetchTarget !== null && statusOptions.data?.available
      ? statusOptions.data.statuses
      : [];

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

  if (laneFetchTarget === null) {
    return (
      <Card withBorder radius="md" p="lg">
        <PanelHeader title={t("setup.github.title")} subtitle={t("setup.github.subtitle")} />
        <Box mt="md">
          <InfoCallout title={t("setup.github.projectUrlMissingTitle")}>
            {t("setup.github.projectUrlMissingHint")}
          </InfoCallout>
        </Box>
      </Card>
    );
  }

  return (
    <Card withBorder radius="md" p="lg">
      <PanelHeader title={t("setup.github.title")} subtitle={t("setup.github.subtitle")} />
      <Stack mt="md">
        <div>
          <Text fw={500} size="sm">
            {t("setup.github.projectUrl")}
          </Text>
          <Text size="sm" c="dimmed">
            {laneFetchTarget.github_project_url}
          </Text>
        </div>
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

// i18next reads these option names itself, so a check context may not shadow
// them when its values are used for interpolation.
const I18N_RESERVED_KEYS = new Set(["count", "context", "ns", "lng", "defaultValue", "replace"]);

/** Scalar context values a check message may interpolate, e.g. a channel name. */
function diagnosticValues(check: DiagnosticCheck): Record<string, string | number> {
  const values: Record<string, string | number> = { target: check.target };
  for (const [key, value] of Object.entries(check.context)) {
    if (!I18N_RESERVED_KEYS.has(key) && (typeof value === "string" || typeof value === "number")) {
      values[key] = value;
    }
  }
  return values;
}

function diagnosticTitle(t: TFunction, check: DiagnosticCheck) {
  const namespace =
    check.status === "ok" ? "overview.diagnosticSuccess" : "overview.diagnosticChecks";
  return t(`${namespace}.${check.code}.title`, {
    defaultValue: check.message,
    ...diagnosticValues(check),
  });
}

function diagnosticDescription(t: TFunction, check: DiagnosticCheck) {
  const namespace =
    check.status === "ok" ? "overview.diagnosticSuccess" : "overview.diagnosticChecks";
  return t(`${namespace}.${check.code}.description`, {
    defaultValue: check.status === "ok" ? "" : check.message,
    ...diagnosticValues(check),
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
  save,
}: {
  title: string;
  subtitle: string;
  badge?: string;
  save?: SectionSave;
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
      {save ? <SectionSaveControl {...save} /> : null}
    </Group>
  );
}

type SectionSave = {
  state: "idle" | "saving" | "saved" | "error";
  saving: boolean;
  onSave: () => void;
};

/**
 * Saving is deliberate here, matching the member editor. Writing on every
 * keystroke gave the user no way to hold a change back, and with another
 * machine editing the same files it also decided, without being asked, that
 * a half-typed value should win over what just arrived.
 */
function SectionSaveControl({ state, saving, onSave }: SectionSave) {
  const { t } = useTranslation();
  return (
    <Group gap="xs" align="center">
      {state === "saved" || state === "error" ? (
        <Badge
          color={state === "error" ? "danger" : "success"}
          variant="light"
          leftSection={<Save size={12} />}
        >
          {t(`setup.save.${state}`)}
        </Badge>
      ) : null}
      <Button loading={saving} onClick={onSave} size="xs">
        {t("setup.save.action")}
      </Button>
    </Group>
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

function useSetupStatus(
  config: ConfigStatus | undefined,
  activeMemberCount: number,
  values: ProjectFormValues,
): SetupStatus {
  const projectReady = Boolean(config?.project_file_exists);
  const githubReady = isGitHubDecisionComplete(values);
  const intelligenceReady = projectReady;
  const membersReady = activeMemberCount > 0;
  const verificationReady = projectReady && githubReady && intelligenceReady && membersReady;
  const done = [projectReady, intelligenceReady, githubReady, membersReady].filter(Boolean).length;
  return {
    projectReady,
    intelligenceReady,
    githubReady,
    membersReady,
    verificationReady,
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
  verificationReady: boolean;
  done: number;
  total: number;
  ready: boolean;
};

type InitialProgress = {
  projectReady: boolean;
  intelligenceReady: boolean;
  githubReady: boolean;
  membersReady: boolean;
  verificationReady: boolean;
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
  if (section === "verification") {
    return status.verificationReady;
  }
  // Hotkeys, synchronization, and this machine's own settings ask for nothing
  // before the project can run, so they are never the step that is still
  // missing -- and never the step that stops the first-run walkthrough.
  return true;
}

function getInitialCoreStatus(
  values: ProjectFormValues,
  activeMemberCount: number,
  selectedCliAgentDetected: boolean,
  storedProviderKeys: Record<string, boolean> | undefined,
): InitialProgress {
  const projectReady =
    values.workspaceDir.trim().length > 0 &&
    values.description.trim().length > 0 &&
    Boolean(values.githubDecision);
  const intelligenceReady =
    Boolean(values.llmApiType) &&
    Boolean(values.cliAgent) &&
    selectedCliAgentDetected &&
    isProviderKeyAvailable(values.llmApiType, values, storedProviderKeys);
  const githubReady = isGitHubDecisionComplete(values);
  const membersReady = activeMemberCount > 0;
  const verificationReady = projectReady && intelligenceReady && githubReady && membersReady;
  const checks = [projectReady, intelligenceReady, githubReady, membersReady];
  const done = checks.filter(Boolean).length;
  const total = checks.length;
  return {
    projectReady,
    intelligenceReady,
    githubReady,
    membersReady,
    verificationReady,
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

// A provider is usable when its API key was typed into the form now, or when it
// is already stored in the saved workspace. Display and readiness share this
// rule so a provider can never look configured while blocking the setup.
function isProviderKeyAvailable(
  provider: string,
  values: ProjectFormValues,
  storedProviderKeys: Record<string, boolean> | undefined,
): boolean {
  return (
    (values.providerApiKeys[provider] ?? "").trim().length > 0 ||
    Boolean(storedProviderKeys?.[provider])
  );
}

function toInitialProjectSetupRequest(values: ProjectFormValues): ProjectSetupRequest {
  return toProjectSetupRequest(values);
}

export function initialProjectValues(
  config: ConfigStatus | undefined,
  appLanguage: ProjectFormValues["language"],
  projectLanguage: ProjectFormValues["language"] | null,
  projectConfig: ProjectConfig | undefined,
): ProjectFormValues {
  if (projectConfig) {
    return {
      workspaceDir: config?.workspace ?? "",
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
  return {
    workspaceDir: config?.workspace ?? "",
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

export function toProjectSetupRequest(values: ProjectFormValues): ProjectSetupRequest {
  const github = values.githubDecision === "enabled" ? parseGitHub(values.githubProjectUrl) : null;
  return {
    config_dir: resolveConfigDir(values.workspaceDir),
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
    // What this form was composed against. Another machine's edit can arrive
    // while the screen sits open, and saving would otherwise replace it with
    // values read before it landed.
    expected_revisions: snapshot.revisions,
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
    // existing OS secret-store value unchanged.
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
  // Team and member scopes send the same full payload. The backend keeps the
  // team file complete and reduces a member's payload to only what differs from
  // the team defaults, so members must send the full editor state (including
  // models, CLI agents, and feature assignments) for that diff to be computed.
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
    // Owner only when the project lives under an organization; a `users/`
    // project has no organization to create a GitHub App under.
    organization: projectValid && projectType === "orgs" ? owner : "",
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
  revisions: ConfigRevisions,
): MemberConfig {
  return {
    // Where the write left this member's files, read before the backend
    // released the lock. The screen stays open, so its next save stands here.
    revisions,
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
    has_github_installation_id: Boolean(request.github_installation_id),
    has_github_app_id: Boolean(request.github_app_id),
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
    extraArgs: parsedCommand.args,
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
    extraArgs: "",
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
  const args = buildSetupCommandArgs(option, draft.argValues, draft.extraArgs);
  return [command, ...args.map(quoteCommandArg)].join(" ");
}

function buildSetupCommandArgs(
  option: CommandOption | null,
  values: Record<string, string>,
  extraArgs: string,
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
  return [...args, ...splitCommandLine(extraArgs)];
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
              {t("overview.scenarioDiagnostics.target")}: {check.target}
            </Text>
          ) : null}
        </Alert>
      ))}
    </Stack>
  );
}

function VerificationSection({
  config,
  projectConfig,
  activeMemberCount,
}: {
  config: ConfigStatus | undefined;
  projectConfig: ProjectConfig | undefined;
  activeMemberCount: number;
}) {
  const { t } = useTranslation();
  const diagnosticsMutation = useMutation({
    mutationFn: () => runScenarioDiagnostics(),
  });

  const hasProjectConfig = Boolean(config?.project_file_exists);
  const githubEnabled = Boolean(projectConfig?.github_enabled);

  return (
    <Card withBorder radius="md" p="lg">
      <Stack gap="md">
        <PanelHeader
          title={t("setup.verification.title")}
          subtitle={t("setup.verification.subtitle")}
        />
        <Group justify="space-between">
          <Text fw={700} size="sm">
            {t("overview.configuration")}
          </Text>
          <Button
            loading={diagnosticsMutation.isPending}
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
          <dt>{t("overview.activeMembers")}</dt>
          <dd>{activeMemberCount}</dd>
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
      </Stack>
    </Card>
  );
}
