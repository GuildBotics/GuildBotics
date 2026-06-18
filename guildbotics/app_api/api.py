from __future__ import annotations

import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, cast

from fastapi import Depends, FastAPI, Header, Query, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from guildbotics.app_api.diagnostics_store import DiagnosticsStore
from guildbotics.app_api.errors import AppApiError
from guildbotics.app_api.events import EventBus, EventBusLogHandler
from guildbotics.app_api.intelligences import IntelligenceConfigService
from guildbotics.app_api.models import (
    AgentFieldStateResponse,
    ApiError,
    CliAgentDetectionsResponse,
    CommandOptionsResponse,
    CommandRunRequest,
    CommandRunResponse,
    ConfigStatus,
    HealthResponse,
    IntelligenceConfigResponse,
    IntelligenceConfigUpdateRequest,
    MemberDeleteRequest,
    MemberResolveRequest,
    MemberResolveResponse,
    ProjectConfigResponse,
    ProjectConfigUpdateRequest,
    ProjectStatusOptionsRequest,
    ProjectStatusOptionsResponse,
    PromptTraceStatus,
    PromptTraceUpdateRequest,
    RoleOption,
    RoleOptionsResponse,
    RoutineOption,
    RoutineOptionsResponse,
    RuntimeDebugStatus,
    RuntimeDebugUpdateRequest,
    RuntimeStatus,
    ScenarioDiagnosticsResponse,
    SchedulerStartRequest,
    TeamSummary,
    TraceDetailResponse,
    TracesResponse,
    VerifyResponse,
    WorkspaceChangeRequest,
)
from guildbotics.app_api.runtime import AppRuntime
from guildbotics.editions.simple.setup_service import (
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
)
from guildbotics.utils.fileio import get_template_path, load_yaml_file

TOKEN_HEADER = "X-GuildBotics-Session-Token"


class ConfigWriteResponse(BaseModel):
    project: ProjectSetupResult | None = None
    member: PersonSetupResult | None = None
    intelligence: dict[str, Any] | None = None


