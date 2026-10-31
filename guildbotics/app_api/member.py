"""Authenticated member CLI transport, independent of turn-scoped MCP."""

from __future__ import annotations

import json
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from shutil import copyfileobj
from typing import IO, Any, Literal

from fastapi import Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from guildbotics.app_api.errors import AppApiError
from guildbotics.app_api.runtime import AppRuntime
from guildbotics.cli import _resolve_version
from guildbotics.cli.member import run_client_git, run_in_process
from guildbotics.cli.member_plan import contains_workspace_option, plan
from guildbotics.environment.member_execution import MemberWorkers
from guildbotics.runtime.member_invocation import MemberInvocation
from guildbotics.utils.correlation import new_id
from guildbotics.utils.fileio import host_temporary_directory, host_temporary_root

MAX_BUNDLE = 1 << 30
MAX_METADATA = 4 << 20
TIMED_OUT = 124


class MemberRequest(BaseModel):
    """Only interactive metadata: callers cannot assign run, lease or guest."""

    model_config = ConfigDict(extra="forbid")
    version: str
    workspace_id: str
    arguments: list[str] = Field(max_length=128)
    stdin: str = Field(default="", max_length=2 << 20)
    interactive_host: str = Field(default="", max_length=1024)
    interactive_thread_key: str = Field(default="", max_length=4096)
    phase: Literal["identity", "record", "known", "push", "tracking"] | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class MemberApi:
    """Own workers until shutdown, including those whose clients disconnected."""

    def __init__(self, runtime: AppRuntime) -> None:
        self.runtime = runtime
        self.workers = MemberWorkers()
        self.closing = False

    async def close(self) -> None:
        self.closing = True
        await self.workers.settle()

    async def execute(
        self,
        body: MemberRequest,
        *,
        describe: bool = False,
        incoming: Path | None = None,
        outgoing: Path | None = None,
        temporary: tempfile.TemporaryDirectory | None = None,
    ) -> StreamingResponse:
        if body.phase is not None and outgoing is None:
            raise AppApiError("member_invalid_request", status_code=422)
        if self.closing:
            raise AppApiError("member_backend_closing", status_code=409)
        if body.version != _resolve_version():
            raise AppApiError("member_version_mismatch", status_code=409)
        if contains_workspace_option(body.arguments):
            raise AppApiError("member_workspace_argument", status_code=422)
        if sum(len(arg.encode()) for arg in body.arguments) > 64 << 10:
            raise AppApiError("member_request_too_large", status_code=413)
        trace = new_id()
        self.runtime._reserve_command(
            trace, exclusive=False, expected_workspace_id=body.workspace_id
        )
        # Allocation belongs to the worker too: every failure releases its reservation.
        stream: IO[bytes] | None = None
        metadata: dict[str, Any] = {}
        deadline = time.monotonic() + 300.0
        invocation = MemberInvocation(
            interactive_host=body.interactive_host,
            interactive_thread_key=body.interactive_thread_key,
            deadline=deadline,
        )

        def download(
            source: IO[bytes], destination: str, attachment: tuple[str, ...]
        ) -> None:
            assert stream is not None
            metadata["download"] = {
                "destination": destination,
                "attachment": attachment,
            }
            copyfileobj(source, stream)
            metadata["size"] = stream.tell()

        def run() -> tuple[int, str, str]:
            nonlocal stream
            # Ownership passes from the worker to the streaming response.
            stream = tempfile.TemporaryFile(dir=host_temporary_root())  # noqa: SIM115
            if describe:
                metadata["plan"] = plan(body.arguments)
                return 0, "", ""
            if body.phase is not None:
                assert outgoing is not None
                result = run_client_git(
                    body.arguments,
                    invocation,
                    body.phase,
                    body.data,
                    incoming,
                    outgoing,
                )
                if outgoing.exists():
                    with outgoing.open("rb") as source:
                        copyfileobj(source, stream)
                    metadata["size"] = stream.tell()
                return result
            return run_in_process(
                body.arguments,
                replace(invocation, download=download),
                cwd=Path("/"),
                stdin=body.stdin,
            )

        def finished() -> None:
            try:
                if temporary is not None:
                    temporary.cleanup()
            finally:
                self.runtime._release_command(trace)

        result = await self.workers.run(run, deadline, on_finished=finished)
        if result.exit_code == TIMED_OUT:
            # The worker still owns its files; do not expose a partial download.
            metadata = {}

        async def chunks():
            try:
                yield (json.dumps({**result.model_dump(), **metadata}) + "\n").encode()
                if result.exit_code != TIMED_OUT and stream is not None:
                    stream.seek(0)
                    while chunk := stream.read(1 << 16):
                        yield chunk
            finally:
                if result.exit_code != TIMED_OUT and stream is not None:
                    stream.close()

        return StreamingResponse(chunks(), media_type="application/octet-stream")

    async def exchange(self, request: Request) -> StreamingResponse:
        """Read one bounded metadata line followed by an optional raw bundle."""
        temporary = host_temporary_directory("guildbotics-member-api-")
        incoming, outgoing = (
            Path(temporary.name) / "in.bundle",
            Path(temporary.name) / "out.bundle",
        )
        header = bytearray()
        parsed = None
        size = 0
        try:
            with incoming.open("wb") as target:
                async for received in request.stream():
                    chunk = received
                    if parsed is None:
                        header.extend(chunk)
                        line, separator, tail = header.partition(b"\n")
                        if len(line) > MAX_METADATA:
                            raise AppApiError(
                                "member_request_too_large", status_code=413
                            )
                        if not separator:
                            continue
                        parsed = MemberRequest.model_validate_json(line)
                        chunk = bytes(tail)
                        header.clear()
                    size += len(chunk)
                    if size > MAX_BUNDLE:
                        raise AppApiError("member_request_too_large", status_code=413)
                    target.write(chunk)
            if parsed is None:
                raise AppApiError("member_invalid_request", status_code=422)
        except ValidationError as exc:
            temporary.cleanup()
            raise AppApiError("member_invalid_request", status_code=422) from exc
        except BaseException:
            temporary.cleanup()
            raise
        try:
            return await self.execute(
                parsed,
                incoming=incoming if size else None,
                outgoing=outgoing,
                temporary=temporary,
            )
        except AppApiError:
            temporary.cleanup()
            raise
