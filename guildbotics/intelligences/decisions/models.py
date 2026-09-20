"""The input and answer contract shared by every judgment engine."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Truth = Literal["true", "false", "unknown"]


class DecisionConfig(BaseModel):
    """The existing brain feature to use, including for offline replay."""

    model_config = ConfigDict(extra="forbid")
    brain: str = "chat_decision"


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["noul", "choice"]
    instructions: str
    criteria: dict[str, str] = Field(default_factory=dict)


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str
    raw: Any
    probabilities: dict[str, float] | None = None
    confidence: float | None = None
    provenance: Literal["jev_distribution", "structured_assertion"]


class Evaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = ""
    answers: dict[str, Answer] = Field(default_factory=dict)
    raw: Any = None
    usage: dict[str, Any] | None = None
    cost: float | None = None
    retries: int | None = None
    error: str = ""


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    route: Literal["agent", "reaction-only", "no-op"]
    reason: str
    reaction: str = ""
