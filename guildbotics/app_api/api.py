from __future__ import annotations

import logging
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, cast

import httpx
from fastapi import (
    Depends,
    FastAPI,
    File,
    Header,
    Query,
    Request,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from guildbotics.app_api.command_input_files import CommandInputFileStore
from guildbotics.app_api.config_revisions import (
    apply_config_write,
    config_repository,
)
from guildbotics.app_api.errors import AppApiError, api_error_message
from guildbotics.app_api.events import EventBus, EventBusLogHandler
from guildbotics.app_api.hotkeys import load_hotkeys, save_hotkeys
from guildbotics.app_api.intelligences import (
    IntelligenceConfigService,
    intelligence_config_dir,
)
from guildbotics.app_api.models import (
    ActivityHistoryResponse,
    AgentFieldStateResponse,
    ApiError,
    ChatReceiveResetResponse,
    CliAgentDetectionsResponse,
    CliAgentUsagesResponse,
    CommandAuthoringApplyRequest,
    CommandAuthoringApplyResponse,
    CommandAuthoringRequest,
    CommandAuthoringResponse,
    CommandFileCreateRequest,
    CommandFileDetail,
    CommandFileExecutionStatus,
    CommandFilesResponse,
    CommandFileUpdateRequest,
    CommandInputFileResponse,
    CommandOptionsResponse,
    CommandRunRequest,
    CommandRunResponse,
    ConfigStatus,
    DefaultPersonUpdateRequest,
    DeviceRenameRequest,
    DeviceSshKey,
    GitHubAppRegistrationStartRequest,
    GitHubAppRegistrationStatus,
    GrantEvaluation,
    GrantScope,
    HealthResponse,
    HotkeySettings,
    HubConnection,
    HubStatus,
    HubTarget,
    HubTrustRequest,
    IntelligenceConfigResponse,
    IntelligenceConfigUpdateRequest,
    LlmProvidersResponse,
    MemberDeleteRequest,
    MemberResolveRequest,
    MemberResolveResponse,
    MemoryEventsResponse,
    ProjectConfigResponse,
    ProjectConfigUpdateRequest,
    ProjectStatusOptionsRequest,
    ProjectStatusOptionsResponse,
    RoleOption,
    RoleOptionsResponse,
    RoutineCommandOptionsResponse,
    RuntimeDebugStatus,
    RuntimeDebugUpdateRequest,
    RuntimeStatus,
    SandboxStatusResponse,
    ScenarioDiagnosticsResponse,
    SchedulerStartRequest,
    SchedulerStopRequest,
    SecretTransferRequest,
    SecretTransferResponse,
    SlackAppRegistrationStartRequest,
    SlackTokenVerifyRequest,
    SystemAlertDismissRequest,
    SystemAlertsResponse,
    TeamSummary,
    TraceDetailResponse,
    TracesResponse,
    TranscriptSettingsStatus,
    TranscriptSettingsUpdateRequest,
    TroubleshootingRequest,
    TroubleshootingResponse,
    VerifyResponse,
    WorkspaceChangeRequest,
    WorkspaceDevices,
    WorkspaceLiveState,
    WorkspaceSecrets,
    WorkspaceServiceOwner,
    WorkspaceServiceOwnerTransferRequest,
    WorkspaceSyncCloneRequest,
    WorkspaceSyncEnableRequest,
    WorkspaceSyncPreview,
    WorkspaceSyncStatus,
)
from guildbotics.app_api.runtime import AppRuntime
from guildbotics.app_api.sandbox_status import (
    evaluate_grant,
)
from guildbotics.app_api.workspace_secrets import WorkspaceSecretService
from guildbotics.app_api.workspace_sync import WorkspaceSyncService
from guildbotics.editions.simple import slack_app_setup
from guildbotics.editions.simple.github_app_setup import (
    GitHubAppRegistration,
    GitHubAppRegistrationService,
)
from guildbotics.editions.simple.setup_service import (
    PROJECT_CONFIG_PATHS,
    PersonConfigSnapshot,
    PersonSetupInput,
    PersonSetupResult,
    PersonUpdateInput,
    ProjectConfigSnapshot,
    ProjectSetupInput,
    ProjectSetupResult,
    ProjectUpdateInput,
    SetupServiceError,
    SimplePersonSetupService,
    SimpleProjectSetupService,
    github_app_key_dir,
    person_config_paths,
)
from guildbotics.editions.simple.slack_app_setup import (
    SlackAppRegistrationInfo,
    SlackTokenVerification,
)
from guildbotics.intelligences.llm_providers import discover_llm_providers
from guildbotics.observability.diagnostics_store import DiagnosticsStore
from guildbotics.utils.env_loader import read_workspace_secrets
from guildbotics.utils.fileio import (
    WorkspaceNotConfiguredError,
    get_template_path,
    load_yaml_file,
)
from guildbotics.utils.shared_write_lock import SharedWriteBusyError
from guildbotics.utils.sync_lock import SyncRepositoryBusyError

TOKEN_HEADER = "X-GuildBotics-Session-Token"
#: The header the Desktop sends naming its display language, so error
#: sentences come back in the language the screen is in.
LANGUAGE_HEADER = "X-GuildBotics-Language"


def _request_language(request: Request) -> str:
    """Return the display language the request asked for, defaulting to en."""
    value = (request.headers.get(LANGUAGE_HEADER) or "").strip().lower()
    return "ja" if value.startswith("ja") else "en"


# Origins the packaged desktop webview serves the app from. Windows uses the
# HTTP workaround origin while the other desktop platforms use the Tauri
# scheme. Browser-preview origins are not fixed, so the launcher injects them
# through ``create_app(allowed_origins=...)`` instead of matching a pattern.
TAURI_ORIGINS = ["tauri://localhost", "http://tauri.localhost"]

logger = logging.getLogger("guildbotics.app_api")


class ConfigWriteResponse(BaseModel):
    project: ProjectSetupResult | None = None
    member: PersonSetupResult | None = None
    intelligence: dict[str, Any] | None = None
    # Where the files this write touched now stand. The screen that saved is
    # still open and will very likely save again, so handing it the new
    # revisions is what keeps its next save from colliding with this one.
    revisions: dict[str, str] = Field(default_factory=dict)


class AvatarMutationResponse(BaseModel):
    # File mtime of the saved avatar, used by the client as a deterministic
    # cache-buster so the URL only changes when the avatar actually changes.
    avatar_timestamp: int


def create_app(
    *,
    session_token: str | None = None,
    allowed_origins: list[str] | None = None,
    runtime: AppRuntime | None = None,
    event_bus: EventBus | None = None,
    diagnostics_store: DiagnosticsStore | None = None,
    command_input_file_store: CommandInputFileStore | None = None,
    restore_workspace_environment: bool = False,
) -> FastAPI:
    token = session_token or secrets.token_urlsafe(32)
    store = diagnostics_store or DiagnosticsStore()
    bus = event_bus or EventBus(store=store)
    if runtime is not None:
        app_runtime = runtime
    elif restore_workspace_environment:
        app_runtime = AppRuntime(
            bus,
            diagnostics_store=store,
            load_workspace_environment=True,
        )
    else:
        app_runtime = AppRuntime(bus, diagnostics_store=store)
    log_handler = EventBusLogHandler(bus)
    system_service_run_id = getattr(
        app_runtime, "system_service_run_id", secrets.token_urlsafe(16)
    )
    input_file_store = command_input_file_store or CommandInputFileStore()
    # The service holds no state of its own: the queue it starts and stops
    # is process-wide, so this instance and the runtime's act on the same one.
    # The runtime owns the common execution boundary; the sync service only
    # attaches/detaches its relay publisher there.
    if isinstance(app_runtime, AppRuntime):
        sync_service = app_runtime.workspace_sync_service
    else:
        sync_service = WorkspaceSyncService()
    # Secrets travel between OS secret stores rather than through Git, but the
    # machine they travel to is the workspace's hub, so the two services share
    # one answer to "where is the hub" instead of resolving it twice.
    secret_service = WorkspaceSecretService(sync_service)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger = logging.getLogger("guildbotics")
        uvicorn_error_logger = logging.getLogger("uvicorn.error")
        added_app_handler = False
        added_uvicorn_handler = False
        input_file_store.start()
        try:
            store.start_system_session(system_service_run_id)
            store.start_maintenance()
            sync_service.activate()
            if not any(
                isinstance(handler, EventBusLogHandler) for handler in logger.handlers
            ):
                logger.addHandler(log_handler)
                added_app_handler = True
            if log_handler not in uvicorn_error_logger.handlers:
                uvicorn_error_logger.addHandler(log_handler)
                added_uvicorn_handler = True
            try:
                yield
            finally:
                try:
                    app_runtime.stop_scheduler(force=True)
                    sync_service.deactivate()
                finally:
                    if added_app_handler:
                        logger.removeHandler(log_handler)
                    if added_uvicorn_handler:
                        uvicorn_error_logger.removeHandler(log_handler)
                    store.finish_system_session()
                    store.stop_maintenance()
        finally:
            input_file_store.close()

    app = FastAPI(title="GuildBotics App API", version="0.1.0", lifespan=lifespan)
    app.state.session_token = token
    app.state.runtime = app_runtime
    app.state.event_bus = bus

    # Added before CORS so that CORS wraps it: the JSON error it builds travels
    # back out through the CORS layer and carries its headers.
    app.add_middleware(_UnhandledErrorMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[*TAURI_ORIGINS, *(allowed_origins or [])],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=[TOKEN_HEADER, LANGUAGE_HEADER, "Content-Type"],
    )

    error_responses: dict[int | str, dict[str, Any]] = {
        400: {"model": ApiError},
        401: {"model": ApiError},
        422: {"model": ApiError},
        409: {"model": ApiError},
        500: {"model": ApiError},
    }

    @app.exception_handler(AppApiError)
    async def app_api_error_handler(request: Request, exc: AppApiError) -> JSONResponse:
        # The one place a message becomes text: rendered from the error's
        # message key in the language the request asked for.
        return _error_response(
            exc.status_code,
            exc.code,
            exc.localized(_request_language(request)),
            exc.context,
        )

    @app.exception_handler(SharedWriteBusyError)
    async def shared_write_busy_handler(
        request: Request, exc: SharedWriteBusyError
    ) -> JSONResponse:
        # One handler rather than one translation per route: every config
        # change and every enrollment step can meet a busy lock, and none of
        # them should have to name it.
        return _error_response(
            503,
            "config_busy",
            api_error_message("config_busy", _request_language(request), {}),
            {},
        )

    @app.exception_handler(SyncRepositoryBusyError)
    async def sync_repository_busy_handler(
        request: Request, exc: SyncRepositoryBusyError
    ) -> JSONResponse:
        # Same shape as SharedWriteBusyError: any endpoint that pauses or
        # joins synchronization can outwait the repository lock, and none of
        # them should have to name it.
        return _error_response(
            409,
            "workspace_sync_busy",
            api_error_message(
                "workspace_sync_busy.repository", _request_language(request), {}
            ),
            {},
        )

    @app.exception_handler(WorkspaceNotConfiguredError)
    async def workspace_not_configured_handler(
        _, exc: WorkspaceNotConfiguredError
    ) -> JSONResponse:
        # The exception's own sentence is the reason, passed through verbatim.
        return _error_response(409, "workspace_not_configured", str(exc), {})

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _error_response(
            422,
            "validation_error",
            api_error_message("validation_error", _request_language(request), {}),
            {"errors": jsonable_encoder(exc.errors())},
        )

    def require_token(
        provided: Annotated[str | None, Header(alias=TOKEN_HEADER)] = None,
    ) -> None:
        if provided != token:
            raise AppApiError(
                "invalid_session_token",
                status_code=401,
            )

    @app.get("/health", response_model=HealthResponse, responses=error_responses)
    def health(_: None = Depends(require_token)) -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get("/config/status", response_model=ConfigStatus, responses=error_responses)
    def config_status(_: None = Depends(require_token)) -> ConfigStatus:
        return app_runtime.get_config_status()

    @app.post("/workspace", response_model=ConfigStatus, responses=error_responses)
    def workspace_change(
        request: WorkspaceChangeRequest,
        _: None = Depends(require_token),
    ) -> ConfigStatus:
        return app_runtime.set_workspace(request.workspace_dir)

    @app.get("/hub", response_model=HubStatus, responses=error_responses)
    def hub_status(_: None = Depends(require_token)) -> HubStatus:
        return sync_service.get_hub()

    @app.post("/hub", response_model=HubStatus, responses=error_responses)
    def hub_create(_: None = Depends(require_token)) -> HubStatus:
        return sync_service.create_hub()

    @app.post("/hub/inspect", response_model=HubConnection, responses=error_responses)
    def hub_inspect(
        request: HubTarget,
        _: None = Depends(require_token),
    ) -> HubConnection:
        return sync_service.inspect_hub(request)

    @app.post("/hub/trust", response_model=HubConnection, responses=error_responses)
    def hub_trust(
        request: HubTrustRequest,
        _: None = Depends(require_token),
    ) -> HubConnection:
        return sync_service.trust_hub(request)

    @app.get("/hub/ssh-key", response_model=DeviceSshKey, responses=error_responses)
    def hub_ssh_key(_: None = Depends(require_token)) -> DeviceSshKey:
        return sync_service.get_ssh_key()

    @app.post("/hub/ssh-key", response_model=DeviceSshKey, responses=error_responses)
    def hub_ssh_key_create(_: None = Depends(require_token)) -> DeviceSshKey:
        return sync_service.create_ssh_key()

    @app.get(
        "/workspace/devices",
        response_model=WorkspaceDevices,
        responses=error_responses,
    )
    def workspace_devices(_: None = Depends(require_token)) -> WorkspaceDevices:
        return sync_service.get_devices()

    @app.post(
        "/workspace/devices/self",
        response_model=WorkspaceDevices,
        responses=error_responses,
    )
    def workspace_device_rename(
        request: DeviceRenameRequest,
        _: None = Depends(require_token),
    ) -> WorkspaceDevices:
        return sync_service.rename_device(request)

    @app.get(
        "/workspace/live",
        response_model=list[WorkspaceLiveState],
        responses=error_responses,
    )
    def workspace_live(_: None = Depends(require_token)) -> list[WorkspaceLiveState]:
        return sync_service.get_live_states()

    @app.get(
        "/workspace/service-owner",
        response_model=WorkspaceServiceOwner,
        responses=error_responses,
    )
    def workspace_service_owner(
        _: None = Depends(require_token),
    ) -> WorkspaceServiceOwner:
        return sync_service.get_service_owner()

    @app.post(
        "/workspace/service-owner/transfer",
        response_model=WorkspaceServiceOwner,
        responses=error_responses,
    )
    def workspace_service_owner_transfer(
        request: WorkspaceServiceOwnerTransferRequest,
        _: None = Depends(require_token),
    ) -> WorkspaceServiceOwner:
        return sync_service.transfer_service_owner(request.device_id)

    @app.get(
        "/workspace/sync",
        response_model=WorkspaceSyncStatus,
        responses=error_responses,
    )
    def workspace_sync_status(
        _: None = Depends(require_token),
    ) -> WorkspaceSyncStatus:
        return sync_service.get_status()

    @app.post(
        "/workspace/sync/preview",
        response_model=WorkspaceSyncPreview,
        responses=error_responses,
    )
    def workspace_sync_preview(
        request: WorkspaceSyncEnableRequest,
        _: None = Depends(require_token),
    ) -> WorkspaceSyncPreview:
        return sync_service.preview(request)

    @app.post(
        "/workspace/sync/enable",
        response_model=WorkspaceSyncStatus,
        responses=error_responses,
    )
    def workspace_sync_enable(
        request: WorkspaceSyncEnableRequest,
        _: None = Depends(require_token),
    ) -> WorkspaceSyncStatus:
        return sync_service.enable(request)

    @app.post(
        "/workspace/sync/hub",
        response_model=WorkspaceSyncStatus,
        responses=error_responses,
    )
    def workspace_sync_change_hub(
        request: WorkspaceSyncEnableRequest,
        _: None = Depends(require_token),
    ) -> WorkspaceSyncStatus:
        return sync_service.change_hub(request)

    @app.post(
        "/workspace/sync/retry",
        response_model=WorkspaceSyncStatus,
        responses=error_responses,
    )
    def workspace_sync_retry(_: None = Depends(require_token)) -> WorkspaceSyncStatus:
        return sync_service.retry()

    @app.post(
        "/workspace/sync/rejections/{rejection_id}/discard",
        response_model=WorkspaceSyncStatus,
        responses=error_responses,
    )
    def workspace_sync_discard_rejection(
        rejection_id: str,
        _: None = Depends(require_token),
    ) -> WorkspaceSyncStatus:
        """Drop one displaced commit the user has finished with."""
        return sync_service.discard_rejection(rejection_id)

    @app.get(
        "/workspace/secrets",
        response_model=WorkspaceSecrets,
        responses=error_responses,
    )
    def workspace_secrets(_: None = Depends(require_token)) -> WorkspaceSecrets:
        """Report which keys this device holds, and which the hub holds."""
        return secret_service.get_secrets()

    @app.post(
        "/workspace/secrets/send",
        response_model=SecretTransferResponse,
        responses=error_responses,
    )
    def workspace_secrets_send(
        request: SecretTransferRequest,
        _: None = Depends(require_token),
    ) -> SecretTransferResponse:
        """Hand this device's values to the hub, because the user asked."""
        return secret_service.send(request)

    @app.post(
        "/workspace/secrets/fetch",
        response_model=SecretTransferResponse,
        responses=error_responses,
    )
    def workspace_secrets_fetch(
        request: SecretTransferRequest,
        _: None = Depends(require_token),
    ) -> SecretTransferResponse:
        """Take values from the hub into this device's own secret store."""
        return secret_service.fetch(request)

    @app.post(
        "/workspace/sync/clone", response_model=ConfigStatus, responses=error_responses
    )
    def workspace_sync_clone(
        request: WorkspaceSyncCloneRequest,
        _: None = Depends(require_token),
    ) -> ConfigStatus:
        """Take a copy of a hub's workspace and switch to it."""
        destination = sync_service.clone(request)
        return app_runtime.set_workspace(destination)

    @app.get("/team", response_model=TeamSummary, responses=error_responses)
    def team(_: None = Depends(require_token)) -> TeamSummary:
        return app_runtime.get_team_summary()

    @app.get(
        "/commands/options",
        response_model=CommandOptionsResponse,
        responses=error_responses,
    )
    def command_options(
        person: str | None = None,
        _: None = Depends(require_token),
    ) -> CommandOptionsResponse:
        return app_runtime.get_command_options(person)

    @app.get(
        "/commands/routine-options",
        response_model=RoutineCommandOptionsResponse,
        responses=error_responses,
    )
    def routine_command_options(
        person: str | None = None,
        _: None = Depends(require_token),
    ) -> RoutineCommandOptionsResponse:
        return app_runtime.get_routine_command_options(person)

    @app.post(
        "/commands/run",
        response_model=CommandRunResponse,
        responses=error_responses,
    )
    async def run_command(
        request: CommandRunRequest,
        _: None = Depends(require_token),
    ) -> CommandRunResponse:
        return await app_runtime.run_command(request)

    @app.post(
        "/commands/input-files",
        response_model=CommandInputFileResponse,
        responses=error_responses,
    )
    def command_input_file_upload(
        file: UploadFile = File(...),  # noqa: B008
        _: None = Depends(require_token),
    ) -> CommandInputFileResponse:
        try:
            path = input_file_store.save(file)
        except ValueError as exc:
            raise AppApiError(
                "command_input_file_invalid",
                reason=str(exc),
                status_code=400,
            ) from exc
        except OSError as exc:
            logger.exception("Failed to save a command input file")
            raise AppApiError(
                "command_input_file_save_failed",
                status_code=500,
            ) from exc
        return CommandInputFileResponse(path=path)

    @app.post(
        "/commands/author",
        response_model=CommandAuthoringResponse,
        responses=error_responses,
    )
    async def author_command(
        request: CommandAuthoringRequest,
        _: None = Depends(require_token),
    ) -> CommandAuthoringResponse:
        return await app_runtime.author_command(request)

    @app.post(
        "/commands/author/apply",
        response_model=CommandAuthoringApplyResponse,
        responses=error_responses,
    )
    def apply_command_authoring(
        request: CommandAuthoringApplyRequest,
        _: None = Depends(require_token),
    ) -> CommandAuthoringApplyResponse:
        return app_runtime.apply_command_authoring(request)

    @app.get(
        "/commands/files",
        response_model=CommandFilesResponse,
        responses=error_responses,
    )
    def list_command_files(
        _: None = Depends(require_token),
    ) -> CommandFilesResponse:
        return app_runtime.list_command_files()

    @app.get(
        "/commands/files/{file_id}",
        response_model=CommandFileDetail,
        responses=error_responses,
    )
    def get_command_file(
        file_id: str,
        _: None = Depends(require_token),
    ) -> CommandFileDetail:
        return app_runtime.get_command_file(file_id)

    @app.post(
        "/commands/files",
        response_model=CommandFileDetail,
        responses=error_responses,
    )
    def create_command_file(
        request: CommandFileCreateRequest,
        _: None = Depends(require_token),
    ) -> CommandFileDetail:
        return app_runtime.create_command_file(request)

    @app.put(
        "/commands/files/{file_id}",
        response_model=CommandFileDetail,
        responses=error_responses,
    )
    def update_command_file(
        file_id: str,
        request: CommandFileUpdateRequest,
        _: None = Depends(require_token),
    ) -> CommandFileDetail:
        return app_runtime.update_command_file(file_id, request)

    @app.delete(
        "/commands/files/{file_id}",
        response_model=CommandFilesResponse,
        responses=error_responses,
    )
    def delete_command_file(
        file_id: str,
        expected_revision: str,
        _: None = Depends(require_token),
    ) -> CommandFilesResponse:
        return app_runtime.delete_command_file(file_id, expected_revision)

    @app.get(
        "/commands/files/{file_id}/execution-status",
        response_model=CommandFileExecutionStatus,
        responses=error_responses,
    )
    def command_file_execution_status(
        file_id: str,
        expected_revision: str,
        person: str | None = None,
        _: None = Depends(require_token),
    ) -> CommandFileExecutionStatus:
        return app_runtime.get_command_file_execution_status(
            file_id, person, expected_revision
        )

    @app.get("/hotkeys", response_model=HotkeySettings, responses=error_responses)
    def get_hotkeys(_: None = Depends(require_token)) -> HotkeySettings:
        return load_hotkeys()

    @app.put("/hotkeys", response_model=HotkeySettings, responses=error_responses)
    def put_hotkeys(
        request: HotkeySettings,
        _: None = Depends(require_token),
    ) -> HotkeySettings:
        return save_hotkeys(request)

    @app.get(
        "/config/roles",
        response_model=RoleOptionsResponse,
        responses=error_responses,
    )
    def config_roles(
        language: Annotated[str, Query(pattern="^(en|ja)$")] = "ja",
        _: None = Depends(require_token),
    ) -> RoleOptionsResponse:
        role_file = get_template_path() / f"roles/default.{language}.yml"
        role_data = cast(dict, load_yaml_file(role_file))
        roles_data = role_data.get("roles", {}) if isinstance(role_data, dict) else {}
        roles: list[RoleOption] = []
        if isinstance(roles_data, dict):
            for role_id, role_value in roles_data.items():
                if not isinstance(role_id, str) or not isinstance(role_value, dict):
                    continue
                roles.append(
                    RoleOption(
                        role_id=role_id,
                        summary=str(role_value.get("summary", "")),
                        description=str(role_value.get("description", "")),
                    )
                )
        return RoleOptionsResponse(roles=roles)

    @app.get(
        "/scheduler/status",
        response_model=RuntimeStatus,
        responses=error_responses,
    )
    def scheduler_status(_: None = Depends(require_token)) -> RuntimeStatus:
        return app_runtime.get_scheduler_status()

    @app.post(
        "/scheduler/start",
        response_model=RuntimeStatus,
        responses=error_responses,
    )
    def scheduler_start(
        request: SchedulerStartRequest,
        _: None = Depends(require_token),
    ) -> RuntimeStatus:
        return app_runtime.start_scheduler(request)

    @app.post(
        "/scheduler/stop",
        response_model=RuntimeStatus,
        responses=error_responses,
    )
    def scheduler_stop(
        request: SchedulerStopRequest | None = None,
        _: None = Depends(require_token),
    ) -> RuntimeStatus:
        return app_runtime.stop_scheduler(force=request.force if request else False)

    @app.post(
        "/chat/receive-state/reset",
        response_model=ChatReceiveResetResponse,
        responses=error_responses,
    )
    def chat_receive_state_reset(
        _: None = Depends(require_token),
    ) -> ChatReceiveResetResponse:
        return app_runtime.reset_chat_receive_state()

    @app.get(
        "/transcripts/settings",
        response_model=TranscriptSettingsStatus,
        responses=error_responses,
    )
    def transcript_settings(
        _: None = Depends(require_token),
    ) -> TranscriptSettingsStatus:
        return app_runtime.get_transcript_settings()

    @app.put(
        "/transcripts/settings",
        response_model=TranscriptSettingsStatus,
        responses=error_responses,
    )
    def transcript_settings_update(
        request: TranscriptSettingsUpdateRequest,
        _: None = Depends(require_token),
    ) -> TranscriptSettingsStatus:
        return app_runtime.update_transcript_settings(request)

    @app.get(
        "/runtime/debug",
        response_model=RuntimeDebugStatus,
        responses=error_responses,
    )
    def runtime_debug_status(_: None = Depends(require_token)) -> RuntimeDebugStatus:
        return app_runtime.get_runtime_debug_status()

    @app.put(
        "/runtime/debug",
        response_model=RuntimeDebugStatus,
        responses=error_responses,
    )
    def runtime_debug_update(
        request: RuntimeDebugUpdateRequest,
        _: None = Depends(require_token),
    ) -> RuntimeDebugStatus:
        return app_runtime.update_runtime_debug(request)

    @app.post("/verify", response_model=VerifyResponse, responses=error_responses)
    def verify(_: None = Depends(require_token)) -> VerifyResponse:
        return app_runtime.verify()

    @app.get(
        "/system-alerts",
        response_model=SystemAlertsResponse,
        responses=error_responses,
    )
    def system_alerts(_: None = Depends(require_token)) -> SystemAlertsResponse:
        return app_runtime.get_system_alerts()

    @app.post(
        "/system-alerts/dismiss",
        response_model=SystemAlertsResponse,
        responses=error_responses,
    )
    def dismiss_system_alert(
        request: SystemAlertDismissRequest, _: None = Depends(require_token)
    ) -> SystemAlertsResponse:
        return app_runtime.dismiss_system_alert(request.alert_id)

    @app.post(
        "/diagnostics/scenario",
        response_model=ScenarioDiagnosticsResponse,
        responses=error_responses,
    )
    async def diagnostics_scenario(
        person_id: str | None = None,
        _: None = Depends(require_token),
    ) -> ScenarioDiagnosticsResponse:
        return await app_runtime.run_scenario_diagnostics(person_id=person_id)

    @app.get(
        "/diagnostics/traces",
        response_model=TracesResponse,
        responses=error_responses,
    )
    def diagnostics_traces(
        source: str | None = None,
        person_id: str | None = None,
        q: str | None = None,
        attr_key: str | None = None,
        attr_value: str | None = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 200,
        _: None = Depends(require_token),
    ) -> TracesResponse:
        return app_runtime.list_traces(
            source=source,
            person_id=person_id,
            query=q,
            attr_key=attr_key,
            attr_value=attr_value,
            limit=limit,
        )

    @app.get(
        "/diagnostics/traces/{trace_id}",
        response_model=TraceDetailResponse,
        responses=error_responses,
    )
    def diagnostics_trace_detail(
        trace_id: str,
        _: None = Depends(require_token),
    ) -> TraceDetailResponse:
        return app_runtime.get_trace_detail(trace_id)

    @app.get(
        "/diagnostics/global",
        response_model=TraceDetailResponse,
        responses=error_responses,
    )
    def diagnostics_global_records(
        limit: Annotated[int, Query(ge=1, le=1000)] = 200,
        _: None = Depends(require_token),
    ) -> TraceDetailResponse:
        return app_runtime.get_global_records(limit=limit)

    @app.post(
        "/diagnostics/troubleshoot",
        response_model=TroubleshootingResponse,
        responses=error_responses,
    )
    async def diagnostics_troubleshoot(
        request: TroubleshootingRequest,
        _: None = Depends(require_token),
    ) -> TroubleshootingResponse:
        return await app_runtime.troubleshoot(request)

    @app.get(
        "/diagnostics/memory-events",
        response_model=MemoryEventsResponse,
        responses=error_responses,
    )
    def diagnostics_memory_events(
        person_id: str | None = None,
        doc_id: str | None = None,
        action: str | None = None,
        trace_id: str | None = None,
        source: str | None = None,
        q: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 200,
        _: None = Depends(require_token),
    ) -> MemoryEventsResponse:
        return app_runtime.list_memory_events(
            person_id=person_id,
            doc_id=doc_id,
            action=action,
            trace_id=trace_id,
            source=source,
            query=q,
            since=since,
            until=until,
            limit=limit,
        )

    @app.get(
        "/activity/history",
        response_model=ActivityHistoryResponse,
        responses=error_responses,
    )
    def activity_history(
        start: str | None = None,
        end: str | None = None,
        limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
        refresh: bool = False,
        sync_start: str | None = None,
        sync_end: str | None = None,
        _: None = Depends(require_token),
    ) -> ActivityHistoryResponse:
        return app_runtime.get_activity_history(
            start=start,
            end=end,
            limit=limit,
            refresh=refresh,
            sync_start=sync_start,
            sync_end=sync_end,
        )

    @app.get(
        "/intelligences/cli-agents/detection",
        response_model=CliAgentDetectionsResponse,
        responses=error_responses,
    )
    def detect_cli_agents(
        _: None = Depends(require_token),
    ) -> CliAgentDetectionsResponse:
        return app_runtime.detect_cli_agents()

    @app.get(
        "/intelligences/cli-agents/usage",
        response_model=CliAgentUsagesResponse,
        responses=error_responses,
    )
    async def cli_agent_usage(
        refresh: bool = False,
        _: None = Depends(require_token),
    ) -> CliAgentUsagesResponse:
        return await app_runtime.get_cli_agent_usage(refresh=refresh)

    @app.get(
        "/intelligences/sandbox",
        response_model=SandboxStatusResponse,
        responses=error_responses,
    )
    def sandbox_status_view(
        _: None = Depends(require_token),
    ) -> SandboxStatusResponse:
        """What every active member's AI CLI slots may reach on this device."""
        return app_runtime.get_sandbox_status()

    @app.get(
        "/intelligences/grant-evaluation",
        response_model=GrantEvaluation,
        responses=error_responses,
    )
    def grant_evaluation_view(
        scope: GrantScope,
        path: str,
        access: str = "read",
        _: None = Depends(require_token),
    ) -> GrantEvaluation:
        """What a grant the user is about to add would mean on this device."""
        return evaluate_grant(scope, path, access)

    @app.get(
        "/intelligences/model-providers",
        response_model=LlmProvidersResponse,
        responses=error_responses,
    )
    def list_model_providers(
        _: None = Depends(require_token),
    ) -> LlmProvidersResponse:
        config_dir = app_runtime.get_config_status().config_dir
        return LlmProvidersResponse(
            providers=discover_llm_providers(config_dir or get_template_path())
        )

    @app.get(
        "/config/intelligences",
        response_model=IntelligenceConfigResponse,
        responses=error_responses,
    )
    def config_intelligences(
        person_id: str | None = None,
        _: None = Depends(require_token),
    ) -> IntelligenceConfigResponse:
        config_dir = _resolve_existing_config_dir(app_runtime)
        revisions = config_repository(config_dir).tree_revisions(
            intelligence_config_dir(person_id)
        )
        try:
            response = IntelligenceConfigService().read_config(
                config_dir=config_dir,
                person_id=person_id,
            )
        except SetupServiceError as exc:
            raise AppApiError(exc.code, reason=exc.message) from exc
        return response.model_copy(update={"revisions": revisions})

    @app.put(
        "/config/intelligences",
        response_model=ConfigWriteResponse,
        responses=error_responses,
    )
    def config_intelligences_update(
        request: IntelligenceConfigUpdateRequest,
        _: None = Depends(require_token),
    ) -> ConfigWriteResponse:
        scope = intelligence_config_dir(request.person_id)
        try:
            receipt = apply_config_write(
                request.config_dir,
                lambda: IntelligenceConfigService().update_config(request),
                expected=request.expected_revisions,
                report=lambda: config_repository(request.config_dir).tree_revisions(
                    scope
                ),
            )
        except SetupServiceError as exc:
            raise AppApiError(exc.code, reason=exc.message) from exc
        return ConfigWriteResponse(
            intelligence={
                "files": [file.model_dump() for file in receipt.result.files]
            },
            revisions=receipt.revisions,
        )

    @app.post(
        "/config/init",
        response_model=ConfigWriteResponse,
        responses=error_responses,
    )
    def config_init(
        request: ProjectSetupInput,
        _: None = Depends(require_token),
    ) -> ConfigWriteResponse:
        # Nothing to compare against on a first setup, but the workspace may
        # already be synchronized -- taking a copy from a hub creates one, and
        # its queue is running while this writes.
        try:
            receipt = apply_config_write(
                request.config_dir,
                lambda: SimpleProjectSetupService().write_project(request),
                report=lambda: config_repository(request.config_dir).revisions(
                    PROJECT_CONFIG_PATHS
                ),
            )
        except SetupServiceError as exc:
            raise AppApiError(exc.code, reason=exc.message) from exc
        return ConfigWriteResponse(project=receipt.result, revisions=receipt.revisions)

    @app.get(
        "/config/project",
        response_model=ProjectConfigResponse,
        responses=error_responses,
    )
    def config_project(_: None = Depends(require_token)) -> ProjectConfigResponse:
        status = app_runtime.get_config_status()
        config_dir = _get_existing_config_dir(status)
        if config_dir is None:
            raise AppApiError(
                "project_not_found",
                context={"project": str(status.project_file)},
                status_code=400,
            )
        # Revisions first: pairing content read earlier with a revision read
        # later would describe a state that never existed, and a save composed
        # against it would slip past the very check it feeds.
        revisions = config_repository(config_dir).revisions(PROJECT_CONFIG_PATHS)
        try:
            snapshot: ProjectConfigSnapshot = (
                SimpleProjectSetupService().read_project_config(
                    config_dir=config_dir,
                )
            )
        except SetupServiceError as exc:
            raise AppApiError(exc.code, reason=exc.message) from exc
        return ProjectConfigResponse.model_validate(
            snapshot.model_dump() | {"revisions": revisions}
        )

    @app.post(
        "/config/project/status-options",
        response_model=ProjectStatusOptionsResponse,
        responses=error_responses,
    )
    async def config_project_status_options(
        request: ProjectStatusOptionsRequest,
        _: None = Depends(require_token),
    ) -> ProjectStatusOptionsResponse:
        return await app_runtime.fetch_project_status_options(request)

    @app.post(
        "/config/project/agent-field",
        response_model=AgentFieldStateResponse,
        responses=error_responses,
    )
    async def config_project_agent_field(
        request: ProjectStatusOptionsRequest,
        _: None = Depends(require_token),
    ) -> AgentFieldStateResponse:
        return await app_runtime.fetch_agent_field_state(request)

    @app.post(
        "/config/project/agent-field/ensure",
        response_model=AgentFieldStateResponse,
        responses=error_responses,
    )
    async def config_project_agent_field_ensure(
        request: ProjectStatusOptionsRequest,
        _: None = Depends(require_token),
    ) -> AgentFieldStateResponse:
        return await app_runtime.ensure_agent_field(request)

    @app.put(
        "/config/project",
        response_model=ConfigWriteResponse,
        responses=error_responses,
    )
    def config_project_update(
        request: ProjectConfigUpdateRequest,
        _: None = Depends(require_token),
    ) -> ConfigWriteResponse:
        try:
            receipt = apply_config_write(
                request.config_dir,
                lambda: SimpleProjectSetupService().update_project(
                    ProjectUpdateInput.model_validate(request.model_dump())
                ),
                expected=request.expected_revisions,
                report=lambda: config_repository(request.config_dir).revisions(
                    PROJECT_CONFIG_PATHS
                ),
            )
        except SetupServiceError as exc:
            raise AppApiError(exc.code, reason=exc.message) from exc
        return ConfigWriteResponse(project=receipt.result, revisions=receipt.revisions)

    @app.put(
        "/config/project/default-person",
        response_model=ConfigWriteResponse,
        responses=error_responses,
    )
    def config_default_person_update(
        request: DefaultPersonUpdateRequest,
        _: None = Depends(require_token),
    ) -> ConfigWriteResponse:
        config_dir = _resolve_existing_config_dir(app_runtime)
        try:
            receipt = apply_config_write(
                config_dir,
                lambda: SimpleProjectSetupService().set_default_person(
                    config_dir=config_dir,
                    person_id=request.person_id,
                ),
                report=lambda: config_repository(config_dir).revisions(
                    PROJECT_CONFIG_PATHS
                ),
            )
        except SetupServiceError as exc:
            raise AppApiError(exc.code, reason=exc.message) from exc
        return ConfigWriteResponse(project=receipt.result, revisions=receipt.revisions)

    @app.post(
        "/config/members",
        response_model=ConfigWriteResponse,
        responses=error_responses,
    )
    def config_members(
        request: PersonSetupInput,
        _: None = Depends(require_token),
    ) -> ConfigWriteResponse:
        try:
            payload = request.model_dump()
            status = app_runtime.get_config_status()
            if _get_existing_config_dir(status) is not None:
                payload["config_dir"] = _resolve_existing_config_dir(app_runtime)
            member = PersonSetupInput.model_validate(payload)

            receipt = apply_config_write(
                member.config_dir,
                lambda: SimplePersonSetupService().write_person(member),
                report=lambda: config_repository(member.config_dir).revisions(
                    person_config_paths(member.person_id)
                ),
            )
        except SetupServiceError as exc:
            raise AppApiError(exc.code, reason=exc.message) from exc
        return ConfigWriteResponse(member=receipt.result, revisions=receipt.revisions)

    @app.get(
        "/config/members/{person_id}",
        response_model=PersonConfigSnapshot,
        responses=error_responses,
    )
    def config_member(
        person_id: str,
        _: None = Depends(require_token),
    ) -> PersonConfigSnapshot:
        status = app_runtime.get_config_status()
        config_dir = _get_existing_config_dir(status)
        if config_dir is None:
            raise AppApiError(
                "project_not_found",
                context={"project": str(status.project_file)},
                status_code=400,
            )
        revisions = config_repository(config_dir).revisions(
            person_config_paths(person_id)
        )
        try:
            snapshot: PersonConfigSnapshot = (
                SimplePersonSetupService().read_person_config(
                    config_dir=config_dir,
                    person_id=person_id,
                )
            )
        except SetupServiceError as exc:
            raise AppApiError(exc.code, reason=exc.message) from exc
        return snapshot.model_copy(update={"revisions": revisions})

    @app.put(
        "/config/members/{person_id}",
        response_model=ConfigWriteResponse,
        responses=error_responses,
    )
    def config_member_update(
        person_id: str,
        request: PersonUpdateInput,
        _: None = Depends(require_token),
    ) -> ConfigWriteResponse:
        try:
            payload = request.model_dump()
            if payload.get("original_person_id") != person_id:
                raise AppApiError(
                    "person_id_mismatch",
                    status_code=400,
                )
            config_dir = _resolve_existing_config_dir(app_runtime)
            payload["config_dir"] = config_dir

            receipt = apply_config_write(
                config_dir,
                lambda: SimplePersonSetupService().update_person(
                    PersonUpdateInput.model_validate(payload)
                ),
                expected=request.expected_revisions,
                # The member may have been renamed, so the paths that matter
                # now are the new ones, not the ones the request was composed
                # against.
                report=lambda: config_repository(config_dir).revisions(
                    person_config_paths(request.person_id)
                ),
            )
        except SetupServiceError as exc:
            raise AppApiError(exc.code, reason=exc.message) from exc
        return ConfigWriteResponse(member=receipt.result, revisions=receipt.revisions)

    @app.delete(
        "/config/members/{person_id}",
        response_model=ConfigWriteResponse,
        responses=error_responses,
    )
    def config_member_delete(
        person_id: str,
        request: MemberDeleteRequest,
        _: None = Depends(require_token),
    ) -> ConfigWriteResponse:
        config_dir = _resolve_existing_config_dir(app_runtime)
        try:
            receipt = apply_config_write(
                config_dir,
                lambda: SimplePersonSetupService().delete_person(
                    config_dir=config_dir,
                    person_id=person_id,
                ),
                report=lambda: config_repository(config_dir).revisions(
                    person_config_paths(person_id)
                ),
            )
        except SetupServiceError as exc:
            raise AppApiError(exc.code, reason=exc.message) from exc
        return ConfigWriteResponse(member=receipt.result, revisions=receipt.revisions)

    @app.post(
        "/config/members/resolve",
        response_model=MemberResolveResponse,
        responses=error_responses,
    )
    def config_members_resolve(
        request: MemberResolveRequest,
        _: None = Depends(require_token),
    ) -> MemberResolveResponse:
        service = SimplePersonSetupService()
        try:
            if request.person_type == "github_apps":
                app_name = service.parse_github_apps_url(request.identity)
                reference = service.resolve_github_user(app_name, is_github_apps=True)
            else:
                reference = service.resolve_github_user(request.identity)
        except SetupServiceError as exc:
            raise AppApiError(exc.code, reason=exc.message) from exc
        return MemberResolveResponse.model_validate(reference.model_dump())

    github_app_registrations = GitHubAppRegistrationService()

    @app.post(
        "/config/members/github-app/registrations",
        response_model=GitHubAppRegistrationStatus,
        responses=error_responses,
    )
    def github_app_registration_start(
        request: GitHubAppRegistrationStartRequest,
        _: None = Depends(require_token),
    ) -> GitHubAppRegistrationStatus:
        base = request.callback_base_url.rstrip("/")
        try:
            registration = github_app_registrations.start(
                app_name=request.app_name,
                organization=request.organization,
                callback_url=f"{base}/github-app/registrations/callback",
                key_dir=github_app_key_dir(),
            )
        except SetupServiceError as exc:
            raise AppApiError(exc.code, reason=exc.message) from exc
        return _github_app_registration_status(
            registration,
            start_url=f"{base}/github-app/registrations/{registration.state}/start",
        )

    @app.get(
        "/config/members/github-app/registrations/{state}",
        response_model=GitHubAppRegistrationStatus,
        responses=error_responses,
    )
    async def github_app_registration_status(
        state: str,
        _: None = Depends(require_token),
    ) -> GitHubAppRegistrationStatus:
        try:
            registration = await github_app_registrations.check_installation(state)
        except SetupServiceError as exc:
            raise AppApiError(exc.code, reason=exc.message, status_code=404) from exc
        return _github_app_registration_status(registration)

    # The two routes below are opened as top-level browser navigations by
    # github.com and the external browser, which cannot send the session
    # token; the unguessable registration state in the URL is the credential
    # (valid until the registration expires, so reloads and retries work).
    @app.get("/github-app/registrations/{state}/start", include_in_schema=False)
    def github_app_registration_start_page(state: str) -> Response:
        from guildbotics.app_api.github_app_registration import (
            manifest_post_page,
            registration_error_page,
        )

        try:
            submission_url, manifest_json = github_app_registrations.manifest_form(
                state
            )
        except SetupServiceError as exc:
            return registration_error_page(exc.message)
        return manifest_post_page(submission_url, manifest_json)

    @app.get("/github-app/registrations/callback", include_in_schema=False)
    async def github_app_registration_callback(
        code: str = "", state: str = ""
    ) -> Response:
        from guildbotics.app_api.github_app_registration import (
            missing_code_error_page,
            registration_error_page,
        )

        if not code:
            return missing_code_error_page()
        try:
            registration = await github_app_registrations.complete(state, code)
        except SetupServiceError as exc:
            return registration_error_page(exc.message)
        except httpx.HTTPError as exc:
            logger.exception("GitHub App manifest conversion failed")
            return registration_error_page(str(exc))
        return RedirectResponse(registration.installation_page_url, status_code=302)

    # Slack has no manifest callback (its OAuth redirect rejects localhost and
    # app-level tokens have no creation API), so the desktop only needs the
    # deep link plus a way to check the two tokens the user pastes back.
    @app.post(
        "/config/members/slack-app/registrations",
        response_model=SlackAppRegistrationInfo,
        responses=error_responses,
    )
    def slack_app_registration_start(
        request: SlackAppRegistrationStartRequest,
        _: None = Depends(require_token),
    ) -> SlackAppRegistrationInfo:
        try:
            return slack_app_setup.start_registration(request.app_name)
        except SetupServiceError as exc:
            raise AppApiError(exc.code, reason=exc.message) from exc

    @app.post(
        "/config/members/slack-app/verify",
        response_model=SlackTokenVerification,
        responses=error_responses,
    )
    async def slack_app_token_verify(
        request: SlackTokenVerifyRequest,
        _: None = Depends(require_token),
    ) -> SlackTokenVerification:
        stored_bot_token = ""
        stored_app_token = ""
        config_dir = app_runtime.get_config_status().config_dir
        if request.person_id and config_dir is not None:
            stored_bot_token, stored_app_token = (
                SimplePersonSetupService().read_slack_tokens(
                    config_dir=config_dir,
                    person_id=request.person_id,
                )
            )
        return await slack_app_setup.verify_tokens(
            request.bot_token,
            request.app_token,
            stored_bot_token=stored_bot_token,
            stored_app_token=stored_app_token,
            channels=request.channels,
        )

    @app.get(
        "/config/members/{person_id}/avatar",
        responses={**error_responses, 404: {"model": ApiError}},
    )
    def config_member_avatar(
        person_id: str,
        token_query: str | None = Query(default=None, alias="token"),
        provided: Annotated[str | None, Header(alias=TOKEN_HEADER)] = None,
    ) -> Response:
        # Image is loaded via a plain <img> tag, which cannot set custom
        # headers, so the session token is also accepted as a query parameter.
        if token not in (provided, token_query):
            raise AppApiError(
                "invalid_session_token",
                status_code=401,
            )
        status = app_runtime.get_config_status()
        config_dir = _get_existing_config_dir(status)
        if config_dir is None:
            raise AppApiError(
                "project_not_found",
                status_code=400,
            )
        from guildbotics.app_api.avatar import find_avatar_file

        avatar_path = find_avatar_file(config_dir, person_id)
        if avatar_path is not None:
            resp = FileResponse(avatar_path)
            resp.headers["Cache-Control"] = "no-cache"
            return resp
        raise AppApiError("avatar_not_found", status_code=404)

    @app.post(
        "/config/members/{person_id}/avatar",
        response_model=AvatarMutationResponse,
        responses=error_responses,
    )
    def config_member_avatar_upload(
        person_id: str,
        file: UploadFile = File(...),  # noqa: B008
        _: None = Depends(require_token),
    ) -> AvatarMutationResponse:
        status = app_runtime.get_config_status()
        config_dir = _get_existing_config_dir(status)
        if config_dir is None:
            raise AppApiError(
                "project_not_found",
                status_code=400,
            )
        from guildbotics.app_api.avatar import read_upload, store_avatar

        try:
            content, suffix = read_upload(file)
            dest_path = apply_config_write(
                config_dir,
                lambda: store_avatar(config_dir, person_id, content, suffix),
            ).result
        except ValueError as exc:
            # Validation failures (e.g. too large) carry a safe, stable message.
            raise AppApiError(
                "avatar_invalid", reason=str(exc), status_code=400
            ) from exc
        except (AppApiError, SharedWriteBusyError):
            # Both already say what happened, and the catch-all below would
            # bury them under a generic 500.
            raise
        except Exception as exc:
            logger.exception("Failed to save avatar for %s", person_id)
            raise AppApiError(
                "avatar_save_failed",
                status_code=500,
            ) from exc
        return AvatarMutationResponse(avatar_timestamp=int(dest_path.stat().st_mtime))

    @app.post(
        "/config/members/{person_id}/avatar/github",
        response_model=AvatarMutationResponse,
        responses=error_responses,
    )
    async def config_member_avatar_github(
        person_id: str,
        _: None = Depends(require_token),
    ) -> AvatarMutationResponse:
        status = app_runtime.get_config_status()
        config_dir = _get_existing_config_dir(status)
        if config_dir is None:
            raise AppApiError(
                "project_not_found",
                status_code=400,
            )
        try:
            member_config = SimplePersonSetupService().read_person_config(
                config_dir=config_dir,
                person_id=person_id,
            )
        except SetupServiceError as exc:
            raise AppApiError(exc.code, reason=exc.message) from exc

        github_username = member_config.github_username
        if not github_username:
            raise AppApiError(
                "github_username_missing",
                status_code=400,
            )

        from guildbotics.app_api.avatar import get_github_avatar_url

        try:
            avatar_url = await get_github_avatar_url(github_username)
            dest_path = await _store_downloaded_avatar(
                config_dir, person_id, avatar_url
            )
        except (AppApiError, SharedWriteBusyError):
            raise
        except Exception as exc:
            logger.exception("Failed to import avatar from GitHub for %s", person_id)
            raise AppApiError(
                "avatar_import_failed",
                "avatar_import_failed.github",
                status_code=500,
            ) from exc
        return AvatarMutationResponse(avatar_timestamp=int(dest_path.stat().st_mtime))

    @app.post(
        "/config/members/{person_id}/avatar/slack",
        response_model=AvatarMutationResponse,
        responses=error_responses,
    )
    async def config_member_avatar_slack(
        person_id: str,
        _: None = Depends(require_token),
    ) -> AvatarMutationResponse:
        status = app_runtime.get_config_status()
        config_dir = _get_existing_config_dir(status)
        if config_dir is None:
            raise AppApiError(
                "project_not_found",
                status_code=400,
            )
        try:
            member_config = SimplePersonSetupService().read_person_config(
                config_dir=config_dir,
                person_id=person_id,
            )
        except SetupServiceError as exc:
            raise AppApiError(exc.code, reason=exc.message) from exc

        slack_user_id = member_config.slack_user_id
        is_human = member_config.person_type == "human"
        if is_human and not slack_user_id:
            raise AppApiError(
                "slack_user_id_missing",
                status_code=400,
            )

        # Real environment variables win over keychain values, and keys they
        # already provide are not fetched from the keychain at all.
        all_env = {
            **read_workspace_secrets(skip=frozenset(os.environ)),
            **dict(os.environ),
        }
        sanitized = person_id.upper().replace("-", "_")
        slack_bot_token = all_env.get(f"{sanitized}_SLACK_BOT_TOKEN")

        if not slack_bot_token:
            slack_bot_token = all_env.get("SLACK_BOT_TOKEN")

        if not slack_bot_token:
            for key, val in all_env.items():
                if key.endswith("_SLACK_BOT_TOKEN") and val:
                    slack_bot_token = val
                    break

        if not slack_bot_token:
            raise AppApiError(
                "slack_token_missing",
                status_code=400,
            )

        from guildbotics.app_api.avatar import get_slack_avatar_url

        try:
            avatar_url = await get_slack_avatar_url(slack_user_id, slack_bot_token)
            dest_path = await _store_downloaded_avatar(
                config_dir, person_id, avatar_url
            )
        except (AppApiError, SharedWriteBusyError):
            raise
        except Exception as exc:
            if "missing_scope" in str(exc):
                raise AppApiError(
                    "slack_missing_scope",
                    status_code=400,
                ) from exc
            logger.exception("Failed to import avatar from Slack for %s", person_id)
            raise AppApiError(
                "avatar_import_failed",
                "avatar_import_failed.slack",
                status_code=500,
            ) from exc
        return AvatarMutationResponse(avatar_timestamp=int(dest_path.stat().st_mtime))

    @app.websocket("/events")
    async def events(websocket: WebSocket, token_query: str = Query(alias="token")):
        await _stream(websocket, token_query, token, bus.subscribe_events)

    @app.websocket("/logs")
    async def logs(websocket: WebSocket, token_query: str = Query(alias="token")):
        await _stream(websocket, token_query, token, bus.subscribe_logs)

    return app


async def _store_downloaded_avatar(config_dir: Path, person_id: str, url: str) -> Path:
    """Fetch an avatar from a provider and put it in place.

    The download waits on a remote server, so it stays outside the workspace's
    shared-write lock; replacing the file is one change to the shared state, so
    it happens inside one. That lock blocks, and this runs on the event loop,
    hence the thread.
    """
    from guildbotics.app_api.avatar import download_avatar, store_avatar

    content, suffix = await download_avatar(url)
    receipt = await run_in_threadpool(
        apply_config_write,
        config_dir,
        lambda: store_avatar(config_dir, person_id, content, suffix),
    )
    return receipt.result


def _resolve_existing_config_dir(app_runtime: AppRuntime) -> Path:
    status = app_runtime.get_config_status()
    config_dir = _get_existing_config_dir(status)
    if config_dir is not None:
        return config_dir
    raise AppApiError(
        "project_not_found",
        context={"project": str(status.project_file)},
        status_code=400,
    )


def _get_existing_config_dir(status: ConfigStatus) -> Path | None:
    if status.project_file_exists and status.config_dir is not None:
        return status.config_dir
    return None


def _github_app_registration_status(
    registration: GitHubAppRegistration, start_url: str = ""
) -> GitHubAppRegistrationStatus:
    return GitHubAppRegistrationStatus(start_url=start_url, **registration.info_dump())


class _UnhandledErrorMiddleware:
    """Answer an unhandled exception with the API's JSON error, inside CORS.

    An ``Exception`` handler registered on the app is served by Starlette's
    outermost ``ServerErrorMiddleware``, above ``CORSMiddleware``, so its
    response reaches the browser without CORS headers: the Desktop app can then
    only report a bare network failure, whatever the backend actually said.
    Building the error here, below the CORS layer, keeps the reason readable in
    the UI.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        responded = False

        async def sender(message: Message) -> None:
            nonlocal responded
            if message["type"] == "http.response.start":
                responded = True
            await send(message)

        try:
            await self.app(scope, receive, sender)
        except Exception as exc:
            logging.getLogger("guildbotics").exception("Unhandled app API error")
            if responded:
                # The status line is already on the wire; only the server-error
                # middleware above can decide what a half-sent body becomes.
                raise
            response = _error_response(
                500,
                "internal_error",
                "An unexpected app API error occurred.",
                {"error_type": type(exc).__name__},
            )
            await response(scope, receive, sender)


def _error_response(
    status_code: int,
    code: str,
    message: str,
    context: dict | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=ApiError(
            code=code, message=message, context=context or {}
        ).model_dump(),
    )


async def _stream(
    websocket: WebSocket,
    provided_token: str,
    expected_token: str,
    subscribe,
) -> None:
    if provided_token != expected_token:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    queue = subscribe()
    try:
        while True:
            await websocket.send_json(await queue.get())
    except WebSocketDisconnect:
        pass
    finally:
        queue.close()
