from typing import ClassVar, Literal

from pydantic import BaseModel, Field


class DecisionResponse(BaseModel):
    """Generic response model for selection or classification tasks.

    Attributes:
        label (str): The selected or classified label.
        reason (str): Explanation for the decision.
        confidence (float): Confidence score between 0 and 1.
    """

    label: str = Field(..., description="The selected or classified label.")
    reason: str = Field(..., description="Explanation for the decision.")
    confidence: float = Field(..., description="Confidence score between 0 and 1.")


class AgentResponse(BaseModel):
    """Response from the Agent after executing a requested task.

    Attributes:
        status (Literal["done", "asking"]): 'done' if the task is complete, 'asking' if more information is needed from the user.
        message (str): If status is 'done', a summary of the completed task. If 'asking', the question to the user.
    """

    DONE: ClassVar[Literal["done"]] = "done"
    ASKING: ClassVar[Literal["asking"]] = "asking"

    status: Literal["done", "asking"] = Field(
        ...,
        description=(
            "Status of the response. 'done' means the agent has completed its task, "
            "'asking' means it needs more information from the user to proceed."
        ),
    )
    message: str = Field(
        ...,
        description=(
            "If status is 'done', this contains a summary of the completed task. "
            "If status is 'asking', this contains the question for the user."
        ),
    )


class MessageResponse(BaseModel):
    """Response model for a message in the chat channel.

    Attributes:
        content (str): The content of the message.
        author (str): The author of the message.
        author_type (str): The type of the author (User or Assistant).
    """

    content: str = Field(..., description="The content of the message.")
    author: str = Field(..., description="The author of the message.")
    author_type: str = Field(
        ..., description="The type of the author (User or Assistant)."
    )


def find_cli_agent_execution_error(
    exc: BaseException, *, category: str = ""
) -> BaseException | None:
    """Find a CliAgentExecutionError through common wrapper exception chains."""
    from guildbotics.intelligences.agent_runtime.models import (
        CliAgentExecutionError,
    )

    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        current = stack.pop()
        obj_id = id(current)
        if obj_id in seen:
            continue
        seen.add(obj_id)
        if isinstance(current, CliAgentExecutionError) and (
            not category or current.category == category
        ):
            return current
        last_error = getattr(current, "last_error", None)
        if isinstance(last_error, BaseException):
            stack.append(last_error)
        if current.__cause__ is not None:
            stack.append(current.__cause__)
        if current.__context__ is not None:
            stack.append(current.__context__)
    return None
