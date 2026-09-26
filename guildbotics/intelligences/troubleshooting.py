"""What the troubleshooting assistant returns.

The Desktop diagnostics screen supplies the user's question and whatever they
are currently looking at. The agent gathers its own evidence by reading the
recorded runs and the workspace configuration, which its environment mounts
read-only, and names the executions it used.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class TroubleshootingResult(BaseModel):
    """One assistant answer and the executions it used as evidence."""

    model_config = ConfigDict(extra="forbid")

    message: str
    trace_ids: list[str] = Field(default_factory=list, max_length=10)
