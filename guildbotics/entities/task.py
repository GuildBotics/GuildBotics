import datetime
from typing import ClassVar

from pydantic import BaseModel, Field

from guildbotics.entities.message import Message


class AwaitedRun(BaseModel):
    """The run that stopped to await a pull request's checks, and the work it
    did, which the run its task starts resumes."""

    run_id: str = Field(..., description="The run that awaited the checks.")
    work_kind: str = Field(..., description="The kind of the work it did.")
    work_identity: str = Field(..., description="The identity of that work.")


class Task(BaseModel):
    """A piece of work a member is selected for: a ticket of the board, or a
    pull request that asks something of the member."""

    # Status constant definitions
    NEW: ClassVar[str] = "new"
    READY: ClassVar[str] = "ready"
    IN_PROGRESS: ClassVar[str] = "in_progress"
    DONE: ClassVar[str] = "done"

    id: str | None = Field(
        default=None, description="The unique identifier for the task."
    )
    url: str | None = Field(
        default=None, description="The URL of the ticket or pull request."
    )
    title: str = Field(..., description="The title of the task.")
    provider: str = Field(
        default="", description="The service provider of the work target."
    )
    label: str = Field(
        default="", description="The short reference the provider shows."
    )
    description: str = Field(..., description="A description of the task.")
    comments: list[Message] = Field(
        default_factory=list, description="Comments associated with the task."
    )
    status: str = Field(
        default=NEW, description='The current status of the task (default is "new").'
    )
    assignee: str | None = Field(
        default=None,
        description="The person_id of the agent currently assigned to the task, if any.",
    )
    pull_request_url: str | None = Field(
        default=None,
        description="The related pull request URL when the task is triggered by PR review state.",
    )
    trigger_reason: str | None = Field(
        default=None,
        description="Short reason why the task was selected for the workflow.",
    )
    created_at: datetime.datetime | None = Field(
        default=None, description="The date and time when the task was created."
    )
    resumes: AwaitedRun | None = Field(
        default=None,
        description="For a pull request whose awaited checks settled: the run "
        "that awaited them, whose work the task resumes.",
    )

    def trace_attributes(self) -> dict[str, str]:
        """Correlation attributes that name this ticket in diagnostics.

        The patrol opens the run's trace with them and the ticket selector
        sets them on a trace opened before selection, so the run names its
        ticket / PR.
        """
        attributes = {
            "target.kind": "pull_request" if self.pull_request_url else "ticket",
            "target.provider": self.provider,
            "target.label": self.label,
        }
        if self.title:
            attributes["target.title"] = self.title
        url = self.pull_request_url or self.url
        if url:
            attributes["target.url"] = url
        return attributes

    def __lt__(self, other: Task) -> bool:
        """
        Compare two tasks based on creation date, oldest first.

        Args:
            other (Task): The other task to compare against.
        Returns:
            bool: True if this task is less than the other task, False otherwise.
        """

        def parse(dt: datetime.datetime | None) -> datetime.datetime:
            """Convert datetime to UTC-aware for consistent comparison."""
            if dt is None:
                # Default to maximum UTC datetime if missing
                return datetime.datetime.max.replace(tzinfo=datetime.UTC)
            if dt.tzinfo is None:
                # Treat naive datetime as UTC
                return dt.replace(tzinfo=datetime.UTC)
            # Normalize any timezone-aware datetime to UTC
            return dt.astimezone(datetime.UTC)

        return parse(self.created_at) < parse(other.created_at)