def create_app(
    *,
    session_token: str | None = None,
    runtime: AppRuntime | None = None,
    event_bus: EventBus | None = None,
    diagnostics_store: DiagnosticsStore | None = None,
) -> FastAPI:
    token = session_token or secrets.token_urlsafe(32)
    store = diagnostics_store or DiagnosticsStore()
    bus = event_bus or EventBus(store=store)
    app_runtime = runtime or AppRuntime(bus, diagnostics_store=store)
    log_handler = EventBusLogHandler(bus)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger = logging.getLogger("guildbotics")
        if not any(
            isinstance(handler, EventBusLogHandler) for handler in logger.handlers
        ):
            logger.addHandler(log_handler)
        try:
            yield
        finally:
            app_runtime.stop_scheduler()
            logger.removeHandler(log_handler)

    app = FastAPI(title="GuildBotics App API", version="0.1.0", lifespan=lifespan)
    app.state.session_token = token
    app.state.runtime = app_runtime
    app.state.event_bus = bus

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["tauri://localhost"],
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=[TOKEN_HEADER, "Content-Type"],
    )

    error_responses: dict[int | str, dict[str, Any]] = {
        400: {"model": ApiError},
        401: {"model": ApiError},
        422: {"model": ApiError},
        409: {"model": ApiError},
        500: {"model": ApiError},
    }

    @app.exception_handler(AppApiError)
    async def app_api_error_handler(_, exc: AppApiError) -> JSONResponse:
        return _error_response(exc.status_code, exc.code, exc.message, exc.context)

    @app.exception_handler(Exception)
    async def unexpected_error_handler(_, exc: Exception) -> JSONResponse:
        logging.getLogger("guildbotics").exception("Unhandled app API error")
        return _error_response(
            500,
            "internal_error",
            "An unexpected app API error occurred.",
            {"error_type": type(exc).__name__},
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        _, exc: RequestValidationError
    ) -> JSONResponse:
        return _error_response(
            422,
            "validation_error",
            "Request validation failed.",
            {"errors": exc.errors()},
        )

    def require_token(
        provided: Annotated[str | None, Header(alias=TOKEN_HEADER)] = None,
    ) -> None:
        if provided != token:
            raise AppApiError(
                "invalid_session_token",
                "Invalid session token.",
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
        "/scheduler/routines",
        response_model=RoutineOptionsResponse,
        responses=error_responses,
    )
    def scheduler_routines(_: None = Depends(require_token)) -> RoutineOptionsResponse:
        routines = [
            RoutineOption(
                command=command,
                requires_github=app_runtime.requires_github_for_routine(command),
            )
            for command in app_runtime.get_default_routines()
        ]
        return RoutineOptionsResponse(routines=routines)

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
    def scheduler_stop(_: None = Depends(require_token)) -> RuntimeStatus:
        return app_runtime.stop_scheduler()

    @app.get(
        "/prompt-trace",
        response_model=PromptTraceStatus,
        responses=error_responses,
    )
    def prompt_trace_status(
        limit: Annotated[int, Query(ge=1, le=1000)] = 20,
        path: str | None = None,
        _: None = Depends(require_token),
    ) -> PromptTraceStatus:
        return app_runtime.get_prompt_trace_status(limit=limit, read_path=path)

    @app.put(
        "/prompt-trace",
        response_model=PromptTraceStatus,
        responses=error_responses,
    )
    def prompt_trace_update(
        request: PromptTraceUpdateRequest,
        limit: Annotated[int, Query(ge=1, le=1000)] = 20,
        _: None = Depends(require_token),
    ) -> PromptTraceStatus:
        return app_runtime.update_prompt_trace(request, limit=limit)

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
        "/config/intelligences",
        response_model=IntelligenceConfigResponse,
        responses=error_responses,
    )
    def config_intelligences(
        person_id: str | None = None,
        _: None = Depends(require_token),
    ) -> IntelligenceConfigResponse:
        config_dir = _resolve_existing_config_dir(app_runtime)
        try:
            return IntelligenceConfigService().read_config(
                config_dir=config_dir,
                person_id=person_id,
            )
        except SetupServiceError as exc:
            raise AppApiError(exc.code, exc.message) from exc

    @app.put(
        "/config/intelligences",
        response_model=ConfigWriteResponse,
        responses=error_responses,
    )
    def config_intelligences_update(
        request: IntelligenceConfigUpdateRequest,
        _: None = Depends(require_token),
    ) -> ConfigWriteResponse:
        try:
            result = IntelligenceConfigService().update_config(request)
        except SetupServiceError as exc:
            raise AppApiError(exc.code, exc.message) from exc
        return ConfigWriteResponse(
            intelligence={"files": [file.model_dump() for file in result.files]}
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
        try:
            result = SimpleProjectSetupService().write_project(request)
        except SetupServiceError as exc:
            raise AppApiError(exc.code, exc.message) from exc
        return ConfigWriteResponse(project=result)

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
                "Project config was not found.",
                context={"project": str(status.project_file)},
                status_code=400,
            )
        try:
            snapshot: ProjectConfigSnapshot = (
                SimpleProjectSetupService().read_project_config(
                    config_dir=config_dir,
                    env_file_path=status.env_file,
                )
            )
        except SetupServiceError as exc:
            raise AppApiError(exc.code, exc.message) from exc
        return ProjectConfigResponse.model_validate(snapshot.model_dump())

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
            result = SimpleProjectSetupService().update_project(
                ProjectUpdateInput.model_validate(request.model_dump())
            )
        except SetupServiceError as exc:
            raise AppApiError(exc.code, exc.message) from exc
        return ConfigWriteResponse(project=result)

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
                payload["env_file_path"] = status.env_file
            result = SimplePersonSetupService().write_person(
                PersonSetupInput.model_validate(payload)
            )
        except SetupServiceError as exc:
            raise AppApiError(exc.code, exc.message) from exc
        return ConfigWriteResponse(member=result)

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
                "Project config was not found.",
                context={"project": str(status.project_file)},
                status_code=400,
            )
        try:
            snapshot: PersonConfigSnapshot = (
                SimplePersonSetupService().read_person_config(
                    config_dir=config_dir,
                    person_id=person_id,
                    env_file_path=status.env_file,
                )
            )
        except SetupServiceError as exc:
            raise AppApiError(exc.code, exc.message) from exc
        return snapshot

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
                    "original_person_id must match the path parameter.",
                    status_code=400,
                )
            status = app_runtime.get_config_status()
            payload["config_dir"] = _resolve_existing_config_dir(app_runtime)
            payload["env_file_path"] = status.env_file
            result = SimplePersonSetupService().update_person(
                PersonUpdateInput.model_validate(payload)
            )
        except SetupServiceError as exc:
            raise AppApiError(exc.code, exc.message) from exc
        return ConfigWriteResponse(member=result)

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
        try:
            status = app_runtime.get_config_status()
            result = SimplePersonSetupService().delete_person(
                config_dir=_resolve_existing_config_dir(app_runtime),
                person_id=person_id,
                env_file_path=status.env_file,
            )
        except SetupServiceError as exc:
            raise AppApiError(exc.code, exc.message) from exc
        return ConfigWriteResponse(member=result)

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
            raise AppApiError(exc.code, exc.message) from exc
        return MemberResolveResponse.model_validate(reference.model_dump())

    @app.websocket("/events")
    async def events(websocket: WebSocket, token_query: str = Query(alias="token")):
        await _stream(websocket, token_query, token, bus.subscribe_events)

    @app.websocket("/logs")
    async def logs(websocket: WebSocket, token_query: str = Query(alias="token")):
        await _stream(websocket, token_query, token, bus.subscribe_logs)

    return app


def _resolve_existing_config_dir(app_runtime: AppRuntime) -> Path:
    status = app_runtime.get_config_status()
    config_dir = _get_existing_config_dir(status)
    if config_dir is not None:
        return config_dir
    raise AppApiError(
        "project_not_found",
        "Project config was not found.",
        context={"project": str(status.project_file)},
        status_code=400,
    )


def _get_existing_config_dir(status: ConfigStatus) -> Path | None:
    if status.project_file_exists:
        return status.config_dir
    return None


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
