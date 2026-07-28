import {
  Alert,
  Badge,
  Button,
  Group,
  Loader,
  Modal,
  SegmentedControl,
  Select,
  Stack,
  Text,
  TextInput,
  Textarea,
  Title,
} from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FilePlus, Save, Trash2 } from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { useTranslation } from "react-i18next";
import { NavLink } from "react-router-dom";

import {
  ApiRequestError,
  authorCommand,
  createCommandFile,
  deleteCommandFile,
  getCommandFile,
  getCommandFileExecutionStatus,
  getConfigStatus,
  getTeam,
  getTraceDetail,
  listCommandFiles,
  runCommand,
  subscribeEvents,
  updateCommandFile,
  type CommandFileFormat,
  type CommandFilesResponse,
  type CommandFileSummary,
  type CommandAuthoringRequest,
} from "../api/client";
import {
  commandFailureDetail,
  commandTraceRefetchInterval,
  openLocalFile,
  upsertCommandRecord,
  type CommandRunRecord,
} from "../App";
import { setNavigationGuard } from "../navigationGuard";
import { AssistantChatPanel } from "../assistant/AssistantChatPanel";
import { useAssistantConversation } from "../assistant/useAssistantConversation";
import { CommandEditor } from "./CommandEditor";
import { CommandHotkeyField } from "./CommandHotkeyField";
import {
  buildFileRunArgs,
  clampEditorRatio,
  deriveSaveStatus,
  EMPTY_EDITOR_STATE,
  loadEditorRatio,
  loadEditorState,
  saveEditorRatio,
  saveEditorState,
} from "./commandEditorState";
import { CommandRunPanel } from "./CommandRunPanel";

const CREATE_FORMATS: CommandFileFormat[] = ["markdown", "python", "shell", "yaml"];
const NEW_DRAFT_FILE_ID = "__new-command-draft__";

type AuthoringTurnRequest = CommandAuthoringRequest & {
  /** Conversation target at submit time, so late replies can be discarded. */
  targetKey: string;
};

type NewCommandDraft = {
  command: string;
  format: CommandFileFormat;
  relativePath: string;
};

