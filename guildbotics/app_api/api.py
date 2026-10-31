from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from pathlib import Path
from typing import Annotated, Any, cast

from fastapi import (
    Depends,
    FastAPI,
    File,
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
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)
from pydantic import BaseModel, Field
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from guildbotics.app_api import decisions, ui
from guildbotics.app_api.agent_environment_status import (
    agent_environment_images,
    evaluate_grant,
)
from guildbotics.app_api.auth import (
    LANGUAGE_HEADER,
    SECURITY_HEADERS,
    SESSION_SECONDS,
    AppToken,
    AppTokenRequest,
    AuthorizationRequest,
    AuthorizationResponse,
    EntryLink,
    EntryLinkRequest,
    Guard,
    ProofRequest,
    ProofResponse,
    ServerAuth,
    SessionRequest,
    SessionResponse,
)
from guildbotics.app_api.command_input_files import (
    CommandInputFileStore,
    command_cwd,
    describe_command_input_paths,
)
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
from guildbotics.app_api.member import MemberApi, MemberRequest
from guildbotics.app_api.models import (
    ActivityHistoryResponse,
    AgentEnvironmentStatusResponse,
    ApiError,
    ChatReceiveResetResponse,
    CliAgentLastTurn,
    CliAgentUsageResponse,
    CommandAuthoringApplyRequest,
    CommandAuthoringApplyResponse,
    CommandAuthoringRequest,
    CommandAuthoringResponse,
    CommandFileCreateRequest,
    CommandFileDetail,
    CommandFileExecutionStatus,
    CommandFilesResponse,
    CommandFileUpdateRequest,
    CommandInputFileCopyRequest,
    CommandInputFileResponse,
    CommandInputGrantSuggestion,
    CommandInputPathsRequest,
    CommandInputPathsResponse,
    CommandInputPathStatus,
    CommandOptionsResponse,
    CommandRunRequest,
    CommandRunResponse,
    ConfigStatus,
    DefaultPersonUpdateRequest,
    EnvironmentImagesResponse,
    GrantEvaluation,
    GrantScope,
    HealthResponse,
    HotkeySettings,
    InferenceFailuresResponse,
    IntelligenceConfigResponse,
    IntelligenceConfigUpdateRequest,
    LlmProvidersResponse,
    MemoryEventsResponse,
    ProjectConfigResponse,
    ProjectConfigUpdateRequest,
    ProviderOption,
    RoleOption,
    RoleOptionsResponse,
    RoutineCommandOptionsResponse,
    RuntimeDebugStatus,
    RuntimeDebugUpdateRequest,
    RuntimeStatus,
    ScenarioDiagnosticsResponse,
    SchedulerStartRequest,
    SchedulerStopRequest,
    ServiceKeepAwake,
    ShutdownResponse,
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
)
from guildbotics.app_api.runtime import AppRuntime
from guildbotics.app_api.traces import (
    global_records,
    last_cli_agent_turns,
    latest_inference_failures,
    list_memory_events,
    list_traces,
    trace_detail,
)
from guildbotics.entities.team import Service, Team
from guildbotics.environment.spec import guest_path
from guildbotics.integrations.factory import PROVIDERS
from guildbotics.integrations.provider import Operation, Page, SetupError, page_path
from guildbotics.intelligences.llm_providers import discover_llm_providers
from guildbotics.setup.setup_service import (
    PROJECT_CONFIG_PATHS,
    IdentitySummary,
    PersonConfigSnapshot,
    PersonSetupInput,
    PersonSetupResult,
    ProjectConfigSnapshot,
    ProjectSetupInput,
    ProjectSetupResult,
    SimplePersonSetupService,
    SimpleProjectSetupService,
    identity_config_paths,
    member_config_paths,
    prepare_accounts,
    remove_accounts,
    settle_accounts,
    share_avatar,
    stored_person_config_paths,
)
from guildbotics.utils.fileio import (
    WorkspaceNotConfiguredError,
    get_identity_config_dir,
    get_template_path,
    load_yaml_file,
)
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.keychain import SecretStoreError
from guildbotics.utils.local_api import API_PREFIX, PROOF_PATH, server_port
from guildbotics.utils.person_id import (
    MemberConfigError,
    MemberDirectoryName,
    PersonId,
    is_valid_person_id,
)
from guildbotics.utils.safe_paths import UnsafePathError
from guildbotics.utils.shared_write_lock import SharedWriteBusyError
from guildbotics.utils.workspace_state import (
    registered_workspaces,
    unregister_workspace,
)
from guildbotics.workspace.config_repository import ConfigWriteReceipt


def _request_language(request: Request) -> str:
    """Return the display language the request asked for, defaulting to en."""
    value = (request.headers.get(LANGUAGE_HEADER) or "").strip().lower()
    return "ja" if value.startswith("ja") else "en"


