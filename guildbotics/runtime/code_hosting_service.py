"""Read resources through the member command window."""

from abc import ABC, abstractmethod
from typing import Any

from guildbotics.runtime.resources import ReadPage


class CodeHostingService(ABC):
    @abstractmethod
    async def read(
        self,
        resource: str,
        subject: str,
        *,
        parameters: dict[str, Any] | None = None,
        continuation: str = "",
    ) -> ReadPage:
        """Read a resource of :data:`~.resources.RESOURCE_SUBJECTS` other than
        the ticket service's; never accept executable queries.

        Args:
            resource: What to read.
            subject: What it is part of: the pull request's or the ticket's
                URL, or the repository's path.
            parameters: The read's conditions, which the host checks.
            continuation: What the previous page of the same subject,
                resource, and conditions returned.
        """

    @abstractmethod
    async def aclose(self) -> None:
        """Release resources owned by this service."""
