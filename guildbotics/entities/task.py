import calendar
import datetime
import random
import re
from typing import ClassVar, cast

from croniter import croniter  # type: ignore[import]
from pydantic import BaseModel, Field

from guildbotics.entities.message import Message

CRON_FIELD_COUNT = 5
MINUTE_FIELD_INDEX = 0
HOUR_FIELD_INDEX = 1
DAY_OF_MONTH_FIELD_INDEX = 2
MONTH_FIELD_INDEX = 3
DAY_OF_WEEK_FIELD_INDEX = 4


class Task(BaseModel):
    """
    A class representing a task.

    Attributes:
        id (Optional[str]): The unique identifier for the task.
        title (str): The title of the task.
        description (str): A description of the task.
        comments (list[Message]): Comments associated with the task.
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
    DONE: ClassVar[str] = "done"

    id: str | None = Field(
        default=None, description="The unique identifier for the task."
    )
    title: str = Field(..., description="The title of the task.")
    description: str = Field(..., description="A description of the task.")
    comments: list[Message] = Field(
        default_factory=list, description="Comments associated with the task."
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
    assignee: str | None = Field(
        default=None,
        description="The person_id of the agent currently assigned to the task, if any.",
    )
    pull_request_url: str | None = Field(
        default=None,
        description="The related pull request URL when the task is triggered by PR review state.",
    )
    number: int | None = Field(
        default=None,
        description="The GitHub issue/PR number, when the task originates from one.",
    )
    url: str | None = Field(
        default=None,
        description="The GitHub issue/PR html URL, when the task originates from one.",
    )
    trigger_reason: str | None = Field(
        default=None,
        description="Short reason why the task was selected for the workflow.",
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

        d1, d2 = parse(self.due_date), parse(other.due_date)
        c1, c2 = parse(self.created_at), parse(other.created_at)

        return (p1, d1, c1) < (p2, d2, c2)


_DEFAULT_RANGES = [
    (0, 59),  # minute
    (0, 23),  # hour
    (1, 31),  # day of month
    (1, 12),  # month
    (0, 6),  # day of week
]


class ScheduledCommand(BaseModel):
    """
    A class representing a scheduled task.

    Attributes:
        command (str): The command to be executed.
        schedule (str): The schedule for the scheduled task in cron format.
    """

    command: str = Field(..., description="The command to be executed.")
    schedule: str = Field(
        ..., description="The schedule for the scheduled command in cron format."
    )

    def __init__(self, **data):
        super().__init__(**data)
        parts = self.schedule.split()
        if len(parts) != CRON_FIELD_COUNT:
            raise ValueError(f"Exactly {CRON_FIELD_COUNT} fields required")
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

            if idx == MINUTE_FIELD_INDEX:  # minute
                result = result.replace(minute=val)
            elif idx == HOUR_FIELD_INDEX:  # hour
                result = result.replace(hour=val)
            elif idx == DAY_OF_MONTH_FIELD_INDEX:  # day of month
                # Clamp so as not to exceed the last day of the month
                last = calendar.monthrange(result.year, result.month)[1]
                day = min(val, last)
                result = result.replace(day=day)
            elif idx == MONTH_FIELD_INDEX:  # month
                # Clamp day so it does not become invalid after changing the month
                last = calendar.monthrange(result.year, val)[1]
                day = min(result.day, last)
                result = result.replace(month=val, day=day)
            elif idx == DAY_OF_WEEK_FIELD_INDEX:  # day of week (cron: 0=Sunday)
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
        # Return string with command, schedule, next_run and execution status
        return (
            f"ScheduledCommand(command={self.command}, "
            f"schedule={self.schedule}, "
            f"next_run={self._next_random}, "
            f"executed={self._executed})"
        )
