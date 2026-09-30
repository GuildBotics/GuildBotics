"""Provider-independent repository reads available to members and commands."""

from abc import ABC, abstractmethod
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from guildbotics.utils.process_limits import STREAM_READ_LIMIT

# Room for JSON escaping in CLI, member result, and host envelope.
MAX_PAGE_BYTES = STREAM_READ_LIMIT // 16


class RepositoryReadError(RuntimeError):
    """A safe repository read failure, without upstream bodies or credentials."""


class ReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


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
    """One page of dependency alerts, including for a single-item request."""

    items: list[DependencyAlert]
    continuation: str | None = None


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
        """Read dependency_alerts; detail reads accept no parameters or continuation.

        Collection parameters follow DependencyAlertQuery. A continuation belongs
        to the same provider, repository, resource and conditions that returned it.
        """

    @abstractmethod
    async def aclose(self) -> None:
        """Release resources owned by this service."""
