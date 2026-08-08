import {
  Alert,
  Button,
  Drawer,
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
import { FilePlus, Save, Sparkles, Trash2 } from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { useTranslation } from "react-i18next";
import { NavLink } from "react-router";

import {
  ApiRequestError,
  applyCommandAuthoring,
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
  type CommandAuthoringResponse,
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
import { MemberSelector } from "../MemberSelector";
import { CommandAuthoringWorkspace } from "./CommandAuthoringWorkspace";
import { CommandBar } from "./CommandBar";
import { CommandEditor } from "./CommandEditor";
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

type AuthoringTurnRequest = CommandAuthoringRequest & {
  /** Conversation target at submit time, so late replies can be discarded. */
  targetKey: string;
};

type PendingAuthoringProposal = {
  response: CommandAuthoringResponse;
  conversationId: string;
  targetKey: string;
  userMessage: string;
  source: "create" | "editor";
  baseContent?: string;
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

function authoringResponseMessage(response: CommandAuthoringResponse, proposalReady: string) {
  return response.action === "propose_changes"
    ? `${proposalReady}\n\n${response.message}`
    : response.message;
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
  const [authoringPersonId, setAuthoringPersonId] = useState<string | null>(null);
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
    setAuthoringPersonId(null);
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
  const [pendingProposal, setPendingProposal] = useState<PendingAuthoringProposal | null>(null);

  const [createOpen, setCreateOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [assistantOpen, setAssistantOpen] = useState(false);
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
    if (!files.length) {
      return;
    }
    if (!selectedFileId || !files.some((file) => file.id === selectedFileId)) {
      setSelectedFileId(files[0].id);
    }
  }, [files, selectedFileId]);

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

  const dirty = draftContent !== savedContent;

  const cacheCommandFile = useCallback(
    (file: Awaited<ReturnType<typeof createCommandFile>>) => {
      queryClient.setQueryData(["command-file", file.id], file);
      queryClient.setQueryData<CommandFilesResponse>(["command-files"], (previous) => {
        const files = previous?.files ?? [];
        if (files.some((current) => current.id === file.id)) {
          return { files };
        }
        const summary: CommandFileSummary = {
          id: file.id,
          command: file.command,
          label: file.label,
          description: file.description,
          relative_path: file.relative_path,
          format: file.format,
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
  const selectedMember = activeMembers.find((member) => member.person_id === person) ?? null;
  const selectedPerson = selectedMember?.person_id ?? null;
  // The backend owns the fallback rule; the team summary reports the member an
  // omitted person resolves to, so no default is picked here.
  const defaultPerson =
    activeMembers.find((member) => member.person_id === team.data?.default_person_id) ?? null;
  const runMember = selectedMember ?? defaultPerson;
  const runPerson = runMember?.person_id ?? null;
  const authoringMember =
    activeMembers.find((member) => member.person_id === authoringPersonId) ?? defaultPerson;
  const authoringPerson = authoringMember?.person_id ?? null;

  const authoringTargetKey = [authoringPerson ?? "", selectedFileId ?? ""].join(":");
  const authoring = useAssistantConversation(authoringTargetKey);

  const saveMutation = useMutation({
    mutationFn: () =>
      updateCommandFile(selectedFileId as string, {
        content: draftContent,
        expected_revision: revision,
      }),
    onMutate: () => {
      setSaveError(null);
      setConflict(false);
    },
    onSuccess: (updated) => {
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
      } else {
        setSaveError(error instanceof Error ? error.message : String(error));
      }
    },
  });

  const createMutation = useMutation({
    mutationFn: (body: { command: string; format: CommandFileFormat }) => createCommandFile(body),
    onSuccess: (created) => {
      cacheCommandFile(created);
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
        person: authoringPerson ?? undefined,
      }),
    onSuccess: (response, request) => {
      setPendingProposal({
        response,
        conversationId: request.conversationId,
        targetKey: [authoringPerson ?? "", "new-command"].join(":"),
        userMessage: request.message,
        source: "create",
      });
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
        file_id: request.file_id,
        revision: request.revision,
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
      if (response.action === "propose_changes") {
        setPendingProposal({
          response,
          conversationId: request.conversation_id,
          targetKey: request.targetKey,
          userMessage: request.message,
          source: "editor",
          baseContent: request.content,
        });
      }
      authoring.appendAssistant(request.conversation_id, request.targetKey, {
        content: authoringResponseMessage(
          response,
          t("commands.authoringProposal.ready", { count: response.changes.length }),
        ),
        traceId: response.trace_id,
      });
    },
  });

  const applyAuthoringMutation = useMutation({
    mutationFn: (proposal: PendingAuthoringProposal) =>
      applyCommandAuthoring(proposal.response.changes),
    onSuccess: (result, proposal) => {
      for (const file of result.files) {
        cacheCommandFile(file);
      }
      const primaryIndex =
        proposal.source === "editor"
          ? proposal.response.changes.findIndex((change) => change.operation === "update")
          : 0;
      const primary = primaryIndex < 0 ? undefined : result.files[primaryIndex];
      if (primary) {
        queryClient.setQueryData(["command-file", primary.id], primary);
        loadedKeyRef.current = `${primary.id}:${primary.revision}`;
        setSelectedFileId(primary.id);
        setSavedContent(primary.content);
        setDraftContent(primary.content);
        setRevision(primary.revision);
        setConflict(false);
        setSaveError(null);
        if (proposal.source === "create") {
          authoring.adopt(
            proposal.conversationId,
            [
              { role: "user", content: proposal.userMessage },
              {
                role: "assistant",
                content: authoringResponseMessage(
                  proposal.response,
                  t("commands.authoringProposal.ready", {
                    count: proposal.response.changes.length,
                  }),
                ),
                traceId: proposal.response.trace_id,
              },
            ],
            [authoringPerson ?? "", primary.id].join(":"),
          );
        }
      }
      setPendingProposal(null);
      setCreateOpen(false);
    },
  });

  const executionStatusQuery = useQuery({
    queryKey: ["command-file-execution", selectedFileId, runPerson, revision],
    queryFn: () =>
      getCommandFileExecutionStatus(selectedFileId as string, {
        person: runPerson ?? undefined,
        expected_revision: revision,
      }),
    enabled: Boolean(selectedFileId && runPerson && revision) && !dirty,
    retry: false,
  });

  const [runBusy, setRunBusy] = useState(false);
  const saveAndRun = useCallback(async () => {
    if (!selectedFileId || !runPerson || !detail) {
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
          person: runPerson,
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
          person: runPerson,
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
    runPerson,
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
    guard(() => {
      applyAuthoringMutation.reset();
      setPendingProposal(null);
      setSelectedFileId(fileId);
    });
  };
  const openCreate = () => {
    guard(() => {
      createMutation.reset();
      aiCreateMutation.reset();
      applyAuthoringMutation.reset();
      setPendingProposal(null);
      setCreateOpen(true);
    });
  };

  const commandsRoot = `${configDir}/commands`;
  const displayPath = detail ? `${commandsRoot}/${detail.relative_path}` : "";
  const editorProposal =
    pendingProposal?.source === "editor" && pendingProposal.targetKey === authoringTargetKey
      ? pendingProposal
      : null;
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

  const isEmpty = !filesQuery.isLoading && files.length === 0;

  return (
    <Stack gap="lg" className="command-editor-page workspace-fill">
      <Group justify="space-between" wrap="wrap">
        <Title order={2}>{t("commands.title")}</Title>
        <Group gap="sm">
          <Button
            variant="default"
            leftSection={<Sparkles size={15} />}
            onClick={() => setAssistantOpen(true)}
          >
            {t("commands.authoring.open")}
          </Button>
          <Button
            variant="default"
            leftSection={<FilePlus size={16} />}
            disabled={authoringMutation.isPending || applyAuthoringMutation.isPending}
            onClick={openCreate}
          >
            {t("commands.newFile")}
          </Button>
          <Button
            variant="default"
            color="danger"
            leftSection={<Trash2 size={16} />}
            disabled={
              !selectedFileId || authoringMutation.isPending || applyAuthoringMutation.isPending
            }
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
            disabled={
              !detail || !dirty || authoringMutation.isPending || applyAuthoringMutation.isPending
            }
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
              <CommandBar
                files={files}
                selectedFileId={selectedFileId}
                onSelectFile={selectFile}
                command={detail?.command ?? null}
                saveStatus={saveStatus}
                path={displayPath}
                pathLabel={detail ? `commands/${detail.relative_path}` : ""}
                disabled={authoringMutation.isPending || applyAuthoringMutation.isPending}
                onOpenExternal={
                  displayPath ? () => void openLocalFile(displayPath).catch(() => {}) : undefined
                }
              />
              {detail ? (
                editorProposal ? (
                  <CommandAuthoringWorkspace
                    changes={editorProposal.response.changes}
                    commandsRoot={commandsRoot}
                    current={{
                      relativePath: detail.relative_path,
                      path: displayPath,
                      content: editorProposal.baseContent ?? draftContent,
                    }}
                    pending={applyAuthoringMutation.isPending}
                    error={
                      applyAuthoringMutation.variables?.source === "editor"
                        ? applyAuthoringMutation.error
                        : null
                    }
                    onApply={() => applyAuthoringMutation.mutate(editorProposal)}
                    onDiscard={() => {
                      applyAuthoringMutation.reset();
                      setPendingProposal(null);
                    }}
                  />
                ) : (
                  <CommandEditor
                    value={draftContent}
                    format={detail.format}
                    disabled={authoringMutation.isPending || applyAuthoringMutation.isPending}
                    onChange={setDraftContent}
                    onSave={() => {
                      if (dirty && !authoringMutation.isPending) {
                        saveMutation.mutate();
                      }
                    }}
                  />
                )
              ) : (
                <div className="empty-row">
                  <Loader size="sm" />
                </div>
              )}
            </div>

            {detail ? (
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

      <Drawer
        className="assistant-chat-drawer"
        opened={assistantOpen}
        onClose={() => setAssistantOpen(false)}
        position="right"
        size={520}
        title={
          <Group gap="xs" wrap="nowrap">
            <MemberSelector
              ariaLabel={t("commands.authoring.runner", { member: authoringMember?.name ?? "" })}
              member={authoringMember}
              members={activeMembers}
              onChange={(personId) => {
                applyAuthoringMutation.reset();
                setPendingProposal(null);
                setAuthoringPersonId(personId);
              }}
            />
            <Text fw={600}>{t("commands.authoring.title")}</Text>
          </Group>
        }
        // Non-modal: the editor behind the drawer stays editable so the source
        // can be read and changed while the conversation continues.
        withOverlay={false}
        lockScroll={false}
        closeOnClickOutside={false}
        trapFocus={false}
      >
        <AssistantChatPanel
          namespace="commands.authoring"
          key={authoring.conversationId}
          messages={authoring.messages}
          pending={authoringMutation.isPending}
          disabled={!authoringPerson || !detail || Boolean(pendingProposal)}
          autoScrollOnAssistantResponse
          // A failure belongs to the command that produced it: switching
          // commands must not carry it into the next conversation.
          error={
            authoringMutation.variables?.targetKey === authoringTargetKey
              ? authoringErrorMessage(authoringMutation.error)
              : null
          }
          onSubmit={(authoringMessage) => {
            if (!detail) {
              return;
            }
            authoringMutation.mutate({
              mode: "edit",
              conversation_id: authoring.conversationId,
              command: detail.command,
              format: detail.format,
              content: draftContent,
              file_id: detail.id,
              revision,
              message: authoringMessage,
              person: authoringPerson ?? undefined,
              targetKey: authoringTargetKey,
            });
          }}
        />
      </Drawer>

      <CreateCommandDialog
        key={createOpen ? "create-open" : "create-closed"}
        opened={createOpen}
        pending={
          createMutation.isPending ||
          aiCreateMutation.isPending ||
          (applyAuthoringMutation.isPending &&
            applyAuthoringMutation.variables?.source === "create")
        }
        manualError={createMutation.error}
        aiError={
          aiCreateMutation.error ||
          (applyAuthoringMutation.variables?.source === "create"
            ? applyAuthoringMutation.error
            : null)
        }
        aiAvailable={Boolean(authoringPerson)}
        aiResponse={pendingProposal?.source === "create" ? pendingProposal.response : null}
        commandsRoot={commandsRoot}
        onClose={() => {
          createMutation.reset();
          aiCreateMutation.reset();
          applyAuthoringMutation.reset();
          setPendingProposal(null);
          setCreateOpen(false);
        }}
        onCreateManually={(command, format) => createMutation.mutate({ command, format })}
        onCreateWithAi={(authoringMessage) =>
          aiCreateMutation.mutate({
            conversationId: newConversationId(),
            message: authoringMessage,
          })
        }
        onApplyAiProposal={() => {
          if (pendingProposal?.source === "create") {
            applyAuthoringMutation.mutate(pendingProposal);
          }
        }}
        onDiscardAiProposal={() => {
          applyAuthoringMutation.reset();
          setPendingProposal(null);
        }}
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
  aiResponse,
  commandsRoot,
  onClose,
  onCreateManually,
  onCreateWithAi,
  onApplyAiProposal,
  onDiscardAiProposal,
}: {
  opened: boolean;
  pending: boolean;
  manualError: unknown;
  aiError: unknown;
  aiAvailable: boolean;
  aiResponse: CommandAuthoringResponse | null;
  commandsRoot: string;
  onClose: () => void;
  onCreateManually: (command: string, format: CommandFileFormat) => void;
  onCreateWithAi: (message: string) => void;
  onApplyAiProposal: () => void;
  onDiscardAiProposal: () => void;
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
      size={method === "ai" && aiResponse?.action === "propose_changes" ? "xl" : "md"}
      title={t("commands.createTitle")}
    >
      <Stack>
        <SegmentedControl
          aria-label={t("commands.createMethodLabel")}
          disabled={pending || Boolean(aiResponse)}
          fullWidth
          value={method}
          onChange={(value) => setMethod(value as "ai" | "manual")}
          data={[
            { value: "ai", label: t("commands.createMethodAi") },
            { value: "manual", label: t("commands.createMethodManual") },
          ]}
        />
        {method === "ai" && aiResponse ? (
          <Stack gap="sm">
            {aiResponse.action === "propose_changes" ? (
              <>
                <Text size="sm">
                  {t("commands.authoringProposal.ready", { count: aiResponse.changes.length })}
                </Text>
                <Text c="dimmed" size="sm">
                  {aiResponse.message}
                </Text>
              </>
            ) : (
              <Text size="sm">{aiResponse.message}</Text>
            )}
            {aiResponse.action === "propose_changes" ? (
              <CommandAuthoringWorkspace
                changes={aiResponse.changes}
                commandsRoot={commandsRoot}
                pending={pending}
                error={aiError}
                onApply={onApplyAiProposal}
                onDiscard={onDiscardAiProposal}
              />
            ) : null}
          </Stack>
        ) : method === "ai" ? (
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
        {errorMessage && !(method === "ai" && aiResponse?.action === "propose_changes") ? (
          <Alert color="warning" title={t("commands.createErrorTitle")}>
            {errorMessage}
          </Alert>
        ) : null}
        <Group justify="flex-end">
          <Button variant="subtle" disabled={pending} onClick={onClose}>
            {t("commands.createCancel")}
          </Button>
          {method === "ai" && aiResponse?.action === "answer" ? (
            <Button variant="default" disabled={pending} onClick={onDiscardAiProposal}>
              {t("commands.authoringProposal.back")}
            </Button>
          ) : method === "ai" && aiResponse ? null : (
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
          )}
        </Group>
      </Stack>
    </Modal>
  );
}