# How long the lifespan teardown may take before the host gives up on it. The
# backend owns this number and hands it to the host with every accepted
# shutdown, so the two cannot disagree about when a teardown is still orderly.
# It has to cover every wait the teardown can run into, each at its limit: a
# forced runtime stop (event listener join, scheduler shutdown, scheduler join,
# work drain) and the diagnostics maintenance join.
TEARDOWN_BUDGET_SECONDS = 60.0

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
    auth: ServerAuth | None = None,
    runtime: AppRuntime | None = None,
    command_input_file_store: CommandInputFileStore | None = None,
    restore_workspace_environment: bool = False,
    workspace_problem: str = "",
) -> FastAPI:
    server_auth = auth or ServerAuth(server_port())
    app_runtime = runtime or AppRuntime(
        EventBus(),
        load_workspace_environment=restore_workspace_environment,
        workspace_problem=workspace_problem,
    )
    bus = app_runtime.event_bus
    store = bus.store
    log_handler = EventBusLogHandler(bus)
    system_service_run_id = app_runtime.system_service_run_id
    input_file_store = command_input_file_store or CommandInputFileStore()
    app_runtime.command_inputs = lambda: input_file_store.directory
    member_api = MemberApi(app_runtime)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger = logging.getLogger("guildbotics")
        uvicorn_error_logger = logging.getLogger("uvicorn.error")
        added_app_handler = False
        added_uvicorn_handler = False
        # The input store owns this retryable refusal, not workspace selection.
        with contextlib.suppress(UnsafePathError):
            input_file_store.start()
        try:
            store.start_system_session(system_service_run_id)
            store.start_maintenance()
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
                    await member_api.close()
                    app_runtime.stop_scheduler(force=True)
                    await app_runtime.close_cli_agent_usage()
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
    app.state.auth = server_auth
    app.state.runtime = app_runtime
    app.state.event_bus = bus
    # The server hosting this app installs the real hook; without one (tests,
    # embedding) there is nothing to stop.
    app.state.request_shutdown = lambda: None

    app.add_middleware(_UnhandledErrorMiddleware)
    # Outermost: nothing runs for a request the guard refuses.
    app.add_middleware(Guard, port=server_auth.port)

    error_responses: dict[int | str, dict[str, Any]] = {
        400: {"model": ApiError},
        401: {"model": ApiError},
        422: {"model": ApiError},
        409: {"model": ApiError},
        500: {"model": ApiError},
    }

    # Every API route is the owner's except the few on ``unauthenticated``,
    # each of which says why it cannot be.
    router = _Routes(
        app,
        {
            "dependencies": [Depends(server_auth.authenticate)],
            "responses": error_responses,
        },
    )
    unauthenticated = _Routes(app, {})

    @app.exception_handler(UnsafePathError)
    async def unsafe_path_handler(
        request: Request, exc: UnsafePathError
    ) -> JSONResponse:
        return _error_response(400, "unsafe_host_path", str(exc), {})

    @app.exception_handler(SecretStoreError)
    async def secret_store_error_handler(
        request: Request, exc: SecretStoreError
    ) -> JSONResponse:
        # A locked keychain or an index that cannot be read stops every route
        # that reaches the store the same way; the reason names the file or
        # the keychain's answer, never a value.
        return _error_response(
            503,
            "secret_store_unavailable",
            api_error_message(
                "secret_store_unavailable", _request_language(request), {"reason": exc}
            ),
            {},
        )

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

    @app.exception_handler(SetupError)
    async def setup_error_handler(request: Request, exc: SetupError) -> JSONResponse:
        return _error_response(
            exc.status_code, exc.code, exc.localized(_request_language(request)), {}
        )

    @app.exception_handler(SharedWriteBusyError)
    async def shared_write_busy_handler(
        request: Request, exc: SharedWriteBusyError
    ) -> JSONResponse:
        # One handler rather than one translation per route: every config
        # change can meet a busy lock, and none of them should have to name it.
        return _error_response(
            503,
            "config_busy",
            api_error_message("config_busy", _request_language(request), {}),
            {},
        )

    @app.exception_handler(WorkspaceNotConfiguredError)
    async def workspace_not_configured_handler(
        _, exc: WorkspaceNotConfiguredError
    ) -> JSONResponse:
        # The exception's own sentence is the reason, passed through verbatim.
        return _error_response(409, "workspace_not_configured", str(exc), {})

    @app.exception_handler(MemberConfigError)
    async def member_config_error_handler(_, exc: MemberConfigError) -> JSONResponse:
        return _error_response(
            400, "member_config_invalid", str(exc), {"path": exc.filename}
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _error_response(
            422,
            "validation_error",
            api_error_message("validation_error", _request_language(request), {}),
            # Request inputs can contain secret fields; validation locations and
            # messages are sufficient for the editor to identify the problem.
            {
                "errors": jsonable_encoder(
                    [
                        {
                            key: value
                            for key, value in error.items()
                            if key not in {"input", "ctx"}
                        }
                        for error in exc.errors()
                    ]
                )
            },
        )

    @router.get("/device/workspaces")
    def device_workspaces() -> list[str]:
        return [str(path) for path in registered_workspaces()]

    @router.delete("/device/workspaces")
    def remove_device_workspace(request: WorkspaceChangeRequest) -> list[str]:
        from guildbotics.utils.safe_paths import inspect_host_path, normalize_host_path

        selected = app_runtime.selected_workspace
        if selected is not None:
            same = normalize_host_path(selected) == normalize_host_path(
                request.workspace_dir
            )
            if not same:
                try:
                    current = inspect_host_path(selected, missing=True)
                    removing = inspect_host_path(request.workspace_dir, missing=True)
                    same = current.contains(removing) and removing.contains(current)
                except UnsafePathError:
                    pass  # Obsolete registrations must remain removable.
            if same:
                raise UnsafePathError(t("safe_paths.active_workspace"))
        unregister_workspace(request.workspace_dir)
        return [str(path) for path in registered_workspaces()]

    @router.post("/member/plan")
    async def member_plan(body: MemberRequest) -> Response:
        return await member_api.execute(body, describe=True)

    @router.post("/member/run")
    async def member_run(body: MemberRequest) -> Response:
        return await member_api.execute(body)

    @router.post("/member/git")
    async def member_git(request: Request) -> Response:
        return await member_api.exchange(request)

    @router.get("/health")
    def health() -> HealthResponse:
        from guildbotics.cli import _resolve_version
        from guildbotics.workspace.identity import (
            ensure_device_identity,
            ensure_workspace_identity,
        )

        return HealthResponse(
            status="ok",
            version=_resolve_version(),
            workspace_id=(
                ensure_workspace_identity().workspace_id
                if app_runtime.get_config_status().workspace is not None
                else None
            ),
            service_instance_id=system_service_run_id,
            workspace=app_runtime.get_config_status().workspace,
            machine_name=ensure_device_identity().display_name,
        )

    @unauthenticated.post(PROOF_PATH)
    def local_api_proof(request: ProofRequest) -> ProofResponse:
        # A client asks this before it trusts the port enough to send its
        # token, and the answer reveals nothing a caller could reuse.
        return server_auth.proof(request)

    @router.post("/session/links", status_code=201)
    def entry_link(request: EntryLinkRequest | None = None) -> EntryLink:
        return server_auth.entry_link(request or EntryLinkRequest())

    @unauthenticated.post("/session")
    def start_session(request: SessionRequest, response: Response) -> SessionResponse:
        # The entry code is the credential: a browser holds nothing else yet.
        cookie, session = server_auth.start_session(request)
        response.set_cookie(
            server_auth.cookie,
            cookie,
            max_age=SESSION_SECONDS,
            path="/",
            httponly=True,
            samesite="strict",
        )
        return session

    @router.get("/session")
    def session(request: Request) -> SessionResponse:
        return server_auth.session(request)

    @router.post("/apps/authorizations", status_code=201)
    def authorize_app(request: AuthorizationRequest) -> AuthorizationResponse:
        return server_auth.authorize(request)

    @unauthenticated.post("/apps/token", status_code=201)
    def app_token(request: AppTokenRequest) -> AppToken:
        # The consent code and the app's PKCE verifier are the credential.
        return server_auth.app_token(request)

    @router.post("/shutdown", status_code=202)
    def shutdown() -> ShutdownResponse:
        """Ask the hosting server to exit through its normal shutdown path.

        The desktop host calls this before it would kill the sidecar, so the
        lifespan teardown (scheduler stop, session finish)
        runs no matter which route the app took to quit. The answer tells the
        host how long that teardown may take, so it does not kill one that is
        still within its own limits.
        """
        app.state.request_shutdown()
        return ShutdownResponse(teardown_budget_seconds=TEARDOWN_BUDGET_SECONDS)

    @router.get("/config/status")
    def config_status() -> ConfigStatus:
        if input_file_store.problem:
            with contextlib.suppress(UnsafePathError):
                input_file_store.start()
        return app_runtime.get_config_status().model_copy(
            update={"input_store_problem": input_file_store.problem}
        )

    @router.post("/workspace")
    def workspace_change(request: WorkspaceChangeRequest) -> ConfigStatus:
        return app_runtime.set_workspace(request.workspace_dir)

    @router.get("/team")
    def team() -> TeamSummary:
        return app_runtime.get_team_summary()

    @router.get("/commands/options")
    def command_options(person: str | None = None) -> CommandOptionsResponse:
        return app_runtime.get_command_options(person)

    @router.get("/commands/routine-options")
    def routine_command_options(
        person: str | None = None,
    ) -> RoutineCommandOptionsResponse:
        return app_runtime.get_routine_command_options(person)

    @router.post("/commands/run")
    async def run_command(request: CommandRunRequest) -> CommandRunResponse:
        return await app_runtime.run_command(request)

    @router.post("/commands/input-files")
    def command_input_file_upload(
        file: UploadFile = File(...),  # noqa: B008
    ) -> CommandInputFileResponse:
        return _store_command_input(lambda: input_file_store.save(file))

    @router.post("/commands/input-files/copy")
    def command_input_file_copy(
        request: CommandInputFileCopyRequest,
    ) -> CommandInputFileResponse:
        return _store_command_input(lambda: input_file_store.copy(request.path))

    def _store_command_input(
        store: Callable[[], Path],
    ) -> CommandInputFileResponse:
        try:
            path = store()
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
        return CommandInputFileResponse(path=path, guest_path=guest_path(path))

    @router.post("/commands/input-paths")
    def command_input_paths(
        request: CommandInputPathsRequest,
    ) -> CommandInputPathsResponse:
        return CommandInputPathsResponse(
            paths=[
                CommandInputPathStatus(
                    path=entry.path,
                    kind=entry.kind,
                    reachable=entry.reachable,
                    guest_path=entry.guest_path,
                    grant=(
                        CommandInputGrantSuggestion(
                            scope=entry.grant.scope, path=entry.grant.path
                        )
                        if entry.grant
                        else None
                    ),
                    problem=entry.problem,
                )
                for entry in describe_command_input_paths(
                    request.paths, command_cwd(request.cwd)
                )
            ]
        )

    @router.post("/commands/author")
    async def author_command(
        request: CommandAuthoringRequest,
    ) -> CommandAuthoringResponse:
        return await app_runtime.author_command(request)

    @router.post("/commands/author/apply")
    def apply_command_authoring(
        request: CommandAuthoringApplyRequest,
    ) -> CommandAuthoringApplyResponse:
        return app_runtime.apply_command_authoring(request)

    @router.get("/commands/files")
    def list_command_files() -> CommandFilesResponse:
        return app_runtime.list_command_files()

    @router.get("/commands/files/{file_id}")
    def get_command_file(file_id: str) -> CommandFileDetail:
        return app_runtime.get_command_file(file_id)

    @router.post("/commands/files")
    def create_command_file(request: CommandFileCreateRequest) -> CommandFileDetail:
        return app_runtime.create_command_file(request)

    @router.put("/commands/files/{file_id}")
    def update_command_file(
        file_id: str, request: CommandFileUpdateRequest
    ) -> CommandFileDetail:
        return app_runtime.update_command_file(file_id, request)

    @router.delete("/commands/files/{file_id}")
    def delete_command_file(
        file_id: str, expected_revision: str
    ) -> CommandFilesResponse:
        return app_runtime.delete_command_file(file_id, expected_revision)

    @router.get("/commands/files/{file_id}/execution-status")
    def command_file_execution_status(
        file_id: str, expected_revision: str, person: str | None = None
    ) -> CommandFileExecutionStatus:
        return app_runtime.get_command_file_execution_status(
            file_id, person, expected_revision
        )

    @router.get("/hotkeys")
    def get_hotkeys() -> HotkeySettings:
        return load_hotkeys()

    @router.put("/hotkeys")
    def put_hotkeys(request: HotkeySettings) -> HotkeySettings:
        saved = save_hotkeys(request)
        # The Desktop of this machine registers them again, whichever screen
        # saved them.
        bus.publish_event("hotkeys.changed")
        return saved

    @router.get("/config/roles")
    def config_roles(
        language: Annotated[str, Query(pattern="^(en|ja)$")] = "ja",
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

    @router.get("/scheduler/status")
    def scheduler_status() -> RuntimeStatus:
        return app_runtime.get_scheduler_status()

    @router.post("/scheduler/start")
    def scheduler_start(request: SchedulerStartRequest) -> RuntimeStatus:
        return app_runtime.start_scheduler(request)

    @router.post("/scheduler/stop")
    def scheduler_stop(request: SchedulerStopRequest | None = None) -> RuntimeStatus:
        on_stop = getattr(app.state, "on_service_stop", None)
        if on_stop is not None:
            on_stop()
        if request is not None and not request.wait:
            # CLI callers wait for the backend PID themselves. Acknowledge
            # before draining, so they can escalate without a blocked request.
            def stop_in_background() -> None:
                try:
                    app_runtime.stop_scheduler(force=request.force)
                except Exception:
                    logger.exception("Background service stop failed")

            threading.Thread(
                target=stop_in_background, name="guildbotics-service-stop", daemon=True
            ).start()
            return app_runtime.get_scheduler_status()
        return app_runtime.stop_scheduler(force=request.force if request else False)

    @router.get("/service/keep-awake")
    def service_keep_awake() -> ServiceKeepAwake:
        return app_runtime.get_service_keep_awake()

    @router.put("/service/keep-awake")
    def service_keep_awake_update(request: ServiceKeepAwake) -> ServiceKeepAwake:
        return app_runtime.update_service_keep_awake(request)

    @router.post("/chat/receive-state/reset")
    def chat_receive_state_reset() -> ChatReceiveResetResponse:
        return app_runtime.reset_chat_receive_state()

    @router.get("/transcripts/settings")
    def transcript_settings() -> TranscriptSettingsStatus:
        return app_runtime.get_transcript_settings()

    @router.put("/transcripts/settings")
    def transcript_settings_update(
        request: TranscriptSettingsUpdateRequest,
    ) -> TranscriptSettingsStatus:
        return app_runtime.update_transcript_settings(request)

    @router.get("/runtime/debug")
    def runtime_debug_status() -> RuntimeDebugStatus:
        return app_runtime.get_runtime_debug_status()

    @router.put("/runtime/debug")
    def runtime_debug_update(request: RuntimeDebugUpdateRequest) -> RuntimeDebugStatus:
        return app_runtime.update_runtime_debug(request)

    @router.post("/verify")
    def verify() -> VerifyResponse:
        return app_runtime.verify()

    @router.get("/system-alerts")
    def system_alerts() -> SystemAlertsResponse:
        return app_runtime.get_system_alerts()

    @router.post("/system-alerts/dismiss")
    def dismiss_system_alert(
        request: SystemAlertDismissRequest,
    ) -> SystemAlertsResponse:
        return app_runtime.dismiss_system_alert(request.alert_id)

    @router.post("/diagnostics/scenario")
    async def diagnostics_scenario(
        person_id: str | None = None,
    ) -> ScenarioDiagnosticsResponse:
        return await app_runtime.run_scenario_diagnostics(person_id=person_id)

    @router.get("/diagnostics/traces")
    def diagnostics_traces(
        source: str | None = None,
        person_id: str | None = None,
        q: str | None = None,
        attr_key: str | None = None,
        attr_value: str | None = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    ) -> TracesResponse:
        return list_traces(
            store,
            source=source,
            person_id=person_id,
            query=q,
            attr_key=attr_key,
            attr_value=attr_value,
            limit=limit,
        )

    @router.get("/diagnostics/traces/{trace_id}")
    def diagnostics_trace_detail(trace_id: str) -> TraceDetailResponse:
        return trace_detail(store, trace_id)

    @router.get("/diagnostics/global")
    def diagnostics_global_records(
        limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    ) -> TraceDetailResponse:
        return global_records(store, limit)

    @router.post("/diagnostics/troubleshoot")
    async def diagnostics_troubleshoot(
        request: TroubleshootingRequest,
    ) -> TroubleshootingResponse:
        return await app_runtime.troubleshoot(request)

    @router.get("/diagnostics/memory-events")
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
    ) -> MemoryEventsResponse:
        return list_memory_events(
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

    @router.get("/activity/history")
    def activity_history(
        start: str | None = None,
        end: str | None = None,
        refresh: bool = False,
        sync_start: str | None = None,
        sync_end: str | None = None,
    ) -> ActivityHistoryResponse:
        return app_runtime.get_activity_history(
            start=start,
            end=end,
            refresh=refresh,
            sync_start=sync_start,
            sync_end=sync_end,
        )

    @router.get("/intelligences/cli-agents/last-turns")
    def cli_agent_last_turns() -> list[CliAgentLastTurn]:
        return last_cli_agent_turns(store)

    @router.get("/intelligences/inference-failures")
    def inference_failures() -> InferenceFailuresResponse:
        return latest_inference_failures(store)

    @router.get("/intelligences/cli-agents/{agent}/usage")
    async def cli_agent_usage(
        agent: str, refresh: bool = False
    ) -> CliAgentUsageResponse:
        return await app_runtime.get_cli_agent_usage(agent, refresh=refresh)

    @router.get("/intelligences/agent-environment")
    def agent_environment_status_view() -> AgentEnvironmentStatusResponse:
        """This device's agent environment, and every active member's slots on it."""
        return app_runtime.get_agent_environment_status()

    @router.post("/intelligences/agent-environment/build")
    def agent_environment_build() -> AgentEnvironmentStatusResponse:
        """Start building this device's snapshot; the status reports its progress."""
        return app_runtime.build_agent_environment()

    @router.get("/intelligences/agent-environment/images")
    def agent_environment_images_view() -> EnvironmentImagesResponse:
        """The images this device holds, for the declaration to name one."""
        return agent_environment_images()

    @router.get("/intelligences/grant-evaluation")
    def grant_evaluation_view(
        scope: GrantScope, path: str, access: str = "read"
    ) -> GrantEvaluation:
        """What a grant the user is about to add would mean on this device."""
        return evaluate_grant(scope, path, access)

    @router.get("/intelligences/model-providers")
    def list_model_providers() -> LlmProvidersResponse:
        config_dir = app_runtime.get_config_status().config_dir
        return LlmProvidersResponse(
            providers=discover_llm_providers(config_dir or get_template_path())
        )

    decisions.register(router, lambda: _resolve_existing_config_dir(app_runtime))

    @router.get("/config/intelligences")
    def config_intelligences(
        person_id: PersonId | None = None,
    ) -> IntelligenceConfigResponse:
        config_dir = _resolve_existing_config_dir(app_runtime)
        revisions = config_repository(config_dir).tree_revisions(
            intelligence_config_dir(person_id)
        )
        response = IntelligenceConfigService().read_config(
            config_dir=config_dir,
            person_id=person_id,
        )
        return response.model_copy(update={"revisions": revisions})

    @router.put("/config/intelligences")
    def config_intelligences_update(
        request: IntelligenceConfigUpdateRequest,
    ) -> ConfigWriteResponse:
        scope = intelligence_config_dir(request.person_id)
        receipt = apply_config_write(
            request.config_dir,
            lambda: IntelligenceConfigService().update_config(request),
            expected=request.expected_revisions,
            report=lambda: config_repository(request.config_dir).tree_revisions(scope),
        )
        return ConfigWriteResponse(
            intelligence={
                "files": [file.model_dump() for file in receipt.result.files]
            },
            revisions=receipt.revisions,
        )

    @router.post("/config/init")
    def config_init(request: ProjectSetupInput) -> ConfigWriteResponse:
        # Nothing to compare against on a first setup, but the write still
        # serializes with every other writer of the workspace's files.
        receipt = apply_config_write(
            request.config_dir,
            lambda: SimpleProjectSetupService().write_project(request),
            report=lambda: config_repository(request.config_dir).revisions(
                PROJECT_CONFIG_PATHS
            ),
        )
        return ConfigWriteResponse(project=receipt.result, revisions=receipt.revisions)

    @router.get("/config/project")
    def config_project() -> ProjectConfigResponse:
        config_dir = _resolve_existing_config_dir(app_runtime)
        # Revisions first: pairing content read earlier with a revision read
        # later would describe a state that never existed, and a save composed
        # against it would slip past the very check it feeds.
        revisions = config_repository(config_dir).revisions(PROJECT_CONFIG_PATHS)
        snapshot: ProjectConfigSnapshot = (
            SimpleProjectSetupService().read_project_config(
                config_dir=config_dir,
            )
        )
        return ProjectConfigResponse.model_validate(
            snapshot.model_dump() | {"revisions": revisions}
        )

    @router.put("/config/project")
    def config_project_update(
        request: ProjectConfigUpdateRequest,
    ) -> ConfigWriteResponse:
        receipt = apply_config_write(
            request.config_dir,
            lambda: SimpleProjectSetupService().update_project(request),
            expected=request.expected_revisions,
            report=lambda: config_repository(request.config_dir).revisions(
                PROJECT_CONFIG_PATHS
            ),
        )
        return ConfigWriteResponse(project=receipt.result, revisions=receipt.revisions)

    @router.put("/config/project/default-person")
    def config_default_person_update(
        request: DefaultPersonUpdateRequest,
    ) -> ConfigWriteResponse:
        config_dir = _resolve_existing_config_dir(app_runtime)
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
        return ConfigWriteResponse(project=receipt.result, revisions=receipt.revisions)

    # A member's save and deletion first reach its providers, outside the
    # shared-write locks, which then block: hence the thread.
    @router.post("/config/members")
    async def config_members(request: PersonSetupInput) -> ConfigWriteResponse:
        """Add a member to the workspace: a new one, or one whose identity
        another workspace already has, as the form read it."""
        status = app_runtime.get_config_status()
        member = request.model_copy(
            update={"config_dir": status.config_dir or request.config_dir}
        )
        # Whatever the form did not read must still not exist: a member of the
        # same ID made meanwhile, from another screen, is not replaced.
        absent = dict.fromkeys(
            (
                *member_config_paths(member.person_id),
                *identity_config_paths(member.person_id),
            ),
            "",
        )
        receipt = await _save_member(member, {**absent, **member.expected_revisions})
        return ConfigWriteResponse(member=receipt.result, revisions=receipt.revisions)

    @router.get("/config/members/{person_id}")
    def config_member(person_id: MemberDirectoryName) -> PersonConfigSnapshot:
        config_dir = _resolve_member_config_dir(app_runtime)
        # The revisions first, as every config read takes them: a change made
        # between the two is then newer than what the form is compared against,
        # and its save is refused rather than written over the change.
        revisions = _member_revisions(config_dir, person_id)
        snapshot: PersonConfigSnapshot = SimplePersonSetupService().read_person_config(
            config_dir=config_dir,
            person_id=person_id,
        )
        return snapshot.model_copy(update={"revisions": revisions})

    @router.put("/config/members/{person_id}")
    async def config_member_update(
        person_id: PersonId, request: PersonSetupInput
    ) -> ConfigWriteResponse:
        if request.person_id != person_id:
            raise AppApiError(
                "person_id_mismatch",
                status_code=400,
            )
        config_dir = _resolve_member_config_dir(app_runtime)
        member = request.model_copy(update={"config_dir": config_dir})
        receipt = await _save_member(member, member.expected_revisions)
        return ConfigWriteResponse(member=receipt.result, revisions=receipt.revisions)

    @router.delete("/config/members/{person_id}")
    async def config_member_delete(
        person_id: MemberDirectoryName,
    ) -> ConfigWriteResponse:
        """Take the member out of the workspace; its identity stays."""
        config_dir = _resolve_member_config_dir(app_runtime)
        receipt = await run_in_threadpool(
            apply_config_write,
            config_dir,
            lambda: SimplePersonSetupService().remove_person(
                config_dir=config_dir,
                person_id=person_id,
            ),
            report=lambda: config_repository(config_dir).revisions(
                stored_person_config_paths(person_id)
            ),
        )
        return ConfigWriteResponse(member=receipt.result, revisions=receipt.revisions)

    @router.get("/identities")
    def identities() -> list[IdentitySummary]:
        """Every member's identity, so that one can be added to the workspace."""
        return SimplePersonSetupService().list_identities(
            config_dir=_resolve_member_config_dir(app_runtime)
        )

    @router.delete("/identities/{person_id}")
    async def identity_delete(person_id: PersonId) -> ConfigWriteResponse:
        """Delete the member: undo what its accounts obtained from each
        provider, take it out of the workspace, then delete its identity."""
        config_dir = _resolve_member_config_dir(app_runtime)
        service = SimplePersonSetupService()
        await remove_accounts(person_id)
        removed = (
            await config_member_delete(person_id)
            if person_id
            in {
                member.person_id
                for member in service.list_person_configs(config_dir=config_dir)
            }
            else None
        )
        receipt = await run_in_threadpool(
            apply_config_write,
            get_identity_config_dir(),
            lambda: service.delete_identity(person_id=person_id),
            report=lambda: _identity_revisions(person_id),
        )
        return ConfigWriteResponse(
            member=receipt.result,
            revisions={**(removed.revisions if removed else {}), **receipt.revisions},
        )

    @router.get("/config/providers")
    def config_providers() -> list[ProviderOption]:
        return [
            ProviderOption(
                name=provider.name,
                title=provider.setup.title,
                kinds=[
                    service.value
                    for service in Service
                    if getattr(provider, service.value) is not None
                ],
                avatar=provider.setup.avatar_url is not None,
                credentials_from=sorted(provider.setup.credentials_from),
            )
            for provider in PROVIDERS.values()
            if provider.setup is not None
        ]

    for provider in PROVIDERS.values():
        setup = provider.setup
        for name, operation in (setup.operations if setup else {}).items():
            router.add_api_route(
                f"/config/providers/{provider.name}/{name}",
                _operation_endpoint(operation, app_runtime.saved_team),
                methods=["POST"],
            )
        # A page is opened by a browser coming back from another site, which
        # sends no SameSite=Strict session cookie; what the page acts on names
        # itself in the query (the unguessable state of a registration, for
        # one).
        for name, page in (setup.pages if setup else {}).items():
            app.add_api_route(
                page_path(provider.name, name),
                _page_endpoint(page),
                methods=["GET"],
                include_in_schema=False,
            )

    @router.get(
        "/config/members/{person_id}/avatar",
        responses={**error_responses, 404: {"model": ApiError}},
    )
    def config_member_avatar(person_id: PersonId) -> Response:
        from guildbotics.app_api.avatar import find_avatar_file

        avatar_path = find_avatar_file(person_id)
        if avatar_path is not None:
            resp = FileResponse(avatar_path)
            resp.headers["Cache-Control"] = "no-cache"
            return resp
        raise AppApiError("avatar_not_found", status_code=404)

    @router.post("/config/members/{person_id}/avatar")
    async def config_member_avatar_upload(
        person_id: PersonId,
        file: UploadFile = File(...),  # noqa: B008
    ) -> AvatarMutationResponse:
        from guildbotics.app_api.avatar import read_upload, store_avatar

        try:
            content, suffix = read_upload(file)
            # The write lock blocks, and this runs on the event loop.
            dest_path = (
                await run_in_threadpool(
                    apply_config_write,
                    get_identity_config_dir(),
                    lambda: store_avatar(person_id, content, suffix),
                )
            ).result
        except ValueError as exc:
            # Validation failures (e.g. too large) carry a safe, stable message.
            raise AppApiError(
                "avatar_invalid", reason=str(exc), status_code=400
            ) from exc
        except AppApiError, SharedWriteBusyError:
            # Both already say what happened, and the catch-all below would
            # bury them under a generic 500.
            raise
        except Exception as exc:
            logger.exception("Failed to save avatar for %s", person_id)
            raise AppApiError(
                "avatar_save_failed",
                status_code=500,
            ) from exc
        await share_avatar(person_id)
        return AvatarMutationResponse(avatar_timestamp=int(dest_path.stat().st_mtime))

    @router.post("/config/members/{person_id}/avatar/{provider}")
    async def config_member_avatar_import(
        person_id: PersonId, provider: str
    ) -> AvatarMutationResponse:
        setup = PROVIDERS[provider].setup if provider in PROVIDERS else None
        if setup is None or setup.avatar_url is None:
            raise AppApiError("avatar_provider_unknown", status_code=404)
        team = app_runtime.saved_team()
        person = next((m for m in team.members if m.person_id == person_id), None)
        if person is None:
            raise AppApiError(
                "person_not_found",
                "person_not_found.plain",
                params={"person": person_id},
            )
        try:
            avatar_url = await setup.avatar_url(person, team)
            dest_path = await _store_downloaded_avatar(person_id, avatar_url)
        except AppApiError, SetupError, SharedWriteBusyError:
            raise
        except Exception as exc:
            logger.exception(
                "Failed to import avatar from %s for %s", provider, person_id
            )
            raise AppApiError(
                "avatar_import_failed",
                params={"provider": setup.title},
                status_code=500,
            ) from exc
        await share_avatar(person_id)
        return AvatarMutationResponse(avatar_timestamp=int(dest_path.stat().st_mtime))

    @app.websocket(API_PREFIX + "/events")
    async def events(websocket: WebSocket):
        await _stream(websocket, server_auth, bus)

    ui.serve(app)
    return app


def _member_revisions(config_dir: Path, person_id: str) -> dict[str, str]:
    """Where a member's two files stand: its identity, and the workspace's file
    of it. The names differ, so one mapping holds both."""
    return {
        **_identity_revisions(person_id),
        **config_repository(config_dir).revisions(
            stored_person_config_paths(person_id)
        ),
    }


def _identity_revisions(person_id: str) -> dict[str, str]:
    """Where a member's identity stands; nothing for a stored directory
    addressed for repair, which no identity can have the name of."""
    if not is_valid_person_id(person_id):
        return {}
    return config_repository(get_identity_config_dir()).revisions(
        identity_config_paths(person_id)
    )


async def _save_member(
    member: PersonSetupInput, expected: Mapping[str, str]
) -> ConfigWriteReceipt[PersonSetupResult]:
    """Save ``member`` between preparing its accounts with their providers,
    which no write waits on, and settling them.

    Its identity and the workspace's file of it are compared and written as
    one write: under the identities' lock and, inside it, the workspace's --
    the order every span that takes both takes them in. Each file's revision
    is read before its own lock is released, so the answer describes what
    this save wrote rather than what a writer did after it.

    A save refused before the write began is settled as refused. One that
    failed after it began may have stored what its accounts obtained, so they
    are not settled: what they obtained stays rather than what is stored
    being undone. The write blocks on the shared-write locks: hence the thread.
    """
    identity_paths = set(identity_config_paths(member.person_id))
    began = False

    def write_workspace() -> PersonSetupResult:
        nonlocal began
        began = True
        return SimplePersonSetupService().save_person(member)

    def write() -> ConfigWriteReceipt[PersonSetupResult]:
        return apply_config_write(
            member.config_dir,
            write_workspace,
            expected={k: v for k, v in expected.items() if k not in identity_paths},
            report=lambda: config_repository(member.config_dir).revisions(
                member_config_paths(member.person_id)
            ),
        )

    try:
        await prepare_accounts(member)
        receipt = await run_in_threadpool(
            lambda: apply_config_write(
                get_identity_config_dir(),
                write,
                expected={k: v for k, v in expected.items() if k in identity_paths},
                report=lambda: _identity_revisions(member.person_id),
            )
        )
    except Exception:
        if not began:
            await settle_accounts(member, saved=False)
        raise
    await settle_accounts(member, saved=True)
    return ConfigWriteReceipt(
        receipt.result.result, {**receipt.revisions, **receipt.result.revisions}
    )


async def _store_downloaded_avatar(person_id: str, url: str) -> Path:
    """Fetch an avatar from a provider and put it in place.

    The download waits on a remote server, so it stays outside the identities'
    shared-write lock; replacing the file is one change to the shared state, so
    it happens inside one. That lock blocks, and this runs on the event loop,
    hence the thread.
    """
    from guildbotics.app_api.avatar import download_avatar, store_avatar

    content, suffix = await download_avatar(url)
    receipt = await run_in_threadpool(
        apply_config_write,
        get_identity_config_dir(),
        lambda: store_avatar(person_id, content, suffix),
    )
    return receipt.result


def _resolve_existing_config_dir(app_runtime: AppRuntime) -> Path:
    status = app_runtime.get_config_status()
    if status.project_file_exists and status.config_dir is not None:
        return status.config_dir
    raise AppApiError(
        "project_not_found",
        context={"project": str(status.project_file)},
        status_code=400,
    )


def _resolve_member_config_dir(app_runtime: AppRuntime) -> Path:
    status = app_runtime.get_config_status()
    if status.config_dir is not None:
        return status.config_dir
    raise AppApiError(
        "project_not_found",
        context={"project": str(status.project_file)},
        status_code=400,
    )


def _operation_endpoint(
    operation: Operation, team: Callable[[], Team]
) -> Callable[[BaseModel], Awaitable[BaseModel]]:
    """An endpoint answering ``operation``, typed by its request and response
    so that they are what the API's schema says."""

    async def endpoint(request: BaseModel) -> BaseModel:
        return await operation.run(request, team)

    endpoint.__annotations__ = {
        "request": operation.request,
        "return": operation.response,
    }
    return endpoint


def _page_endpoint(
    page: Callable[[Mapping[str, str]], Awaitable[Page]],
) -> Callable[[Request], Awaitable[Response]]:
    async def endpoint(request: Request) -> Response:
        shown = await page(request.query_params)
        if shown.redirect:
            return RedirectResponse(shown.redirect, status_code=302)
        response = HTMLResponse(shown.html, status_code=shown.status_code)
        if shown.form_action:
            # The guard keeps a policy the response sets itself.
            response.headers["Content-Security-Policy"] = SECURITY_HEADERS[
                "content-security-policy"
            ].replace("form-action 'self'", f"form-action 'self' {shown.form_action}")
        return response

    return endpoint


class _Routes:
    """The app's API route decorators, each adding the API prefix and the same
    route options.

    The routes are registered on the app itself: FastAPI builds the routes of
    an included ``APIRouter`` again when the app first serves a request, a cost
    every app pays once more.
    """

    def __init__(self, app: FastAPI, options: dict[str, Any]) -> None:
        self._app = app
        self._options = options

    def __getattr__(self, method: str) -> Callable[..., Any]:
        register = getattr(self._app, method)
        return lambda path, *args, **kwargs: register(
            API_PREFIX + path, *args, **{**self._options, **kwargs}
        )


class _UnhandledErrorMiddleware:
    """Answer an unhandled exception with the API's JSON error, inside the guard.

    An ``Exception`` handler registered on the app is served by Starlette's
    outermost ``ServerErrorMiddleware``, above the guard, so its response would
    leave as plain text without the security headers. Building the error here
    keeps the reason readable in the UI.
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


async def _stream(websocket: WebSocket, auth: ServerAuth, bus: EventBus) -> None:
    if not auth.admits(websocket):
        await websocket.close(code=1008)
        return

    await websocket.accept()
    queue = bus.subscribe_events()

    async def send() -> None:
        while True:
            await websocket.send_json(await queue.get())

    sender = asyncio.create_task(send())
    try:
        # Clients never send. Receiving is how a closed socket becomes visible
        # while no event is pending; a handler that only sends would hold the
        # server's shutdown open until the next event.
        while (await websocket.receive())["type"] != "websocket.disconnect":
            pass
    finally:
        sender.cancel()
        with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect):
            await sender
        queue.close()
