import calendar
import datetime
import random
import re
from typing import ClassVar, Optional, cast

from croniter import croniter  # type: ignore[import]
from pydantic import BaseModel, Field

from guildbotics.entities.message import Message


class Task(BaseModel):
    """
    A class representing a task.

    Attributes:
        id (Optional[str]): The unique identifier for the task.
        title (str): The title of the task.
        description (str): A description of the task.
        comments (list[Message]): Comments associated with the task.
        workflow (str): The workflow associated with the task.
        mode (Optional[str]): The mode of the task deliverable creation process. For example, "edit" mode is used for creating git commits and pull requests.
        status (str): The current status of the task (default is "new").
        role (str | None): The role associated with the task.
        owner (str | None): The owner of the task.
        priority (Optional[int]): The priority level for this assignment.
        created_at (Optional[datetime]): The date and time when the task was created.
        due_date (Optional[datetime]): The date and time when the task is due.
        repository (Optional[str]): The git repository associated with the task.
    """

    # Status constant definitions
    NEW: ClassVar[str] = "new"
    READY: ClassVar[str] = "ready"
    IN_PROGRESS: ClassVar[str] = "in_progress"
    IN_REVIEW: ClassVar[str] = "in_review"
    RETROSPECTIVE: ClassVar[str] = "retrospective"
    DONE: ClassVar[str] = "done"

    OUTPUT_PREFIX: ClassVar[str] = "Output: "

    id: str | None = Field(
        default=None, description="The unique identifier for the task."
    )
    title: str = Field(..., description="The title of the task.")
    description: str = Field(..., description="A description of the task.")
    comments: list[Message] = Field(
        default_factory=list, description="Comments associated with the task."
    )
    workflow: str = Field(
        default="ticket_driven", description="The workflow associated with the task."
    )
    mode: str | None = Field(
        default=None,
        description='The mode of the task deliverable creation process. For example, "edit" mode is used for creating git commits and pull requests.',
    )
    status: str = Field(
        default=NEW, description='The current status of the task (default is "new").'
    )
    role: str | None = Field(
        default=None, description="The role associated with the task."
    )
    owner: str | None = Field(default=None, description="The owner of the task.")
    priority: int | None = Field(
        default=None, description="The priority level for this assignment."
    )
    created_at: datetime.datetime | None = Field(
        default=None, description="The date and time when the task was created."
    )
    due_date: datetime.datetime | None = Field(
        default=None, description="The date and time when the task is due."
    )
    repository: str | None = Field(
        default=None, description="The git repository associated with the task."
    )

    def __lt__(self, other: "Task") -> bool:
        """
        Compare two tasks based on priority, due date, and creation date.
        Args:
            other (Task): The other task to compare against.
        Returns:
            bool: True if this task is less than the other task, False otherwise.
        """
        p1 = self.priority or 9999
        p2 = other.priority or 9999
        if p1 != p2:
            return p1 < p2

        def parse(dt: Optional[datetime.datetime]) -> datetime.datetime:
            """Convert datetime to UTC-aware for consistent comparison."""
            if dt is None:
                # Default to maximum UTC datetime if missing
                return datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)
            if dt.tzinfo is None:
                # Treat naive datetime as UTC
                return dt.replace(tzinfo=datetime.timezone.utc)
            # Normalize any timezone-aware datetime to UTC
            return dt.astimezone(datetime.timezone.utc)

        d1, d2 = parse(self.due_date), parse(other.due_date)
        c1, c2 = parse(self.created_at), parse(other.created_at)

        return (p1, d1, c1) < (p2, d2, c2)

    def find_output_title_and_url_from_comments(
        self, strict: bool = True
    ) -> tuple[str, str]:
        """Find the title and URL from task comments."""
        for comment in self.comments:
            if comment.author_type != Message.ASSISTANT:
                continue
            comment_lines = comment.content.splitlines()
            for line in comment_lines:
                line = line.strip()
                if line.startswith(self.OUTPUT_PREFIX):
                    # line = "Output: [Title](https://example.com)"
                    title_and_url = line[len(self.OUTPUT_PREFIX) :].strip()
                    # title_and_url = "[Title](https://example.com)"
                    if title_and_url.startswith("[") and "](" in title_and_url:
                        title = title_and_url.split("](")[0][1:]
                        url = title_and_url.split("](")[1][:-1]
                        if title and url:
                            return title, url
        # If no title and URL found, raise an error.
        if strict:
            raise ValueError(
                "No page title and URL found in task comments. Please ensure the task has been processed correctly."
            )
        return "", ""


