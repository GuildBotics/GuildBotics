"""Doubles for running the Desktop assistants' bundled commands for real.

Only the member's agent is replaced: the command runner, the bundled command
and the prompt it invokes run as they do in production, so a test observes
exactly what reaches the agent.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from guildbotics.entities import Person, Project, Team
from guildbotics.intelligences.agent_runtime.environment import (
    current_command_access,
)
from guildbotics.observability import correlation_fields


class ScriptedAgent:
    """A member's agent that records each turn and answers from a script.

    A reply that is an exception is raised instead of returned.
    """

    def __init__(self, response_class: type[BaseModel] | None, *replies: Any) -> None:
        self.response_class = response_class
        self.replies = list(replies)
        self.turns: list[dict[str, Any]] = []

    async def run(self, message: str, **kwargs: Any) -> Any:
        self.turns.append(
            {
                "message": message,
                # A conversion of unstructured output carries neither.
                "execution": kwargs.get("session_state", {}).get(
                    "agent_execution_context"
                ),
                "cwd": kwargs.get("cwd"),
                "access": current_command_access(),
                "correlation": correlation_fields(),
            }
        )
        reply = self.replies[min(len(self.turns), len(self.replies)) - 1]
        if isinstance(reply, BaseException):
            raise reply
        return reply


class AgentContext:
    """A member context whose every brain is ``agent``."""

    def __init__(
        self,
        agent: ScriptedAgent,
        team: Team | None = None,
        person: Person | None = None,
    ) -> None:
        self.agent = agent
        self.team = team or Team(
            project=Project(name="demo", language="en"),
            members=[Person(person_id="bot", name="Bot", is_active=True)],
        )
        self.person = person or self.team.members[0]
        self.pipe = ""
        self.shared_state: dict[str, Any] = {}
        self._invoker: Any = None

    def clone_for(self, person: Person) -> AgentContext:
        clone = AgentContext(self.agent, self.team, person)
        clone.pipe = self.pipe
        return clone

    async def aclose(self) -> None:
        return None

    def set_invoker(self, invoker: Any) -> None:
        self._invoker = invoker

    async def invoke(self, name: str, /, *args: Any, **kwargs: Any) -> Any:
        return await self._invoker(name, *args, **kwargs)

    def update(self, key: str, value: Any, text_value: str) -> None:
        self.shared_state[key] = value
        self.pipe = text_value

    def get_brain(self, name: str, config: Any, class_resolver: Any) -> ScriptedAgent:
        return self.agent
