"""Provider-independent repository reads available to members and commands."""

from abc import ABC, abstractmethod
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from guildbotics.utils.process_limits import STREAM_READ_LIMIT

# Room for JSON escaping in CLI, member result, and host envelope.
MAX_PAGE_BYTES = STREAM_READ_LIMIT // 16
MAX_LOG_TAIL_BYTES = MAX_PAGE_BYTES // 10


class RepositoryReadError(RuntimeError):
    """A safe repository read failure, without upstream bodies or credentials."""


class ReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ReadinessQuery(ReadModel):
    failed_logs: bool = False
    log_tail_bytes: int = Field(default=MAX_LOG_TAIL_BYTES, ge=1, le=MAX_LOG_TAIL_BYTES)


class DependencyAlertQuery(ReadModel):
    state: Literal["open", "resolved", "dismissed"] = "open"
    page_size: int = Field(default=30, ge=1, le=100)


class AdvisoryIdentifier(ReadModel):
    type: str
    value: str


class DependencyAlert(ReadModel):
    """A dependency vulnerability; identifiers and repository names are opaque."""

    id: str
    state: Literal["open", "resolved", "dismissed"]
    url: str | None = None
    package: str | None = None
    ecosystem: str | None = None
    manifest_path: str | None = None
    severity: str | None = None
    identifiers: list[AdvisoryIdentifier] = Field(default_factory=list)
    summary: str | None = None
    description: str | None = None
    affected_versions: str | None = None
    patched_version: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class RepositoryReadPage(ReadModel):
    """One bounded resource page, with a host-observed work target when present."""

    items: list[
        Annotated[DependencyAlert | dict[str, Any], Field(union_mode="left_to_right")]
    ]
    continuation: str | None = None
    target: dict[str, Any] | None = None


class CodeHostingService(ABC):
    @abstractmethod
    async def read(
        self,
        resource: str,
        repo: str,
        *,
        identifier: str = "",
        parameters: dict[str, Any] | None = None,
        continuation: str = "",
    ) -> RepositoryReadPage:
        """Read a host-defined resource; never accept URLs or executable queries.

        dependency_alerts follows DependencyAlertQuery. Issue and pull-request
        resources require their number as identifier; collection reads take
        page_size (1-100) and thread comments also take a thread node identifier.
        Readiness accepts failed_logs and log_tail_bytes (1-65536).
        A continuation belongs to the same provider, repository, resource,
        identifier, and conditions that returned it.
        """

    @abstractmethod
    async def aclose(self) -> None:
        """Release resources owned by this service."""