_DEFAULT_RANGES = [
    (0, 59),  # minute
    (0, 23),  # hour
    (1, 31),  # day of month
    (1, 12),  # month
    (0, 6),  # day of week
]


class ScheduledTask(BaseModel):
    """
    A class representing a scheduled task.

    Attributes:
        task (Task): The task information associated with the scheduled task.
        schedule (str): The schedule for the scheduled task in cron format.
    """

    task: Task = Field(
        ..., description="The task information associated with the scheduled task."
    )
    schedule: str = Field(
        ..., description="The schedule for the scheduled task in cron format."
    )

    def __init__(self, **data):
        super().__init__(**data)
        parts = self.schedule.split()
        if len(parts) != 5:
            raise ValueError("Exactly 5 fields required")
        # max_schedule: ?(a-b)->b, ?->default_max
        max_fields = []
        for idx, f in enumerate(parts):
            m = re.match(r"^\?\((\d+)-(\d+)\)$", f)
            if m:
                _, b = m.groups()
                max_fields.append(b)
            elif f == "?":
                _, default_max = _DEFAULT_RANGES[idx]
                max_fields.append(str(default_max))
            else:
                max_fields.append(f)
        self._max_schedule = " ".join(max_fields)

        now = datetime.datetime.now()
        self._next_boundary = croniter(self._max_schedule, now).get_next(
            datetime.datetime
        )
        self._next_random = self._sample_random(self._next_boundary, parts)
        self._executed = False

    def _sample_random(
        self, boundary: datetime.datetime, parts: list[str]
    ) -> datetime.datetime:
        # Replace each field directly based on the boundary
        result = boundary
        for idx, f in enumerate(parts):
            m = re.match(r"^\?\((\d+)-(\d+)\)$", f)
            if f == "?":
                a, b = _DEFAULT_RANGES[idx]
                val = random.randint(a, b)
            elif m:
                a, b = map(int, m.groups())
                val = random.randint(a, b)
            else:
                continue

            if idx == 0:  # minute
                result = result.replace(minute=val)
            elif idx == 1:  # hour
                result = result.replace(hour=val)
            elif idx == 2:  # day of month
                # Clamp so as not to exceed the last day of the month
                last = calendar.monthrange(result.year, result.month)[1]
                day = min(val, last)
                result = result.replace(day=day)
            elif idx == 3:  # month
                # Clamp day so it does not become invalid after changing the month
                last = calendar.monthrange(result.year, val)[1]
                day = min(result.day, last)
                result = result.replace(month=val, day=day)
            elif idx == 4:  # day of week (cron: 0=Sunday)
                # Adjust Python weekday: Mon=0…Sun=6 to cron format
                cron_cur = (result.weekday() + 1) % 7
                diff = (cron_cur - val) % 7
                result = result - datetime.timedelta(days=diff)
        return result

    def should_run(self, now: datetime.datetime) -> bool:
        if not self._executed and now >= self._next_random:
            self._executed = True
            return True
        if now >= self._next_boundary:
            self._next_boundary = cast(
                datetime.datetime,
                croniter(self._max_schedule, self._next_boundary).get_next(
                    datetime.datetime
                ),
            )
            parts = self.schedule.split()
            self._next_random = self._sample_random(self._next_boundary, parts)
            self._executed = False
        return False

    def __str__(self):
        # Return string with task, schedule, next_run and execution status
        return (
            f"ScheduledTask(task={self.task}, "
            f"schedule={self.schedule}, "
            f"next_run={self._next_random}, "
            f"executed={self._executed})"
        )
