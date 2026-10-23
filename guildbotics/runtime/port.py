"""What every port of an external service shares: how its operations fail,
the reactions a member adds, and the status a run that did not finish leaves
where its work came from.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError


class ServiceError(RuntimeError):
    """An operation of a service failed. The message is fit for the user: it
    carries neither what the service answered nor a credential."""


class ServiceNotFound(ServiceError):
    """What the operation names is not there, or is not this service's."""


class ServiceUnavailable(ServiceError):
    """No connection was made, so this operation never reached the service.

    Autonomous callers can defer it until a later polling cycle. Failures
    after a request may have arrived must not use this type.
    """


class ServiceUnsupported(ServiceError):
    """The service cannot do the operation at all, as opposed to failing to
    do it this time."""


SemanticReaction = Literal["ack", "agree", "celebrate", "support"]
SEMANTIC_REACTIONS: tuple[SemanticReaction, ...] = (
    "ack",
    "agree",
    "celebrate",
    "support",
)

WorkflowStatusReason = Literal[
    "failed",
    "rate_limited",
    "review_limit",
    "awaiting_checks",
    "checks_settled",
    "asking",
    "blocked",
]
#: What a status's payload names itself, and how it asks to be routed: what
#: carries a status is never work.
_STATUS_KIND = "workflow_error"
_STATUS_ROUTING = "suppress"


class WorkflowStatus(BaseModel):
    """Why a member's run stopped, carried by the comment or the message that
    says so where its work came from, or by the chat message that announces
    that the work waits for a person.

    The service keeps it with the comment or message, in its own form, and
    gives it back on reading; whatever carries a status is not something to
    act on.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: WorkflowStatusReason
    #: The member whose run it reports on.
    person_id: str
    #: The run it reports on; empty for a notice no run made.
    run_id: str
    #: What the run answered: the item's URL, or the chat event's id.
    subject: str = ""
    #: When a rate-limited run can be retried, if known (ISO 8601).
    retry_after_at: str = ""
    #: The provider's own words for that, if any.
    retry_after_text: str = ""
    #: The pull request head whose checks a run that awaits them stopped at.
    head: str = ""

    def payload(self) -> dict[str, str]:
        """The status as its carriers write it: the fields it has, marked as a
        status."""
        fields = {key: value for key, value in self.model_dump().items() if value}
        return {"kind": _STATUS_KIND, "routing": _STATUS_ROUTING, **fields}

    @classmethod
    def from_payload(cls, payload: object) -> WorkflowStatus | None:
        """The status ``payload`` carries; ``None`` when it is none."""
        if not isinstance(payload, dict) or payload.get("kind") != _STATUS_KIND:
            return None
        known = {key: payload[key] for key in cls.model_fields if key in payload}
        try:
            return cls.model_validate({"run_id": "", **known})
        except ValidationError:
            return None

    def holds_selection(self, now: datetime | None = None) -> bool:
        """Whether the work stays out of selection: a failure until someone
        speaks again, a rate limit until its reset (or for good when the reset
        is not known). A wait for checks, and its end, hold nothing: what the
        run awaits resumes it (:mod:`guildbotics.integrations.pull_request_patrol`).
        Nor does the announcement of a run that ended asking or blocked: the
        member's own comment on the item is what holds it."""
        if self.reason == "failed":
            return True
        if self.reason != "rate_limited":
            return False
        try:
            retry_at = datetime.fromisoformat(self.retry_after_at)
        except ValueError:
            return True
        if retry_at.tzinfo is None:
            return True
        return retry_at > (now or datetime.now(UTC))
