"""Record the host's hand-over of a PR at the automatic review limit."""

from guildbotics.entities.task import Task
from guildbotics.integrations.pull_request_patrol import MAX_REVIEW_ROUNDS
from guildbotics.observability.diagnostics_events import record_correlated_event
from guildbotics.utils.correlation import require_trace


def record_review_limit(
    task: Task,
    event_type: str,
    *,
    comment_url: str = "",
    error: BaseException | None = None,
    comment_error: BaseException | None = None,
) -> None:
    """Record one boundary or the shared fact of a completed conversion.

    Only ``pull_request.review_limit`` is shared activity; boundary events
    describe this trace in diagnostics without creating an agent TaskRun.
    Errors carry their type, never provider responses or credentials.
    """
    record_correlated_event(
        event_type=event_type,
        attributes=task.trace_attributes(),
        payload={
            "run_id": require_trace().trace_id,
            "reason": "review_limit",
            "action": "draft" if event_type == "pull_request.review_limit" else "",
            "count": MAX_REVIEW_ROUNDS,
            "pull_request": {
                "url": task.pull_request_url,
                "label": task.label,
                "title": task.title,
            },
            "comment_url": comment_url,
            "error": type(error).__name__ if error is not None else "",
            "comment_error": type(comment_error).__name__ if comment_error else "",
        },
    )