function stringPayload(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function newConversationId(): string {
  return globalThis.crypto.randomUUID();
}

function authoringErrorMessage(error: unknown): string | null {
  if (error instanceof Error) {
    return error.message;
  }
  return error ? String(error) : null;
}

export function CommandsPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const config = useQuery({ queryKey: ["config"], queryFn: getConfigStatus });
  const team = useQuery({ queryKey: ["team"], queryFn: getTeam, retry: false });
  const hasProjectConfig = Boolean(config.data?.project_file_exists);
  const storageDir = config.data?.storage_dir;
  const configDir = config.data?.config_dir ?? "";

  const [initial] = useState(() => loadEditorState(storageDir));
  const [selectedFileId, setSelectedFileId] = useState<string | null>(initial.selectedFileId);
  const [person, setPerson] = useState<string | null>(initial.person);
  const [argValues, setArgValues] = useState<Record<string, string>>(initial.argValues);
  const [extraArgs, setExtraArgs] = useState(initial.extraArgs);
  const [message, setMessage] = useState(initial.message);
  const [cwd, setCwd] = useState(initial.cwd);
  const [showAdvanced, setShowAdvanced] = useState(initial.showAdvanced);
  const [history, setHistory] = useState<CommandRunRecord[]>(initial.history);
  const [activeTraceId, setActiveTraceId] = useState<string | null>(initial.activeTraceId);
  const [activeTab, setActiveTab] = useState<string | null>(initial.activeTab ?? "events");

  // `storageDir` arrives asynchronously from the config query, so the initial
  // useState may run before it is known. Re-hydrate from the per-workspace key
  // whenever it resolves or changes, and suppress persistence until then so a
  // half-initialized state is never written to the wrong workspace's key.
  const [hydratedDir, setHydratedDir] = useState<string | undefined>(storageDir);
  useEffect(() => {
    if (!storageDir || storageDir === hydratedDir) {
      return;
    }
    const restored = loadEditorState(storageDir);
    setSelectedFileId(restored.selectedFileId);
    setPerson(restored.person);
    setArgValues(restored.argValues);
    setExtraArgs(restored.extraArgs);
    setMessage(restored.message);
    setCwd(restored.cwd);
    setShowAdvanced(restored.showAdvanced);
    setHistory(restored.history);
    setActiveTraceId(restored.activeTraceId);
    setActiveTab(restored.activeTab ?? "events");
    setHydratedDir(storageDir);
  }, [storageDir, hydratedDir]);

  const [draftContent, setDraftContent] = useState("");
  const [savedContent, setSavedContent] = useState("");
  const [revision, setRevision] = useState("");
  const [conflict, setConflict] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const loadedKeyRef = useRef<string>("");
  const selectedFileIdRef = useRef<string | null>(selectedFileId);
  selectedFileIdRef.current = selectedFileId;
  const [newCommandDraft, setNewCommandDraft] = useState<NewCommandDraft | null>(null);
  const newCommandDraftRef = useRef(newCommandDraft);
  newCommandDraftRef.current = newCommandDraft;

  const [createOpen, setCreateOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [pendingAction, setPendingAction] = useState<(() => void) | null>(null);

  // Draggable editor / verify split. The ratio is the fraction of height given
  // to the editor; it is clamped and persisted as a global layout preference.
  const splitRef = useRef<HTMLDivElement>(null);
  const [editorRatio, setEditorRatio] = useState(() => loadEditorRatio());
  const startSplitDrag = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    let latest = 0;
    const onMove = (moveEvent: PointerEvent) => {
      const rect = splitRef.current?.getBoundingClientRect();
      if (!rect || rect.height === 0) {
        return;
      }
      latest = clampEditorRatio((moveEvent.clientY - rect.top) / rect.height);
      setEditorRatio(latest);
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      if (latest) {
        saveEditorRatio(latest);
      }
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  }, []);
  const nudgeSplit = useCallback((delta: number) => {
    setEditorRatio((current) => {
      const next = clampEditorRatio(current + delta);
      saveEditorRatio(next);
      return next;
    });
  }, []);

  const filesQuery = useQuery({
    queryKey: ["command-files"],
    queryFn: listCommandFiles,
    enabled: hasProjectConfig,
    retry: false,
  });
  const files = useMemo(() => filesQuery.data?.files ?? [], [filesQuery.data?.files]);

  // Keep the selection valid as the file list loads / changes.
  useEffect(() => {
    if (!files.length || newCommandDraft) {
      return;
    }
    if (!selectedFileId || !files.some((file) => file.id === selectedFileId)) {
      setSelectedFileId(files[0].id);
    }
  }, [files, newCommandDraft, selectedFileId]);

  const detailQuery = useQuery({
    queryKey: ["command-file", selectedFileId],
    queryFn: () => getCommandFile(selectedFileId as string),
    enabled: Boolean(selectedFileId),
    retry: false,
  });
  const detail = detailQuery.data ?? null;

  // Load saved content into the editor whenever a new file/revision arrives.
  useEffect(() => {
    if (!detail) {
      return;
    }
    const key = `${detail.id}:${detail.revision}`;
    if (loadedKeyRef.current === key) {
      return;
    }
    loadedKeyRef.current = key;
    setSavedContent(detail.content);
    setDraftContent(detail.content);
    setRevision(detail.revision);
    setConflict(false);
    setSaveError(null);
  }, [detail]);

  const dirty = Boolean(newCommandDraft) || draftContent !== savedContent;

  const cacheCreatedCommand = useCallback(
    (created: Awaited<ReturnType<typeof createCommandFile>>) => {
      queryClient.setQueryData(["command-file", created.id], created);
      queryClient.setQueryData<CommandFilesResponse>(["command-files"], (previous) => {
        const files = previous?.files ?? [];
        if (files.some((file) => file.id === created.id)) {
          return { files };
        }
        const summary: CommandFileSummary = {
          id: created.id,
          command: created.command,
          label: created.label,
          description: created.description,
          relative_path: created.relative_path,
          format: created.format,
        };
        return {
          files: [...files, summary].sort((a, b) => a.command.localeCompare(b.command)),
        };
      });
      void queryClient.invalidateQueries({ queryKey: ["command-files"] });
      void queryClient.invalidateQueries({ queryKey: ["command-options"] });
      void queryClient.invalidateQueries({ queryKey: ["routine-command-options"] });
    },
    [queryClient],
  );

  const activeMembers = useMemo(
    () => (team.data?.members ?? []).filter((member) => member.is_active),
    [team.data?.members],
  );
  const selectedPerson =
    activeMembers.find((member) => member.person_id === person)?.person_id ?? null;
  // The backend owns the fallback rule; the team summary reports the member an
  // omitted person resolves to, so no default is picked here.
  const defaultPerson =
    activeMembers.find((member) => member.person_id === team.data?.default_person_id) ?? null;
  const effectivePerson = selectedPerson ?? defaultPerson?.person_id ?? null;

  // One authoring conversation per member and command draft. Saving a new
  // draft renames the target, so the conversation is carried over explicitly
  // rather than restarted.
  const authoringTargetKey = `${effectivePerson ?? ""}:${
    newCommandDraft ? NEW_DRAFT_FILE_ID : (selectedFileId ?? "")
  }`;
  const authoring = useAssistantConversation(authoringTargetKey);

  const saveMutation = useMutation({
    mutationFn: () =>
      newCommandDraft
        ? createCommandFile({
            command: newCommandDraft.command,
            format: newCommandDraft.format,
            content: draftContent,
          })
        : updateCommandFile(selectedFileId as string, {
            content: draftContent,
            expected_revision: revision,
          }),
    onMutate: () => {
      setSaveError(null);
      setConflict(false);
    },
    onSuccess: (updated) => {
      if (newCommandDraft) {
        // The draft keeps its conversation once it becomes a real file.
        authoring.adopt(
          authoring.conversationId,
          authoring.messages,
          `${effectivePerson ?? ""}:${updated.id}`,
        );
        cacheCreatedCommand(updated);
        setNewCommandDraft(null);
        setSelectedFileId(updated.id);
      }
      loadedKeyRef.current = `${updated.id}:${updated.revision}`;
      setSavedContent(updated.content);
      setRevision(updated.revision);
      queryClient.setQueryData(["command-file", updated.id], updated);
      void queryClient.invalidateQueries({ queryKey: ["command-files"] });
      void queryClient.invalidateQueries({ queryKey: ["command-options"] });
      void queryClient.invalidateQueries({ queryKey: ["routine-command-options"] });
    },
    onError: (error) => {
      if (error instanceof ApiRequestError && error.code === "command_file_changed") {
        setConflict(true);
      } else if (
        newCommandDraft &&
        error instanceof ApiRequestError &&
        error.code === "command_file_exists"
      ) {
        setSaveError(`${error.message} ${t("commands.aiNameCollisionHelp")}`);
      } else {
        setSaveError(error instanceof Error ? error.message : String(error));
      }
    },
  });

  const createMutation = useMutation({
    mutationFn: (body: { command: string; format: CommandFileFormat }) => createCommandFile(body),
    onSuccess: (created) => {
      cacheCreatedCommand(created);
      setSelectedFileId(created.id);
      setCreateOpen(false);
    },
  });

  const deleteMutation = useMutation({
    // The loaded revision goes with the request so a file edited elsewhere
    // since it was opened is refused instead of silently discarded.
    mutationFn: (target: { fileId: string; revision: string }) =>
      deleteCommandFile(target.fileId, target.revision),
    onSuccess: (remaining, { fileId }) => {
      queryClient.removeQueries({ queryKey: ["command-file", fileId] });
      queryClient.setQueryData<CommandFilesResponse>(["command-files"], remaining);
      void queryClient.invalidateQueries({ queryKey: ["command-files"] });
      void queryClient.invalidateQueries({ queryKey: ["command-options"] });
      void queryClient.invalidateQueries({ queryKey: ["routine-command-options"] });
      // Dropping the buffer first keeps the unsaved-changes guard from asking
      // about a file that no longer exists.
      setDraftContent("");
      setSavedContent("");
      loadedKeyRef.current = "";
      setSelectedFileId(remaining.files[0]?.id ?? null);
      setDeleteOpen(false);
    },
  });

  const aiCreateMutation = useMutation({
    mutationFn: (request: { conversationId: string; message: string }) =>
      authorCommand({
        mode: "create",
        conversation_id: request.conversationId,
        message: request.message,
        person: effectivePerson ?? undefined,
      }),
    onSuccess: (response, request) => {
      authoring.adopt(
        request.conversationId,
        [
          { role: "user", content: request.message },
          {
            role: "assistant",
            content: response.message,
            traceId: response.trace_id,
          },
        ],
        `${effectivePerson ?? ""}:${NEW_DRAFT_FILE_ID}`,
      );
      setNewCommandDraft({
        command: response.command,
        format: response.format,
        relativePath: response.relative_path,
      });
      setSelectedFileId(null);
      loadedKeyRef.current = "";
      setDraftContent(response.content);
      setSavedContent("");
      setRevision("");
      setConflict(false);
      setSaveError(null);
      setCreateOpen(false);
    },
  });

  const authoringMutation = useMutation({
    mutationFn: (request: AuthoringTurnRequest) =>
      authorCommand({
        mode: request.mode,
        conversation_id: request.conversation_id,
        command: request.command,
        format: request.format,
        content: request.content,
        message: request.message,
        person: request.person,
      }),
    onMutate: (request) => {
      authoring.appendUser(request.conversation_id, request.targetKey, request.message);
    },
    onSuccess: (response, request) => {
      if (!authoring.isCurrent(request.conversation_id, request.targetKey)) {
        return;
      }
      if (request.mode === "create") {
        setNewCommandDraft({
          command: response.command,
          format: response.format,
          relativePath: response.relative_path,
        });
      }
      setDraftContent(response.content);
      authoring.appendAssistant(request.conversation_id, request.targetKey, {
        content: response.message,
        traceId: response.trace_id,
      });
    },
  });

  const executionStatusQuery = useQuery({
    queryKey: ["command-file-execution", selectedFileId, effectivePerson, revision],
    queryFn: () =>
      getCommandFileExecutionStatus(selectedFileId as string, {
        person: effectivePerson ?? undefined,
        expected_revision: revision,
      }),
    enabled: Boolean(selectedFileId && effectivePerson && revision) && !dirty,
    retry: false,
  });

  const [runBusy, setRunBusy] = useState(false);
  const saveAndRun = useCallback(async () => {
    if (!selectedFileId || !effectivePerson || !detail) {
      return;
    }
    setRunBusy(true);
    try {
      // Build the run payload from the just-saved file so edited frontmatter
      // (args / inputs / command) is honored, not the pre-save definition.
      const runFile = dirty ? await saveMutation.mutateAsync() : detail;
      setActiveTraceId(null);
      setActiveTab("events");
      const args = buildFileRunArgs(runFile, argValues, extraArgs);
      const response = await runCommand({
        command: runFile.command,
        args,
        person: selectedPerson ?? undefined,
        message: runFile.inputs.message !== "hidden" ? message : "",
        cwd: cwd.trim() || undefined,
        expected_command_file_id: runFile.id,
        expected_command_file_revision: runFile.revision,
      });
      setActiveTraceId(response.trace_id);
      setActiveTab("output");
      setHistory((current) =>
        upsertCommandRecord(current, {
          traceId: response.trace_id,
          person: effectivePerson,
          command: runFile.command,
          startedAt: new Date().toISOString(),
          status: "success",
          output: response.output,
        }),
      );
    } catch (error) {
      const traceId = `local-${Date.now()}`;
      setActiveTraceId(traceId);
      setActiveTab("output");
      setHistory((current) =>
        upsertCommandRecord(current, {
          traceId,
          person: effectivePerson,
          command: detail.command,
          startedAt: new Date().toISOString(),
          status: "failed",
          error: error instanceof Error ? error.message : String(error),
        }),
      );
    } finally {
      setRunBusy(false);
    }
  }, [
    argValues,
    cwd,
    detail,
    dirty,
    effectivePerson,
    extraArgs,
    message,
    saveMutation,
    selectedFileId,
    selectedPerson,
  ]);

  // Command lifecycle events keep the run history in sync with the service.
  useEffect(() => {
    return subscribeEvents((event) => {
      if (!event.type.startsWith("command.") || !event.trace_id) {
        return;
      }
      const base = {
        traceId: event.trace_id,
        person: stringPayload(event.payload.person),
        command: stringPayload(event.payload.command),
        startedAt: event.timestamp,
      };
      if (event.type === "command.started") {
        setActiveTraceId(event.trace_id);
        setHistory((current) => upsertCommandRecord(current, { ...base, status: "running" }));
      } else if (event.type === "command.failed") {
        setHistory((current) =>
          upsertCommandRecord(current, {
            ...base,
            status: "failed",
            error: commandFailureDetail(event),
          }),
        );
      } else if (event.type === "command.finished") {
        setHistory((current) => upsertCommandRecord(current, { ...base, status: "success" }));
      }
    });
  }, []);

  const selectedRecord = useMemo(
    () => history.find((record) => record.traceId === activeTraceId) ?? history[0] ?? null,
    [activeTraceId, history],
  );
  const visibleTraceId = selectedRecord?.traceId ?? activeTraceId;
  const traceDetail = useQuery({
    queryKey: ["diagnostics-trace", visibleTraceId],
    queryFn: () => getTraceDetail(visibleTraceId as string),
    enabled: Boolean(visibleTraceId) && !visibleTraceId?.startsWith("local-"),
    refetchInterval: commandTraceRefetchInterval(selectedRecord?.status),
  });
  const traceRecords = useMemo(
    () => [...(traceDetail.data?.records ?? [])].reverse(),
    [traceDetail.data?.records],
  );

  // Persist the editor UI state (never the draft source) per workspace.
  const persistRef = useRef(EMPTY_EDITOR_STATE);
  persistRef.current = {
    selectedFileId,
    person,
    argValues,
    extraArgs,
    message,
    cwd,
    showAdvanced,
    history,
    activeTraceId,
    activeTab,
  };
  useEffect(() => {
    // Only persist once the state has been hydrated for this workspace, so the
    // window between a workspace switch and its re-hydration cannot overwrite
    // the new workspace's saved state with the previous one.
    if (!storageDir || storageDir !== hydratedDir) {
      return;
    }
    const handle = window.setTimeout(() => saveEditorState(persistRef.current, storageDir), 400);
    return () => window.clearTimeout(handle);
  }, [
    selectedFileId,
    person,
    argValues,
    extraArgs,
    message,
    cwd,
    showAdvanced,
    history,
    activeTraceId,
    activeTab,
    storageDir,
    hydratedDir,
  ]);

  // Warn before the window/tab closes with unsaved edits.
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;
  useEffect(() => {
    const handler = (event: BeforeUnloadEvent) => {
      if (dirtyRef.current) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, []);

  // In-app navigation (e.g. sidebar) routes through the app-wide guard so a
  // dirty buffer prompts save/discard/cancel instead of being lost silently.
  useEffect(() => {
    setNavigationGuard({
      shouldBlock: () => dirtyRef.current,
      confirm: (proceed) => setPendingAction(() => proceed),
    });
    return () => setNavigationGuard(null);
  }, []);

  // Guard actions that would discard unsaved edits behind a confirm dialog.
  const guard = useCallback(
    (action: () => void) => {
      if (dirty) {
        setPendingAction(() => action);
      } else {
        action();
      }
    },
    [dirty],
  );

  const selectFile = (fileId: string | null) => {
    if (fileId === NEW_DRAFT_FILE_ID) {
      return;
    }
    guard(() => {
      if (newCommandDraft) {
        setNewCommandDraft(null);
        loadedKeyRef.current = "";
      }
      setSelectedFileId(fileId);
    });
  };
  const openCreate = () => {
    guard(() => {
      if (newCommandDraft) {
        setNewCommandDraft(null);
        loadedKeyRef.current = "";
        setSelectedFileId(files[0]?.id ?? null);
      }
      createMutation.reset();
      aiCreateMutation.reset();
      setCreateOpen(true);
    });
  };

  const activeDraft = newCommandDraft ?? detail;
  const displayPath = newCommandDraft
    ? `${configDir}/commands/${newCommandDraft.relativePath}`
    : detail
      ? `${configDir}/commands/${detail.relative_path}`
      : "";
  const saveStatus = deriveSaveStatus(draftContent, savedContent, saveMutation.isPending, conflict);

  if (!hasProjectConfig) {
    return (
      <Stack gap="lg">
        <Title order={2}>{t("commands.title")}</Title>
        <Alert color="warning" title={t("overview.setupRequiredTitle")}>
          <Group justify="space-between" align="center">
            <Text size="sm">{t("overview.setupRequiredBody")}</Text>
            <Button component={NavLink} to="/setup" variant="light">
              {t("overview.openSetup")}
            </Button>
          </Group>
        </Alert>
      </Stack>
    );
  }

  const isEmpty = !newCommandDraft && !filesQuery.isLoading && files.length === 0;

  return (
    <Stack gap="lg" className="command-editor-page">
      <Group justify="space-between" wrap="wrap">
        <Title order={2}>{t("commands.title")}</Title>
        <Group gap="sm">
          <Button
            variant="default"
            leftSection={<FilePlus size={16} />}
            disabled={authoringMutation.isPending}
            onClick={openCreate}
          >
            {t("commands.newFile")}
          </Button>
          <Button
            variant="default"
            color="danger"
            leftSection={<Trash2 size={16} />}
            disabled={!selectedFileId || Boolean(newCommandDraft) || authoringMutation.isPending}
            onClick={() => {
              deleteMutation.reset();
              setDeleteOpen(true);
            }}
          >
            {t("commands.deleteFile")}
          </Button>
          <Button
            leftSection={<Save size={16} />}
            loading={saveMutation.isPending}
            disabled={!activeDraft || !dirty || authoringMutation.isPending}
            onClick={() => saveMutation.mutate()}
          >
            {t("commands.save")}
          </Button>
        </Group>
      </Group>

      {isEmpty ? (
        <Alert color="neutral" title={t("commands.emptyTitle")}>
          <Group justify="space-between" align="center">
            <Text size="sm">{t("commands.emptyBody")}</Text>
            <Button leftSection={<FilePlus size={16} />} onClick={openCreate}>
              {t("commands.newFile")}
            </Button>
          </Group>
        </Alert>
      ) : (
        <>
          <Select
            aria-label={t("commands.editSelectLabel")}
            label={t("commands.editSelectLabel")}
            searchable
            nothingFoundMessage={t("commands.noCommandOptions")}
            value={newCommandDraft ? NEW_DRAFT_FILE_ID : selectedFileId}
            disabled={authoringMutation.isPending}
            onChange={(value) => selectFile(value)}
            data={[
              ...(newCommandDraft
                ? [
                    {
                      value: NEW_DRAFT_FILE_ID,
                      label: t("commands.newDraftOption", {
                        command: newCommandDraft.command,
                        format: t(`commands.formats.${newCommandDraft.format}`),
                      }),
                    },
                  ]
                : []),
              ...files.map((file) => ({
                value: file.id,
                label: `${file.label} (${file.command})`,
              })),
            ]}
          />

          {newCommandDraft ? (
            <Group gap="xs">
              <Badge variant="light">{t(`commands.formats.${newCommandDraft.format}`)}</Badge>
              <Text c="dimmed" size="sm">
                {t("commands.proposedByAi")}
              </Text>
            </Group>
          ) : (
            <CommandHotkeyField command={detail?.command ?? null} />
          )}

          <Group gap="xs">
            <Badge variant="light" color={saveStatus === "clean" ? "success" : "warning"}>
              {t(`commands.saveState.${saveStatus}`)}
            </Badge>
          </Group>

          {saveError ? (
            <Alert color="warning" title={t("commands.saveErrorTitle")}>
              {saveError}
            </Alert>
          ) : null}

          {conflict ? (
            <Alert color="warning" title={t("commands.conflictTitle")}>
              <Stack gap="xs">
                <Text size="sm">{t("commands.conflictBody")}</Text>
                <Group gap="xs">
                  <Button
                    size="xs"
                    variant="light"
                    onClick={() => {
                      loadedKeyRef.current = "";
                      setConflict(false);
                      void detailQuery.refetch();
                    }}
                  >
                    {t("commands.conflictReload")}
                  </Button>
                  <Button size="xs" variant="subtle" onClick={() => setConflict(false)}>
                    {t("commands.conflictKeep")}
                  </Button>
                </Group>
              </Stack>
            </Alert>
          ) : null}

          <div className="command-split" ref={splitRef}>
            <div className="command-split-editor" style={{ flexGrow: editorRatio }}>
              {activeDraft ? (
                <div className="command-editor-with-assistant">
                  <CommandEditor
                    value={draftContent}
                    format={activeDraft.format}
                    path={displayPath}
                    disabled={authoringMutation.isPending}
                    onChange={setDraftContent}
                    onSave={() => {
                      if (activeDraft && dirty && !authoringMutation.isPending) {
                        saveMutation.mutate();
                      }
                    }}
                    onOpenExternal={
                      newCommandDraft
                        ? undefined
                        : () => void openLocalFile(displayPath).catch(() => {})
                    }
                  />
                  <AssistantChatPanel
                    namespace="commands.authoring"
                    key={authoring.conversationId}
                    messages={authoring.messages}
                    pending={authoringMutation.isPending}
                    disabled={!effectivePerson}
                    autoScrollOnAssistantResponse
                    // A failure belongs to the command that produced it:
                    // switching commands must not carry it into the next
                    // conversation.
                    error={
                      authoringMutation.variables?.targetKey === authoringTargetKey
                        ? authoringErrorMessage(authoringMutation.error)
                        : null
                    }
                    onSubmit={(authoringMessage) =>
                      authoringMutation.mutate({
                        mode: newCommandDraft ? "create" : "edit",
                        conversation_id: authoring.conversationId,
                        command: activeDraft.command,
                        format: activeDraft.format,
                        content: draftContent,
                        message: authoringMessage,
                        person: effectivePerson ?? undefined,
                        targetKey: authoringTargetKey,
                      })
                    }
                  />
                </div>
              ) : (
                <div className="empty-row">
                  <Loader size="sm" />
                </div>
              )}
            </div>

            {!newCommandDraft && detail ? (
              <>
                <div
                  className="command-split-handle"
                  role="separator"
                  aria-orientation="horizontal"
                  aria-label={t("commands.resizeEditor")}
                  tabIndex={0}
                  onPointerDown={startSplitDrag}
                  onKeyDown={(event) => {
                    if (event.key === "ArrowUp") {
                      event.preventDefault();
                      nudgeSplit(-0.05);
                    } else if (event.key === "ArrowDown") {
                      event.preventDefault();
                      nudgeSplit(0.05);
                    }
                  }}
                />
                <div className="command-split-verify" style={{ flexGrow: 1 - editorRatio }}>
                  <CommandRunPanel
                    file={detail}
                    members={activeMembers}
                    person={selectedPerson}
                    defaultPerson={defaultPerson}
                    onPersonChange={setPerson}
                    argValues={argValues}
                    onArgValueChange={(name, value) =>
                      setArgValues((current) => ({ ...current, [name]: value }))
                    }
                    extraArgs={extraArgs}
                    onExtraArgsChange={setExtraArgs}
                    message={message}
                    onMessageChange={setMessage}
                    cwd={cwd}
                    onCwdChange={setCwd}
                    workspaceCwd={config.data?.cwd ?? ""}
                    showAdvanced={showAdvanced}
                    onToggleAdvanced={setShowAdvanced}
                    executionStatus={dirty ? null : (executionStatusQuery.data ?? null)}
                    runBusy={runBusy || authoringMutation.isPending}
                    onSaveAndRun={() => void saveAndRun()}
                    selectedRecord={selectedRecord}
                    traceRecords={traceRecords}
                    traceLoading={traceDetail.isFetching && !traceDetail.data}
                    transcriptAvailable={traceDetail.data?.transcript_available}
                    activeTab={activeTab}
                    onTabChange={setActiveTab}
                  />
                </div>
              </>
            ) : null}
          </div>
        </>
      )}

      <CreateCommandDialog
        key={createOpen ? "create-open" : "create-closed"}
        opened={createOpen}
        pending={createMutation.isPending || aiCreateMutation.isPending}
        manualError={createMutation.error}
        aiError={aiCreateMutation.error}
        aiAvailable={Boolean(effectivePerson)}
        onClose={() => {
          createMutation.reset();
          aiCreateMutation.reset();
          setCreateOpen(false);
        }}
        onCreateManually={(command, format) => createMutation.mutate({ command, format })}
        onCreateWithAi={(authoringMessage) =>
          aiCreateMutation.mutate({
            conversationId: newConversationId(),
            message: authoringMessage,
          })
        }
      />

      <Modal
        opened={deleteOpen}
        onClose={() => setDeleteOpen(false)}
        title={t("commands.deleteConfirmTitle")}
        centered
      >
        <Stack>
          <Text size="sm">
            {t("commands.deleteConfirmBody", { command: detail?.command ?? "" })}
          </Text>
          {deleteMutation.error ? (
            <Alert color="danger" title={t("commands.deleteErrorTitle")}>
              {deleteMutation.error instanceof ApiRequestError &&
              deleteMutation.error.code === "command_file_changed"
                ? t("commands.deleteConflictBody")
                : deleteMutation.error.message}
            </Alert>
          ) : null}
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setDeleteOpen(false)}>
              {t("commands.deleteCancel")}
            </Button>
            <Button
              color="danger"
              loading={deleteMutation.isPending}
              disabled={!selectedFileId || !revision}
              onClick={() => deleteMutation.mutate({ fileId: selectedFileId as string, revision })}
            >
              {t("commands.deleteFile")}
            </Button>
          </Group>
        </Stack>
      </Modal>

      <Modal
        opened={pendingAction != null}
        onClose={() => setPendingAction(null)}
        title={t("commands.unsavedTitle")}
      >
        <Stack>
          <Text size="sm">{t("commands.unsavedBody")}</Text>
          <Group justify="flex-end">
            <Button variant="subtle" onClick={() => setPendingAction(null)}>
              {t("commands.unsavedCancel")}
            </Button>
            <Button
              variant="default"
              onClick={() => {
                setDraftContent(savedContent);
                const action = pendingAction;
                setPendingAction(null);
                action?.();
              }}
            >
              {t("commands.unsavedDiscard")}
            </Button>
            <Button
              onClick={async () => {
                const action = pendingAction;
                setPendingAction(null);
                try {
                  await saveMutation.mutateAsync();
                  action?.();
                } catch {
                  // Save failed (conflict/validation): keep the buffer, stay put.
                }
              }}
            >
              {t("commands.unsavedSave")}
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Stack>
  );
}

function CreateCommandDialog({
  opened,
  pending,
  manualError,
  aiError,
  aiAvailable,
  onClose,
  onCreateManually,
  onCreateWithAi,
}: {
  opened: boolean;
  pending: boolean;
  manualError: unknown;
  aiError: unknown;
  aiAvailable: boolean;
  onClose: () => void;
  onCreateManually: (command: string, format: CommandFileFormat) => void;
  onCreateWithAi: (message: string) => void;
}) {
  const { t } = useTranslation();
  const [method, setMethod] = useState<"ai" | "manual">("ai");
  const [name, setName] = useState("");
  const [format, setFormat] = useState<CommandFileFormat>("markdown");
  const [request, setRequest] = useState("");
  const error = method === "ai" ? aiError : manualError;

  const errorMessage =
    error instanceof ApiRequestError
      ? (t(`commands.errors.${error.code}`, { defaultValue: error.message }) as string)
      : error instanceof Error
        ? error.message
        : null;

  return (
    <Modal
      opened={opened}
      onClose={() => {
        if (!pending) {
          onClose();
        }
      }}
      closeButtonProps={{ disabled: pending }}
      closeOnClickOutside={!pending}
      closeOnEscape={!pending}
      title={t("commands.createTitle")}
    >
      <Stack>
        <SegmentedControl
          aria-label={t("commands.createMethodLabel")}
          disabled={pending}
          fullWidth
          value={method}
          onChange={(value) => setMethod(value as "ai" | "manual")}
          data={[
            { value: "ai", label: t("commands.createMethodAi") },
            { value: "manual", label: t("commands.createMethodManual") },
          ]}
        />
        {method === "ai" ? (
          <Textarea
            autosize
            disabled={pending}
            minRows={4}
            maxRows={10}
            label={t("commands.createAiRequestLabel")}
            description={t("commands.createAiRequestHelp")}
            placeholder={t("commands.createAiRequestPlaceholder")}
            value={request}
            onChange={(event) => setRequest(event.currentTarget.value)}
          />
        ) : (
          <>
            <TextInput
              label={t("commands.createNameLabel")}
              disabled={pending}
              description={t("commands.createNameHelp")}
              value={name}
              onChange={(event) => setName(event.currentTarget.value)}
            />
            <Select
              label={t("commands.createFormatLabel")}
              disabled={pending}
              value={format}
              onChange={(value) => value && setFormat(value as CommandFileFormat)}
              data={CREATE_FORMATS.map((value) => ({
                value,
                label: t(`commands.formats.${value}`),
              }))}
            />
          </>
        )}
        {method === "ai" && !aiAvailable ? (
          <Alert color="warning">{t("commands.noMembersBody")}</Alert>
        ) : null}
        {errorMessage ? (
          <Alert color="warning" title={t("commands.createErrorTitle")}>
            {errorMessage}
          </Alert>
        ) : null}
        <Group justify="flex-end">
          <Button variant="subtle" disabled={pending} onClick={onClose}>
            {t("commands.createCancel")}
          </Button>
          <Button
            loading={pending}
            disabled={method === "ai" ? !aiAvailable || !request.trim() : !name.trim()}
            onClick={() =>
              method === "ai"
                ? onCreateWithAi(request.trim())
                : onCreateManually(name.trim(), format)
            }
          >
            {t(method === "ai" ? "commands.createAiSubmit" : "commands.createSubmit")}
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
